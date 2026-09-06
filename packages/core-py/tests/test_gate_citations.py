"""unit citations: verification is mechanical, and only the verifier writes ``verified``."""

from __future__ import annotations

import json
import time

import pytest

from context_shunt.binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from context_shunt.citations import CitationVerifier, strip_unsupported_assertions
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.reader import Reader
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes
from tests.support import FakeLuna, answer_json

pytestmark = pytest.mark.gate_citations


def _registry_with(cases):
    registry = SourceRegistry()
    entries = {}
    for name, spec in cases["sources"].items():
        media = JSON_MEDIA_TYPE if spec["media_type"] == "application/json" else TEXT_MEDIA_TYPE
        entries[name] = registry.register(
            "sess_a", snapshot_bytes(spec["content"].encode(), media_type_hint=media)
        )
    return registry, entries


def test_every_conformance_case(citation_cases):
    registry, entries = _registry_with(citation_cases)
    verifier = CitationVerifier(registry)
    failures = []
    for case in citation_cases["cases"]:
        entry = entries[case["source"]]
        session = "sess_b" if case.get("foreign_session") else "sess_a"
        if case.get("expired"):
            expiring = SourceRegistry()
            handle = expiring.register("sess_a", entry.snapshot)
            expiring._time = lambda: time.time() + 10**6  # noqa: SLF001 - TTL fast-forward
            result = CitationVerifier(expiring).verify(
                "sess_a",
                {
                    "source_id": handle.source_id,
                    "snapshot_id": handle.snapshot.snapshot_id,
                    "locator": case["locator"],
                    "quote": case["quote"],
                },
            )
        else:
            result = verifier.verify(
                session,
                {
                    "source_id": case.get("source_id", entry.source_id),
                    "snapshot_id": case.get("snapshot_id", entry.snapshot.snapshot_id),
                    "locator": case["locator"],
                    "quote": case["quote"],
                },
            )
        want = (case["expect"]["verified"], case["expect"]["reason"])
        got = (result.verified, result.reason.value)
        if want != got:
            failures.append((case["id"], want, got))
    assert not failures, failures


def test_conformance_corpus_covers_text_and_json_and_failures(citation_cases):
    reasons = {c["expect"]["reason"] for c in citation_cases["cases"]}
    assert len(citation_cases["cases"]) >= 25
    assert {"OK", "QUOTE_NOT_FOUND", "LINE_OUT_OF_RANGE", "SNAPSHOT_MISMATCH"} <= reasons


def test_model_claiming_verified_does_not_make_it_verified():
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(b"alpha\nbeta\n"))
    reply = json.dumps(
        {
            "answer": "It says gamma [c1].",
            "citations": [
                {"id": "c1", "line_start": 1, "line_end": 1, "quote": "gamma", "verified": True}
            ],
        }
    )
    luna = FakeLuna(replies=[reply])
    env = Reader(registry, luna).answer("sess", _req(entry))
    assert env["code"] == "CITATION_INVALID"
    assert env["citations"] == []
    assert env["answer"] == ""


def test_assertions_without_valid_evidence_are_removed_but_valid_ones_survive():
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(b"alpha\nbeta\n"))
    reply = answer_json(
        "The first line is alpha [c1]. The third line is gamma [c2].",
        [
            {"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha"},
            {"id": "c2", "line_start": 3, "line_end": 3, "quote": "gamma"},
        ],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _req(entry))
    assert env["code"] == "ANSWERED"
    assert "alpha" in env["answer"] and "gamma" not in env["answer"]
    assert [c["id"] for c in env["citations"]] == ["c1"]


def test_uncited_sentences_do_not_survive():
    kept = strip_unsupported_assertions("Alpha is here [c1]. Also it is fast.", {"c1"})
    assert kept == "Alpha is here [c1]."


def test_long_line_split_across_chunks_still_cites_the_original_line():
    body = ("Q" * 40000) + " needle\n" + "second\n"
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(body.encode()))
    reply = answer_json(
        "The marker is on line one [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "QQQQQQQQ"}],
    )
    env = Reader(registry, FakeLuna(default_reply=reply)).answer("sess", _req(entry))
    assert env["code"] in ("ANSWERED", "NO_MATCH")
    if env["citations"]:
        assert env["citations"][0]["locator"] == {"kind": "lines", "start": 1, "end": 1}


def test_quote_over_cap_is_rejected_even_when_present_in_the_source():
    registry = SourceRegistry()
    body = b"Z" * 600 + b"\n"
    entry = registry.register("sess", snapshot_bytes(body))
    verifier = CitationVerifier(registry)
    result = verifier.verify(
        "sess",
        {
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "locator": {"kind": "lines", "start": 1, "end": 1},
            "quote": "Z" * (DEFAULT_LIMITS.max_quote_bytes + 1),
        },
    )
    assert not result.verified and result.reason.value == "QUOTE_OVER_CAP"


def test_source_change_between_snapshot_and_citation_is_rejected():
    registry = SourceRegistry()
    first = registry.register("sess", snapshot_bytes(b"alpha\n"))
    second = registry.register("sess", snapshot_bytes(b"changed\n"))
    verifier = CitationVerifier(registry)
    mixed = {
        "source_id": second.source_id,
        "snapshot_id": first.snapshot.snapshot_id,
        "locator": {"kind": "lines", "start": 1, "end": 1},
        "quote": "changed",
    }
    assert not verifier.verify("sess", mixed).verified


def _req(entry, selector=None):
    return {
        "schema_version": "1.0",
        "request_id": "req_c",
        "operation": "read",
        "question": "What does the source say?",
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": selector or {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
