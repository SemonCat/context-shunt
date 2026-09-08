"""shadow A/B: the artifact broker against the incumbent, with honest NOT_RUN.

What this measures
------------------
Four lanes over the same synthetic artifacts, defined in ``evals/shadow/corpus.json``:

``raw_baseline``
    the whole artifact into the main context. The counterfactual, and the evidence
    ceiling - recall is 1.0 because nothing was dropped.
``legacy_compact``
    a reference emulation of a heuristic head/tail compactor. The incumbent. It is
    implemented here, in the harness, precisely because it is *not* product code.
``deterministic_retrieval``
    import the artifact and answer with a literal search through the zero-model
    inspector. Needs no provider, so it always runs.
``reader``
    import the artifact and ask the question-aware reader. Provider-dependent.

What this refuses to measure
----------------------------
The reader lane needs a live bridge, and four of the eight gates depend on it. Those
gates report ``NOT_RUN`` rather than being scored from the deterministic lanes, because
each substitution would be a false pass:

* *task correctness* and *semantic support* need a lane that answers questions;
* *mechanical citation validity* would be a vacuous 1.0 against a lane that publishes
  exact extracts and no citations;
* *net cost reduction* needs a pricing table, and this repository has none - inventing
  one would fabricate the headline number.

The two gates that can be measured from the repository alone are measured for real:
main-context reduction, and no evidence regression against the raw baseline.

Fresh scope per item
--------------------
Every item gets its own session, store and cache root. The disclosure ceilings are
cumulative (256 KiB per source, 1 MiB per session), so paging a corpus of oversized
artifacts through one scope would exhaust the session ceiling partway through and turn
later items into ``DISCLOSURE_EXHAUSTED`` - a degraded score with no visible cause.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from context_shunt.accounting import estimate_tokens
from context_shunt.artifacts import NATIVE_IMPORT_CONTRACT
from context_shunt.config import load as load_config
from context_shunt.envelope import serialized_bytes
from context_shunt.limits import DEFAULT_LIMITS, EMITTED_SCHEMA_VERSION
from context_shunt.session import ShuntSession

from .support import make_capability

pytestmark = pytest.mark.shadow_ab

REPO = Path(__file__).resolve().parents[3]
CORPUS_PATH = REPO / "evals" / "shadow" / "corpus.json"
REPORT_DIR = REPO / "reports"

#: Same prerequisite the live Luna eval and the provider benchmark use. Without it the
#: reader lane is NOT_RUN, and so is every gate that depends on it.
READER_ENV = "CONTEXT_SHUNT_LUNA_EVAL"

NOT_RUN = "NOT_RUN"


def _corpus() -> dict[str, Any]:
    with CORPUS_PATH.open("rb") as fh:
        return json.load(fh)


def _corpus_hash() -> str:
    return hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest()


# -- deterministic artifact generation -------------------------------------


def _filler_line(shape: str, index: int) -> str:
    """One line of plausible, content-free filler. Deterministic in ``index``."""
    second = index % 60
    minute = (index // 60) % 60
    stamp = f"2026-09-01T{(index // 3600) % 24:02d}:{minute:02d}:{second:02d}Z"
    if shape == "log_json_lines":
        return (
            f'{{"ts":"{stamp}","level":"info","svc":"checkout",'
            f'"msg":"request handled","route":"/cart/{index % 97}","ms":{index % 250}}}'
        )
    if shape == "journal_text":
        return f"{stamp} checkout[{1000 + index % 900}]: handled request seq={index}"
    if shape == "tracker_json":
        return (
            f'    {{"key":"PROJ-{2000 + index}","status":"open",'
            f'"summary":"routine item {index}","assignee":"team-checkout"}}'
        )
    if shape == "wiki_text":
        return f"Step {index}: routine operational detail for the component, paragraph {index}."
    raise AssertionError(f"unknown shape: {shape}")


def build_body(item: dict[str, Any], limits=DEFAULT_LIMITS) -> bytes:
    """Generate one artifact. Same corpus, same bytes, on every machine."""
    shape = item["shape"]
    spec = item["body"]
    if spec.get("over_source_cap"):
        unit = _filler_line(shape, 0) + "\n"
        want = limits.max_source_bytes + 4096
        return (unit.encode("utf-8") * (want // len(unit) + 1))[:want]

    total = int(spec["lines"])
    needle_line = spec.get("needle_line")
    lines = [_filler_line(shape, index) for index in range(1, total + 1)]
    if needle_line is not None and item.get("expected_quote"):
        # The expectation has to be *in* the artifact, or no honest lane could find it.
        lines[needle_line - 1] = _needle_line(shape, item["expected_quote"], needle_line)
    if spec.get("inject_secret_marker"):
        lines[min(len(lines) - 1, 5)] = (
            "2026-09-01T00:00:05Z worker[1001]: startup aws_secret_access_key=loaded"
        )

    if shape == "tracker_json":
        body = '{"issues":[\n' + ",\n".join(lines) + "\n]}\n"
    else:
        body = "\n".join(lines) + "\n"
    return body.encode("utf-8")


def _needle_line(shape: str, quote: str, index: int) -> str:
    """Wrap the expected quote in a line of the right shape, so the shape stays honest."""
    stamp = f"2026-09-01T{(index // 3600) % 24:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z"
    if shape == "log_json_lines":
        return f'{{"ts":"{stamp}","level":"error",{quote}}}'
    if shape == "journal_text":
        return f"{stamp} {quote}"
    if shape == "tracker_json":
        return f'    {{"key":"PROJ-{3000 + index}",{quote}}}'
    if shape == "wiki_text":
        return quote
    raise AssertionError(f"unknown shape: {shape}")


# -- lane: the incumbent heuristic compactor -------------------------------


def legacy_compact(body: bytes, rules: dict[str, Any]) -> str:
    """A reference emulation of the incumbent, fixed by the corpus so it cannot drift.

    Head lines, tail lines, one elision marker, capped at a byte budget. Deterministic
    and model-free - the comparison is against the *shape* of heuristic compaction, not
    against any particular implementation of it.
    """
    lines = body.decode("utf-8", "replace").splitlines()
    head_n = int(rules["head_lines"])
    tail_n = int(rules["tail_lines"])
    max_bytes = int(rules["max_bytes"])
    if len(lines) <= head_n + tail_n:
        out = "\n".join(lines)
        return out[:max_bytes]
    elided = len(lines) - head_n - tail_n
    marker = str(rules["elision_marker"]).replace("{count}", str(elided))
    out = "\n".join([*lines[:head_n], marker, *lines[-tail_n:]])
    encoded = out.encode("utf-8")
    if len(encoded) <= max_bytes:
        return out
    # A byte cap can split a character; the incumbent truncates, so this does too, and
    # then repairs the boundary rather than emitting invalid UTF-8.
    return encoded[:max_bytes].decode("utf-8", "ignore")


# -- per-item measurement ---------------------------------------------------


@dataclass
class LaneResult:
    lane: str
    status: str = "measured"  # measured | refused | not_run
    main_context_bytes: int = 0
    main_context_tokens: int = 0
    evidence_recall: float | None = None
    answered: bool | None = None
    partial: bool = False
    citations_published: int = 0
    citations_valid: int = 0
    reader_input_tokens: int | None = None
    reader_output_tokens: int | None = None
    attempts_started: int = 0
    latency_ms: float = 0.0
    code: str = ""
    detail: str = ""


@dataclass
class ItemResult:
    item_id: str
    answerable: bool
    refused_by_core: str = ""
    lanes: dict[str, LaneResult] = field(default_factory=dict)


class Bench:
    """One item's private world: an import root, a private cache, and one session."""

    def __init__(self, tmp_path: Path, item: dict[str, Any]):
        self.item = item
        self.root = tmp_path / item["id"]
        self.import_root = self.root / "artifacts"
        self.workspace = self.root / "ws"
        self.cache = self.root / "cache"
        self.import_root.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.body = build_body(item)
        self.artifact = self.import_root / f"{item['id']}.artifact"
        self.artifact.write_bytes(self.body)
        self.manifest = self.import_root / f"{item['id']}.manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "import_contract": NATIVE_IMPORT_CONTRACT,
                    "producer": {
                        "id": "shadow-harness",
                        "manifest_schema": NATIVE_IMPORT_CONTRACT,
                    },
                    "artifact": {
                        "path": str(self.artifact),
                        "bytes": len(self.body),
                        "sha256": hashlib.sha256(self.body).hexdigest(),
                        "media_type": item["media_type"],
                    },
                    "origin": {"tool": "shadow_query", "upstream_truncated": False},
                }
            ),
            encoding="utf-8",
        )

    def session(self, provider=None) -> ShuntSession:
        config = load_config(
            {
                "workspace_roots": [str(self.workspace)],
                "cache_dir": str(self.cache),
                "artifact_import": {
                    "enabled": True,
                    "roots": [str(self.import_root)],
                    "accepted_manifest_schemas": [NATIVE_IMPORT_CONTRACT],
                },
            },
            default_spill_dir=self.cache,
        )
        return ShuntSession(
            self.item["id"],
            config,
            make_capability(artifact_import=True),
            provider=provider,
        )


