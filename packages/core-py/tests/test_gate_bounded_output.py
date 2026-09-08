"""unit bounded-output: every reply and every spill decision respects the caps."""

from __future__ import annotations

import pytest

from context_shunt import envelope as E
from context_shunt.errors import ShuntError
from context_shunt.guard import OutputGuardError, enforce, enforce_or_fixed
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.provenance import ResultKind
from context_shunt.provider import FallbackChainProvider
from context_shunt.reader import Reader
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes
from context_shunt.spill import SpillEngine
from context_shunt.store import SnapshotStore
from tests.support import (
    FakeLuna,
    answer_json,
    derived_provenance,
    make_identity,
    make_registry,
    transient,
)

pytestmark = pytest.mark.gate_bounded_output
L = DEFAULT_LIMITS


def _engine(tmp_path, *, enabled=True, limits=L):
    """A spill engine over a real store. The engine has no provider reference at all."""
    store = SnapshotStore(tmp_path / "cache", limits)
    identity = make_identity("sess")
    store.open_scope(identity)
    registry = SourceRegistry(store, identity, limits)
    return SpillEngine(registry, limits=limits, enabled=enabled), registry, store


def _build_result(spec, cases_doc=None):
    kind = spec["kind"]
    if kind == "string":
        if "repeat" in spec:
            return spec["repeat"]["unit"] * spec["repeat"]["bytes"]
        return spec["value"]
    if kind == "json":
        if "generate" in spec:
            return _generate(spec["generate"])
        value = spec["value"]
        if spec.get("pad_bytes"):
            value = {**value, "_pad": "p" * spec["pad_bytes"]}
        return value
    if kind == "blocks":
        blocks = []
        for block in spec["blocks"]:
            entry = {"type": block["type"]}
            if "media_type" in block:
                entry["media_type"] = block["media_type"]
            entry["text" if block["type"] == "text" else "data"] = (
                block["repeat"]["unit"] * block["repeat"]["bytes"]
                if "repeat" in block
                else block["value"]
            )
            blocks.append(entry)
        return {"content": blocks}
    if kind == "internal_envelope":
        return {"marker": spec["repeat"]["unit"] * spec["repeat"]["bytes"]}
    raise AssertionError(f"unknown result kind {kind}")


def _generate(spec):
    shape = spec["shape"]
    if shape == "object":
        return {f"k{i}": f"value-{i}" for i in range(spec["entries"])}
    if shape == "array":
        return [f"value-{i}" for i in range(spec["entries"])]
    if shape == "nested":
        return {"rows": [{"id": i, "tags": ["a", "b"]} for i in range(spec["entries"])]}
    if shape == "deep":
        node = "leaf"
        for _ in range(spec["depth"] - 1):
            node = {"n": node}
        return node
    if shape == "cycle":
        node = {}
        node["self"] = node
        return node
    if shape == "unserializable":
        return {"when": object()}
    raise AssertionError(shape)


def test_every_spill_conformance_case(tmp_path, spill_cases):
    failures = []
    for case in spill_cases["cases"]:
        limits = (
            L.narrow(store_max_bytes=case["store_quota_bytes"])
            if "store_quota_bytes" in case
            else L
        )
        engine, registry, store = _engine(tmp_path / case["id"], limits=limits)
        result = _build_result(case["result"])
        internal_id = None
        if case["result"]["kind"] == "internal_envelope":
            entry = registry.register("sess", snapshot_bytes(b"internal"), internal=True)
            internal_id = entry.source_id
        if case.get("inject") == "publish_write_failure":
            store.publish = _raise(ShuntError("STORE_FAILED", "WRITE_FAILED", retryable=False))
        if case.get("inject") == "publish_content_mismatch":
            store.publish = _raise(
                ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", retryable=False)
            )
        outcome = engine.evaluate("sess", "req_s", result, internal_source_id=internal_id)
        want = case["expect"]
        if outcome.action != want["action"] or (want.get("code") and outcome.code != want["code"]):
            failures.append((case["id"], want, outcome.action, outcome.code))
    assert not failures, failures


def _raise(exc):
    def _fn(*_a, **_kw):
        raise exc

    return _fn


def test_spill_makes_no_model_calls(tmp_path, spill_cases):
    engine, registry, _ = _engine(tmp_path)
    assert not hasattr(engine, "_provider")
    outcome = engine.evaluate("sess", "req_s", "x" * 40000)
    assert outcome.action == "spill"
    assert outcome.envelope["answer"] == ""
    assert outcome.envelope["citations"] == []


