"""The final output boundary.

Nothing reaches a host without passing through here. By default, the guard measures the
*serialized* envelope - every field, every citation, every metadata value - against the cap
that applies to its ``result_kind``, re-checks the per-field caps, refuses unknown fields,
and refuses any citation not marked verified. A trusted Python/Hermes configuration may
skip only reader answer-output caps for ``ANSWERED``; structural, citation, secret, and all
non-reader checks still run. If the guard itself fails it emits a fixed small error envelope
rather than falling back to whatever it was given.

Two caps, both normative
------------------------
Most envelopes are capped at 16 KiB. An ``ANSWERED`` envelope is the sole exception when
its trusted deployment explicitly disables reader answer-output caps. A deterministic
extraction or a stats page carries a
bounded payload of its own - up to 16 KiB of exact snapshot bytes, or one page of records
- so those are measured against ``max_extended_envelope_bytes`` (20 KiB), leaving 4 KiB
for the envelope around a full-size extraction. The extraction payload itself is measured
separately against the 16 KiB per-result cap, so the escape hatch cannot widen by hiding
bytes in envelope overhead.

Adding a field to the envelope means adding it here too. ``_ALLOWED_KEYS`` is a closed
set, and an envelope carrying anything outside it is converted to the fixed error
envelope rather than published.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .envelope import build, serialized_bytes
from .errors import ShuntError
from .limits import (
    DEFAULT_LIMITS,
    EMITTED_SCHEMA_VERSION,
    SUPPORTED_REQUEST_VERSIONS,
    Limits,
    envelope_byte_cap,
    legal_pair,
)
from .paths import contains_secret_marker
from .schema import validate_envelope

_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "status",
        "code",
        "answer",
        "citations",
        "coverage",
        "sources",
        "retryable",
        "guidance",
        "pointer",
        # -- 1.1 --
        "result_kind",
        "provenance",
        "accounting_id",
        "extraction",
        "legacy_compaction",
        "failure_detail",
        "stats",
        "recovery",
        "import_receipt",
    }
)
_ALLOWED_SOURCE_KEYS = frozenset({"source_id", "snapshot_id", "media_type", "bytes", "expires_at"})
_REQUIRED_V11_KEYS = frozenset({"result_kind", "provenance", "accounting_id"})


class OutputGuardError(Exception):
    """Raised only inside the guard; adapters convert it to the fixed error envelope."""


_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_SAFE_ACCOUNTING_ID = re.compile(r"acc_[0-9a-f]{16}\Z")
_SAFE_RAW_ARTIFACT_PATH = re.compile(
    r"/(?:(?!\.\.?/)[^\x00-\x1f\x7f/]+/)*"
    r"artifacts/scp_[0-9a-f]{32}/src_[0-9a-f]{16}\.[0-9a-f]{32}\.txt\Z"
)


def fixed_error(request_id: str, code: str = "LIMIT_EXCEEDED") -> dict[str, Any]:
    """The smallest legal envelope. Used when nothing else can be trusted."""
    safe_id = (
        request_id
        if isinstance(request_id, str) and _SAFE_REQUEST_ID.fullmatch(request_id)
        else "req_unknown"
    )
    safe_code = code if legal_pair("error", code) else "LIMIT_EXCEEDED"
    return build(
        request_id=safe_id,
        status="error",
        code=safe_code,
        retryable=False,
    )


def enforce(
    envelope: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
    *,
    enforce_reader_output_caps: bool = True,
) -> dict[str, Any]:
    """Validate and return the envelope, or raise :class:`OutputGuardError`."""
    if not isinstance(envelope, dict):
        raise OutputGuardError("not an object")
    unknown = set(envelope) - _ALLOWED_KEYS
    if unknown:
        raise OutputGuardError("unknown envelope field")
    version = envelope.get("schema_version")
    if version not in SUPPORTED_REQUEST_VERSIONS:
        raise OutputGuardError("bad schema version")
    status, code = envelope.get("status"), envelope.get("code")
    if not isinstance(status, str) or not isinstance(code, str) or not legal_pair(status, code):
        raise OutputGuardError("illegal status/code pairing")

    _check_version_fields(envelope, version)
    cap_answer_output = enforce_reader_output_caps or code != "ANSWERED"

    answer = envelope.get("answer")
    if not isinstance(answer, str):
        raise OutputGuardError("answer must be a string")
    if cap_answer_output and len(answer.encode("utf-8")) > limits.max_answer_bytes:
        raise OutputGuardError("answer over cap")
    if contains_secret_marker(answer.encode("utf-8")):
        raise OutputGuardError("secret marker in answer")

    citations = envelope.get("citations")
    if not isinstance(citations, list) or (
        cap_answer_output and len(citations) > limits.max_citations
    ):
        raise OutputGuardError("citations over cap")
    for citation in citations:
        if not isinstance(citation, dict) or citation.get("verified") is not True:
            raise OutputGuardError("unverified citation")
        quote = citation.get("quote", "")
        if not isinstance(quote, str) or (
            cap_answer_output and len(quote.encode("utf-8")) > limits.max_quote_bytes
        ):
            raise OutputGuardError("quote over cap")
        if contains_secret_marker(quote.encode("utf-8")):
            raise OutputGuardError("secret marker in quote")

    coverage = envelope.get("coverage")
    if not isinstance(coverage, dict):
        raise OutputGuardError("coverage missing")
    if status != "ok" and coverage.get("complete") is True:
        raise OutputGuardError("non-ok result claims complete coverage")
    if coverage.get("complete") is True and coverage.get("upstream_truncated") is not False:
        raise OutputGuardError("complete coverage requires known-complete origin")

    sources = envelope.get("sources")
    if not isinstance(sources, list):
        raise OutputGuardError("sources missing")
    for source in sources:
        if not isinstance(source, dict) or set(source) - _ALLOWED_SOURCE_KEYS:
            raise OutputGuardError("source handle carries an unexpected field")
        source_bytes = source.get("bytes")
        if (
            type(source_bytes) is not int
            or source_bytes < 0
            or source_bytes > limits.max_source_bytes
        ):
            raise OutputGuardError("source bytes over cap")

    if code in ("SPILLED", "EXTRACTED", "STATS", "IMPORTED", "LEGACY_COMPACTED") and (
        answer or citations
    ):
        raise OutputGuardError(f"{code} must not carry an answer")

    _check_extraction(envelope, limits)
    _check_legacy_compaction(envelope, limits)

    if cap_answer_output and serialized_bytes(envelope) > envelope_byte_cap(
        envelope.get("result_kind"), limits
    ):
        raise OutputGuardError("envelope over byte cap")
    if not validate_envelope(envelope, enforce_reader_output_caps=cap_answer_output):
        raise OutputGuardError("envelope schema violation")
    return envelope


def _check_version_fields(envelope: dict[str, Any], version: str) -> None:
    """Version and content must agree in both directions.

    A 1.1 envelope missing a mandatory 1.1 field is refused rather than published with the
    field quietly absent; a 1.0 envelope carrying a 1.1 field is refused rather than
    published under a version string that understates what it contains.
    """
    present_v11 = {key for key in _ALLOWED_KEYS if key in envelope} & (
        _REQUIRED_V11_KEYS
        | {
            "failure_detail",
            "extraction",
            "legacy_compaction",
            "stats",
            "recovery",
            "import_receipt",
        }
    )
    if version == "1.0":
        if present_v11:
            raise OutputGuardError("1.0 envelope carries a 1.1 field")
        return
    missing = _REQUIRED_V11_KEYS - set(envelope)
    if missing:
        raise OutputGuardError("1.1 envelope missing a mandatory field")
    if not _SAFE_ACCOUNTING_ID.fullmatch(str(envelope.get("accounting_id", ""))):
        raise OutputGuardError("bad accounting id")
    provenance = envelope.get("provenance")
    if not isinstance(provenance, dict):
        raise OutputGuardError("provenance must be an object")
    derived = provenance.get("derived")
    if not isinstance(derived, bool):
        raise OutputGuardError("provenance.derived must be a boolean")
    if version == "1.3":
        started = provenance.get("attempts_started")
        measured = provenance.get("attempts_usage_complete")
        usage_complete = provenance.get("usage_complete")
        if (
            type(started) is not int
            or type(measured) is not int
            or not isinstance(usage_complete, bool)
            or measured > started
            or usage_complete is not (measured == started)
        ):
            raise OutputGuardError("provenance usage counts disagree")
    if derived != (envelope.get("result_kind") == "model_derived"):
        raise OutputGuardError("provenance.derived disagrees with result_kind")


def _check_extraction(envelope: dict[str, Any], limits: Limits) -> None:
    extraction = envelope.get("extraction")
    if extraction is None:
        return
    if not isinstance(extraction, dict):
        raise OutputGuardError("extraction must be an object")
    if extraction.get("deterministic") is not True:
        raise OutputGuardError("extraction must declare itself deterministic")
    segments = extraction.get("segments")
    if not isinstance(segments, list) or len(segments) > limits.inspect_max_segments:
        raise OutputGuardError("extraction segments over cap")
    total = 0
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise OutputGuardError("extraction segment malformed")
        text = segment["text"].encode("utf-8")
        if contains_secret_marker(text):
            raise OutputGuardError("secret marker in extraction")
        total += len(text)
    if total > limits.max_extraction_bytes:
        raise OutputGuardError("extraction over per-result cap")
    if extraction.get("result_bytes") != total:
        raise OutputGuardError("extraction result_bytes disagrees with its segments")


def _check_legacy_compaction(envelope: dict[str, Any], limits: Limits) -> None:
    block = envelope.get("legacy_compaction")
    if block is None:
        return
    if not isinstance(block, dict):
        raise OutputGuardError("legacy_compaction must be an object")
    if (
        envelope.get("code") != "LEGACY_COMPACTED"
        or envelope.get("status") != "partial"
        or envelope.get("result_kind") != "legacy_compaction"
        or envelope.get("answer")
        or envelope.get("citations")
    ):
        raise OutputGuardError("legacy_compaction cannot masquerade as an answer")
    coverage = envelope.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("complete") is not False:
        raise OutputGuardError("legacy_compaction must declare incomplete coverage")
    if block.get("deterministic") is not True:
        raise OutputGuardError("legacy_compaction must declare itself deterministic")
    summary = block.get("summary")
    if not isinstance(summary, str):
        raise OutputGuardError("legacy_compaction summary malformed")
    text = summary.encode("utf-8")
    if contains_secret_marker(text):
        raise OutputGuardError("secret marker in legacy_compaction summary")
    if len(text) > limits.max_extraction_bytes:
        raise OutputGuardError("legacy_compaction over per-result cap")
    if block.get("summary_bytes") != len(text):
        raise OutputGuardError("legacy_compaction summary_bytes disagrees with summary")
    artifact_path = block.get("raw_artifact_path")
    if artifact_path is not None and (
        not isinstance(artifact_path, str)
        or envelope.get("schema_version") not in ("1.2", "1.3")
        or _SAFE_RAW_ARTIFACT_PATH.fullmatch(artifact_path) is None
        or len(artifact_path) > 1024
        or len(artifact_path.encode("utf-8")) > 4096
        or not isinstance(block.get("source_id"), str)
        or not isinstance(block.get("snapshot_id"), str)
    ):
        raise OutputGuardError("legacy_compaction raw artifact path malformed")
    # provenance.derived must already be false for this envelope (checked in
    # `_check_version_fields` via result_kind agreement); this is the belt to that
    # braces - a compaction block can never accompany a claim of model derivation.
    provenance = envelope.get("provenance")
    if not isinstance(provenance, dict) or (
        provenance.get("derived") is not False
        or provenance.get("label") != "legacy_compaction"
        or provenance.get("citations_mechanically_verified") is not False
    ):
        raise OutputGuardError("legacy_compaction provenance is authoritative")


def enforce_or_fixed(
    envelope: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
    *,
    enforce_reader_output_caps: bool = True,
) -> dict[str, Any]:
    """Never raises. A guard failure yields the fixed error envelope, never the input."""
    request_id = "req_unknown"
    try:
        candidate = envelope.get("request_id") if isinstance(envelope, dict) else None
        if isinstance(candidate, str):
            request_id = candidate
        return enforce(
            envelope,
            limits,
            enforce_reader_output_caps=enforce_reader_output_caps,
        )
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
    "EMITTED_SCHEMA_VERSION",
    "OutputGuardError",
    "build",
    "enforce",
    "enforce_or_fixed",
    "enforce_tool_result",
    "fixed_error",
]
