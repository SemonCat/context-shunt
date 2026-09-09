"""unit reader: question propagation, Luna pinning, coverage and safe failure."""

from __future__ import annotations

import json

import pytest

from context_shunt.binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from context_shunt.errors import ShuntError
from context_shunt.limits import DEFAULT_LIMITS, READER_MODEL
from context_shunt.provenance import (
    Attribution,
    AttributionPolicy,
    ModelIdentity,
    Provenance,
    TokenMethod,
    Usage,
)
from context_shunt.provider import (
    FallbackChainProvider,
    HostBridgeProvider,
    ModelResponse,
    ProviderTarget,
    UnavailableProvider,
)
from context_shunt.reader import Reader
from context_shunt.snapshot import snapshot_bytes
from tests.support import FakeLuna, answer_json, make_registry

pytestmark = pytest.mark.gate_reader

SOURCE = 'import os\nmax_retries = 3\nbackoff = "exponential"\ntimeout_seconds = 30\n'
QUESTION = "Where is the retry ceiling defined and what is it?"


def _fixture(tmp_path, reply=None, *, content: str = SOURCE, media=TEXT_MEDIA_TYPE):
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(content.encode(), media_type_hint=media))
    luna = FakeLuna(replies=[reply] if reply is not None else [])
    return registry, entry, luna, Reader(registry, luna)


