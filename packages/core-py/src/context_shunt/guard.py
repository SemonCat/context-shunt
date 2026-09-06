"""The final output boundary.

Nothing reaches a host without passing through here. The guard measures the *serialized*
envelope - every field, every citation, every metadata value - against the 16 KiB cap,
re-checks the per-field caps, refuses unknown fields, and refuses any citation not marked
verified. If the guard itself fails it emits a fixed small error envelope rather than
falling back to whatever it was given.
"""

from __future__ import annotations

import json
from typing import Any

from .envelope import build, serialized_bytes
from .errors import ShuntError
from .limits import DEFAULT_LIMITS, SCHEMA_VERSION, Limits, legal_pair
from .paths import contains_secret_marker

_ALLOWED_KEYS = frozenset(
    {
        "schema_version", "request_id", "status", "code", "answer", "citations",
        "coverage", "sources", "retryable", "guidance", "pointer",
    }
)
_ALLOWED_SOURCE_KEYS = frozenset(
    {"source_id", "snapshot_id", "media_type", "bytes", "expires_at"}
)


class OutputGuardError(Exception):
    """Raised only inside the guard; adapters convert it to the fixed error envelope."""


def fixed_error(request_id: str, code: str = "LIMIT_EXCEEDED") -> dict[str, Any]:
    """The smallest legal envelope. Used when nothing else can be trusted."""
    safe_id = request_id if isinstance(request_id, str) and request_id[:64].strip() else "req_unknown"
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": safe_id[:64],
        "status": "error",
        "code": code,
        "answer": "",
        "citations": [],
        "coverage": {
            "complete": False,
            "processed_chunks": 0,
            "planned_chunks": 0,
            "omitted": [],
            "upstream_truncated": None,
        },
        "sources": [],
        "retryable": False,
    }


def enforce(envelope: dict[str, Any], limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Validate and return the envelope, or raise :class:`OutputGuardError`."""
    if not isinstance(envelope, dict):
        raise OutputGuardError("not an object")
    unknown = set(envelope) - _ALLOWED_KEYS
    if unknown:
        raise OutputGuardError("unknown envelope field")
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise OutputGuardError("bad schema version")
    status, code = envelope.get("status"), envelope.get("code")
    if not isinstance(status, str) or not isinstance(code, str) or not legal_pair(status, code):
        raise OutputGuardError("illegal status/code pairing")

    answer = envelope.get("answer")
    if not isinstance(answer, str):
        raise OutputGuardError("answer must be a string")
    if len(answer.encode("utf-8")) > limits.max_answer_bytes:
        raise OutputGuardError("answer over cap")
    if contains_secret_marker(answer.encode("utf-8")):
        raise OutputGuardError("secret marker in answer")

    citations = envelope.get("citations")
    if not isinstance(citations, list) or len(citations) > limits.max_citations:
        raise OutputGuardError("citations over cap")
    for citation in citations:
        if not isinstance(citation, dict) or citation.get("verified") is not True:
            raise OutputGuardError("unverified citation")
        quote = citation.get("quote", "")
        if not isinstance(quote, str) or len(quote.encode("utf-8")) > limits.max_quote_bytes:
            raise OutputGuardError("quote over cap")
        if contains_secret_marker(quote.encode("utf-8")):
            raise OutputGuardError("secret marker in quote")

    coverage = envelope.get("coverage")
    if not isinstance(coverage, dict):
        raise OutputGuardError("coverage missing")
    if status != "ok" and coverage.get("complete") is True:
        raise OutputGuardError("non-ok result claims complete coverage")

    for source in envelope.get("sources", []):
        if not isinstance(source, dict) or set(source) - _ALLOWED_SOURCE_KEYS:
            raise OutputGuardError("source handle carries an unexpected field")
        if int(source.get("bytes", 0)) > limits.max_source_bytes:
            raise OutputGuardError("source bytes over cap")

    if code == "SPILLED" and (answer or citations):
        raise OutputGuardError("SPILLED must not carry an answer")

    if serialized_bytes(envelope) > limits.max_envelope_bytes:
        raise OutputGuardError("envelope over byte cap")
    return envelope


def enforce_or_fixed(envelope: dict[str, Any], limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Never raises. A guard failure yields the fixed error envelope, never the input."""
    request_id = "req_unknown"
    try:
        candidate = envelope.get("request_id") if isinstance(envelope, dict) else None
        if isinstance(candidate, str):
            request_id = candidate
        return enforce(envelope, limits)
    except Exception:
        return fixed_error(request_id)


def enforce_tool_result(payload: Any, limits: Limits = DEFAULT_LIMITS) -> int:
    """Serialized size of a complete tool result, including every block and metadata field."""
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_reject)
    except (TypeError, ValueError):
        raise ShuntError("LIMIT_EXCEEDED", "UNSERIALIZABLE_RESULT") from None
    return len(text.encode("utf-8"))


def _reject(_value: Any) -> Any:
    raise TypeError("unserializable value")


__all__ = [
    "OutputGuardError",
    "build",
    "enforce",
    "enforce_or_fixed",
    "enforce_tool_result",
    "fixed_error",
]
