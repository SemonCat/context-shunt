"""Physical-line counting and indexing.

The contract (docs/architecture.md, gate rule 1) is: physical lines are LF-separated;
a trailing LF does not add an empty line; an empty file is 0 lines; CRLF is one line.
``wc -l`` does not implement this — it undercounts a file with no trailing newline — so
the counter is written to the contract and pinned by ``contracts/v1/conformance/line-count-cases.json``.

Counting is streaming: it never materializes the file, and ``count_lines_bounded`` stops
as soon as the answer can no longer change the gate decision.
"""

from __future__ import annotations

from array import array
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


def count_lines_bounded(stream: Iterator[bytes], *, max_lines: int, max_bytes: int) -> BoundedCount:
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

    __slots__ = ("_checkpoints", "_data", "_line_count")

    _STRIDE = 64

    def __init__(self, data: bytes):
        self._data = data
        checkpoints = array("I")
        if not data:
            self._checkpoints = checkpoints
            self._line_count = 0
            return
        checkpoints.append(0)
        lines = 0
        position = data.find(_LF)
        while position != -1:
            lines += 1
            next_start = position + 1
            if next_start < len(data) and lines % self._STRIDE == 0:
                checkpoints.append(next_start)
            position = data.find(_LF, next_start)
        self._checkpoints = checkpoints
        self._line_count = lines + (0 if data.endswith(b"\n") else 1)

    @property
    def line_count(self) -> int:
        return self._line_count

    def line_bytes(self, ordinal: int) -> bytes:
        """Line content without its terminating LF. ``ordinal`` is 1-based."""
        if ordinal < 1 or ordinal > self.line_count:
            raise IndexError("line out of range")
        start = self._line_start(ordinal)
        end = self._data.find(_LF, start)
        return self._data[start : len(self._data) if end == -1 else end]

    def line_text(self, ordinal: int) -> str:
        return self.line_bytes(ordinal).decode("utf-8", errors="strict")

    def range_bytes(self, start: int, end: int) -> bytes:
        if start < 1 or end < start or end > self.line_count:
            raise IndexError("line range out of range")
        range_start = self._line_start(start)
        range_end = range_start
        for ordinal in range(start, end + 1):
            newline = self._data.find(_LF, range_end)
            if newline == -1:
                range_end = len(self._data)
                break
            range_end = newline if ordinal == end else newline + 1
        return self._data[range_start:range_end]

    def range_text(self, start: int, end: int) -> str:
        return self.range_bytes(start, end).decode("utf-8", errors="strict")

    def _line_start(self, ordinal: int) -> int:
        block = (ordinal - 1) // self._STRIDE
        base_ordinal = block * self._STRIDE + 1
        offset = self._checkpoints[block]
        for _current in range(base_ordinal, ordinal):
            newline = self._data.find(_LF, offset)
            if newline == -1:
                raise IndexError("line out of range")
            offset = newline + 1
        return offset
