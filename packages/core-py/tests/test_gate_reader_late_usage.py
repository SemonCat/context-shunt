"""unit reader: the envelope's ``provenance.attempts_usage_complete`` field (schema 1.3+).

The historical failure this replaces: two real production reads each made several reader
attempts before answering (``acc_d179a538c9a51b7a``: 7 started, 5 with provider-reported
usage; ``acc_968992724b9a99d6``: 4 started, 2 with usage) and the missing usage on the
unmeasured attempts totalled in the tens of thousands of tokens. That magnitude was computed
internally the whole time - ``ReaderCost.attempts_usage_complete`` already carried the exact
count, and it was already exposed on the ``stats`` operation's per-record accounting - but the
envelope a caller gets back from an ordinary ``read`` only ever carried the derived boolean
``provenance.usage_complete``. A bool cannot distinguish "one late attempt out of seven" from
"almost nothing is measured": both are simply ``false``. The real count never reached the one
place a caller actually looks at cost for a single answer.

``attempts_usage_complete`` is additive (1.3): a new, optional envelope-schema property that
carries the same int already computed for ``ReaderCost`` through to ``Provenance.to_dict()``,
without touching ``usage_complete`` or any existing required field.
"""

from __future__ import annotations

import pytest

from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.provider import FallbackChainProvider, HostBridgeProvider
from context_shunt.reader import Reader
from context_shunt.snapshot import snapshot_bytes
from tests.support import answer_json, make_registry

pytestmark = pytest.mark.gate_reader

L = DEFAULT_LIMITS
QUESTION = "What is the mode?"
SOURCE = b"mode = fast\n"
_ANSWER_TEXT = answer_json(
    "mode = fast [c1]",
    [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "mode = fast"}],
)


def _fixture(tmp_path, provider):
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE))
    request = {
        "schema_version": "1.3",
        "request_id": "req_late_usage",
        "operation": "read",
        "question": QUESTION,
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    return Reader(registry, provider).answer("sess", request)


def test_a_partially_measured_answer_reports_the_real_attempt_count(tmp_path):
    """Shaped after ``acc_d179a538c9a51b7a``: several attempts, only some with usage.

    One candidate fails without ever reporting billed usage at all (the same shape as an
    unmeasured attempt in production - not a malformed claim, simply nothing reported); the
    next candidate answers and reports exact usage. Two attempts were started; only one
    came back measured. ``usage_complete`` alone would say ``False`` and stop there -
    identical to the case where *nothing* was measured. The new field is the only place a
    caller can see that it was one attempt out of two, not zero out of two.
    """
    from context_shunt.errors import ShuntError

    def unmeasured_failure(**_kwargs):
        # No `billed_usage` attached - this candidate's cost is genuinely unknown, exactly
        # like the real incident's late/unreported attempts.
        raise ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)

    def exact_winner(**_kwargs):
        return {
            "text": _ANSWER_TEXT,
            "input_tokens": 9,
            "output_tokens": 11,
            "usage_exact": True,
        }

    chain = FallbackChainProvider(
        HostBridgeProvider(unmeasured_failure, L, provider="openai"),
        [HostBridgeProvider(exact_winner, L, "fallback", provider="openai")],
    )
    result = _fixture(tmp_path, chain)

    assert result.envelope["code"] == "ANSWERED"
    provenance = result.envelope["provenance"]

    assert provenance["attempts_started"] == 2
    # This is the fact the old boolean could not carry: one of the two attempts is
    # unmeasured, not all of them.
    assert provenance["attempts_usage_complete"] == 1
    assert provenance["usage_complete"] is False

    # And the two numbers must actually match the underlying cost ledger - the envelope
    # is not allowed to invent its own count.
    assert provenance["attempts_started"] == result.cost.attempts_started
    assert provenance["attempts_usage_complete"] == result.cost.attempts_usage_complete


def test_a_fully_measured_answer_still_reports_the_complete_count(tmp_path):
    """The control: when every attempt is measured, the new field says so explicitly.

    ``attempts_usage_complete == attempts_started`` here is not a coincidence the caller has
    to infer from a bare ``True`` - it is a number they can check against the attempt count
    they can also see, so trusting the reported cost never depends on taking the bool's word
    for it.
    """
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE))
    from tests.support import FakeLuna

    reply = answer_json(
        "The mode is fast [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "mode = fast"}],
    )
    result = Reader(registry, FakeLuna(replies=[reply])).answer(
        "sess",
        {
            "schema_version": "1.3",
            "request_id": "req_late_usage_ok",
            "operation": "read",
            "question": QUESTION,
            "sources": [
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": {"kind": "all"},
                }
            ],
            "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
        },
    )

    provenance = result.envelope["provenance"]
    assert provenance["attempts_started"] == 1
    assert provenance["attempts_usage_complete"] == 1
    assert provenance["usage_complete"] is True


def test_an_all_failed_request_reports_zero_measured_of_several_started(tmp_path):
    """Shaped after ``acc_968992724b9a99d6``: nothing answered, several attempts were made.

    A request that fails outright still made physical calls, and this is the failure-path
    construction of ``Provenance`` (``_failure_provenance``), not the success path. Before
    this fix, a failed request's envelope carried ``attempts_started`` but had no way at all
    to say how many of those attempts had reported usage - only the unconditional ``False``
    on ``usage_complete``, which is true of every failure regardless of magnitude.
    """
    from context_shunt.errors import ShuntError

    def unmeasured_failure(**_kwargs):
        raise ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)

    chain = FallbackChainProvider(
        HostBridgeProvider(unmeasured_failure, L, provider="openai"),
        [HostBridgeProvider(unmeasured_failure, L, "fallback", provider="openai")],
    )
    result = _fixture(tmp_path, chain)

    provenance = result.envelope["provenance"]
    assert provenance["attempts_started"] >= 2
    assert provenance["attempts_usage_complete"] == 0
    assert provenance["usage_complete"] is False
