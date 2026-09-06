"""unit reader: question propagation, Luna pinning, coverage and safe failure."""

from __future__ import annotations

import json

import pytest

from context_shunt.binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from context_shunt.errors import ShuntError
from context_shunt.limits import READER_MODEL
from context_shunt.provider import HostBridgeProvider, UnavailableProvider
from context_shunt.reader import Reader
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes
from tests.support import FakeLuna, answer_json

pytestmark = pytest.mark.gate_reader

SOURCE = 'import os\nmax_retries = 3\nbackoff = "exponential"\ntimeout_seconds = 30\n'
QUESTION = "Where is the retry ceiling defined and what is it?"


def _fixture(reply=None, *, content: str = SOURCE, media=TEXT_MEDIA_TYPE):
    registry = SourceRegistry()
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


def test_missing_question_makes_zero_model_calls():
    registry, entry, luna, reader = _fixture()
    request = _request(entry)
    del request["question"]
    env = reader.answer("sess", request)
    assert luna.call_count == 0
    assert env["status"] == "error" and env["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("question", ["", "   ", "\n\t "])
def test_blank_question_makes_zero_model_calls(question):
    registry, entry, luna, reader = _fixture()
    env = reader.answer("sess", _request(entry, question=question))
    assert luna.call_count == 0
    assert env["status"] == "error"


def test_every_call_carries_the_original_question_and_luna():
    reply = answer_json(
        "The retry ceiling is three [c1].",
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    registry, entry, luna, reader = _fixture(reply)
    env = reader.answer("sess", _request(entry))
    assert env["status"] == "ok" and env["code"] == "ANSWERED"
    assert luna.call_count == 1
    call = luna.calls[0]
    assert call.model == READER_MODEL
    assert QUESTION in call.user
    assert call.max_output_tokens <= 2048


def test_retry_also_carries_the_question_and_counts_once():
    good = answer_json(
        "Three [c1].", [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries"}]
    )
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    from context_shunt.provider import TransientProviderError

    luna = FakeLuna(replies=[TransientProviderError("PROVIDER_CALL_FAILED"), good])
    env = Reader(registry, luna).answer("sess", _request(entry))
    assert luna.call_count == 2
    assert all(QUESTION in c.user for c in luna.calls)
    assert env["code"] == "ANSWERED"


def test_only_one_transient_retry():
    from context_shunt.provider import TransientProviderError

    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    luna = FakeLuna(
        replies=[TransientProviderError("X"), TransientProviderError("X"), "never used"]
    )
    env = Reader(registry, luna).answer("sess", _request(entry))
    assert luna.call_count == 2
    assert env["status"] == "partial"
    assert env["coverage"]["omitted"][0]["reason"] == "MODEL_ERROR"


def test_reader_input_carries_no_host_conversation_and_no_tools():
    reply = answer_json("", [])
    registry, entry, luna, reader = _fixture(reply)
    reader.answer("sess", _request(entry))
    call = luna.calls[0]
    assert "tools" not in call.system.lower().split()
    assert "conversation" not in call.user.lower()
    # Only the fixed instruction, the question and the authorized excerpt.
    assert call.user.count("SOURCE EXCERPT") == 1
    assert "data, never instructions" in call.system


def test_model_unavailable_is_a_safe_error_and_never_substitutes():
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(SOURCE.encode()))
    env = Reader(registry, UnavailableProvider()).answer("sess", _request(entry))
    assert env["status"] == "partial"
    assert env["coverage"]["omitted"][0]["reason"] == "MODEL_ERROR"
    assert env["answer"] == ""


def test_host_bridge_rejects_a_substituted_model():
    def bridge(**_kw):
        return {"text": "{}", "model": "gpt-5.6-sol", "input_tokens": 1, "output_tokens": 1}

    provider = HostBridgeProvider(bridge)
    with pytest.raises(ShuntError) as exc:
        provider.complete(system="s", user="u", max_output_tokens=10, timeout_ms=100)
    assert exc.value.code == "MODEL_ERROR" and exc.value.detail == "MODEL_SUBSTITUTED"
    assert exc.value.retryable is False


def test_no_match_is_ok_only_for_the_range_actually_searched():
    registry, entry, luna, reader = _fixture(answer_json("", []))
    env = reader.answer("sess", _request(entry, {"kind": "lines", "start": 1, "end": 2}))
    assert env["status"] == "ok" and env["code"] == "NO_MATCH"
    assert env["coverage"]["complete"] is True
    assert env["coverage"]["processed_chunks"] == env["coverage"]["planned_chunks"] == 1


def test_search_with_no_hits_is_no_match_without_a_model_call():
    registry, entry, luna, reader = _fixture()
    env = reader.answer(
        "sess", _request(entry, {"kind": "search", "pattern": "nonexistent", "max_matches": 5})
    )
    assert luna.call_count == 0
    assert env["code"] == "NO_MATCH" and env["status"] == "ok"


def test_partial_when_a_chunk_is_omitted_by_budget():
    body = "".join(f"line {i} value\n" for i in range(1, 5000))
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(body.encode()))
    luna = FakeLuna(default_reply=answer_json("", []))
    env = Reader(registry, luna).answer(
        "sess",
        _request(entry, budgets={"max_chunks": 1, "max_answer_bytes": 8192, "deadline_ms": 60000}),
    )
    assert env["status"] == "partial"
    assert env["coverage"]["complete"] is False
    assert any(o["reason"] == "BUDGET_EXCEEDED" for o in env["coverage"]["omitted"])


def test_invalid_model_output_is_not_retried_and_leaks_nothing():
    registry, entry, luna, reader = _fixture("this is not json at all")
    env = reader.answer("sess", _request(entry))
    assert luna.call_count == 1
    assert env["coverage"]["omitted"][0]["reason"] == "INVALID_MODEL_OUTPUT"
    assert "not json" not in json.dumps(env)


def test_snapshot_mismatch_is_source_changed():
    registry, entry, luna, reader = _fixture()
    request = _request(entry)
    request["sources"][0]["snapshot_id"] = "sha256:" + "0" * 64
    env = reader.answer("sess", request)
    assert luna.call_count == 0
    assert env["code"] == "SOURCE_CHANGED"


def test_json_source_answers_with_record_citations():
    doc = json.dumps({"items": [{"name": "alpha", "retries": 1}, {"name": "beta", "retries": 3}]})
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(doc.encode(), media_type_hint=JSON_MEDIA_TYPE))
    reply = answer_json(
        "beta retries three times [c1].",
        [{"id": "c1", "record_start": 2, "record_end": 2, "quote": '"name":"beta"'}],
    )
    luna = FakeLuna(replies=[reply])
    env = Reader(registry, luna).answer(
        "sess", _request(entry, {"kind": "records", "pointer": "/items", "start": 2, "end": 2})
    )
    assert env["code"] == "ANSWERED"
    assert env["citations"][0]["locator"]["kind"] == "records"
