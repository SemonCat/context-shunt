"""Bounded filesystem probe used by the gate.

Scanning stops at 351 lines or the byte cap, whichever comes first, so probing a
multi-gigabyte file costs the same as probing a small one. A probe that stopped early is
``exact=False``, which the gate treats as unknown scale and blocks. Nothing is executed
and the file is never buffered.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from typing import BinaryIO

from .gate import ProbeResult, ProbeSelection
from .limits import DEFAULT_LIMITS, Limits
from .textindex import count_lines_bounded

_CHUNK = 16 * 1024


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "device"
    return "other"


class FileProber:
    def __init__(self, limits: Limits = DEFAULT_LIMITS):
        self._limits = limits

    def __call__(self, path: str, selection: ProbeSelection | None = None) -> ProbeResult:
        selection = selection or ProbeSelection()
        try:
            st = os.lstat(path)
        except OSError:
            return ProbeResult(exists=False)
        if stat.S_ISLNK(st.st_mode):
            # A symlink is not a regular file for gate purposes; the path policy decides
            # whether the target may become a source at all.
            return ProbeResult(exists=True, kind="other")
        kind = _kind(st.st_mode)
        if kind != "file":
            return ProbeResult(exists=True, kind=kind)

        # Bind classification and sizing to one nonblocking, no-follow descriptor. This
        # closes the lstat/open swap window and prevents a swapped FIFO from hanging the
        # synchronous pre-tool hook.
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = -1
        try:
            fd = os.open(path, flags)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not _same_identity(before, st):
                return ProbeResult(exists=True, kind="other")
            with os.fdopen(fd, "rb", buffering=0) as handle:
                fd = -1
                if selection.mode == "metadata":
                    result = ProbeResult(exists=True, kind="file")
                elif selection.mode == "lines":
                    result = _selected_lines(
                        handle, selection.start_line, selection.limit, self._limits
                    )
                elif selection.mode == "tail":
                    result = _selected_tail(handle, before.st_size, selection.limit, self._limits)
                elif selection.mode == "search":
                    result = _search_upper_bound(handle, path, selection.max_matches, self._limits)
                else:
                    counted = count_lines_bounded(
                        _iter_handle(handle),
                        max_lines=self._limits.probe_max_lines_scanned,
                        # A full read is already blocked at this output byte cap.
                        max_bytes=self._limits.max_targeted_read_bytes + 1,
                    )
                    result = ProbeResult(
                        exists=True,
                        kind="file",
                        lines=counted.lines,
                        bytes=before.st_size if counted.exact else counted.bytes_seen,
                        exact=counted.exact,
                    )
                after = os.fstat(handle.fileno())
                return (
                    result
                    if _same_identity(before, after)
                    else ProbeResult(exists=True, kind="other")
                )
        except OSError:
            return ProbeResult(exists=True, kind="other")
        finally:
            if fd >= 0:
                os.close(fd)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _iter_handle(handle: BinaryIO) -> Iterator[bytes]:
    while block := handle.read(_CHUNK):
        yield block


def _selected_lines(handle: BinaryIO, start_line: int, limit: int, limits: Limits) -> ProbeResult:
    end_line = start_line + limit
    line = 1
    lines = 0
    selected_bytes = 0
    scanned = 0
    current_selected = False
    while block := handle.read(_CHUNK):
        for byte in block:
            scanned += 1
            if scanned > limits.max_source_bytes:
                return ProbeResult(exists=True, lines=lines, bytes=selected_bytes, exact=False)
            current_selected = start_line <= line < end_line
            if current_selected:
                selected_bytes += 1
                if selected_bytes > limits.max_targeted_read_bytes:
                    return ProbeResult(exists=True, lines=lines, bytes=selected_bytes, exact=True)
            if byte == 0x0A:
                if current_selected:
                    lines += 1
                line += 1
                if line >= end_line:
                    return ProbeResult(exists=True, lines=lines, bytes=selected_bytes, exact=True)
    if current_selected and selected_bytes:
        lines += 1
    return ProbeResult(exists=True, lines=lines, bytes=selected_bytes, exact=True)


def _selected_tail(handle: BinaryIO, file_bytes: int, limit: int, limits: Limits) -> ProbeResult:
    if file_bytes == 0:
        return ProbeResult(exists=True)
    trailing_newline = _read_exact_at(handle, file_bytes - 1, 1) == b"\n"
    needed_newlines = limit + (1 if trailing_newline else 0)
    found = 0
    cursor = file_bytes
    while cursor > 0:
        start = max(0, cursor - _CHUNK)
        block = _read_exact_at(handle, start, cursor - start)
        for index in range(len(block) - 1, -1, -1):
            if block[index] == 0x0A:
                found += 1
                if found == needed_newlines:
                    selected_bytes = file_bytes - (start + index + 1)
                    return ProbeResult(exists=True, lines=limit, bytes=selected_bytes, exact=True)
        cursor = start
        if file_bytes - cursor > limits.max_targeted_read_bytes:
            return ProbeResult(
                exists=True,
                lines=limit,
                bytes=limits.max_targeted_read_bytes + 1,
                exact=True,
            )
    return ProbeResult(exists=True, lines=limit, bytes=file_bytes, exact=True)


def _read_exact_at(handle: BinaryIO, position: int, length: int) -> bytes:
    handle.seek(position)
    out = bytearray()
    while len(out) < length:
        block = handle.read(length - len(out))
        if not block:
            raise OSError("short read")
        out.extend(block)
    return bytes(out)


def _search_upper_bound(
    handle: BinaryIO, path: str, max_matches: int, limits: Limits
) -> ProbeResult:
    prefix_bytes = len(path.encode("utf-8")) + 64
    bytes_seen = 0
    line_bytes = 0
    line_count = 0
    max_line_bytes = 0
    ended_with_newline = False
    while block := handle.read(_CHUNK):
        for byte in block:
            bytes_seen += 1
            if bytes_seen > limits.max_source_bytes:
                return ProbeResult(exists=True, lines=line_count, bytes=bytes_seen, exact=False)
            line_bytes += 1
            current_upper = max_matches * (line_bytes + prefix_bytes)
            if current_upper > limits.max_targeted_read_bytes:
                return ProbeResult(
                    exists=True,
                    lines=min(max_matches, line_count + 1),
                    bytes=current_upper,
                    exact=True,
                )
            ended_with_newline = byte == 0x0A
            if ended_with_newline:
                line_count += 1
                max_line_bytes = max(max_line_bytes, line_bytes)
                line_bytes = 0
                upper = max_matches * (max_line_bytes + prefix_bytes)
                if upper > limits.max_targeted_read_bytes:
                    return ProbeResult(exists=True, lines=max_matches, bytes=upper, exact=True)
    if line_bytes or (bytes_seen and not ended_with_newline):
        line_count += 1
        max_line_bytes = max(max_line_bytes, line_bytes)
    matches = min(max_matches, line_count)
    return ProbeResult(
        exists=True,
        lines=matches,
        bytes=matches * (max_line_bytes + prefix_bytes),
        exact=True,
    )
