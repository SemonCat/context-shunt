"""Safe synthetic bookkeeping for the bounded 2026-09-16 regression metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.gate_capability, pytest.mark.gate_accounting]

CORPUS = (
    Path(__file__).resolve().parents[3]
    / "evals"
    / "production-regressions"
    / "corpus.json"
)


def _load():
    return json.loads(CORPUS.read_text())


def _signature(case):
    return (
        case["tool"],
        case["status"],
        case["code"],
        case["failure_detail"],
        case["complete"],
        tuple(sorted(set(case["omissions"]))),
        case["label"],
        case["usage_complete"],
        case["citations_verified"],
    )


def test_corpus_has_every_bounded_metadata_outcome_class_once():
    cases = _load()["observed_outcome_classes"]
    assert len(cases) == 18
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({_signature(case) for case in cases}) == len(cases)
    assert {case["label"] for case in cases} == {
        "model_generated_answer",
        "legacy_compaction",
        "deterministic_extraction",
        "no_model_output",
        "host_boundary",
    }


def test_provenance_and_missing_raw_coverage_are_explicit():
    corpus = _load()
    provenance = corpus["provenance"]
    assert provenance["contains_source_prose"] is False
    assert provenance["exact_raw_replay_claimed"] is False
    retention = corpus["retention"]
    assert retention["retained_raw_sources"] == retention["retained_raw_artifacts"] == 3
    assert retention["expired_payloads"] == "unavailable"
    assert retention["exact_replay_scope"] == "only_explicitly_retained_sources"


def test_cutoff_mismatch_stays_unresolved_and_tokens_keep_distinct_meanings():
    bookkeeping = _load()["bookkeeping"]
    initial = bookkeeping["initial_cutoff"]
    newer = bookkeeping["newer_cutoff"]
    assert initial == {"accounting_spills": 53, "transcript_spills": 52, "reconciled": False}
    assert newer["transcript_spills"] == 53 and newer["reconciled"] is False
    assert bookkeeping["cutoffs_are_distinct"] is True
    assert bookkeeping["count_adjustment_is_not_reconciliation"] is True
    tokens = bookkeeping["token_metrics"]
    assert tokens["reader_total_tokens_reported"] == (
        tokens["reader_input_tokens_reported"] + tokens["reader_output_tokens_reported"]
    )
    assert tokens["net_tokens_saved_estimate"] == (
        tokens["main_context_tokens_saved_estimate"]
        - tokens["reader_total_tokens_reported"]
    )
    assert tokens["attempts_usage_complete"] < tokens["attempts_started"]
    assert tokens["usage_complete"] is False


def test_correlation_and_reuse_are_identity_based_not_count_or_proximity_based():
    corpus = _load()
    correlation = corpus["correlation"]
    identity = correlation["synthetic_identity"]
    assert identity["request_id"].removeprefix("req_") == identity["tool_call_id"]
    assert identity["operation_id"].startswith("acc_")
    assert correlation["join_by_identity_not_count"] is True
    reuse = {case["id"]: case for case in corpus["reuse_cases"]}
    assert reuse["same-handle-exact-query"]["cache_reused"] is True
    assert reuse["same-handle-new-query"]["cache_reused"] is False
    assert reuse["deterministic-after-reader-failure"]["legitimate_recovery"] is True
    assert reuse["producer-requery"]["correlation"] == "unknown_without_host_event"
