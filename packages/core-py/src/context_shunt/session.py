"""Per-session wiring shared by both adapters.

Adapters normalize host events into ``(tool, args)`` and model calls; everything below -
gate, registration, reader, spill, guard, metrics - lives here so the two hosts cannot
drift apart on semantics.
"""

from __future__ import annotations

from typing import Any

from . import envelope as E
from .binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from .capability import CapabilityReport
from .clock import Clock, MonotonicClock
from .config import Config
from .errors import ShuntError
from .gate import Decision, GateDecision, PreReadGate, guidance_for
from .guard import enforce_or_fixed
from .metrics import MetricsSink, NullMetrics
from .paths import authorize
from .probe import FileProber
from .provider import LunaProvider, UnavailableProvider
from .reader import Reader
from .registry import RegisteredSource, SourceRegistry
from .snapshot import snapshot_file
from .spill import SpillStore, SumaSpillEngine


class ShuntSession:
    def __init__(
        self,
        session_id: str,
        config: Config,
        capability: CapabilityReport,
        *,
        provider: LunaProvider | None = None,
        clock: Clock | None = None,
        metrics: MetricsSink | None = None,
        registry: SourceRegistry | None = None,
    ):
        self.session_id = session_id
        self.config = config
        self.capability = capability
        self._clock = clock or MonotonicClock()
        self._metrics = metrics or NullMetrics()
        self._registry = registry or SourceRegistry(config.limits)
        self._gate = PreReadGate(FileProber(config.limits), config.limits, self._clock)
        self._provider = provider or UnavailableProvider()
        self._reader = Reader(
            self._registry,
            self._provider,
            limits=config.limits,
            clock=self._clock,
            metrics=self._metrics,
        )
        self._spill_store = SpillStore(config.spill_dir, config.limits)
        suma_enabled = config.suma_post_tool.enabled and capability.enabled("suma_post_tool")
        self._spill = SumaSpillEngine(
            self._spill_store, self._registry, limits=config.limits, enabled=suma_enabled
        )

    # -- accessors ---------------------------------------------------------
    @property
    def registry(self) -> SourceRegistry:
        return self._registry

    @property
    def spill(self) -> SumaSpillEngine:
        return self._spill

    @property
    def suma_enabled(self) -> bool:
        return self._spill.enabled

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
        env = E.build(
            request_id=request_id,
            status="blocked",
            code=decision.code or "UNCLASSIFIABLE_READ",
            coverage=E.Coverage(upstream_truncated=None),
            retryable=False,
            guidance=guidance_for(decision),
        )
        return enforce_or_fixed(env, self.config.limits)

    # -- sources -----------------------------------------------------------
    def register_path(self, path: str, *, media_type: str | None = None) -> RegisteredSource:
        authorized = authorize(path, self.config.path_policy())
        hint = media_type or (
            JSON_MEDIA_TYPE if authorized.real.suffix.lower() == ".json" else TEXT_MEDIA_TYPE
        )
        snapshot = snapshot_file(authorized, limits=self.config.limits, media_type_hint=hint)
        return self._registry.register(self.session_id, snapshot)

    # -- reader ------------------------------------------------------------
    def read(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.config.reader.enabled:
            exc = ShuntError("INVALID_REQUEST", "READER_DISABLED", retryable=False)
            return enforce_or_fixed(
                E.error_envelope(str((request or {}).get("request_id") or "req_unknown"), exc),
                self.config.limits,
            )
        env = self._reader.answer(self.session_id, request)
        return enforce_or_fixed(env, self.config.limits)

    # -- optional Suma post-tool ------------------------------------------
    def post_tool_result(
        self, request_id: str, result: Any, *, internal_source_id: str | None = None
    ):
        """Only ever consulted when the capability probe proved the host order is safe."""
        if not self.suma_enabled:
            return None
        outcome = self._spill.evaluate(
            self.session_id, request_id, result, internal_source_id=internal_source_id
        )
        if outcome.envelope is not None:
            outcome = type(outcome)(
                action=outcome.action,
                envelope=enforce_or_fixed(outcome.envelope, self.config.limits),
                code=outcome.code,
                bytes_measured=outcome.bytes_measured,
            )
        self._metrics.count("suma_outcome", {"result": outcome.action})
        return outcome

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Session teardown removes handles and private artifacts."""
        self._registry.expire_session(self.session_id)
        self._spill_store.purge_session(self.session_id)
