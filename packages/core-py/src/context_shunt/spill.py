"""Optional oversized post-tool mode: pure spill and pointer.

This engine never summarizes. It has no model bridge at all, which is the point: the old
heuristic compactor is replaced, not chained. An eligible oversized result is validated,
published through the hybrid store as an internal handle, and only then replaced with a
pointer envelope. Reading it later goes through the question-driven reader or the
deterministic inspect path like any other handle.

Failure never falls back to the raw payload. Quota exhaustion, a full disk, a permission
error, an unserializable value and a content mismatch all produce the same bounded
``SPILL_FAILED`` envelope with no handle.

Capture scope
-------------
Only an *explicitly eligible oversized candidate* is captured: a result that serializes
above ``max_tool_result_bytes``. A short result passes through untouched and is never
stored, so this path cannot become a shadow log of every tool call.

Host support
------------
Enabling this additionally requires proof that the host captures the complete result
before truncation and accepts a safe replacement before persistence and context insertion.
Neither supported host provides both today, so the mode is reported unsupported and stays
off; see ``capability.py`` and ``docs/capability-matrix.md``. The store and engine below
remain present and tested behind that capability gate rather than being deleted, so the
day a host does provide the ordering there is a tested implementation to enable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import envelope as E
from .binaryguard import assert_supported_blocks
from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .provenance import ProvenanceLabel, ResultKind, deterministic
from .registry import SourceRegistry
from .snapshot import json_depth_and_nodes, snapshot_bytes


@dataclass(frozen=True)
class SpillOutcome:
    action: str  # passthrough | spill | blocked | error
    envelope: dict[str, Any] | None = None
    code: str | None = None
    bytes_measured: int = 0
    source_id: str | None = None


class SpillEngine:
    """Sizing and spilling for one complete tool/MCP result."""

    def __init__(
        self,
        registry: SourceRegistry,
        *,
        limits: Limits = DEFAULT_LIMITS,
        enabled: bool = False,
    ):
        self._registry = registry
        self._limits = limits
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def evaluate(
        self,
        session_id: str,
        request_id: str,
        result: Any,
        *,
        internal_source_id: str | None = None,
    ) -> SpillOutcome:
        """Decide what a host should do with one complete tool result."""
        if not self._enabled:
            return SpillOutcome(action="passthrough")
        if internal_source_id and self._registry.is_internal(session_id, internal_source_id):
            # Store-verified internal envelope: never spill our own pointer again.
            return SpillOutcome(action="passthrough")
        try:
            serialized = self._serialize(result)
        except ShuntError as exc:
            return SpillOutcome(
                action="blocked" if exc.code == "BINARY_UNSUPPORTED" else "error",
                envelope=E.error_envelope(request_id, exc),
                code=exc.code,
            )
        except Exception:
            safe = ShuntError("SPILL_FAILED", "SERIALIZE_FAILED", retryable=False)
            return SpillOutcome(
                action="error",
                envelope=E.error_envelope(request_id, safe),
                code=safe.code,
            )

        size = len(serialized)
        if size > self._limits.max_source_bytes:
            exc = ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", retryable=False)
            return SpillOutcome(
                action="error",
                envelope=E.error_envelope(request_id, exc),
                code=exc.code,
                bytes_measured=size,
            )
        if size <= self._limits.max_tool_result_bytes:
            # Not an eligible oversized candidate. Nothing is captured.
            return SpillOutcome(action="passthrough", bytes_measured=size)

        try:
            # Validate before persistence so a binary/secret/invalid payload cannot leave
            # an orphaned artifact after the operation is rejected.
            snapshot = snapshot_bytes(serialized, media_type_hint="text/plain", limits=self._limits)
            entry = self._registry.register(
                session_id, snapshot, internal=True, kind="spilled_tool"
            )
        except ShuntError as exc:
            code = (
                exc.code
                if exc.code in ("SPILL_FAILED", "UNSAFE_SOURCE", "STORE_FAILED", "LIMIT_EXCEEDED")
                else "SPILL_FAILED"
            )
            safe = ShuntError(code, exc.detail, retryable=False)
            return SpillOutcome(
                action="error",
                envelope=E.error_envelope(request_id, safe),
                code=code,
                bytes_measured=size,
            )
        except Exception:
            safe = ShuntError("SPILL_FAILED", "WRITE_FAILED", retryable=False)
            return SpillOutcome(
                action="error",
                envelope=E.error_envelope(request_id, safe),
                code=safe.code,
                bytes_measured=size,
            )

        pointer = {
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "bytes": size,
            "expires_at": E.iso_expiry(entry.expires_at_epoch),
            "internal": True,
        }
        env = E.build(
            request_id=request_id,
            status="ok",
            code="SPILLED",
            coverage=E.Coverage(upstream_truncated=None),
            sources=[
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "media_type": entry.snapshot.media_type,
                    "bytes": size,
                    "expires_at": pointer["expires_at"],
                }
            ],
            retryable=False,
            pointer=pointer,
            result_kind=ResultKind.POINTER,
            provenance=deterministic(ProvenanceLabel.POINTER_ONLY),
            guidance=(
                "The tool result was too large for this conversation and was moved out of it. "
                "Ask the context-shunt reader a question about this pointer for a cited answer, "
                "or use context_shunt_inspect for exact lines."
            ),
        )
        return SpillOutcome(
            action="spill",
            envelope=env,
            code="SPILLED",
            bytes_measured=size,
            source_id=entry.source_id,
        )

    def _serialize(self, result: Any) -> bytes:
        """Deterministic serialization of string, object, array or content-block results."""
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        if isinstance(result, str):
            return result.encode("utf-8")
        blocks = _content_blocks(result)
        if blocks is not None:
            assert_supported_blocks(blocks)
        json_depth_and_nodes(result, self._limits)
        try:
            text = json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_reject
            )
        except (TypeError, ValueError):
            raise ShuntError("LIMIT_EXCEEDED", "UNSERIALIZABLE_RESULT", retryable=False) from None
        return text.encode("utf-8")


#: Historical name kept so existing adapters and gates keep importing successfully.
SumaSpillEngine = SpillEngine


def _content_blocks(result: Any) -> list[dict] | None:
    if isinstance(result, dict):
        blocks = result.get("content")
        if isinstance(blocks, list):
            return blocks
    if isinstance(result, list) and any(isinstance(b, dict) and "type" in b for b in result):
        return result
    return None


def _reject(_value: Any) -> Any:
    raise TypeError("unserializable value")


__all__ = ["SpillEngine", "SpillOutcome", "SumaSpillEngine"]
