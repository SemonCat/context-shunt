"""unit reader: literal-OR search (``patterns``) versus the frozen literal ``pattern``.

The historical failure this replaces: a real caller wanted "any of several exact business
rule phrases" and sent them joined with ``|`` inside the single ``pattern`` field, expecting
alternation. ``_search_selector_to_lines`` has always done a plain ``pattern in line``
substring check - by design, so a hostile pattern can never make matching backtrack - so the
four-clause string was never a substring of any line, the selector resolved to an empty line
range, and the whole read returned ``NO_MATCH`` with zero reader calls: a real, dispositive
answer was in the source and the caller never learned that.

``test_a_pattern_containing_pipe_is_never_split`` is the regression proof that the fix does
not "smarten" ``pattern`` into a parser - that would silently change the meaning of any
existing literal pattern that legitimately contains a ``|`` character. ``patterns`` (1.3) is
the new, explicit, additive field: a list of literal strings, OR-combined. The two tests
right after it are the direct proof that ``patterns`` recovers exactly the case ``pattern``
could not.
"""

from __future__ import annotations

import pytest

from context_shunt.reader import Reader
from context_shunt.snapshot import snapshot_bytes
from tests.support import FakeLuna, answer_json, make_registry

pytestmark = pytest.mark.gate_reader

QUESTION = "Does the auto-suspend policy apply to this account?"

# Synthetic, sanitized stand-in for the real production source: several differently-worded
# trigger phrases for the same business rule, none of which is a substring of the others -
# structurally identical to the real bug's shape, content invented.
SOURCE = (
    "account_tier: standard\n"
    "billing_cycle: monthly\n"
    "policy_note: escalation applies after third monday delinquency\n"
    "reviewed_by: ops-rotation\n"
)


def _fixture(tmp_path, reply=None):
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    luna = FakeLuna(replies=[reply] if reply is not None else [])
    return registry, entry, luna, Reader(registry, luna)


def _request(entry, selector, **kw):
    base = {
        "schema_version": "1.3",
        "request_id": "req_pat",
        "operation": "read",
        "question": QUESTION,
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": selector,
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    base.update(kw)
    return base


def test_a_pattern_containing_pipe_is_never_split(tmp_path):
    """The exact real-world mistake: joining alternatives with ``|`` inside ``pattern``.

    None of "escalation applies after third monday delinquency|third Monday|auto suspend"
    is a substring of any line, so this must resolve to NO_MATCH with zero model calls -
    proving the fix does not retroactively teach ``pattern`` to parse ``|``.
    """
    registry, entry, luna, reader = _fixture(tmp_path)
    selector = {
        "kind": "search",
        "pattern": "escalation applies after third monday delinquency|third Monday|auto suspend",
        "max_matches": 5,
    }
    env = reader.answer("sess", _request(entry, selector)).envelope
    assert luna.call_count == 0
    assert env["code"] == "NO_MATCH" and env["status"] == "ok"
    assert env["coverage"]["complete"] is True


def test_patterns_ors_several_literals_and_finds_the_real_hit(tmp_path):
    """The fix: the same intent expressed as ``patterns`` finds the line and reads it."""
    reply = answer_json(
        "Yes, escalation applies after a third-Monday delinquency [c1].",
        [
            {
                "id": "c1",
                "line_start": 3,
                "line_end": 3,
                "quote": "policy_note: escalation applies after third monday delinquency",
            }
        ],
    )
    registry, entry, luna, reader = _fixture(tmp_path, reply)
    selector = {
        "kind": "search",
        "patterns": [
            "escalation applies after third monday delinquency",
            "third Monday",
            "auto suspend",
        ],
        "max_matches": 5,
    }
    env = reader.answer("sess", _request(entry, selector)).envelope
    assert luna.call_count == 1
    assert env["code"] == "ANSWERED" and env["status"] == "ok"
    assert env["citations"][0]["quote"] == (
        "policy_note: escalation applies after third monday delinquency"
    )


def test_patterns_matches_if_any_single_literal_is_present(tmp_path):
    """A ``patterns`` list ORs, it does not require every phrase on the same line."""
    reply = answer_json(
        "ok [c1].",
        [{"id": "c1", "line_start": 4, "line_end": 4, "quote": "reviewed_by: ops-rotation"}],
    )
    registry, entry, luna, reader = _fixture(tmp_path, reply)
    selector = {
        "kind": "search",
        "patterns": ["reviewed_by: ops-rotation", "this phrase is absent from the source"],
        "max_matches": 5,
    }
    env = reader.answer("sess", _request(entry, selector)).envelope
    assert luna.call_count == 1
    assert env["code"] == "ANSWERED"


def test_patterns_with_no_hit_is_still_a_zero_call_no_match(tmp_path):
    registry, entry, luna, reader = _fixture(tmp_path)
    selector = {
        "kind": "search",
        "patterns": ["nowhere in the source", "also nowhere"],
        "max_matches": 5,
    }
    env = reader.answer("sess", _request(entry, selector)).envelope
    assert luna.call_count == 0
    assert env["code"] == "NO_MATCH" and env["status"] == "ok"
