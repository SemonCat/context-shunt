#!/usr/bin/env python3
"""Reproducible three-lane benchmark for the five audited workflow shapes.

This is deliberately provider-free. Reader tokens use the repository's bytes/4 estimator
and say so; attempts that model the observed late/unknown completion shape stay explicit
rather than being converted to zero. Controlled end-to-end time uses one fixed mock model
latency (25ms per attempt) and one fixed local processing rate (50 MB/s), making lane
comparisons stable across machines. ``measured_local_ms`` is also reported, but is only a
sanity measurement of the deterministic benchmark implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "core-py" / "src"))

from context_shunt.legacy_compact import compact_tool_result

CORPUS = Path(__file__).with_name("corpus.json")
BYTES_PER_TOKEN = 4
MOCK_MODEL_MS = 25.0
PROCESSING_BYTES_PER_MS = 50_000.0


def tokens(byte_count: int) -> int:
    return math.ceil(max(0, byte_count) / BYTES_PER_TOKEN)


def pointer_bytes(size: int, ordinal: int) -> int:
    digest = hashlib.sha256(f"synthetic:{ordinal}:{size}".encode()).hexdigest()
    pointer = {
        "schema_version": "1.2", "status": "ok", "code": "SPILLED", "answer": "",
        "citations": [], "coverage": {"complete": False, "processed_chunks": 0,
        "planned_chunks": 0, "omitted": [], "upstream_truncated": False},
        "sources": [], "retryable": False,
        "pointer": {"source_id": f"src_{digest[:16]}", "snapshot_id": f"sha256:{digest}",
        "bytes": size, "expires_at": "2026-09-14T02:00:00Z", "internal": True},
    }
    return len(json.dumps(pointer, separators=(",", ":")).encode())


def synthetic_source(size: int, ordinal: int) -> str:
    header = (
        f'{{"synthetic":true,"source":{ordinal},"events":['
        '{"level":"error","trace_id":"trace-a","service":"billing"},'
        '{"level":"info","trace_id":"trace-b","service":"checkout"}],"padding":"'
    )
    tail = '"}'
    padding = max(0, size - len((header + tail).encode()))
    text = header + ("x" * padding) + tail
    return text[:size]


def legacy_summary_bytes(size: int, ordinal: int) -> int:
    # Execute the owned, golden-tested port of incumbent v0.3.0, at the configured
    # session fallback cap. This is product code, not the older shadow approximation.
    source = synthetic_source(size, ordinal)
    summary = compact_tool_result(source, hard_chars=16_000)
    return len(summary.encode())


def lane_result(workflow: dict[str, Any], lane: str) -> dict[str, Any]:
    started = time.perf_counter()
    source_sizes = workflow["source_bytes"]
    if lane == "legacy_compactor":
        transform = sum(legacy_summary_bytes(size, i) for i, size in enumerate(source_sizes))
        main_bytes = transform + workflow["requery_bytes"] + workflow["full_read_bytes"]
        attempts = complete_attempts = input_bytes = output_bytes = result_bytes = 0
        correctness = 1.0 if workflow["id"].endswith(("requery", "unread-pointer")) else 0.0
        coverage = "partial" if correctness < 1.0 else "complete"
        cache_hits = 0
        processed_bytes = sum(source_sizes) + workflow["requery_bytes"] + workflow["full_read_bytes"]
    else:
        profile = workflow[lane]
        pointers = sum(pointer_bytes(size, i) for i, size in enumerate(source_sizes))
        main_bytes = pointers + profile["result_bytes"] + workflow["requery_bytes"]
        # The PRE lane includes the recorded full-read loss. NEW replaces only workflow 5's
        # full read with the bounded selected aggregate result; every other host recovery
        # byte remains in both lanes.
        if lane == "pre" or workflow["id"] != "session-5-full-read-and-unread-pointer":
            main_bytes += workflow["full_read_bytes"]
        attempts = profile["reader_attempts"]
        complete_attempts = profile["usage_complete_attempts"]
        input_bytes = profile["reader_input_bytes"]
        output_bytes = profile["reader_output_bytes"]
        result_bytes = profile["result_bytes"]
        correctness = profile["correctness"]
        coverage = profile["coverage"]
        cache_hits = profile.get("answer_cache_hits", 0)
        processed_bytes = sum(source_sizes) + input_bytes + output_bytes

    estimated_in = tokens(input_bytes) if attempts else None
    estimated_out = tokens(output_bytes) if attempts else None
    usage_complete = attempts == complete_attempts
    ratio = (complete_attempts / attempts) if attempts else 1.0
    reported_in = math.floor((estimated_in or 0) * ratio) if attempts else None
    reported_out = math.floor((estimated_out or 0) * ratio) if attempts else None
    controlled_ms = processed_bytes / PROCESSING_BYTES_PER_MS + attempts * MOCK_MODEL_MS
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "workflow": workflow["id"], "lane": lane,
        "correctness_expectation": workflow["correctness_expectation"],
        "main_context_bytes": main_bytes, "main_context_tokens_estimated": tokens(main_bytes),
        "reader_input_tokens_reported": reported_in,
        "reader_output_tokens_reported": reported_out,
        "reader_input_tokens_estimated_total": estimated_in,
        "reader_output_tokens_estimated_total": estimated_out,
        "reader_cache_tokens": None,
        "reader_token_method": "bytes_div_4" if attempts else "not_applicable",
        "attempts_started": attempts, "attempts_usage_complete": complete_attempts,
        "unknown_or_late_attempts": attempts - complete_attempts,
        "usage_complete": usage_complete, "answer_cache_hits": cache_hits,
        "requery_bytes": workflow["requery_bytes"],
        "full_read_bytes": workflow["full_read_bytes"] if lane != "new" else (
            0 if workflow["id"] == "session-5-full-read-and-unread-pointer"
            else workflow["full_read_bytes"]
        ),
        "result_bytes": result_bytes, "coverage": coverage,
        "correctness": correctness,
        "citation_validity": None,
        "controlled_wall_ms": round(controlled_ms, 3),
        "measured_local_ms": round(elapsed, 6),
    }


def markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    rows = [
        "# Five-workflow intent-reader benchmark",
        "",
        "Synthetic production-derived shapes; no production content or provider call. Token values are bytes/4 estimates, not billed tokens. Controlled wall time uses a fixed 25ms mock-provider latency per attempt plus 50 MB/s processing. `null` reader/cache tokens mean not applicable or not reported—never zero substituted for unknown.",
        "",
        "| Lane | Main tokens (est.) | Reader in/out reported | Reader in/out est. total | Provider cache | Attempts (usage complete) | Unknown/late | Requery bytes | Full-read bytes | Accuracy | Controlled wall ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for lane in ("legacy_compactor", "pre", "new"):
        row = totals[lane]
        shown = lambda value: json.dumps(value, separators=(",", ":"))
        rows.append(
            f"| {lane} | {row['main_context_tokens_estimated']} | "
            f"{shown(row['reader_input_tokens_reported'])}/{shown(row['reader_output_tokens_reported'])} | "
            f"{shown(row['reader_input_tokens_estimated_total'])}/{shown(row['reader_output_tokens_estimated_total'])} | "
            f"{shown(row['reader_cache_tokens'])} | "
            f"{row['attempts_started']} ({row['attempts_usage_complete']}) | "
            f"{row['unknown_or_late_attempts']} | {row['requery_bytes']} | {row['full_read_bytes']} | "
            f"{row['mean_correctness']:.3f} | {row['controlled_wall_ms']:.3f} |"
        )
    rows += [
        "",
        "The legacy lane executes the golden-tested owned port of incumbent compactor v0.3.0. PRE parameters are frozen from branch `1686db6` and the sanitized pre-change trace; NEW parameters apply only the tested owned-reader capabilities: workflow 3's exact repeat is a scoped cache hit, workflows 2/3 use deterministic aggregation, and workflow 5 uses bounded selected retrieval. The replay is deterministic rather than a claim that either git tree or a provider was executed live. Workflow 4 remains unchanged because correlating Hermes requery calls is a host-owned gap; its 19,912 recovery bytes remain counted in every lane. The legacy/current full-read loss in workflow 5 is likewise included, rather than credited as a saving.",
        "",
        "Deterministic outputs do not need model citations, so citation validity is `null`, not a vacuous 100%. Reader attempts with incomplete usage retain both reported lower-bound and estimated-total fields in the JSON artifact.",
        "",
        f"Corpus SHA-256: `{report['corpus_sha256']}`.",
        "",
        "Machine-readable evidence: [`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json).",
    ]
    return "\n".join(rows) + "\n"


def validate(report: dict[str, Any]) -> None:
    """Fail the benchmark if one of the acceptance-relevant comparisons disappears."""
    totals = report["totals"]
    assert len(report["results"]) == 15
    assert totals["new"]["main_context_tokens_estimated"] < totals["pre"]["main_context_tokens_estimated"]
    assert totals["new"]["reader_input_tokens_estimated_total"] < totals["pre"]["reader_input_tokens_estimated_total"]
    assert totals["new"]["mean_correctness"] == 1.0
    assert totals["pre"]["unknown_or_late_attempts"] > 0
    assert totals["new"]["answer_cache_hits"] == 1
    session4 = [row for row in report["results"] if row["workflow"] == "session-4-abandoned-pointer-requery"]
    assert {row["requery_bytes"] for row in session4} == {19_912}
    session5 = {row["lane"]: row for row in report["results"] if row["workflow"] == "session-5-full-read-and-unread-pointer"}
    assert session5["legacy_compactor"]["full_read_bytes"] == 17_601
    assert session5["pre"]["full_read_bytes"] == 17_601
    assert session5["new"]["full_read_bytes"] == 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    raw = CORPUS.read_bytes()
    corpus = json.loads(raw)
    results = [lane_result(workflow, lane) for workflow in corpus["workflows"]
               for lane in ("legacy_compactor", "pre", "new")]
    totals: dict[str, Any] = {}
    for lane in ("legacy_compactor", "pre", "new"):
        selected = [row for row in results if row["lane"] == lane]
        has_attempts = any(row["attempts_started"] for row in selected)
        totals[lane] = {
            "main_context_bytes": sum(row["main_context_bytes"] for row in selected),
            "main_context_tokens_estimated": sum(row["main_context_tokens_estimated"] for row in selected),
            "reader_input_tokens_estimated_total": (
                sum(row["reader_input_tokens_estimated_total"] or 0 for row in selected)
                if has_attempts else None
            ),
            "reader_output_tokens_estimated_total": (
                sum(row["reader_output_tokens_estimated_total"] or 0 for row in selected)
                if has_attempts else None
            ),
            "reader_input_tokens_reported": (
                sum(row["reader_input_tokens_reported"] or 0 for row in selected)
                if has_attempts else None
            ),
            "reader_output_tokens_reported": (
                sum(row["reader_output_tokens_reported"] or 0 for row in selected)
                if has_attempts else None
            ),
            "reader_cache_tokens": None,
            "attempts_started": sum(row["attempts_started"] for row in selected),
            "attempts_usage_complete": sum(row["attempts_usage_complete"] for row in selected),
            "unknown_or_late_attempts": sum(row["unknown_or_late_attempts"] for row in selected),
            "answer_cache_hits": sum(row["answer_cache_hits"] for row in selected),
            "requery_bytes": sum(row["requery_bytes"] for row in selected),
            "full_read_bytes": sum(row["full_read_bytes"] for row in selected),
            "mean_correctness": statistics.fmean(row["correctness"] for row in selected),
            "controlled_wall_ms": sum(row["controlled_wall_ms"] for row in selected),
            "measured_local_ms": sum(row["measured_local_ms"] for row in selected),
        }
    report = {
        "schema": "context_shunt.intent_reader_benchmark.v1",
        "baseline_commit": "1686db6",
        "corpus_sha256": hashlib.sha256(raw).hexdigest(),
        "token_method": "bytes_div_4",
        "controlled_time": {"mock_model_ms_per_attempt": MOCK_MODEL_MS,
                            "processing_bytes_per_ms": PROCESSING_BYTES_PER_MS},
        "results": results, "totals": totals,
    }
    validate(report)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(encoded)
    if args.markdown_output:
        args.markdown_output.write_text(markdown(report))
    print(encoded, end="")


if __name__ == "__main__":
    main()
