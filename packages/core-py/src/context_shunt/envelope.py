"""The single bounded reply shape.

Every gate, reader, inspect, stats and spill path returns this object. Construction goes
through the helpers below so an illegal status/code pairing, a coverage claim that outruns
what actually happened, or a 1.1 envelope missing its mandatory provenance cannot be
expressed.

Revision 1.1 adds three mandatory fields to every envelope this core emits:

``result_kind``
    What the envelope is, so a model-derived answer can never be mistaken for raw source.
``provenance``
    Where the content came from, including the truthful attribution status.
``accounting_id``
    An opaque pointer to the operation's metrics record. The record lives in the store and
    is written *after* the envelope is serialized, so the envelope never measures itself.

A 1.0 envelope stays exactly a 1.0 envelope: :func:`build` will not attach a 1.1 block to
one, because declaring the older revision while carrying newer fields is a version lie
rather than a compatible extension.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .errors import RETRYABLE_CODES, ShuntError
from .limits import EMITTED_SCHEMA_VERSION, legal_pair
from .provenance import Provenance, ProvenanceLabel, ResultKind, deterministic

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
        "DISCLOSURE_EXHAUSTED",
        "SCAN_BUDGET_EXHAUSTED",
        "PROVENANCE_UNAVAILABLE",
    }
)

RECOVERY_ACTIONS = frozenset(
    {
        "RETRY_SAME_QUESTION",
        "REFINE_QUESTION_SAME_SNAPSHOT",
        "INSPECT_HANDLE",
        "NARROW_SELECTOR",
        "WAIT_AND_RETRY",
        "RECAPTURE_SOURCE",
        "CONFIGURE_READER_MODEL",
        "REVIEW_DISCLOSURE_BUDGET",
        "NONE",
    }
)

#: Failures that leave the caller's handles intact, so recovery is a retry or a refined
#: question over the same snapshot - never a recapture of the source.
_HANDLES_SURVIVE = frozenset(
    {
        "MODEL_ERROR",
        "INVALID_MODEL_OUTPUT",
        "CITATION_INVALID",
        "TIMEOUT",
        "CANCELLED",
        "LIMIT_EXCEEDED",
        "DISCLOSURE_EXHAUSTED",
        "PROVENANCE_UNAVAILABLE",
        "INVALID_REQUEST",
    }
)

_RECOVERY_BY_CODE: dict[str, tuple[str, ...]] = {
    "MODEL_ERROR": ("RETRY_SAME_QUESTION", "REFINE_QUESTION_SAME_SNAPSHOT", "INSPECT_HANDLE"),
    "INVALID_MODEL_OUTPUT": ("RETRY_SAME_QUESTION", "INSPECT_HANDLE"),
    "CITATION_INVALID": ("REFINE_QUESTION_SAME_SNAPSHOT", "INSPECT_HANDLE"),
    "TIMEOUT": ("WAIT_AND_RETRY", "NARROW_SELECTOR", "INSPECT_HANDLE"),
    "CANCELLED": ("RETRY_SAME_QUESTION",),
    "LIMIT_EXCEEDED": ("NARROW_SELECTOR", "INSPECT_HANDLE"),
    "DISCLOSURE_EXHAUSTED": ("REVIEW_DISCLOSURE_BUDGET", "REFINE_QUESTION_SAME_SNAPSHOT"),
    "PROVENANCE_UNAVAILABLE": ("CONFIGURE_READER_MODEL", "INSPECT_HANDLE"),
    "SOURCE_EXPIRED": ("RECAPTURE_SOURCE",),
    "SOURCE_CHANGED": ("RECAPTURE_SOURCE",),
    "STORE_FAILED": ("RECAPTURE_SOURCE",),
    "SPILL_FAILED": ("RECAPTURE_SOURCE",),
}


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
    result_kind: ResultKind | None = None,
    provenance: Provenance | None = None,
    accounting_id: str | None = None,
    extraction: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
    schema_version: str = EMITTED_SCHEMA_VERSION,
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
    if code == "EXTRACTED":
        if answer or citations:
            raise ValueError("EXTRACTED must carry no answer and no citations")
        if extraction is None:
            raise ValueError("EXTRACTED requires an extraction block")
    if code == "STATS":
        if answer or citations:
            raise ValueError("STATS must carry no answer and no citations")
        if stats is None:
            raise ValueError("STATS requires a stats block")

    env: dict[str, Any] = {
        "schema_version": schema_version,
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

    if schema_version != "1.1":
        # A 1.0 envelope carries no 1.1 block, ever. Attaching one while still declaring
        # 1.0 would make the version string a lie rather than a compatible extension.
        return env

    kind = result_kind or _default_result_kind(code)
    prov = provenance or _default_provenance(kind)
    if prov.derived != (kind is ResultKind.MODEL_DERIVED):
        raise ValueError("provenance.derived must agree with result_kind")
    env["result_kind"] = kind.value
    env["provenance"] = prov.to_dict()
    env["accounting_id"] = accounting_id or _PLACEHOLDER_ACCOUNTING_ID
    if extraction is not None:
        env["extraction"] = extraction
    if stats is not None:
        env["stats"] = stats
    if recovery is not None:
        env["recovery"] = _validated_recovery(recovery)
    return env


#: Used only when a caller builds an envelope without an operation record, which happens
#: for the fixed fallback envelope the guard emits when nothing else can be trusted.
_PLACEHOLDER_ACCOUNTING_ID = "acc_" + "0" * 16


def _default_result_kind(code: str) -> ResultKind:
    if code == "EXTRACTED":
        return ResultKind.DETERMINISTIC_EXTRACTION
    if code == "STATS":
        return ResultKind.STATS
    if code == "SPILLED":
        return ResultKind.POINTER
    if code in ("LARGE_READ", "UNCLASSIFIABLE_READ"):
        return ResultKind.GATE_DECISION
    if code in ("ANSWERED", "NO_MATCH"):
        return ResultKind.MODEL_DERIVED
    return ResultKind.FAILURE


def _default_provenance(kind: ResultKind) -> Provenance:
    labels = {
        ResultKind.DETERMINISTIC_EXTRACTION: ProvenanceLabel.DETERMINISTIC_EXTRACTION,
        ResultKind.STATS: ProvenanceLabel.SESSION_METRICS,
        ResultKind.POINTER: ProvenanceLabel.POINTER_ONLY,
        ResultKind.GATE_DECISION: ProvenanceLabel.GATE_DECISION,
        ResultKind.FAILURE: ProvenanceLabel.NO_MODEL_OUTPUT,
    }
    if kind is ResultKind.MODEL_DERIVED:
        # A model-derived envelope must be given real provenance by the reader; there is
        # no honest default, so refuse rather than invent one.
        raise ValueError("a model-derived envelope requires explicit provenance")
    return deterministic(labels[kind])


def _validated_recovery(recovery: dict[str, Any]) -> dict[str, Any]:
    actions = recovery.get("actions") or []
    if not isinstance(actions, list) or any(a not in RECOVERY_ACTIONS for a in actions):
        raise ValueError("unknown recovery action")
    return {"handles_valid": bool(recovery.get("handles_valid")), "actions": list(actions[:6])}


def recovery_for(code: str, *, handles_valid: bool | None = None) -> dict[str, Any]:
    """Deterministic next steps for a failure code.

    ``handles_valid`` is stated explicitly because it is the fact a caller most needs: a
    reader, provider, citation or provenance failure leaves every handle intact, so the
    right move is a retry or a refined question over the *same* snapshot. Only a failure
    of the handle itself calls for a recapture.
    """
    valid = code in _HANDLES_SURVIVE if handles_valid is None else handles_valid
    return {
        "handles_valid": valid,
        "actions": list(_RECOVERY_BY_CODE.get(code, ("NONE",))),
    }


def error_envelope(
    request_id: str,
    exc: ShuntError,
    *,
    guidance: str | None = None,
    accounting_id: str | None = None,
    provenance: Provenance | None = None,
    sources: list[dict[str, Any]] | None = None,
    handles_valid: bool | None = None,
    schema_version: str = EMITTED_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Map a bounded failure to an envelope. The exception message never rides along."""
    status = "blocked" if exc.code in _BLOCKED_CODES else "error"
    return build(
        request_id=request_id,
        status=status,
        code=exc.code,
        retryable=exc.retryable,
        guidance=guidance,
        sources=sources,
        accounting_id=accounting_id,
        provenance=provenance,
        recovery=recovery_for(exc.code, handles_valid=handles_valid),
        schema_version=schema_version,
    )


_BLOCKED_CODES = frozenset(
    {"LARGE_READ", "UNCLASSIFIABLE_READ", "UNSAFE_SOURCE", "BINARY_UNSUPPORTED", "HOST_UNSAFE"}
)


def serialized(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def serialized_bytes(envelope: dict[str, Any]) -> int:
    return len(serialized(envelope).encode("utf-8"))
