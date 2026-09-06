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
    "probe_max_lines_scanned",
    "max_tool_result_bytes",
    "max_envelope_bytes",
    "max_targeted_read_bytes",
    "max_source_bytes",
    "max_chunk_bytes",
    "max_answer_bytes",
    "max_quote_bytes",
    "max_question_bytes",
    "session_spill_quota_bytes",
    "max_sources_per_request",
    "max_chunks_per_request",
    "max_citations",
    "max_concurrent_model_calls",
    "max_chunk_overlap_lines",
    "max_transient_retries",
    "max_chunk_tokens",
    "max_request_input_tokens",
    "max_output_tokens_per_call",
    "bytes_per_token_estimate",
    "gate_probe_deadline_ms",
    "spill_io_deadline_ms",
    "request_deadline_ms",
    "model_call_deadline_ms",
    "spill_ttl_seconds",
    "json_max_depth",
    "json_max_nodes",
}
_POSITIVE_LIMITS = {
    "bytes_per_token_estimate",
    "max_chunk_bytes",
    "max_chunk_tokens",
    "max_concurrent_model_calls",
}
_CONFIG_KEYS = {
    "workspace_roots",
    "spill_dir",
    "denylist",
    "gate_enabled",
    "reader",
    "suma_post_tool",
    "writer",
    "operations",
    "limits",
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
    if raw is not None and not isinstance(raw, dict):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    raw = dict(raw or {})
    if set(raw) - _CONFIG_KEYS:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)

    for key in ("reader", "suma_post_tool", "writer", "limits"):
        if key in raw and not isinstance(raw[key], dict):
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    operations = raw.get("operations")
    if operations is not None and (
        not isinstance(operations, list) or any(not isinstance(item, str) for item in operations)
    ):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    for value in (
        raw.get("gate_enabled"),
        (raw.get("reader") or {}).get("enabled"),
        (raw.get("suma_post_tool") or {}).get("enabled"),
        (raw.get("writer") or {}).get("enabled"),
    ):
        if value is not None and not isinstance(value, bool):
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)

    writer = raw.get("writer") or {}
    if writer.get("enabled"):
        # Not "not implemented yet" - refused, so it cannot become a hidden capability.
        raise ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", retryable=False)
    if "operations" in raw and "propose_patch" in (raw.get("operations") or []):
        raise ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", retryable=False)

    roots_raw = raw.get("workspace_roots")
    if (
        not isinstance(roots_raw, list)
        or not roots_raw
        or any(not isinstance(root, str) or not root for root in roots_raw)
    ):
        raise ShuntError("UNSAFE_SOURCE", "NO_WORKSPACE_ROOT", retryable=False)
    roots = tuple(str(Path(root).expanduser().resolve(strict=False)) for root in roots_raw)

    spill_value = raw.get("spill_dir")
    if spill_value is not None and not isinstance(spill_value, str):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)

    reader_raw = raw.get("reader") or {}
    model = reader_raw.get("model", DEFAULT_LIMITS.reader_model)
    if not isinstance(model, str):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    if model != DEFAULT_LIMITS.reader_model:
        # v1 is a single-model contract; a different model is a configuration error, not
        # a silent substitution.
        raise ShuntError("MODEL_ERROR", "MODEL_NOT_ALLOWED", retryable=False)

    limits = DEFAULT_LIMITS
    raw_limits = raw.get("limits") or {}
    unknown = set(raw_limits) - _NARROWABLE
    if unknown:
        raise ShuntError("INVALID_REQUEST", "UNKNOWN_LIMIT_OVERRIDE", retryable=False)
    overrides: dict[str, int] = {}
    for key, value in raw_limits.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or (value == 0 and key in _POSITIVE_LIMITS)
        ):
            raise ShuntError("INVALID_REQUEST", "BAD_LIMIT_OVERRIDE", retryable=False)
        overrides[key] = value
    if overrides:
        try:
            limits = limits.narrow(**overrides)
        except ValueError:
            raise ShuntError("INVALID_REQUEST", "LIMIT_MAY_ONLY_NARROW", retryable=False) from None

    spill_dir = Path(spill_value or default_spill_dir).expanduser().resolve(strict=False)
    if any(spill_dir == Path(root) or Path(root) in spill_dir.parents for root in roots):
        raise ShuntError("UNSAFE_SOURCE", "SPILL_INSIDE_WORKSPACE", retryable=False)

    return Config(
        workspace_roots=roots,
        spill_dir=spill_dir,
        denylist=_read_denylist(raw.get("denylist")),
        gate_enabled=raw.get("gate_enabled", True),
        reader=ReaderConfig(enabled=reader_raw.get("enabled", True), model=model),
        suma_post_tool=SumaConfig(enabled=(raw.get("suma_post_tool") or {}).get("enabled", False)),
        limits=limits,
    )


def _read_denylist(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    return tuple(value)
