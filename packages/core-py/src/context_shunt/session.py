"""Per-session wiring shared by both adapters.

Adapters normalize host events into ``(tool, args)`` and model calls; everything below -
gate, capture, store, reader, inspect, stats, spill, guard, accounting - lives here so the
two hosts cannot drift apart on semantics.

Capture scope
-------------
Only content that is *actually being withheld* is captured: a read the gate blocked, an
explicitly eligible oversized post-tool candidate, or an external artifact a producer
already persisted and a deployment explicitly authorized this session to adopt. Short
results and ordinary file reads are never pre-stored, so the store never becomes a shadow
copy of the workspace.

Storage failure never becomes passthrough
-----------------------------------------
If the store cannot publish, the original operation stays blocked and the caller gets a
fixed safe error with no handle. There is no path in which a capture failure lets the raw
payload through instead.

Session lifecycle
-----------------
:meth:`ShuntSession.close` revokes handles and is reserved for a *real* session boundary.
An ordinary per-turn event must call :meth:`end_turn` instead, which does nothing but an
opportunistic sweep - on Hermes ``on_session_end`` fires at the end of every
``run_conversation`` call, so closing there would delete the recovery handles the next turn
depends on.
"""

from __future__ import annotations

import contextlib
from typing import Any

from . import envelope as E
from .accounting import (
    Baseline,
    DeliveryBoundary,
    Egress,
    OperationKind,
    ReaderCost,
    compose,
    new_operation_id,
    totals_to_dict,
)
from .artifacts import ArtifactImporter, ArtifactManifest
from .binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from .capability import CapabilityReport
from .clock import Clock, MonotonicClock
from .config import Config
from .errors import ShuntError
from .gate import Decision, GateDecision, PreReadGate, guidance_for
from .guard import OutputGuardError, enforce, enforce_or_fixed, fixed_error
from .inspect import CURSOR_PREFIX, Inspector, decode_cursor, encode_cursor
from .limits import EMITTED_SCHEMA_VERSION
from .metrics import MetricsSink, NullMetrics
from .paths import authorize
from .probe import FileProber
from .provenance import (
    AttributionPolicy,
    Provenance,
    ProvenanceLabel,
    ResultKind,
    TokenMethod,
    deterministic,
)
from .provider import (
    FallbackChainProvider,
    HostBridgeProvider,
    ReaderProvider,
    UnavailableProvider,
)
from .reader import Reader, ReaderResult
from .registry import RegisteredSource, SourceRegistry
from .schema import validate_request
from .snapshot import snapshot_file
from .spill import SpillEngine
from .store import ScopeIdentity, SnapshotStore

_INSPECT_OPERATIONS = frozenset({"inspect"})
_STATS_OPERATIONS = frozenset({"stats"})
#: Envelope schema cap on ``extraction.next_cursor``. The scaffolding measurement assumes
#: a cursor of exactly this length so a real one can never overshoot the budget it set.
_MAX_CURSOR_CHARS = 512


