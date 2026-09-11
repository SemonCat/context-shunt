"""Automatic escape hatch: availability-only, exact, guarded and accounted once."""

import json
import time
from pathlib import Path

import pytest

from context_shunt.accounting import new_operation_id
from context_shunt.envelope import serialized_bytes
from context_shunt.errors import ShuntError
from context_shunt.guard import enforce
from context_shunt.legacy_compact import compact_tool_result
from context_shunt.provenance import TokenMethod, Usage
from context_shunt.provider import FallbackChainProvider, TransientProviderError
from context_shunt.session import ShuntSession
from tests.support import FakeLuna, make_capability, make_config

pytestmark = pytest.mark.gate_inspect


def setup(tmp_path, provider=None, **config):
    session = ShuntSession(
        "sess",
        make_config(tmp_path, **config),
        make_capability(),
        provider=provider or FakeLuna(default_reply=TransientProviderError("PRIVATE_BODY")),
    )
    body = ('prefix 日本 "quoted" \\ tab\t\n' * 1000) + "TAIL_CANARY"
    path = tmp_path / "ws" / "source.txt"
    path.write_text(body)
    entry = session.register_path(str(path))
    request = {
        "schema_version": "1.1",
        "request_id": "req_fallback",
        "operation": "read",
        "question": "What is here?",
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


def automatic_extract(session, request):
    """Exercise the exact extraction tier without routing through session.read()."""
    operation_id = new_operation_id()
    result = session._reader.answer(session.session_id, request, accounting_id=operation_id)
    envelope = session._automatic_extract(request, result, request["request_id"], operation_id)
    return envelope, result, operation_id


def stats(session):
    return session.stats(
        {"schema_version": "1.1", "request_id": "req_stats", "operation": "stats"}
    )["stats"]["records"]


def test_all_providers_fail_mandatory_legacy_compaction_is_bounded_and_accounted(tmp_path):
    failure = TransientProviderError("PRIVATE_BODY")
    failure.billed_usage = Usage(input_tokens=10, output_tokens=5, method=TokenMethod.EXACT)
    a = FakeLuna(default_reply=failure)
    b = FakeLuna(default_reply=failure)
    session, entry, request, body = setup(tmp_path, FallbackChainProvider(a, [b]))
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    enforce(env)
    assert a.call_count == b.call_count == 2
    assert env["status"] == "partial" and not env["coverage"]["complete"]
    assert env["result_kind"] == "legacy_compaction"
    assert env["provenance"]["derived"] is False
    assert env["provenance"]["attempts_started"] == 4
    assert env["answer"] == "" and env["citations"] == []
    assert "deterministic legacy-shaped compaction" in env["guidance"]
    assert "not an LLM summary" in env["guidance"] and "MODEL_ERROR" in env["guidance"]
    assert env["legacy_compaction"]["summary"] == compact_tool_result(body, hard_chars=16_000)
    assert env["legacy_compaction"]["summary_bytes"] <= 16_384
    assert env["sources"][0]["snapshot_id"] == entry.snapshot.snapshot_id
    assert env["recovery"]["handles_valid"] is True
    assert "PRIVATE_BODY" not in json.dumps(env)
    rows = [r for r in stats(session) if r["operation_id"] == env["accounting_id"]]
    assert len(rows) == 1
    row = rows[0]
    assert row["attempts_started"] == 4
    assert row["reader_input_tokens"] == 40 and row["reader_output_tokens"] == 20
    assert row["reader_token_method"] == "exact"
    assert row["main_model_envelope_bytes"] == serialized_bytes(env)
    assert row["delivery_boundary"] == "extraction"


@pytest.mark.parametrize(
    "config",
    [
        {"reader": {"automatic_extract": False}},
        {"inspect": {"enabled": False}},
    ],
)
def test_disabled_retains_error_and_actions(tmp_path, config):
    session, _, request, _ = setup(tmp_path, **config)
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED" and "extraction" not in env
    assert env["legacy_compaction"]["original_failure"] == "MODEL_ERROR"
    assert "INSPECT_HANDLE" in env["recovery"]["actions"]


def test_disclosure_exhausted_returns_original_failure_after_one_legacy_fallback(tmp_path):
    session, _, request, _ = setup(tmp_path, limits={"disclosure_max_per_source_bytes": 32})
    first = session.read(request)
    assert first["code"] == "LEGACY_COMPACTED"
    assert first["legacy_compaction"]["summary_bytes"] <= 32
    second = session.read(request)
    assert second["code"] == "DISCLOSURE_EXHAUSTED" and "legacy_compaction" not in second
    assert "extraction" not in second


def test_multi_source_preserves_handles_and_omissions(tmp_path):
    session, _, request, _ = setup(tmp_path)
    path = tmp_path / "ws" / "second.txt"
    path.write_text("SECOND_SOURCE_CANARY\n" * 100)
    second = session.register_path(str(path))
    request["sources"].append(
        {
            "source_id": second.source_id,
            "snapshot_id": second.snapshot.snapshot_id,
            "selector": {"kind": "all"},
        }
    )
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED" and len(env["sources"]) == 2
    assert env["legacy_compaction"]["original_failure"] == "MODEL_ERROR"
    assert {o["source_id"] for o in env["coverage"]["omitted"]} == {
        s["source_id"] for s in request["sources"]
    }
    assert "SECOND_SOURCE_CANARY" not in json.dumps(env)


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("not JSON", "LEGACY_COMPACTED"),
        ('{"claims": [], "citations": []}', "NO_MATCH"),
        ('{"answer":"weak answer", "citations": []}', "LEGACY_COMPACTED"),
        (
            '{"answer":"Wrong [c1].", "citations":[{"id":"c1","line_start":1,"line_end":1,"quote":"missing"}]}',
            "LEGACY_COMPACTED",
        ),
        (ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", False), "MODEL_ERROR"),
        (ShuntError("CANCELLED"), "CANCELLED"),
    ],
)
def test_nonavailability_never_extracts_or_bypasses_refusals(tmp_path, reply, expected):
    session, _, request, _ = setup(tmp_path, FakeLuna(default_reply=reply))
    env = session.read(request)
    assert env["code"] == expected
    assert "extraction" not in env
    if expected == "LEGACY_COMPACTED":
        assert env["legacy_compaction"]["original_failure"] in {
            "INVALID_MODEL_OUTPUT",
            "CITATION_INVALID",
        }
    else:
        assert "legacy_compaction" not in env