def _raw_lane(bench: Bench) -> LaneResult:
    """The counterfactual. Nothing was dropped, so recall is 1.0 by construction."""
    started = time.perf_counter()
    size = len(bench.body)
    return LaneResult(
        lane="raw_baseline",
        main_context_bytes=size,
        main_context_tokens=estimate_tokens(size),
        evidence_recall=1.0 if bench.item.get("expected_quote") else None,
        answered=bool(bench.item["answerable"]),
        latency_ms=(time.perf_counter() - started) * 1000,
        code="RAW",
    )


def _legacy_lane(bench: Bench, rules: dict[str, Any]) -> LaneResult:
    started = time.perf_counter()
    compacted = legacy_compact(bench.body, rules)
    size = len(compacted.encode("utf-8"))
    quote = bench.item.get("expected_quote")
    recall = None if not quote else (1.0 if quote in compacted else 0.0)
    return LaneResult(
        lane="legacy_compact",
        main_context_bytes=size,
        main_context_tokens=estimate_tokens(size),
        evidence_recall=recall,
        # A heuristic compactor answers nothing; whether the agent could answer depends
        # entirely on whether the evidence happened to survive truncation.
        answered=None if recall is None else bool(recall),
        latency_ms=(time.perf_counter() - started) * 1000,
        code="COMPACTED",
    )


