"""unit cancellation: deadlines and cancellation are asserted on a fake clock."""

from __future__ import annotations

import pytest

from context_shunt.clock import Deadline, FakeClock
from context_shunt.errors import CancelledError, DeadlineExceeded
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.provenance import ModelIdentity, TokenMethod, Usage
from context_shunt.provider import ModelResponse, ProviderTarget, TransientProviderError
from context_shunt.reader import Reader
from context_shunt.snapshot import snapshot_bytes
from tests.support import answer_json, make_registry

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
        return ModelResponse(
            text=reply,
            requested=ModelIdentity(provider="openai", model=L.reader_model),
            resolved=ModelIdentity(provider="openai", model=L.reader_model),
            reported=ModelIdentity(provider="openai", model=L.reader_model),
            provider_confirms_generation=True,
            usage=Usage(input_tokens=1, output_tokens=1, method=TokenMethod.EXACT),
        )

    @property
    def target(self) -> ProviderTarget:
        return ProviderTarget(model=L.reader_model, provider="openai")


def make_registry_at(clock):
    """A registry backed by a temp store, for tests that drive a fake clock."""
    import tempfile
    from pathlib import Path as _Path

    return make_registry(_Path(tempfile.mkdtemp()), session_id="sess")


def test_contract_deadlines():
    assert (L.gate_probe_deadline_ms, L.spill_io_deadline_ms) == (1000, 5000)
    assert (L.model_call_deadline_ms, L.request_deadline_ms) == (45000, 60000)


def test_the_per_call_deadline_can_actually_serve_the_default_reader_model():
    """The default model must be reachable under the default deadline.

    ``model_call`` was 20000 through the 1.1 work. The default reader model is
    ``gpt-5.6-luna``, a reasoning model, and a measured live reader call against it takes
    roughly 34s wall - so every call aborted at 20s and the reader could never answer on a
    stock configuration. Because ``deadlines_ms`` is a normative contract value that a
    deployment may only *narrow*, no operator could raise it either: the default
    configuration was unusable and unfixable from outside.

    The floor here is the measured latency plus headroom. It is asserted rather than
    commented so that lowering the cap back under what the default model needs fails here
    instead of silently disabling the reader again.
    """
    measured_live_call_ms = 34_000
    assert L.model_call_deadline_ms >= measured_live_call_ms
    # ... and one call plus its budget must still fit inside the request deadline.
    assert L.model_call_deadline_ms <= L.request_deadline_ms


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


def _entry(tmp_path, lines=6000):
    registry = make_registry(tmp_path, session_id="sess")
    body = "".join(f"key{i} = value{i}\n" for i in range(lines))
    return registry, registry.register("sess", snapshot_bytes(body.encode()))


def test_request_deadline_stops_remaining_chunks_and_reports_partial(tmp_path):
    clock = FakeClock()
    registry, entry = _entry(tmp_path)
    provider = ClockProvider(clock, 25000, answer_json("", []))
    reader = Reader(registry, provider, clock=clock)
    env = reader.answer("sess", _request(entry)).envelope
    # Two workers can both start a second round at fake time 50s, while 10s remains.
    # Their late results are discarded; no third round may start after the 60s check.
    assert provider.calls <= 2 * L.max_concurrent_model_calls
    assert env["status"] in ("partial", "error")
    if env["status"] == "partial":
        assert any(o["reason"] == "TIMEOUT" for o in env["coverage"]["omitted"])
    assert env["coverage"]["complete"] is False


def test_cancel_before_publish_stops_new_work_and_blocks_late_results(tmp_path):
    clock = FakeClock()
    registry, entry = _entry(tmp_path, lines=6000)
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
    env = reader.answer("sess", _request(entry), deadline=deadline).envelope
    assert env["status"] == "error" and env["code"] == "CANCELLED"
    assert env["answer"] == ""
    assert provider.calls_after_cancel <= L.max_concurrent_model_calls


def test_cancel_while_queued_makes_zero_provider_calls(tmp_path):
    clock = FakeClock()
    registry, entry = _entry(tmp_path, lines=100)
    provider = ClockProvider(clock, 10, answer_json("", []))
    deadline = Deadline.start(clock, L.request_deadline_ms)
    deadline.cancel()
    env = (
        Reader(registry, provider, clock=clock)
        .answer("sess", _request(entry), deadline=deadline)
        .envelope
    )
    assert provider.calls == 0
    assert env["code"] == "CANCELLED"


