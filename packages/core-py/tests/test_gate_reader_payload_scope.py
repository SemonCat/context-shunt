"""unit reader: the provider payload is intent + bounded evidence, never the wider session.

The product requirement this audits (not a bug found in production, but the central claim
that must hold before any reader payload is trusted): Sol forms a bounded query naming a
question and one or more source handles; Luna receives only that question, the locator of
the chunk under evidence, and the chunk's bounded text - never the full parent conversation,
never a sibling source registered in the same session that the request did not select.

A negative test proves nothing if the excluded material was never reachable to begin with.
So this plants the sentinel where the reader genuinely *could* have picked it up if it
iterated the session's registry instead of the request's own ``sources`` list: a second
source registered under the identical session id, holding content the request never
references. ``FakeLuna.calls`` records the exact ``system``/``user`` strings sent to the
provider - the real request/response boundary this core has with a model - so asserting
against it is asserting against the actual payload, not an internal intermediate value.
"""

from __future__ import annotations

import pytest

from context_shunt.reader import Reader
from context_shunt.snapshot import snapshot_bytes
from tests.support import FakeLuna, answer_json, make_registry

pytestmark = pytest.mark.gate_reader

QUESTION = "What is the configured retry limit?"

# The source the request actually selects and expects evidence from.
SELECTED_SOURCE = (
    "service: checkout-api\n"
    "retry_limit: 7\n"
    "timeout_ms: 4000\n"
)

# A second source, registered in the same session, that the request never names. Its
# content is an unrelated sentinel - the kind of thing that would appear in a full parent
# conversation or an unrelated private context, never in a bounded evidence chunk.
SENTINEL = "PRIVATE-CONTEXT-SENTINEL-b6f1"
UNSELECTED_SOURCE = f"internal_note: {SENTINEL} do not disclose customer_id 9182\n"


def _fixture(tmp_path, reply):
    registry = make_registry(tmp_path, session_id="sess")
    selected = registry.register("sess", snapshot_bytes(SELECTED_SOURCE.encode()))
    # Registered in the identical session, never referenced by the request below.
    registry.register("sess", snapshot_bytes(UNSELECTED_SOURCE.encode()))
    luna = FakeLuna(replies=[reply])
    return registry, selected, luna, Reader(registry, luna)


def _request(entry):
    return {
        "schema_version": "1.3",
        "request_id": "req_payload_scope",
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


def test_the_provider_payload_excludes_an_unselected_sibling_source(tmp_path):
    reply = answer_json(
        "The retry limit is 7 [c1].",
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "retry_limit: 7"}],
    )
    _registry, entry, luna, reader = _fixture(tmp_path, reply)
    result = reader.answer("sess", _request(entry))

    assert result.envelope["code"] == "ANSWERED"
    assert luna.calls, "the fixture must actually call the provider, or this proves nothing"

    for call in luna.calls:
        # Not just the user turn - the fixed system prompt is likewise never a place a
        # sibling source or a private context could ride along, but a payload built by
        # string-concatenating the wrong thing would corrupt either.
        assert SENTINEL not in call.system
        assert SENTINEL not in call.user
        assert "9182" not in call.user

    # A payload that excluded everything would pass the assertions above vacuously. Prove
    # the selected evidence actually arrived, so absence of the sentinel is because it was
    # never in scope - not because nothing reached the provider at all.
    assert any("retry_limit: 7" in call.user for call in luna.calls)
    assert all(QUESTION in call.user for call in luna.calls)


def test_the_provider_payload_excludes_an_unselected_sibling_even_on_a_narrow_selector(tmp_path):
    """The same proof under a ``search`` selector, the narrower and more common request shape.

    A selector narrows *within* the named source; it must never be the only thing standing
    between the provider and every other handle in the session.
    """
    reply = answer_json(
        "The retry limit is 7 [c1].",
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "retry_limit: 7"}],
    )
    registry = make_registry(tmp_path, session_id="sess")
    selected = registry.register("sess", snapshot_bytes(SELECTED_SOURCE.encode()))
    registry.register("sess", snapshot_bytes(UNSELECTED_SOURCE.encode()))
    luna = FakeLuna(replies=[reply])
    reader = Reader(registry, luna)

    request = {
        "schema_version": "1.3",
        "request_id": "req_payload_scope_search",
        "operation": "read",
        "question": QUESTION,
        "sources": [
            {
                "source_id": selected.source_id,
                "snapshot_id": selected.snapshot.snapshot_id,
                "selector": {"kind": "search", "pattern": "retry_limit", "max_matches": 10},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    result = reader.answer("sess", request)

    assert result.envelope["code"] == "ANSWERED"
    assert luna.calls
    for call in luna.calls:
        assert SENTINEL not in call.system
        assert SENTINEL not in call.user
    assert any("retry_limit: 7" in call.user for call in luna.calls)
