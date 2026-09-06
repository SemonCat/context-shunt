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

That content cap is not the same measurement the output guard makes. The guard weighs the
*serialized* envelope, and JSON escaping makes those two numbers diverge: a ``"`` costs one
byte in the snapshot and two on the wire, and a C0 control character costs one and six. So
a page is bounded twice - once on content, so the disclosure ceiling stays a statement
about source bytes, and once on wire cost, so a full page of quote-dense source cannot
produce an envelope the guard would then refuse. Whichever binds first ends the page; the
caller pages on. See :func:`escaped_json_cost` for why that second measurement is computed
here rather than delegated to the platform serializer.

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

#: Characters JSON gives a two-byte short escape. Everything else below 0x20 costs six
#: (``\u00XX``); everything at or above it costs its UTF-8 length.
_SHORT_ESCAPES = frozenset({0x22, 0x5C, 0x08, 0x0C, 0x0A, 0x0D, 0x09})


def escaped_json_cost(text: str) -> int:
    """Serialized bytes ``text`` occupies inside a JSON string, excluding the quote marks.

    Hand-rolled on purpose. ``json.dumps(ensure_ascii=False)`` and ``JSON.stringify`` agree
    on ordinary text but not on every input, and a page boundary that differs between the
    two cores would be a parity failure against the shared fixtures. This table is a closed
    set defined over code points, so both cores return the same number by construction.
    """
    total = 0
    for char in text:
        point = ord(char)
        if point in _SHORT_ESCAPES:
            total += 2
        elif point < 0x20:
            total += 6
        elif point < 0x80:
            total += 1
        elif point < 0x800:
            total += 2
        elif point < 0x10000:
            total += 3
        else:
            total += 4
    return total


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

    @staticmethod
    def wire_overhead(kind: str, start: int, end: int) -> int:
        """Serialized cost of the segment object around its text, plus its list comma.

        Measured rather than hardcoded so that adding a field to :meth:`to_dict` cannot
        silently under-budget the page. The structure is pure ASCII, so the platform
        serializers agree on it; only the caller-derived ``text`` needs
        :func:`escaped_json_cost`.
        """
        skeleton = {"kind": kind, "start": start, "end": end, "text": ""}
        return len(json.dumps(skeleton, ensure_ascii=False, separators=(",", ":"))) + 1

    def wire_bytes(self) -> int:
        return self.wire_overhead(self.kind, self.start, self.end) + escaped_json_cost(self.text)


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
    #: Which budget ended the page. Only meaningful alongside ``stalled``, where it is the
    #: difference between "this unit is larger than any page" and "this unit is larger than
    #: what is left of the disclosure allowance" - two refusals with different remedies.
    stall_reason: str = "content"


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
        max_wire_bytes: int,
        state: dict[str, Any] | None = None,
    ) -> Extraction:
        budget = min(
            max_result_bytes,
            self._limits.inspect_max_result_bytes,
            self._limits.max_extraction_bytes,
        )
        if budget <= 0:
            raise ShuntError("INVALID_REQUEST", "ZERO_RESULT_BUDGET", retryable=False)
        if max_wire_bytes <= 0:
            raise ShuntError("LIMIT_EXCEEDED", "NO_ENVELOPE_HEADROOM", retryable=False)
        scan_budget = min(max_scan_lines, self._limits.inspect_max_scan_lines)
        kind = selector.get("kind")
        if kind == "lines":
            return self._lines(index, selector, budget, max_wire_bytes, scan_budget, state or {})
        if kind == "bytes":
            return self._bytes(data, selector, budget, max_wire_bytes, state or {})
        if kind == "search":
            return self._search(index, selector, budget, max_wire_bytes, scan_budget, state or {})
        raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR", retryable=False)

    # -- lines -------------------------------------------------------------

    def _lines(
        self,
        index: LineIndex,
        selector: dict[str, Any],
        budget: int,
        wire_budget: int,
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
        # The whole page is one segment, so its structural cost is paid once. It is
        # measured against the widest end ordinal the page could reach, never a narrower
        # one that would let the last line overshoot.
        wire_used = Segment.wire_overhead("lines", start, end)
        stopped_on_wire = False
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
            wire_size = escaped_json_cost(chunk)
            if wire_used + wire_size > wire_budget:
                stopped_on_wire = True
                break
            emitted.append(line)
            used += size
            wire_used += wire_size
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
            if out.stalled and stopped_on_wire:
                out.stall_reason = "wire"
        return out

    # -- bytes -------------------------------------------------------------

    def _bytes(
        self,
        data: bytes,
        selector: dict[str, Any],
        budget: int,
        wire_budget: int,
        state: dict[str, Any],
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
            if end <= begin:
                # Nothing remains to emit, so the empty page is a complete answer.
                return out
            # Nothing fits without splitting a character; advancing is the only honest move.
            out.complete = False
            advanced = min(end, begin + 1)
            out.next_cursor_state = {"offset": advanced}
            out.stalled = advanced <= start
            return out
        text = data[begin:finish].decode("utf-8", errors="strict")
        # A byte range may be cut at any character boundary, so the wire budget shortens the
        # page rather than refusing it: walk the decoded window and stop at the last
        # character whose escaped cost still fits.
        overhead = Segment.wire_overhead("bytes", begin, finish)
        wire_used = overhead
        kept_bytes = 0
        kept_chars = 0
        for char in text:
            cost = escaped_json_cost(char)
            if wire_used + cost > wire_budget:
                break
            wire_used += cost
            kept_bytes += len(char.encode("utf-8"))
            kept_chars += 1
        if kept_chars < len(text):
            finish = begin + kept_bytes
            text = text[:kept_chars]
        if finish <= begin:
            # Not even one character fits the envelope headroom. Advancing would emit a
            # cursor the caller could not make progress with, so this is refused instead.
            out.complete = False
            out.next_cursor_state = {"offset": begin}
            out.stalled = True
            out.stall_reason = "wire"
            return out
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
        wire_budget: int,
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
        wire_used = 0
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
                wire_size = Segment.wire_overhead("lines", low, high) + escaped_json_cost(text)
                over_wire = wire_used + wire_size > wire_budget
                if (
                    used + size > budget
                    or over_wire
                    or len(out.segments) >= self._limits.inspect_max_segments
                ):
                    out.complete = False
                    out.next_cursor_state = {
                        "line": ordinal,
                        "matches": already + (out.matches_found or 0),
                    }
                    out.result_bytes = used
                    # A first match too large for the page leaves the cursor where it was.
                    # Reporting which budget bound it keeps "this match cannot ever fit"
                    # distinct from "the allowance ran out".
                    out.stalled = not out.segments and ordinal == max(1, int(state.get("line", 1)))
                    if out.stalled and over_wire:
                        out.stall_reason = "wire"
                    return out
                out.segments.append(Segment(kind="lines", start=low, end=high, text=text))
                used += size
                wire_used += wire_size
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
