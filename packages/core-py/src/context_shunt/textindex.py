"""Physical-line counting and indexing.

The contract (docs/architecture.md, gate rule 1) is: physical lines are LF-separated;
a trailing LF does not add an empty line; an empty file is 0 lines; CRLF is one line.
``wc -l`` does not implement this — it undercounts a file with no trailing newline — so
the counter is written to the contract and pinned by ``contracts/v1/conformance/line-count-cases.json``.

Counting is streaming: it never materializes the file, and ``count_lines_bounded`` stops
as soon as the answer can no longer change the gate decision.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

_LF = 0x0A
_CHUNK = 64 * 1024


def count_lines(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


@dataclass(frozen=True)
class BoundedCount:
    """``exact`` is False when scanning stopped at a bound, so ``lines`` is a lower bound."""

    lines: int
    bytes_seen: int
    exact: bool


def count_lines_bounded(
    stream: Iterator[bytes], *, max_lines: int, max_bytes: int
) -> BoundedCount:
    """Count lines while streaming, stopping at ``max_lines`` or ``max_bytes``.

    An inexact result is treated as "unknown scale" by the gate, which blocks.
    """
    lines = 0
    total = 0
    trailing_lf = True
    for block in stream:
        if not block:
            continue
        take = block
        if total + len(take) > max_bytes:
            take = take[: max_bytes - total]
        total += len(take)
        lines += take.count(b"\n")
        trailing_lf = take.endswith(b"\n")
        if lines >= max_lines or total >= max_bytes:
            # Clamp: past the bound the exact count is unknown and irrelevant, and an
            # unclamped number would imply a precision the scan did not pay for.
            return BoundedCount(lines=min(lines, max_lines), bytes_seen=total, exact=False)
    if total == 0:
        return BoundedCount(lines=0, bytes_seen=0, exact=True)
    return BoundedCount(lines=lines + (0 if trailing_lf else 1), bytes_seen=total, exact=True)


def iter_file(path, chunk: int = _CHUNK) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                return
            yield block


class LineIndex:
    """1-based inclusive physical-line index over immutable snapshot bytes.

    Newlines are not normalized: a CRLF line keeps its CR, because normalizing would
    drift every downstream byte offset and therefore every citation.
    """

    __slots__ = ("_data", "_starts", "_ends")

    def __init__(self, data: bytes):
        self._data = data
        starts: list[int] = []
        ends: list[int] = []
        if data:
            pos = 0
            n = len(data)
            while pos < n:
                nl = data.find(_LF, pos)
                if nl == -1:
                    starts.append(pos)
                    ends.append(n)
                    break
                starts.append(pos)
                ends.append(nl)
                pos = nl + 1
        self._starts = tuple(starts)
        self._ends = tuple(ends)

    @property
    def line_count(self) -> int:
        return len(self._starts)

    def line_bytes(self, ordinal: int) -> bytes:
        """Line content without its terminating LF. ``ordinal`` is 1-based."""
        if ordinal < 1 or ordinal > self.line_count:
            raise IndexError("line out of range")
        i = ordinal - 1
        return self._data[self._starts[i] : self._ends[i]]

    def line_text(self, ordinal: int) -> str:
        return self.line_bytes(ordinal).decode("utf-8", errors="strict")

    def range_bytes(self, start: int, end: int) -> bytes:
        if start < 1 or end < start or end > self.line_count:
            raise IndexError("line range out of range")
        return self._data[self._starts[start - 1] : self._ends[end - 1]]

    def range_text(self, start: int, end: int) -> str:
        return self.range_bytes(start, end).decode("utf-8", errors="strict")
