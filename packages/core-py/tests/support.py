"""Shared deterministic test doubles."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from context_shunt.capability import CapabilityReport, supported, unsupported
from context_shunt.config import Config, load as load_config
from context_shunt.limits import DEFAULT_LIMITS, READER_MODEL
from context_shunt.provider import ModelResponse, ModelUsage, TransientProviderError
from context_shunt.errors import ShuntError


@dataclass
class RecordedCall:
    system: str
    user: str
    model: str
    max_output_tokens: int
    timeout_ms: int


@dataclass
class FakeLuna:
    """Records every call so gates can assert model, question propagation and counts."""

    replies: list[Any] = field(default_factory=list)
    calls: list[RecordedCall] = field(default_factory=list)
    model: str = READER_MODEL
    default_reply: str | None = None

    def complete(self, *, system: str, user: str, max_output_tokens: int, timeout_ms: int):
        self.calls.append(
            RecordedCall(system, user, self.model, max_output_tokens, timeout_ms)
        )
        reply = self.replies.pop(0) if self.replies else self.default_reply
        if reply is None:
            reply = json.dumps({"answer": "", "citations": []})
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(user)
        return ModelResponse(
            text=reply, model=self.model, usage=ModelUsage(input_tokens=10, output_tokens=5, estimated=False)
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


def answer_json(answer: str, citations: list[dict[str, Any]]) -> str:
    return json.dumps({"answer": answer, "citations": citations})


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "workspace_roots": [str(tmp_path / "ws")],
        "spill_dir": str(tmp_path / "cache"),
    }
    raw.update(overrides)
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    return load_config(raw, default_spill_dir=tmp_path / "cache")


def make_capability(*, suma: bool = False, adapter: str = "test") -> CapabilityReport:
    from context_shunt.capability import DisabledReason

    return CapabilityReport(
        adapter=adapter,
        adapter_version="1.0.0",
        host_name="test-host",
        host_version="0.0.0",
        contract_version="1.0",
        reader_model=READER_MODEL,
        tools_covered=("read", "search", "shell"),
        modes=[
            supported("local_gate"),
            supported("reader"),
            supported("suma_post_tool")
            if suma
            else unsupported("suma_post_tool", DisabledReason.ORDERING_UNPROVEN),
        ],
        tested_fixture_id="gate-cases.json",
    )


def transient() -> ShuntError:
    return TransientProviderError("PROVIDER_CALL_FAILED")


LIMITS = DEFAULT_LIMITS
