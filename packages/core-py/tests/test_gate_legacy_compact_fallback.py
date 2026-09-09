"""Session fallback ordering, bounded delivery and preserved reader accounting."""

from __future__ import annotations

import json

import pytest

from context_shunt.guard import enforce
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


def test_citation_invalid_no_evidence_triggers_legacy_compaction(tmp_path):
    session, entry, request, body = setup(tmp_path, _no_evidence_luna())
    env = session.read(request)
    enforce(env)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["status"] == "partial"
    assert env["result_kind"] == "legacy_compaction"
    assert env["provenance"]["derived"] is False
    assert env["provenance"]["label"] == "legacy_compaction"
    assert env["answer"] == "" and env["citations"] == []
    block = env["legacy_compaction"]
    assert block["original_failure"] == "CITATION_INVALID"
    assert block["source_id"] == entry.source_id
    assert block["snapshot_id"] == entry.snapshot.snapshot_id
    assert "ERROR: connection refused" in block["summary"]
    assert block["summary_bytes"] == len(block["summary"].encode("utf-8"))
    assert block["original_bytes"] == len(body.encode("utf-8"))
    assert env["recovery"]["handles_valid"] is True
    assert "ported from the incumbent" in env["guidance"]
    assert "not model-derived" in env["guidance"]


def test_legacy_compaction_disabled_by_config_keeps_bare_failure(tmp_path):
    session, _, request, _ = setup(
        tmp_path, _no_evidence_luna(), **{"reader": {"legacy_compaction": False}}
    )
    env = session.read(request)
    assert env["code"] == "CITATION_INVALID" and "legacy_compaction" not in env


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
    # Falls back to the reader's own bounded failure envelope, unchanged.
    assert env["code"] == "CITATION_INVALID"
    assert "legacy_compaction" not in env
    assert body not in json.dumps(env)


def test_summary_never_exceeds_the_extraction_byte_cap_even_with_multibyte_text(tmp_path):
    from context_shunt.limits import DEFAULT_LIMITS

    wide_body = "日本語のログ行 ERROR: 失敗しました\n" * 5000
    session, entry, request, _ = setup(
        tmp_path,
        _no_evidence_luna(quote="NONEXISTENT_QUOTE_TEXT_ABC123"),
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
    session, entry, request, _ = setup(tmp_path, _no_evidence_luna())
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
    expected = "LEGACY_COMPACTED" if legacy_mode == "enabled" else "EXTRACTED"
    assert env["code"] == expected
    assert observed[0].availability_failure
    assert env["status"] == "partial" and not env["provenance"]["derived"]
    assert env["sources"] == observed[0].envelope["sources"]
    assert env["recovery"]["handles_valid"]
    assert env["provenance"]["attempts_started"] == first.call_count + second.call_count > 0
    assert env["answer"] == "" and env["citations"] == []
    assert body not in json.dumps(env) and "PRIVATE_BODY" not in json.dumps(env)
    assert "PRIVATE_COMPACTOR_BODY" not in json.dumps(env)
    assert "-----BEGIN PRIVATE KEY-----" not in json.dumps(env)
    if legacy_mode == "enabled":
        assert env["legacy_compaction"]["original_failure"] == failure_code
        assert env["result_kind"] == "legacy_compaction"
        assert "not model-derived" in env["guidance"] and "not an LLM summary" in env["guidance"]
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
def test_both_fallbacks_disabled_preserve_failure(tmp_path, failure_code):
    from context_shunt.errors import ShuntError

    session, _, request, _ = setup(
        tmp_path,
        FakeLuna(default_reply=ShuntError(failure_code)),
        reader={"legacy_compaction": False, "automatic_extract": False},
    )
    env = session.read(request)
    assert env["code"] == failure_code
    assert "legacy_compaction" not in env and "extraction" not in env
