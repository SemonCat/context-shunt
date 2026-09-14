"""Session fallback ordering, bounded delivery and preserved reader accounting."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from context_shunt.errors import ShuntError
from context_shunt.guard import OutputGuardError, enforce
from context_shunt.legacy_compact import compact_tool_result
from context_shunt.limits import EMITTED_SCHEMA_VERSION
from context_shunt.session import ShuntSession
from tests.support import FakeLuna, claims_json, make_capability, make_config

pytestmark = pytest.mark.gate_legacy_compact


def setup(tmp_path, provider=None, body=None, **config):
    session = ShuntSession(
        "sess", make_config(tmp_path, **config), make_capability(), provider=provider
    )
    body = (
        body
        if body is not None
        else (
            "row 0 ERROR: connection refused\n"
            + "row %d ordinary line\n" * 300 % tuple(range(1, 301))
        )
    )
    path = tmp_path / "ws" / "source.txt"
    path.write_text(body)
    entry = session.register_path(str(path))
    request = {
        "schema_version": "1.1",
        "request_id": "req_legacy",
        "operation": "read",
        "question": "What happened?",
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 1, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    return session, entry, request, body


def _no_evidence_luna(quote: str = "ERROR: connection refused") -> FakeLuna:
    # A real quote, but the locator names the wrong line: verification fails on the
    # snapshot bytes and nothing survives, which is CITATION_INVALID/NO_VALID_EVIDENCE.
    reply = claims_json(
        [{"text": "Something failed.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 250, "line_end": 250, "quote": quote}],
    )
    return FakeLuna(replies=[reply])


def test_legacy_compaction_false_cannot_disable_mandatory_fallback(tmp_path):
    session, _, request, body = setup(
        tmp_path, _no_evidence_luna(), **{"reader": {"legacy_compaction": False}}
    )
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["original_failure"] == "CITATION_INVALID"
    assert env["failure_detail"] == "NO_VALID_EVIDENCE"
    assert env["legacy_compaction"]["summary"] == compact_tool_result(body, hard_chars=16000)


def test_output_guard_rejects_untrusted_or_pre_1_2_raw_artifact_locator(tmp_path):
    session, _, request, _ = setup(tmp_path, FakeLuna(default_reply=ShuntError("MODEL_ERROR")))
    env = session.read(request)
    assert env["schema_version"] == EMITTED_SCHEMA_VERSION == "1.3"
    assert "raw_artifact_path" in env["legacy_compaction"]
    enforce(env)

    arbitrary = deepcopy(env)
    arbitrary["legacy_compaction"]["raw_artifact_path"] = "/etc/passwd"
    with pytest.raises(OutputGuardError, match="raw artifact path malformed"):
        enforce(arbitrary)

    pre_1_2 = deepcopy(env)
    pre_1_2["schema_version"] = "1.1"
    with pytest.raises(OutputGuardError, match="raw artifact path malformed"):
        enforce(pre_1_2)


def test_reader_disabled_config_keeps_bare_failure_and_never_runs_compaction(tmp_path):
    session, _, request, _ = setup(tmp_path, _no_evidence_luna(), **{"reader": {"enabled": False}})
    env = session.read(request)
    assert env["code"] == "INVALID_REQUEST" and "legacy_compaction" not in env


def test_compaction_failure_degrades_to_original_bounded_failure_never_raw(tmp_path, monkeypatch):
    session, _, request, body = setup(tmp_path, _no_evidence_luna())

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected failure mid-compaction")

    monkeypatch.setattr("context_shunt.session.compact_tool_result", _boom)
    env = session.read(request)
    enforce(env)
    # The independent incumbent path still provides the mandatory fallback when the
    # normal wrapper itself fails.
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["original_failure"] == "CITATION_INVALID"
    assert env["legacy_compaction"]["summary"] == compact_tool_result(body, hard_chars=16000)
    assert body not in json.dumps(env)


def test_summary_never_exceeds_the_extraction_byte_cap_even_with_multibyte_text(tmp_path):
    from context_shunt.limits import DEFAULT_LIMITS

    wide_body = "日本語のログ行 ERROR: 失敗しました\n" * 5000
    session, entry, request, _ = setup(
        tmp_path,
        FakeLuna(default_reply=ShuntError("MODEL_ERROR")),
        body=wide_body,
        **{"reader": {"legacy_compaction_max_chars": 60_000}},
    )
    env = session.read(request)
    enforce(env)
    assert env["code"] == "LEGACY_COMPACTED"
    summary = env["legacy_compaction"]["summary"]
    assert len(summary.encode("utf-8")) <= DEFAULT_LIMITS.max_extraction_bytes
    # No raw crash on decode/encode boundaries; the guard's secret-marker/UTF-8 checks
    # already ran inside `enforce(env)` above.
    summary.encode("utf-8")


def test_legacy_compaction_only_covers_the_first_requested_source(tmp_path):
    session, entry, request, _ = setup(tmp_path, FakeLuna(default_reply=ShuntError("MODEL_ERROR")))
    second_path = tmp_path / "ws" / "second.txt"
    second_path.write_text("second source body\n" * 50)
    second_entry = session.register_path(str(second_path))
    request["sources"].append(
        {
            "source_id": second_entry.source_id,
            "snapshot_id": second_entry.snapshot.snapshot_id,
            "selector": {"kind": "all"},
        }
    )
    env = session.read(request)
    enforce(env)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["source_id"] == entry.source_id
    assert any(o["reason"] == "UNKNOWN_REMAINDER" for o in env["coverage"]["omitted"])
    assert env["coverage"]["complete"] is False


@pytest.mark.parametrize("failure_code", ["MODEL_ERROR", "TIMEOUT"])
@pytest.mark.parametrize("legacy_mode", ["enabled", "disabled", "raises", "unsafe"])
def test_availability_fallback_precedence(tmp_path, monkeypatch, failure_code, legacy_mode):
    from context_shunt.errors import ShuntError
    from context_shunt.provenance import TokenMethod, Usage
    from context_shunt.provider import FallbackChainProvider, TransientProviderError

    failure = (
        TransientProviderError("PRIVATE_BODY")
        if failure_code == "MODEL_ERROR"
        else ShuntError("TIMEOUT", "MODEL_CALL", retryable=True)
    )
    failure.billed_usage = Usage(input_tokens=10, output_tokens=5, method=TokenMethod.EXACT)
    first, second = FakeLuna(default_reply=failure), FakeLuna(default_reply=failure)
    session, entry, request, body = setup(
        tmp_path,
        FallbackChainProvider(first, [second]),
        reader={"automatic_extract": True, "legacy_compaction": legacy_mode != "disabled"},
    )
    original_answer = session._reader.answer
    observed = []

    def answer(*args, **kwargs):
        result = original_answer(*args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(session._reader, "answer", answer)
    if legacy_mode == "raises":

        def broken(*args, **kwargs):
            raise RuntimeError("PRIVATE_COMPACTOR_BODY")

        monkeypatch.setattr(session, "_legacy_compaction_fallback", broken)
    elif legacy_mode == "unsafe":
        monkeypatch.setattr(
            "context_shunt.session.compact_tool_result",
            lambda *a, **k: "-----BEGIN PRIVATE KEY-----",
        )
    env = session.read(request)
    enforce(env)
    assert env["code"] == "LEGACY_COMPACTED"
    assert observed[0].availability_failure
    assert env["status"] == "partial" and not env["provenance"]["derived"]
    assert env["sources"] == observed[0].envelope["sources"]
    assert env["recovery"]["handles_valid"]
    assert env["provenance"]["attempts_started"] == first.call_count + second.call_count > 0
    assert env["answer"] == "" and env["citations"] == []
    assert body not in json.dumps(env) and "PRIVATE_BODY" not in json.dumps(env)
    assert "PRIVATE_COMPACTOR_BODY" not in json.dumps(env)
    assert "-----BEGIN PRIVATE KEY-----" not in json.dumps(env)
    assert env["legacy_compaction"]["original_failure"] == failure_code
    assert env["result_kind"] == "legacy_compaction"
    assert "not model-derived" in env["guidance"] and "not an LLM summary" in env["guidance"]
    assert "never as the question's answer" in env["guidance"]
    assert "exact count" in env["guidance"] and "citation evidence" in env["guidance"]
    assert env["coverage"]["complete"] is False
    assert env["provenance"]["citations_mechanically_verified"] is False
    for omission in observed[0].envelope["coverage"]["omitted"]:
        assert omission in env["coverage"]["omitted"]
    for key in ("processed_chunks", "planned_chunks", "upstream_truncated"):
        assert env["coverage"][key] == observed[0].envelope["coverage"][key]
    stats = session.stats({"schema_version": "1.1", "request_id": "stats", "operation": "stats"})
    rows = [r for r in stats["stats"]["records"] if r["operation_id"] == env["accounting_id"]]
    assert len(rows) == 1
    assert rows[0]["attempts_started"] == first.call_count + second.call_count
    assert rows[0]["reader_input_tokens"] == observed[0].cost.input_tokens


@pytest.mark.parametrize("failure_code", ["MODEL_ERROR", "TIMEOUT"])
def test_legacy_fallback_is_mandatory_when_all_optional_switches_are_false(tmp_path, failure_code):
    from context_shunt.errors import ShuntError

    session, _, request, _ = setup(
        tmp_path,
        FakeLuna(default_reply=ShuntError(failure_code)),
        reader={"legacy_compaction": False, "automatic_extract": False},
    )
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["original_failure"] == failure_code
    assert "extraction" not in env


def test_citation_failure_uses_legacy_compaction_and_preserves_evidence(tmp_path):
    session, entry, request, body = setup(tmp_path, _no_evidence_luna())
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["status"] == "partial"
    assert env["answer"] == "" and env["citations"] == []
    assert env["legacy_compaction"]["original_failure"] == "CITATION_INVALID"
    assert env["failure_detail"] == "NO_VALID_EVIDENCE"
    assert env["sources"][0]["source_id"] == entry.source_id
    assert env["recovery"]["handles_valid"] is True
    assert "INSPECT_HANDLE" in env["recovery"]["actions"]
    assert "not model-derived" in env["guidance"]
    assert body not in json.dumps(env)

    inspected = session.inspect(
        {
            "schema_version": "1.1",
            "request_id": "verify_evidence",
            "budgets": {"max_result_bytes": 1024, "max_scan_lines": 100},
            "operation": "inspect",
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "selector": {"kind": "lines", "start": 1, "end": 1},
        }
    )
    assert inspected["code"] == "EXTRACTED"
    assert "connection refused" in json.dumps(inspected["extraction"])
    rows = session.stats({"schema_version": "1.1", "request_id": "stats", "operation": "stats"})
    record = next(r for r in rows["stats"]["records"] if r["operation_id"] == env["accounting_id"])
    assert record["code"] == "LEGACY_COMPACTED"
    assert record["delivery_boundary"] == "extraction"
    assert record["attempts_started"] > 0


def test_legacy_compaction_block_cannot_masquerade_as_a_question_answer(tmp_path):
    session, _entry, request, _body = setup(tmp_path, _no_evidence_luna())
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"

    disguised = deepcopy(env)
    disguised["code"] = "ANSWERED"
    disguised["answer"] = "Exactly 0 matches across the source."
    with pytest.raises(OutputGuardError, match="cannot masquerade"):
        enforce(disguised)


@pytest.mark.parametrize("shape", ["legacy", "claims"])
def test_uncited_semantic_reply_is_citation_failure_with_cost_and_handle(tmp_path, shape):
    reply = json.dumps(
        {"answer": "UNVERIFIED_SENTINEL", "citations": []}
        if shape == "legacy"
        else {"claims": [{"text": "UNVERIFIED_SENTINEL", "citation_ids": []}], "citations": []}
    )
    session, entry, request, body = setup(
        tmp_path,
        FakeLuna(replies=[reply]),
        reader={"legacy_compaction": False, "automatic_extract": False},
    )
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["status"] == "partial"
    assert env["answer"] == "" and env["citations"] == []
    assert env["sources"][0]["source_id"] == entry.source_id
    assert env["recovery"]["handles_valid"] is True
    assert env["provenance"]["derived"] is False
    assert env["legacy_compaction"]["original_failure"] == "CITATION_INVALID"
    assert env["provenance"]["attempts_started"] > 0
    assert body not in json.dumps(env)
    assert "UNVERIFIED_SENTINEL" not in json.dumps(env)
    rows = session.stats({"schema_version": "1.1", "request_id": "stats", "operation": "stats"})
    record = next(r for r in rows["stats"]["records"] if r["operation_id"] == env["accounting_id"])
    assert record["code"] == "LEGACY_COMPACTED"
    assert record["attempts_started"] == 2
    assert record["attempts_usage_complete"] == 2
    assert record["reader_input_tokens"] == 20
    assert record["reader_output_tokens"] == 10


@pytest.mark.parametrize("shape", ["legacy", "claims"])
def test_citation_recovery_revalidates_handles_after_provider_wait(tmp_path, monkeypatch, shape):
    reply = json.dumps(
        {"answer": "UNVERIFIED_SENTINEL", "citations": []}
        if shape == "legacy"
        else {"claims": [{"text": "UNVERIFIED_SENTINEL", "citation_ids": []}], "citations": []}
    )
    session, _, request, _ = setup(tmp_path, FakeLuna(replies=[reply]))
    original = session._registry.resolve
    calls = 0

    def resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ShuntError("SOURCE_EXPIRED")
        return original(*args, **kwargs)

    monkeypatch.setattr(session._registry, "resolve", resolve)
    env = session.read(request)
    assert env["code"] == "SOURCE_EXPIRED"
    assert env["recovery"]["handles_valid"] is False
    assert "RECAPTURE_SOURCE" in env["recovery"]["actions"]
    assert "legacy_compaction" not in env and "extraction" not in env


@pytest.mark.parametrize(
    "reply", [{"answer": "", "citations": []}, {"claims": [], "citations": []}]
)
def test_empty_semantic_reply_is_valid_no_match(tmp_path, reply):
    session, entry, request, body = setup(
        tmp_path, FakeLuna(replies=[json.dumps(reply)]), body="irrelevant excerpt\n"
    )
    env = session.read(request)
    assert env["status"] == "ok" and env["code"] == "NO_MATCH"
    assert env["coverage"]["complete"] is True
    assert env["answer"] == "" and env["citations"] == []
    assert env["sources"][0]["source_id"] == entry.source_id
    assert body not in json.dumps(env)
    rows = session.stats({"schema_version": "1.1", "request_id": "stats", "operation": "stats"})
    record = next(r for r in rows["stats"]["records"] if r["operation_id"] == env["accounting_id"])
    assert record["code"] == "NO_MATCH"
    assert record["attempts_started"] == 1
    assert record["reader_input_tokens"] == 10 and record["reader_output_tokens"] == 5


@pytest.mark.parametrize(
    "reply", [{"answer": "", "citations": [{}]}, {"claims": [], "citations": [{}]}]
)
def test_empty_reply_with_malformed_citations_is_not_valid_no_match(tmp_path, reply):
    session, _, request, _ = setup(tmp_path, FakeLuna(replies=[json.dumps(reply)]))
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["original_failure"] == "CITATION_INVALID"
    assert env["coverage"]["complete"] is False


@pytest.mark.parametrize("shape", ["legacy", "claims"])
@pytest.mark.parametrize(
    "case", ["empty", "uncited", "unused", "cited", "invalid_empty", "verified_empty"]
)
def test_semantic_support_requires_referenced_evidence(tmp_path, shape, case):
    citation = {"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha"}
    citations = [] if case in ("empty", "uncited") else [citation]
    if case == "invalid_empty":
        citations = [{}]
    empty = case in ("empty", "invalid_empty", "verified_empty")
    text = "" if empty else ("alpha [c1]." if case == "cited" else "UNVERIFIED_SENTINEL")
    reply = (
        {"answer": text, "citations": citations}
        if shape == "legacy"
        else {
            "claims": []
            if empty
            else [
                {
                    "text": "alpha." if case == "cited" else text,
                    "citation_ids": ["c1"] if case == "cited" else [],
                }
            ],
            "citations": citations,
        }
    )
    session, entry, request, _ = setup(
        tmp_path, FakeLuna(replies=[json.dumps(reply)]), body="alpha\n"
    )
    env = session.read(request)
    reader_expected = (
        "NO_MATCH"
        if case in ("empty", "verified_empty")
        else "ANSWERED"
        if case == "cited"
        else "CITATION_INVALID"
    )
    expected = "LEGACY_COMPACTED" if reader_expected == "CITATION_INVALID" else reader_expected
    assert env["code"] == expected
    assert env["status"] == ("partial" if expected == "LEGACY_COMPACTED" else "ok")
    assert env["coverage"]["complete"] is (expected != "LEGACY_COMPACTED")
    assert env["sources"][0]["source_id"] == entry.source_id
    assert "UNVERIFIED_SENTINEL" not in json.dumps(env)
    if expected == "LEGACY_COMPACTED":
        assert env["answer"] == "" and env["citations"] == []
        assert env["recovery"]["handles_valid"] is True
        assert env["legacy_compaction"]["original_failure"] == "CITATION_INVALID"
    rows = session.stats({"schema_version": "1.1", "request_id": "stats", "operation": "stats"})
    record = next(r for r in rows["stats"]["records"] if r["operation_id"] == env["accounting_id"])
    assert record["code"] == expected
    attempts = 2 if case in ("uncited", "unused") else 1
    assert record["attempts_started"] == attempts
    assert (
        record["reader_input_tokens"] == 10 * attempts
        and record["reader_output_tokens"] == 5 * attempts
    )


@pytest.mark.parametrize("shape", ["legacy", "claims"])
def test_unused_valid_citation_recovery_revalidates_ttl(tmp_path, monkeypatch, shape):
    citation = {"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha"}
    reply = (
        {"answer": "UNVERIFIED_SENTINEL", "citations": [citation]}
        if shape == "legacy"
        else {
            "claims": [{"text": "UNVERIFIED_SENTINEL", "citation_ids": []}],
            "citations": [citation],
        }
    )
    session, _, request, _ = setup(tmp_path, FakeLuna(replies=[json.dumps(reply)]), body="alpha\n")
    original = session._registry.resolve
    calls = 0

    def resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 2:
            raise ShuntError("SOURCE_EXPIRED")
        return original(*args, **kwargs)

    monkeypatch.setattr(session._registry, "resolve", resolve)
    env = session.read(request)
    assert env["code"] == "SOURCE_EXPIRED"
    assert calls >= 3
    assert env["recovery"]["handles_valid"] is False
    assert "RECAPTURE_SOURCE" in env["recovery"]["actions"]
    assert "UNVERIFIED_SENTINEL" not in json.dumps(env)