def _retrieval_lane(bench: Bench) -> tuple[LaneResult, str]:
    """Import, then search. Zero model calls, so this lane always runs."""
    started = time.perf_counter()
    session = bench.session()
    refusal = ""
    try:
        imported = session.import_artifact("req_import", manifest_path=str(bench.manifest))
        if imported["code"] != "IMPORTED":
            refusal = imported["code"]
            return (
                LaneResult(
                    lane="deterministic_retrieval",
                    status="refused",
                    main_context_bytes=serialized_bytes(imported),
                    main_context_tokens=estimate_tokens(serialized_bytes(imported)),
                    evidence_recall=0.0 if bench.item.get("expected_quote") else None,
                    answered=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    code=imported["code"],
                ),
                refusal,
            )
        pointer = imported["pointer"]
        extracted = session.inspect(
            {
                "schema_version": EMITTED_SCHEMA_VERSION,
                "request_id": "req_inspect",
                "operation": "inspect",
                "source_id": pointer["source_id"],
                "snapshot_id": pointer["snapshot_id"],
                "selector": {
                    "kind": "search",
                    "needle": bench.item["search_needle"],
                    "max_matches": 4,
                    "context_lines": 1,
                },
                "budgets": {"max_result_bytes": 16384, "max_scan_lines": 20000},
            }
        )
        segments = "\n".join(
            str(segment.get("text", ""))
            for segment in (extracted.get("extraction") or {}).get("segments", [])
        )
        quote = bench.item.get("expected_quote")
        recall = None if not quote else (1.0 if quote in segments else 0.0)
        # Both envelopes reach the main context on this lane: the pointer the import
        # returned, and the extract the search returned. Charging only the extract would
        # understate the lane by exactly the amount that makes it look good.
        total_bytes = serialized_bytes(imported) + serialized_bytes(extracted)
        return (
            LaneResult(
                lane="deterministic_retrieval",
                main_context_bytes=total_bytes,
                main_context_tokens=estimate_tokens(total_bytes),
                evidence_recall=recall,
                answered=bool(segments.strip()),
                partial=extracted["status"] == "partial",
                latency_ms=(time.perf_counter() - started) * 1000,
                code=extracted["code"],
            ),
            refusal,
        )
    finally:
        session.close()


