"""unit cancellation: deadlines and cancellation are asserted on a fake clock."""

from __future__ import annotations

import pytest

from context_shunt.clock import Deadline, FakeClock
from context_shunt.errors import CancelledError, DeadlineExceeded
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.provider import ModelResponse, ModelUsage, TransientProviderError
from context_shunt.reader import Reader
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes
from tests.support import answer_json

pytestmark = pytest.mark.gate_cancellation
L = DEFAULT_LIMITS


class ClockProvider:
    """A provider that consumes fake time and records whether it was ever called."""

    def __init__(self, clock: FakeClock, cost_ms: int, reply):
        self.clock = clock
        self.cost_ms = cost_ms
        self.reply = reply
        self.calls = 0
        self.calls_after_cancel = 0
        self.cancelled = False

    def complete(self, *, system, user, max_output_tokens, timeout_ms):
        self.calls += 1
        if self.cancelled:
            self.calls_after_cancel += 1
        self.clock.advance(self.cost_ms)
        reply = self.reply() if callable(self.reply) else self.reply
        if isinstance(reply, Exception):
            raise reply
        return ModelResponse(text=reply, model=L.reader_model, usage=ModelUsage())


def test_contract_deadlines():
    assert (L.gate_probe_deadline_ms, L.spill_io_deadline_ms) == (1000, 5000)
    assert (L.model_call_deadline_ms, L.request_deadline_ms) == (20000, 60000)


def test_deadline_expires_and_reports_timeout():
    clock = FakeClock()
    deadline = Deadline.start(clock, 60000)
    deadline.check("STAGE")
    clock.advance(60001)
    with pytest.raises(DeadlineExceeded):
        deadline.check("STAGE")


def test_cancellation_beats_an_unexpired_deadline():
    clock = FakeClock()
    deadline = Deadline.start(clock, 60000)
    deadline.cancel()
    with pytest.raises(CancelledError):
        deadline.check("STAGE")


def test_stage_budget_never_outlives_the_request_budget():
    clock = FakeClock()
    deadline = Deadline.start(clock, 60000)
    clock.advance(55000)
    assert deadline.sub_budget(L.model_call_deadline_ms) == 5000


def _request(entry, deadline_ms=60000):
    return {
        "schema_version": "1.0",
        "request_id": "req_cancel",
        "operation": "read",
        "question": "What is configured here?",
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": deadline_ms},
    }


def _entry(lines=6000):
    registry = SourceRegistry()
    body = "".join(f"key{i} = value{i}\n" for i in range(lines))
    return registry, registry.register("sess", snapshot_bytes(body.encode()))


def test_request_deadline_stops_remaining_chunks_and_reports_partial():
    clock = FakeClock()
    registry, entry = _entry()
    provider = ClockProvider(clock, 25000, answer_json("", []))
    reader = Reader(registry, provider, clock=clock)
    env = reader.answer("sess", _request(entry))
    assert provider.calls <= 3, "work stopped once the budget was spent"
    assert env["status"] in ("partial", "error")
    if env["status"] == "partial":
        assert any(o["reason"] == "TIMEOUT" for o in env["coverage"]["omitted"])
    assert env["coverage"]["complete"] is False


def test_cancel_before_publish_stops_new_work_and_blocks_late_results():
    clock = FakeClock()
    registry, entry = _entry(lines=6000)
    state = {"n": 0}

    def reply():
        state["n"] += 1
        if state["n"] == 1:
            deadline.cancel()
            provider.cancelled = True
        return answer_json("", [])

    provider = ClockProvider(clock, 10, reply)
    reader = Reader(registry, provider, clock=clock)
    deadline = Deadline.start(clock, L.request_deadline_ms)
    env = reader.answer("sess", _request(entry), deadline=deadline)
    assert env["status"] == "error" and env["code"] == "CANCELLED"
    assert env["answer"] == ""
    assert provider.calls_after_cancel <= L.max_concurrent_model_calls


def test_cancel_while_queued_makes_zero_provider_calls():
    clock = FakeClock()
    registry, entry = _entry(lines=100)
    provider = ClockProvider(clock, 10, answer_json("", []))
    deadline = Deadline.start(clock, L.request_deadline_ms)
    deadline.cancel()
    env = Reader(registry, provider, clock=clock).answer("sess", _request(entry), deadline=deadline)
    assert provider.calls == 0
    assert env["code"] == "CANCELLED"


def test_retry_shares_the_same_budget_and_happens_at_most_once():
    clock = FakeClock()
    registry, entry = _entry(lines=100)
    provider = ClockProvider(clock, 21000, TransientProviderError("PROVIDER_CALL_FAILED"))
    env = Reader(registry, provider, clock=clock).answer("sess", _request(entry))
    # First attempt spends 21s, the retry another 21s; a third attempt never happens.
    assert provider.calls == 2
    assert env["status"] in ("partial", "error")


def test_timeout_after_deadline_publishes_nothing_new():
    clock = FakeClock()
    registry, entry = _entry(lines=100)
    good = answer_json(
        "key0 is present [c1]", [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "key0"}]
    )
    provider = ClockProvider(clock, 61000, good)
    env = Reader(registry, provider, clock=clock).answer("sess", _request(entry))
    # The answer arrived after the request budget was already spent, so it is not published.
    assert env["answer"] == ""
    assert env["coverage"]["complete"] is False
