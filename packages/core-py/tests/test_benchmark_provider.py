"""benchmark provider: the half of the benchmark that needs a real model.

``benchmark core`` measures everything that needs no provider. This module measures the
two things that do - reader latency and reader token cost - and it measures them against a
live ``gpt-5.6-luna`` or not at all. Like :mod:`test_eval_luna` it refuses to run without
a bridge, because a latency number from a mock describes the mock.

What it asserts, and why those are the right assertions
-------------------------------------------------------
A performance gate that fails on a slow afternoon is noise, so the latency assertion is
deliberately loose: every call must finish inside the reader's own configured deadline.
That catches a route that is broken or hanging - the failure mode worth gating on -
without pretending a wall-clock threshold is a property of the software.

Latency is only meaningful over calls that *worked*, though. This gate used to ask only
whether an attempt had started, so a route where every single call failed satisfied it
completely and published healthy percentiles for a sequence of errors. Every sample is an
answerable item on a route that is supposed to answer, so each one must come back an
acceptable outcome, and an all-failing provider now fails.

The assertion that carries real weight is the accounting one. This is the only gate where
the token columns meet a *live* provider, so it is the only place that can prove against
a real host - rather than a fixture - that an unreported usage is never passed off as
provider truth.

What that means concretely is set by :class:`ReaderCost` and gated by
``test_gate_accounting``: a route that reports no usage yields a *named deterministic
estimate* (``bytes_div_4``) computed from the bytes this core itself sent and received,
never ``exact``, and ``cache_tokens`` - which nothing estimated - stays null. ``None`` is
reserved for the different fact "no attempt was made". This module previously asserted
``input_tokens is None`` for an unreported route, which contradicts that design; the
assertion had never executed, because the gate was hardcoded to NOT_RUN until the
prerequisite was actually consulted, so the contradiction went unnoticed.

The invariant worth gating on is therefore: the method column must tell the truth about
where the number came from. A zero, or an ``exact`` label over an estimate, means the
accounting layer is laundering absence into a measurement and every savings figure
downstream is suspect.

Cost is reported, never asserted: what a call costs is a property of the host's routing
and pricing, not of this project.
"""

from __future__ import annotations

import importlib
import json
import os
import statistics
import time
from pathlib import Path

import pytest

from context_shunt.binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from context_shunt.limits import DEFAULT_LIMITS, READER_MODEL
from context_shunt.provenance import TokenMethod
from context_shunt.provider import HostBridgeProvider
from context_shunt.reader import Reader
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes
from context_shunt.store import ScopeIdentity, SnapshotStore

pytestmark = pytest.mark.benchmark_provider

REPO = Path(__file__).resolve().parents[3]
CORPUS_PATH = REPO / "evals" / "luna-corpus.json"
BRIDGE_ENV = "CONTEXT_SHUNT_LUNA_BRIDGE"
ENABLE_ENV = "CONTEXT_SHUNT_LUNA_EVAL"

#: One answerable item per category. Enough samples for a percentile that means something,
#: few enough that the gate does not spend the corpus - `eval luna` is where the whole
#: corpus is scored.
SAMPLES_PER_CATEGORY = 2


def _load_bridge():
    """Resolve ``module:callable`` from the environment. Never a fallback, never a mock."""
    spec = os.environ.get(BRIDGE_ENV, "")
    if not spec or ":" not in spec:
        pytest.fail(
            f"benchmark provider requires a live bridge: set {BRIDGE_ENV}=module:callable "
            f"serving {READER_MODEL}. A mock is not a measurement."
        )
    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def _sample_items(corpus: dict) -> list[dict]:
    chosen: list[dict] = []
    seen: dict[str, int] = {}
    for item in corpus["items"]:
        if not item["answerable"]:
            continue
        taken = seen.get(item["category"], 0)
        if taken >= SAMPLES_PER_CATEGORY:
            continue
        seen[item["category"]] = taken + 1
        chosen.append(item)
    return chosen


def _summed(samples: list[dict], field: str) -> int | None:
    """Sum a token column, or null if any sample is missing it. Never a partial total."""
    values = [s[field] for s in samples]
    if any(value is None for value in values):
        return None
    return sum(values)


def _registry(root: Path) -> SourceRegistry:
    identity = ScopeIdentity(
        host="bench", profile="luna", principal="local", session="bench", generation=1
    )
    store = SnapshotStore(root, DEFAULT_LIMITS)
    store.open_scope(identity)
    return SourceRegistry(store, identity, DEFAULT_LIMITS)


