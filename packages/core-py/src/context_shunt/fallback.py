"""Mandatory, content-checked incumbent compaction without capture/store dependencies."""

from __future__ import annotations

from typing import Any

from . import envelope as E
from .binaryguard import assert_text
from .errors import ShuntError, fallback_allowed, safe_failure_detail
from .guard import enforce
from .legacy_compact import compact_tool_result
from .limits import DEFAULT_LIMITS, Limits, envelope_byte_cap
from .paths import assert_no_secret


def compact_failure(
    request_id: str,
    data: bytes,
    failure: ShuntError,
    *,
    limits: Limits = DEFAULT_LIMITS,
    hard_chars: int = 16000,
) -> dict[str, Any]:
    """A failed capture has bytes, but no published capability to invent or advertise."""
    if not fallback_allowed(failure.code, failure.detail):
        return E.error_envelope(request_id, failure, handles_valid=False)
    if len(data) > limits.max_source_bytes:
        return E.error_envelope(
            request_id, ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP"), handles_valid=False
        )
    try:
        text = assert_text(data)
        assert_no_secret(data, "SOURCE")
    except ShuntError as exc:
        return E.error_envelope(request_id, exc, handles_valid=False)
    summary = compact_tool_result(text, hard_chars=hard_chars)
    summary = summary.encode("utf-8")[: limits.max_extraction_bytes].decode(
        "utf-8", errors="ignore"
    )
    env = E.build(
        request_id=request_id,
        status="partial",
        code="LEGACY_COMPACTED",
        failure_detail=safe_failure_detail(failure.detail),
        coverage=E.Coverage(complete=False),
        sources=[],
        retryable=False,
        recovery=E.recovery_for(failure.code, handles_valid=False),
        guidance="Deterministic incumbent compaction, independent of any question; not model-derived and incomplete. Capture failed before a reusable handle was published.",
        legacy_compaction={
            "deterministic": True,
            "summary": summary,
            "summary_bytes": len(summary.encode("utf-8")),
            "original_bytes": len(data),
            "hard_cap_chars": hard_chars,
            "original_failure": failure.code,
        },
    )
    return fit_compaction(env, limits)


def fit_compaction(env: dict[str, Any], limits: Limits) -> dict[str, Any]:
    """Only trim the deterministic summary when JSON escaping consumes wire headroom."""
    block = env["legacy_compaction"]
    cap = envelope_byte_cap("legacy_compaction", limits)
    while E.serialized_bytes(env) > cap and block["summary"]:
        excess = E.serialized_bytes(env) - cap
        data = block["summary"].encode("utf-8")
        block["summary"] = data[: max(0, len(data) - excess)].decode("utf-8", errors="ignore")
        block["summary_bytes"] = len(block["summary"].encode("utf-8"))
    return enforce(env, limits)


def compact_paths_failure(
    request_id: str, paths: list[str], failure: ShuntError, config
) -> dict[str, Any]:
    """Initial read bootstrap failed: independently authorize every path before disclosure."""
    from .binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
    from .paths import authorize
    from .snapshot import snapshot_file

    snapshots = []
    try:
        for path in paths:
            authorized = authorize(path, config.path_policy())
            snapshots.append(
                snapshot_file(
                    authorized,
                    limits=config.limits,
                    media_type_hint=JSON_MEDIA_TYPE
                    if authorized.real.suffix.lower() == ".json"
                    else TEXT_MEDIA_TYPE,
                )
            )
    except ShuntError as exc:
        return E.error_envelope(request_id, exc, handles_valid=False)
    return compact_failure(
        request_id,
        snapshots[0].data,
        failure,
        limits=config.limits,
        hard_chars=config.reader.legacy_compaction_max_chars,
    )
