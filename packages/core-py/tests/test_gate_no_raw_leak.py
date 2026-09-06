"""unit no-raw-leak: intercepted payload must not appear anywhere the agent can see.

Unique sentinels are planted in non-quoted regions of the source, then every failure
path is injected. The sentinel count in the envelope, in metrics, in logs and in every
exception message must be zero.
"""

from __future__ import annotations

import json
import logging

import pytest

from context_shunt.errors import ShuntError
from context_shunt.guard import enforce_or_fixed
from context_shunt.metrics import InMemoryMetrics
from context_shunt.provider import HostBridgeProvider, TransientProviderError
from context_shunt.reader import Reader
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes
from context_shunt.spill import SpillStore, SumaSpillEngine
from tests.support import FakeLuna, answer_json

pytestmark = pytest.mark.gate_no_raw_leak

HEAD = "SENTINEL-HEAD-9f2a1c"
MID = "SENTINEL-MID-4b7e33"
TAIL = "SENTINEL-TAIL-c81d05"
SENTINELS = (HEAD, MID, TAIL)


def _payload(size: int = 60000) -> str:
    filler = "".join(f"filler line {i}\n" for i in range(size // 20))
    third = len(filler) // 3
    return HEAD + "\n" + filler[:third] + MID + "\n" + filler[third:] + TAIL + "\n"


def _assert_clean(*blobs: str):
    for blob in blobs:
        for sentinel in SENTINELS:
            assert sentinel not in blob, f"{sentinel} leaked"


def _request(entry, question="Summarize the configuration."):
    return {
        "schema_version": "1.0",
        "request_id": "req_leak",
        "operation": "read",
        "question": question,
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }


@pytest.mark.parametrize(
    "failure",
    [
        "model_error",
        "invalid_json",
        "bad_citation",
        "provider_exception_with_payload",
    ],
)
def test_reader_failures_never_return_raw(failure, caplog):
    caplog.set_level(logging.DEBUG)
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(_payload().encode()))
    metrics = InMemoryMetrics()

    if failure == "model_error":
        provider = FakeLuna(default_reply=TransientProviderError("PROVIDER_CALL_FAILED"))
    elif failure == "invalid_json":
        provider = FakeLuna(default_reply="not json " + MID)
    elif failure == "bad_citation":
        provider = FakeLuna(
            default_reply=answer_json(
                f"The file starts with {HEAD} [c1].",
                [{"id": "c1", "line_start": 99999, "line_end": 99999, "quote": HEAD}],
            )
        )
    else:

        def bridge(**_kw):
            raise RuntimeError(f"upstream rejected prompt containing {MID} and {TAIL}")

        provider = HostBridgeProvider(bridge)

    env = Reader(registry, provider, metrics=metrics).answer("sess", _request(entry))
    guarded = enforce_or_fixed(env)
    _assert_clean(json.dumps(guarded), metrics.rendered(), caplog.text)
    assert guarded["status"] in ("partial", "error", "ok")


def test_provider_error_body_is_dropped_at_the_boundary():
    def bridge(**_kw):
        raise RuntimeError(f"HTTP 500 body: {{'echo': '{HEAD}'}}")

    provider = HostBridgeProvider(bridge)
    with pytest.raises(ShuntError) as exc:
        provider.complete(system="s", user="u", max_output_tokens=10, timeout_ms=100)
    assert exc.value.code == "MODEL_ERROR"
    _assert_clean(str(exc.value), exc.value.safe_message(), repr(exc.value))


def test_error_details_cannot_carry_free_text():
    with pytest.raises(ValueError):
        ShuntError("MODEL_ERROR", f"failed on {HEAD}")


@pytest.mark.parametrize("inject", ["write_failure", "readback_mismatch", "quota"])
def test_spill_failures_never_return_raw(tmp_path, inject, caplog):
    caplog.set_level(logging.DEBUG)
    registry = SourceRegistry()
    store = SpillStore(tmp_path / "cache")
    if inject == "quota":
        store.seed_usage("sess", store._limits.session_spill_quota_bytes)  # noqa: SLF001
    else:
        detail = "WRITE_FAILED" if inject == "write_failure" else "READBACK_MISMATCH"

        def _boom(*_a, **_kw):
            raise ShuntError("SPILL_FAILED", detail, retryable=False)

        store.write = _boom
    engine = SumaSpillEngine(store, registry, enabled=True)
    outcome = engine.evaluate("sess", "req_leak", _payload())
    assert outcome.action == "error" and outcome.code == "SPILL_FAILED"
    _assert_clean(json.dumps(outcome.envelope), caplog.text)


def test_successful_spill_pointer_carries_no_payload(tmp_path):
    registry = SourceRegistry()
    engine = SumaSpillEngine(SpillStore(tmp_path / "cache"), registry, enabled=True)
    outcome = engine.evaluate("sess", "req_leak", _payload())
    assert outcome.action == "spill"
    _assert_clean(json.dumps(outcome.envelope))
    # The payload is still recoverable through the authorized private snapshot only.
    entry = registry.resolve("sess", outcome.envelope["pointer"]["source_id"])
    assert HEAD.encode() in entry.snapshot.data


def test_output_guard_failure_emits_a_fixed_envelope_not_the_input():
    poisoned = {
        "schema_version": "1.0",
        "request_id": "req_leak",
        "status": "ok",
        "code": "ANSWERED",
        "answer": HEAD,
        "citations": [],
        "coverage": {
            "complete": True,
            "processed_chunks": 1,
            "planned_chunks": 1,
            "omitted": [],
            "upstream_truncated": False,
        },
        "sources": [],
        "retryable": False,
        "raw": _payload(),
    }
    guarded = enforce_or_fixed(poisoned)
    _assert_clean(json.dumps(guarded))
    assert guarded["status"] == "error"


def test_gate_block_envelope_carries_no_source_content(tmp_path):
    from context_shunt.gate import PreReadGate
    from context_shunt.probe import FileProber
    from context_shunt.session import ShuntSession
    from tests.support import make_capability, make_config

    config = make_config(tmp_path)
    path = tmp_path / "ws" / "big.txt"
    path.write_text(_payload())
    session = ShuntSession("sess", config, make_capability())
    decision = session.evaluate_tool_call("read", {"file_path": str(path)})
    assert decision.blocked
    env = session.block_envelope("req_leak", decision)
    _assert_clean(json.dumps(env))
    assert str(path) not in json.dumps(env)
    assert isinstance(PreReadGate(FileProber()), PreReadGate)


def test_metric_labels_reject_content():
    from context_shunt.metrics import MetricsError

    metrics = InMemoryMetrics()
    # A path is not an allowed label key at all.
    with pytest.raises(MetricsError):
        metrics.count("gate_decision", {"path": HEAD})
    # An allowed key still rejects a value that is not a bounded enum token.
    with pytest.raises(MetricsError):
        metrics.count("gate_decision", {"reason": HEAD + " " + MID})
    # Full request ids are forbidden as dimensions even though they are not content.
    with pytest.raises(MetricsError):
        metrics.count("gate_decision", {"request_id": "req_leak"})
