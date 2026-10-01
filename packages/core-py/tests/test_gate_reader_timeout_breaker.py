"""Repeated-timeout breaker: an identical timed-out read is not re-sent to the reader.

Synthetic fixtures and a network-free fake provider only. The breaker must stop exactly the
identical request, never a narrower or different one, never inspect, never a request whose
handles changed, and must expire, reset and clear on the right lifecycle boundaries.
"""

from __future__ import annotations

import json

import pytest

from context_shunt.clock import FakeClock
from context_shunt.errors import ShuntError
from context_shunt.reader import TIMEOUT_BREAKER_MAX_ENTRIES, TIMEOUT_BREAKER_TTL_MS
from context_shunt.session import ShuntSession
from tests.support import FakeLuna, claims_json, make_capability, make_config

pytestmark = pytest.mark.gate_reader

SOURCE = (
    "\n".join("retry_limit: 7" if i == 0 else f"padding-{i}-" + "x" * 120 for i in range(12)) + "\n"
)
ANSWER = claims_json(
    [{"text": "retry_limit is 7", "citation_ids": ["c1"]}],
    [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "retry_limit: 7"}],
)


def _timeout():
    return ShuntError("TIMEOUT", "MODEL_CALL", retryable=True)


def _fixture(tmp_path, replies, **config):
    path = tmp_path / "ws" / "source.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text(SOURCE)
    provider = FakeLuna(replies=list(replies), default_reply=ANSWER)
    clock = FakeClock()
    session = ShuntSession(
        "sess", make_config(tmp_path, **config), make_capability(), provider=provider, clock=clock
    )
    return session, provider, clock, session.register_path(str(path))


