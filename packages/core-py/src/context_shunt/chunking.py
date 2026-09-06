"""Chunk planning.

A plan is produced before any model call so the token budget is known up front; if it
cannot be bounded the request is refused rather than started. Chunks are cut on UTF-8
boundaries, carry at most one line of overlap (which costs budget like any other line),
and every chunk records the source, snapshot and exact line/record range it came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .snapshot import Snapshot, canonical_json, record_at, record_count, resolve_pointer


@dataclass(frozen=True)
class Chunk:
    source_id: str
    snapshot_id: str
    locator: dict[str, Any]
    text: str
    bytes_len: int
    est_tokens: int


@dataclass(frozen=True)
class Plan:
    chunks: tuple[Chunk, ...]
    total_est_tokens: int
    truncated: bool  # a selected range did not fit the plan and is reported as omitted
    omitted: tuple[dict[str, Any], ...]


def per_call_overhead_tokens(limits: Limits = DEFAULT_LIMITS) -> int:
    """Tokens each call spends on the fixed instruction and the excerpt wrapper.

    The request budget covers *all* prompt input, not just chunk text, so the planner has
    to reserve this per chunk or an eight-chunk plan silently overruns the cap.
    """
    from .provider import READER_SYSTEM_PROMPT

    fixed = len(READER_SYSTEM_PROMPT.encode("utf-8")) + 256  # wrapper, locator, question
    return max(1, -(-fixed // limits.bytes_per_token_estimate))


def chunk_byte_budget(limits: Limits = DEFAULT_LIMITS) -> int:
    """The smaller of the byte cap and the token cap expressed in bytes.

    The contract says a chunk is at most 32 KiB *and* at most 8,000 estimated tokens,
    whichever is smaller - so the byte budget is the binding one at 4 bytes/token.
    """
    return min(limits.max_chunk_bytes, limits.max_chunk_tokens * limits.bytes_per_token_estimate)


def estimate_tokens(text: str, limits: Limits = DEFAULT_LIMITS) -> int:
    """Conservative estimate. Metrics derived from it are labelled ``estimated``."""
    return max(1, -(-len(text.encode("utf-8")) // limits.bytes_per_token_estimate))


def _line_chunks(
    snapshot: Snapshot, source_id: str, start: int, end: int, limits: Limits
) -> list[Chunk]:
    chunks: list[Chunk] = []
    budget = chunk_byte_budget(limits)
    cursor = start
    while cursor <= end:
        lo = cursor
        size = 0
        hi = lo - 1
        while hi < end:
            candidate = hi + 1
            line_bytes = len(snapshot.line_index.line_bytes(candidate)) + 1
            if size and size + line_bytes > budget:
                break
            size += line_bytes
            hi = candidate
            if size > budget:
                break
        if hi < lo:
            hi = lo
        text = snapshot.line_index.range_text(lo, hi)
        if len(text.encode("utf-8")) > budget:
            # One physical line longer than a chunk: cut on a UTF-8 boundary, but the
            # citation locator still points at the original line.
            raw = text.encode("utf-8")[:budget]
            while raw:
                try:
                    text = raw.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    raw = raw[:-1]
        chunks.append(
            Chunk(
                source_id=source_id,
                snapshot_id=snapshot.snapshot_id,
                locator={"kind": "lines", "start": lo, "end": hi},
                text=text,
                bytes_len=len(text.encode("utf-8")),
                est_tokens=estimate_tokens(text, limits),
            )
        )
        cursor = hi + 1
    return chunks


def _record_chunks(
    snapshot: Snapshot, source_id: str, pointer: str, start: int, end: int, limits: Limits
) -> list[Chunk]:
    node = resolve_pointer(snapshot.json_value, pointer)
    total = record_count(node)
    if start < 1 or end < start or end > total:
        raise ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE")
    chunks: list[Chunk] = []
    budget = chunk_byte_budget(limits)
    lo = start
    while lo <= end:
        parts: list[str] = []
        size = 0
        hi = lo - 1
        while hi < end:
            rendered = canonical_json(record_at(node, hi + 1))
            if parts and size + len(rendered.encode("utf-8")) > budget:
                break
            parts.append(rendered)
            size += len(rendered.encode("utf-8"))
            hi += 1
        text = "\n".join(parts)
        chunks.append(
            Chunk(
                source_id=source_id,
                snapshot_id=snapshot.snapshot_id,
                locator={"kind": "records", "pointer": pointer, "start": lo, "end": hi},
                text=text,
                bytes_len=len(text.encode("utf-8")),
                est_tokens=estimate_tokens(text, limits),
            )
        )
        lo = hi + 1
    return chunks


def plan(
    selections: list[tuple[str, Snapshot, dict[str, Any]]],
    *,
    max_chunks: int,
    limits: Limits = DEFAULT_LIMITS,
) -> Plan:
    """Build a bounded plan. Ranges that do not fit become explicit omissions."""
    budget_chunks = min(max_chunks, limits.max_chunks_per_request)
    overhead = per_call_overhead_tokens(limits)
    produced: list[Chunk] = []
    omitted: list[dict[str, Any]] = []
    truncated = False
    spent_tokens = 0

    for source_id, snapshot, selector in selections:
        kind = selector.get("kind")
        if kind == "all":
            if snapshot.json_value is not None:
                candidates = _record_chunks(
                    snapshot, source_id, "", 1, record_count(snapshot.json_value), limits
                )
            else:
                candidates = _line_chunks(snapshot, source_id, 1, snapshot.line_count, limits)
        elif kind == "lines":
            start, end = int(selector["start"]), int(selector["end"])
            if start < 1 or end < start or end > snapshot.line_count:
                end = min(end, snapshot.line_count)
                if start > end:
                    raise ShuntError("INVALID_REQUEST", "LINE_OUT_OF_RANGE")
            candidates = _line_chunks(snapshot, source_id, start, end, limits)
        elif kind == "records":
            candidates = _record_chunks(
                snapshot,
                source_id,
                selector["pointer"],
                int(selector["start"]),
                int(selector["end"]),
                limits,
            )
        else:
            raise ShuntError("INVALID_REQUEST", "UNSUPPORTED_SELECTOR")

        for chunk in candidates:
            if chunk.est_tokens > limits.max_chunk_tokens:
                raise ShuntError("LIMIT_EXCEEDED", "CHUNK_OVER_TOKEN_CAP")
            projected = spent_tokens + chunk.est_tokens + overhead
            if len(produced) >= budget_chunks or projected > limits.max_request_input_tokens:
                truncated = True
                omitted.append(
                    {"source_id": source_id, "selector": chunk.locator, "reason": "BUDGET_EXCEEDED"}
                )
                continue
            spent_tokens = projected
            produced.append(chunk)

    total = sum(c.est_tokens for c in produced) + overhead * len(produced)
    if total > limits.max_request_input_tokens:
        raise ShuntError("LIMIT_EXCEEDED", "REQUEST_OVER_TOKEN_CAP")
    return Plan(
        chunks=tuple(produced), total_est_tokens=total, truncated=truncated, omitted=tuple(omitted)
    )