@pytest.mark.skipif(
    not os.environ.get(ENABLE_ENV),
    reason=f"set {ENABLE_ENV}=1 with a live {READER_MODEL} bridge; scripts/verify reports NOT_RUN",
)
def test_reader_latency_and_cost_against_live_luna(tmp_path):
    corpus = json.loads(CORPUS_PATH.read_text())
    provider = HostBridgeProvider(_load_bridge(), DEFAULT_LIMITS, READER_MODEL)
    deadline_ms = DEFAULT_LIMITS.request_deadline_ms

    samples: list[dict] = []
    for index, item in enumerate(_sample_items(corpus)):
        registry = _registry(tmp_path / f"s{index}")
        media = JSON_MEDIA_TYPE if item["media_type"] == "application/json" else TEXT_MEDIA_TYPE
        entry = registry.register(
            "bench", snapshot_bytes(item["content"].encode("utf-8"), media_type_hint=media)
        )
        started = time.monotonic()
        result = Reader(registry, provider).answer(
            "bench",
            {
                "schema_version": "1.0",
                "request_id": f"req_bench_{item['id']}",
                "operation": "read",
                "question": item["question"],
                "sources": [
                    {
                        "source_id": entry.source_id,
                        "snapshot_id": entry.snapshot.snapshot_id,
                        "selector": {"kind": "all"},
                    }
                ],
                "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": deadline_ms},
            },
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        cost = result.cost
        samples.append(
            {
                "category": item["category"],
                "latency_ms": elapsed_ms,
                "status": result.envelope["status"],
                "code": result.envelope["code"],
                "attempts_started": cost.attempts_started,
                "attempts_usage_complete": cost.attempts_usage_complete,
                "input_tokens": cost.input_tokens,
                "output_tokens": cost.output_tokens,
                "cache_tokens": cost.cache_tokens,
                "token_method": cost.method.value,
            }
        )

    assert samples, "the benchmark must measure something"

    # An *attempt* is not a measurement. Every sample here is an answerable item on a
    # route that is supposed to work, so a sample that came back an error describes a
    # broken route, not a slow one - and a route where every call fails used to satisfy
    # this gate completely, because it only ever asked whether an attempt had started.
    acceptable = [s for s in samples if s["status"] != "error"]
    latencies = sorted(s["latency_ms"] for s in samples)
    p50 = statistics.median(latencies)
    p95 = latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))]

    # Which token columns the host actually filled in, as a fact about the route.
    reported = [s for s in samples if s["attempts_usage_complete"] > 0]
    usage_exposed = len(reported) == len(samples) and all(
        s["input_tokens"] is not None for s in reported
    )

    report = {
        "model": READER_MODEL,
        "corpus_sha256": __import__("hashlib").sha256(CORPUS_PATH.read_bytes()).hexdigest(),
        "samples": len(samples),
        "latency_ms_p50": p50,
        "latency_ms_p95": p95,
        "latency_ms_max": latencies[-1],
        "reader_deadline_ms": deadline_ms,
        "usage_exposed_by_host": usage_exposed,
        # Null, not zero, when the host reports nothing. The reason travels with it so a
        # reader of the report cannot mistake absence for a measured zero.
        "input_tokens_total": (sum(s["input_tokens"] for s in samples) if usage_exposed else None),
        "output_tokens_total": (
            sum(s["output_tokens"] for s in samples) if usage_exposed else None
        ),
        "token_cost_unmeasured_reason": (
            None if usage_exposed else "host route reports no usage; absence is not zero"
        ),
        # The host-reported totals above stay null. These are this core's own byte-based
        # estimate, carried separately and with its method attached, so a reader of the
        # report can never mistake the two for each other.
        "estimated_input_tokens_total": _summed(samples, "input_tokens"),
        "estimated_output_tokens_total": _summed(samples, "output_tokens"),
        "token_estimate_methods": sorted({s["token_method"] for s in samples}),
        "acceptable_outcomes": len(acceptable),
        "failed_outcomes": len(samples) - len(acceptable),
        "attempts_started_total": sum(s["attempts_started"] for s in samples),
        "attempts_usage_complete_total": sum(s["attempts_usage_complete"] for s in samples),
        "by_category": {
            s["category"]: {
                "latency_ms": s["latency_ms"],
                "status": s["status"],
                "code": s["code"],
            }
            for s in samples
        },
    }
    reports = REPO / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "benchmark-provider.json").write_text(json.dumps(report, indent=2) + "\n")

    # A route that hangs is the failure worth gating on; a slow one is not a defect.
    assert latencies[-1] <= deadline_ms, report

    # A benchmark of a route that cannot answer is not a benchmark. Every sample must be
    # an acceptable outcome, so an all-failing provider fails this gate instead of
    # reporting healthy latency percentiles for a sequence of errors.
    assert len(acceptable) == len(samples), report

    # The assertion this gate exists for: against a live route, the method column must say
    # where every number came from, and an unreported usage must never be labelled exact.
    for sample in samples:
        assert sample["attempts_started"] > 0, report
        # Accounting must cover every attempt the request actually started, retries and
        # fallback attempts included - never more than were started, and never a partial
        # tally silently labelled complete.
        assert 0 <= sample["attempts_usage_complete"] <= sample["attempts_started"], report
        if sample["attempts_usage_complete"] < sample["attempts_started"]:
            # Unreported: a named estimate this core can reproduce from its own bytes,
            # never provider truth, and never a zero standing in for the unknown.
            assert sample["token_method"] == TokenMethod.BYTES_DIV_4.value, report
            assert isinstance(sample["input_tokens"], int) and sample["input_tokens"] > 0, report
            assert isinstance(sample["output_tokens"], int), report
            # Nothing estimates a cache hit, so this one really does stay null.
            assert sample["cache_tokens"] is None, report
        else:
            assert isinstance(sample["input_tokens"], int), report
            assert sample["token_method"] == TokenMethod.EXACT.value, report