def test_spill_envelope_is_under_the_cap_regardless_of_payload_size(tmp_path):
    engine, _, _ = _engine(tmp_path)
    outcome = engine.evaluate("sess", "req_s", "y" * 4_000_000)
    env = enforce(outcome.envelope)
    assert E.serialized_bytes(env) <= L.max_envelope_bytes
    assert env["pointer"]["bytes"] == 4_000_000


def test_string_dict_and_list_all_spill_without_a_heuristic_summary(tmp_path):
    engine, _, _ = _engine(tmp_path)
    for payload in ("s" * 40000, {"k": "v" * 40000}, ["item" * 4000 for _ in range(4)]):
        outcome = engine.evaluate("sess", "req_s", payload)
        assert outcome.action == "spill"
        assert outcome.envelope["answer"] == ""


def test_answer_quote_and_citation_caps_are_enforced():
    base = E.build(
        request_id="req_1",
        status="ok",
        code="ANSWERED",
        answer="a [c1]",
        citations=[_citation()],
        coverage=E.Coverage(
            complete=True, processed_chunks=1, planned_chunks=1, upstream_truncated=False
        ),
        provenance=derived_provenance(),
        accounting_id="acc_" + "0" * 15 + "1",
    )
    enforce(base)
    with pytest.raises(OutputGuardError):
        enforce({**base, "answer": "x" * (L.max_answer_bytes + 1)})
    with pytest.raises(OutputGuardError):
        enforce({**base, "citations": [{**_citation(), "quote": "q" * (L.max_quote_bytes + 1)}]})
    with pytest.raises(OutputGuardError):
        enforce({**base, "citations": [_citation() for _ in range(L.max_citations + 1)]})


def test_unverified_citation_never_leaves_the_guard():
    env = E.build(
        request_id="req_1",
        status="ok",
        code="ANSWERED",
        answer="a [c1]",
        citations=[{**_citation(), "verified": False}],
        coverage=E.Coverage(
            complete=True, processed_chunks=1, planned_chunks=1, upstream_truncated=False
        ),
        provenance=derived_provenance(),
        accounting_id="acc_" + "0" * 15 + "2",
    )
    assert enforce_or_fixed(env)["code"] == "LIMIT_EXCEEDED"


