from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from context_shunt.provider import READER_SYSTEM_PROMPT, build_user_message


def _worker_module(root: Path):
    path = root / "evals/intent-reader-audit/worker.py"
    spec = importlib.util.spec_from_file_location("intent_reader_audit_worker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    pre_versions = {
        tuple(row["core_supported_request_versions"])
        for row in report["results"]
        if row["lane"] == "pre"
    }
    assert pre_versions == {("1.0", "1.1", "1.2", "1.3")}
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


def test_live_payload_attestation_detects_parent_context_leak(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[3]
    worker = _worker_module(root)
    provider = object.__new__(worker.InstrumentedLiveProvider)
    locator = {"kind": "lines", "start": 1, "end": 1}
    marker = "PRIVATE_PARENT_CONTEXT_CANARY_7f9070"
    monkeypatch.setenv("CONTEXT_SHUNT_EVAL_PARENT_CONTEXT_CANARY", marker)

    clean = provider._payload_attestation(
        READER_SYSTEM_PROMPT,
        build_user_message("What is retry_limit?", "retry_limit: 7", locator),
        2048,
        45000,
    )
    assert clean["roles"] == ["system", "user"]
    assert clean["system_is_exact_reader_contract"] is True
    assert clean["user_is_exact_reader_template"] is True
    assert clean["user_sections"] == ["locator", "source_excerpt", "question"]
    assert clean["parent_context_canary_absent"] is True
    assert not {"system", "user", "source_excerpt", "question"} & clean.keys()

    leaked = provider._payload_attestation(
        READER_SYSTEM_PROMPT,
        build_user_message(f"What is retry_limit? {marker}", "retry_limit: 7", locator),
        2048,
        45000,
    )
    assert leaked["user_is_exact_reader_template"] is True
    assert leaked["parent_context_canary_absent"] is False


def test_committed_real_luna_evidence_is_redacted_and_bound_to_the_executed_code() -> None:
    root = Path(__file__).resolve().parents[3]
    report = json.loads(
        (root / "evals/intent-reader-audit/real-luna-latest.json").read_text()
    )
    corpus = root / "evals/intent-reader-audit/corpus.json"
    assert report["acceptance"] == {"passed": True, "errors": []}
    assert report["attempt_outcomes"] == {
        "completed": 10,
        "timed_out": 1,
        "failed": 0,
        "late_or_in_flight_usage_unknown": 0,
    }
    assert sum(report["attempt_outcomes"].values()) == sum(
        report["totals"][lane]["reader_attempts_observed"] for lane in ("pre", "new")
    )
    assert sum(
        report["totals"][lane]["reader_unknown_usage_attempts"]
        for lane in ("pre", "new")
    ) == 1
    assert report["corpus_sha256"] == hashlib.sha256(corpus.read_bytes()).hexdigest()

    digest = hashlib.sha256()
    for relative in sorted(report["implementation"]["working_tree_relevant_files"]):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    assert digest.hexdigest() == report["implementation"][
        "working_tree_relevant_files_sha256"
    ]

    forbidden_raw_keys = {"system", "user", "answer", "quote", "source_excerpt"}

    def assert_redacted(value):
        if isinstance(value, dict):
            assert not (forbidden_raw_keys & value.keys())
            for child in value.values():
                assert_redacted(child)
        elif isinstance(value, list):
            for child in value:
                assert_redacted(child)

    assert_redacted(report)
    real_calls = [
        call
        for row in report["results"]
        if row["lane"] in {"pre", "new"}
        for call in row["reader"]["calls"]
    ]
    assert len(real_calls) == 11
    completed = [call for call in real_calls if call["status"] == "completed"]
    timed_out = [
        call for call in real_calls if call["status"] == "timed_out_usage_unknown"
    ]
    assert len(completed) == 10 and len(timed_out) == 1
    assert all(call["resolved_model"] == "gpt-5.6-luna" for call in completed)
    assert all(call.get("resolved_model") is None for call in timed_out)
    assert all(
        call.get("reported_input_tokens") is None
        and call.get("reported_output_tokens") is None
        and call.get("reported_cache_tokens") is None
        for call in timed_out
    )
    assert all(call["payload"]["roles"] == ["system", "user"] for call in real_calls)
    assert all(
        call["payload"]["system_is_exact_reader_contract"]
        and call["payload"]["user_is_exact_reader_template"]
        and call["payload"]["parent_context_canary_absent"]
        for call in real_calls
    )

    new = {
        row["workflow"]: row
        for row in report["results"]
        if row["lane"] == "new"
    }
    for deterministic in (
        "session-2-minified-loki-counts",
        "session-4-abandoned-pointer-requery",
        "session-5-full-read-and-unread-pointer",
    ):
        assert new[deterministic]["reader"]["attempts_observed"] == 0
