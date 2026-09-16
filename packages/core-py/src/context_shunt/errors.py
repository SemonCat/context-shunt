"""Bounded, content-free failures.

Every failure that reaches an adapter is a :class:`ShuntError` carrying a contract
``code`` and a short operator-safe message. Raw payloads, prompts, provider error
bodies, shell arguments, secrets and absolute paths never enter these objects — that
is the single invariant the ``no-raw-leak`` gate measures.
"""

from __future__ import annotations

from typing import Any

# Short, fixed operator-facing text per code. Nothing here is derived from input.
SAFE_MESSAGES: dict[str, str] = {
    "INVALID_REQUEST": "request rejected by the v1 contract",
    "UNSUPPORTED_VERSION": "unsupported contract version",
    "UNSAFE_SOURCE": "source rejected by the path or content policy",
    "SOURCE_CHANGED": "source changed while a consistent snapshot was being taken",
    "SOURCE_EXPIRED": "source handle is expired or belongs to another session",
    "BINARY_UNSUPPORTED": "binary or unsupported content is not read by v1",
    "LIMIT_EXCEEDED": "a configured limit was exceeded",
    "TIMEOUT": "deadline exceeded",
    "MODEL_ERROR": "reader model call failed",
    "INVALID_MODEL_OUTPUT": "reader model output did not match the required shape",
    "CITATION_INVALID": "no assertion survived citation verification",
    "SPILL_FAILED": "oversized result could not be spilled",
    "STORE_FAILED": "snapshot store could not publish or authorize a handle",
    "DISCLOSURE_EXHAUSTED": "cumulative disclosure ceiling reached for this source or session",
    "PROVENANCE_UNAVAILABLE": "reader provenance could not be established under the configured policy",
    "HOST_UNSAFE": "host cannot guarantee the required interception order",
    "CANCELLED": "request cancelled",
    "LARGE_READ": "full read exceeds the configured line or byte threshold",
    "UNCLASSIFIABLE_READ": "read-like command could not be proven bounded and safe",
    "UPSTREAM_TRUNCATED": "upstream result was already truncated",
    "EXTRACTED": "deterministic extraction returned exact snapshot bytes",
    "IMPORTED": "external producer artifact adopted as an immutable snapshot handle",
    "LEGACY_COMPACTED": "reader failure covered by a deterministic legacy-shaped compaction; not model-derived",
    "STATS": "session accounting returned",
}

RETRYABLE_CODES = frozenset({"MODEL_ERROR", "TIMEOUT"})


class ShuntError(Exception):
    """A failure that is safe to surface. ``detail`` is a fixed enum-like token only."""

    #: Usage the provider already billed for the call that produced this failure, when
    #: there was one. A rejected *reply* is still a paid *call*, so the counts travel with
    #: the error rather than being discarded with the text. Never carries prompt or reply
    #: content - only token counts - so it cannot widen what an error may reveal.
    #: How many provider attempts the failing operation actually made, when a composite
    #: provider made more than one. A failure is as billable as a success, so the count
    #: travels with it; ``1`` is assumed when nothing set it.
    #: How many of those attempts reported complete usage. Without it the reader could
    #: only guess - it counted one aggregate error as one report, so a chain of two billed
    #: candidates looked like one usage-complete attempt out of two started.
    #: Bytes of reply this core measured for itself on a call whose *usage claim* was
    #: refused. Not a token count and not the provider's word for anything: it is what the
    #: response body weighed, so the ledger can build a conservative estimate instead of
    #: publishing ``output_tokens: 0`` for a call that plainly produced output. A byte
    #: count reveals no content, so it cannot widen what an error may say.
    #: One per-call identity record for each physical call the failing operation made,
    #: when a composite provider made more than one. A call that failed observed nothing
    #: about which model ran, so these records are mostly unknown - which is the point:
    #: without them the reader could only spread the *answering* call's identity over
    #: calls that never reported one.
    __slots__ = (
        "billed_usage",
        "call_identities",
        "code",
        "detail",
        "internal_attempts",
        "response_bytes",
        "retryable",
        "usage_complete_attempts",
    )

    def __init__(self, code: str, detail: str | None = None, retryable: bool | None = None):
        self.billed_usage: Any | None = None
        self.call_identities: tuple[Any, ...] = ()
        self.internal_attempts: int = 1
        self.response_bytes: int | None = None
        self.usage_complete_attempts: int | None = None
        if code not in SAFE_MESSAGES:
            raise ValueError(f"unknown error code: {code}")
        if detail is not None and not _is_safe_detail(detail):
            raise ValueError("error detail must be a short bounded token, not free text")
        self.code = code
        self.detail = detail
        self.retryable = RETRYABLE_CODES.__contains__(code) if retryable is None else retryable
        super().__init__(self.safe_message())

    def safe_message(self) -> str:
        base = SAFE_MESSAGES[self.code]
        return f"{base} ({self.detail})" if self.detail else base