#: Why the reader lane is not scored here, even with a live bridge configured.
#:
#: Scoring a model lane needs a fixed corpus, fixed thresholds and a fixed number of runs
#: per item, all decided before the run. ``scripts/verify eval luna`` is that gate and
#: owns those controls. A number produced here without them would look like a score, so
#: this lane is permanently unscored and the five gates that depend on it stay NOT_RUN.
_READER_LANE_REASON = (
    "the reader lane is scored by scripts/verify eval luna, which owns the fixed corpus, "
    "thresholds and runs-per-item; this harness does not score a model lane"
)


def _reader_lane_not_run() -> LaneResult:
    return LaneResult(lane="reader", status="not_run", code=NOT_RUN, detail=_READER_LANE_REASON)


# -- the report -------------------------------------------------------------


def _reduction(baseline: int, candidate: int) -> float | None:
    if baseline <= 0:
        return None
    return (baseline - candidate) / baseline


def build_report(tmp_path: Path) -> dict[str, Any]:
    corpus = _corpus()
    rules = corpus["legacy_compactor"]

    results: list[ItemResult] = []
    for item in corpus["items"]:
        bench = Bench(tmp_path, item)
        result = ItemResult(item_id=item["id"], answerable=bool(item["answerable"]))
        result.lanes["raw_baseline"] = _raw_lane(bench)
        result.lanes["legacy_compact"] = _legacy_lane(bench, rules)
        retrieval, refusal = _retrieval_lane(bench)
        result.lanes["deterministic_retrieval"] = retrieval
        result.refused_by_core = refusal
        result.lanes["reader"] = _reader_lane_not_run()
        results.append(result)

    # Every item stays in the denominator, including the two the core refuses outright.
    # Dropping a refused item would raise the score by hiding a run.
    scorable = [r for r in results if r.answerable and not r.refused_by_core]
    refused = [r for r in results if r.refused_by_core]

    def totals(lane: str) -> dict[str, int]:
        return {
            "main_context_bytes": sum(r.lanes[lane].main_context_bytes for r in results),
            "main_context_tokens": sum(r.lanes[lane].main_context_tokens for r in results),
        }

    # Two denominators, deliberately separate. The whole-corpus total includes the item
    # the broker *refuses*, whose 8 MiB counterfactual would otherwise carry ~88% of the
    # headline reduction - a saving on a payload no lane can answer from. The gate is
    # measured over the items the broker actually brokered; the refused items' raw bytes
    # are reported on their own, which is the same rule the corpus states for scoring.
    brokered = [r for r in results if not r.refused_by_core]

    def totals_over(rows: list[ItemResult], lane: str) -> dict[str, int]:
        return {
            "main_context_bytes": sum(r.lanes[lane].main_context_bytes for r in rows),
            "main_context_tokens": sum(r.lanes[lane].main_context_tokens for r in rows),
        }

    raw_totals = totals("raw_baseline")
    legacy_totals = totals("legacy_compact")
    retrieval_totals = totals("deterministic_retrieval")
    raw_brokered = totals_over(brokered, "raw_baseline")
    legacy_brokered = totals_over(brokered, "legacy_compact")
    retrieval_brokered = totals_over(brokered, "deterministic_retrieval")
    refused_raw_bytes = sum(
        r.lanes["raw_baseline"].main_context_bytes for r in results if r.refused_by_core
    )

    def recall(lane: str) -> float | None:
        """Mean evidence recall over the items that *have* an expectation to recall.

        A refused item has no ``expected_quote`` - the corpus cannot ask a lane to find
        evidence in a payload the core will not read - so it contributes no recall value
        and is excluded here. That is why the refusal count is reported separately rather
        than folded into this average: this number answers "when there was evidence to
        keep, was it kept", and the refusals answer a different question. Both lanes use
        the identical denominator, so the lane-to-lane comparison is sound.
        """
        values = [
            r.lanes[lane].evidence_recall
            for r in results
            if r.answerable and r.lanes[lane].evidence_recall is not None
        ]
        return sum(values) / len(values) if values else None

    # The retrieval lane is the only model-free lane that does real work; the raw and
    # legacy lanes are byte arithmetic and would make the bound meaningless.
    max_latency = max(r.lanes["deterministic_retrieval"].latency_ms for r in results)

    gates: dict[str, Any] = {
        "main_context_reduction": _gate_ratio(
            "main_context_reduction",
            corpus["gates"]["main_context_reduction"]["threshold"],
            _reduction(
                raw_brokered["main_context_tokens"],
                retrieval_brokered["main_context_tokens"],
            ),
        ),
        "no_evidence_regression_vs_raw": _gate_ratio(
            "no_evidence_regression_vs_raw",
            corpus["gates"]["no_evidence_regression_vs_raw"]["threshold"],
            recall("deterministic_retrieval"),
        ),
        "bounded_latency": _gate_upper_bound(
            "bounded_latency",
            corpus["gates"]["bounded_latency"]["threshold_ms"],
            max_latency,
        ),
        "task_correctness": _gate_not_run("task_correctness", _SCORED_ELSEWHERE),
        "semantic_evidence_support": _gate_not_run(
            "semantic_evidence_support", _SCORED_ELSEWHERE
        ),
        "mechanical_citation_validity": _gate_not_run(
            "mechanical_citation_validity",
            f"{_SCORED_ELSEWHERE}; the retrieval lane publishes no citations, so scoring "
            "it there would be a vacuous 1.0",
        ),
        "net_cost_reduction": _gate_not_run(
            "net_cost_reduction",
            "a scored reader lane and a versioned pricing table; this repository has no "
            "price table, so this stays NOT_RUN even with a live bridge",
        ),
        "bounded_follow_up_rate": _gate_not_run("bounded_follow_up_rate", _SCORED_ELSEWHERE),
    }

    return {
        "corpus_sha256": _corpus_hash(),
        "contract_version": EMITTED_SCHEMA_VERSION,
        "items": len(results),
        # Answerable and not refused: the items a lane could actually get right. Both
        # other groups stay visible - `refused_by_core` names the refusals, and the
        # no-answer items are counted in `items` and scored on whether the lane
        # correctly found nothing.
        "answerable_and_importable_items": len(scorable),
        "no_answer_items": sum(1 for r in results if not r.answerable),
        "refused_by_core": {r.item_id: r.refused_by_core for r in refused},
        # Derived from the lane results, never from an environment variable: the lane is
        # not driven here at all, so claiming otherwise because a bridge happens to be
        # configured would be exactly the false pass this harness exists to avoid.
        "reader_lane": (
            "measured"
            if any(r.lanes["reader"].status == "measured" for r in results)
            else NOT_RUN
        ),
        "refused_raw_bytes": refused_raw_bytes,
        "lanes": {
            "raw_baseline": raw_totals | {"brokered_only": raw_brokered},
            "legacy_compact": legacy_totals
            | {
                "brokered_only": legacy_brokered,
                "evidence_recall": recall("legacy_compact"),
                # The gated figure. Over brokered items only, for the same reason the
                # gate is: a saving on a refused payload is not a saving anyone collects.
                "main_context_reduction_vs_raw": _reduction(
                    raw_brokered["main_context_tokens"],
                    legacy_brokered["main_context_tokens"],
                ),
                "main_context_reduction_vs_raw_whole_corpus": _reduction(
                    raw_totals["main_context_tokens"], legacy_totals["main_context_tokens"]
                ),
            },
            "deterministic_retrieval": retrieval_totals
            | {
                "brokered_only": retrieval_brokered,
                "evidence_recall": recall("deterministic_retrieval"),
                "main_context_reduction_vs_raw": _reduction(
                    raw_brokered["main_context_tokens"],
                    retrieval_brokered["main_context_tokens"],
                ),
                "main_context_reduction_vs_raw_whole_corpus": _reduction(
                    raw_totals["main_context_tokens"],
                    retrieval_totals["main_context_tokens"],
                ),
                "max_latency_ms": round(max_latency, 3),
            },
            "reader": {"status": NOT_RUN, "reason": _READER_LANE_REASON},
        },
        "gates": gates,
        "per_item": [
            {
                "id": r.item_id,
                "answerable": r.answerable,
                "refused_by_core": r.refused_by_core or None,
                "lanes": {
                    name: {
                        "status": lane.status,
                        "code": lane.code,
                        "main_context_tokens": lane.main_context_tokens,
                        "evidence_recall": lane.evidence_recall,
                        "answered": lane.answered,
                        "partial": lane.partial,
                    }
                    for name, lane in r.lanes.items()
                },
            }
            for r in results
        ],
    }