def test_format_failure_then_outage_does_not_extract(tmp_path):
    session, _, request, _ = setup(
        tmp_path, FakeLuna(replies=["bad"], default_reply=TransientProviderError())
    )
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["original_failure"] in {
        "INVALID_MODEL_OUTPUT",
        "MODEL_ERROR",
    }
    assert "extraction" not in env


def test_actual_call_timeout_direct_extraction_remains_available(tmp_path):
    def slow(_):
        time.sleep(0.15)
        return '{"answer":"", "citations":[]}'

    session, _, request, _ = setup(
        tmp_path, FakeLuna(default_reply=slow), limits={"model_call_deadline_ms": 10}
    )
    env, _, _ = automatic_extract(session, request)
    assert env["code"] == "EXTRACTED" and "TIMEOUT" in env["guidance"]
    assert env["provenance"]["attempts_started"] == 1


@pytest.mark.parametrize("failure", ["STORE_FAILED", "SOURCE_EXPIRED", "SOURCE_CHANGED"])
def test_handle_or_store_failure_during_fallback_keeps_original(tmp_path, monkeypatch, failure):
    session, _, request, _ = setup(tmp_path)
    original = session._reader.answer

    def answer(*args, **kwargs):
        result = original(*args, **kwargs)

        def unavailable(*_):
            raise ShuntError(failure, "PRIVATE_STORE_BODY")

        monkeypatch.setattr(session.registry, "resolve", unavailable)
        return result

    monkeypatch.setattr(session._reader, "answer", answer)
    env = session.read(request)
    assert env["code"] == failure and "extraction" not in env
    assert "PRIVATE_STORE_BODY" not in json.dumps(env)
    assert env["recovery"]["handles_valid"] is False


@pytest.mark.parametrize("value", [0, 4097, True, 2.5, "2048", None])
def test_bad_config_rejected(tmp_path, value):
    with pytest.raises(ShuntError):
        make_config(tmp_path, reader={"fallback_max_bytes": value})


def test_config_and_request_cap(tmp_path):
    session, _, request, _ = setup(tmp_path, reader={"fallback_max_bytes": 100})
    request["budgets"]["max_answer_bytes"] = 50
    env, _, _ = automatic_extract(session, request)
    assert env["code"] == "EXTRACTED"
    assert env["extraction"]["result_bytes"] <= 50


