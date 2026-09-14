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


def _real_run_module(root: Path):
    path = root / "evals/intent-reader-audit/real_run.py"
    spec = importlib.util.spec_from_file_location("intent_reader_audit_real_run", path)
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
    outcomes = report["attempt_outcomes"]
    assert outcomes["completed"] > 0
    assert outcomes["failed"] == 0
    assert outcomes["late_or_in_flight_usage_unknown"] == 0
    assert sum(report["attempt_outcomes"].values()) == sum(
        report["totals"][lane]["reader_attempts_observed"] for lane in ("pre", "new")
    )
    assert sum(
        report["totals"][lane]["reader_unknown_usage_attempts"]
        for lane in ("pre", "new")
    ) == outcomes["timed_out"]
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
    assert len(real_calls) == sum(outcomes.values())
    completed = [call for call in real_calls if call["status"] == "completed"]
    timed_out = [
        call for call in real_calls if call["status"] == "timed_out_usage_unknown"
    ]
    assert len(completed) == outcomes["completed"]
    assert len(timed_out) == outcomes["timed_out"]
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
    assert all(
        row.get("semantic_answer_published") is False
        or row["citation_validity"] is True
        for row in report["results"]
        if row["lane"] in {"pre", "new"}
    )
    assert "/Users/" not in report["command"]
    assert 'CONTEXT_SHUNT_OPENCLAW_ROOT="$CONTEXT_SHUNT_OPENCLAW_ROOT"' in report[
        "command"
    ]

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

    # A missing core total remains unknown; it is never folded into an exact-looking zero.
    pre_rows = [
        json.loads(json.dumps(row))
        for row in report["results"]
        if row["lane"] == "pre"
    ]
    with_attempt = next(row for row in pre_rows if row["reader"]["attempts_observed"])
    with_attempt["reader"]["core_accounted_input_tokens"] = None
    assert real_run_totals(root, pre_rows)["reader_core_accounted_input_tokens"] is None

    # Physical calls with no provider usage are unknown, never an exact-looking sum([])=0.
    for row in pre_rows:
        for call in row["reader"]["calls"]:
            call["reported_input_tokens"] = None
            call["reported_output_tokens"] = None
    unknown = real_run_totals(root, pre_rows)
    assert unknown["reader_input_tokens_reported_lower_bound"] is None
    assert unknown["reader_output_tokens_reported_lower_bound"] is None

    # Providers may expose the two fields independently; neither direction crashes or
    # fabricates the absent half.
    first_call = next(
        call for row in pre_rows for call in row["reader"]["calls"]
    )
    first_call["reported_input_tokens"] = 17
    input_only = real_run_totals(root, pre_rows)
    assert input_only["reader_input_tokens_reported_lower_bound"] == 17
    assert input_only["reader_output_tokens_reported_lower_bound"] is None
    first_call["reported_input_tokens"] = None
    first_call["reported_output_tokens"] = 9
    output_only = real_run_totals(root, pre_rows)
    assert output_only["reader_input_tokens_reported_lower_bound"] is None
    assert output_only["reader_output_tokens_reported_lower_bound"] == 9

    first_call["transport_input_bytes"] = None
    first_call["transport_output_bytes"] = None
    real_run = _real_run_module(root)
    incomplete_transport = real_run._live_totals({"rows": pre_rows, "lane_elapsed_ms_observed": 1})
    assert incomplete_transport["transport_input_bytes_observed"] is None
    assert incomplete_transport["transport_output_bytes_observed"] is None


def test_real_luna_checkout_binding_refuses_uncommitted_source(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    real_run = _real_run_module(root)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "config", "user.email", "intent-reader-eval@example.invalid"],
        cwd=checkout,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Intent Reader Eval"], cwd=checkout, check=True)
    source = checkout / "runtime.py"
    source.write_text("BOUND = True\n")
    subprocess.run(["git", "add", "runtime.py"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=checkout, check=True)

    identity = real_run._checkout_identity(checkout, "fixture")
    assert identity["clean"] is True
    assert len(identity["commit"]) == 40
    assert len(identity["git_tree"]) == 40

    source.write_text("BOUND = False\n")
    with pytest.raises(SystemExit, match="fixture checkout is not clean"):
        real_run._checkout_identity(checkout, "fixture")

    source.write_text("BOUND = True\n")
    checkpoint = checkout / "checkpoint.json"
    checkpoint.write_text("{}\n")
    with pytest.raises(SystemExit, match="fixture checkout is not clean"):
        real_run._checkout_identity(checkout, "fixture")


def test_real_luna_has_no_resume_path_for_editable_lane_evidence() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "evals/intent-reader-audit/real_run.py").read_text()
    assert "--resume-lanes" not in source
    assert "_checkpoint_errors" not in source


