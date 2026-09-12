"""Availability is an invariant, including when the deprecated switch is false."""

import pytest

from context_shunt.errors import ShuntError
from context_shunt.guard import enforce
from context_shunt.legacy_compact import compact_tool_result
from tests.support import FakeLuna
from tests.test_gate_legacy_compact_fallback import _no_evidence_luna, setup

pytestmark = pytest.mark.gate_legacy_compact


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "code", ["MODEL_ERROR", "TIMEOUT", "INVALID_MODEL_OUTPUT", "CITATION_INVALID"]
)
def test_owned_reader_failure_is_always_legacy(tmp_path, enabled, code):
    provider = (
        _no_evidence_luna()
        if code == "CITATION_INVALID"
        else FakeLuna(default_reply=ShuntError(code))
    )
    session, _, request, body = setup(tmp_path, provider, reader={"legacy_compaction": enabled})
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["original_failure"] == code
    assert env["legacy_compaction"]["summary"] == compact_tool_result(body, hard_chars=16000)
    assert not env["coverage"]["complete"]
    assert not env["provenance"]["derived"]
    enforce(env)


@pytest.mark.parametrize(
    "detail", ["NO_VALID_EVIDENCE", "MARKER_NOT_PUBLISHED", "STORE_BYTE_QUOTA"]
)
def test_fixed_failure_detail_survives_envelope(detail):
    from context_shunt.envelope import error_envelope

    env = error_envelope("req_detail", ShuntError("CITATION_INVALID", detail))
    assert env["failure_detail"] == detail
    enforce(env)


def test_arbitrary_enum_shaped_detail_is_not_published():
    from context_shunt.envelope import error_envelope

    env = error_envelope("req_detail", ShuntError("MODEL_ERROR", "PRIVATE_CUSTOMER_12345"))
    assert env["failure_detail"] == "OTHER"
    assert "PRIVATE_CUSTOMER" not in str(env)


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("TOOL_ARGS_VIOLATION", {"handles_valid": True, "actions": ["NONE"]}),
        (
            "INVALID_SNAPSHOT_ID",
            {"handles_valid": False, "actions": ["REUSE_POINTER_PAIR"]},
        ),
    ],
)
def test_invalid_request_recovery_distinguishes_schema_from_snapshot(detail, expected):
    from context_shunt.envelope import error_envelope

    env = error_envelope("req_invalid", ShuntError("INVALID_REQUEST", detail))
    assert env["failure_detail"] == detail
    assert env["recovery"] == expected
    if detail == "INVALID_SNAPSHOT_ID":
        assert "exact source_id/snapshot_id pair" in env["guidance"]
    else:
        assert "exact source_id/snapshot_id pair" not in env["guidance"]
        assert "tool argument schema" in env["guidance"]


@pytest.mark.parametrize(
    "failure",
    [
        ShuntError("STORE_FAILED", "WRITE_FAILED"),
        ShuntError("LIMIT_EXCEEDED", "STORE_BYTE_QUOTA"),
        RuntimeError("PRIVATE_PAYLOAD"),
    ],
)
def test_capture_internal_failure_has_legacy_without_fake_handle(tmp_path, monkeypatch, failure):
    from context_shunt.spill import SpillEngine

    session, _, _, body = setup(tmp_path, body="row ERROR status 500\n" * 2000)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(session._registry, "register", fail)
    outcome = SpillEngine(session._registry, enabled=True).evaluate("sess", "req_capture", body)
    env = outcome.envelope
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["sources"] == [] and not env["recovery"]["handles_valid"]
    assert "source_id" not in env["legacy_compaction"]
    assert env["legacy_compaction"]["summary"] == compact_tool_result(body, hard_chars=16000)
    assert "PRIVATE_PAYLOAD" not in str(env)
    enforce(env)