@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "contracts/v1/conformance/automatic-extract-cases.json"
        ).read_text()
    ),
    ids=lambda c: c["name"],
)
def test_shared_automatic_extract_contract(tmp_path, case):
    session, _, request, _ = setup(tmp_path, reader={"fallback_max_bytes": case["cap"]})
    path = tmp_path / "ws" / "fixture.txt"
    path.write_text(case["text"])
    entry = session.register_path(str(path))
    request["sources"] = [
        {
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "selector": {"kind": "all"},
        }
    ]
    if case["expected"] is None:
        with pytest.raises(ShuntError) as raised:
            automatic_extract(session, request)
        assert raised.value.code == "LIMIT_EXCEEDED"
        assert raised.value.detail in {"EMPTY_FALLBACK", "UNIT_OVER_PAGE_BUDGET"}
    else:
        env, _, _ = automatic_extract(session, request)
        enforce(env)
        assert env["status"] == "partial" and env["code"] == "EXTRACTED"
        assert env["extraction"]["segments"] == [
            {"kind": "bytes", "start": 0, "end": case["end"], "text": case["expected"]}
        ]
        assert env["extraction"]["complete"] is False
        assert env["provenance"]["derived"] is False


def test_line_oversized_source_is_never_returned_whole(tmp_path):
    session, _, request, _ = setup(tmp_path)
    path = tmp_path / "ws" / "tiny-lines.txt"
    body = "x\n" * 351
    path.write_text(body)
    entry = session.register_path(str(path))
    request["sources"] = [
        {
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "selector": {"kind": "all"},
        }
    ]
    env, _, _ = automatic_extract(session, request)
    assert env["code"] == "EXTRACTED"
    assert 0 < env["extraction"]["result_bytes"] < len(body.encode())
    assert env["extraction"]["complete"] is False


def test_guard_refusal_does_not_consume_disclosure(tmp_path):
    session, entry, request, _ = setup(tmp_path, limits={"max_extended_envelope_bytes": 2048})
    before = session.store.disclosure_allowance(session.identity, entry.source_id).remaining
    with pytest.raises(ShuntError) as raised:
        automatic_extract(session, request)
    assert raised.value.code in {"LIMIT_EXCEEDED", "HOST_UNSAFE"}
    assert session.store.disclosure_allowance(session.identity, entry.source_id).remaining == before


def test_request_timeout_without_response_extracts(tmp_path):
    def slow(_):
        time.sleep(0.15)
        return '{"answer":"", "citations":[]}'

    session, _, request, _ = setup(tmp_path, FakeLuna(default_reply=slow))
    request["budgets"]["deadline_ms"] = 20
    env, _, _ = automatic_extract(session, request)
    assert env["code"] == "EXTRACTED" and "TIMEOUT" in env["guidance"]


def test_session_disclosure_exhausted(tmp_path):
    session, entry, request, _ = setup(tmp_path, limits={"disclosure_max_per_session_bytes": 32})
    assert session.store.charge_disclosure(session.identity, entry.source_id, "bytes", 32).granted
    env = session.read(request)
    assert env["code"] == "DISCLOSURE_EXHAUSTED" and "extraction" not in env
    assert "legacy_compaction" not in env


@pytest.mark.parametrize("value", [None, 0, 1, "false", {}])
def test_automatic_flag_requires_boolean(tmp_path, value):
    with pytest.raises(ShuntError):
        make_config(tmp_path, reader={"automatic_extract": value})


def test_timeout_preserves_already_started_fallback_attempts(tmp_path):
    def slow(_):
        time.sleep(0.15)
        return '{"answer":"", "citations":[]}'

    first = FakeLuna(default_reply=TransientProviderError())
    second = FakeLuna(default_reply=slow)
    session, _, request, _ = setup(
        tmp_path, FallbackChainProvider(first, [second]), limits={"model_call_deadline_ms": 20}
    )
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["original_failure"] == "TIMEOUT"
    assert first.call_count == second.call_count == 1
    assert env["provenance"]["attempts_started"] == 2
    row = next(r for r in stats(session) if r["operation_id"] == env["accounting_id"])
    assert row["attempts_started"] == 2


def test_secret_guard_refusal_does_not_charge_or_leak(tmp_path, monkeypatch):
    from context_shunt.inspect import Segment

    session, entry, request, _ = setup(tmp_path)
    original = session._inspector.extract
    secret_marker = "-----BEGIN PRIVATE KEY-----"

    def poisoned(*args, **kwargs):
        extraction = original(*args, **kwargs)
        extraction.segments = [Segment("bytes", 0, len(secret_marker), secret_marker)]
        extraction.result_bytes = len(secret_marker)
        return extraction

    monkeypatch.setattr(session._inspector, "extract", poisoned)
    before = session.store.disclosure_allowance(session.identity, entry.source_id).remaining
    with pytest.raises(ShuntError) as raised:
        automatic_extract(session, request)
    assert raised.value.code in {"LIMIT_EXCEEDED", "HOST_UNSAFE"}
    assert secret_marker not in str(raised.value)
    assert session.store.disclosure_allowance(session.identity, entry.source_id).remaining == before
