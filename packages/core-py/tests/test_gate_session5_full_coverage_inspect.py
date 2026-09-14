"""integration accounting: a spilled source fully re-read through inspect, end to end.

Session 5 of the 2026-09-14 audit spilled one tool result (a Slack-history dump, ~17.6KB
across several hundred short lines) and then paged it back with two ``inspect`` calls
covering the whole byte range (``0:10000`` and ``10000:17601`` in production). The
production finding was that this source, taken alone, made the estimate *worse* than not
shunting it at all - the spill's one-time baseline credit was smaller than what the two
follow-up extractions cost, so treating the spill's credit as a standalone "saving" would
be misleading.

This drives the real ``ShuntSession``/``SpillEngine``/accounting pipeline end to end -  no
hand-built ``OperationRecord`` - and asks the one question that matters: does
``operation_totals`` for this scope actually say the net is negative once the full source
has been re-read through inspect? A prior segment's reading of ``session.py`` already
confirmed each ``inspect`` call is booked with ``baseline=Baseline.none()``,
``baseline_credited=False`` (so its own net is always negative, its own envelope overhead)
and never re-claims the spill's credit; this test is the empirical proof that summing the
real records over a full-coverage session actually produces the negative total the
production finding described, not just that each record is individually honest.
"""

from __future__ import annotations

import pytest

from context_shunt.limits import EMITTED_SCHEMA_VERSION
from context_shunt.provider import UnavailableProvider
from context_shunt.session import ShuntSession
from tests.support import make_capability, make_config

pytestmark = pytest.mark.gate_accounting


def _slack_history_dump(lines: int = 420) -> str:
    """Synthetic content shaped after the production source: many short lines.

    Entirely invented usernames/text - no real merchant, user or log content. What matters
    structurally is the shape: short, uniform lines whose total is a little over the
    16384-byte spill/inspect-page threshold, the same shape that made the real source spill
    on size and then need two inspect pages to read back in full.
    """
    rows = [f"user_{i % 7:02d}: synthetic message body number {i:04d}" for i in range(lines)]
    body = "\n".join(rows) + "\n"
    assert len(body.encode("utf-8")) > 16384, "fixture must actually exceed the spill threshold"
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


def _stats_totals(session):
    return session.stats(
        {
            "schema_version": EMITTED_SCHEMA_VERSION,
            "request_id": "req_stats",
            "operation": "stats",
        }
    )["stats"]["totals"]


def test_a_fully_reread_spilled_source_reports_a_negative_net_not_a_false_saving(tmp_path):
    """End to end: spill once, inspect the whole thing back, and check the honest total.

    The provider is one that refuses every call, since this source shape is never answered
    by the reader in this scenario - only spilled and paged back through the deterministic
    inspect path, exactly like session 5.
    """
    config = make_config(
        tmp_path, tool_result_capture={"enabled": True, "host_ordering_verified_locally": True}
    )
    session = ShuntSession(
        "sess",
        config,
        make_capability(tool_result_capture=True),
        provider=UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    )

    body = _slack_history_dump()
    body_bytes = body.encode("utf-8")
    outcome = session.post_tool_result("req_slack_history", body)
    assert outcome.action == "spill"
    assert outcome.envelope["code"] == "SPILLED"
    pointer = outcome.envelope["pointer"]

    # Mirror the production split: two inspect pages covering the entire byte range.
    split = 10000
    first = session.inspect(_inspect_request(pointer, 0, split))
    assert first["code"] == "EXTRACTED"
    second = session.inspect(_inspect_request(pointer, split, len(body_bytes)))
    assert second["code"] == "EXTRACTED"

    totals = _stats_totals(session)
    records = session.stats(
        {
            "schema_version": EMITTED_SCHEMA_VERSION,
            "request_id": "req_stats_records",
            "operation": "stats",
        }
    )["stats"]["records"]
    spill_record = next(r for r in records if r["kind"] == "spill")
    inspect_records = [r for r in records if r["kind"] == "inspect"]
    assert len(inspect_records) == 2

    # Per-record honesty, already established: the spill alone is credited once, and each
    # inspect is its own negative overhead, never re-claiming that credit.
    assert spill_record["baseline_credit_tokens"] > 0
    assert all(r["baseline_credit_tokens"] == 0 for r in inspect_records)
    assert all(r["net_tokens_saved"] < 0 for r in inspect_records)

    # The empirical claim this test exists to check: once the source has genuinely been
    # read back in full, the *scope's* net is honestly negative - the spill's one-time
    # credit does not mask the cost of retrieving the whole thing back through inspect.
    # If this ever turns positive, the accounting is claiming a saving for a source that
    # was, in total, read back in full at a net cost - exactly the production finding.
    assert totals["net_tokens_saved"] < 0, (
        "a fully re-read spilled source reported a net saving; this is the misleading "
        "outcome session 5 identified in production"
    )
    # And the total is not accidentally the spill record alone - it does include the
    # inspect overhead that made the difference.
    assert totals["net_tokens_saved"] < spill_record["net_tokens_saved"]