def _request(entry, selector=None, **kw):
    base = {
        "schema_version": "1.0",
        "request_id": "req_r1",
        "operation": "read",
        "question": QUESTION,
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": selector or {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    base.update(kw)
    return base


def test_missing_question_makes_zero_model_calls(tmp_path):
    registry, entry, luna, reader = _fixture(tmp_path)
    request = _request(entry)
    del request["question"]
    env = reader.answer("sess", request).envelope
    assert luna.call_count == 0
    assert env["status"] == "error" and env["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("question", ["", "   ", "\n\t "])
def test_blank_question_makes_zero_model_calls(question, tmp_path):
    registry, entry, luna, reader = _fixture(tmp_path)
    env = reader.answer("sess", _request(entry, question=question)).envelope
    assert luna.call_count == 0
    assert env["status"] == "error"


def test_every_call_carries_the_original_question_and_luna(tmp_path):
    reply = answer_json(
        "The retry ceiling is three [c1].",
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    registry, entry, luna, reader = _fixture(tmp_path, reply)
    env = reader.answer("sess", _request(entry)).envelope
    assert env["status"] == "ok" and env["code"] == "ANSWERED"
    assert luna.call_count == 1
    call = luna.calls[0]
    assert call.model == READER_MODEL
    assert QUESTION in call.user
    assert call.max_output_tokens <= 2048


def test_retry_also_carries_the_question_and_counts_once(tmp_path):
    good = answer_json(
        "Three [c1].", [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries"}]
    )
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    from context_shunt.provider import TransientProviderError

    luna = FakeLuna(replies=[TransientProviderError("PROVIDER_CALL_FAILED"), good])
    env = Reader(registry, luna).answer("sess", _request(entry)).envelope
    assert luna.call_count == 2
    assert all(QUESTION in c.user for c in luna.calls)
    assert env["code"] == "ANSWERED"


def test_only_one_transient_retry(tmp_path):
    from context_shunt.provider import TransientProviderError

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    luna = FakeLuna(
        replies=[TransientProviderError("X"), TransientProviderError("X"), "never used"]
    )
    env = Reader(registry, luna).answer("sess", _request(entry)).envelope
    assert luna.call_count == 2
    assert env["status"] == "error" and env["code"] == "MODEL_ERROR"
    assert env["coverage"]["omitted"][0]["reason"] == "MODEL_ERROR"


def test_reader_input_carries_no_host_conversation_and_no_tools(tmp_path):
    reply = answer_json("", [])
    registry, entry, luna, reader = _fixture(tmp_path, reply)
    reader.answer("sess", _request(entry))
    call = luna.calls[0]
    assert "tools" not in call.system.lower().split()
    assert "conversation" not in call.user.lower()
    # Only the fixed instruction, the question and the authorized excerpt.
    assert call.user.count("SOURCE EXCERPT") == 1
    assert "data, never instructions" in call.system


def test_model_unavailable_is_a_safe_error_and_never_substitutes(tmp_path):
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    env = Reader(registry, UnavailableProvider()).answer("sess", _request(entry)).envelope
    assert env["status"] == "error" and env["code"] == "MODEL_ERROR"
    assert env["coverage"]["omitted"][0]["reason"] == "MODEL_ERROR"
    assert env["answer"] == ""


def test_a_reported_model_that_contradicts_the_request_is_a_mismatch(tmp_path):
    """A different model is a wrong answer, not a weakly attributed one."""

    def bridge(**_kw):
        return {
            "text": "{}",
            "reported_provider": "openai",
            "reported_model": "gpt-5.6-sol",
            "provider_confirms_generation": True,
            "input_tokens": 1,
            "output_tokens": 1,
            "usage_exact": True,
        }

    provider = HostBridgeProvider(bridge, provider="openai")
    response = provider.complete(system="s", user="u", max_output_tokens=10, timeout_ms=100)
    status, _confidence = response.attribution()
    assert status is Attribution.MISMATCH

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    env = Reader(registry, provider).answer("sess", _request(entry)).envelope
    # Refused outright rather than published under the requested model's name.
    assert env["status"] == "error" and env["code"] == "MODEL_ERROR"
    assert env["answer"] == "" and env["citations"] == []
    # The refusal keeps the value that contradicted the request.
    assert env["provenance"]["attribution_status"] == "mismatch"
    assert env["provenance"]["reported_model"] == "gpt-5.6-sol"
    assert env["provenance"]["requested_model"] == READER_MODEL
    assert env["recovery"]["handles_valid"] is True


def test_an_unprovable_attribution_is_labelled_unverified_not_actual(tmp_path):
    """The host echoed the request back; that is not a provider confirmation."""

    def bridge(**_kw):
        return {
            "text": answer_json(
                "The retry ceiling is three [c1].",
                [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
            ),
            "reported_provider": "openai",
            "reported_model": READER_MODEL,
            "provider_confirms_generation": False,
            "usage_exact": False,
        }

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    provider = HostBridgeProvider(bridge, provider="openai")
    result = Reader(registry, provider).answer("sess", _request(entry))
    env = result.envelope
    assert env["code"] == "ANSWERED"
    assert env["provenance"]["attribution_status"] == "unverified"
    assert env["provenance"]["requested_model"] == READER_MODEL
    assert env["provenance"]["reported_model"] == READER_MODEL
    assert env["provenance"]["usage_complete"] is False
    # Absent provider usage becomes a named estimate, never a zero.
    assert result.cost.method is TokenMethod.BYTES_DIV_4
    assert result.cost.input_tokens is not None and result.cost.input_tokens > 0


def test_require_match_policy_refuses_an_unverified_attribution(tmp_path):
    def bridge(**_kw):
        return {"text": answer_json("", []), "provider_confirms_generation": False}

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    reader = Reader(
        registry,
        HostBridgeProvider(bridge, provider="openai"),
        attribution_policy=AttributionPolicy.REQUIRE_MATCH,
    )
    env = reader.answer("sess", _request(entry)).envelope
    assert env["code"] == "PROVENANCE_UNAVAILABLE"
    # A provenance failure is not a handle failure: recovery keeps the snapshot.
    assert env["recovery"]["handles_valid"] is True
    assert "CONFIGURE_READER_MODEL" in env["recovery"]["actions"]


def test_no_match_is_ok_only_for_the_range_actually_searched(tmp_path):
    registry, entry, luna, reader = _fixture(tmp_path, answer_json("", []))
    env = reader.answer("sess", _request(entry, {"kind": "lines", "start": 1, "end": 2})).envelope
    assert env["status"] == "ok" and env["code"] == "NO_MATCH"
    assert env["coverage"]["complete"] is True
    assert env["coverage"]["processed_chunks"] == env["coverage"]["planned_chunks"] == 1


def test_search_with_no_hits_is_no_match_without_a_model_call(tmp_path):
    registry, entry, luna, reader = _fixture(tmp_path)
    env = reader.answer(
        "sess", _request(entry, {"kind": "search", "pattern": "nonexistent", "max_matches": 5})
    ).envelope
    assert luna.call_count == 0
    assert env["code"] == "NO_MATCH" and env["status"] == "ok"


def test_partial_when_a_chunk_is_omitted_by_budget(tmp_path):
    body = "".join(f"line {i} value\n" for i in range(1, 5000))
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(body.encode()))
    luna = FakeLuna(default_reply=answer_json("", []))
    env = (
        Reader(registry, luna)
        .answer(
            "sess",
            _request(
                entry, budgets={"max_chunks": 1, "max_answer_bytes": 8192, "deadline_ms": 60000}
            ),
        )
        .envelope
    )
    assert env["status"] == "partial"
    assert env["coverage"]["complete"] is False
    assert any(o["reason"] == "BUDGET_EXCEEDED" for o in env["coverage"]["omitted"])


def test_invalid_model_output_gets_one_format_retry_then_fails_closed_and_leaks_nothing(
    tmp_path,
):
    """Malformed JSON is a schema failure: eligible for exactly one format retry, drawn
    from its own budget - separate from, and independent of, the transient-provider retry
    budget (the two may both fire for the same chunk; see
    ``test_the_format_and_transient_retry_budgets_are_independent_and_may_both_fire``
    below). Once that one format retry is also malformed, the chunk fails closed and
    nothing it said crosses the boundary."""
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    luna = FakeLuna(replies=["this is not json at all", "still not json, still not json"])
    env = Reader(registry, luna).answer("sess", _request(entry)).envelope
    assert luna.call_count == 2
    assert env["coverage"]["omitted"][0]["reason"] == "INVALID_MODEL_OUTPUT"
    assert "not json" not in json.dumps(env)


def test_invalid_model_output_recovers_on_its_one_format_retry(tmp_path):
    """The one-shot format retry is a real recovery path, not just a second failure."""
    good = answer_json(
        "Three [c1].", [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}]
    )
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    luna = FakeLuna(replies=["this is not json at all", good])
    env = Reader(registry, luna).answer("sess", _request(entry)).envelope
    assert luna.call_count == 2
    assert env["status"] == "ok" and env["code"] == "ANSWERED"


def test_the_format_and_transient_retry_budgets_are_independent_and_may_both_fire(tmp_path):
    """A transient provider failure and a schema failure on the same chunk each draw from
    their own budget, and both budgets may be spent on the same chunk: one call, one
    transient retry, one format retry - three calls total, the sum of the two limits, not
    a single shared retry slot. 'Independent' means neither budget can steal the other's
    slot, not that only one of them may ever fire."""
    from context_shunt.provider import TransientProviderError

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    good = answer_json(
        "Three [c1].", [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}]
    )
    luna = FakeLuna(replies=[TransientProviderError("PROVIDER_CALL_FAILED"), "not json", good])
    env = Reader(registry, luna).answer("sess", _request(entry)).envelope
    assert luna.call_count == 3
    assert env["status"] == "ok" and env["code"] == "ANSWERED"


def test_snapshot_mismatch_is_source_changed(tmp_path):
    registry, entry, luna, reader = _fixture(tmp_path)
    request = _request(entry)
    request["sources"][0]["snapshot_id"] = "sha256:" + "0" * 64
    env = reader.answer("sess", request).envelope
    assert luna.call_count == 0
    assert env["code"] == "SOURCE_CHANGED"


def test_json_source_answers_with_record_citations(tmp_path):
    doc = json.dumps({"items": [{"name": "alpha", "retries": 1}, {"name": "beta", "retries": 3}]})
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(doc.encode(), media_type_hint=JSON_MEDIA_TYPE))
    reply = answer_json(
        "beta retries three times [c1].",
        [{"id": "c1", "record_start": 2, "record_end": 2, "quote": '"name":"beta"'}],
    )
    luna = FakeLuna(replies=[reply])
    env = (
        Reader(registry, luna)
        .answer(
            "sess", _request(entry, {"kind": "records", "pointer": "/items", "start": 2, "end": 2})
        )
        .envelope
    )
    assert env["code"] == "ANSWERED"
    assert env["citations"][0]["locator"]["kind"] == "records"


# -- attribution: what counts as the same model, and what proves ACTUAL -------


@pytest.mark.parametrize(
    "observed,agrees",
    [
        ("gpt-5.6-luna", True),
        ("openai/gpt-5.6-luna", True),
        ("GPT-5.6-Luna", True),
        ("gpt-5.6-luna-2026-05-01", True),  # provider stamped a build date
        ("gpt-5.6-luna-2", True),  # numbered revision
        ("gpt-5.6-luna-evil", False),  # a different model wearing the prefix
        ("gpt-5.6-luna-uncensored", False),
        ("gpt-5.6-lunatic", False),
        ("gpt-5.6-sol", False),
    ],
)
def test_only_dated_or_numbered_decoration_counts_as_the_same_model(observed, agrees):
    """A prefix match is not an identity match.

    Decoration was accepted as any `requested + "-" + anything`, so `gpt-5.6-luna-evil`
    was classified as the requested model and could be published as such. Only a date or
    a numeric revision is decoration; an alphabetic suffix names a *different* model
    (`gpt-4` and `gpt-4-turbo` are not the same model either).
    """
    from context_shunt.provenance import _model_agrees

    assert _model_agrees("gpt-5.6-luna", observed) is agrees


def test_a_prefix_extension_is_a_mismatch_end_to_end(tmp_path):
    """The rename must be refused by the reader, not merely scored differently."""

    def bridge(**_kw):
        return {
            "text": "{}",
            "reported_provider": "openai",
            "reported_model": "gpt-5.6-luna-evil",
            "provider_confirms_generation": True,
            "input_tokens": 1,
            "output_tokens": 1,
            "usage_exact": True,
        }

    provider = HostBridgeProvider(bridge, provider="openai")
    status, _confidence = provider.complete(
        system="s", user="u", max_output_tokens=10, timeout_ms=100
    ).attribution()
    assert status is Attribution.MISMATCH

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    env = Reader(registry, provider).answer("sess", _request(entry)).envelope
    assert env["status"] == "error" and env["code"] == "MODEL_ERROR"
    assert env["provenance"]["reported_model"] == "gpt-5.6-luna-evil"


def test_actual_needs_a_reported_model_not_merely_a_reported_provider():
    """`ACTUAL` is a claim about which *model* generated the tokens.

    `known` is true when either half of the identity is set, so a bridge that confirmed
    generation while naming only a provider was classified `actual` with no model
    identity behind it. That is the strongest label the envelope has, so it needs the
    model actually named.
    """
    from context_shunt.provenance import Attribution, Confidence, ModelIdentity, classify

    requested = ModelIdentity(provider="openai", model="gpt-5.6-luna")

    status, confidence = classify(
        requested=requested,
        resolved=ModelIdentity(),
        reported=ModelIdentity(provider="openai"),  # provider only
        provider_confirms_generation=True,
    )
    assert status is not Attribution.ACTUAL

    # Naming the model is what earns the strongest label.
    status, confidence = classify(
        requested=requested,
        resolved=ModelIdentity(),
        reported=ModelIdentity(provider="openai", model="gpt-5.6-luna"),
        provider_confirms_generation=True,
    )
    assert status is Attribution.ACTUAL and confidence is Confidence.HIGH


# -- the exported Reader API stayed usable across the 1.1 revision -----------


def test_the_pre_1_1_reader_api_still_works(tmp_path):
    """`answer` used to return the envelope dict; 1.1 changed it without an overload.

    Every `answer(...)["status"]` call site broke on the revision - the eval gate's own
    body was one of them. `ReaderResult` subscripts like the envelope again, and
    `answer_envelope` is the explicit form for callers that only wanted the envelope.
    """
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    luna = FakeLuna(default_reply=answer_json("mode = fast [c1]", [(1, 1, "mode = fast")]))
    result = Reader(registry, luna).answer("sess", _request(entry))

    # The pre-1.1 shape: subscript straight into the envelope.
    assert result["status"] == result.envelope["status"]
    assert result["code"] == result.envelope["code"]
    assert "citations" in result
    assert set(result.keys()) == set(result.envelope)
    assert dict(result) == result.envelope

    # Bracket means envelope, attribute means record - they never collide.
    assert result["provenance"] == result.envelope["provenance"]
    assert result.provenance is not result.envelope["provenance"]
    assert isinstance(result.provenance, Provenance)

    # And the explicit overload returns exactly the envelope.
    envelope = Reader(registry, luna).answer_envelope("sess", _request(entry))
    assert isinstance(envelope, dict) and envelope["status"] == result["status"]


# -- the availability fallback chain, actually wired and actually bounded -----


class _Recording:
    """A provider that fails or answers on demand and records its own budget."""

    def __init__(self, model: str, outcome, cost_ms: int = 0, clock=None):
        self.model = model
        self.outcome = outcome
        self.cost_ms = cost_ms
        self.clock = clock
        self.timeouts: list[int] = []
        self.calls = 0

    @property
    def target(self) -> ProviderTarget:
        return ProviderTarget(model=self.model, provider="openai")

    def complete(self, *, system, user, max_output_tokens, timeout_ms):
        self.calls += 1
        self.timeouts.append(timeout_ms)
        if self.clock is not None and self.cost_ms:
            self.clock.advance(self.cost_ms)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return ModelResponse(
            text=self.outcome,
            requested=ProviderTarget(model=self.model, provider="openai").identity(),
            resolved=ModelIdentity(provider="openai", model=self.model),
            usage=Usage(input_tokens=3, output_tokens=2, method=TokenMethod.EXACT),
        )


def test_no_fallback_attempt_starts_once_the_budget_is_gone():
    """Each attempt got the *whole* per-call budget, so a chain could run N times over.

    The chain only sees `timeout_ms`, not the request deadline, so it has to police the
    budget itself. Without that, three providers each got the full allowance and a single
    reader call could outlive the request deadline several times over - and a fallback
    could still be *started* after the caller had already been handed TIMEOUT.
    """
    unavailable = ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)
    first = _Recording("m1", unavailable)
    second = _Recording("m2", unavailable)
    third = _Recording("m3", "{}")
    chain = FallbackChainProvider(first, [second, third])

    with pytest.raises(ShuntError):
        chain.complete(system="s", user="u", max_output_tokens=10, timeout_ms=0)
    assert first.calls == 0, "a chain with no budget must not call anyone"

    # With a budget, each attempt gets what is *left*, never the full allowance again.
    first = _Recording("m1", unavailable)
    second = _Recording("m2", "{}")
    chain = FallbackChainProvider(first, [second])
    chain.complete(system="s", user="u", max_output_tokens=10, timeout_ms=1000)
    assert first.timeouts[0] <= 1000
    assert second.timeouts[0] <= first.timeouts[0]


