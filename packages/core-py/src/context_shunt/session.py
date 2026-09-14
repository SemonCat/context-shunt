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
from dataclasses import replace
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
from .aggregate import aggregate_snapshot
from .artifacts import ArtifactImporter, ArtifactManifest
from .binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from .capability import CapabilityReport
from .clock import Clock, MonotonicClock
from .config import Config
from .errors import ShuntError, fallback_allowed
from .fallback import compact_failure, fit_compaction
from .gate import Decision, GateDecision, PreReadGate, guidance_for
from .guard import OutputGuardError, enforce, enforce_or_fixed, fixed_error
from .inspect import CURSOR_PREFIX, Inspector, decode_cursor, encode_cursor
from .legacy_compact import compact_tool_result
from .limits import EMITTED_SCHEMA_VERSION
from .metrics import MetricsSink, NullMetrics
from .paths import assert_no_secret, authorize
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
from .snapshot import Snapshot, record_count, resolve_pointer, snapshot_file
from .spill import SpillEngine, SpillOutcome
from .store import ScopeIdentity, SnapshotStore

_INSPECT_OPERATIONS = frozenset({"inspect"})
_STATS_OPERATIONS = frozenset({"stats"})
#: Envelope schema cap on ``extraction.next_cursor``. The scaffolding measurement assumes
#: a cursor of exactly this length so a real one can never overshoot the budget it set.
_MAX_CURSOR_CHARS = 512
_INSPECT_RECOVERY_GUIDANCE = (
    "Use context_shunt_inspect with the exact retained source_id/snapshot_id pair. For a "
    'minified one-line source, first use selector={"kind":"search","needle":"<literal>",'
    '"max_matches":5,"context_lines":0}. Then request only needed surrounding bytes with a '
    'bytes selector={"kind":"bytes","start":<0-based UTF-8 boundary>,"end":<exclusive UTF-8 '
    "boundary>}. To continue a page, resend the identical "
    "selector plus extraction.next_cursor; do not restart lines 1..1 without its cursor."
)
_SEARCH_WINDOW_GUIDANCE = (
    "Oversized search hit: exact byte window only; surrounding context is omitted. "
    + _INSPECT_RECOVERY_GUIDANCE
)


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
            enforce_output_caps=config.reader.enforce_output_caps,
        )
        self._inspector = Inspector(config.limits)
        tool_result_capture_enabled = config.tool_result_capture.enabled and capability.enabled(
            "tool_result_capture"
        )
        self._spill = SpillEngine(
            self._registry, limits=config.limits, enabled=tool_result_capture_enabled
        )
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
    def tool_result_capture_enabled(self) -> bool:
        return self._spill.enabled

    @property
    def suma_enabled(self) -> bool:
        """Deprecated alias for :attr:`tool_result_capture_enabled`."""
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
        snapshots = self._capture_paths(paths, media_type=media_type)
        return self._registry.register_batch(self.session_id, snapshots)

    def capture_read_paths(self, request_id: str, paths: list[str]):
        """Adapter capture boundary: retain authorized bytes if store publication fails."""
        snapshots = self._capture_paths(paths)
        try:
            return self._registry.register_batch(self.session_id, snapshots)
        except Exception as raw_exc:
            exc = (
                raw_exc
                if isinstance(raw_exc, ShuntError)
                else ShuntError("STORE_FAILED", "INTERNAL_ERROR")
            )
            return compact_failure(
                request_id,
                snapshots[0].data,
                exc,
                limits=self.config.limits,
                hard_chars=self.config.reader.legacy_compaction_max_chars,
            )

    def _capture_paths(self, paths: list[str], *, media_type: str | None = None) -> list[Snapshot]:
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
        return snapshots

    # -- reader ------------------------------------------------------------
    def read(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = str((request or {}).get("request_id") or "req_unknown")
        if not self.config.reader.enabled:
            exc = ShuntError("INVALID_REQUEST", "READER_DISABLED", retryable=False)
            return self._publish_failure(request_id, exc, OperationKind.READ)

        operation_id = new_operation_id()
        try:
            result: ReaderResult = self._reader.answer(
                self.session_id, request, accounting_id=operation_id
            )
        except Exception as raw_exc:
            # Validate before recovering an unexpected implementation failure: a broken
            # reader must never turn malformed arguments into permission to disclose.
            try:
                validate_request(request)
                exc = (
                    raw_exc
                    if isinstance(raw_exc, ShuntError)
                    else ShuntError("STORE_FAILED", "INTERNAL_ERROR")
                )
            except ShuntError as invalid:
                exc = invalid
            provenance = deterministic(ProvenanceLabel.NO_MODEL_OUTPUT)
            result = ReaderResult(
                envelope=E.error_envelope(request_id, exc),
                provenance=provenance,
                cost=ReaderCost.none(),
            )
        candidate = result.envelope
        if (
            candidate.get("status") == "error"
            and fallback_allowed(candidate.get("code", ""), candidate.get("failure_detail"))
            and candidate.get("provenance", {}).get("attribution_status") != "mismatch"
        ):
            # Prefer a bounded heuristic summary after exhausted reader attempts. Policy
            # refusals are not availability failures and must never receive a soft landing.
            try:
                candidate = enforce(
                    self._legacy_compaction_fallback(request, result, request_id, operation_id),
                    self.config.limits,
                    enforce_reader_output_caps=self.config.reader.enforce_output_caps,
                )
            except ShuntError as refused:
                candidate = E.error_envelope(request_id, refused, accounting_id=operation_id)
            except Exception:
                # Recover through the pure compactor even if the normal wrapper failed.
                # The independent path repeats authorization and all disclosure guards.
                try:
                    candidate = self._emergency_compaction(
                        request, result, request_id, operation_id
                    )
                except ShuntError as refused:
                    candidate = E.error_envelope(request_id, refused, accounting_id=operation_id)
        if candidate.get("code") == "CITATION_INVALID":
            # A failed citation is not a semantic answer. Keep evidence handles and
            # reader cost intact; let the caller choose an exact bounded selector.
            try:
                for handle in candidate.get("sources", []):
                    entry = self._registry.resolve(self.session_id, handle["source_id"])
                    if entry.snapshot.snapshot_id != handle["snapshot_id"]:
                        raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
            except ShuntError as exc:
                candidate = {**candidate, "recovery": E.recovery_for(exc.code, handles_valid=False)}
            candidate = {
                **candidate,
                "guidance": (
                    "Semantic answer unavailable; evidence needs verification. "
                    + _INSPECT_RECOVERY_GUIDANCE
                    + " No heuristic summary was substituted."
                ),
            }
        published = enforce_or_fixed(
            candidate,
            self.config.limits,
            enforce_reader_output_caps=self.config.reader.enforce_output_caps,
        )
        refined = bool((request or {}).get("refined"))
        try:
            baseline, credited_bytes = self._baseline_for(result.source_ids)
        except Exception:
            baseline, credited_bytes = Baseline.none(), 0
        self._record(
            operation_id=operation_id,
            kind=OperationKind.REFINED_READ if refined else OperationKind.READ,
            envelope=published,
            baseline=baseline,
            baseline_credited=credited_bytes > 0,
            credited_bytes=credited_bytes,
            reader=result.cost,
            boundary=(
                DeliveryBoundary.EXTRACTION
                if published.get("code") in ("EXTRACTED", "LEGACY_COMPACTED")
                else DeliveryBoundary.ENVELOPE
            ),
        )
        return published

    def _automatic_extract(
        self, request: dict[str, Any], result: ReaderResult, request_id: str, operation_id: str
    ) -> dict[str, Any]:
        # Revalidate every handle after the provider wait; expiry/reset/store failures
        # must not turn a partially resolvable request into a disclosure.
        validate_request(request)
        assert_no_secret(request["question"].encode("utf-8"), "QUESTION")
        entries = []
        for source in request["sources"]:
            entry = self._registry.resolve(self.session_id, source["source_id"])
            if entry.snapshot.snapshot_id != source["snapshot_id"]:
                raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
            entries.append(entry)
        first = request["sources"][0]
        entry = self._registry.resolve(self.session_id, first["source_id"])
        limits = self.config.limits
        budget = min(
            self.config.reader.fallback_max_bytes,
            4096,
            entry.snapshot.bytes_len - 1,
            request["budgets"]["max_answer_bytes"],
            limits.max_answer_bytes,
            limits.inspect_max_result_bytes,
            limits.max_extraction_bytes,
        )
        if budget <= 0:
            raise ShuntError("LIMIT_EXCEEDED", "EMPTY_FALLBACK")
        return self._inspect(
            {
                "schema_version": EMITTED_SCHEMA_VERSION,
                "request_id": request_id,
                "operation": "inspect",
                "source_id": first["source_id"],
                "snapshot_id": first["snapshot_id"],
                "selector": {"kind": "bytes", "start": 0, "end": entry.snapshot.bytes_len},
                "budgets": {
                    "max_result_bytes": budget,
                    "max_scan_lines": limits.inspect_max_scan_lines,
                },
            },
            request_id,
            operation_id,
            fallback=result,
        )

    def _legacy_compaction_fallback(
        self,
        request: dict[str, Any],
        result: ReaderResult,
        request_id: str,
        operation_id: str,
        *,
        max_result_bytes: int | None = None,
        compactor=None,
    ) -> dict[str, Any]:
        """Deterministic legacy-shaped compaction over the first requested source.

        Ported from the incumbent tool-result compactor (``legacy_compact.py``): signal
        lines, head/tail sampling, repeated-line collapsing, JSON structure and secret
        redaction. Heuristic, not exact - always ``partial`` and always labelled
        ``derived=false``, so it can never be mistaken for either an exact
        ``deterministic_extraction`` or a ``model_derived`` answer. Covers only the first
        requested source, matching the existing automatic-extract escape hatch's own
        simplification; any other selected sources are recorded as an omission rather than
        silently dropped.
        """
        validate_request(request)
        assert_no_secret(request["question"].encode("utf-8"), "QUESTION")
        entries = []
        for source in request["sources"]:
            entry = self._registry.resolve(self.session_id, source["source_id"])
            if entry.snapshot.snapshot_id != source["snapshot_id"]:
                raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
            selector = source["selector"]
            if selector["kind"] == "lines" and selector["start"] > min(
                selector["end"], entry.snapshot.line_count
            ):
                raise ShuntError("INVALID_REQUEST", "LINE_OUT_OF_RANGE")
            if selector["kind"] == "records":
                node = resolve_pointer(entry.snapshot.json_value, selector["pointer"])
                if selector["end"] < selector["start"] or selector["end"] > record_count(node):
                    raise ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE")
            entries.append(entry)
        first = request["sources"][0]
        entry = self._registry.resolve(self.session_id, first["source_id"])
        original_bytes = entry.snapshot.bytes_len
        text = entry.snapshot.data.decode("utf-8")

        hard_chars = self.config.reader.legacy_compaction_max_chars
        allowance = self._store.disclosure_allowance(self._identity, entry.source_id)
        if allowance.exhausted:
            raise ShuntError("DISCLOSURE_EXHAUSTED")
        summary = (compactor or compact_tool_result)(text, hard_chars=hard_chars)
        summary = _utf8_safe_cap(
            summary,
            min(
                self.config.limits.max_extraction_bytes,
                allowance.remaining,
                max_result_bytes
                if max_result_bytes is not None
                else self.config.limits.max_extraction_bytes,
            ),
        )
        summary_bytes = len(summary.encode("utf-8"))

        original_failure = str(result.envelope.get("code") or "")
        if not fallback_allowed(original_failure, result.envelope.get("failure_detail")):
            # Should be unreachable given the caller's own guard, but the block's own
            # runtime policy only accepts availability failures - refuse rather than publish an
            # invented one.
            raise ShuntError("STORE_FAILED", "LEGACY_COMPACTION_FAILURE_UNKNOWN")

        source_handles = [
            {
                "source_id": item.source_id,
                "snapshot_id": item.snapshot.snapshot_id,
                "media_type": item.snapshot.media_type,
                "bytes": item.snapshot.bytes_len,
                "expires_at": E.iso_expiry(item.expires_at_epoch),
            }
            for item in entries
        ]
        coverage = E.Coverage(
            **{
                **result.envelope["coverage"],
                "omitted": list(result.envelope["coverage"]["omitted"]),
            }
        )
        coverage.complete = False
        for handle in source_handles:
            coverage.omit_once(handle["source_id"], {"kind": "all"}, "UNKNOWN_REMAINDER")

        attempts = result.cost.attempts_started
        try:
            raw_artifact_path = self._store.raw_artifact_path(
                self._identity, entry.source_id
            )
        except ShuntError as exc:
            if exc.code != "STORE_FAILED":
                raise
            raw_artifact_path = None
        guidance = (
            "Escape hatch: deterministic legacy-shaped compaction of the source, ported "
            "from the incumbent tool-result compactor; not model-derived and not an LLM "
            "summary. Original reader failure: "
            + original_failure
            + ". Covers only the first requested source, independent of the question; "
            "other sources and structure the heuristic dropped are omitted. Treat the "
            "summary only as navigation: never as the question's answer, exhaustive "
            "coverage, an exact count, or citation evidence. Use the retained handles "
            "with context_shunt_inspect for exact bounded evidence."
        )
        legacy_compaction = {
            "deterministic": True,
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "summary": summary,
            "summary_bytes": summary_bytes,
            "original_bytes": original_bytes,
            "hard_cap_chars": hard_chars,
            "original_failure": original_failure,
        }
        if raw_artifact_path is not None:
            guidance += (
                " The complete raw source is not inlined; read it in bounded pages with "
                "the host file tool at legacy_compaction.raw_artifact_path."
            )
            legacy_compaction["raw_artifact_path"] = raw_artifact_path
        else:
            guidance += (
                " The full-path compatibility mirror is unavailable; the retained handles "
                "remain the exact bounded recovery interface."
            )
        env = E.build(
            request_id=request_id,
            status="partial",
            code="LEGACY_COMPACTED",
            failure_detail=result.envelope.get("failure_detail", "UNSPECIFIED"),
            coverage=coverage,
            sources=source_handles,
            retryable=False,
            result_kind=ResultKind.LEGACY_COMPACTION,
            provenance=replace(
                result.provenance,
                derived=False,
                label=ProvenanceLabel.LEGACY_COMPACTION,
                citations_mechanically_verified=False,
                attempts_started=attempts,
                usage_complete=(
                    result.cost.attempts_usage_complete == attempts if attempts else True
                ),
            ),
            guidance=guidance,
            recovery=E.recovery_for(original_failure, handles_valid=True),
            accounting_id=operation_id,
            legacy_compaction=legacy_compaction,
        )
        fit_compaction(env, self.config.limits)
        summary_bytes = env["legacy_compaction"]["summary_bytes"]
        charge = self._store.charge_disclosure(
            self._identity, entry.source_id, "bytes", summary_bytes
        )
        if not charge.granted:
            raise ShuntError("DISCLOSURE_EXHAUSTED")
        # The path refers only to a mirror prepared during source publication. Never read,
        # write or fsync the raw payload here: this fallback commonly runs after the reader
        # deadline, and response publication must not acquire an unbounded I/O tail.
        return env

    def _emergency_compaction(self, request, result, request_id, operation_id):
        from .fallback import compact_tool_result as incumbent

        return ShuntSession._legacy_compaction_fallback(
            self,
            request,
            result,
            request_id,
            operation_id,
            compactor=incumbent,
        )

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
        except Exception as raw_exc:
            exc = (
                raw_exc
                if isinstance(raw_exc, ShuntError)
                else ShuntError("STORE_FAILED", "INTERNAL_ERROR")
            )
            if fallback_allowed(exc.code, exc.detail):
                try:
                    candidate = self._inspect_legacy_fallback(
                        request, request_id, operation_id, exc
                    )
                    self._record(
                        operation_id=operation_id,
                        kind=OperationKind.INSPECT,
                        envelope=candidate,
                        baseline=Baseline.none(),
                        baseline_credited=False,
                        reader=ReaderCost.none(),
                        boundary=DeliveryBoundary.EXTRACTION,
                    )
                    return candidate
                except ShuntError as refused:
                    exc = refused
            return self._publish_failure(
                request_id, exc, OperationKind.INSPECT, operation_id=operation_id
            )

    def _inspect_legacy_fallback(self, request, request_id, operation_id, exc):
        validated = validate_request(request, operations=_INSPECT_OPERATIONS)
        entry = self._registry.resolve(self.session_id, validated["source_id"])
        if entry.snapshot.snapshot_id != validated["snapshot_id"]:
            raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
        state = {}
        if "cursor" in validated:
            state = decode_cursor(
                self._store.cursor_key(),
                validated["cursor"],
                entry.source_id,
                entry.snapshot.snapshot_id,
                validated["selector"],
            )
        selector = validated["selector"]
        kind = selector["kind"]
        if kind in ("lines", "bytes") and selector["end"] < selector["start"]:
            raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR")
        if kind == "bytes":
            data = entry.snapshot.data
            start = max(selector["start"], int(state.get("offset", selector["start"])))
            end = min(selector["end"], len(data))
            if start < end and any(
                0 <= pos < len(data) and data[pos] & 0xC0 == 0x80
                for pos in (selector["start"], start, end)
            ):
                raise ShuntError("INVALID_REQUEST", "UTF8_RANGE_BOUNDARY")
        if (
            kind == "search"
            and len(selector["needle"].encode("utf-8"))
            > self.config.limits.inspect_max_needle_bytes
        ):
            raise ShuntError("INVALID_REQUEST", "NEEDLE_OVER_CAP")
        allowance = self._store.disclosure_allowance(self._identity, entry.source_id)
        if allowance.exhausted:
            raise ShuntError("DISCLOSURE_EXHAUSTED")
        read_request = {
            "schema_version": EMITTED_SCHEMA_VERSION,
            "request_id": request_id,
            "operation": "read",
            "question": "Deterministic fallback",
            "sources": [
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": {"kind": "all"},
                }
            ],
            "budgets": {
                "max_chunks": 1,
                "max_answer_bytes": self.config.limits.max_answer_bytes,
                "deadline_ms": self.config.limits.request_deadline_ms,
            },
        }
        provenance = deterministic(ProvenanceLabel.NO_MODEL_OUTPUT)
        result = ReaderResult(
            envelope=E.error_envelope(request_id, exc),
            provenance=provenance,
            cost=ReaderCost.none(),
        )
        return self._legacy_compaction_fallback(
            read_request,
            result,
            request_id,
            operation_id,
            max_result_bytes=validated["budgets"]["max_result_bytes"],
        )

    def _inspect(
        self,
        request: dict[str, Any],
        request_id: str,
        operation_id: str,
        *,
        fallback: ReaderResult | None = None,
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
        cursor_version = state.get("schema_version")
        if cursor_version is not None and cursor_version != validated["schema_version"]:
            raise ShuntError("INVALID_REQUEST", "BAD_CURSOR", retryable=False)
        effective_request_version = validated["schema_version"]
        if cursor_version is None and int(state.get("matches", 0)) > 0:
            # Cursors minted before the version was embedded used cumulative match state.
            # Treat them conservatively even if presented on a 1.3 request.
            effective_request_version = "1.2"

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
            if fallback is not None:
                raise ShuntError("DISCLOSURE_EXHAUSTED")
            return self._disclosure_exhausted(
                request_id, operation_id, entry, selector, handles, allowance
            )

        # Reserve extra room for all handles, omissions and escape-hatch guidance.
        # The full composed envelope is still guarded before any disclosure is charged.
        if selector["kind"] == "aggregate" and "cursor" in validated:
            raise ShuntError("INVALID_REQUEST", "BAD_CURSOR", retryable=False)
        max_wire_bytes = self._extraction_wire_budget(
            request_id, operation_id, entry, selector, handles
        ) - (4096 if fallback is not None else 0)
        extraction = (
            aggregate_snapshot(
                entry.snapshot,
                selector,
                max_result_bytes=budget,
                max_wire_bytes=max_wire_bytes,
                max_records=int(budgets["max_scan_lines"]),
                limits=self.config.limits,
            )
            if selector["kind"] == "aggregate"
            else self._inspector.extract(
                entry.snapshot.data,
                entry.snapshot.line_index,
                selector,
                max_result_bytes=budget,
                max_scan_lines=int(budgets["max_scan_lines"]),
                max_wire_bytes=max_wire_bytes,
                state=state,
                request_version=effective_request_version,
            )
        )
        if extraction.stalled:
            # The page emitted nothing *and* the cursor did not move, so continuing would
            # loop forever. A scan-budget stop is not this case: it emits nothing but does
            # advance the scan position, and is reported as an honest empty page.
            if extraction.stall_reason == "wire":
                # This unit cannot fit any envelope, whatever the allowance says. Calling it
                # a disclosure problem would send the caller to a remedy that never works.
                raise ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_WIRE_BUDGET", retryable=False)
            if extraction.stall_reason == "cap":
                raise ShuntError(
                    "LIMIT_EXCEEDED", "SEARCH_MAX_MATCHES_EXHAUSTED", retryable=False
                )
            if clipped_by_allowance:
                return self._disclosure_exhausted(
                    request_id, operation_id, entry, selector, handles, allowance
                )
            raise ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_PAGE_BUDGET", retryable=False)

        if fallback is not None and not extraction.result_bytes:
            raise ShuntError("LIMIT_EXCEEDED", "EMPTY_FALLBACK")
        next_cursor_state = extraction.next_cursor_state
        if next_cursor_state is not None:
            next_cursor_state = {
                **next_cursor_state,
                "schema_version": validated["schema_version"],
            }
        next_cursor = (
            encode_cursor(key, source_id, snapshot_id, selector, next_cursor_state)
            if next_cursor_state is not None
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

        if fallback is not None:
            coverage = E.Coverage(upstream_truncated=None)
            for handle in fallback.envelope["sources"]:
                coverage.omit(handle["source_id"], {"kind": "all"}, "UNKNOWN_REMAINDER")
            handles = fallback.envelope["sources"]
        fallback_failure = (
            str(fallback.availability_failure or fallback.envelope.get("code") or "MODEL_ERROR")
            if fallback
            else None
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
            if extraction.records_scanned is not None:
                block["records_scanned"] = extraction.records_scanned
            if extraction.records_matched is not None:
                block["records_matched"] = extraction.records_matched
            return E.build(
                request_id=request_id,
                status="ok" if extraction.complete and fallback is None else "partial",
                code="EXTRACTED",
                coverage=coverage,
                sources=handles,
                retryable=False,
                result_kind=ResultKind.DETERMINISTIC_EXTRACTION,
                provenance=replace(
                    deterministic(ProvenanceLabel.DETERMINISTIC_EXTRACTION),
                    attempts_started=fallback.cost.attempts_started if fallback else 0,
                    usage_complete=(
                        fallback.cost.attempts_usage_complete == fallback.cost.attempts_started
                    )
                    if fallback
                    else True,
                ),
                guidance=(
                    "Escape hatch: exact deterministic fallback extraction; not model-derived "
                    "and not an LLM summary. Original failure: "
                    + fallback_failure
                    + ". Selection: byte prefix of first requested source, independent of question "
                    "and reader selectors; other sources and unreturned bytes omitted."
                )
                if fallback
                else _SEARCH_WINDOW_GUIDANCE
                if selector.get("kind") == "search"
                and any(s.kind == "bytes" for s in extraction.segments)
                else None,
                recovery=E.recovery_for(fallback_failure) if fallback else None,
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
        probe = compose(
            limits.disclosure_max_per_source_bytes, limits.disclosure_max_per_session_bytes, False
        )
        if E.serialized_bytes(probe) > limits.max_extended_envelope_bytes:
            raise ShuntError("LIMIT_EXCEEDED", "NO_ENVELOPE_HEADROOM")
        try:
            enforce(probe, limits)
        except OutputGuardError as exc:
            raise ShuntError("LIMIT_EXCEEDED", "EXTRACTION_REFUSED", retryable=False) from exc

        # Check-and-increment before a byte is returned: a concurrent inspect that
        # consumed the allowance in the meantime causes this page to disclose nothing.
        charge = self._store.charge_disclosure(
            self._identity,
            source_id,
            "bytes" if extraction.mode == "aggregate" else extraction.mode,
            extraction.result_bytes,
        )
        if not charge.granted:
            if fallback is not None:
                raise ShuntError("DISCLOSURE_EXHAUSTED")
            return self._disclosure_exhausted(
                request_id, operation_id, entry, selector, handles, allowance
            )

        env = compose(
            charge.disclosed_bytes_source, charge.disclosed_bytes_session, charge.limit_reached
        )
        published = enforce_or_fixed(env, self.config.limits)
        if fallback is not None:
            return published
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
            guidance=_SEARCH_WINDOW_GUIDANCE if selector.get("kind") == "search" else None,
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
        pointer_delivered = (
            published.get("status") == "ok"
            and published.get("code") == "IMPORTED"
            and bool(published.get("pointer"))
            and any(
                handle.get("source_id") == entry.source_id
                for handle in published.get("sources", [])
            )
        )
        baseline = (
            # A producer that already shortened the payload only lets us observe the
            # shortened size; crediting the full artifact there would be invented.
            Baseline.host_truncated(outcome.byte_count, limits=self.config.limits)
            if normalized.upstream_truncated
            else Baseline.withheld_payload(outcome.byte_count, limits=self.config.limits)
        )
        # The output guard may reject a valid import envelope after the artifact has been
        # adopted. In that case the effective adapter egress is the fixed error envelope:
        # no pointer reached the caller, so it cannot claim pointer delivery or consume
        # the one-time baseline credit. Keep the immutable snapshot intact for TTL cleanup
        # just as the spill path does for its rejected pointer.
        credited = bool(
            pointer_delivered and self._store.credit_baseline(self._identity, entry.source_id)
        )
        self._record(
            operation_id=operation_id,
            kind=OperationKind.CAPTURE,
            envelope=published,
            baseline=baseline,
            baseline_credited=credited,
            reader=ReaderCost.none(),
            boundary=(DeliveryBoundary.POINTER if pointer_delivered else DeliveryBoundary.ENVELOPE),
        )
        self._metrics.count(
            "artifact_import",
            {
                "result": "imported" if pointer_delivered else "refused",
                "code": published.get("code", "LIMIT_EXCEEDED"),
            },
        )
        return published

    # -- optional oversized-tool-result capture ----------------------------
    def post_tool_result(
        self,
        request_id: str,
        result: Any,
        *,
        internal_source_id: str | None = None,
        upstream_truncated: bool = False,
    ):
        """Capture-then-pointer for one complete tool result.

        Only ever consulted when the capability probe reports ``tool_result_capture``
        supported (config enabled *and* the operator's ordering attestation present - see
        ``config.ToolResultCaptureConfig``). Never returns the raw result: every branch of
        :meth:`SpillEngine.evaluate` returns either ``passthrough`` (nothing eligible, so
        nothing is touched) or a bounded envelope. Answering the caller's actual question
        about a captured pointer is a separate step, through ``context_shunt_read`` - this
        method never sees the caller's question, so it never could.
        """
        if not self.tool_result_capture_enabled:
            return None
        operation_id = new_operation_id()
        try:
            outcome = self._spill.evaluate(
                self.session_id,
                request_id,
                result,
                internal_source_id=internal_source_id,
                upstream_truncated=upstream_truncated,
            )
        except Exception as raw_exc:
            if (
                not isinstance(result, str)
                or len(result.encode("utf-8")) <= self.config.limits.max_tool_result_bytes
            ):
                raise
            exc = (
                raw_exc
                if isinstance(raw_exc, ShuntError)
                else ShuntError("SPILL_FAILED", "INTERNAL_ERROR")
            )
            env = compact_failure(
                request_id,
                result.encode("utf-8"),
                exc,
                limits=self.config.limits,
                hard_chars=self.config.reader.legacy_compaction_max_chars,
            )
            outcome = SpillOutcome(
                action="error",
                envelope=env,
                code=env["code"],
                bytes_measured=len(result.encode("utf-8")),
            )
        if outcome.envelope is not None:
            envelope = enforce_or_fixed(outcome.envelope, self.config.limits)
            pointer_delivered = (
                outcome.action == "spill"
                and envelope.get("code") == "SPILLED"
                and bool(envelope.get("pointer"))
                and any(h["source_id"] == outcome.source_id for h in envelope.get("sources", []))
            )
            # Guard rejection is an error envelope, never a delivered pointer. Keep the
            # stored artifact intact, but do not expose its handle or consume its credit.
            rejected_pointer = outcome.action == "spill" and not pointer_delivered
            if rejected_pointer and envelope.get("code") == "SPILLED":
                envelope = fixed_error(request_id, "SPILL_FAILED")
            outcome = type(outcome)(
                action="error" if rejected_pointer else outcome.action,
                envelope=envelope,
                code=envelope["code"],
                bytes_measured=outcome.bytes_measured,
                source_id=None if rejected_pointer else outcome.source_id,
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
        self._metrics.count("tool_result_capture_outcome", {"result": outcome.action})
        return outcome

    # -- accounting --------------------------------------------------------
    def reject_tool_call(
        self, request_id: str, exc: ShuntError, tool: str
    ) -> dict[str, Any]:
        """Publish and account one plugin-owned public-argument rejection.

        Host schema validation happens before a handler and is outside this plugin's
        accounting boundary. Once Hermes invokes one of our handlers, however, its strict
        contract rejection is Shunt-owned and can be recorded without touching a source,
        provider, or protected tool result.
        """
        kinds = {
            "context_shunt_read": OperationKind.READ,
            "context_shunt_inspect": OperationKind.INSPECT,
            "context_shunt_stats": OperationKind.STATS,
            "context_shunt_import": OperationKind.CAPTURE,
        }
        kind = kinds.get(tool)
        if kind is None:
            raise ValueError("unknown context-shunt tool")
        return self._publish_failure(request_id, exc, kind)

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
        except Exception:
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


def _utf8_safe_cap(text: str, max_bytes: int) -> str:
    """Bound ``text`` to ``max_bytes`` UTF-8 bytes without splitting a multi-byte character.

    ``legacy_compact.compact_tool_result`` caps by character count against its own
    (configurable) hard limit, which is measured in Python string length, not UTF-8 bytes -
    a summary full of multi-byte characters can still exceed the envelope contract's byte
    cap even after that cap is applied. A raw byte slice can land mid-character; decoding
    with ``errors="ignore"`` drops at most the one partial trailing character rather than
    raising, which is the correct trade for a bounded fallback that must never fail closed
    over its own safety margin.
    """
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore")


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
        enforce_output_caps=config.reader.enforce_output_caps,
    )
    if not config.reader.fallback_chain:
        return primary
    alternatives = [
        HostBridgeProvider(
            (fallback_calls or {}).get(f"{ref.provider}/{ref.model}", call),
            config.limits,
            ref.model,
            provider=ref.provider,
            enforce_output_caps=config.reader.enforce_output_caps,
        )
        for ref in config.reader.fallback_chain
    ]
    # The chain shares the same scheduling and generation budgets as its candidates.
    return FallbackChainProvider(primary, alternatives, config.limits)


__all__ = [
    "AttributionPolicy",
    "EMITTED_SCHEMA_VERSION",
    "Provenance",
    "ShuntSession",
    "TokenMethod",
    "build_provider",
]
