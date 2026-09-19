"""unit reader: authoritative line-number gutters on "lines" excerpts.

The historical failure this replaces: a "lines" chunk's excerpt carried no per-line
numbering, only the chunk's own ``{"start": ..., "end": ...}`` locator in the header. To
answer with a correct ``line_start``/``line_end`` the model had to count physical lines
itself from that header value - and a long, mostly-repetitive chunk with a blank line
right before the fact it needed made that count wrong even when the byte-exact quote it
cited was correct. Verification is intentionally strict about this (the quote is right,
the line is not), so the whole answer was discarded as ``CITATION_INVALID/NO_VALID_EVIDENCE``.

This corpus item is the real one a live trace against ``gpt-5.6-luna`` reproduced:
``evals/luna-corpus.json``'s ``crosschunk_0``. The trace's second chunk (lines 669..1202)
answered ``threshold_0 is 100`` with the exact quote ``" threshold_0 = 100"`` but reported
``line_start=line_end=1201`` - one short of the true 1202, the model having miscounted
across the blank line at 1201. ``test_without_gutters_reproduces_the_live_citation_failure``
reproduces exactly that mistake with a deterministic stub (no paid call), and is the direct
"red" proof: it fails against the pre-fix reader, which passed the chunk's raw text with no
gutters. ``test_with_gutters_the_same_stub_reads_the_line_off_the_gutter`` is "green": the
same stub, given the gutter this task adds, never needs to count at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from context_shunt import reader as reader_module
from context_shunt.binaryguard import TEXT_MEDIA_TYPE
from context_shunt.reader import Reader
from context_shunt.snapshot import snapshot_bytes
from tests.support import FakeLuna, claims_json, make_registry

pytestmark = pytest.mark.gate_reader

REPO = Path(__file__).resolve().parents[3]

_EXCERPT_RE = re.compile(r"<<<BEGIN EXCERPT\n(.*)\nEND EXCERPT>>>", re.S)
_LOCATOR_RE = re.compile(r"SOURCE EXCERPT \(locator (\{.*?\})\):")
_GUTTER_RE = re.compile(r"^(\d+): (.*)$")


def _crosschunk_item() -> dict:
    corpus = json.loads((REPO / "evals" / "luna-corpus.json").read_text())
    (item,) = [i for i in corpus["items"] if i["id"] == "crosschunk_0"]
    return item


def _request(entry, question: str) -> dict:
    return {
        "schema_version": "1.0",
        "request_id": "req_gutter",
        "operation": "read",
        "question": question,
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }


def _line_aware_stub(quote: str):
    """A model stand-in that answers correctly off a gutter, and reproduces the live
    trace's exact off-by-one mistake when no gutter is there to read.

    This is not a hand-picked pass/fail switch: it is the one behavioural difference the
    fix is supposed to make. Given a gutter, it reads the number already written next to
    the matching line. Given none, it falls back to counting lines itself from the
    chunk's own ``start`` - the only information the pre-fix excerpt carried - which is
    exactly the arithmetic the live trace's model got wrong by one on this fixture.
    """

    def reply(user: str) -> str:
        match = _EXCERPT_RE.search(user)
        assert match, "reader must send a BEGIN/END excerpt"
        excerpt = match.group(1)
        if quote not in excerpt:
            return claims_json([], [])
        locator = json.loads(_LOCATOR_RE.search(user).group(1))
        start = int(locator["start"])
        lines = excerpt.split("\n")
        found = next(i for i, line in enumerate(lines) if quote in line)
        gutter = _GUTTER_RE.match(lines[found])
        if gutter is not None:
            line_no = int(gutter.group(1))
        else:
            # Reproduces the live trace: undercounts by one across the blank line that
            # sits just before the final fact in this fixture.
            line_no = start + found - 1
        citation = {"id": "c1", "line_start": line_no, "line_end": line_no, "quote": quote}
        claim = {"text": "threshold_0 is 100.", "citation_ids": ["c1"]}
        return claims_json([claim], [citation])

    return reply


def test_with_gutters_the_same_stub_reads_the_line_off_the_gutter(tmp_path):
    item = _crosschunk_item()
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register(
        "sess", snapshot_bytes(item["content"].encode(), media_type_hint=TEXT_MEDIA_TYPE)
    )
    luna = FakeLuna(default_reply=_line_aware_stub(item["expected_quote"]))
    env = Reader(registry, luna).answer("sess", _request(entry, item["question"])).envelope

    assert env["status"] == "ok" and env["code"] == "ANSWERED"
    assert "100" in env["answer"]
    (citation,) = [c for c in env["citations"] if c["quote"] == item["expected_quote"]]
    assert citation["verified"] is True
    assert citation["locator"]["start"] == item["expected_locator"]["start"] == 1202
    assert citation["locator"]["end"] == item["expected_locator"]["end"] == 1202


def test_without_gutters_reproduces_the_live_citation_failure(tmp_path, monkeypatch):
    """The direct "red" proof: reverting to the pre-fix excerpt (raw chunk text, no
    gutters) makes the exact live-trace mistake reappear and the answer is discarded."""
    monkeypatch.setattr(reader_module, "render_excerpt", lambda chunk: chunk.text)

    item = _crosschunk_item()
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register(
        "sess", snapshot_bytes(item["content"].encode(), media_type_hint=TEXT_MEDIA_TYPE)
    )
    luna = FakeLuna(default_reply=_line_aware_stub(item["expected_quote"]))
    env = Reader(registry, luna).answer("sess", _request(entry, item["question"])).envelope

    assert env["status"] == "error"
    assert env["code"] == "CITATION_INVALID"
    assert env["failure_detail"] == "NO_VALID_EVIDENCE"
    assert env.get("answer", "") == ""