#: What every reader-dependent gate is missing. Not "a bridge": a *scored* lane, which
#: needs controls this harness deliberately does not own.
_SCORED_ELSEWHERE = (
    "a scored reader lane; scripts/verify eval luna owns the fixed corpus, thresholds and "
    "runs-per-item that a score requires"
)


def _gate_ratio(name: str, threshold: float, measured: float | None) -> dict[str, Any]:
    if measured is None:
        return _gate_not_run(name, "no measurable denominator")
    return {
        "name": name,
        "status": "pass" if measured >= threshold else "fail",
        "threshold": threshold,
        "measured": round(measured, 4),
    }


def _gate_upper_bound(name: str, threshold_ms: float, measured_ms: float) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if measured_ms <= threshold_ms else "fail",
        "threshold_ms": threshold_ms,
        "measured_ms": round(measured_ms, 3),
    }


def _gate_not_run(name: str, missing: str) -> dict[str, Any]:
    return {"name": name, "status": NOT_RUN, "missing": missing}


# -- tests ------------------------------------------------------------------


def test_the_corpus_is_fixed_and_self_consistent():
    corpus = _corpus()
    items = corpus["items"]
    assert len(items) == 12
    assert len({item["id"] for item in items}) == 12
    assert corpus["runs_per_item"] == 1
    assert set(corpus["lanes"]) == {
        "raw_baseline",
        "legacy_compact",
        "deterministic_retrieval",
        "reader",
    }
    assert corpus["gates"]["mechanical_citation_validity"]["threshold"] == 1.0
    assert corpus["gates"]["task_correctness"]["threshold"] >= 0.95
    assert corpus["gates"]["semantic_evidence_support"]["threshold"] >= 0.95
    assert corpus["gates"]["net_cost_reduction"]["threshold"] >= 0.30
    assert corpus["gates"]["main_context_reduction"]["threshold"] >= 0.60
    for item in items:
        assert item["question"].strip()
        assert item["search_needle"].strip()
        body = build_body(item)
        if item.get("expected_quote"):
            # The expectation must be in the artifact, or no lane could honestly find it.
            assert item["expected_quote"].encode("utf-8") in body, item["id"]
        else:
            assert item["expected_facts"] == []
    # A corpus with no fail-closed item would only ever prove the pleasant path.
    assert sum(1 for item in items if item.get("refusal_expected")) == 2