def _is_safe_detail(detail: str) -> bool:
    """Details are bounded UPPER_SNAKE tokens so no payload can ride along."""
    return (
        0 < len(detail) <= 48
        and detail == detail.upper()
        and all(ch.isalnum() or ch == "_" for ch in detail)
    )


class CancelledError(ShuntError):
    def __init__(self, detail: str | None = None):
        super().__init__("CANCELLED", detail, retryable=False)


class DeadlineExceeded(ShuntError):
    def __init__(self, detail: str | None = None):
        super().__init__("TIMEOUT", detail, retryable=True)


SAFE_FAILURE_DETAILS = frozenset(
    (
        "ACCOUNTING_FAILED",
        "AMBIGUOUS_RESPONSE_SHAPE",
        "ANSWER_OVER_CAP",
        "ANSWER_OVER_ENVELOPE",
        "ARTIFACT_HASH_MISMATCH",
        "ARTIFACT_IMPORT_DISABLED",
        "ARTIFACT_SIZE_MISMATCH",
        "ATTRIBUTION_UNPROVEN",
        "AVAILABILITY_EXHAUSTED",
        "BAD_ATTRIBUTION_POLICY",
        "BAD_BLOB_HASH",
        "BAD_BRIDGE_SHAPE",
        "BAD_BYTE_RANGE",
        "BAD_CONFIGURATION",
        "BAD_CURSOR",
        "BAD_DISCLOSURE_BYTES",
        "BAD_DISCLOSURE_KIND",
        "BAD_HANDLE_KIND",
        "BAD_JSON",
        "BAD_LIMIT_OVERRIDE",
        "BAD_LINE_RANGE",
        "BAD_POINTER",
        "BAD_RESPONSE_SHAPE",
        "BAD_SCHEMA_VERSION",
        "BAD_SELECTOR",
        "BAD_USAGE",
        "BASELINE_FAILED",
        "BATCH_TOO_LARGE",
        "BINARY_CONTENT",
        "BLOB_CONTENT_MISMATCH",
        "BLOB_METADATA_CONFLICT",
        "BLOB_MISSING",
        "BLOB_READ_FAILED",
        "BLOB_STAT_FAILED",
        "CACHE_INSIDE_IMPORT_ROOT",
        "CHUNK_BOUNDARY_INVALID",
        "CHUNK_OVER_TOKEN_CAP",
        "CONSUMER_UNAVAILABLE",
        "CURSOR_KEY_MISSING",
        "DDL_VERSION_MISMATCH",
        "DISCLOSURE_FAILED",
        "EMPTY_FALLBACK",
        "EMPTY_QUESTION",
        "EXTRACTION_REFUSED",
        "FIELD_REQUIRES_1_1",
        "HARDLINKED",
        "IDENTITY_CHANGED",
        "IMPORT_FAILED",
        "INSPECT_DISABLED",
        "INTERNAL_ERROR",
        "INVALID_ENCODING",
        "INVALID_JSON",
        "INVALID_SNAPSHOT_ID",
        "JSON_CYCLE",
        "JSON_TOO_DEEP",
        "JSON_TOO_MANY_NODES",
        "JSON_UNSUPPORTED_VALUE",
        "LEGACY_COMPACTION_FAILURE_UNKNOWN",
        "LIMIT_MAY_ONLY_NARROW",
        "LINE_OUT_OF_RANGE",
        "MANIFEST_NOT_JSON",
        "MANIFEST_NOT_OBJECT",
        "MANIFEST_OVER_BYTE_CAP",
        "MANIFEST_SCHEMA_NOT_ALLOWED",
        "MANIFEST_SCHEMA_UNDECLARED",
        "MANIFEST_SCHEMA_UNKNOWN",
        "MANIFEST_SCHEMA_VIOLATION",
        "MANIFEST_SOURCE_AMBIGUOUS",
        "MANIFEST_UNSUPPORTED_URI",
        "MARKER_NOT_PUBLISHED",
        "MIGRATION_FAILED",
        "MODEL_CALL",
        "MODEL_OUTPUT_OVER_CAP",
        "MODEL_SUBSTITUTED",
        "MODIFIED_DURING_READ",
        "NEEDLE_OVER_CAP",
        "NESTED_CHAIN_LIMITS_DIFFER",
        "NOT_FOUND",
        "NOT_JSON",
        "NOT_OBJECT",
        "NOT_REGULAR_FILE",
        "NO_ENVELOPE_HEADROOM",
        "NO_IMPORT_ROOT",
        "NO_PROVIDER",
        "NO_SOURCE",
        "NO_VALID_EVIDENCE",
        "NO_WORKSPACE_ROOT",
        "OPEN_FAILED",
        "OPERATION_NOT_ACCEPTED_HERE",
        "OPERATION_REQUIRES_1_1",
        "OTHER",
        "OUTSIDE_WORKSPACE_ROOT",
        "PERMISSION_FAILED",
        "PLAN",
        "POINTER_NOT_FOUND",
        "PROBE",
        "PROVIDER_CALL_FAILED",
        "PUBLISH",
        "PUBLISH_FAILED",
        "QUESTION_OVER_BYTE_CAP",
        "READER_DISABLED",
        "RECORD_OUT_OF_RANGE",
        "RELATIVE_PATH",
        "REQUEST_OVER_TOKEN_CAP",
        "RESOLVE",
        "RESULT_OVER_SOURCE_CAP",
        "REVOKE_FAILED",
        "SCHEMA_VIOLATION",
        "SCOPE_CLOSED",
        "SCOPE_CLOSE_FAILED",
        "SCOPE_INCOMPLETE",
        "SCOPE_OPEN_FAILED",
        "SEARCH_INDEX_MISMATCH",
        "SEARCH_MAX_MATCHES_EXHAUSTED",
        "SECRET_IN_ANSWER",
        "SECRET_IN_QUESTION",
        "SECRET_IN_QUOTE",
        "SECRET_IN_SOURCE",
        "SECRET_PATH",
        "SERIALIZE_FAILED",
        "SNAPSHOT_MISMATCH",
        "SOURCE_OVER_BYTE_CAP",
        "SPILL_INSIDE_WORKSPACE",
        "STALE_GENERATION",
        "STATS_DISABLED",
        "STORE_BYTE_QUOTA",
        "STORE_ENTRY_QUOTA",
        "SWEEP_FAILED",
        "SYMLINK",
        "TEMP_RECORD_FAILED",
        "TOOL_ARGS_VIOLATION",
        "TOOL_RESULT_CAPTURE_CONFIG_CONFLICT",
        "TOO_MANY_SOURCES",
        "UNIT_OVER_PAGE_BUDGET",
        "UNIT_OVER_WIRE_BUDGET",
        "UNKNOWN_BLOCK",
        "UNKNOWN_HANDLE",
        "UNKNOWN_LIMIT_OVERRIDE",
        "UNKNOWN_OPERATION",
        "UNSAFE_BLOB_PATH",
        "UNSAFE_CACHE_PATH",
        "UNSERIALIZABLE_RESULT",
        "UNSPECIFIED",
        "UNSUPPORTED_BLOCK",
        "UNSUPPORTED_SELECTOR",
        "UTF8_RANGE_BOUNDARY",
        "WRITER_OPERATION_UNSUPPORTED",
        "WRITER_UNSUPPORTED_CONFIGURATION",
        "WRITE_FAILED",
        "ZERO_RESULT_BUDGET",
    )
)


