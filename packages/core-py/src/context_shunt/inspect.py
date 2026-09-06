"""Deterministic extraction: exact snapshot bytes, zero model calls.

``context_shunt_inspect`` is the escape hatch for "I need to see the actual text", and it
is deliberately not a retrieval tool. Four properties make it safe to hand to an agent:

**It is exact, and it says so.** Every byte returned is copied from the immutable snapshot
the caller named. The envelope is labelled ``deterministic_extraction`` with
``provenance.derived = false``, so it can never be read as a summary. No provider is
consulted; the engine has no provider reference at all, which is what makes "zero LLM
calls" a structural fact rather than a promise.

**It is bounded per result.** One page is capped at ``inspect.max_result_bytes`` (16 KiB),
measured on the UTF-8 bytes of the emitted segments.

**It is bounded cumulatively.** Paging is the obvious way to defeat a per-result cap, so
every page is charged against a per-source and a per-session disclosure ceiling before a
byte is returned. Once the ceiling is reached, further pages return no content and say
``DISCLOSURE_EXHAUSTED``. There is no configuration in which repeated small reads can
reassemble a whole payload into the main context.

**Continuation is authenticated, not arithmetic.** A cursor is an opaque HMAC-tagged token
bound to the handle, the snapshot hash and the canonical selector. It cannot be edited to
jump the scan budget, cannot be pointed at a different snapshot, and cannot be replayed
into another store, because the key lives in that store's metadata.

Scanning is linear: only a literal needle is accepted, never a regular expression, so no
caller-supplied pattern can be made to backtrack.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .textindex import LineIndex

CURSOR_PREFIX = "csr_"
_CURSOR_VERSION = 1
_MAC_BYTES = 16


@dataclass(frozen=True)
class Segment:
    kind: str  # "lines" | "bytes"
    start: int
    end: int
    text: str

    def byte_len(self) -> int:
        return len(self.text.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "start": self.start, "end": self.end, "text": self.text}


@dataclass
class Extraction:
    mode: str
    segments: list[Segment] = field(default_factory=list)
    result_bytes: int = 0
    complete: bool = True
    next_cursor_state: dict[str, Any] | None = None
    lines_scanned: int = 0
    scan_budget_exhausted: bool = False
    matches_found: int | None = None
    #: True when the page emitted nothing *and* the scan position did not move, so a
    #: caller following ``next_cursor`` would loop forever. The extractor knows the start
    #: position, so it is the only place that can tell this apart from an honest empty
    #: page (a scan-budget stop emits nothing but does advance).
    stalled: bool = False


def canonical_selector(selector: dict[str, Any]) -> str:
    """Stable text a cursor is bound to. Any selector change invalidates the cursor."""
    return json.dumps(selector, sort_keys=True, separators=(",", ":"))


def _binding(handle_id: str, snapshot_id: str, selector: dict[str, Any]) -> str:
    material = "\x1f".join((handle_id, snapshot_id, canonical_selector(selector)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def encode_cursor(
    key: bytes, handle_id: str, snapshot_id: str, selector: dict[str, Any], state: dict[str, Any]
) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "b": _binding(handle_id, snapshot_id, selector),
            "s": state,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    mac = hmac.new(key, payload, hashlib.sha256).digest()[:_MAC_BYTES]
    return CURSOR_PREFIX + base64.urlsafe_b64encode(payload + mac).decode("ascii").rstrip("=")


def decode_cursor(
    key: bytes, token: str, handle_id: str, snapshot_id: str, selector: dict[str, Any]
) -> dict[str, Any]:
    """Authenticate a cursor and confirm it belongs to exactly this request.

    Every rejection is the same bounded error, so a caller probing with edited cursors
    learns nothing about which check failed.
    """
    if not isinstance(token, str) or not token.startswith(CURSOR_PREFIX):
        raise ShuntError("INVALID_REQUEST", "BAD_CURSOR", retryable=False)
    body = token[len(CURSOR_PREFIX) :]
    padding = "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body + padding)
    except (ValueError, TypeError):
        raise ShuntError("INVALID_REQUEST", "BAD_CURSOR", retryable=False) from None
    if len(raw) <= _MAC_BYTES:
        raise ShuntError("INVALID_REQUEST", "BAD_CURSOR", retryable=False)
    payload, mac = raw[:-_MAC_BYTES], raw[-_MAC_BYTES:]
    expected = hmac.new(key, payload, hashlib.sha256).digest()[:_MAC_BYTES]
    if not hmac.compare_digest(mac, expected):
        raise ShuntError("INVALID_REQUEST", "BAD_CURSOR", retryable=False)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ShuntError("INVALID_REQUEST", "BAD_CURSOR", retryable=False) from None
    if (
        not isinstance(decoded, dict)
        or decoded.get("v") != _CURSOR_VERSION
        or decoded.get("b") != _binding(handle_id, snapshot_id, selector)
        or not isinstance(decoded.get("s"), dict)
    ):
        raise ShuntError("INVALID_REQUEST", "BAD_CURSOR", retryable=False)
    return dict(decoded["s"])


class Inspector:
    """Stateless extractor over one immutable snapshot. Holds no provider reference."""

    def __init__(self, limits: Limits = DEFAULT_LIMITS):
        self._limits = limits

    def extract(
        self,
        data: bytes,
        index: LineIndex,
        selector: dict[str, Any],
        *,
        max_result_bytes: int,
        max_scan_lines: int,
        state: dict[str, Any] | None = None,
    ) -> Extraction:
        budget = min(
            max_result_bytes,
            self._limits.inspect_max_result_bytes,
            self._limits.max_extraction_bytes,
        )
        if budget <= 0:
            raise ShuntError("INVALID_REQUEST", "ZERO_RESULT_BUDGET", retryable=False)
        scan_budget = min(max_scan_lines, self._limits.inspect_max_scan_lines)
        kind = selector.get("kind")
        if kind == "lines":
            return self._lines(index, selector, budget, scan_budget, state or {})
        if kind == "bytes":
            return self._bytes(data, selector, budget, state or {})
        if kind == "search":
            return self._search(index, selector, budget, scan_budget, state or {})
        raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR", retryable=False)

    # -- lines -------------------------------------------------------------

    def _lines(
        self,
        index: LineIndex,
        selector: dict[str, Any],
        budget: int,
        scan_budget: int,
        state: dict[str, Any],
    ) -> Extraction:
        requested_start = int(selector["start"])
        requested_end = int(selector["end"])
        if requested_end < requested_start:
            raise ShuntError("INVALID_REQUEST", "BAD_LINE_RANGE", retryable=False)
        start = max(requested_start, int(state.get("line", requested_start)))
        end = min(requested_end, index.line_count)
        out = Extraction(mode="lines")
        if start > end:
            # The range is entirely past the end of the snapshot: an empty exact answer.
            return out

        emitted: list[str] = []
        used = 0
        ordinal = start
        page_lines = min(self._limits.inspect_max_lines_per_page, scan_budget)
        while ordinal <= end and out.lines_scanned < page_lines:
            try:
                line = index.line_text(ordinal)
            except (IndexError, UnicodeDecodeError):
                break
            chunk = line if not emitted else "\n" + line
            size = len(chunk.encode("utf-8"))
            if used + size > budget:
                break
            emitted.append(line)
            used += size
            out.lines_scanned += 1
            ordinal += 1

        if emitted:
            out.segments.append(
                Segment(kind="lines", start=start, end=ordinal - 1, text="\n".join(emitted))
            )
        out.result_bytes = used
        out.scan_budget_exhausted = out.lines_scanned >= page_lines and ordinal <= end
        if ordinal <= end:
            out.complete = False
            out.next_cursor_state = {"line": ordinal}
            out.stalled = ordinal == start and not emitted
        return out

    # -- bytes -------------------------------------------------------------

    def _bytes(
        self, data: bytes, selector: dict[str, Any], budget: int, state: dict[str, Any]
    ) -> Extraction:
        requested_start = int(selector["start"])
        requested_end = int(selector["end"])
        if requested_end < requested_start:
            raise ShuntError("INVALID_REQUEST", "BAD_BYTE_RANGE", retryable=False)
        start = max(requested_start, int(state.get("offset", requested_start)))
        end = min(requested_end, len(data))
        out = Extraction(mode="bytes")
        if start >= end:
            return out

        take = min(end - start, budget, self._limits.inspect_max_bytes_per_page)
        # A byte range can land inside a multi-byte character. Both edges are pulled to a
        # UTF-8 boundary so the emitted text is exactly a substring of the snapshot and
        # never a mojibake fragment; the cursor resumes from the boundary actually used.
        begin = _forward_to_boundary(data, start)
        finish = _back_to_boundary(data, begin, begin + take)
        if finish <= begin:
            out.complete = end > begin
            if not out.complete:
                return out
            # Nothing fits without splitting a character; advancing is the only honest move.
            advanced = min(end, begin + 1)
            out.next_cursor_state = {"offset": advanced}
            out.stalled = advanced <= start
            return out
        text = data[begin:finish].decode("utf-8", errors="strict")
        out.segments.append(Segment(kind="bytes", start=begin, end=finish, text=text))
        out.result_bytes = finish - begin
        if finish < end:
            out.complete = False
            out.next_cursor_state = {"offset": finish}
        return out

    # -- search ------------------------------------------------------------

    def _search(
        self,
        index: LineIndex,
        selector: dict[str, Any],
        budget: int,
        scan_budget: int,
        state: dict[str, Any],
    ) -> Extraction:
        needle = selector["needle"]
        if len(needle.encode("utf-8")) > self._limits.inspect_max_needle_bytes:
            raise ShuntError("INVALID_REQUEST", "NEEDLE_OVER_CAP", retryable=False)
        max_matches = min(int(selector["max_matches"]), self._limits.inspect_max_search_matches)
        context = int(selector.get("context_lines", 0))
        out = Extraction(mode="search", matches_found=0)

        ordinal = max(1, int(state.get("line", 1)))
        already = max(0, int(state.get("matches", 0)))
        used = 0
        remaining_matches = max_matches - already
        if remaining_matches <= 0:
            return out

        while ordinal <= index.line_count and out.lines_scanned < scan_budget:
            try:
                line = index.line_text(ordinal)
            except (IndexError, UnicodeDecodeError):
                ordinal += 1
                out.lines_scanned += 1
                continue
            out.lines_scanned += 1
            if needle in line:
                low = max(1, ordinal - context)
                high = min(index.line_count, ordinal + context)
                try:
                    text = index.range_text(low, high)
                except (IndexError, UnicodeDecodeError):
                    ordinal += 1
                    continue
                size = len(text.encode("utf-8"))
                if used + size > budget or len(out.segments) >= self._limits.inspect_max_segments:
                    out.complete = False
                    out.next_cursor_state = {
                        "line": ordinal,
                        "matches": already + (out.matches_found or 0),
                    }
                    out.result_bytes = used
                    return out
                out.segments.append(Segment(kind="lines", start=low, end=high, text=text))
                used += size
                out.matches_found = (out.matches_found or 0) + 1
                if (out.matches_found or 0) >= remaining_matches:
                    ordinal += 1
                    break
            ordinal += 1

        out.result_bytes = used
        more_lines = ordinal <= index.line_count
        matched_all = (out.matches_found or 0) >= remaining_matches
        if more_lines and out.lines_scanned >= scan_budget and not matched_all:
            out.scan_budget_exhausted = True
        if more_lines and not matched_all:
            out.complete = False
            out.next_cursor_state = {
                "line": ordinal,
                "matches": already + (out.matches_found or 0),
            }
        return out


def _forward_to_boundary(data: bytes, offset: int) -> int:
    """Advance to the next UTF-8 character start at or after ``offset``."""
    limit = len(data)
    while offset < limit and (data[offset] & 0xC0) == 0x80:
        offset += 1
    return offset


def _back_to_boundary(data: bytes, begin: int, offset: int) -> int:
    """Pull back to the last UTF-8 character end at or before ``offset``."""
    offset = min(offset, len(data))
    while offset > begin and (data[offset - 1] & 0xC0) == 0x80:
        offset -= 1
    if offset > begin:
        lead = data[offset - 1]
        width = _sequence_width(lead)
        if width > 1:
            # ``offset`` sits just after a lead byte whose continuation bytes were cut.
            offset -= 1
    return offset


def _sequence_width(lead: int) -> int:
    if lead < 0x80:
        return 1
    if lead >= 0xF0:
        return 4
    if lead >= 0xE0:
        return 3
    if lead >= 0xC0:
        return 2
    return 1


__all__ = [
    "CURSOR_PREFIX",
    "Extraction",
    "Inspector",
    "Segment",
    "canonical_selector",
    "decode_cursor",
    "encode_cursor",
]