def test_the_artifacts_are_byte_identical_across_runs():
    """A shadow comparison whose inputs drift compares nothing."""
    corpus = _corpus()
    first = [hashlib.sha256(build_body(item)).hexdigest() for item in corpus["items"]]
    second = [hashlib.sha256(build_body(item)).hexdigest() for item in corpus["items"]]
    assert first == second
    # And they are large enough for the comparison to mean anything.
    sizes = [len(build_body(item)) for item in corpus["items"]]
    assert min(sizes) > DEFAULT_LIMITS.max_tool_result_bytes


def test_the_legacy_compactor_drops_evidence_from_the_middle():
    """The incumbent's failure mode, demonstrated rather than asserted in prose."""
    corpus = _corpus()
    rules = corpus["legacy_compactor"]
    middle = next(item for item in corpus["items"] if item["id"] == "log_page_mid_error")
    tail = next(item for item in corpus["items"] if item["id"] == "log_page_tail_error")
    assert middle["expected_quote"] not in legacy_compact(build_body(middle), rules)
    assert tail["expected_quote"] in legacy_compact(build_body(tail), rules)


def test_the_shadow_report_meets_the_gates_that_can_run(tmp_path):
    report = build_report(tmp_path)
    _write_report(report)

    runnable = {
        name: gate
        for name, gate in report["gates"].items()
        if gate["status"] != NOT_RUN
    }
    # The gates that need a provider or a pricing table must say so, not be scored from
    # a lane that cannot answer the question they ask.
    assert set(runnable) == {
        "main_context_reduction",
        "no_evidence_regression_vs_raw",
        "bounded_latency",
    }
    for name, gate in runnable.items():
        assert gate["status"] == "pass", (name, gate, report["lanes"])

    assert report["gates"]["mechanical_citation_validity"]["status"] == NOT_RUN
    assert report["gates"]["net_cost_reduction"]["status"] == NOT_RUN
    # Unconditional. The reader lane is never driven here, so a configured bridge must not
    # be able to turn this into a claim that it was - which an `or os.environ.get(...)`
    # would have done silently.
    assert report["reader_lane"] == NOT_RUN
    assert all(
        row["lanes"]["reader"]["status"] == "not_run" for row in report["per_item"]
    )