@pytest.mark.parametrize(
    "failure",
    [ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_WIRE_BUDGET"), RuntimeError("PRIVATE_INSPECT")],
)
def test_inspect_owned_failure_compacts_and_charges(tmp_path, monkeypatch, failure):
    session, entry, _, body = setup(tmp_path)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(session._inspector, "extract", fail)
    request = {
        "schema_version": "1.1",
        "request_id": "req_inspect",
        "operation": "inspect",
        "source_id": entry.source_id,
        "snapshot_id": entry.snapshot.snapshot_id,
        "selector": {"kind": "lines", "start": 1, "end": 2},
        "budgets": {"max_result_bytes": 4096, "max_scan_lines": 20000},
    }
    before = session._store.disclosure_allowance(session._identity, entry.source_id).remaining
    env = session.inspect(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert (
        env["legacy_compaction"]["summary"]
        == compact_tool_result(body, hard_chars=16000).encode()[:4096].decode()
    )
    after = session._store.disclosure_allowance(session._identity, entry.source_id).remaining
    assert before - after == env["legacy_compaction"]["summary_bytes"]
    enforce(env)


@pytest.mark.parametrize(
    "code,detail",
    [
        ("INVALID_REQUEST", "TOOL_ARGS_VIOLATION"),
        ("UNSUPPORTED_VERSION", None),
        ("UNSAFE_SOURCE", None),
        ("BINARY_UNSUPPORTED", None),
        ("SOURCE_CHANGED", "SNAPSHOT_MISMATCH"),
        ("SOURCE_EXPIRED", "UNKNOWN_HANDLE"),
        ("PROVENANCE_UNAVAILABLE", None),
        ("MODEL_ERROR", "MODEL_SUBSTITUTED"),
        ("DISCLOSURE_EXHAUSTED", None),
        ("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP"),
        ("LIMIT_EXCEEDED", "REQUEST_OVER_TOKEN_CAP"),
        ("LIMIT_EXCEEDED", "UNKNOWN_CAP"),
        ("STORE_FAILED", "BLOB_CONTENT_MISMATCH"),
        ("HOST_UNSAFE", None),
        ("CANCELLED", None),
    ],
)
def test_policy_boundary_is_not_legacy(code, detail):
    from context_shunt.errors import fallback_allowed
    from context_shunt.fallback import compact_failure

    assert not fallback_allowed(code, detail)
    env = compact_failure("req_refused", b"innocent source", ShuntError(code, detail))
    assert env["code"] == code and "legacy_compaction" not in env
    enforce(env)


def test_unexpected_reader_internal_error_is_legacy(tmp_path, monkeypatch):
    session, _, request, _ = setup(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_READER_ERROR")

    monkeypatch.setattr(session._reader, "answer", fail)
    env = session.read(request)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["failure_detail"] == "INTERNAL_ERROR"
    assert "PRIVATE_READER_ERROR" not in str(env)
    request["sources"][0]["snapshot_id"] = "sha256:" + "a" * 71
    env = session.read(request)
    assert env["code"] == "INVALID_REQUEST"
    assert not env["recovery"]["handles_valid"]


def test_incumbent_golden_outputs(contracts_dir):
    import json

    cases = json.loads((contracts_dir / "conformance/legacy-golden.json").read_text())["cases"]
    for case in cases:
        text = (
            case["prefix"]
            + case["separator"].join([case["unit"]] * case["repeat"])
            + case["suffix"]
        )
        assert compact_tool_result(text, hard_chars=case["hard_chars"]) == case["expected"], case[
            "id"
        ]


@pytest.mark.parametrize("cap", [1, 10, 64, 100, 1000])
def test_incumbent_small_caps_are_hard_bounds(cap):
    assert len(compact_tool_result("ordinary row\n" * 500, hard_chars=cap)) <= cap


def test_capture_secret_policy_is_not_bypassed():
    from context_shunt.fallback import compact_failure

    data = ("aws_secret_access_key=" + "a" * 40 + "\n") * 1000
    env = compact_failure("req_secret", data.encode(), ShuntError("STORE_FAILED", "WRITE_FAILED"))
    assert env["code"] == "UNSAFE_SOURCE"
    assert "legacy_compaction" not in env
    assert "a" * 40 not in str(env)


def test_deep_structured_text_and_escaped_output_are_bounded():
    from context_shunt.fallback import compact_failure

    for text in ["[" * 2000 + "0" + "]" * 2000, ('\\"\t\n' * 10000)]:
        env = compact_failure("req_deep", text.encode(), ShuntError("STORE_FAILED", "WRITE_FAILED"))
        assert env["code"] == "LEGACY_COMPACTED"
        enforce(env)


def test_initial_path_capture_store_failure_retains_authorized_bytes(tmp_path, monkeypatch):
    session, _, _, body = setup(tmp_path)

    def fail(*args, **kwargs):
        raise ShuntError("STORE_FAILED", "WRITE_FAILED")

    monkeypatch.setattr(session._registry, "register_batch", fail)
    env = session.capture_read_paths("req_path", [str(tmp_path / "ws" / "source.txt")])
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["summary"] == compact_tool_result(body, hard_chars=16000)
    secret = tmp_path / "ws" / ".env"
    secret.write_text("ordinary looking text")
    with pytest.raises(ShuntError) as refused:
        session.capture_read_paths("req_path", [str(tmp_path / "ws" / "source.txt"), str(secret)])
    assert refused.value.code == "UNSAFE_SOURCE"


def test_reader_fallback_obeys_disclosure_exhaustion(tmp_path):
    session, entry, request, _ = setup(tmp_path, FakeLuna(default_reply=ShuntError("MODEL_ERROR")))
    allowance = session._store.disclosure_allowance(session._identity, entry.source_id)
    session._store.charge_disclosure(
        session._identity, entry.source_id, "bytes", allowance.remaining
    )
    env = session.read(request)
    assert env["code"] == "DISCLOSURE_EXHAUSTED"
    assert "legacy_compaction" not in env


def test_safe_detail_enum_matches_canonical_contract(contracts_dir):
    import json

    from context_shunt.errors import SAFE_FAILURE_DETAILS

    schema = json.loads((contracts_dir / "envelope.schema.json").read_text())
    assert set(schema["properties"]["failure_detail"]["enum"]) == SAFE_FAILURE_DETAILS


@pytest.mark.parametrize(
    "selector", [{"kind": "lines", "start": 2, "end": 1}, {"kind": "bytes", "start": 1, "end": 2}]
)
def test_inspect_internal_error_cannot_bypass_invalid_selector(tmp_path, monkeypatch, selector):
    session, entry, _, _ = setup(tmp_path, body="é\n")

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_INSPECT")

    monkeypatch.setattr(session._inspector, "extract", fail)
    env = session.inspect(
        {
            "schema_version": "1.1",
            "request_id": "req_invalid",
            "operation": "inspect",
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "selector": selector,
            "budgets": {"max_result_bytes": 4096, "max_scan_lines": 20000},
        }
    )
    assert env["code"] == "INVALID_REQUEST"
    assert "legacy_compaction" not in env
