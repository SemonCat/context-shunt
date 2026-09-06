"""The pre-read gate.

Runs *before* the underlying tool executes, so a blocked decision means the host tool
was never invoked and no oversized payload ever existed. The decision is tri-state:

``passthrough``  the call is not read-like (or the file does not exist); host policy owns it
``allow``        read-like and provably small or provably bounded
``blocked``      ``LARGE_READ`` / ``UNCLASSIFIABLE_READ`` / ``UNSAFE_SOURCE``

Sizing comes from a bounded probe that stops at 351 lines or the byte cap and carries
its own 1s deadline; an inexact probe is "unknown scale", which blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .clock import Clock, Deadline, MonotonicClock
from .errors import DeadlineExceeded
from .limits import DEFAULT_LIMITS, Limits
from .shell import Classification, ShellForm, classify_command


class Decision(str, Enum):
    PASSTHROUGH = "passthrough"
    ALLOW = "allow"
    BLOCKED = "blocked"


class GateForm(str, Enum):
    NOT_READ_LIKE = "not_read_like"
    FULL_READ = "full_read"
    BOUNDED_LINES = "bounded_lines"
    BOUNDED_SEARCH = "bounded_search"
    BOUNDED_METADATA = "bounded_metadata"
    UNCLASSIFIABLE = "unclassifiable"
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class ProbeResult:
    exists: bool
    kind: str = "file"  # file | directory | fifo | socket | device | other
    lines: int = 0
    bytes: int = 0
    exact: bool = True


class Prober(Protocol):
    def __call__(self, path: str) -> ProbeResult: ...


@dataclass(frozen=True)
class GateDecision:
    decision: Decision
    form: GateForm
    code: str | None = None
    reason: str = ""
    # Non-sensitive sizing for the blocked guidance. Never the file content.
    observed_lines: int | None = None
    observed_bytes: int | None = None

    @property
    def blocked(self) -> bool:
        return self.decision is Decision.BLOCKED


_ALLOW_NOT_READ = GateDecision(Decision.PASSTHROUGH, GateForm.NOT_READ_LIKE)


def _blocked(code: str, form: GateForm, reason: str, **kw: Any) -> GateDecision:
    return GateDecision(Decision.BLOCKED, form, code=code, reason=reason, **kw)


class PreReadGate:
    def __init__(
        self,
        prober: Prober,
        limits: Limits = DEFAULT_LIMITS,
        clock: Clock | None = None,
    ):
        self._probe = prober
        self._limits = limits
        self._clock = clock or MonotonicClock()

    # -- public ------------------------------------------------------------
    def evaluate(self, tool: str, args: dict[str, Any]) -> GateDecision:
        deadline = Deadline.start(self._clock, self._limits.gate_probe_deadline_ms)
        try:
            if tool == "read":
                return self._evaluate_read(args, deadline)
            if tool == "search":
                return self._evaluate_search(args)
            if tool == "shell":
                return self._evaluate_shell(str(args.get("command") or ""), deadline)
            return _ALLOW_NOT_READ
        except DeadlineExceeded:
            # An unfinished probe means unknown scale, which blocks rather than executes.
            return _blocked("LARGE_READ", GateForm.FULL_READ, "PROBE_TIMEOUT")

    # -- read tool ---------------------------------------------------------
    def _evaluate_read(self, args: dict[str, Any], deadline: Deadline) -> GateDecision:
        path = args.get("file_path") or args.get("path")
        if not path or not isinstance(path, str):
            return _ALLOW_NOT_READ
        limit = args.get("limit")
        offset = args.get("offset")
        for name, value in (("limit", limit), ("offset", offset)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                return _blocked("UNCLASSIFIABLE_READ", GateForm.UNCLASSIFIABLE, f"BAD_{name.upper()}")
        if limit is not None and limit < 1:
            return _blocked("UNCLASSIFIABLE_READ", GateForm.UNCLASSIFIABLE, "BAD_LIMIT")
        if offset is not None and offset < 0:
            return _blocked("UNCLASSIFIABLE_READ", GateForm.UNCLASSIFIABLE, "BAD_OFFSET")

        bounded = limit is not None and limit <= self._limits.targeted_read_max_lines
        probe = self._probe_safe(path, deadline)
        if probe is None:
            return _ALLOW_NOT_READ
        if isinstance(probe, GateDecision):
            return probe
        if bounded:
            return GateDecision(Decision.ALLOW, GateForm.BOUNDED_LINES)
        return self._size_full_read([probe])

    # -- search tool -------------------------------------------------------
    def _evaluate_search(self, args: dict[str, Any]) -> GateDecision:
        max_matches = args.get("max_matches")
        if (
            max_matches is None
            or isinstance(max_matches, bool)
            or not isinstance(max_matches, int)
            or max_matches < 1
            or max_matches > self._limits.targeted_search_max_matches
        ):
            return _blocked("UNCLASSIFIABLE_READ", GateForm.UNCLASSIFIABLE, "UNBOUNDED_SEARCH")
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return _blocked("UNCLASSIFIABLE_READ", GateForm.UNCLASSIFIABLE, "BAD_PATTERN")
        return GateDecision(Decision.ALLOW, GateForm.BOUNDED_SEARCH)

    # -- shell -------------------------------------------------------------
    def _evaluate_shell(self, command: str, deadline: Deadline) -> GateDecision:
        c: Classification = classify_command(command)
        if c.form is ShellForm.NOT_READ_LIKE:
            return _ALLOW_NOT_READ
        if c.form is ShellForm.UNCLASSIFIABLE:
            return _blocked("UNCLASSIFIABLE_READ", GateForm.UNCLASSIFIABLE, c.reason or "UNPROVABLE")

        probes: list[ProbeResult] = []
        for path in c.files:
            probe = self._probe_safe(path, deadline)
            if probe is None:
                return _ALLOW_NOT_READ
            if isinstance(probe, GateDecision):
                return probe
            probes.append(probe)

        if c.form is ShellForm.BOUNDED_METADATA:
            return GateDecision(Decision.ALLOW, GateForm.BOUNDED_METADATA)
        if c.form is ShellForm.BOUNDED_SEARCH:
            if (c.bound_matches or 0) > self._limits.targeted_search_max_matches:
                return _blocked("UNCLASSIFIABLE_READ", GateForm.UNCLASSIFIABLE, "UNBOUNDED_SEARCH")
            return GateDecision(Decision.ALLOW, GateForm.BOUNDED_SEARCH)
        if c.form is ShellForm.BOUNDED_LINES:
            bound = c.bound_lines or 0
            if bound > self._limits.targeted_read_max_lines:
                return _blocked(
                    "LARGE_READ", GateForm.BOUNDED_LINES, "BOUND_OVER_CAP", observed_lines=bound
                )
            return GateDecision(Decision.ALLOW, GateForm.BOUNDED_LINES)
        return self._size_full_read(probes)

    # -- helpers -----------------------------------------------------------
    def _probe_safe(self, path: str, deadline: Deadline) -> ProbeResult | GateDecision | None:
        """Probe one path. ``None`` means "let the host handle it" (missing file)."""
        deadline.check("PROBE")
        probe = self._probe(path)
        # Checked again after the scan: a probe that overran the budget leaves the
        # overall scale unknown, and unknown scale blocks.
        deadline.check("PROBE")
        if not probe.exists:
            return None
        if probe.kind != "file":
            return _blocked("UNSAFE_SOURCE", GateForm.UNSAFE, "NOT_REGULAR_FILE")
        return probe

    def _size_full_read(self, probes: list[ProbeResult]) -> GateDecision:
        total_lines = sum(p.lines for p in probes)
        total_bytes = sum(p.bytes for p in probes)
        inexact = any(not p.exact for p in probes)
        if inexact or total_lines > self._limits.full_read_max_lines:
            return _blocked(
                "LARGE_READ",
                GateForm.FULL_READ,
                "OVER_LINE_THRESHOLD" if not inexact else "UNKNOWN_SCALE",
                observed_lines=None if inexact else total_lines,
                observed_bytes=None if inexact else total_bytes,
            )
        if total_bytes > self._limits.max_targeted_read_bytes:
            return _blocked(
                "LARGE_READ",
                GateForm.FULL_READ,
                "OVER_BYTE_THRESHOLD",
                observed_lines=total_lines,
                observed_bytes=total_bytes,
            )
        return GateDecision(Decision.ALLOW, GateForm.FULL_READ)


GUIDANCE_LARGE_READ = (
    "This source is over the full-read threshold, so the read was stopped before it ran. "
    "Re-read a specific range with offset+limit (<=350 lines), run a bounded search, or ask "
    "the context-shunt reader a question about it - the reader answers from the source "
    "without putting it in this conversation."
)
GUIDANCE_UNCLASSIFIABLE_READ = (
    "This command reads a file but could not be proven bounded and safe, so it was stopped "
    "before it ran. Use a plain read with offset+limit, a bounded search, or ask the "
    "context-shunt reader a question about the file."
)
GUIDANCE_UNSAFE_SOURCE = (
    "The target is not a regular file, so it was not read. Name a regular file inside a "
    "configured workspace root."
)


def guidance_for(decision: GateDecision) -> str:
    if decision.code == "LARGE_READ":
        return GUIDANCE_LARGE_READ
    if decision.code == "UNCLASSIFIABLE_READ":
        return GUIDANCE_UNCLASSIFIABLE_READ
    return GUIDANCE_UNSAFE_SOURCE