def test_retry_shares_the_same_budget_and_happens_at_most_once(tmp_path):
    clock = FakeClock()
    registry, entry = _entry(tmp_path, lines=100)
    provider = ClockProvider(clock, 21000, TransientProviderError("PROVIDER_CALL_FAILED"))
    env = Reader(registry, provider, clock=clock).answer("sess", _request(entry)).envelope
    # First attempt spends 21s, the retry another 21s; a third attempt never happens.
    assert provider.calls == 2
    assert env["status"] in ("partial", "error")


def test_timeout_after_deadline_publishes_nothing_new(tmp_path):
    clock = FakeClock()
    registry, entry = _entry(tmp_path, lines=100)
    good = answer_json(
        "key0 is present [c1]", [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "key0"}]
    )
    provider = ClockProvider(clock, 61000, good)
    env = Reader(registry, provider, clock=clock).answer("sess", _request(entry)).envelope
    # The answer arrived after the request budget was already spent, so it is not published.
    assert env["answer"] == ""
    assert env["coverage"]["complete"] is False


# -- the deadline itself is the bound; it must not be movable -----------------


def test_a_deadline_cannot_be_widened_after_it_starts():
    """The request budget is what stops a late answer being published.

    ``Deadline`` was a plain mutable dataclass, so a caller holding one could reassign
    ``started_ms`` or ``budget_ms`` and grant itself an effectively unbounded budget -
    setting ``started_ms`` far in the past yielded ~10^12 ms remaining. The TypeScript
    twin has always been ``readonly`` behind a private constructor, so this is also a
    parity gap.
    """
    clock = FakeClock()
    deadline = Deadline.start(clock, 60_000)
    assert deadline.remaining_ms() == 60_000

    for field_name, value in (("budget_ms", 10**12), ("started_ms", -(10**12))):
        with pytest.raises(Exception) as exc:
            setattr(deadline, field_name, value)
        assert "frozen" in str(exc.value).lower() or isinstance(exc.value, AttributeError)

    # The budget is exactly what it was started with, and it only ever shrinks.
    assert deadline.remaining_ms() == 60_000
    clock.advance(25_000)
    assert deadline.remaining_ms() == 35_000


def test_positional_construction_keeps_remaining_time_semantics():
    """`Deadline(clock, started_ms, budget_ms)` stays the documented positional order."""
    clock = FakeClock()
    clock.advance(5_000)
    deadline = Deadline(clock, clock.now_ms(), 60_000)
    assert deadline.remaining_ms() == 60_000
    assert deadline.elapsed_ms() == 0
    clock.advance(20_000)
    assert deadline.elapsed_ms() == 20_000
    assert deadline.remaining_ms() == 40_000
    # A stage never outlives the request budget, whichever is smaller.
    assert deadline.sub_budget(45_000) == 40_000
    assert deadline.sub_budget(10_000) == 10_000
    # Remaining time is floored at zero rather than going negative.
    clock.advance(100_000)
    assert deadline.remaining_ms() == 0 and deadline.expired() is True


def test_a_deadline_after_the_call_still_accounts_for_what_the_provider_was_paid():
    """Tokens the provider already billed must not vanish because publishing failed.

    `answer` catches `ShuntError` and returned `ReaderCost.none()` - "no attempt was
    made". But the request budget is checked again at PUBLISH, *after* every model call
    has completed and been billed. A request that ran its calls and then ran out of time
    reported zero cost, so a session's accounting understated real spend and every
    savings figure derived from it was overstated.
    """
    clock = FakeClock()
    # The call itself fits the budget; the remaining time does not survive to PUBLISH.
    provider = ClockProvider(
        clock, 60_000, answer_json("mode = fast [c1]", [(1, 1, "mode = fast")])
    )
    registry = make_registry_at(clock)
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))

    result = Reader(registry, provider, clock=clock).answer("sess", _request(entry))

    assert provider.calls == 1, "the provider must actually have been called"
    assert result.envelope["status"] == "error"
    assert result.envelope["code"] == "TIMEOUT"
    # The call happened and was billed, so the cost must say so.
    assert result.cost.attempts_started == 1
    assert result.cost.input_tokens == 1
    assert result.cost.output_tokens == 1
    assert result.cost.method is TokenMethod.EXACT
    assert result.provenance.attempts_started == 1