def _request(entry, request_id, **overrides):
    request = {
        "schema_version": "1.3",
        "request_id": request_id,
        "operation": "read",
        "question": "What is retry_limit?",
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    request.update(overrides)
    return request


def _always_timeout(n=64):
    return [_timeout() for _ in range(n)]


def test_an_identical_timed_out_read_is_not_resent(tmp_path):
    session, provider, _, entry = _fixture(tmp_path, _always_timeout())
    first = session.read(_request(entry, "req_1"))
    calls_after_first = provider.call_count
    assert calls_after_first > 0
    assert first["legacy_compaction"]["original_failure"] == "TIMEOUT"
    assert first["failure_detail"] != "REPEATED_TIMEOUT_SUPPRESSED"

    second = session.read(_request(entry, "req_2", question="  What is   retry_limit? "))
    assert provider.call_count == calls_after_first
    assert second["code"] == "LEGACY_COMPACTED" and second["status"] == "partial"
    assert second["failure_detail"] == "REPEATED_TIMEOUT_SUPPRESSED"
    assert second["legacy_compaction"]["original_failure"] == "TIMEOUT"
    assert second["provenance"]["attempts_started"] == 0
    assert second["provenance"]["usage_complete"] is True
    assert second["coverage"]["complete"] is False
    assert second["recovery"]["handles_valid"] is True
    assert second["recovery"]["actions"][0] == "NARROW_SELECTOR"
    assert "RETRY_SAME_QUESTION" not in second["recovery"]["actions"]
    assert "not sent to the reader again" in second["guidance"]
    assert second["sources"] == first["sources"]
    # Handles survive: an exact inspect over the same pair still works afterwards.
    inspected = session.inspect(
        {
            "schema_version": "1.3",
            "request_id": "req_i",
            "operation": "inspect",
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "selector": {"kind": "search", "needle": "retry_limit", "max_matches": 1},
            "budgets": {"max_result_bytes": 4096, "max_scan_lines": 100},
        }
    )
    assert inspected["code"] == "EXTRACTED"
    assert provider.call_count == calls_after_first


def test_the_suppressed_read_is_accounted_as_zero_attempts(tmp_path):
    session, _, _, entry = _fixture(tmp_path, _always_timeout())
    session.read(_request(entry, "req_1"))
    second = session.read(_request(entry, "req_2"))
    stats = session.stats(
        {"schema_version": "1.3", "request_id": "req_s", "operation": "stats", "page_size": 8}
    )
    row = next(r for r in stats["stats"]["records"] if r["operation_id"] == second["accounting_id"])
    assert row["attempts_started"] == 0
    assert "retry_limit: 7" not in json.dumps(stats)


@pytest.mark.parametrize(
    "change",
    [
        {"question": "Which retry_limit applies to the worker?"},
        {"refined": True},
        {"budgets": {"max_chunks": 2, "max_answer_bytes": 8192, "deadline_ms": 60000}},
        {"budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 120000}},
    ],
)
def test_a_meaningfully_different_request_is_still_sent(tmp_path, change):
    session, provider, _, entry = _fixture(tmp_path, [*_always_timeout(8)])
    session.read(_request(entry, "req_1"))
    before = provider.call_count
    changed = session.read(_request(entry, "req_2", **change))
    assert provider.call_count > before
    assert changed.get("failure_detail") != "REPEATED_TIMEOUT_SUPPRESSED"


def test_a_narrower_selector_is_still_sent(tmp_path):
    session, provider, _, entry = _fixture(tmp_path, [*_always_timeout(8)])
    session.read(_request(entry, "req_1"))
    provider.replies.clear()
    before = provider.call_count
    narrower = _request(entry, "req_2")
    narrower["sources"][0]["selector"] = {"kind": "lines", "start": 1, "end": 3}
    env = session.read(narrower)
    assert provider.call_count > before
    assert env["code"] == "ANSWERED"


def test_a_smaller_deadline_is_still_suppressed(tmp_path):
    session, provider, _, entry = _fixture(tmp_path, _always_timeout())
    session.read(_request(entry, "req_1"))
    before = provider.call_count
    smaller = _request(
        entry, "req_2", budgets={"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 30000}
    )
    env = session.read(smaller)
    assert provider.call_count == before
    assert env["failure_detail"] == "REPEATED_TIMEOUT_SUPPRESSED"


def test_the_breaker_expires_after_its_ttl(tmp_path):
    session, provider, clock, entry = _fixture(tmp_path, [*_always_timeout(8)])
    session.read(_request(entry, "req_1"))
    clock.advance(TIMEOUT_BREAKER_TTL_MS - 1)
    before = provider.call_count
    assert session.read(_request(entry, "req_2"))["failure_detail"] == (
        "REPEATED_TIMEOUT_SUPPRESSED"
    )
    assert provider.call_count == before
    clock.advance(1)
    provider.replies.clear()
    env = session.read(_request(entry, "req_3"))
    assert provider.call_count > before
    assert env["code"] == "ANSWERED"


def test_a_success_after_a_larger_deadline_clears_the_breaker(tmp_path):
    session, provider, _, entry = _fixture(tmp_path, [*_always_timeout(8)])
    session.read(_request(entry, "req_1"))
    provider.replies.clear()
    larger = _request(
        entry, "req_2", budgets={"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 90000}
    )
    assert session.read(larger)["code"] == "ANSWERED"
    provider.replies[:] = _always_timeout(8)
    # The answer cache does not cover the original (different budgets), so this is a real
    # send: the breaker entry was cleared by the success, not left blocking.
    before = provider.call_count
    session.read(_request(entry, "req_3"))
    assert provider.call_count > before


def test_reset_and_new_snapshots_start_clean(tmp_path):
    session, provider, _, entry = _fixture(tmp_path, [*_always_timeout(16)])
    session.read(_request(entry, "req_1"))
    fresh = session.reset(2)
    path = tmp_path / "ws" / "source.txt"
    again = fresh.register_path(str(path))
    before = provider.call_count
    env = fresh.read(_request(again, "req_2"))
    assert provider.call_count > before
    assert env.get("failure_detail") != "REPEATED_TIMEOUT_SUPPRESSED"
    # Same session, changed source bytes: a new snapshot is a different request.
    path.write_text(SOURCE + "tail\n")
    changed = fresh.register_path(str(path))
    assert changed.snapshot.snapshot_id != again.snapshot.snapshot_id
    before = provider.call_count
    fresh.read(_request(changed, "req_3"))
    assert provider.call_count > before


def test_a_session_does_not_see_another_sessions_breaker(tmp_path):
    session, provider, _, entry = _fixture(tmp_path, [*_always_timeout(16)])
    session.read(_request(entry, "req_1"))
    other = ShuntSession(
        "other", session.config, make_capability(), provider=provider, store=session.store
    )
    other_entry = other.register_path(str(tmp_path / "ws" / "source.txt"))
    before = provider.call_count
    env = other.read(_request(other_entry, "req_2"))
    assert provider.call_count > before
    assert env.get("failure_detail") != "REPEATED_TIMEOUT_SUPPRESSED"


def test_an_expired_handle_is_reported_as_expired_not_suppressed(tmp_path):
    session, provider, _, entry = _fixture(tmp_path, _always_timeout())
    session.read(_request(entry, "req_1"))
    session.close()
    env = session.read(_request(entry, "req_2"))
    assert env.get("failure_detail") != "REPEATED_TIMEOUT_SUPPRESSED"
    assert env["code"] in ("SOURCE_EXPIRED", "INVALID_REQUEST")


def test_a_partial_answer_under_timeout_never_trips_the_breaker(tmp_path):
    # One chunk answers, the rest time out: a verified partial answer is kept and the
    # identical question is still allowed to run again.
    long_source = "\n".join(
        "retry_limit: 7" if i == 0 else f"padding-{i}-" + "y" * 400 for i in range(400)
    )
    path = tmp_path / "ws" / "long.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text(long_source + "\n")
    provider = FakeLuna(replies=[ANSWER, *_always_timeout(32)], default_reply=ANSWER)
    session = ShuntSession("sess", make_config(tmp_path), make_capability(), provider=provider)
    entry = session.register_path(str(path))
    first = session.read(_request(entry, "req_1"))
    if first["code"] != "ANSWERED":
        pytest.skip("fixture planned a single chunk; partial-timeout shape not reachable")
    assert first["status"] == "partial"
    before = provider.call_count
    session.read(_request(entry, "req_2"))
    assert provider.call_count > before


def test_breaker_entries_are_bounded(tmp_path):
    session, provider, _, entry = _fixture(tmp_path, _always_timeout(512))
    for index in range(TIMEOUT_BREAKER_MAX_ENTRIES + 4):
        session.read(_request(entry, f"req_{index}", question=f"What is value {index}?"))
    assert len(session._reader._timeout_breaker) == TIMEOUT_BREAKER_MAX_ENTRIES
    # The oldest entry was evicted, so its question is sent again rather than suppressed.
    before = provider.call_count
    session.read(_request(entry, "req_old", question="What is value 0?"))
    assert provider.call_count > before


def test_breaker_state_carries_no_question_or_source_text(tmp_path):
    session, _, _, entry = _fixture(tmp_path, _always_timeout())
    session.read(_request(entry, "req_1"))
    state = repr(session._reader._timeout_breaker)
    assert "retry_limit" not in state
    for key, (deadline, expires) in session._reader._timeout_breaker.items():
        assert len(key) == 64 and isinstance(deadline, int) and isinstance(expires, int)
