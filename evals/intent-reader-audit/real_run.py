#!/usr/bin/env python3
"""Run the five sanitized workflows through the real, role-preserving Luna route.

This is intentionally separate from ``run.py``. That file is the deterministic
execution benchmark and its provider is a fixture; this file is opt-in, uses the real
OpenClaw runtime, and never labels mock timing or token arithmetic as provider evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORPUS = HERE / "corpus.json"
BASELINE = "1686db6"
MODEL = "gpt-5.6-luna"
DEFAULT_ROUTE = "sub2api-openai/gpt-5.6-luna"
LANES = ("legacy_compactor", "pre", "new")

sys.path.insert(0, str(HERE))
import run as local_benchmark  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [call for row in payload["rows"] for call in row["reader"]["calls"]]


def _live_totals(payload: dict[str, Any]) -> dict[str, Any]:
    base = local_benchmark.totals(payload)
    calls = _calls(payload)
    base.update(
        {
            "lane_elapsed_ms_observed": payload["lane_elapsed_ms_observed"],
            "completed_attempts": sum(call.get("status") == "completed" for call in calls),
            "timed_out_attempts": sum(
                call.get("status") == "timed_out_usage_unknown" for call in calls
            ),
            "failed_attempts": sum(
                call.get("status") == "failed_usage_unknown" for call in calls
            ),
            "in_flight_or_late_unknown_attempts": sum(
                call.get("status") == "in_flight_usage_unknown" for call in calls
            ),
            "transport_input_bytes_observed": (
                sum(
                    call["transport_input_bytes"]
                    for call in calls
                    if isinstance(call.get("transport_input_bytes"), int)
                )
                if calls
                else None
            ),
            "transport_output_bytes_observed": (
                sum(
                    call["transport_output_bytes"]
                    for call in calls
                    if isinstance(call.get("transport_output_bytes"), int)
                )
                if calls
                else None
            ),
            "coverage_envelopes": sum(
                len(row.get("coverage_observed", [])) for row in payload["rows"]
            ),
            "coverage_complete": sum(
                item.get("complete") is True
                for row in payload["rows"]
                for item in row.get("coverage_observed", [])
            ),
            "citation_applicable_workflows": sum(
                row["citation_validity"] is not None for row in payload["rows"]
            ),
            "citation_valid_workflows": sum(
                row["citation_validity"] is True for row in payload["rows"]
            ),
        }
    )
    return base


def _transport_errors(payloads: dict[str, dict[str, Any]], route: str) -> list[str]:
    errors: list[str] = []
    expected_provider, expected_model = route.split("/", 1)
    for lane in ("pre", "new"):
        payload = payloads[lane]
        for row in payload["rows"]:
            core_attempts = row["reader"]["attempts_observed"]
            calls = row["reader"]["calls"]
            if core_attempts != len(calls):
                errors.append(f"{lane}/{row['workflow']}: attempt ledger gap")
            for call in calls:
                prefix = f"{lane}/{row['workflow']}/call-{call['call']}"
                attestation = call.get("payload", {})
                status = call.get("status")
                if call.get("requested_route") != route:
                    errors.append(f"{prefix}: requested route was not recorded exactly")
                if call.get("requested_model") != expected_model:
                    errors.append(f"{prefix}: requested model was not recorded exactly")
                if status == "completed":
                    if call.get("route_attested") is not True:
                        errors.append(f"{prefix}: route was not attested")
                    if call.get("resolved_provider") != expected_provider:
                        errors.append(f"{prefix}: wrong or unknown provider")
                    if call.get("resolved_model") != expected_model:
                        errors.append(f"{prefix}: wrong or unknown model")
                    if call.get("execution_mode") != "isolated-agent-runtime":
                        errors.append(f"{prefix}: wrong or unknown execution mode")
                elif status in {
                    "timed_out_usage_unknown",
                    "in_flight_usage_unknown",
                }:
                    if any(
                        call.get(key) is not None
                        for key in (
                            "reported_input_tokens",
                            "reported_output_tokens",
                            "reported_cache_tokens",
                        )
                    ):
                        errors.append(f"{prefix}: unknown attempt was assigned usage")
                else:
                    errors.append(f"{prefix}: provider attempt failed")
                if attestation.get("roles") != ["system", "user"]:
                    errors.append(f"{prefix}: role split missing")
                if attestation.get("system_is_exact_reader_contract") is not True:
                    errors.append(f"{prefix}: system role was not the exact reader contract")
                if attestation.get("user_is_exact_reader_template") is not True:
                    errors.append(f"{prefix}: user role carried material outside the reader template")
                if attestation.get("parent_context_canary_absent") is not True:
                    errors.append(f"{prefix}: parent-context canary reached the reader payload")
                if not 0 < int(attestation.get("role_content_bytes") or 0) <= 262_144:
                    errors.append(f"{prefix}: role content was outside the transport bound")
                if not 0 < int(attestation.get("max_output_tokens") or 0) <= 2_048:
                    errors.append(f"{prefix}: output cap was outside the transport bound")
                if not 0 < int(attestation.get("timeout_ms") or 0) <= 60_000:
                    errors.append(f"{prefix}: deadline was outside the transport bound")
    return errors


def _acceptance_errors(payloads: dict[str, dict[str, Any]], route: str) -> list[str]:
    errors = local_benchmark.acceptance_errors(payloads)
    errors.extend(_transport_errors(payloads, route))
    new = local_benchmark.indexed(payloads["new"])
    for workflow in (
        "session-2-minified-loki-counts",
        "session-4-abandoned-pointer-requery",
        "session-5-full-read-and-unread-pointer",
    ):
        if new[workflow]["reader"]["attempts_observed"] != 0:
            errors.append(f"NEW deterministic workflow {workflow} called Luna")
    for lane in ("pre", "new"):
        for row in payloads[lane]["rows"]:
            if row["citation_validity"] is False:
                errors.append(f"{lane}/{row['workflow']}: published citation failed verification")
    return errors


def _mock_red_evidence() -> dict[str, Any]:
    path = HERE / "latest.json"
    report = json.loads(path.read_text())
    return {
        "kind": "deterministic_fixture_sabotage_not_real_provider_evidence",
        "artifact": "evals/intent-reader-audit/latest.json",
        "corpus_sha256": report.get("corpus_sha256"),
        "checks": report.get("red_checks", []),
    }


def _markdown(report: dict[str, Any]) -> str:
    def shown(value: Any) -> str:
        return "unknown" if value is None else str(value)

    lines = [
        "# Five-workflow real Luna validation",
        "",
        "This opt-in run executed the same five sanitized fixtures through the owned legacy compactor, PRE `ShuntSession`/`Reader` isolated from commit `1686db6`, and the NEW working-tree implementation. Semantic reads used OpenClaw's supported in-host `runtime.llm.complete` isolated-agent transport with distinct system and user roles. Deterministic NEW aggregation/search cases did not call a model.",
        "",
        f"Resolved route required and observed on every completed call: `{report['route']['requested']}`.",
        "",
        "| Lane | Correct | Citations valid/applicable | Coverage complete/total | Calls (reported/unknown) | Provider tokens in/out/cache* | Role bytes | Transport bytes in/out | Lane wall ms | Cache hits |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for lane in LANES:
        value = report["totals"][lane]
        lines.append(
            f"| {lane} | {value['workflows_correct']}/{value['workflows_total']} | "
            f"{value['citation_valid_workflows']}/{value['citation_applicable_workflows']} | "
            f"{value['coverage_complete']}/{value['coverage_envelopes']} | "
            f"{value['reader_attempts_observed']} ({value['reader_attempts_usage_reported']}/{value['reader_unknown_usage_attempts']}) | "
            f"{shown(value['reader_input_tokens_reported_lower_bound'])}/{shown(value['reader_output_tokens_reported_lower_bound'])}/{shown(value['reader_cache_tokens_reported_lower_bound'])} | "
            f"{shown(value['reader_input_payload_bytes_observed'])} | "
            f"{shown(value['transport_input_bytes_observed'])}/{shown(value['transport_output_bytes_observed'])} | "
            f"{value['lane_elapsed_ms_observed']:.3f} | {value['answer_cache_hits_observed']} |"
        )
    lines += [
        "",
        "\\* Token values are provider-reported lower bounds only. Missing input/output or cache usage remains `unknown`; no count is reconstructed from bytes or a completion ratio. Main-context byte/token estimates are separate in the JSON evidence.",
        "",
        "Wall time is the observed duration of each independently launched lane, including its own initialization. It is reported as execution evidence, not as an end-to-end time-savings claim; the legacy lane makes no provider calls and is not latency-comparable to PRE/NEW.",
        "",
        f"All {report['attempt_outcomes']['completed']} real attempts completed; this accepted run observed {report['attempt_outcomes']['timed_out']} timeouts, {report['attempt_outcomes']['failed']} failures, and {report['attempt_outcomes']['late_or_in_flight_usage_unknown']} late/in-flight attempts with unknown usage. A nonzero value would remain in those fields rather than being reconstructed from response bytes.",
        "",
        "Each real call retains only redacted payload evidence: role names, byte lengths, SHA-256 digests, exact-template booleans, bounded caps, resolved route/execution identity, status, duration, and provider usage when reported. It stores no system prompt, question, source excerpt, completion, citation quote, credential, or host stderr.",
        "",
        "The cache/aggregation red checks remain explicitly fixture-based sabotage checks in `latest.json`; they demonstrate that bypassing either feature changes measured calls/correctness but are not relabeled as real-provider evidence. No deterministic case was forced through Luna for this run.",
        "",
        f"Acceptance: **{'PASS' if report['acceptance']['passed'] else 'FAIL'}**.",
        "",
        "Exact command:",
        "",
        "```sh",
        report["command"],
        "```",
        "",
        "Machine-readable redacted evidence: [`evals/intent-reader-audit/real-luna-latest.json`](../evals/intent-reader-audit/real-luna-latest.json).",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--resume-lanes", type=Path)
    args = parser.parse_args()
    if os.environ.get("CONTEXT_SHUNT_LUNA_EVAL") != "1":
        raise SystemExit("NOT_RUN: set CONTEXT_SHUNT_LUNA_EVAL=1")
    if os.environ.get("CONTEXT_SHUNT_OPENCLAW_SERVER"):
        raise SystemExit("NOT_RUN: real evidence refuses CONTEXT_SHUNT_OPENCLAW_SERVER override")
    host_root_raw = os.environ.get("CONTEXT_SHUNT_OPENCLAW_ROOT", "")
    host_root = Path(host_root_raw)
    if not host_root.is_dir():
        raise SystemExit("NOT_RUN: CONTEXT_SHUNT_OPENCLAW_ROOT is not a checkout")
    route = os.environ.get("CONTEXT_SHUNT_OPENCLAW_ROUTE", DEFAULT_ROUTE)
    if "/" not in route or route.split("/", 1)[1] != MODEL:
        raise SystemExit("NOT_RUN: configured route is not gpt-5.6-luna")

    lane_checkpoint = HERE / "real-luna-lanes-latest.json"
    if args.resume_lanes:
        payloads = json.loads(args.resume_lanes.read_text())
    else:
        current_package = ROOT / "packages" / "core-py" / "src"
        with TemporaryDirectory(prefix="context-shunt-real-pre-") as temporary:
            baseline_package = local_benchmark.extract_baseline(Path(temporary))
            payloads = {
                "legacy_compactor": local_benchmark.run_worker(
                    "legacy_compactor", current_package, provider_kind="live"
                ),
                "pre": local_benchmark.run_worker(
                    "pre", baseline_package, provider_kind="live"
                ),
                "new": local_benchmark.run_worker(
                    "new", current_package, provider_kind="live"
                ),
            }
            labels = {
                "legacy_compactor": "working_tree:packages/core-py/src/context_shunt/__init__.py",
                "pre": f"isolated_git_archive:{BASELINE}:packages/core-py/src/context_shunt/__init__.py",
                "new": "working_tree:packages/core-py/src/context_shunt/__init__.py",
            }
            roots = {
                "legacy_compactor": current_package,
                "pre": baseline_package,
                "new": current_package,
            }
            for lane, payload in payloads.items():
                local_benchmark.normalize_origins(payload, labels[lane], roots[lane])
        # Written before any aggregate/report rendering. It contains the same redacted
        # rows as the final artifact and lets a presentation-only failure be repaired
        # without spending another real provider call.
        lane_checkpoint.write_text(json.dumps(payloads, indent=2, sort_keys=True) + "\n")

    corpus_raw = CORPUS.read_bytes()
    errors = _acceptance_errors(payloads, route)
    attempt_outcomes = {
        "completed": sum(
            _live_totals(payloads[lane])["completed_attempts"]
            for lane in ("pre", "new")
        ),
        "timed_out": sum(
            _live_totals(payloads[lane])["timed_out_attempts"]
            for lane in ("pre", "new")
        ),
        "failed": sum(
            _live_totals(payloads[lane])["failed_attempts"]
            for lane in ("pre", "new")
        ),
        "late_or_in_flight_usage_unknown": sum(
            _live_totals(payloads[lane])["in_flight_or_late_unknown_attempts"]
            for lane in ("pre", "new")
        ),
    }
    relevant_files = [
        "evals/bridges/openclaw_inhost.py",
        "evals/bridges/openclaw_inhost_server.mts",
        "evals/intent-reader-audit/corpus.json",
        "evals/intent-reader-audit/worker.py",
        "packages/core-py/src/context_shunt/aggregate.py",
        "packages/core-py/src/context_shunt/reader.py",
        "packages/core-py/src/context_shunt/session.py",
    ]
    report = {
        "schema": "context_shunt.intent_reader_real_luna.v1",
        "evaluated_worktree_head": _git(ROOT, "rev-parse", "HEAD"),
        "baseline_commit": BASELINE,
        "corpus_sha256": hashlib.sha256(corpus_raw).hexdigest(),
        "corpus_contains_production_content": False,
        "route": {
            "requested": route,
            "model_required": MODEL,
            "transport_required": "runtime.llm.complete/isolated-agent-runtime",
            "roles_required": ["system", "user"],
            "server_override_refused": True,
        },
        "host": {
            "kind": "openclaw_source_checkout",
            "git_commit": _git(host_root, "rev-parse", "HEAD"),
        },
        "implementation": {
            "working_tree_relevant_files": relevant_files,
            "working_tree_relevant_files_sha256": local_benchmark.digest_files(
                ROOT, relevant_files
            ),
            "pre_git_tree": _git(ROOT, "rev-parse", f"{BASELINE}^{{tree}}"),
        },
        "execution": {
            "legacy": "actual owned compact_tool_result route; no model applicable",
            "pre": f"actual ShuntSession/Reader from isolated git archive {BASELINE}",
            "new": "actual working-tree ShuntSession/Reader/inspect aggregation/cache",
            "provider": "real Luna through supported OpenClaw in-host isolated runtime",
            "retry_bound": "core defaults; one transient and one format retry per chunk",
            "output_cap_tokens_per_call": 2048,
            "request_deadline_ms": 60000,
            "model_call_deadline_ms": 45000,
        },
        "results": [row for lane in LANES for row in payloads[lane]["rows"]],
        "totals": {lane: _live_totals(payloads[lane]) for lane in LANES},
        "attempt_outcomes": attempt_outcomes,
        "parent_context_canary_sha256": hashlib.sha256(
            os.environ.get(
                "CONTEXT_SHUNT_EVAL_PARENT_CONTEXT_CANARY",
                "PRIVATE_PARENT_CONTEXT_CANARY_DO_NOT_SEND",
            ).encode("utf-8")
        ).hexdigest(),
        "red_check": _mock_red_evidence(),
        "acceptance": {"passed": not errors, "errors": errors},
        "command": (
            "CONTEXT_SHUNT_LUNA_EVAL=1 "
            f"CONTEXT_SHUNT_OPENCLAW_ROOT={host_root} "
            f"CONTEXT_SHUNT_OPENCLAW_ROUTE={route} "
            "CONTEXT_SHUNT_EVAL_PARENT_CONTEXT_CANARY=PRIVATE_PARENT_CONTEXT_CANARY_7f9070 "
            ".venv/bin/python evals/intent-reader-audit/real_run.py "
            "--json-output evals/intent-reader-audit/real-luna-latest.json "
            "--markdown-output docs/five-workflow-real-luna.md"
        ),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(encoded)
    if args.markdown_output:
        args.markdown_output.write_text(_markdown(report))
    print(encoded, end="")
    if errors:
        raise SystemExit("real Luna acceptance failed; see redacted report")


if __name__ == "__main__":
    main()
