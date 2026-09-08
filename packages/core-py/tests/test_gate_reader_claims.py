"""unit reader: the structured-claims contract.

The historical failure this replaces: a model produced a factually correct answer with a
mechanically valid ``citations`` entry, and the reader still erased it, because
``strip_unsupported_assertions`` requires every sentence to carry a hand-placed ``[cN]``
marker and the model had forgotten to write one. A 120-run Luna eval recorded this on 69 of
120 answerable runs. Under the claims contract there is no marker for the model to forget -
the reader places every one, mechanically, from a ``citation_ids`` list it structurally
validates. ``test_a_claim_with_no_hand_placed_marker_still_publishes`` is the test that
would have failed against the pre-fix reader; it is the direct regression proof.
"""

from __future__ import annotations

import json

import pytest

from context_shunt.provider import TransientProviderError
from context_shunt.reader import Reader
from context_shunt.snapshot import snapshot_bytes
from tests.support import FakeLuna, answer_json, claims_json, make_registry

pytestmark = pytest.mark.gate_reader

SOURCE = 'import os\nmax_retries = 3\nbackoff = "exponential"\ntimeout_seconds = 30\n'
QUESTION = "Where is the retry ceiling defined and what is it?"


def _fixture(tmp_path, *sources: str, session_id: str = "sess"):
    """One registry with one entry per source string, in request order."""
    registry = make_registry(tmp_path, session_id=session_id)
    entries = [registry.register(session_id, snapshot_bytes(s.encode())) for s in sources]
    return registry, entries


