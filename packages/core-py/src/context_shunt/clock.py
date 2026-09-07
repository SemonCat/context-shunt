"""Deadlines and cancellation with an injectable clock.

Production uses :class:`MonotonicClock`; the cancellation gate uses :class:`FakeClock`
so the 1s / 5s / 20s / 60s budgets are asserted deterministically rather than by sleeping.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .errors import CancelledError, DeadlineExceeded


class Clock:
    def now_ms(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class MonotonicClock(Clock):
    def now_ms(self) -> int:
        return int(time.monotonic() * 1000)


@dataclass
class FakeClock(Clock):
    _now: int = 0

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        self._now += ms


@dataclass(frozen=True)
class Deadline:
    """A budget shared by every stage of one request.

    ``check`` is called before starting new work and before publishing anything, so a
    late provider response can never be published after the budget is spent.

    Frozen, because the deadline *is* the bound. While it was a plain mutable dataclass a
    caller holding one could reassign ``started_ms`` or ``budget_ms`` and grant itself an
    effectively unbounded budget, which is the one thing this type exists to prevent. The
    TypeScript twin has always been ``readonly`` behind a private constructor; this closes
    the parity gap. Cancellation still works: the ``Event`` is mutated, never replaced.

    The positional order stays ``(clock, started_ms, budget_ms)`` so existing construction
    keeps its documented remaining-time semantics.
    """

    clock: Clock
    started_ms: int
    budget_ms: int
    _cancelled: threading.Event = field(default_factory=threading.Event)

    @classmethod
    def start(cls, clock: Clock, budget_ms: int) -> Deadline:
        return cls(clock=clock, started_ms=clock.now_ms(), budget_ms=budget_ms)

    def elapsed_ms(self) -> int:
        return self.clock.now_ms() - self.started_ms

    def remaining_ms(self) -> int:
        return max(0, self.budget_ms - self.elapsed_ms())

    def expired(self) -> bool:
        return self.remaining_ms() <= 0

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self, stage: str) -> None:
        if self.cancelled:
            raise CancelledError(stage)
        if self.expired():
            raise DeadlineExceeded(stage)

    def sub_budget(self, stage_budget_ms: int) -> int:
        """A stage may never outlive the request budget."""
        return min(stage_budget_ms, self.remaining_ms())