class ShuntSession:
    def __init__(
        self,
        session_id: str,
        config: Config,
        capability: CapabilityReport,
        *,
        provider: ReaderProvider | None = None,
        clock: Clock | None = None,
        metrics: MetricsSink | None = None,
        store: SnapshotStore | None = None,
        identity: ScopeIdentity | None = None,
    ):
        self.session_id = session_id
        self.config = config
        self.capability = capability
        self._clock = clock or MonotonicClock()
        self._metrics = metrics or NullMetrics()
        self._store = store or SnapshotStore(config.cache_root, config.limits)
        self._identity = identity or ScopeIdentity(
            host=capability.host_name or "unknown",
            profile=capability.adapter or "default",
            principal="local",
            session=session_id,
            generation=1,
        )
        self._store.open_scope(self._identity)
        self._registry = SourceRegistry(self._store, self._identity, config.limits)
        self._gate = PreReadGate(FileProber(config.limits), config.limits, self._clock)
        self._provider = provider or UnavailableProvider()
        self._reader = Reader(
            self._registry,
            self._provider,
            limits=config.limits,
            clock=self._clock,
            metrics=self._metrics,
            attribution_policy=config.reader.attribution_policy,
        )
        self._inspector = Inspector(config.limits)
        suma_enabled = config.suma_post_tool.enabled and capability.enabled("suma_post_tool")
        self._spill = SpillEngine(self._registry, limits=config.limits, enabled=suma_enabled)
        # The import boundary is built only when configuration *and* the capability probe
        # agree. A deployment that enabled it without roots never reaches here: the
        # config loader refuses that combination rather than defaulting to allow-all.
        self._import_enabled = config.artifact_import.enabled and capability.enabled(
            "artifact_import"
        )
        self._importer = (
            ArtifactImporter(
                self._registry,
                policy=config.import_path_policy(),
                accepted_schemas=config.artifact_import.accepted_manifest_schemas,
                limits=config.limits,
            )
            if self._import_enabled
            else None
        )

    # -- accessors ---------------------------------------------------------
    @property
    def registry(self) -> SourceRegistry:
        return self._registry

    @property
    def store(self) -> SnapshotStore:
        return self._store

    @property
    def identity(self) -> ScopeIdentity:
        return self._identity

    @property
    def spill(self) -> SpillEngine:
        return self._spill

    @property
    def suma_enabled(self) -> bool:
        return self._spill.enabled

    @property
    def artifact_import_enabled(self) -> bool:
        return self._importer is not None

    # -- gate --------------------------------------------------------------
    def evaluate_tool_call(self, tool: str, args: dict[str, Any]) -> GateDecision:
        if not self.config.gate_enabled:
            return GateDecision(Decision.PASSTHROUGH, form=self._gate.evaluate(tool, args).form)
        decision = self._gate.evaluate(tool, args)
        self._metrics.count(
            "gate_decision",
            {
                "decision": decision.decision.value,
                "form": decision.form.value,
                "reason": decision.reason or "NONE",
            },
        )
        return decision

    def block_envelope(self, request_id: str, decision: GateDecision) -> dict[str, Any]:
        operation_id = new_operation_id()
        env = E.build(
            request_id=request_id,
            status="blocked",
            code=decision.code or "UNCLASSIFIABLE_READ",
            coverage=E.Coverage(upstream_truncated=None),
            retryable=False,
            guidance=guidance_for(decision),
            result_kind=ResultKind.GATE_DECISION,
            provenance=deterministic(ProvenanceLabel.GATE_DECISION),
            accounting_id=operation_id,
            recovery={
                "handles_valid": False,
                "actions": ["INSPECT_HANDLE", "NARROW_SELECTOR"],
            },
        )
        published = enforce_or_fixed(env, self.config.limits)
        self._record(
            operation_id=operation_id,
            kind=OperationKind.GATE_BLOCK,
            envelope=published,
            baseline=Baseline.none(),
            baseline_credited=False,
            reader=ReaderCost.none(),
            boundary=DeliveryBoundary.BLOCK_MESSAGE,
        )
        return published

    # -- capture -----------------------------------------------------------
    def register_path(self, path: str, *, media_type: str | None = None) -> RegisteredSource:
        """Authorize, snapshot and publish one path. Only ever called for withheld content."""
        return self.register_paths([path], media_type=media_type)[0]

    def register_paths(
        self, paths: list[str], *, media_type: str | None = None
    ) -> list[RegisteredSource]:
        """Authorize every path, then publish all handles as one batch or none.

        The whole request is validated before anything is captured: one unsafe, secret,
        binary or oversized path rejects the batch, so a partially authorized multi-source
        capture can never leave usable handles behind.
        """
        if not paths:
            raise ShuntError("INVALID_REQUEST", "NO_SOURCE", retryable=False)
        if len(paths) > self.config.limits.max_sources_per_request:
            raise ShuntError("LIMIT_EXCEEDED", "TOO_MANY_SOURCES", retryable=False)
        policy = self.config.path_policy()
        snapshots = []
        for path in paths:
            authorized = authorize(path, policy)
            hint = media_type or (
                JSON_MEDIA_TYPE if authorized.real.suffix.lower() == ".json" else TEXT_MEDIA_TYPE
            )
            snapshots.append(
                snapshot_file(authorized, limits=self.config.limits, media_type_hint=hint)
            )
        return self._registry.register_batch(self.session_id, snapshots)

    # -- reader ------------------------------------------------------------
    def read(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = str((request or {}).get("request_id") or "req_unknown")
        if not self.config.reader.enabled:
            exc = ShuntError("INVALID_REQUEST", "READER_DISABLED", retryable=False)
            return self._publish_failure(request_id, exc, OperationKind.READ)

        operation_id = new_operation_id()
        result: ReaderResult = self._reader.answer(
            self.session_id, request, accounting_id=operation_id
        )
        published = enforce_or_fixed(result.envelope, self.config.limits)
        refined = bool((request or {}).get("refined"))
        baseline, credited_bytes = self._baseline_for(result.source_ids)
        self._record(
            operation_id=operation_id,
            kind=OperationKind.REFINED_READ if refined else OperationKind.READ,
            envelope=published,
            baseline=baseline,
            baseline_credited=credited_bytes > 0,
            credited_bytes=credited_bytes,
            reader=result.cost,
            boundary=DeliveryBoundary.ENVELOPE,
        )
        return published

    def _baseline_for(self, source_ids: tuple[str, ...]) -> tuple[Baseline, int]:
        """The withheld-payload baseline, and how many of its bytes this read may claim.

        The two differ for a mixed selection: the measurement covers every selected
        source, while the credit covers only those this read newly withheld.
        """
        total = 0
        credited_bytes = 0
        for source_id in source_ids:
            try:
                handle = self._registry.handle(self.session_id, source_id)
            except ShuntError:
                continue
            # The measurement is every selected source, so a read that claims nothing
            # still reports what the payload was worth.
            total += handle.bytes_len
            # The credit is only what this read newly withholds. `credit_baseline` records
            # the claim against the content and returns True exactly once, so a source an
            # earlier read already credited contributes nothing here. Folding the results
            # into a single OR credited the *whole* selection whenever any part of it was
            # new, which inflated the saving on every mixed-source read.
            if self._store.credit_baseline(self._identity, source_id):
                credited_bytes += handle.bytes_len
        if total == 0:
            return Baseline.none(), 0
        return Baseline.withheld_payload(total, limits=self.config.limits), credited_bytes

    # -- inspect -----------------------------------------------------------
    def inspect(self, request: dict[str, Any]) -> dict[str, Any]:
        """Deterministic extraction. No provider is consulted on this path at all."""
        request_id = str((request or {}).get("request_id") or "req_unknown")
        if not self.config.tools.inspect_enabled:
            exc = ShuntError("INVALID_REQUEST", "INSPECT_DISABLED", retryable=False)
            return self._publish_failure(request_id, exc, OperationKind.INSPECT)
        operation_id = new_operation_id()
        try:
            return self._inspect(request, request_id, operation_id)
        except ShuntError as exc:
            return self._publish_failure(
                request_id, exc, OperationKind.INSPECT, operation_id=operation_id
            )

    def _inspect(
        self, request: dict[str, Any], request_id: str, operation_id: str
    ) -> dict[str, Any]:
        validated = validate_request(request, operations=_INSPECT_OPERATIONS)
        source_id = validated["source_id"]
        snapshot_id = validated["snapshot_id"]
        selector = validated["selector"]
        budgets = validated["budgets"]

        entry = self._registry.resolve(self.session_id, source_id)
        if entry.snapshot.snapshot_id != snapshot_id:
            raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")

        key = self._store.cursor_key()
        state = (
            decode_cursor(key, validated["cursor"], source_id, snapshot_id, selector)
            if "cursor" in validated
            else {}
        )

        allowance = self._store.disclosure_allowance(self._identity, source_id)
        requested_budget = int(budgets["max_result_bytes"])
        budget = min(requested_budget, allowance.remaining)
        clipped_by_allowance = allowance.remaining < requested_budget
        handles = [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "media_type": entry.snapshot.media_type,
                "bytes": entry.snapshot.bytes_len,
                "expires_at": E.iso_expiry(entry.expires_at_epoch),
            }
        ]

        if allowance.exhausted or budget <= 0:
            return self._disclosure_exhausted(
                request_id, operation_id, entry, selector, handles, allowance
            )

        extraction = self._inspector.extract(
            entry.snapshot.data,
            entry.snapshot.line_index,
            selector,
            max_result_bytes=budget,
            max_scan_lines=int(budgets["max_scan_lines"]),
            max_wire_bytes=self._extraction_wire_budget(
                request_id, operation_id, entry, selector, handles
            ),
            state=state,
        )
        if extraction.stalled:
            # The page emitted nothing *and* the cursor did not move, so continuing would
            # loop forever. A scan-budget stop is not this case: it emits nothing but does
            # advance the scan position, and is reported as an honest empty page.
            if extraction.stall_reason == "wire":
                # This unit cannot fit any envelope, whatever the allowance says. Calling it
                # a disclosure problem would send the caller to a remedy that never works.
                raise ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_WIRE_BUDGET", retryable=False)
            if clipped_by_allowance:
                return self._disclosure_exhausted(
                    request_id, operation_id, entry, selector, handles, allowance
                )
            raise ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_PAGE_BUDGET", retryable=False)

        next_cursor = (
            encode_cursor(key, source_id, snapshot_id, selector, extraction.next_cursor_state)
            if extraction.next_cursor_state is not None
            else None
        )
        coverage = E.Coverage(upstream_truncated=False, complete=extraction.complete)
        if not extraction.complete:
            coverage.omit(
                source_id,
                _omission_selector(selector),
                "SCAN_BUDGET_EXHAUSTED"
                if extraction.scan_budget_exhausted
                else "UNKNOWN_REMAINDER",
            )

        def compose(source_used: int, session_used: int, limit_reached: bool) -> dict[str, Any]:
            block: dict[str, Any] = {
                "mode": extraction.mode,
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "deterministic": True,
                "segments": [segment.to_dict() for segment in extraction.segments],
                "result_bytes": extraction.result_bytes,
                "complete": extraction.complete,
                "next_cursor": next_cursor,
                "lines_scanned": extraction.lines_scanned,
                "scan_budget_exhausted": extraction.scan_budget_exhausted,
                "disclosed_bytes_source": source_used,
                "disclosed_bytes_session": session_used,
                "disclosure_limit_reached": limit_reached,
            }
            if extraction.matches_found is not None:
                block["matches_found"] = extraction.matches_found
            return E.build(
                request_id=request_id,
                status="ok" if extraction.complete else "partial",
                code="EXTRACTED",
                coverage=coverage,
                sources=handles,
                retryable=False,
                result_kind=ResultKind.DETERMINISTIC_EXTRACTION,
                provenance=deterministic(ProvenanceLabel.DETERMINISTIC_EXTRACTION),
                accounting_id=operation_id,
                extraction=block,
            )

        # Guard the page *before* charging for it, so a refusal cannot consume allowance the
        # caller never receives. The probe carries the widest values the three disclosure
        # counters can legally take, and `false` for the flag because it is the longer of the
        # two literals; every other field is the one that will actually be published. The
        # published envelope is therefore never larger than the probe and never differs from
        # it anywhere the guard looks, so a probe that passes cannot become a failure below.
        limits = self.config.limits
        try:
            enforce(
                compose(
                    limits.disclosure_max_per_source_bytes,
                    limits.disclosure_max_per_session_bytes,
                    False,
                ),
                limits,
            )
        except OutputGuardError as exc:
            raise ShuntError("LIMIT_EXCEEDED", "EXTRACTION_REFUSED", retryable=False) from exc

        # Check-and-increment before a byte is returned: a concurrent inspect that
        # consumed the allowance in the meantime causes this page to disclose nothing.
        charge = self._store.charge_disclosure(
            self._identity, source_id, extraction.mode, extraction.result_bytes
        )
        if not charge.granted:
            return self._disclosure_exhausted(
                request_id, operation_id, entry, selector, handles, allowance
            )

        env = compose(
            charge.disclosed_bytes_source, charge.disclosed_bytes_session, charge.limit_reached
        )
        published = enforce_or_fixed(env, self.config.limits)
        self._record(
            operation_id=operation_id,
            kind=OperationKind.INSPECT,
            envelope=published,
            # An inspect page discloses rather than withholds, so it claims no baseline
            # saving and its envelope shows up as pure overhead.
            baseline=Baseline.none(),
            baseline_credited=False,
            reader=ReaderCost.none(),
            boundary=DeliveryBoundary.EXTRACTION,
        )
        return published

    def _extraction_wire_budget(
        self,
        request_id: str,
        operation_id: str,
        entry: RegisteredSource,
        selector: dict[str, Any],
        handles: list[dict[str, Any]],
    ) -> int:
        """Serialized room left for segment text once the envelope around it is paid for.

        Measured rather than reserved as a constant. The scaffolding is not fixed: an
        omission echoes the caller's selector, and a ``search`` selector carries a
        caller-supplied needle, so a constant sized against a short needle would
        under-budget a long one. Everything here is set to its most expensive legal shape -
        incomplete coverage with an omission, a match count present, a maximum-length
        cursor, both counters at their caps - so the real envelope is never larger than
        what this measured.
        """
        limits = self.config.limits
        coverage = E.Coverage(upstream_truncated=False, complete=False)
        coverage.omit(entry.source_id, _omission_selector(selector), "SCAN_BUDGET_EXHAUSTED")
        skeleton = E.build(
            request_id=request_id,
            status="partial",
            code="EXTRACTED",
            coverage=coverage,
            sources=handles,
            retryable=False,
            result_kind=ResultKind.DETERMINISTIC_EXTRACTION,
            provenance=deterministic(ProvenanceLabel.DETERMINISTIC_EXTRACTION),
            accounting_id=operation_id,
            extraction={
                "mode": selector.get("kind", "lines"),
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "deterministic": True,
                "segments": [],
                "result_bytes": limits.max_extraction_bytes,
                "complete": False,
                "next_cursor": CURSOR_PREFIX + "c" * (_MAX_CURSOR_CHARS - len(CURSOR_PREFIX)),
                "lines_scanned": limits.inspect_max_scan_lines,
                "scan_budget_exhausted": True,
                "matches_found": limits.inspect_max_search_matches,
                "disclosed_bytes_source": limits.disclosure_max_per_source_bytes,
                "disclosed_bytes_session": limits.disclosure_max_per_session_bytes,
                "disclosure_limit_reached": False,
            },
        )
        return limits.max_extended_envelope_bytes - E.serialized_bytes(skeleton)

    def _disclosure_exhausted(
        self,
        request_id: str,
        operation_id: str,
        entry: RegisteredSource,
        selector: dict[str, Any],
        handles: list[dict[str, Any]],
        allowance,
    ) -> dict[str, Any]:
        coverage = E.Coverage(upstream_truncated=False)
        coverage.omit(entry.source_id, _omission_selector(selector), "DISCLOSURE_EXHAUSTED")
        block = {
            "mode": selector.get("kind", "lines"),
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "deterministic": True,
            "segments": [],
            "result_bytes": 0,
            "complete": False,
            "next_cursor": None,
            "lines_scanned": 0,
            "scan_budget_exhausted": False,
            "disclosed_bytes_source": (
                self.config.limits.disclosure_max_per_source_bytes - allowance.per_source_remaining
            ),
            "disclosed_bytes_session": (
                self.config.limits.disclosure_max_per_session_bytes
                - allowance.per_session_remaining
            ),
            "disclosure_limit_reached": True,
        }
        env = E.build(
            request_id=request_id,
            status="partial",
            code="DISCLOSURE_EXHAUSTED",
            coverage=coverage,
            sources=handles,
            retryable=False,
            result_kind=ResultKind.DETERMINISTIC_EXTRACTION,
            provenance=deterministic(ProvenanceLabel.DETERMINISTIC_EXTRACTION),
            accounting_id=operation_id,
            extraction=block,
            recovery=E.recovery_for("DISCLOSURE_EXHAUSTED", handles_valid=True),
        )
        published = enforce_or_fixed(env, self.config.limits)
        self._record(
            operation_id=operation_id,
            kind=OperationKind.INSPECT,
            envelope=published,
            baseline=Baseline.none(),
            baseline_credited=False,
            reader=ReaderCost.none(),
            boundary=DeliveryBoundary.EXTRACTION,
        )
        return published

    # -- stats -------------------------------------------------------------
    def stats(self, request: dict[str, Any]) -> dict[str, Any]:
        """Read-only session aggregate. It cannot reset, retain, widen or cross sessions."""
        request_id = str((request or {}).get("request_id") or "req_unknown")
        if not self.config.tools.stats_enabled:
            exc = ShuntError("INVALID_REQUEST", "STATS_DISABLED", retryable=False)
            return self._publish_failure(request_id, exc, OperationKind.STATS)
        operation_id = new_operation_id()
        try:
            validated = validate_request(request, operations=_STATS_OPERATIONS)
        except ShuntError as exc:
            return self._publish_failure(
                request_id, exc, OperationKind.STATS, operation_id=operation_id
            )

        page = int(validated.get("page", 1))
        page_size = int(validated.get("page_size", self.config.limits.stats_max_records_per_page))
        page_size = min(page_size, self.config.limits.stats_max_records_per_page)
        total = self._store.operation_count(self._identity)
        records = self._store.operation_page(self._identity, page=page, page_size=page_size)
        consumed = (page - 1) * page_size + len(records)
        next_page = (
            page + 1 if consumed < total and page < self.config.limits.stats_max_pages else None
        )

        block = {
            "scope": "session",
            "totals": totals_to_dict(self._store.operation_totals(self._identity)),
            "records": [record.to_dict() for record in records],
            "page": page,
            "page_size": page_size,
            "total_records": total,
            "next_page": next_page,
        }
        env = E.build(
            request_id=request_id,
            status="ok",
            code="STATS",
            coverage=E.Coverage(complete=True, upstream_truncated=False),
            retryable=False,
            result_kind=ResultKind.STATS,
            provenance=deterministic(ProvenanceLabel.SESSION_METRICS),
            accounting_id=operation_id,
            stats=block,
        )
        published = enforce_or_fixed(env, self.config.limits)
        self._record(
            operation_id=operation_id,
            kind=OperationKind.STATS,
            envelope=published,
            baseline=Baseline.none(),
            baseline_credited=False,
            reader=ReaderCost.none(),
            boundary=DeliveryBoundary.ENVELOPE,
        )
        return published

    # -- external artifact import -----------------------------------------
    def import_artifact(
        self,
        request_id: str,
        *,
        manifest_path: str | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Adopt one external producer artifact and return a pointer envelope.

        Exactly one manifest form: a path to a manifest file inside an import root, or a
        document the caller already parsed. Both are equally untrusted; the path form
        additionally has to survive the path policy before it can be read at all.

        Failure is always a bounded envelope with no handle and no payload. There is no
        code path here that returns the artifact's bytes to the caller - reaching them is
        the reader's or the inspector's job, through the handle this returns.
        """
        if self._importer is None:
            exc = ShuntError("INVALID_REQUEST", "ARTIFACT_IMPORT_DISABLED", retryable=False)
            return self._publish_failure(request_id, exc, OperationKind.CAPTURE)
        if (manifest_path is None) == (manifest is None):
            exc = ShuntError("INVALID_REQUEST", "MANIFEST_SOURCE_AMBIGUOUS", retryable=False)
            return self._publish_failure(request_id, exc, OperationKind.CAPTURE)

        operation_id = new_operation_id()
        try:
            normalized: ArtifactManifest = (
                self._importer.read_manifest_file(manifest_path)
                if manifest_path is not None
                else self._importer.normalize(manifest)
            )
            outcome = self._importer.adopt(self.session_id, normalized)
        except ShuntError as exc:
            self._metrics.count("artifact_import", {"result": "refused", "code": exc.code})
            return self._publish_failure(
                request_id, exc, OperationKind.CAPTURE, operation_id=operation_id
            )
        except Exception:
            # Anything unclassified is reported as a store failure with no detail, for
            # the same reason the spill path does: an unexpected exception must not
            # become a channel for text the caller never validated.
            safe = ShuntError("STORE_FAILED", "IMPORT_FAILED", retryable=False)
            self._metrics.count("artifact_import", {"result": "refused", "code": safe.code})
            return self._publish_failure(
                request_id, safe, OperationKind.CAPTURE, operation_id=operation_id
            )

        entry = outcome.source
        expires_at = E.iso_expiry(entry.expires_at_epoch)
        env = E.build(
            request_id=request_id,
            status="ok",
            code="IMPORTED",
            coverage=E.Coverage(upstream_truncated=normalized.upstream_truncated),
            sources=[
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "media_type": entry.snapshot.media_type,
                    "bytes": outcome.byte_count,
                    "expires_at": expires_at,
                }
            ],
            retryable=False,
            pointer={
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "bytes": outcome.byte_count,
                "expires_at": expires_at,
                "internal": True,
            },
            import_receipt=outcome.receipt,
            result_kind=ResultKind.POINTER,
            provenance=deterministic(ProvenanceLabel.POINTER_ONLY),
            accounting_id=operation_id,
            guidance=(
                "A large tool-result artifact was adopted without entering this "
                "conversation. Ask the context-shunt reader a question about this handle "
                "for a cited answer, or use context_shunt_inspect for exact lines."
            ),
        )
        published = enforce_or_fixed(env, self.config.limits)
        baseline = (
            # A producer that already shortened the payload only lets us observe the
            # shortened size; crediting the full artifact there would be invented.
            Baseline.host_truncated(outcome.byte_count, limits=self.config.limits)
            if normalized.upstream_truncated
            else Baseline.withheld_payload(outcome.byte_count, limits=self.config.limits)
        )
        credited = self._store.credit_baseline(self._identity, entry.source_id)
        self._record(
            operation_id=operation_id,
            kind=OperationKind.CAPTURE,
            envelope=published,
            baseline=baseline,
            baseline_credited=credited,
            reader=ReaderCost.none(),
            boundary=DeliveryBoundary.POINTER,
        )
        self._metrics.count("artifact_import", {"result": "imported", "code": "IMPORTED"})
        return published

    # -- optional Suma post-tool ------------------------------------------
    def post_tool_result(
        self,
        request_id: str,
        result: Any,
        *,
        internal_source_id: str | None = None,
        upstream_truncated: bool = False,
    ):
        """Only ever consulted when the capability probe proved the host order is safe."""
        if not self.suma_enabled:
            return None
        operation_id = new_operation_id()
        outcome = self._spill.evaluate(
            self.session_id, request_id, result, internal_source_id=internal_source_id
        )
        if outcome.envelope is not None:
            envelope = enforce_or_fixed(outcome.envelope, self.config.limits)
            outcome = type(outcome)(
                action=outcome.action,
                envelope=envelope,
                code=outcome.code,
                bytes_measured=outcome.bytes_measured,
                source_id=outcome.source_id,
            )
            baseline = (
                # A host that already truncated the upstream result only lets us observe
                # the truncated size; crediting the full payload there would be invented.
                Baseline.host_truncated(outcome.bytes_measured, limits=self.config.limits)
                if upstream_truncated
                else Baseline.withheld_payload(outcome.bytes_measured, limits=self.config.limits)
            )
            credited = bool(
                outcome.source_id and self._store.credit_baseline(self._identity, outcome.source_id)
            )
            self._record(
                operation_id=operation_id,
                kind=OperationKind.SPILL,
                envelope=envelope,
                baseline=baseline,
                baseline_credited=credited,
                reader=ReaderCost.none(),
                boundary=(
                    DeliveryBoundary.POINTER
                    if outcome.action == "spill"
                    else DeliveryBoundary.ENVELOPE
                ),
            )
        self._metrics.count("suma_outcome", {"result": outcome.action})
        return outcome

    # -- accounting --------------------------------------------------------
    def _record(
        self,
        *,
        operation_id: str,
        kind: OperationKind,
        envelope: dict[str, Any],
        baseline: Baseline,
        baseline_credited: bool,
        reader: ReaderCost,
        boundary: DeliveryBoundary,
        credited_bytes: int | None = None,
    ) -> None:
        """Measure the exact serialized egress, then write the record.

        The envelope already carries only the opaque ``accounting_id``, so measuring it
        here cannot be self-referential: the numbers derived from the measurement live in
        the store, never inside the thing being measured.
        """
        egress = Egress(boundary=boundary, byte_count=E.serialized_bytes(envelope))
        record = compose(
            operation_id=operation_id,
            kind=kind,
            status=str(envelope.get("status", "error")),
            code=str(envelope.get("code", "LIMIT_EXCEEDED")),
            baseline=baseline,
            baseline_credited=baseline_credited,
            credited_bytes=credited_bytes,
            reader=reader,
            egress=egress,
            limits=self.config.limits,
        )
        try:
            self._store.record_operation(self._identity, record)
        except ShuntError:
            # Losing a metric must never fail the caller's operation, and it must never
            # be papered over as a zero: the operation simply has no record.
            self._metrics.count("accounting_dropped", {"stage": kind.value})

    def _publish_failure(
        self,
        request_id: str,
        exc: ShuntError,
        kind: OperationKind,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        operation_id = operation_id or new_operation_id()
        env = E.error_envelope(request_id, exc, accounting_id=operation_id)
        published = enforce_or_fixed(env, self.config.limits)
        self._record(
            operation_id=operation_id,
            kind=kind,
            envelope=published,
            baseline=Baseline.none(),
            baseline_credited=False,
            reader=ReaderCost.none(),
            boundary=DeliveryBoundary.ENVELOPE,
        )
        return published

    def safe_error(self, request_id: str, code: str = "STORE_FAILED") -> dict[str, Any]:
        """The fixed reply for a failure the session could not classify. Carries no handle."""
        return fixed_error(request_id, code)

    # -- lifecycle ---------------------------------------------------------
    def end_turn(self) -> None:
        """An ordinary turn boundary. Handles survive; TTL and the sweep do the work.

        This is what a per-turn host event must call. Hermes fires ``on_session_end`` at
        the end of every ``run_conversation`` call (``agent/turn_finalizer.py``), and
        OpenClaw fires ``session_end`` with ``reason: "compaction"`` mid-conversation -
        destroying handles at either point would delete exactly the recovery state the
        next turn needs.
        """
        with contextlib.suppress(ShuntError):
            self._store.sweep()

    def close(self) -> None:
        """A real session boundary: revoke this scope's handles and drop its artifacts."""
        with contextlib.suppress(ShuntError):
            self._registry.expire_session(self.session_id)

    def reset(self, generation: int) -> ShuntSession:
        """Start a new generation. Every handle from the old one stops resolving."""
        self.close()
        return ShuntSession(
            self.session_id,
            self.config,
            self.capability,
            provider=self._provider,
            clock=self._clock,
            metrics=self._metrics,
            store=self._store,
            identity=ScopeIdentity(
                host=self._identity.host,
                profile=self._identity.profile,
                principal=self._identity.principal,
                session=self._identity.session,
                generation=generation,
            ),
        )


def _omission_selector(selector: dict[str, Any]) -> dict[str, Any]:
    """Map an inspect selector onto the envelope's locator union.

    The envelope locator has no ``bytes`` or ``needle`` form - deliberately, because an
    omission record is metadata and must not carry a caller's search string. A byte or
    search selector is reported as the scope it addressed, never as its text.
    """
    kind = selector.get("kind")
    if kind == "lines":
        return {"kind": "lines", "start": int(selector["start"]), "end": int(selector["end"])}
    return {"kind": "all"}


def build_provider(
    config: Config,
    call,
    *,
    fallback_calls: dict[str, Any] | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> ReaderProvider:
    """Assemble the configured provider, including an availability-only fallback chain.

    ``call`` is the host bridge. ``fallback_calls`` maps a ``"provider/model"`` key to a
    bridge for that target when the host needs a different callable per target; when it is
    absent the same bridge is reused with a different requested target, which is the
    normal case for a host that owns its own routing.

    ``model`` and ``provider`` override the *primary* target only. Hermes needs this: its
    canonical reader configuration lives in the host's ``auxiliary.context_shunt_reader``
    block, which takes precedence over the plugin's own. The fallback chain is not
    overridable that way, because the host has no equivalent block for it.
    """
    primary = HostBridgeProvider(
        call,
        config.limits,
        config.reader.model if model is None else model,
        provider=config.reader.provider if provider is None else provider,
    )
    if not config.reader.fallback_chain:
        return primary
    alternatives = [
        HostBridgeProvider(
            (fallback_calls or {}).get(f"{ref.provider}/{ref.model}", call),
            config.limits,
            ref.model,
            provider=ref.provider,
        )
        for ref in config.reader.fallback_chain
    ]
    # The chain checks each constituent's usage against the *configured* ceilings, so it
    # is given the same limits every candidate was built with.
    return FallbackChainProvider(primary, alternatives, config.limits)


__all__ = [
    "AttributionPolicy",
    "EMITTED_SCHEMA_VERSION",
    "Provenance",
    "ShuntSession",
    "TokenMethod",
    "build_provider",
]
