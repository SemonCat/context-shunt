"""integration inspect: a search truncated by ``max_matches`` must not claim completeness.

Session 3 of the 2026-09-14 audit (`20260913_220020_6c5f7a47`) asked an "exact error
count/distinct trace IDs" question that only the LLM reader answered, and the reader's
answer timed out at 2/4 chunks - a partial answer that the reader path already, correctly,
never lets read as a confirmed whole-source count (see `test_gate_reader.py`'s
`test_partial_exact_zero_claim_is_scoped_inside_the_published_answer` and friends). But the
product objective is broader than "the LLM must not lie about an incomplete scan": "exact
counts/grouping/filtering should use deterministic processing where feasible - don't make
an LLM scan everything for a simple count." `inspect`'s ``search`` selector is exactly that
deterministic alternative for a literal-substring count, and it was never audited for the
same honesty property the reader path already has.

It should have been: before this test, a search selector that stopped because it hit its
own requested ``max_matches`` cap - not because the source ran out - still reported
``complete: true`` and returned no continuation cursor, indiscriminately of whether further
matches existed just past the cutoff. A caller asking for `max_matches=20` over a source
that actually contains 49 matches got back ``matches_found: 20, complete: true,
next_cursor: null`` - a result indistinguishable from "the source has exactly 20 matches
and none more", when the true count was more than double that and 1,799 of the source's
3,000 lines were never even scanned. That is precisely the "claim of a whole-source count
under partial coverage" the audit asks to reject, just surfacing in the deterministic path
that was supposed to be the safe alternative to the LLM one.

The fix scopes ``complete`` to what it can actually mean: the entire remaining source was
looked at for this needle, not merely that the caller's own cap was satisfied. Hitting the
match cap now behaves exactly like hitting the scan or byte budget already did - it emits
a continuation cursor and marks the page incomplete - so a caller who genuinely wants an
exact total can keep paging (by raising `max_matches` on the next call) until the whole
source has actually been scanned, entirely without invoking the reader.
"""

from __future__ import annotations

import pytest

from context_shunt.limits import EMITTED_SCHEMA_VERSION
from context_shunt.provider import UnavailableProvider
from context_shunt.session import ShuntSession
from tests.support import make_capability, make_config

pytestmark = pytest.mark.gate_inspect


def _loki_shaped_source(total_lines: int = 3000, error_every: int = 60) -> tuple[str, int]:
    """Many short lines, a literal needle scattered through them - shaped like the Loki
    payloads in session 3, not copied from any real log."""
    lines = []
    true_count = 0
    for i in range(total_lines):
        if i % error_every == 0 and i > 0:
            lines.append(f"line {i:05d} ERROR synthetic failure code {i % 7}")
            true_count += 1
        else:
            lines.append(f"line {i:05d} ok synthetic value {i % 7}")
    return "\n".join(lines) + "\n", true_count


def _search_request(pointer, *, max_matches: int, max_scan_lines: int = 20000, cursor: str | None = None):
    request = {
        "schema_version": EMITTED_SCHEMA_VERSION,
        "request_id": "req_search",
        "operation": "inspect",
        "source_id": pointer["source_id"],
        "snapshot_id": pointer["snapshot_id"],
        "selector": {"kind": "search", "needle": "ERROR", "max_matches": max_matches},
        "budgets": {"max_result_bytes": 16384, "max_scan_lines": max_scan_lines},
    }
    if cursor is not None:
        request["cursor"] = cursor
    return request


def _spilled_pointer(tmp_path, session, body):
    outcome = session.post_tool_result("req_source", body)
    assert outcome.action == "spill"
    return outcome.envelope["pointer"]


def test_a_search_capped_by_max_matches_no_longer_claims_the_whole_source_was_seen(tmp_path):
    config = make_config(
        tmp_path, tool_result_capture={"enabled": True, "host_ordering_verified_locally": True}
    )
    session = ShuntSession(
        "sess", config, make_capability(tool_result_capture=True),
        provider=UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    )
    body, true_count = _loki_shaped_source()
    assert true_count == 49
    pointer = _spilled_pointer(tmp_path, session, body)

    env = session.inspect(_search_request(pointer, max_matches=20))
    extraction = env["extraction"]
    assert extraction["matches_found"] == 20

    # The bug this locks shut: a page that stopped only because it hit its own requested
    # cap - with most of the source never scanned - must not be indistinguishable from a
    # page that genuinely reached the end of the source. Unscanned lines remain, so a 21st
    # match may exist just past the cutoff; "complete" cannot be true here.
    assert extraction["complete"] is False, (
        "a max_matches cutoff was reported as a complete scan even though only "
        f"{extraction['lines_scanned']} of the source's lines were ever looked at"
    )
    assert extraction["next_cursor"] is not None, (
        "a capped search must leave a way to keep paging toward the true total, exactly "
        "like a byte- or scan-budget cutoff already does"
    )
    assert extraction["lines_scanned"] < 3000


def test_a_caller_can_page_a_scan_budget_cutoff_to_the_true_total(tmp_path):
    """Deterministic aggregation without the reader: page to the real total, zero LLM calls.

    A cursor is bound to its selector (``max_matches`` included), so a resumed call cannot
    change ``max_matches`` mid-page - by design, a continuation has to resume the same
    query, not a mutated one. What a caller *can* vary between pages is the scan budget, so
    this drives the same recovery through a small ``max_scan_lines`` instead: the first
    page stops on the scan budget with plenty of cap headroom left (``max_matches=200``,
    well above the true count), and resuming with the identical selector but another scan
    budget keeps going until the source is genuinely exhausted.
    """
    config = make_config(
        tmp_path, tool_result_capture={"enabled": True, "host_ordering_verified_locally": True}
    )
    session = ShuntSession(
        "sess", config, make_capability(tool_result_capture=True),
        provider=UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    )
    body, true_count = _loki_shaped_source()
    pointer = _spilled_pointer(tmp_path, session, body)

    seen = 0
    request = _search_request(pointer, max_matches=200, max_scan_lines=1000)
    pages = 0
    while True:
        pages += 1
        assert pages <= 10, "should converge in a handful of pages, not loop indefinitely"
        env = session.inspect(request)
        extraction = env["extraction"]
        seen += extraction["matches_found"]
        if extraction["complete"]:
            break
        assert extraction["next_cursor"] is not None
        request = _search_request(
            pointer, max_matches=200, max_scan_lines=1000, cursor=extraction["next_cursor"]
        )

    assert pages > 1, "fixture must actually require more than one page to be a real proof"
    assert seen == true_count


def test_a_single_page_with_headroom_above_the_true_total_is_an_honest_exact_count(tmp_path):
    """When the requested cap is never actually reached, one page already proves the total."""
    config = make_config(
        tmp_path, tool_result_capture={"enabled": True, "host_ordering_verified_locally": True}
    )
    session = ShuntSession(
        "sess", config, make_capability(tool_result_capture=True),
        provider=UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    )
    body, true_count = _loki_shaped_source()
    pointer = _spilled_pointer(tmp_path, session, body)

    env = session.inspect(_search_request(pointer, max_matches=200))
    extraction = env["extraction"]
    assert extraction["matches_found"] == true_count
    assert extraction["complete"] is True
    assert extraction["next_cursor"] is None
    # The whole source really was scanned, not merely "the cap was never hit" by luck.
    assert extraction["lines_scanned"] == 3000