def test_the_chain_reports_every_attempt_it_started():
    """A fallback that took three tries billed three calls; the envelope said one."""
    unavailable = ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)
    chain = FallbackChainProvider(
        _Recording("m1", unavailable), [_Recording("m2", unavailable), _Recording("m3", "{}")]
    )
    response = chain.complete(system="s", user="u", max_output_tokens=10, timeout_ms=5000)
    assert response.fallback_used is True
    assert response.attempts == 3


def test_a_reader_result_still_serializes_like_the_envelope_it_replaced(tmp_path):
    """`Mapping` restored subscripting, but not everything a dict was used for.

    `Reader.answer` used to return the envelope dict, so call sites passed it straight to
    `json.dumps` or to anything expecting a real `dict`. A `Mapping` is not JSON
    serializable and is not a `dict`, so those call sites still broke - the compatibility
    was partial.
    """
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    luna = FakeLuna(default_reply=answer_json("mode = fast [c1]", [(1, 1, "mode = fast")]))
    result = Reader(registry, luna).answer("sess", _request(entry))

    # The shape a pre-1.1 caller would have serialized.
    assert json.loads(json.dumps(result)) == result.envelope
    assert dict(result) == result.envelope
    # And it still behaves as the record it is.
    assert result.envelope["status"] == result["status"]