def test_real_luna_refuses_an_alternate_provider_before_starting_a_lane(
    monkeypatch, tmp_path
) -> None:
    root = Path(__file__).resolve().parents[3]
    real_run = _real_run_module(root)
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("CONTEXT_SHUNT_LUNA_EVAL", "1")
    monkeypatch.setenv("CONTEXT_SHUNT_OPENCLAW_ROOT", str(host))
    monkeypatch.setenv(
        "CONTEXT_SHUNT_OPENCLAW_ROUTE", "alternate-provider/gpt-5.6-luna"
    )
    monkeypatch.delenv("CONTEXT_SHUNT_OPENCLAW_SERVER", raising=False)
    monkeypatch.setattr("sys.argv", ["real_run.py"])
    monkeypatch.setattr(
        real_run.local_benchmark,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("a live lane was started"),
    )
    with pytest.raises(SystemExit, match="must be the qualifying route"):
        real_run.main()


def test_real_luna_revalidates_checkout_binding_between_stages(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[3]
    real_run = _real_run_module(root)
    monkeypatch.setattr(real_run, "_binding", lambda *_args: {"tree": "changed"})
    with pytest.raises(SystemExit, match="checkout binding changed before new lane"):
        real_run._assert_binding_unchanged(
            {"tree": "expected"}, Path("/redacted-host"), "provider/gpt-5.6-luna", "before new lane"
        )


def test_real_luna_acceptance_rejects_a_citationless_semantic_answer() -> None:
    root = Path(__file__).resolve().parents[3]
    real_run = _real_run_module(root)
    report = json.loads(
        (root / "evals/intent-reader-audit/real-luna-latest.json").read_text()
    )
    payloads = {
        lane: {
            "rows": [
                json.loads(json.dumps(row))
                for row in report["results"]
                if row["lane"] == lane
            ]
        }
        for lane in real_run.LANES
    }
    target = next(
        row for row in payloads["new"]["rows"]
        if row["reader"]["attempts_observed"] > 0
    )
    for lane in ("pre", "new"):
        for row in payloads[lane]["rows"]:
            row.setdefault("semantic_answer_published", False)
    target["semantic_answer_published"] = True
    target["citation_validity"] = None
    target["semantic_answer_evidence"] = [
        {
            "answer_sha256": "a" * 64,
            "answer_bytes": 1,
            "citations_published": 0,
            "citations_all_verified": False,
            "cache_reused": False,
            "matches_prior_uncached_answer": None,
        }
    ]
    errors = real_run._acceptance_errors(payloads, report["route"]["requested"])
    assert any("semantic answer lacked verified citations" in error for error in errors)


def test_repeated_answer_correctness_cannot_be_satisfied_by_only_one_answer() -> None:
    root = Path(__file__).resolve().parents[3]
    worker = _worker_module(root)
    expected = {"answer_matches": [r"retry_limit\s*:\s*7"]}
    correct, checks = worker.evaluate(
        expected,
        evidence=[],
        aggregates=[],
        answers=["retry_limit: 7", ""],
    )
    assert correct is False
    assert checks == [
        {
            "kind": "answer_matches",
            "expected_regex": r"retry_limit\s*:\s*7",
            "answers_observed": 2,
            "answers_matching": 1,
            "passed": False,
        }
    ]


def test_real_luna_acceptance_rejects_bad_per_answer_and_cache_evidence() -> None:
    root = Path(__file__).resolve().parents[3]
    real_run = _real_run_module(root)
    base_row = {
        "workflow": "fixture",
        "reader": {"attempts_observed": 0, "calls": []},
        "semantic_answer_published": True,
        "semantic_answer_evidence": [
            {
                "answer_sha256": "a" * 64,
                "answer_bytes": 10,
                "citations_published": 1,
                "citations_all_verified": True,
                "cache_reused": False,
                "matches_prior_uncached_answer": None,
            },
            {
                "answer_sha256": "b" * 64,
                "answer_bytes": 10,
                "citations_published": 0,
                "citations_all_verified": False,
                "cache_reused": True,
                "matches_prior_uncached_answer": False,
            },
        ],
        "citation_validity": False,
    }
    payloads = {
        "pre": {"rows": [json.loads(json.dumps(base_row))]},
        "new": {"rows": []},
    }
    errors = real_run._semantic_evidence_errors(payloads)
    assert any("answer-2: semantic answer lacked verified citations" in error for error in errors)
    assert any("answer-2: cached answer did not match its verified origin" in error for error in errors)


def real_run_totals(root: Path, rows: list[dict]):
    return _real_run_module(root).local_benchmark.totals({"rows": rows})
