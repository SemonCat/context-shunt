"""unit citations: verification is mechanical, and only the verifier writes ``verified``."""

from __future__ import annotations

import json

import pytest

from context_shunt.binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from context_shunt.citations import (
    CitationVerifier,
    normalize_claims,
    render_claims,
    strip_unsupported_assertions,
    unpublished_marker_ids,
)
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.reader import Reader
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes
from context_shunt.store import SnapshotStore
from tests.support import FakeLuna, answer_json, make_identity, make_registry

pytestmark = pytest.mark.gate_citations


def _registry_with(cases, tmp_path):
    registry = make_registry(tmp_path, session_id="sess_a")
    entries = {}
    for name, spec in cases["sources"].items():
        media = JSON_MEDIA_TYPE if spec["media_type"] == "application/json" else TEXT_MEDIA_TYPE
        entries[name] = registry.register(
            "sess_a", snapshot_bytes(spec["content"].encode(), media_type_hint=media)
        )
    return registry, entries


def test_every_conformance_case(citation_cases, tmp_path):
    registry, entries = _registry_with(citation_cases, tmp_path)
    verifier = CitationVerifier(registry)
    failures = []
    for case in citation_cases["cases"]:
        entry = entries[case["source"]]
        session = "sess_b" if case.get("foreign_session") else "sess_a"
        if case.get("expired"):
            clock = {"now": 1_700_000_000_000}
            store = SnapshotStore(
                tmp_path / f"expiring-{case['id']}",
                wall_clock_ms=lambda state=clock: state["now"],
            )
            identity = make_identity("sess_a")
            expiring = SourceRegistry(store, identity)
            handle = expiring.register("sess_a", entry.snapshot)
            clock["now"] += (DEFAULT_LIMITS.store_handle_ttl_seconds + 1) * 1000
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


def test_model_claiming_verified_does_not_make_it_verified(tmp_path):
    registry = make_registry(tmp_path, session_id="sess")
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
    env = Reader(registry, luna).answer("sess", _req(entry)).envelope
    assert env["code"] == "CITATION_INVALID"
    assert env["citations"] == []
    assert env["answer"] == ""


def test_assertions_without_valid_evidence_are_removed_but_valid_ones_survive(tmp_path):
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"alpha\nbeta\n"))
    reply = answer_json(
        "The first line is alpha [c1]. The third line is gamma [c2].",
        [
            {"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha"},
            {"id": "c2", "line_start": 3, "line_end": 3, "quote": "gamma"},
        ],
    )
    env = Reader(registry, FakeLuna(replies=[reply])).answer("sess", _req(entry)).envelope
    assert env["code"] == "ANSWERED"
    assert "alpha" in env["answer"] and "gamma" not in env["answer"]
    assert [c["id"] for c in env["citations"]] == ["c1"]


def test_uncited_sentences_do_not_survive():
    kept = strip_unsupported_assertions("Alpha is here [c1]. Also it is fast.", {"c1"})
    assert kept == "Alpha is here [c1]."


def test_long_line_split_across_chunks_still_cites_the_original_line(tmp_path):
    body = ("Q" * 40000) + " needle\n" + "second\n"
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(body.encode()))
    reply = answer_json(
        "The marker is on line one [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "QQQQQQQQ"}],
    )
    env = Reader(registry, FakeLuna(default_reply=reply)).answer("sess", _req(entry)).envelope
    assert env["code"] in ("ANSWERED", "NO_MATCH")
    if env["citations"]:
        assert env["citations"][0]["locator"] == {"kind": "lines", "start": 1, "end": 1}


def test_quote_over_cap_is_rejected_even_when_present_in_the_source(tmp_path):
    registry = make_registry(tmp_path, session_id="sess")
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


def test_source_change_between_snapshot_and_citation_is_rejected(tmp_path):
    registry = make_registry(tmp_path, session_id="sess")
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


def test_every_claims_conformance_case(claims_cases):
    """Both cores must agree on every case here - see contracts/v1/conformance/claims-cases.json.

    This is the structural half of the fix for the historical marker-omission class: a
    claim survives only when its citation_ids are well-formed, unique, and every one of
    them names an id the same response actually declared. Rendering then places every
    marker mechanically, so the model can never again produce a citation the reader cannot
    show.
    """
    failures = []
    for case in claims_cases["cases"]:
        valid_ids = set(case["citations_seen"])
        survivors = normalize_claims(case["claims"], valid_ids)
        rendered = render_claims(survivors)
        want = (case["expect"]["surviving_claims"], case["expect"]["rendered"])
        got = (survivors, rendered)
        if want != got:
            failures.append((case["id"], want, got))
    assert not failures, failures


def test_marker_syntax_in_claim_text_is_refused_not_escaped(claims_cases):
    """A claim that writes its own marker is dropped, whatever the marker names.

    The published ``answer`` is rendered from ``text`` verbatim, so a model-authored
    ``[c999]`` used to reach the envelope as though the program had placed it - naming a
    citation that was never published, inside an envelope still reporting
    ``citations_mechanically_verified: true``. Escaping would keep model bytes in a field
    whose whole meaning is that the program wrote them, so the claim goes instead.
    """
    forged = normalize_claims(
        [{"text": "Retries stop after three attempts [c999].", "citation_ids": ["c1"]}], {"c1"}
    )
    assert forged == []
    # The id being real changes nothing.
    assert normalize_claims([{"text": "Three [c1].", "citation_ids": ["c1"]}], {"c1"}) == []
    # Brackets that are not marker syntax are ordinary prose.
    kept = normalize_claims(
        [{"text": "Read from config[cache] on startup.", "citation_ids": ["c1"]}], {"c1"}
    )
    assert [c["text"] for c in kept] == ["Read from config[cache] on startup."]
    # And the shared corpus says the same, so both cores are held to it.
    ids = {c["id"] for c in claims_cases["cases"]}
    assert "marker_in_claim_text_drops_the_claim" in ids
    assert "bracketed_text_that_is_not_marker_syntax_survives" in ids


def test_every_marker_conformance_case(claims_cases):
    """The publication invariant, case for case - see claims-cases.json ``marker_cases``.

    Every ``[cN]`` in a published answer must name a citation the same envelope publishes.
    ``normalize_claims`` closes the forged-marker route in; this is the check on the way
    out, so no later path can reintroduce one.
    """
    failures = []
    for case in claims_cases["marker_cases"]["cases"]:
        got = unpublished_marker_ids(case["text"], set(case["published"]))
        if got != case["expect"]["unpublished"]:
            failures.append((case["id"], case["expect"]["unpublished"], got))
    assert not failures, failures
    assert len(claims_cases["marker_cases"]["cases"]) >= 6


def test_claims_conformance_corpus_covers_the_fail_closed_reasons(claims_cases):
    ids = {c["id"] for c in claims_cases["cases"]}
    assert len(claims_cases["cases"]) >= 15
    assert {
        "unknown_citation_id_drops_the_claim",
        "duplicate_citation_id_within_a_claim_drops_it",
        "empty_citation_ids_drops_the_claim",
        "multi_citation_claim",
        "multi_claim_answer",
        "one_bad_claim_does_not_sink_a_good_one",
    } <= ids


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
