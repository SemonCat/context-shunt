"""unit legacy-compaction-fallback: session-level wiring for the ported compaction.

Companion to ``test_gate_legacy_compact.py`` (the pure algorithm) and
``test_automatic_extract.py`` (the narrower, unchanged byte-prefix escape hatch). These
tests cover exactly the new surface: which reader failures newly get a richer deterministic
summary instead of a bare error, which ones deliberately still do not, that the summary is
never mistaken for a model answer or exact bytes, and that a compaction failure degrades to
the original bounded failure rather than ever leaking raw source.

``CITATION_INVALID`` (every citation offered failed mechanical verification) is the
trigger used throughout: it is the one code in
``session._LEGACY_COMPACTION_TRIGGER_CODES`` reachable as a terminal ``status: error``
envelope from a single chunk without also tripping the wholly-unavailable path
``automatic_extract`` already owns. ``INVALID_MODEL_OUTPUT`` is deliberately not in that
trigger set - ``reader.py`` never publishes it as a terminal envelope code, only as a
``coverage.omitted`` reason on an otherwise ``NO_MATCH`` envelope - so there is no envelope
shape for a test to construct there.
"""

from __future__ import annotations

import json

import pytest

from context_shunt.guard import enforce
from context_shunt.session import ShuntSession
from tests.support import FakeLuna, claims_json, make_capability, make_config

pytestmark = pytest.mark.gate_legacy_compact


def setup(tmp_path, provider=None, body=None, **config):
    session = ShuntSession(
        "sess", make_config(tmp_path, **config), make_capability(), provider=provider
    )
    body = body if body is not None else (
        "row 0 ERROR: connection refused\n" + "row %d ordinary line\n" * 300 % tuple(range(1, 301))
    )
    path = tmp_path / "ws" / "source.txt"
    path.write_text(body)
    entry = session.register_path(str(path))
    request = {
        "schema_version": "1.1",
        "request_id": "req_legacy",
        "operation": "read",
        "question": "What happened?",
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 1, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    return session, entry, request, body


def _no_evidence_luna(quote: str = "ERROR: connection refused") -> FakeLuna:
    # A real quote, but the locator names the wrong line: verification fails on the
    # snapshot bytes and nothing survives, which is CITATION_INVALID/NO_VALID_EVIDENCE.
    reply = claims_json(
        [{"text": "Something failed.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 250, "line_end": 250, "quote": quote}],
    )
    return FakeLuna(replies=[reply])


def test_citation_invalid_no_evidence_triggers_legacy_compaction(tmp_path):
    session, entry, request, body = setup(tmp_path, _no_evidence_luna())
    env = session.read(request)
    enforce(env)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["status"] == "partial"
    assert env["result_kind"] == "legacy_compaction"
    assert env["provenance"]["derived"] is False
    assert env["provenance"]["label"] == "legacy_compaction"
    assert env["answer"] == "" and env["citations"] == []
    block = env["legacy_compaction"]
    assert block["original_failure"] == "CITATION_INVALID"
    assert block["source_id"] == entry.source_id
    assert block["snapshot_id"] == entry.snapshot.snapshot_id
    assert "ERROR: connection refused" in block["summary"]
    assert block["summary_bytes"] == len(block["summary"].encode("utf-8"))
    assert block["original_bytes"] == len(body.encode("utf-8"))
    assert env["recovery"]["handles_valid"] is True
    assert "ported from the incumbent" in env["guidance"]
    assert "not model-derived" in env["guidance"]


def test_legacy_compaction_disabled_by_config_keeps_bare_failure(tmp_path):
    session, _, request, _ = setup(
        tmp_path, _no_evidence_luna(), **{"reader": {"legacy_compaction": False}}
    )
    env = session.read(request)
    assert env["code"] == "CITATION_INVALID" and "legacy_compaction" not in env


def test_reader_disabled_config_keeps_bare_failure_and_never_runs_compaction(tmp_path):
    session, _, request, _ = setup(
        tmp_path, _no_evidence_luna(), **{"reader": {"enabled": False}}
    )
    env = session.read(request)
    assert env["code"] == "INVALID_REQUEST" and "legacy_compaction" not in env


def test_compaction_failure_degrades_to_original_bounded_failure_never_raw(tmp_path, monkeypatch):
    session, _, request, body = setup(tmp_path, _no_evidence_luna())

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected failure mid-compaction")

    monkeypatch.setattr("context_shunt.session.compact_tool_result", _boom)
    env = session.read(request)
    enforce(env)
    # Falls back to the reader's own bounded failure envelope, unchanged.
    assert env["code"] == "CITATION_INVALID"
    assert "legacy_compaction" not in env
    assert body not in json.dumps(env)


def test_summary_never_exceeds_the_extraction_byte_cap_even_with_multibyte_text(tmp_path):
    from context_shunt.limits import DEFAULT_LIMITS

    wide_body = "日本語のログ行 ERROR: 失敗しました\n" * 5000
    session, entry, request, _ = setup(
        tmp_path,
        _no_evidence_luna(quote="NONEXISTENT_QUOTE_TEXT_ABC123"),
        body=wide_body,
        **{"reader": {"legacy_compaction_max_chars": 60_000}},
    )
    env = session.read(request)
    enforce(env)
    assert env["code"] == "LEGACY_COMPACTED"
    summary = env["legacy_compaction"]["summary"]
    assert len(summary.encode("utf-8")) <= DEFAULT_LIMITS.max_extraction_bytes
    # No raw crash on decode/encode boundaries; the guard's secret-marker/UTF-8 checks
    # already ran inside `enforce(env)` above.
    summary.encode("utf-8")


def test_legacy_compaction_only_covers_the_first_requested_source(tmp_path):
    session, entry, request, _ = setup(tmp_path, _no_evidence_luna())
    second_path = tmp_path / "ws" / "second.txt"
    second_path.write_text("second source body\n" * 50)
    second_entry = session.register_path(str(second_path))
    request["sources"].append(
        {
            "source_id": second_entry.source_id,
            "snapshot_id": second_entry.snapshot.snapshot_id,
            "selector": {"kind": "all"},
        }
    )
    env = session.read(request)
    enforce(env)
    assert env["code"] == "LEGACY_COMPACTED"
    assert env["legacy_compaction"]["source_id"] == entry.source_id
    assert any(o["reason"] == "UNKNOWN_REMAINDER" for o in env["coverage"]["omitted"])
    assert env["coverage"]["complete"] is False


def test_wholesale_provider_outage_still_prefers_deterministic_extraction(tmp_path):
    """The narrower, already-tested escape hatch keeps precedence when it applies."""
    from context_shunt.provider import TransientProviderError

    failure = TransientProviderError("PRIVATE_BODY")
    session, _, request, _ = setup(tmp_path, FakeLuna(default_reply=failure))
    env = session.read(request)
    assert env["code"] == "EXTRACTED"
    assert env["result_kind"] == "deterministic_extraction"


def test_wholesale_outage_with_automatic_extract_disabled_gets_no_soft_landing(tmp_path):
    """Disabling automatic_extract must not silently reroute through legacy_compaction."""
    from context_shunt.provider import TransientProviderError

    failure = TransientProviderError("PRIVATE_BODY")
    session, _, request, _ = setup(
        tmp_path, FakeLuna(default_reply=failure), **{"reader": {"automatic_extract": False}}
    )
    env = session.read(request)
    assert env["code"] == "MODEL_ERROR" and "legacy_compaction" not in env and "extraction" not in env