def safe_failure_detail(detail: str | None) -> str:
    return (
        detail if detail in SAFE_FAILURE_DETAILS else "UNSPECIFIED" if detail is None else "OTHER"
    )


CAPACITY_FAILURE_DETAILS = frozenset(
    (
        "STORE_ENTRY_QUOTA",
        "STORE_BYTE_QUOTA",
        "UNIT_OVER_WIRE_BUDGET",
        "UNIT_OVER_PAGE_BUDGET",
        "NO_ENVELOPE_HEADROOM",
        "ANSWER_OVER_CAP",
        "ANSWER_OVER_ENVELOPE",
        "INTERNAL_ERROR",
        "REQUEST_OVER_TOKEN_CAP",
    )
)


def fallback_allowed(code: str, detail: str | None = None) -> bool:
    """Classify ownership, never treating an overloaded limit as permission."""
    if detail in {
        "MODEL_SUBSTITUTED",
        "BLOB_CONTENT_MISMATCH",
        "SNAPSHOT_MISMATCH",
        "UNKNOWN_HANDLE",
    }:
        return False
    if code == "LIMIT_EXCEEDED":
        return detail in CAPACITY_FAILURE_DETAILS
    if code == "HOST_UNSAFE":
        return detail == "INTERNAL_ERROR"
    return code in {
        "MODEL_ERROR",
        "TIMEOUT",
        "INVALID_MODEL_OUTPUT",
        "CITATION_INVALID",
        "SPILL_FAILED",
        "STORE_FAILED",
    }