def test_a_refused_item_is_reported_rather_than_averaged_away(tmp_path):
    """A refusal is named in its own field, and its counterfactual never inflates a gate.

    The over-cap item alone is ~88% of the whole-corpus raw baseline. Crediting that as a
    saving would make the headline reduction a statement about a payload no lane can
    answer from, so the gate is measured over brokered items and the refused bytes are
    reported separately.
    """
    report = build_report(tmp_path)
    assert set(report["refused_by_core"]) == {
        "artifact_with_credential_marker",
        "artifact_over_source_cap",
    }
    assert report["items"] == 12
    # 8 answerable and importable, 2 answerable but refused by the core, 2 no-answer.
    assert report["answerable_and_importable_items"] == 8
    assert report["no_answer_items"] == 2
    expected = {
        item["id"]: item["refusal_expected"]
        for item in _corpus()["items"]
        if item.get("refusal_expected")
    }
    # The corpus states which refusal it expects, so a refusal that changed class would
    # fail here rather than quietly scoring zero for a different reason.
    assert report["refused_by_core"] == expected
    for item_id in expected:
        entry = next(row for row in report["per_item"] if row["id"] == item_id)
        assert entry["lanes"]["deterministic_retrieval"]["status"] == "refused", item_id
        assert entry["lanes"]["deterministic_retrieval"]["answered"] is False, item_id

    # The refused bytes are visible on their own, and they are not inside the gated
    # denominator: the gate's reduction must be measurably different from the
    # whole-corpus one, or the split would be decorative.
    assert report["refused_raw_bytes"] > 0
    retrieval = report["lanes"]["deterministic_retrieval"]
    assert (
        retrieval["main_context_reduction_vs_raw"]
        < retrieval["main_context_reduction_vs_raw_whole_corpus"]
    )
    assert report["gates"]["main_context_reduction"]["measured"] == round(
        retrieval["main_context_reduction_vs_raw"], 4
    )


