#!/usr/bin/env python3
"""Execute the five audited workflows through legacy, PRE, and NEW code paths.

PRE is imported from an isolated ``git archive`` of the pinned baseline. NEW and the
owned legacy compactor port are imported from the working tree. The provider is a
deterministic, grounded fixture, but every product operation is really executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus.json"
WORKER = HERE / "worker.py"
BASELINE = "1686db6"
LANES = ("legacy_compactor", "pre", "new")


def digest_files(root: Path, relative_paths: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def run_worker(
    lane: str,
    package_root: Path,
    variant: str = "normal",
    *,
    provider_kind: str = "mock",
) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(package_root), str(ROOT / "evals")))
    completed = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            "--lane",
            lane,
            "--corpus",
            str(CORPUS),
            "--variant",
            variant,
            "--provider-kind",
            provider_kind,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900 if provider_kind == "live" else 120,
    )
    if completed.returncode:
        raise RuntimeError(f"{lane}/{variant} worker failed:\n{completed.stderr}")
    return json.loads(completed.stdout)


def extract_baseline(target: Path) -> Path:
    archive = target / "baseline.tar"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "--output",
            str(archive),
            BASELINE,
            "packages/core-py",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    with tarfile.open(archive) as bundle:
        bundle.extractall(target, filter="data")
    return target / "packages" / "core-py" / "src"


def normalize_origins(payload: dict[str, Any], label: str, package_root: Path) -> None:
    expected = (package_root / "context_shunt" / "__init__.py").resolve()
    for row in payload["rows"]:
        origin = Path(row["module_origin"])
        if origin != expected:
            raise RuntimeError(f"unexpected module origin for {label}: {origin}")
        row["module_origin"] = label


def indexed(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["workflow"]: row for row in payload["rows"]}


def totals(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload["rows"]
    calls = [call for row in rows for call in row["reader"]["calls"]]
    input_reported = [
        call for call in calls if call.get("reported_input_tokens") is not None
    ]
    output_reported = [
        call for call in calls if call.get("reported_output_tokens") is not None
    ]
    reported = [
        call
        for call in calls
        if call.get("reported_input_tokens") is not None
        and call.get("reported_output_tokens") is not None
    ]
    cache_reported = [
        call for call in calls if call.get("reported_cache_tokens") is not None
    ]
    main_bytes = sum(row["main_context_bytes_observed"] for row in rows)

    def core_total(field: str) -> int | None:
        values = [
            row["reader"][field]
            for row in rows
            if row["reader"]["attempts_observed"] > 0
        ]
        if not values or any(value is None for value in values):
            return None
        return sum(values)

    return {
        "workflows_correct": sum(bool(row["correct"]) for row in rows),
        "workflows_total": len(rows),
        "correctness_rate_observed": sum(bool(row["correct"]) for row in rows)
        / len(rows),
        "main_context_bytes_observed": main_bytes,
        "main_context_tokens_estimated_bytes_div_4": math.ceil(main_bytes / 4),
        "reader_attempts_observed": len(calls),
        "reader_attempts_usage_reported": len(reported),
        "reader_unknown_usage_attempts": len(calls) - len(reported),
        "reader_input_payload_bytes_observed": (
            sum(
                call.get(
                    "input_payload_bytes",
                    call.get("payload", {}).get("role_content_bytes", 0),
                )
                for call in calls
            )
            if calls
            else None
        ),
        "reader_output_payload_bytes_observed": (
            sum(call.get("output_payload_bytes", 0) for call in calls)
            if calls
            else None
        ),
        # These are fixture-returned Usage fields. Missing reports remain unknown;
        # each field is independently a lower bound and never reconstructed from a ratio.
        "reader_input_tokens_reported_lower_bound": (
            sum(call["reported_input_tokens"] for call in input_reported)
            if input_reported
            else None
        ),
        "reader_output_tokens_reported_lower_bound": (
            sum(call["reported_output_tokens"] for call in output_reported)
            if output_reported
            else None
        ),
        "reader_cache_tokens_reported_lower_bound": (
            sum(call["reported_cache_tokens"] for call in cache_reported)
            if cache_reported
            else None
        ),
        "reader_core_accounted_input_tokens": core_total("core_accounted_input_tokens"),
        "reader_core_accounted_output_tokens": core_total("core_accounted_output_tokens"),
        "reader_core_accounted_cache_tokens": core_total("core_accounted_cache_tokens"),
        "answer_cache_hits_observed": sum(
            row["answer_cache_hits_observed"] for row in rows
        ),
        "requery_bytes_observed": sum(row["requery_bytes_observed"] for row in rows),
        "full_read_bytes_observed": sum(
            row["full_read_bytes_observed"] for row in rows
        ),
        "harness_elapsed_ms_observed": round(
            sum(row["harness_elapsed_ms_observed"] for row in rows), 6
        ),
        "mock_delay_ms_configured_total": sum(
            row["mock_delay_ms_configured_total"] for row in rows
        ),
        "mock_delay_ms_observed_total": round(
            sum(row["mock_delay_ms_observed_total"] for row in rows), 6
        ),
    }


def acceptance_errors(lanes: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    legacy, pre, new = (indexed(lanes[name]) for name in LANES)
    if not all(row["correct"] for row in new.values()):
        errors.append("NEW did not derive every independently expected output")
    if new["session-2-minified-loki-counts"]["reader"]["attempts_observed"] != 0:
        errors.append("NEW minified-Loki aggregation unexpectedly called the provider")
    if pre["session-2-minified-loki-counts"]["correct"]:
        errors.append(
            "PRE unexpectedly satisfied the deterministic aggregate expectations"
        )
    repeated = "session-3-distinct-and-exact-repeat"
    if new[repeated]["answer_cache_hits_observed"] != 1:
        errors.append("NEW exact repeat did not produce exactly one observed cache hit")
    if not (
        new[repeated]["reader"]["attempts_observed"]
        < pre[repeated]["reader"]["attempts_observed"]
    ):
        errors.append("NEW exact repeat did not reduce actual provider attempts")
    for lane_rows in (legacy, pre, new):
        if (
            lane_rows["session-4-abandoned-pointer-requery"]["requery_bytes_observed"]
            != 19_912
        ):
            errors.append("one lane did not preserve the 19,912-byte requery loss")
            break
    last = "session-5-full-read-and-unread-pointer"
    if legacy[last]["full_read_bytes_observed"] != 17_601:
        errors.append("legacy did not execute the 17,601-byte full read")
    if pre[last]["full_read_bytes_observed"] != 17_601:
        errors.append("PRE did not execute the 17,601-byte full read")
    if new[last]["full_read_bytes_observed"] != 0:
        errors.append("NEW unexpectedly performed a full read")
    for workflow in new:
        hashes = {
            tuple(indexed(payload)[workflow]["source_sha256"])
            for payload in lanes.values()
        }
        if len(hashes) != 1:
            errors.append(
                f"lanes did not execute identical source content for {workflow}"
            )
    return errors


def red_checks(
    normal: dict[str, Any], no_cache: dict[str, Any], no_aggregation: dict[str, Any]
) -> list[dict[str, Any]]:
    normal_rows, cache_rows, aggregate_rows = map(
        indexed, (normal, no_cache, no_aggregation)
    )
    repeated = "session-3-distinct-and-exact-repeat"
    aggregate_ids = ("session-2-minified-loki-counts", repeated)
    return [
        {
            "name": "cache_bypass_changes_execution",
            "passed": (
                cache_rows[repeated]["reader"]["attempts_observed"]
                > normal_rows[repeated]["reader"]["attempts_observed"]
                and cache_rows[repeated]["answer_cache_hits_observed"]
                < normal_rows[repeated]["answer_cache_hits_observed"]
            ),
            "normal_attempts": normal_rows[repeated]["reader"]["attempts_observed"],
            "bypassed_attempts": cache_rows[repeated]["reader"]["attempts_observed"],
            "normal_cache_hits": normal_rows[repeated]["answer_cache_hits_observed"],
            "bypassed_cache_hits": cache_rows[repeated]["answer_cache_hits_observed"],
        },
        {
            "name": "aggregation_bypass_changes_execution_and_correctness",
            "passed": (
                sum(
                    aggregate_rows[key]["reader"]["attempts_observed"]
                    for key in aggregate_ids
                )
                > sum(
                    normal_rows[key]["reader"]["attempts_observed"]
                    for key in aggregate_ids
                )
                and any(not aggregate_rows[key]["correct"] for key in aggregate_ids)
            ),
            "normal_attempts": sum(
                normal_rows[key]["reader"]["attempts_observed"] for key in aggregate_ids
            ),
            "bypassed_attempts": sum(
                aggregate_rows[key]["reader"]["attempts_observed"]
                for key in aggregate_ids
            ),
            "normal_correct": {
                key: normal_rows[key]["correct"] for key in aggregate_ids
            },
            "bypassed_correct": {
                key: aggregate_rows[key]["correct"] for key in aggregate_ids
            },
        },
    ]


def markdown(report: dict[str, Any]) -> str:
    def shown(item: Any) -> str:
        return "unknown" if item is None else str(item)

    lines = [
        "# Five-workflow intent-reader execution benchmark",
        "",
        "This benchmark executes identical synthetic production-derived content through the owned legacy compactor, PRE code isolated from commit `1686db6`, and the NEW working-tree ShuntSession. The provider is a deterministic grounded fixture; payload bytes, attempts, returned usage, and elapsed time are observed during execution.",
        "",
        "| Lane | Correct | Main bytes (tokens est.) | Reader payload in/out bytes | Provider tokens in/out/cache* | Core accounted in/out/cache | Attempts (reported/unknown) | Answer-cache hits | Requery | Full read | Harness ms | Mock delay configured/observed ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for lane in LANES:
        value = report["totals"][lane]
        lines.append(
            f"| {lane} | {value['workflows_correct']}/{value['workflows_total']} | "
            f"{value['main_context_bytes_observed']} ({value['main_context_tokens_estimated_bytes_div_4']}) | "
            f"{shown(value['reader_input_payload_bytes_observed'])}/{shown(value['reader_output_payload_bytes_observed'])} | "
            f"{shown(value['reader_input_tokens_reported_lower_bound'])}/{shown(value['reader_output_tokens_reported_lower_bound'])}/{shown(value['reader_cache_tokens_reported_lower_bound'])} | "
            f"{shown(value['reader_core_accounted_input_tokens'])}/{shown(value['reader_core_accounted_output_tokens'])}/{shown(value['reader_core_accounted_cache_tokens'])} | "
            f"{value['reader_attempts_observed']} ({value['reader_attempts_usage_reported']}/{value['reader_unknown_usage_attempts']}) | "
            f"{value['answer_cache_hits_observed']} | {value['requery_bytes_observed']} | "
            f"{value['full_read_bytes_observed']} | {value['harness_elapsed_ms_observed']:.3f} | "
            f"{value['mock_delay_ms_configured_total']:.1f}/{value['mock_delay_ms_observed_total']:.3f} |"
        )
    lines += [
        "",
        "\\* Provider tokens are only values returned by the instrumented fixture. A deliberately missing usage report remains an unknown attempt, so each token total is a reported lower bound—not a completion-ratio estimate. The fixture uses bytes/4 as its explicit token tariff; these fields are copied from its actual `ModelResponse.usage`, not inferred afterward. Main-context tokens alone are estimated from observed bytes at bytes/4.",
        "",
        "Correctness is computed from emitted answers/extractions against independently declared expectations in `corpus.json`. `Harness ms` is measured elapsed execution time; configured and observed mock delay are reported separately, with no arithmetic controlled-time substitute.",
        "",
        "## Red checks",
        "",
    ]
    for check in report["red_checks"]:
        state = "PASS" if check["passed"] else "FAIL"
        lines.append(
            f"- `{check['name']}`: **{state}** — `{json.dumps(check, sort_keys=True)}`"
        )
    lines += [
        "",
        "Workflow 4 retains 19,912 requery bytes in all lanes. Workflow 5 executes a 17,601-byte full read in legacy/PRE and bounded search in NEW. Those losses are measured operations, not assigned profile fields.",
        "",
        f"Corpus SHA-256: `{report['corpus_sha256']}`.",
        "",
        "Machine-readable evidence: [`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json).",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument(
        "--candidate-new-variant",
        choices=("normal", "no-cache", "no-aggregation"),
        default="normal",
    )
    args = parser.parse_args()
    current_package = ROOT / "packages" / "core-py" / "src"
    with TemporaryDirectory(prefix="context-shunt-pre-") as temporary:
        baseline_package = extract_baseline(Path(temporary))
        payloads = {
            "legacy_compactor": run_worker("legacy_compactor", current_package),
            "pre": run_worker("pre", baseline_package),
            "new": run_worker("new", current_package, args.candidate_new_variant),
        }
        normal = (
            payloads["new"]
            if args.candidate_new_variant == "normal"
            else run_worker("new", current_package)
        )
        no_cache = run_worker("new", current_package, "no-cache")
        no_aggregation = run_worker("new", current_package, "no-aggregation")
        labels = {
            "legacy_compactor": "working_tree:packages/core-py/src/context_shunt/__init__.py",
            "pre": f"isolated_git_archive:{BASELINE}:packages/core-py/src/context_shunt/__init__.py",
            "new": "working_tree:packages/core-py/src/context_shunt/__init__.py",
        }
        package_roots = {
            "legacy_compactor": current_package,
            "pre": baseline_package,
            "new": current_package,
        }
        for name, payload in payloads.items():
            normalize_origins(payload, labels[name], package_roots[name])
        normalized_ids = {id(payloads["new"])}
        for payload in (normal, no_cache, no_aggregation):
            if id(payload) not in normalized_ids:
                normalize_origins(payload, labels["new"], current_package)
                normalized_ids.add(id(payload))

    checks = red_checks(normal, no_cache, no_aggregation)
    raw = CORPUS.read_bytes()
    report = {
        "schema": "context_shunt.intent_reader_execution_benchmark.v2",
        "baseline_commit": BASELINE,
        "candidate_new_variant": args.candidate_new_variant,
        "corpus_sha256": hashlib.sha256(raw).hexdigest(),
        "corpus_declares_lane_profiles": False,
        "execution": {
            "legacy": "actual owned compact_tool_result route",
            "pre": f"actual ShuntSession/Reader imported from isolated git archive {BASELINE}",
            "new": "actual working-tree ShuntSession/Reader/inspect aggregate/cache routes",
            "provider": "deterministic grounded instrumented fixture",
            "timing": "observed harness elapsed; fixture delay separately reported",
        },
        "implementation_sha256": {
            "pre_git_tree": git_output("rev-parse", f"{BASELINE}^{{tree}}"),
            "new_python_relevant_files": digest_files(
                ROOT,
                [
                    "packages/core-py/src/context_shunt/aggregate.py",
                    "packages/core-py/src/context_shunt/inspect.py",
                    "packages/core-py/src/context_shunt/reader.py",
                    "packages/core-py/src/context_shunt/session.py",
                ],
            ),
        },
        "results": [row for lane in LANES for row in payloads[lane]["rows"]],
        "totals": {lane: totals(payloads[lane]) for lane in LANES},
        "red_checks": checks,
    }
    errors = acceptance_errors(payloads)
    errors.extend(check["name"] for check in checks if not check["passed"])
    report["acceptance"] = {"passed": not errors, "errors": errors}
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(encoded)
    if args.markdown_output:
        args.markdown_output.write_text(markdown(report))
    print(encoded, end="")
    if errors:
        raise SystemExit("benchmark acceptance failed: " + "; ".join(errors))


if __name__ == "__main__":
    main()
