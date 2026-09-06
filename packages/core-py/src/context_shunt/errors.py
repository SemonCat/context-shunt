"""Bounded, content-free failures.

Every failure that reaches an adapter is a :class:`ShuntError` carrying a contract
``code`` and a short operator-safe message. Raw payloads, prompts, provider error
bodies, shell arguments, secrets and absolute paths never enter these objects — that
is the single invariant the ``no-raw-leak`` gate measures.
"""

from __future__ import annotations

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
    "HOST_UNSAFE": "host cannot guarantee the required interception order",
    "CANCELLED": "request cancelled",
    "LARGE_READ": "full read exceeds the configured line or byte threshold",
    "UNCLASSIFIABLE_READ": "read-like command could not be proven bounded and safe",
    "UPSTREAM_TRUNCATED": "upstream result was already truncated",
}

RETRYABLE_CODES = frozenset({"MODEL_ERROR", "TIMEOUT"})


class ShuntError(Exception):
    """A failure that is safe to surface. ``detail`` is a fixed enum-like token only."""

    __slots__ = ("code", "detail", "retryable")

    def __init__(self, code: str, detail: str | None = None, retryable: bool | None = None):
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