def test_reader_answer_is_capped_to_the_requested_budget(tmp_path):
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"alpha value here\n"))
    long_answer = "Alpha is present [c1]. " * 2000
    reply = answer_json(
        long_answer, [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha"}]
    )
    env = (
        Reader(registry, FakeLuna(default_reply=reply))
        .answer(
            "sess",
            {
                "schema_version": "1.0",
                "request_id": "req_b",
                "operation": "read",
                "question": "Is alpha present?",
                "sources": [
                    {
                        "source_id": entry.source_id,
                        "snapshot_id": entry.snapshot.snapshot_id,
                        "selector": {"kind": "all"},
                    }
                ],
                "budgets": {"max_chunks": 8, "max_answer_bytes": 512, "deadline_ms": 60000},
            },
        )
        .envelope
    )
    assert len(env["answer"].encode("utf-8")) <= 512
    assert enforce(env) is env


def test_oversized_source_is_refused_not_buffered(tmp_path):
    engine, _, _ = _engine(tmp_path)
    outcome = engine.evaluate("sess", "req_s", "z" * (L.max_source_bytes + 1))
    assert outcome.action == "error" and outcome.code == "LIMIT_EXCEEDED"


def test_max_concurrency_and_chunk_caps_are_contract_values():
    assert L.max_concurrent_model_calls == 2
    assert L.max_chunks_per_request == 8
    assert L.max_request_input_tokens == 64000
    assert L.max_output_tokens_per_call == 2048
    assert L.max_chunk_tokens == 8000


def test_serialized_size_counts_all_blocks_and_metadata(tmp_path):
    engine, _, _ = _engine(tmp_path)
    payload = {
        "content": [{"type": "text", "text": "a" * 8200}, {"type": "text", "text": "b" * 8200}],
        "meta": {"tool": "x"},
    }
    outcome = engine.evaluate("sess", "req_s", payload)
    assert outcome.action == "spill"
    assert outcome.bytes_measured > L.max_tool_result_bytes


def _citation():
    return {
        "id": "c1",
        "source_id": "src_abcd1234",
        "snapshot_id": "sha256:" + "a" * 64,
        "locator": {"kind": "lines", "start": 1, "end": 1},
        "quote": "alpha",
        "verified": True,
    }


# -- the envelope must fit even when every field individually does ----------


def _quote_dense_source(entries: int = 40, pairs: int = 40) -> str:
    """Ordinary quote-dense source: keys and string values, as any code or JSON file has."""
    rows = []
    for i in range(entries):
        body = ",".join(f'"k{i:02d}{j:02d}":"v{i:02d}{j:02d}"' for j in range(pairs))
        rows.append(f"export const e{i:02d}={{{body}}};")
    return "\n".join(rows)


def _cited_reply(lines: list[str], filler_repeats: int, count: int = 16) -> str:
    filler = "short keys mapped onto short string values in declaration order " * filler_repeats
    citations = [
        {"id": f"c{i}", "line_start": i + 1, "line_end": i + 1, "quote": lines[i][:512]}
        for i in range(count)
    ]
    answer = " ".join(f"Entry {i:02d} uses {filler}[c{i}]." for i in range(count))
    return answer_json(answer, citations)


def _answer_over(tmp_path, filler_repeats: int):
    registry = make_registry(tmp_path, session_id="sess")
    body = _quote_dense_source()
    entry = registry.register("sess", snapshot_bytes(body.encode()))
    reply = _cited_reply(body.split("\n"), filler_repeats)
    return (
        Reader(registry, FakeLuna(default_reply=reply))
        .answer(
            "sess",
            {
                "schema_version": "1.0",
                "request_id": "req_fit",
                "operation": "read",
                "question": "What does this file declare?",
                "sources": [
                    {
                        "source_id": entry.source_id,
                        "snapshot_id": entry.snapshot.snapshot_id,
                        "selector": {"kind": "all"},
                    }
                ],
                "budgets": {
                    "max_chunks": 8,
                    "max_answer_bytes": DEFAULT_LIMITS.max_answer_bytes,
                    "deadline_ms": 60000,
                },
            },
        )
        .envelope
    )


def test_the_field_caps_alone_do_not_keep_an_envelope_under_the_cap():
    """This is why the reader has to measure: the per-field caps are not jointly satisfiable.

    A 1 KiB answer with the maximum number of maximum-length quotes is legal field by field
    and 17374 bytes serialized, over the 16 KiB envelope cap. Before the fit step the guard
    turned that into a bare LIMIT_EXCEEDED after the model call was already paid for.
    """
    quote = '"k":"v",' * 64
    assert len(quote.encode()) == DEFAULT_LIMITS.max_quote_bytes
    coverage = E.Coverage(upstream_truncated=False, complete=True)
    citations = [
        {
            "id": f"c{i}",
            "source_id": f"src_{i:016x}",
            "snapshot_id": "sha256:" + "0" * 64,
            "locator": {"kind": "lines", "start": 1, "end": 1},
            "quote": quote,
            "verified": True,
        }
        for i in range(DEFAULT_LIMITS.max_citations)
    ]
    env = E.build(
        request_id="req_x",
        status="ok",
        code="ANSWERED",
        coverage=coverage,
        sources=[],
        retryable=False,
        answer="a" * 1024,
        citations=citations,
        result_kind=ResultKind.MODEL_DERIVED,
        provenance=derived_provenance(),
        accounting_id="acc_" + "0" * 16,
    )
    assert E.serialized_bytes(env) > DEFAULT_LIMITS.max_envelope_bytes
    with pytest.raises(OutputGuardError):
        enforce(env)


def test_an_answer_that_fits_is_published_untouched(tmp_path):
    """The fit step must not shrink anything that was already inside the cap."""
    env = _answer_over(tmp_path, filler_repeats=1)
    assert env["code"] == "ANSWERED" and env["status"] == "ok"
    assert len(env["citations"]) == 16
    assert not [o for o in env["coverage"]["omitted"] if o["reason"] == "BUDGET_EXCEEDED"]
    # Close to the cap on purpose: if scaffolding grows, this becomes a trimming case and
    # that should be visible rather than silent.
    assert E.serialized_bytes(env) <= DEFAULT_LIMITS.max_envelope_bytes


def test_an_oversized_answer_drops_evidence_and_says_so_instead_of_refusing(tmp_path):
    env = _answer_over(tmp_path, filler_repeats=2)
    assert env["code"] == "ANSWERED", "a good verified answer must not become LIMIT_EXCEEDED"
    assert env["status"] == "partial", "dropping evidence is not a complete result"
    assert env["answer"]
    assert 0 < len(env["citations"]) < 16
    dropped = [o for o in env["coverage"]["omitted"] if o["reason"] == "BUDGET_EXCEEDED"]
    assert dropped, "what was removed has to be recorded, not silently absent"
    assert E.serialized_bytes(env) <= DEFAULT_LIMITS.max_envelope_bytes
    # Every surviving citation is still referenced by the answer, and vice versa.
    from context_shunt.citations import referenced_ids

    assert set(referenced_ids(env["answer"])) == {c["id"] for c in env["citations"]}


def test_trimming_is_deterministic_and_independent_of_citation_order(tmp_path):
    """Dropping by cost, not position: the model's ordering must not change the outcome."""
    first = _answer_over(tmp_path / "a", filler_repeats=3)
    second = _answer_over(tmp_path / "b", filler_repeats=3)
    assert {c["id"] for c in first["citations"]} == {c["id"] for c in second["citations"]}
    assert first["answer"] == second["answer"]


# -- caps report what they drop ---------------------------------------------------------

_CAP_SOURCE = "".join(f"key{i:02d} = value{i:02d}\n" for i in range(1, 21))
_CAP_QUESTION = "Which keys are configured and to what?"


def _cap_fixture(tmp_path, source: str = _CAP_SOURCE):
    registry = make_registry(tmp_path, session_id="sess")
    return registry, registry.register("sess", snapshot_bytes(source.encode()))


def _cap_request(entry, *, max_answer_bytes: int = 8192, question: str = _CAP_QUESTION):
    return {
        "schema_version": "1.0",
        "request_id": "req_cap",
        "operation": "read",
        "question": question,
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {
            "max_chunks": 8,
            "max_answer_bytes": max_answer_bytes,
            "deadline_ms": 60000,
        },
    }


def _claims_reply(claims, citations):
    import json as _json

    return _json.dumps({"claims": claims, "citations": citations})


def test_the_citation_cap_keeps_the_citations_the_claims_actually_reference(tmp_path):
    """The release blocker: an arbitrary-order citation cap lost material twice.

    Twenty citations verify and the ceiling is sixteen. The four claims in this reply cite
    the *last* four. Truncating in emission order dropped exactly those four citations,
    and then every claim that referenced them, so a request whose answer would have fitted
    published nothing at all. Taking the referenced citations first makes the whole answer
    fit, and because only unreferenced evidence overflowed, nothing was lost - so the
    envelope says `complete`, not `partial`. Reporting the unused overflow as dropped
    material would make two identical answers differ by which side of the ceiling their
    *unread* evidence happened to land on.
    """
    registry, entry = _cap_fixture(tmp_path)
    citations = [
        {"id": f"c{i}", "line_start": i, "line_end": i, "quote": f"key{i:02d} = value{i:02d}"}
        for i in range(1, 21)
    ]
    claims = [
        {"text": f"Key {i} is set to value{i:02d}.", "citation_ids": [f"c{i}"]}
        for i in range(17, 21)
    ]
    env = (
        Reader(registry, FakeLuna(replies=[_claims_reply(claims, citations)]))
        .answer("sess", _cap_request(entry))
        .envelope
    )
    assert env["code"] == "ANSWERED"
    for i in range(17, 21):
        assert f"value{i:02d}" in env["answer"]
    assert {c["id"] for c in env["citations"]} == {f"c{i}" for i in range(17, 21)}
    assert env["status"] == "ok" and env["coverage"]["complete"] is True
    assert not [o for o in env["coverage"]["omitted"] if o["reason"] == "BUDGET_EXCEEDED"]


def test_the_citation_cap_reports_the_referenced_citations_it_could_not_keep(tmp_path):
    """When the overflow *is* referenced, material really is lost and must be declared.

    Twenty claims each cite their own citation, so prioritization cannot save anything:
    four referenced citations pass the ceiling, the four claims resting on them go with
    them, and the answer is genuinely short of what the source supports. That is the case
    `complete: false` exists for - and the discriminator between this test and the one
    above is whether a claim referenced the citation, not how many verified.
    """
    registry, entry = _cap_fixture(tmp_path)
    citations = [
        {"id": f"c{i}", "line_start": i, "line_end": i, "quote": f"key{i:02d} = value{i:02d}"}
        for i in range(1, 21)
    ]
    claims = [
        {"text": f"Key {i} is set to value{i:02d}.", "citation_ids": [f"c{i}"]}
        for i in range(1, 21)
    ]
    env = (
        Reader(registry, FakeLuna(replies=[_claims_reply(claims, citations)]))
        .answer("sess", _cap_request(entry))
        .envelope
    )
    assert env["code"] == "ANSWERED"
    assert len(env["citations"]) == L.max_citations
    assert env["status"] == "partial" and env["coverage"]["complete"] is False
    dropped = [o for o in env["coverage"]["omitted"] if o["reason"] == "BUDGET_EXCEEDED"]
    assert len(dropped) == 20 - L.max_citations
    # The claims that rested on the dropped citations are gone from the answer too.
    for i in range(L.max_citations + 1, 21):
        assert f"value{i:02d}" not in env["answer"]


def test_unread_citations_over_the_cap_cannot_invent_an_answer_a_cap_emptied(tmp_path):
    """A reply with no usable claims is `NO_MATCH`, however much evidence it attached.

    The first cut of the cap fix counted *every* overflowing citation as dropped material,
    so twenty citations and no surviving claim came back `LIMIT_EXCEEDED/ANSWER_OVER_CAP`:
    a ceiling was blamed for emptying an answer that had never existed. The reply below
    cites nothing from its claims, so there is no answer material for a cap to lose.
    """
    registry, entry = _cap_fixture(tmp_path)
    citations = [
        {"id": f"c{i}", "line_start": i, "line_end": i, "quote": f"key{i:02d} = value{i:02d}"}
        for i in range(1, 21)
    ]
    env = (
        Reader(registry, FakeLuna(replies=[_claims_reply([], citations)]))
        .answer("sess", _cap_request(entry))
        .envelope
    )
    assert env["status"] == "ok" and env["code"] == "NO_MATCH"


def test_the_claims_cap_records_what_it_dropped(tmp_path):
    """Claims past ``max_claims_per_answer`` are never read, so they are reported."""
    registry, entry = _cap_fixture(tmp_path)
    citations = [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "key01 = value01"}]
    claims = [
        {"text": f"Assertion number {i}.", "citation_ids": ["c1"]}
        for i in range(L.max_claims_per_answer + 6)
    ]
    env = (
        Reader(registry, FakeLuna(replies=[_claims_reply(claims, citations)]))
        .answer("sess", _cap_request(entry))
        .envelope
    )
    assert env["code"] == "ANSWERED"
    assert env["answer"].count("Assertion number") == L.max_claims_per_answer
    assert env["status"] == "partial" and env["coverage"]["complete"] is False
    assert any(o["reason"] == "BUDGET_EXCEEDED" for o in env["coverage"]["omitted"])


