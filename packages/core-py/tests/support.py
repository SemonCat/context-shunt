"""Shared deterministic test doubles."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from context_shunt.capability import CapabilityReport, supported, unsupported
from context_shunt.config import Config
from context_shunt.config import load as load_config
from context_shunt.errors import ShuntError
from context_shunt.limits import DEFAULT_LIMITS, EMITTED_SCHEMA_VERSION, READER_MODEL
from context_shunt.provenance import (
    Attribution,
    AttributionPolicy,
    Confidence,
    ModelIdentity,
    Provenance,
    ProvenanceLabel,
    TokenMethod,
    Usage,
)
from context_shunt.provider import ModelResponse, ProviderTarget, TransientProviderError
from context_shunt.registry import SourceRegistry
from context_shunt.store import ScopeIdentity, SnapshotStore


@dataclass
class RecordedCall:
    system: str
    user: str
    model: str
    max_output_tokens: int
    timeout_ms: int


@dataclass
class FakeLuna:
    """Records every call so gates can assert model, question propagation and counts.

    By default it behaves like a host that *can* prove attribution: it reports the model
    it was asked for and confirms the report came from the provider. Tests that need the
    weaker, more common case set ``confirms_generation=False`` or clear ``reported``.
    """

    replies: list[Any] = field(default_factory=list)
    calls: list[RecordedCall] = field(default_factory=list)
    model: str = READER_MODEL
    provider: str = "openai"
    default_reply: str | None = None
    confirms_generation: bool = True
    report_model: bool = True
    usage_exact: bool = True

    @property
    def target(self) -> ProviderTarget:
        return ProviderTarget(model=self.model, provider=self.provider)

    def complete(self, *, system: str, user: str, max_output_tokens: int, timeout_ms: int):
        self.calls.append(RecordedCall(system, user, self.model, max_output_tokens, timeout_ms))
        reply = self.replies.pop(0) if self.replies else self.default_reply
        if reply is None:
            reply = json.dumps({"answer": "", "citations": []})
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(user)
        reported = (
            ModelIdentity(provider=self.provider, model=self.model)
            if self.report_model
            else ModelIdentity()
        )
        return ModelResponse(
            text=reply,
            requested=ModelIdentity(provider=self.provider, model=self.model),
            resolved=ModelIdentity(provider=self.provider, model=self.model),
            reported=reported,
            provider_confirms_generation=self.confirms_generation and self.report_model,
            usage=Usage(
                input_tokens=10 if self.usage_exact else None,
                output_tokens=5 if self.usage_exact else None,
                method=TokenMethod.EXACT if self.usage_exact else TokenMethod.UNKNOWN,
            ),
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


def answer_json(answer: str, citations: list[dict[str, Any]]) -> str:
    """The legacy reply shape: prose the model marks up itself with ``[cN]``."""
    return json.dumps({"answer": answer, "citations": citations})


def claims_json(claims: list[dict[str, Any]], citations: list[dict[str, Any]]) -> str:
    """The current reply shape: structured claims plus the citations they reference.

    ``claims`` items are ``{"text": str, "citation_ids": [str, ...]}``; markers are never
    written by the caller here either - the reader places them, mechanically, from
    ``citation_ids``.
    """
    return json.dumps({"claims": claims, "citations": citations})


def derived_provenance(**overrides: Any) -> Provenance:
    """Provenance for a hand-built model-derived envelope in a test.

    Defaults to the strongest honest case (a provider-confirmed match), so a test that
    cares about a weaker attribution has to say so explicitly.
    """
    base: dict[str, Any] = {
        "derived": True,
        "label": ProvenanceLabel.MODEL_GENERATED_ANSWER,
        "attribution_status": Attribution.ACTUAL,
        "attribution_confidence": Confidence.HIGH,
        "attribution_policy": AttributionPolicy.ALLOW_UNVERIFIED,
        "attempts_started": 1,
        "usage_complete": True,
        "citations_mechanically_verified": True,
        "requested": ModelIdentity(provider="openai", model=READER_MODEL),
        "resolved": ModelIdentity(provider="openai", model=READER_MODEL),
        "reported": ModelIdentity(provider="openai", model=READER_MODEL),
    }
    base.update(overrides)
    return Provenance(**base)


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "workspace_roots": [str(tmp_path / "ws")],
        "cache_dir": str(tmp_path / "cache"),
    }
    raw.update(overrides)
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    return load_config(raw, default_spill_dir=tmp_path / "cache")


def make_identity(session_id: str = "sess-1", generation: int = 1) -> ScopeIdentity:
    return ScopeIdentity(
        host="test-host",
        profile="test",
        principal="local",
        session=session_id,
        generation=generation,
    )


def make_store(tmp_path: Path, config: Config | None = None) -> SnapshotStore:
    root = config.cache_root if config is not None else tmp_path / "cache"
    return SnapshotStore(root, config.limits if config is not None else DEFAULT_LIMITS)


def make_registry(
    tmp_path: Path,
    config: Config | None = None,
    *,
    session_id: str = "sess-1",
    generation: int = 1,
) -> SourceRegistry:
    config = config or make_config(tmp_path)
    store = make_store(tmp_path, config)
    identity = make_identity(session_id, generation)
    store.open_scope(identity)
    return SourceRegistry(store, identity, config.limits)


def make_capability(
    *, suma: bool = False, artifact_import: bool = False, adapter: str = "test"
) -> CapabilityReport:
    from context_shunt.capability import DisabledReason

    return CapabilityReport(
        adapter=adapter,
        adapter_version="1.1.0",
        host_name="test-host",
        host_version="0.0.0",
        contract_version=EMITTED_SCHEMA_VERSION,
        reader_model=READER_MODEL,
        tools_covered=("read", "search", "shell"),
        modes=[
            supported("local_gate"),
            supported("reader"),
            supported("suma_post_tool")
            if suma
            else unsupported("suma_post_tool", DisabledReason.ORDERING_UNPROVEN),
            supported("artifact_import")
            if artifact_import
            else unsupported("artifact_import", DisabledReason.IMPORT_UNIMPLEMENTED),
        ],
        tested_fixture_id="gate-cases.json",
    )


def transient() -> ShuntError:
    return TransientProviderError("PROVIDER_CALL_FAILED")


LIMITS = DEFAULT_LIMITS
