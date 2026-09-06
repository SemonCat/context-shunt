"""benchmark core: the deterministic half of the benchmark gate.

Everything measured here needs no provider: gate latency, envelope sizes, main-context
byte savings against a full direct-read baseline, and the incremental peak RSS of refusing
an oversized source. Reader latency and token cost are the provider half and are reported
NOT_RUN by ``scripts/verify benchmark all`` until live Luna access exists - they are never
estimated and never passed off as measured.

Payload bytes are generated in the test and never written to a report.
"""

from __future__ import annotations

import json
import resource
import sys
import time
from pathlib import Path

import pytest

from context_shunt import envelope as E
from context_shunt.gate import PreReadGate
from context_shunt.guard import enforce
from context_shunt.limits import DEFAULT_LIMITS as L
from context_shunt.probe import FileProber
from context_shunt.session import ShuntSession
from context_shunt.snapshot import snapshot_bytes
from context_shunt.spill import SpillEngine
from tests.support import make_capability, make_config

pytestmark = pytest.mark.benchmark

RUNS = 30
MIB = 1024 * 1024


def _write_lines(path: Path, lines: int, width: int = 60) -> int:
    body = "".join(f"{'x' * width} line {i}\n" for i in range(lines))
    path.write_text(body)
    return len(body.encode("utf-8"))


def _write_bytes(path: Path, total: int) -> None:
    block = ("y" * 127 + "\n").encode("utf-8")
    with path.open("wb") as fh:
        written = 0
        while written < total:
            chunk = block if written + len(block) <= total else block[: total - written]
            fh.write(chunk)
            written += len(chunk)


