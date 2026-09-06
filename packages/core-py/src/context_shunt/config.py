"""Configuration.

Two rules matter more than the rest:

* ``writer.enabled = true`` is refused at load time. v1 has no writer, so accepting the
  flag and quietly ignoring it would turn a missing feature into a hidden one.
* ``suma_post_tool.enabled`` defaults to ``false`` and, even when set, only takes effect
  if the adapter's capability probe proves a safe capture/replacement order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .paths import PathPolicy

_NARROWABLE = {
    "full_read_max_lines",
    "targeted_read_max_lines",
    "targeted_search_max_matches",
    "max_tool_result_bytes",
    "max_envelope_bytes",
    "max_source_bytes",
    "max_chunk_bytes",
    "max_answer_bytes",
    "max_quote_bytes",
    "session_spill_quota_bytes",
    "max_sources_per_request",
    "max_chunks_per_request",
    "max_citations",
    "max_concurrent_model_calls",
    "max_chunk_tokens",
    "max_request_input_tokens",
    "max_output_tokens_per_call",
    "request_deadline_ms",
    "model_call_deadline_ms",
    "spill_ttl_seconds",
}


@dataclass(frozen=True)
class SumaConfig:
    enabled: bool = False


@dataclass(frozen=True)
class ReaderConfig:
    enabled: bool = True
    model: str = DEFAULT_LIMITS.reader_model


@dataclass(frozen=True)
class Config:
    workspace_roots: tuple[str, ...]
    spill_dir: Path
    denylist: tuple[str, ...] = ()
    gate_enabled: bool = True
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    suma_post_tool: SumaConfig = field(default_factory=SumaConfig)
    limits: Limits = DEFAULT_LIMITS

    def path_policy(self) -> PathPolicy:
        return PathPolicy.from_config(list(self.workspace_roots), list(self.denylist))


def load(raw: dict[str, Any] | None, *, default_spill_dir: Path) -> Config:
    raw = dict(raw or {})

    writer = raw.get("writer") or {}
    if writer.get("enabled"):
        # Not "not implemented yet" - refused, so it cannot become a hidden capability.
        raise ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", retryable=False)
    if "operations" in raw and "propose_patch" in (raw.get("operations") or []):
        raise ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", retryable=False)

    roots = tuple(raw.get("workspace_roots") or [])
    if not roots:
        raise ShuntError("UNSAFE_SOURCE", "NO_WORKSPACE_ROOT", retryable=False)

    reader_raw = raw.get("reader") or {}
    model = str(reader_raw.get("model", DEFAULT_LIMITS.reader_model))
    if model != DEFAULT_LIMITS.reader_model:
        # v1 is a single-model contract; a different model is a configuration error, not
        # a silent substitution.
        raise ShuntError("MODEL_ERROR", "MODEL_NOT_ALLOWED", retryable=False)

    limits = DEFAULT_LIMITS
    overrides = {k: int(v) for k, v in (raw.get("limits") or {}).items() if k in _NARROWABLE}
    unknown = set(raw.get("limits") or {}) - _NARROWABLE
    if unknown:
        raise ShuntError("INVALID_REQUEST", "UNKNOWN_LIMIT_OVERRIDE", retryable=False)
    if overrides:
        try:
            limits = limits.narrow(**overrides)
        except ValueError:
            raise ShuntError("INVALID_REQUEST", "LIMIT_MAY_ONLY_NARROW", retryable=False) from None

    return Config(
        workspace_roots=roots,
        spill_dir=Path(raw.get("spill_dir") or default_spill_dir).expanduser(),
        denylist=tuple(raw.get("denylist") or ()),
        gate_enabled=bool(raw.get("gate_enabled", True)),
        reader=ReaderConfig(enabled=bool(reader_raw.get("enabled", True)), model=model),
        suma_post_tool=SumaConfig(
            enabled=bool((raw.get("suma_post_tool") or {}).get("enabled", False))
        ),
        limits=limits,
    )