def test_the_answer_byte_cap_records_what_it_dropped(tmp_path):
    """Shrinking the answer to fit and then reporting `complete: true` told the caller the
    whole selection had been read when part of the reading had just been deleted."""
    registry, entry = _cap_fixture(tmp_path)
    citations = [
        {"id": f"c{i}", "line_start": i, "line_end": i, "quote": f"key{i:02d} = value{i:02d}"}
        for i in range(1, 4)
    ]
    claims = [
        {"text": f"Key {i} is set to value{i:02d}.", "citation_ids": [f"c{i}"]} for i in range(1, 4)
    ]
    env = (
        Reader(registry, FakeLuna(replies=[_claims_reply(claims, citations)]))
        .answer("sess", _cap_request(entry, max_answer_bytes=40))
        .envelope
    )
    assert env["code"] == "ANSWERED"
    assert len(env["answer"].encode("utf-8")) <= 40
    assert env["status"] == "partial" and env["coverage"]["complete"] is False
    assert any(o["reason"] == "BUDGET_EXCEEDED" for o in env["coverage"]["omitted"])
    assert enforce(env) is env


def test_an_answer_a_cap_emptied_is_limit_exceeded_not_no_match(tmp_path):
    """NO_MATCH says the sources held nothing. That is a different, untrue statement.

    Here the source answered, and the answer was deleted a claim at a time until the byte
    ceiling was satisfied and nothing was left. The honest report names the ceiling.
    """
    registry, entry = _cap_fixture(tmp_path)
    citations = [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "key01 = value01"}]
    claims = [{"text": "Key 1 is set to value01.", "citation_ids": ["c1"]}]
    env = (
        Reader(registry, FakeLuna(replies=[_claims_reply(claims, citations)]))
        .answer("sess", _cap_request(entry, max_answer_bytes=1))
        .envelope
    )
    assert env["status"] == "error" and env["code"] == "LIMIT_EXCEEDED"
    assert env.get("answer", "") == ""
    assert env["recovery"]["handles_valid"] is True