def _percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def _rss_bytes() -> int:
    """Peak RSS in bytes.

    ``ru_maxrss`` is bytes on Darwin and kilobytes on Linux. The unit has to come from the
    platform, not from the magnitude: guessing by size silently switches units partway
    through a run and turns a real measurement into a meaningless one.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage if sys.platform == "darwin" else usage * 1024


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("bench")
    paths = {}
    for name, lines in (("l350", 350), ("l351", 351)):
        paths[name] = root / f"{name}.txt"
        _write_lines(paths[name], lines, width=8)
    for name, size in (("b64k", 64 * 1024), ("b1m", MIB), ("b8m", 8 * MIB)):
        paths[name] = root / f"{name}.txt"
        _write_bytes(paths[name], size)
    paths["over8m"] = root / "over8m.txt"
    _write_bytes(paths["over8m"], 8 * MIB + 4096)
    paths["longline"] = root / "longline.txt"
    paths["longline"].write_text("Z" * (2 * MIB) + "\n")
    return paths


def test_gate_latency_p95_under_one_second(corpus):
    gate = PreReadGate(FileProber())
    samples: list[float] = []
    for _ in range(RUNS):
        for path in corpus.values():
            started = time.perf_counter()
            gate.evaluate("read", {"file_path": str(path)})
            samples.append(time.perf_counter() - started)
    p95 = _percentile(samples, 95)
    assert p95 <= L.gate_probe_deadline_ms / 1000.0, f"gate p95 {p95:.4f}s"
    assert _percentile(samples, 50) <= p95


def test_spill_latency_p95_under_five_seconds(tmp_path):
    """Publication cost, including fsync and the SQLite commit, stays inside the budget."""
    from tests.support import make_registry

    samples: list[float] = []
    for i in range(RUNS):
        registry = make_registry(tmp_path / f"run{i}", session_id=f"sess{i}")
        engine = SpillEngine(registry, enabled=True)
        # Distinct bytes per run: content addressing would otherwise dedupe away the write.
        payload = f"s{i}-" + "s" * (2 * MIB)
        started = time.perf_counter()
        outcome = engine.evaluate(f"sess{i}", "req_b", payload)
        samples.append(time.perf_counter() - started)
        assert outcome.action == "spill"
    p95 = _percentile(samples, 95)
    assert p95 <= L.spill_io_deadline_ms / 1000.0, f"spill p95 {p95:.4f}s"


def test_every_shunt_envelope_is_under_the_cap(corpus, tmp_path):
    config = make_config(tmp_path)
    ws = tmp_path / "ws"
    session = ShuntSession("bench", config, make_capability())
    for name, path in corpus.items():
        target = ws / f"{name}.txt"
        target.write_bytes(path.read_bytes()[: 4 * MIB])
        decision = session.evaluate_tool_call("read", {"file_path": str(target)})
        if decision.blocked:
            envelope = session.block_envelope("req_bench", decision)
            assert E.serialized_bytes(enforce(envelope)) <= L.max_envelope_bytes
    session.close()


@pytest.mark.parametrize("name", ["b64k", "b1m", "b8m", "longline"])
def test_main_context_bytes_drop_at_least_75_percent(corpus, tmp_path, name):
    """Compared with a full direct read, the blocked path costs the envelope only.

    A host that truncates on its own has a smaller baseline; that baseline is reported
    separately by the caller and never mixed with this one.
    """
    config = make_config(tmp_path)
    target = tmp_path / "ws" / f"{name}.txt"
    target.write_bytes(corpus[name].read_bytes())
    direct_read_bytes = target.stat().st_size
    assert direct_read_bytes >= 64 * 1024

    session = ShuntSession("bench", config, make_capability())
    decision = session.evaluate_tool_call("read", {"file_path": str(target)})
    assert decision.blocked
    shunt_bytes = E.serialized_bytes(session.block_envelope("req_bench", decision))
    session.close()

    reduction = 1.0 - (shunt_bytes / direct_read_bytes)
    assert reduction >= 0.75, f"{name}: only {reduction:.3%} reduction"
    assert shunt_bytes <= L.max_envelope_bytes


@pytest.mark.parametrize("size_mib", [16, 64, 256])
def test_oversized_source_refusal_is_bounded_in_memory(tmp_path, size_mib):
    path = tmp_path / f"huge{size_mib}.txt"
    _write_bytes(path, size_mib * MIB)
    baseline = _rss_bytes()

    gate = PreReadGate(FileProber())
    decision = gate.evaluate("read", {"file_path": str(path)})
    assert decision.blocked and decision.code == "LARGE_READ"

    from context_shunt.errors import ShuntError
    from context_shunt.paths import PathPolicy, authorize
    from context_shunt.snapshot import snapshot_file

    if size_mib * MIB > L.max_source_bytes:
        with pytest.raises(ShuntError) as exc:
            snapshot_file(authorize(str(path), PathPolicy.from_config([str(tmp_path)])))
        assert exc.value.code == "LIMIT_EXCEEDED"

    growth = max(0, _rss_bytes() - baseline)
    assert growth <= 32 * MIB, f"{size_mib} MiB source grew RSS by {growth / MIB:.1f} MiB"


def test_request_stays_inside_the_per_request_caps(tmp_path):
    from tests.support import FakeLuna, answer_json, make_registry

    body = "".join(f"key{i} = value{i}\n" for i in range(20000))
    registry = make_registry(tmp_path, session_id="bench")
    entry = registry.register("bench", snapshot_bytes(body.encode()))
    luna = FakeLuna(default_reply=answer_json("", []))
    from context_shunt.reader import Reader

    env = (
        Reader(registry, luna)
        .answer(
            "bench",
            {
                "schema_version": "1.0",
                "request_id": "req_bench",
                "operation": "read",
                "question": "Which keys are configured?",
                "sources": [
                    {
                        "source_id": entry.source_id,
                        "snapshot_id": entry.snapshot.snapshot_id,
                        "selector": {"kind": "all"},
                    }
                ],
                "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
            },
        )
        .envelope
    )
    assert luna.call_count <= L.max_chunks_per_request
    assert env["coverage"]["planned_chunks"] <= L.max_chunks_per_request
    assert E.serialized_bytes(env) <= L.max_envelope_bytes
    estimated_input = (
        sum(len(c.user.encode("utf-8")) for c in luna.calls) // L.bytes_per_token_estimate
    )
    assert estimated_input <= L.max_request_input_tokens


def test_bounded_terminal_response_arrives_within_a_second_of_the_deadline(tmp_path):
    from context_shunt.clock import Deadline, FakeClock
    from context_shunt.provenance import ModelIdentity, Usage
    from context_shunt.provider import ModelResponse
    from context_shunt.reader import Reader
    from tests.support import make_registry

    clock = FakeClock()
    registry = make_registry(tmp_path, session_id="bench")
    entry = registry.register("bench", snapshot_bytes(b"alpha\n"))

    class Overrun:
        def complete(self, **_kw):
            clock.advance(L.request_deadline_ms + 500)
            return ModelResponse(
                text="{}",
                requested=ModelIdentity(model=L.reader_model),
                usage=Usage(),
            )

    deadline = Deadline.start(clock, L.request_deadline_ms)
    started = time.perf_counter()
    env = (
        Reader(registry, Overrun(), clock=clock)
        .answer(
            "bench",
            {
                "schema_version": "1.0",
                "request_id": "req_bench",
                "operation": "read",
                "question": "What does it say?",
                "sources": [
                    {
                        "source_id": entry.source_id,
                        "snapshot_id": entry.snapshot.snapshot_id,
                        "selector": {"kind": "all"},
                    }
                ],
                "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
            },
            deadline=deadline,
        )
        .envelope
    )
    wall = time.perf_counter() - started
    assert wall < 1.0
    assert json.loads(json.dumps(env))["coverage"]["complete"] is False
    assert clock.now_ms() - deadline.started_ms >= L.request_deadline_ms