def _request(entries, question: str = QUESTION, **kw):
    base = {
        "schema_version": "1.0",
        "request_id": "req_claims",
        "operation": "read",
        "question": question,
        "sources": [
            {
                "source_id": e.source_id,
                "snapshot_id": e.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
            for e in entries
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    base.update(kw)
    return base


# -- the central regression -------------------------------------------------------------


def test_a_claim_with_no_hand_placed_marker_still_publishes(tmp_path):
    """The fix, proven directly: the model writes no ``[c1]`` anywhere, and the answer is
    still published with a marker the *reader* placed. Run this against the pre-fix reader
    (which only understood ``answer``+hand-placed markers) and it fails: a claims-shaped
    reply has no ``answer`` field for ``strip_unsupported_assertions`` to operate on at
    all, so it published nothing."""
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = claims_json(
        [{"text": "Retries stop after three attempts.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["status"] == "ok" and env["code"] == "ANSWERED"
    assert env["answer"] == "Retries stop after three attempts [c1]."
    assert [c["id"] for c in env["citations"]] == ["c1"]
    assert env["citations"][0]["verified"] is True


def test_multi_claim_multi_citation_renders_every_marker(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = claims_json(
        [
            {"text": "Retries stop after three attempts.", "citation_ids": ["c1"]},
            {
                "text": "The timeout backs off exponentially before that ceiling.",
                "citation_ids": ["c1", "c2"],
            },
        ],
        [
            {"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"},
            {"id": "c2", "line_start": 3, "line_end": 3, "quote": 'backoff = "exponential"'},
        ],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["code"] == "ANSWERED"
    assert env["answer"] == (
        "Retries stop after three attempts [c1]. "
        "The timeout backs off exponentially before that ceiling [c1][c2]."
    )
    assert {c["id"] for c in env["citations"]} == {"c1", "c2"}


# -- fail-closed on the claim, not the whole answer --------------------------------------


def test_unknown_citation_id_drops_only_that_claim(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = claims_json(
        [
            {"text": "Retries stop after three attempts.", "citation_ids": ["c1"]},
            {"text": "A fact with a citation id nobody declared.", "citation_ids": ["c9"]},
        ],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["code"] == "ANSWERED"
    assert env["answer"] == "Retries stop after three attempts [c1]."


def test_a_citation_id_that_fails_mechanical_verification_drops_the_claim(tmp_path):
    """Structurally well-formed, but the quote does not appear where claimed - fails
    closed at the second, verification stage rather than the first, structural one."""
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = claims_json(
        [{"text": "Timeout is thirty seconds.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "timeout_seconds = 30"}],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["code"] in ("NO_MATCH", "CITATION_INVALID")
    assert env.get("answer", "") == ""


def test_duplicate_citation_id_within_a_claim_drops_it(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = claims_json(
        [{"text": "Repeated evidence.", "citation_ids": ["c1", "c1"]}],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["code"] == "NO_MATCH"
    assert env["answer"] == ""


def test_a_claim_with_no_citations_never_survives(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = claims_json(
        [{"text": "An assertion nobody backed with evidence.", "citation_ids": []}], []
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["code"] == "NO_MATCH"
    assert env["answer"] == ""


# -- both shapes at once is ambiguous, never guessed at -----------------------------------


def test_both_claims_and_answer_in_one_reply_is_refused_and_can_recover(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    ambiguous = json.dumps(
        {
            "answer": "Three [c1].",
            "claims": [{"text": "Three.", "citation_ids": ["c1"]}],
            "citations": [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
        }
    )
    good = claims_json(
        [{"text": "Retries stop after three attempts.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    luna = FakeLuna(replies=[ambiguous, good])
    env = Reader(registry, luna).answer("sess", _request([entry])).envelope
    assert luna.call_count == 2
    assert env["code"] == "ANSWERED"


def test_both_claims_and_answer_exhausts_its_retry_and_fails_closed(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    ambiguous = json.dumps(
        {"answer": "x [c1].", "claims": [], "citations": [{"id": "c1", "quote": "x"}]}
    )
    luna = FakeLuna(replies=[ambiguous, ambiguous])
    env = Reader(registry, luna).answer("sess", _request([entry])).envelope
    assert luna.call_count == 2
    assert env["coverage"]["omitted"][0]["reason"] == "INVALID_MODEL_OUTPUT"


# -- chunk aggregation and namespacing -----------------------------------------------------


def test_claims_from_two_chunks_are_namespaced_into_disjoint_global_ids(tmp_path):
    """Both chunks reply with local id ``c1``; the aggregator must give each a distinct
    global id and remap every claim's citation_ids to match - not just the citations
    array, which is exactly the seam the old regex-based marker rewrite lived in.

    The two chunk calls run concurrently on real threads (``max_concurrent_model_calls``),
    so which one reaches the fake provider first is not deterministic. The reply is chosen
    from the excerpt each call actually received, not from call order, so the test does
    not depend on a race it cannot control.
    """
    registry, (first, second) = _fixture(tmp_path, "alpha config line\n", "beta config line\n")

    def reply_for(user: str) -> str:
        if "alpha" in user:
            return claims_json(
                [{"text": "The first source mentions alpha.", "citation_ids": ["c1"]}],
                [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha config line"}],
            )
        return claims_json(
            [{"text": "The second source mentions beta.", "citation_ids": ["c1"]}],
            [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "beta config line"}],
        )

    luna = FakeLuna(default_reply=reply_for)
    env = (
        Reader(registry, luna)
        .answer("sess", _request([first, second], question="What do the sources say?"))
        .envelope
    )
    assert env["code"] == "ANSWERED"
    ids = [c["id"] for c in env["citations"]]
    assert len(ids) == len(set(ids)) == 2
    assert "alpha" in env["answer"] and "beta" in env["answer"]
    for cid in ids:
        assert f"[{cid}]" in env["answer"]


# -- legacy compatibility: accepted only when already valid --------------------------------


def test_legacy_answer_with_a_valid_marker_is_still_accepted(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = answer_json(
        "The retry ceiling is three [c1].",
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["code"] == "ANSWERED"
    assert "[c1]" in env["answer"]


def test_legacy_answer_missing_its_marker_is_still_erased_not_rescued(tmp_path):
    """The old contract's rule is unchanged, deliberately: a legacy reply that omits the
    marker is not inferred or auto-rescued from its citations array. Only the claims
    contract removes the need for a marker in the first place."""
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = answer_json(
        "The retry ceiling is three.",
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["code"] == "NO_MATCH"
    assert env["answer"] == ""


# -- unsafe content is refused regardless of citation validity -----------------------------


def test_a_secret_in_claim_text_is_refused_even_with_a_valid_citation(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = claims_json(
        [
            {
                "text": "The API key is sk-ant-abcdef1234567890abcdef1234567890abcdef.",
                "citation_ids": ["c1"],
            }
        ],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    # `assert_no_secret` omits the offending chunk (pre-existing behaviour, unchanged by
    # the claims contract) rather than propagating a top-level error: what matters here is
    # that the secret is never published and no answer ships from that chunk.
    assert env["status"] != "ok"
    assert env.get("answer", "") == ""
    assert "sk-ant-" not in json.dumps(env)


def test_a_secret_in_a_structurally_dropped_claim_is_still_caught(tmp_path):
    """The claim is malformed (unknown citation id) and would be silently dropped by
    normalize_claims - but the secret scan runs over every raw claim before that filtering,
    so a secret cannot ride out on a claim discarded for an unrelated reason."""
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    reply = claims_json(
        [
            {
                "text": "The API key is sk-ant-abcdef1234567890abcdef1234567890abcdef.",
                "citation_ids": ["c9"],
            }
        ],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _request([entry])).envelope
    assert env["status"] != "ok"
    assert env.get("answer", "") == ""
    assert "sk-ant-" not in json.dumps(env)


# -- format retry -----------------------------------------------------------------------


def test_a_schema_failure_gets_exactly_one_format_retry(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    good = claims_json(
        [{"text": "Retries stop after three attempts.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    luna = FakeLuna(replies=["not json at all", good])
    env = Reader(registry, luna).answer("sess", _request([entry])).envelope
    assert luna.call_count == 2
    assert env["code"] == "ANSWERED"


def test_the_format_retry_is_exhausted_after_one_attempt(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    luna = FakeLuna(replies=["not json", "still not json", "would have worked"])
    env = Reader(registry, luna).answer("sess", _request([entry])).envelope
    assert luna.call_count == 2
    assert env["coverage"]["omitted"][0]["reason"] == "INVALID_MODEL_OUTPUT"


def test_format_retry_and_transient_retry_are_independently_budgeted(tmp_path):
    registry, (entry,) = _fixture(tmp_path, SOURCE)
    good = claims_json(
        [{"text": "Retries stop after three attempts.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    luna = FakeLuna(replies=[TransientProviderError("PROVIDER_CALL_FAILED"), "not json", good])
    env = Reader(registry, luna).answer("sess", _request([entry])).envelope
    assert luna.call_count == 3
    assert env["code"] == "ANSWERED"


def test_a_content_judgement_is_never_retried_as_a_format_failure(tmp_path):
    """BAD_USAGE is not in the format-retry allowlist: it is a provider accounting
    problem, not a claims/citations shape problem, so it must not spend the same call
    budget the format retry protects."""
    from context_shunt.provenance import TokenMethod, Usage
    from context_shunt.provider import ModelResponse, ProviderTarget

    class _BadUsageProvider:
        target = ProviderTarget(model="gpt-5.6-luna")
        calls = 0

        def complete(self, *, system, user, max_output_tokens, timeout_ms, **_kw):
            self.calls += 1
            return ModelResponse(
                text=claims_json([], []),
                requested=ProviderTarget(model="gpt-5.6-luna").identity(),
                usage=Usage(input_tokens=10**9, output_tokens=5, method=TokenMethod.EXACT),
            )

    registry, (entry,) = _fixture(tmp_path, SOURCE)
    provider = _BadUsageProvider()
    env = Reader(registry, provider).answer("sess", _request([entry])).envelope
    assert env["coverage"]["omitted"][0]["reason"] == "INVALID_MODEL_OUTPUT"
    # Not retryable at all: BAD_USAGE is neither a transient-provider failure nor an
    # allowlisted format failure, so it costs exactly the one call it made.
    assert provider.calls == 1


# -- prompt regression: literal identifiers/numbers/booleans are not paraphrased away ----


def test_prompt_instructs_verbatim_identifiers_numbers_and_booleans():
    """A live eval found the model paraphrasing hyphenated identifiers ("payments-team"
    -> "payments team") and boolean flags in claim text, missing a corpus's literal
    expected-fact check even though the answer was semantically correct and the citation
    verified. The prompt now asks for verbatim preservation of exactly those token
    classes; this pins the instruction so it cannot be silently dropped again."""
    from context_shunt.provider import READER_SYSTEM_PROMPT

    assert "exactly as they appear in the excerpt" in READER_SYSTEM_PROMPT
    assert "hyphenated or compound names" in READER_SYSTEM_PROMPT
    assert "boolean or yes/no values" in READER_SYSTEM_PROMPT
    # The marker rule this whole contract exists for must still be there too.
    assert "no citation marker such as" in READER_SYSTEM_PROMPT
