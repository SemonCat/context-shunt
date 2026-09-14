"""integration accounting: a mixed scope hides a real loss behind an unrealized credit.

Session 5 of the 2026-09-14 audit (`20260914_010010_6cbb09e5`) is not one source, it is
two, and the scope-level net only makes sense read as their sum:

- The Slack-history dump (~17.6KB) spilled, then was paged back in full through two
  `inspect` calls covering its whole byte range. Read alone, this source's net is honestly
  negative (see `test_gate_session5_full_coverage_inspect.py`): the one-time spill credit
  was smaller than the two extractions it took to read it all back.
- The bkt-rules source (~18.3KB) also spilled, but was never read back at all - its
  one-time credit stands alone, uncontested, exactly like a session-4 abandoned pointer
  (`test_gate_session4_unread_pointer_requery.py`): honestly labelled
  `full_payload_counterfactual`, never a claim that the credit was realized.

The production finding was `net_tokens_saved=3312` for the whole scope - a small positive
number that reads, on its own, as "this scope saved a little". What it actually is: one
genuinely negative source (the fully-reread Slack history) plus one merely-uncontested
counterfactual (the never-reread bkt rules) that happens to be large enough to flip the
scope's sign to positive. A caller who stops at the scope total and calls it "a saving"
would be wrong for both halves at once - wrong that the Slack source saved anything (it
cost more than it saved) and wrong that the bkt-rules credit was ever spent on anything
(nothing ever used it).

This is the composite the two earlier per-source tests do not cover on their own: neither
proves what happens when both shapes share one scope, which is the actual production
shape. Nothing here is a bug - the per-record honesty (spill credited, inspect never
re-crediting, `full_payload_counterfactual` never asserting realization) already holds -
but a positive scope total for a mixed-shape scope like this one must not be read as "the
scope saved tokens", and this test locks that reading down as regression-tested rather
than left as an inference a report author has to make by hand.
"""

from __future__ import annotations

import pytest

from context_shunt.accounting import BaselineKind
from context_shunt.limits import EMITTED_SCHEMA_VERSION
from context_shunt.provider import UnavailableProvider
from context_shunt.session import ShuntSession
from tests.support import make_capability, make_config

pytestmark = pytest.mark.gate_accounting


def _slack_history_dump(lines: int = 420) -> str:
    rows = [f"user_{i % 7:02d}: synthetic message body number {i:04d}" for i in range(lines)]
    body = "\n".join(rows) + "\n"
    assert len(body.encode("utf-8")) > 16384
    return body


def _bkt_rules_dump(rows: int = 430) -> str:
    body = "\n".join(f"rule_{i:04d}: synthetic bkt condition {i:04d}" for i in range(rows)) + "\n"
    assert len(body.encode("utf-8")) > 16384
    return body


def _inspect_request(pointer, start: int, end: int):
    return {
        "schema_version": EMITTED_SCHEMA_VERSION,
        "request_id": f"req_inspect_{start}_{end}",
        "operation": "inspect",
        "source_id": pointer["source_id"],
        "snapshot_id": pointer["snapshot_id"],
        "selector": {"kind": "bytes", "start": start, "end": end},
        "budgets": {"max_result_bytes": 16384, "max_scan_lines": 20000},
    }


def _stats(session: ShuntSession):
    return session.stats(
        {"schema_version": EMITTED_SCHEMA_VERSION, "request_id": "req_stats", "operation": "stats"}
    )["stats"]


def test_a_realized_loss_and_an_unrealized_credit_share_one_scope(tmp_path):
    config = make_config(
        tmp_path, tool_result_capture={"enabled": True, "host_ordering_verified_locally": True}
    )
    session = ShuntSession(
        "sess", config, make_capability(tool_result_capture=True),
        provider=UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    )

    # Source A: Slack history, spilled then fully re-read through inspect (a real cost).
    slack_body = _slack_history_dump()
    slack_bytes = slack_body.encode("utf-8")
    slack_outcome = session.post_tool_result("req_slack_history", slack_body)
    assert slack_outcome.action == "spill"
    slack_pointer = slack_outcome.envelope["pointer"]
    split = 10000
    first = session.inspect(_inspect_request(slack_pointer, 0, split))
    assert first["code"] == "EXTRACTED"
    second = session.inspect(_inspect_request(slack_pointer, split, len(slack_bytes)))
    assert second["code"] == "EXTRACTED"

    # Source B: bkt rules, spilled and never read back at all (an uncontested credit).
    bkt_body = _bkt_rules_dump()
    bkt_outcome = session.post_tool_result("req_bkt_rules", bkt_body)
    assert bkt_outcome.action == "spill"
    # Deliberately: no read, no inspect against bkt_outcome's pointer.

    stats = _stats(session)
    records = stats["records"]
    kinds = {r["kind"] for r in records}
    assert "spill" in kinds and "inspect" in kinds

    # No accounting record carries a source_id (deliberately - the ledger is non-content
    # metadata only), so the two spills are told apart by insertion order: the ledger is a
    # chronological log, and the Slack source was spilled strictly before the bkt-rules one.
    spill_records = [r for r in records if r["kind"] == "spill"]
    assert len(spill_records) == 2
    slack_spill, bkt_spill = spill_records
    inspect_records = [r for r in records if r["kind"] == "inspect"]
    assert len(inspect_records) == 2

    # Per-record honesty, as established in the two source-specific tests this composes:
    assert slack_spill["baseline_kind"] == BaselineKind.FULL_PAYLOAD_COUNTERFACTUAL.value
    assert bkt_spill["baseline_kind"] == BaselineKind.FULL_PAYLOAD_COUNTERFACTUAL.value
    assert all(r["baseline_credit_tokens"] == 0 for r in inspect_records)
    assert all(r["net_tokens_saved"] < 0 for r in inspect_records)

    # The composite claim under test: the Slack source's own contribution to the scope
    # (its spill credit plus both inspect costs) is net-negative on its own -
    slack_contribution = slack_spill["net_tokens_saved"] + sum(r["net_tokens_saved"] for r in inspect_records)
    assert slack_contribution < 0, (
        "the fully-reread source must be a real loss on its own terms, not merely diluted "
        "by the other source's uncontested credit"
    )
    # - while the bkt-rules source, never read back, still carries its full uncontested
    # credit exactly like a session-4 abandoned pointer:
    assert bkt_spill["net_tokens_saved"] > 0

    # And yet the *scope* total can come out positive, because the uncontested credit is
    # large enough to outweigh the realized loss:
    totals = stats["totals"]
    assert totals["net_tokens_saved"] > 0, (
        "fixture must reproduce the production shape: a positive scope total composed of "
        "one real loss and one merely-unrealized credit"
    )
    # The number that would be wrong to read off the total alone: it is not proof this
    # scope saved tokens. It is proof that an uncredited pointer happened to be larger than
    # the realized loss on the other source. Demonstrate that arithmetically:
    assert totals["net_tokens_saved"] < bkt_spill["net_tokens_saved"], (
        "the scope total must be smaller than the uncontested credit alone - i.e. it is "
        "already netted against a real loss, not a clean saving on top of it"
    )
    assert totals["net_tokens_saved"] == pytest.approx(
        slack_contribution + bkt_spill["net_tokens_saved"], abs=1
    )