def test_a_decorated_model_id_is_not_certified_as_the_same_model():
    """No alias contract says `luna` and `luna-2` are the same model.

    Decoration was allowed to establish the *strongest* label, so a provider that answered
    with `luna-2` was certified `actual/high` against a request for `luna`. A provider is
    free to use numeric names for genuinely different models, so this could falsely certify
    model-specific routing. It is equally wrong to call it a contradiction - a build date
    really is the same model - so a decorated match is neither `actual` nor `mismatch`: it
    is the weaker truthful label.
    """
    from context_shunt.provenance import Attribution, ModelIdentity, classify

    requested = ModelIdentity(provider="openai", model="gpt-5.6-luna")

    def status(observed: str) -> Attribution:
        return classify(
            requested=requested,
            resolved=ModelIdentity(),
            reported=ModelIdentity(provider="openai", model=observed),
            provider_confirms_generation=True,
        )[0]

    # Exactly the requested model, however it is decorated by a namespace or case.
    assert status("gpt-5.6-luna") is Attribution.ACTUAL
    assert status("openai/gpt-5.6-luna") is Attribution.ACTUAL
    assert status("GPT-5.6-Luna") is Attribution.ACTUAL

    # Plausibly the same model, but nothing establishes it. Not certified, not contradicted.
    for decorated in ("gpt-5.6-luna-2", "gpt-5.6-luna-2026-05-01"):
        assert status(decorated) is not Attribution.ACTUAL, decorated
        assert status(decorated) is not Attribution.MISMATCH, decorated

    # A different model is still a contradiction.
    assert status("gpt-5.6-luna-evil") is Attribution.MISMATCH
    assert status("gpt-5.6-sol") is Attribution.MISMATCH


