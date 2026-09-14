"""integration accounting: an abandoned spilled pointer, and a requery Shunt cannot see.

Session 4 of the 2026-09-14 audit (``cron_2cf04e39ace6_20260914_061013``) spilled two GBrain
search results to pointers (message ``960329``, ~34.9KB; message ``960357``, ~17.6KB). The
reader was never invoked against either pointer. Instead, the caller reissued two smaller,
more targeted GBrain searches of its own (message ``960331``, ~10.8KB wire; message
``960359``, ~9.1KB) that landed under the tool-result-capture threshold and passed straight
through unchanged. The scope's ledger reported ``net_tokens_saved=12516`` - a real,
correctly-computed sum of what Shunt observed - but that figure says nothing about whether
the original two pointers were ever useful: the caller apparently abandoned them and paid
for a second round of tool calls that Shunt's own capture hook never sees at all. A
passthrough below the capture threshold produces no envelope and therefore no operation
record (``SpillEngine.evaluate``'s ``action="passthrough"`` branches; ``session.py``'s
``post_tool_result`` only calls ``self._record(...)`` when ``outcome.envelope is not
None``).

This is not an arithmetic bug. ``net_tokens_saved`` correctly totals only the operations
Shunt actually performed, and every credit it books already carries the honest
``full_payload_counterfactual`` label rather than a claim of confirmed use - see
``accounting.BaselineKind``, whose vocabulary has no member that could assert a realized or
confirmed saving in the first place. What this locks down as regressions is exactly those
two honesty properties:

1. the scope total is unaffected by a same-shaped recovery workload that Shunt never
   touches, because that workload never reaches its hook in a form worth recording - the
   ledger is honest about what it saw, not silent about the rest by omission of a caveat;
2. the credit vocabulary permanently has no member that could claim an unread pointer's
   saving was ever confirmed or realized.

The unclosed half - correlating an abandoned pointer with the specific recovery call that
replaced it - needs Hermes' own tool-call event stream, which this process cannot see from
inside the capture hook. That is a host-side capability question, not a repo bug, and it is
written up as a reviewable proposal rather than an installed patch in
``docs/host-proposal-recovery-correlation.md``.
"""

from __future__ import annotations

import pytest

from context_shunt.accounting import BaselineKind
from context_shunt.limits import EMITTED_SCHEMA_VERSION
from context_shunt.provider import UnavailableProvider
from context_shunt.session import ShuntSession
from tests.support import make_capability, make_config

pytestmark = pytest.mark.gate_accounting


def _gbrain_result(rows: int, tag: str) -> str:
    """Synthetic content shaped after a GBrain search result: many short JSON-ish rows."""
    body = "\n".join(f'{{"doc": "{tag}_{i:05d}", "score": 0.{i % 100:02d}}}' for i in range(rows)) + "\n"
    return body


def _stats(session: ShuntSession):
    return session.stats(
        {"schema_version": EMITTED_SCHEMA_VERSION, "request_id": "req_stats", "operation": "stats"}
    )["stats"]


def test_two_never_read_spills_credit_a_counterfactual_never_a_confirmation(tmp_path):
    config = make_config(
        tmp_path, tool_result_capture={"enabled": True, "host_ordering_verified_locally": True}
    )
    session = ShuntSession(
        "sess", config, make_capability(tool_result_capture=True),
        provider=UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    )

    first = _gbrain_result(700, "alpha")  # comfortably over the 16384-byte spill threshold
    second = _gbrain_result(460, "beta")
    assert len(first.encode("utf-8")) > 16384
    assert len(second.encode("utf-8")) > 16384

    outcome_a = session.post_tool_result("req_search_960329", first)
    outcome_b = session.post_tool_result("req_search_960357", second)
    assert outcome_a.action == "spill" and outcome_a.envelope["code"] == "SPILLED"
    assert outcome_b.action == "spill" and outcome_b.envelope["code"] == "SPILLED"

    records = _stats(session)["records"]
    spill_records = [r for r in records if r["kind"] == "spill"]
    assert len(spill_records) == 2
    for r in spill_records:
        # A withheld payload is credited as what it *would* have cost - never as proof the
        # caller went on to use it.
        assert r["baseline_kind"] == BaselineKind.FULL_PAYLOAD_COUNTERFACTUAL.value
        assert r["baseline_credit_tokens"] > 0
        assert r["net_tokens_saved"] > 0

    # No read or inspect ever touched either pointer - matching the production shape, where
    # the caller abandoned both and went looking elsewhere instead.
    assert not [r for r in records if r["kind"] in ("read", "refined_read", "inspect")]


def test_a_below_threshold_recovery_search_never_reaches_the_ledger(tmp_path):
    config = make_config(
        tmp_path, tool_result_capture={"enabled": True, "host_ordering_verified_locally": True}
    )
    session = ShuntSession(
        "sess", config, make_capability(tool_result_capture=True),
        provider=UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    )

    session.post_tool_result("req_search_960329", _gbrain_result(700, "alpha"))
    session.post_tool_result("req_search_960357", _gbrain_result(460, "beta"))

    # The reissued, narrower recovery searches: smaller by construction, so they land under
    # the capture threshold and pass straight through, exactly like the production requery.
    recovery_one = _gbrain_result(210, "recover1")
    recovery_two = _gbrain_result(180, "recover2")
    assert len(recovery_one.encode("utf-8")) <= 16384
    assert len(recovery_two.encode("utf-8")) <= 16384

    outcome_c = session.post_tool_result("req_search_960331", recovery_one)
    outcome_d = session.post_tool_result("req_search_960359", recovery_two)
    # Directly observable without touching stats at all: a passthrough carries no envelope,
    # so ``post_tool_result`` never calls ``self._record(...)`` for it (see session.py's
    # ``if outcome.envelope is not None:`` guard) - there is nothing left for the ledger to
    # even consider.
    assert outcome_c.action == "passthrough" and outcome_c.envelope is None
    assert outcome_d.action == "passthrough" and outcome_d.envelope is None

    # Querying stats is itself an accounted operation (kind "stats"), so a single query at
    # the end - after every post - is what proves the point: the only non-stats records in
    # the whole scope are the two original spills. Two further tool results passed through
    # this same hook and left no trace at all, favourable or not.
    records = _stats(session)["records"]
    non_stats = [r for r in records if r["kind"] != "stats"]
    assert len(non_stats) == 2
    assert all(r["kind"] == "spill" for r in non_stats)
    assert all(r["baseline_kind"] == BaselineKind.FULL_PAYLOAD_COUNTERFACTUAL.value for r in non_stats)
    assert all(r["net_tokens_saved"] > 0 for r in non_stats)


def test_the_baseline_vocabulary_has_no_member_that_could_claim_a_realized_saving():
    """Locks the enum so a future change to this claim is conscious, not accidental.

    If someone ever adds a ``BaselineKind`` member meaning "this withheld payload was
    confirmed used" (or similar), that is a real product decision - correlating an unread
    pointer with whatever later satisfied the caller's need is exactly the kind of judgment
    call that belongs in a reviewed proposal, not a silent enum addition. This test simply
    means such a change cannot land without deliberately touching this assertion.
    """
    assert {member.value for member in BaselineKind} == {
        "full_payload_counterfactual",
        "host_truncated_observed",
        "none",
    }
