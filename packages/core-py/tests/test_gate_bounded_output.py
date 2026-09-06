"""unit bounded-output: every reply and every spill decision respects the caps."""

from __future__ import annotations

import pytest

from context_shunt import envelope as E
from context_shunt.errors import ShuntError
from context_shunt.guard import OutputGuardError, enforce, enforce_or_fixed
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.reader import Reader
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes
from context_shunt.spill import SpillEngine
from context_shunt.store import SnapshotStore
from tests.support import FakeLuna, answer_json, derived_provenance, make_identity, make_registry

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