def test_a_partial_fallback_usage_is_not_labelled_exact():
    """Exact means every started attempt reported, not just the one that won.

    A chain that failed once and then succeeded merged the winner's exact usage with an
    empty accumulator, so the cost came back `exact` while only one of two billed attempts
    had reported anything. A partial sum presented as exact understates real spend with
    the strongest possible label on it.
    """
    from context_shunt.reader import _reader_cost

    unreported = Usage(method=TokenMethod.NOT_APPLICABLE)
    exact_one = Usage(input_tokens=4, output_tokens=3, method=TokenMethod.EXACT)

    complete = _reader_cost(
        exact_one,
        attempts=1,
        usage_complete=1,
        prompt_bytes=40,
        completion_bytes=8,
        limits=DEFAULT_LIMITS,
    )
    assert complete.method is TokenMethod.EXACT

    partial = _reader_cost(
        unreported.merge(exact_one),
        attempts=2,
        usage_complete=1,
        prompt_bytes=40,
        completion_bytes=8,
        limits=DEFAULT_LIMITS,
    )
    assert partial.method is not TokenMethod.EXACT
    assert partial.attempts_started == 2 and partial.attempts_usage_complete == 1


def test_a_chain_that_fails_everywhere_still_reports_every_attempt(tmp_path):
    """Attempts were attached to a successful response only.

    The chain counts its internal attempts and puts the total on the `ModelResponse` it
    returns - so when *every* candidate fails it rethrows the last error and the count
    goes with it. The reader then counts one outer invocation per retry and nothing else,
    so a two-provider chain under the reader's one retry made four real provider calls and
    reported two. Every one of those calls reached a provider and was billed.
    """
    from context_shunt.provider import FallbackChainProvider

    calls = {"n": 0}

    def dead(**_kwargs):
        calls["n"] += 1
        raise RuntimeError("upstream unavailable")

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    chain = FallbackChainProvider(HostBridgeProvider(dead), [HostBridgeProvider(dead)])
    result = Reader(registry, chain).answer("sess", _request(entry))

    assert calls["n"] > 0
    assert result.cost.attempts_started == calls["n"], (
        f"{calls['n']} provider calls happened, {result.cost.attempts_started} reported"
    )
    # Nothing reported usage, so nothing may claim to have measured it.
    assert result.cost.attempts_usage_complete == 0
    assert result.cost.method is not TokenMethod.EXACT