def test_the_broker_beats_the_incumbent_on_evidence_not_only_on_size(tmp_path):
    """Smaller is easy; the incumbent is already small. Keeping the evidence is the point."""
    report = build_report(tmp_path)
    legacy = report["lanes"]["legacy_compact"]
    retrieval = report["lanes"]["deterministic_retrieval"]
    assert legacy["evidence_recall"] is not None
    assert retrieval["evidence_recall"] is not None
    assert retrieval["evidence_recall"] > legacy["evidence_recall"]
    # And it is not paying for that with the context it was supposed to save.
    assert retrieval["main_context_reduction_vs_raw"] >= 0.6


def test_the_report_carries_no_artifact_content(tmp_path):
    """The report is evidence about a run, not a copy of what the run read."""
    report = build_report(tmp_path)
    wire = json.dumps(report)
    corpus = _corpus()
    for item in corpus["items"]:
        body = build_body(item).decode("utf-8", "ignore")
        assert body[:64] not in wire
        if item.get("expected_quote"):
            assert item["expected_quote"] not in wire
        assert item["question"] not in wire
    assert str(REPO) not in wire


def _write_report(report: dict[str, Any]) -> None:
    """Drop the report next to the other verify reports, if that directory exists.

    ``reports/`` is gitignored, so this leaves the worktree clean; a checkout without it
    simply produces no file and the assertions above still hold.
    """
    if not REPORT_DIR.is_dir():
        return
    path = REPORT_DIR / "shadow-ab-latest.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


#: A bridge spec, not just the opt-in flag. The deterministic half of this harness must
#: stay green whatever the reader environment says: keying the wiring check off the flag
#: alone made `verify shadow deterministic` fail for anyone who had set the flag for the
#: live eval without exporting a bridge in the same shell.
BRIDGE_ENV = "CONTEXT_SHUNT_LUNA_BRIDGE"


@pytest.mark.skipif(
    ":" not in os.environ.get(BRIDGE_ENV, ""),
    reason=f"set {BRIDGE_ENV}=module:callable for the wiring check; it is never a score",
)
def test_the_reader_lane_is_wired_when_a_bridge_exists(tmp_path):
    """The reader lane's wiring, exercised only where a live bridge is configured.

    Deliberately not a scored gate: with a bridge present this proves the lane can be
    driven end to end, and the scored reader gates stay the live eval's job
    (``scripts/verify eval luna``), where the corpus and thresholds are fixed for that
    purpose. A number produced here without those controls would look like a score.
    """
    import importlib

    module_name, _, attr = os.environ[BRIDGE_ENV].partition(":")
    bridge = getattr(importlib.import_module(module_name), attr)

    from context_shunt.provider import HostBridgeProvider

    corpus = _corpus()
    item = next(row for row in corpus["items"] if row["id"] == "log_page_mid_error")
    bench = Bench(tmp_path, item)
    session = bench.session(provider=HostBridgeProvider(bridge, DEFAULT_LIMITS))
    try:
        imported = session.import_artifact("req_import", manifest_path=str(bench.manifest))
        assert imported["code"] == "IMPORTED"
        pointer = imported["pointer"]
        answered = session.read(
            {
                "schema_version": EMITTED_SCHEMA_VERSION,
                "request_id": "req_read",
                "operation": "read",
                "question": item["question"],
                "refined": True,
                "sources": [
                    {
                        "source_id": pointer["source_id"],
                        "snapshot_id": pointer["snapshot_id"],
                        "selector": {"kind": "all"},
                    }
                ],
                "budgets": {
                    "max_chunks": 8,
                    "max_answer_bytes": 8192,
                    "deadline_ms": 60000,
                },
            }
        )
        assert answered["code"] in ("ANSWERED", "NO_MATCH")
        for citation in answered["citations"]:
            assert citation["verified"] is True
    finally:
        session.close()
