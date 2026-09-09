"""Automatic escape hatch: availability-only, exact, guarded and accounted once."""

import json
import time
from pathlib import Path

import pytest

from context_shunt.envelope import serialized_bytes
from context_shunt.errors import ShuntError
from context_shunt.guard import enforce
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


def stats(session):
    return session.stats(
        {"schema_version": "1.1", "request_id": "req_stats", "operation": "stats"}
    )["stats"]["records"]


def test_all_providers_fail_exact_bounded_accounted(tmp_path):
    failure = TransientProviderError("PRIVATE_BODY")
    failure.billed_usage = Usage(input_tokens=10, output_tokens=5, method=TokenMethod.EXACT)
    a = FakeLuna(default_reply=failure)
    b = FakeLuna(default_reply=failure)
    session, entry, request, body = setup(tmp_path, FallbackChainProvider(a, [b]))
    env = session.read(request)
    assert env["code"] == "EXTRACTED"
    enforce(env)
    assert a.call_count == b.call_count == 2
    assert env["status"] == "partial" and not env["coverage"]["complete"]
    assert env["result_kind"] == "deterministic_extraction"
    assert env["provenance"]["derived"] is False
    assert env["provenance"]["attribution_status"] == "not_applicable"
    assert env["provenance"]["attempts_started"] == 4
    assert env["answer"] == "" and env["citations"] == []
    assert "Escape hatch: exact deterministic fallback extraction" in env["guidance"]
    assert "not an LLM summary" in env["guidance"] and "MODEL_ERROR" in env["guidance"]
    segment = env["extraction"]["segments"][0]
    assert segment["kind"] == "bytes" and segment["start"] == 0
    assert segment["text"].encode() == body.encode()[: segment["end"]]
    assert 0 < env["extraction"]["result_bytes"] <= 2048
    assert env["sources"][0]["snapshot_id"] == entry.snapshot.snapshot_id
    assert env["recovery"]["handles_valid"] is True
    assert "PRIVATE_BODY" not in json.dumps(env) and "TAIL_CANARY" not in json.dumps(env)
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
    assert env["code"] == "MODEL_ERROR" and "extraction" not in env
    assert "INSPECT_HANDLE" in env["recovery"]["actions"]


def test_disclosure_exhausted_returns_original_failure(tmp_path):
    session, _, request, _ = setup(tmp_path, limits={"disclosure_max_per_source_bytes": 32})
    first = session.read(request)
    assert first["extraction"]["result_bytes"] <= 32
    second = session.read(request)
    assert second["code"] == "MODEL_ERROR" and "extraction" not in second


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
    assert env["code"] == "EXTRACTED" and len(env["sources"]) == 2
    assert {o["source_id"] for o in env["coverage"]["omitted"]} == {
        s["source_id"] for s in request["sources"]
    }
    assert "SECOND_SOURCE_CANARY" not in json.dumps(env)


@pytest.mark.parametrize(
    "reply",
    [
        "not JSON",
        '{"claims": [], "citations": []}',
        '{"answer":"weak answer", "citations": []}',
        '{"answer":"Wrong [c1].", "citations":[{"id":"c1","line_start":1,"line_end":1,"quote":"missing"}]}',
        ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", False),
        ShuntError("CANCELLED"),
    ],
)
def test_nonavailability_never_extracts(tmp_path, reply):
    session, _, request, _ = setup(tmp_path, FakeLuna(default_reply=reply))
    assert "extraction" not in session.read(request)


def test_format_failure_then_outage_does_not_extract(tmp_path):
    session, _, request, _ = setup(
        tmp_path, FakeLuna(replies=["bad"], default_reply=TransientProviderError())
    )
    assert "extraction" not in session.read(request)


def test_actual_call_timeout_can_extract(tmp_path):
    def slow(_):
        time.sleep(0.15)
        return '{"answer":"", "citations":[]}'

    session, _, request, _ = setup(
        tmp_path, FakeLuna(default_reply=slow), limits={"model_call_deadline_ms": 10}
    )
    env = session.read(request)
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
    assert env["code"] == "MODEL_ERROR" and "extraction" not in env
    assert "PRIVATE_STORE_BODY" not in json.dumps(env)
    assert env["recovery"]["handles_valid"] is False


@pytest.mark.parametrize("value", [0, 4097, True, 2.5, "2048", None])
def test_bad_config_rejected(tmp_path, value):
    with pytest.raises(ShuntError):
        make_config(tmp_path, reader={"fallback_max_bytes": value})


def test_config_and_request_cap(tmp_path):
    session, _, request, _ = setup(tmp_path, reader={"fallback_max_bytes": 100})
    request["budgets"]["max_answer_bytes"] = 50
    env = session.read(request)
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
    env = session.read(request)
    if case["expected"] is None:
        assert env["code"] == "MODEL_ERROR" and "extraction" not in env
    else:
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
    env = session.read(request)
    assert 0 < env["extraction"]["result_bytes"] < len(body.encode())
    assert env["extraction"]["complete"] is False


def test_guard_refusal_does_not_consume_disclosure(tmp_path):
    session, entry, request, _ = setup(tmp_path, limits={"max_extended_envelope_bytes": 2048})
    before = session.store.disclosure_allowance(session.identity, entry.source_id).remaining
    env = session.read(request)
    assert env["code"] == "MODEL_ERROR" and "extraction" not in env
    assert session.store.disclosure_allowance(session.identity, entry.source_id).remaining == before


def test_request_timeout_without_response_extracts(tmp_path):
    def slow(_):
        time.sleep(0.15)
        return '{"answer":"", "citations":[]}'

    session, _, request, _ = setup(tmp_path, FakeLuna(default_reply=slow))
    request["budgets"]["deadline_ms"] = 20
    env = session.read(request)
    assert env["code"] == "EXTRACTED" and "TIMEOUT" in env["guidance"]


def test_session_disclosure_exhausted(tmp_path):
    session, entry, request, _ = setup(tmp_path, limits={"disclosure_max_per_session_bytes": 32})
    assert session.store.charge_disclosure(session.identity, entry.source_id, "bytes", 32).granted
    env = session.read(request)
    assert env["code"] == "MODEL_ERROR" and "extraction" not in env


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
    assert env["code"] == "EXTRACTED"
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
    env = session.read(request)
    assert env["code"] == "MODEL_ERROR" and "extraction" not in env
    assert secret_marker not in json.dumps(env)
    assert session.store.disclosure_allowance(session.identity, entry.source_id).remaining == before
