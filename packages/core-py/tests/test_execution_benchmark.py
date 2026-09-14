from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.benchmark
def test_intent_reader_benchmark_executes_pinned_pre_and_working_new(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    report_path = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "evals/intent-reader-audit/run.py"),
            "--json-output",
            str(report_path),
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text())

    assert report["schema"] == "context_shunt.intent_reader_execution_benchmark.v2"
    assert report["acceptance"] == {"passed": True, "errors": []}
    assert report["corpus_declares_lane_profiles"] is False
    assert len(report["results"]) == 15
    assert all(check["passed"] for check in report["red_checks"])

    origins = {row["module_origin"] for row in report["results"]}
    assert origins == {
        "isolated_git_archive:1686db6:packages/core-py/src/context_shunt/__init__.py",
        "working_tree:packages/core-py/src/context_shunt/__init__.py",
    }
    assert {row["execution"] for row in report["results"]} == {
        "actual_incumbent_compactor_port",
        "actual_shunt_session",
    }
    versions = {row["lane"]: row["core_emitted_envelope_version"] for row in report["results"]}
    assert versions == {"legacy_compactor": None, "pre": "1.2", "new": "1.3"}
    assert not any("controlled_wall_ms" in row for row in report["results"])
    corpus = json.loads((root / "evals/intent-reader-audit/corpus.json").read_text())
    forbidden_profile_fields = {
        "pre",
        "new",
        "reader_attempts",
        "reader_input_bytes",
        "reader_output_bytes",
        "correctness",
        "result_bytes",
        "controlled_wall_ms",
    }
    assert not any(forbidden_profile_fields & workflow.keys() for workflow in corpus["workflows"])

    pre_calls = [
        call for row in report["results"] if row["lane"] == "pre" for call in row["reader"]["calls"]
    ]
    reported = [call for call in pre_calls if call["reported_input_tokens"] is not None]
    assert report["totals"]["pre"]["reader_input_tokens_reported_lower_bound"] == sum(
        call["reported_input_tokens"] for call in reported
    )
    assert report["totals"]["pre"]["reader_unknown_usage_attempts"] == len(pre_calls) - len(
        reported
    )
