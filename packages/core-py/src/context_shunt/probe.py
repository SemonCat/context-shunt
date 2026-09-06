"""Bounded filesystem probe used by the gate.

Scanning stops at 351 lines or the byte cap, whichever comes first, so probing a
multi-gigabyte file costs the same as probing a small one. A probe that stopped early is
``exact=False``, which the gate treats as unknown scale and blocks. Nothing is executed
and the file is never buffered.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .gate import ProbeResult
from .limits import DEFAULT_LIMITS, Limits
from .textindex import count_lines_bounded, iter_file


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

    def __call__(self, path: str) -> ProbeResult:
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
        cap_bytes = self._limits.max_source_bytes
        try:
            counted = count_lines_bounded(
                iter_file(Path(path)),
                max_lines=self._limits.probe_max_lines_scanned,
                max_bytes=cap_bytes,
            )
        except OSError:
            return ProbeResult(exists=True, kind="other")
        return ProbeResult(
            exists=True,
            kind="file",
            lines=counted.lines,
            bytes=st.st_size if counted.exact else counted.bytes_seen,
            exact=counted.exact,
        )