def test_a_source_that_says_nothing_is_still_no_match(tmp_path):
    """The control: an empty answer with nothing dropped is still NO_MATCH."""
    registry, entry = _cap_fixture(tmp_path)
    env = (
        Reader(registry, FakeLuna(replies=[_claims_reply([], [])]))
        .answer("sess", _cap_request(entry))
        .envelope
    )
    assert env["status"] == "ok" and env["code"] == "NO_MATCH"


# -- the shared input budget covers every physical call ---------------------------------


def _per_call_tokens(entry, limits=L, question: str = _CAP_QUESTION) -> int:
    """What the reader debits for one physical call of this chunk's prompt."""
    from context_shunt.chunking import estimate_tokens as chunk_tokens
    from context_shunt.chunking import plan
    from context_shunt.provider import READER_SYSTEM_PROMPT, build_user_message

    the_plan = plan(
        [(entry.source_id, entry.snapshot, {"kind": "all"})],
        max_chunks=8,
        limits=limits,
        question=question,
    )
    chunk = the_plan.chunks[0]
    return chunk_tokens(READER_SYSTEM_PROMPT, limits) + chunk_tokens(
        build_user_message(question, chunk.text, chunk.locator), limits
    )


def test_a_fallback_candidate_the_budget_cannot_afford_is_never_started(tmp_path):
    """The release blocker: the budget was debited once, outside the provider chain.

    Every candidate re-sends the whole prompt, so a chain of three transmitted three
    prompts against a single debit and could exceed ``max_request_input_tokens`` outright.
    The budget here fits exactly one call: the primary is tried and fails, and the
    alternative is refused before it is started rather than after it is billed.
    """
    registry, entry = _cap_fixture(tmp_path)
    budget = _per_call_tokens(entry)
    limits = L.narrow(max_request_input_tokens=budget)
    reply = _claims_reply(
        [{"text": "Key 1 is set to value01.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "key01 = value01"}],
    )
    primary = FakeLuna(replies=[transient()])
    alternative = FakeLuna(replies=[reply])
    chain = FallbackChainProvider(primary, [alternative], limits)
    env = Reader(registry, chain, limits=limits).answer("sess", _cap_request(entry)).envelope
    assert primary.call_count == 1
    assert alternative.call_count == 0, "a candidate the budget cannot afford was started"
    assert any(o["reason"] == "BUDGET_EXCEEDED" for o in env["coverage"]["omitted"])


def test_a_fallback_candidate_the_budget_can_afford_still_runs(tmp_path):
    """The control: two calls' worth of budget lets the chain advance exactly once."""
    registry, entry = _cap_fixture(tmp_path)
    limits = L.narrow(max_request_input_tokens=_per_call_tokens(entry) * 2)
    reply = _claims_reply(
        [{"text": "Key 1 is set to value01.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "key01 = value01"}],
    )
    primary = FakeLuna(replies=[transient()])
    alternative = FakeLuna(replies=[reply])
    third = FakeLuna(replies=[reply])
    chain = FallbackChainProvider(primary, [alternative, third], limits)
    env = Reader(registry, chain, limits=limits).answer("sess", _cap_request(entry)).envelope
    assert primary.call_count == 1 and alternative.call_count == 1
    assert third.call_count == 0
    assert env["code"] == "ANSWERED"


def test_a_cap_does_not_relabel_a_verification_failure(tmp_path):
    """Precedence: nothing verified is a citation failure, whatever a cap also dropped.

    The cap branch answers "the source said something and it would not fit". When no
    citation verified there was nothing to fit, so the verification failure keeps
    precedence and the envelope still names it.
    """
    registry, entry = _cap_fixture(tmp_path)
    # The quote is real but the locator names a different line, so verification fails on
    # the snapshot bytes and nothing survives.
    citations = [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "key01 = value01"}]
    claims = [
        {"text": f"Assertion number {i}.", "citation_ids": ["c1"]}
        for i in range(L.max_claims_per_answer + 6)
    ]
    env = (
        Reader(registry, FakeLuna(replies=[_claims_reply(claims, citations)]))
        .answer("sess", _cap_request(entry))
        .envelope
    )
    assert env["status"] == "error" and env["code"] == "CITATION_INVALID"
    assert env["recovery"]["handles_valid"] is True
