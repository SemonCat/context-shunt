"""The single bounded reply shape.

Every gate, reader and spill path returns this object. Construction goes through the
helpers below so an illegal status/code pairing or a coverage claim that outruns what
actually happened cannot be expressed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .errors import RETRYABLE_CODES, ShuntError
from .limits import SCHEMA_VERSION, legal_pair

OMISSION_REASONS = frozenset(
    {
        "BUDGET_EXCEEDED",
        "TIMEOUT",
        "CANCELLED",
        "CHUNK_FAILED",
        "MODEL_ERROR",
        "INVALID_MODEL_OUTPUT",
        "CITATION_INVALID",
        "UPSTREAM_TRUNCATED",
        "UNKNOWN_REMAINDER",
    }
)


def iso_expiry(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Coverage:
    complete: bool = False
    processed_chunks: int = 0
    planned_chunks: int = 0
    omitted: list[dict[str, Any]] = field(default_factory=list)
    upstream_truncated: bool | None = None

    def omit(self, source_id: str, selector: dict[str, Any], reason: str) -> None:
        if reason not in OMISSION_REASONS:
            raise ValueError(f"unknown omission reason: {reason}")
        self.omitted.append({"source_id": source_id, "selector": selector, "reason": reason})

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "processed_chunks": self.processed_chunks,
            "planned_chunks": self.planned_chunks,
            "omitted": list(self.omitted),
            "upstream_truncated": self.upstream_truncated,
        }


def build(
    *,
    request_id: str,
    status: str,
    code: str,
    answer: str = "",
    citations: list[dict[str, Any]] | None = None,
    coverage: Coverage | None = None,
    sources: list[dict[str, Any]] | None = None,
    retryable: bool | None = None,
    guidance: str | None = None,
    pointer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not legal_pair(status, code):
        raise ValueError(f"illegal status/code pairing: {status}/{code}")
    cov = coverage or Coverage()
    if status != "ok" and cov.complete:
        raise ValueError("only an ok result may claim complete coverage")
    if code == "SPILLED":
        if answer or citations:
            raise ValueError("SPILLED must carry no answer and no citations")
        if pointer is None:
            raise ValueError("SPILLED requires a pointer")
    env: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "status": status,
        "code": code,
        "answer": answer,
        "citations": list(citations or []),
        "coverage": cov.to_dict(),
        "sources": list(sources or []),
        "retryable": (code in RETRYABLE_CODES) if retryable is None else retryable,
    }
    if guidance:
        env["guidance"] = guidance
    if pointer is not None:
        env["pointer"] = pointer
    return env


def error_envelope(
    request_id: str, exc: ShuntError, *, guidance: str | None = None
) -> dict[str, Any]:
    """Map a bounded failure to an envelope. The exception message never rides along."""
    status = "blocked" if exc.code in _BLOCKED_CODES else "error"
    return build(
        request_id=request_id,
        status=status,
        code=exc.code,
        retryable=exc.retryable,
        guidance=guidance,
    )


_BLOCKED_CODES = frozenset(
    {"LARGE_READ", "UNCLASSIFIABLE_READ", "UNSAFE_SOURCE", "BINARY_UNSUPPORTED", "HOST_UNSAFE"}
)


def serialized_bytes(envelope: dict[str, Any]) -> int:
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
