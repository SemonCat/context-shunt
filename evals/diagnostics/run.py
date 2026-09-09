"""Bounded per-run diagnostics for a live ``eval luna`` pass, without another full 120-run.

Scratch/dev tooling, like ``evals/bridges/openclaw_cli.py`` beside it - not shipped in
``packages/``, and not itself scored. It exists to answer one question the aggregate
``eval-luna.json`` report cannot: *which* corpus items and categories account for a given
count of correctness misses, citation-support misses, leaks, or identity-unknown runs, and
whether a given item's failure repeats (a deterministic bug) or does not (host/provider
flakiness).

Every field this prints or writes is drawn from :func:`tests.test_eval_luna._score_run` -
the exact function the scored gate itself uses - so a diagnostic run can never silently
disagree with what the gate measured. It never retains a source, question, answer, or
quote: only the bounded fields below, matching the discipline ``test_eval_luna.py``
already documents for its own aggregate report.

Usage::

    CONTEXT_SHUNT_LUNA_BRIDGE=bridges.openclaw_cli:complete \\
    PYTHONPATH=evals \\
      ./.venv/bin/python evals/diagnostics/run.py --items fact_port,fact_retry --runs 1

    # All 40 items, one run each - "start with one pass", not another blind 120-run.
    ./.venv/bin/python evals/diagnostics/run.py --runs 1

Recorded per run: item id, run index, category, answerable, status, code, a short
fail-closed ``reason`` token, expected-fact-present, locator-match, semantic-support,
the raw reply shape(s) this run's calls used, whether any claim referenced an id it never
declared, the observed model identity (or none), the leaked-region count, and the call
count. Output is printed as JSON lines and, if ``--out`` is given, appended there -
default ``reports/eval-luna-diagnostics.jsonl``, which is gitignored like every other
``reports/`` artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORE_SRC = REPO / "packages" / "core-py" / "src"
CORE_ROOT = REPO / "packages" / "core-py"
BRIDGE_ENV = "CONTEXT_SHUNT_LUNA_BRIDGE"


def _wire_imports() -> None:
    for path in (str(CORE_SRC), str(CORE_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)


_wire_imports()

from context_shunt.binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE  # noqa: E402
from context_shunt.citations import CitationVerifier  # noqa: E402
from context_shunt.errors import ShuntError  # noqa: E402
from context_shunt.limits import DEFAULT_LIMITS, READER_MODEL  # noqa: E402
from context_shunt.provider import HostBridgeProvider  # noqa: E402
from context_shunt.reader import Reader  # noqa: E402
from context_shunt.snapshot import record_count, resolve_pointer, snapshot_bytes  # noqa: E402

import tests.test_eval_luna as tel  # noqa: E402


@dataclass(frozen=True)
class RunDiagnostic:
    item_id: str
    run: int
    category: str
    answerable: bool
    status: str
    code: str | None
    reason: str
    facts_present: bool | None
    located: bool | None
    supported: bool | None
    raw_reply_shapes: dict[str, int]
    claim_referenced_unknown_id: bool
    observed_model: str | None
    leak_count: int
    call_count: int


def _reason(item: dict, answer: str, score) -> str:
    if not item["answerable"]:
        return "OK" if not answer.strip() else "FALSE_COMPLETE"
    if not answer.strip():
        return "NO_ANSWER"
    parts = []
    if not score.facts_present:
        parts.append("FACTS_MISSING")
    if not score.located:
        parts.append("LOCATOR_MISMATCH")
    if not score.supported:
        parts.append("QUOTE_NOT_SUPPORTING")
    return "+".join(parts) if parts else "OK"


def _load_bridge():
    spec = os.environ.get(BRIDGE_ENV, "")
    if not spec or ":" not in spec:
        raise SystemExit(
            f"set {BRIDGE_ENV}=module:callable (e.g. bridges.openclaw_cli:complete)"
        )
    import importlib

    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def _run_one(item: dict, run_index: int, tmp_root: Path, bridge_call) -> RunDiagnostic:
    media = (
        JSON_MEDIA_TYPE if item["media_type"] == "application/json" else TEXT_MEDIA_TYPE
    )
    recorder = tel._ClaimsRecorder(bridge_call)
    provider = HostBridgeProvider(recorder, DEFAULT_LIMITS, READER_MODEL)
    registry = tel._eval_registry(tmp_root)
    try:
        entry = registry.register(
            "eval",
            snapshot_bytes(item["content"].encode("utf-8"), media_type_hint=media),
        )
    except ShuntError as exc:
        return RunDiagnostic(
            item_id=item["id"],
            run=run_index,
            category=item["category"],
            answerable=item["answerable"],
            status="refused",
            code=f"{exc.code}/{exc.detail}",
            reason=f"REFUSED_BY_CORE:{exc.code}/{exc.detail}",
            facts_present=None,
            located=None,
            supported=None,
            raw_reply_shapes={},
            claim_referenced_unknown_id=False,
            observed_model=None,
            leak_count=0,
            call_count=0,
        )

    selector: dict = {"kind": "all"}
    if item["media_type"] == "application/json" and item["expected_locator"]:
        pointer = item["expected_locator"]["pointer"]
        records = record_count(resolve_pointer(entry.snapshot.json_value, pointer))
        selector = {"kind": "records", "pointer": pointer, "start": 1, "end": records}

    result = Reader(registry, provider).answer(
        "eval",
        {
            "schema_version": "1.0",
            "request_id": f"diag_{item['id']}_{run_index}",
            "operation": "read",
            "question": item["question"],
            "sources": [
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": selector,
                }
            ],
            "budgets": {
                "max_chunks": 8,
                "max_answer_bytes": 8192,
                "deadline_ms": 60000,
            },
        },
    )
    envelope = result.envelope
    answer = envelope.get("answer", "")
    verifier = CitationVerifier(registry)
    score = tel._score_run(item, envelope, verifier)
    observed = tel._observed_model(envelope)

    return RunDiagnostic(
        item_id=item["id"],
        run=run_index,
        category=item["category"],
        answerable=item["answerable"],
        status=envelope["status"],
        code=envelope["code"],
        reason=_reason(item, answer, score),
        facts_present=score.facts_present,
        located=score.located,
        supported=score.supported,
        raw_reply_shapes=dict(recorder.shape_totals),
        claim_referenced_unknown_id=recorder.claim_referenced_unknown_id,
        observed_model=observed,
        leak_count=score.leaked_regions,
        call_count=result.cost.attempts_started,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--items",
        default="all",
        help="comma-separated corpus item ids, or 'all' for every item (default: all)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="runs per selected item (default: 1 - one pass)",
    )
    parser.add_argument(
        "--out",
        default=str(REPO / "reports" / "eval-luna-diagnostics.jsonl"),
        help="path to append JSON-line records to (gitignored 'reports/' by default)",
    )
    args = parser.parse_args()

    bridge_call = _load_bridge()
    corpus = tel._corpus()
    all_items = {it["id"]: it for it in corpus["items"]}
    if args.items == "all":
        selected = list(all_items.values())
    else:
        wanted = [s.strip() for s in args.items.split(",") if s.strip()]
        unknown = [w for w in wanted if w not in all_items]
        if unknown:
            raise SystemExit(f"unknown item id(s): {unknown}")
        selected = [all_items[w] for w in wanted]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[RunDiagnostic] = []
    tmp_base = Path(f"/tmp/shunt-diag-{os.getpid()}")
    with out_path.open("a", encoding="utf-8") as fh:
        for item in selected:
            for run in range(args.runs):
                row = _run_one(item, run, tmp_base / f"{item['id']}-{run}", bridge_call)
                rows.append(row)
                line = json.dumps(asdict(row), separators=(",", ":"))
                print(line)
                fh.write(line + "\n")
                fh.flush()

    import shutil

    shutil.rmtree(tmp_base, ignore_errors=True)

    print(f"--- {len(rows)} runs, written to {out_path} ---")
    misses = [r for r in rows if r.reason != "OK" and r.status != "refused"]
    print(f"non-OK reasons (excluding expected core refusals): {len(misses)}")
    for row in rows:
        if row.reason != "OK":
            print(
                f"  {row.item_id:28s} run={row.run} reason={row.reason} leak={row.leak_count}"
            )


if __name__ == "__main__":
    main()
