"""unit inspect: deterministic extraction, cursors, and the limits that make it safe.

The escape hatch that returns real source bytes is the one most worth attacking, so this
gate covers: zero model calls, the exact per-result cap, cursor authenticity, the scan
budget, and the cumulative disclosure ceiling that stops paging from reassembling a whole
payload in the main context.
"""

from __future__ import annotations

import json

import pytest

from context_shunt.errors import ShuntError
from context_shunt.inspect import Inspector, decode_cursor, encode_cursor
from context_shunt.limits import DEFAULT_LIMITS, EMITTED_SCHEMA_VERSION
from context_shunt.provider import UnavailableProvider
from context_shunt.session import ShuntSession
from context_shunt.textindex import LineIndex
from tests.support import FakeLuna, make_capability, make_config

pytestmark = pytest.mark.gate_inspect

L = DEFAULT_LIMITS
CANARY_HEAD = "CANARY-HEAD-1a2b3c"
CANARY_MID = "CANARY-MID-4d5e6f"
CANARY_TAIL = "CANARY-TAIL-7a8b9c"


def _body(lines: int = 600) -> str:
    rows = [f"row {i:04d} value-{i}" for i in range(1, lines + 1)]
    rows[0] = f"{rows[0]} {CANARY_HEAD}"
    rows[lines // 2] = f"{rows[lines // 2]} {CANARY_MID}"
    rows[-1] = f"{rows[-1]} {CANARY_TAIL}"
    return "\n".join(rows) + "\n"


def _session(tmp_path, provider=None, **config_overrides):
    """A session whose provider fails on every call, so a model call would be visible."""
    config = make_config(tmp_path, **config_overrides)
    return ShuntSession(
        "sess",
        config,
        make_capability(),
        provider=provider or UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    )


def _captured(tmp_path, session, body: str | None = None):
    path = tmp_path / "ws" / "big.txt"
    path.write_text(body if body is not None else _body())
    return session.register_path(str(path))


def _request(entry, selector, **kw):
    request = {
        "schema_version": EMITTED_SCHEMA_VERSION,
        "request_id": "req_i1",
        "operation": "inspect",
        "source_id": entry.source_id,
        "snapshot_id": entry.snapshot.snapshot_id,
        "selector": selector,
        "budgets": {
            "max_result_bytes": kw.pop("max_result_bytes", L.inspect_max_result_bytes),
            "max_scan_lines": kw.pop("max_scan_lines", L.inspect_max_scan_lines),
        },
    }
    request.update(kw)
    return request


# -- deterministic, and labelled as such ------------------------------------


def test_extraction_is_exact_and_labelled_not_derived(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session)
    env = session.inspect(_request(entry, {"kind": "lines", "start": 1, "end": 3}))
    assert env["status"] == "ok" and env["code"] == "EXTRACTED"
    assert env["result_kind"] == "deterministic_extraction"
    assert env["provenance"]["derived"] is False
    assert env["provenance"]["attribution_status"] == "not_applicable"
    assert env["extraction"]["deterministic"] is True
    # The bytes are a literal substring of the snapshot, not a summary of it.
    text = env["extraction"]["segments"][0]["text"]
    assert text in entry.snapshot.data.decode("utf-8")
    assert CANARY_HEAD in text
    assert env["answer"] == "" and env["citations"] == []


def test_inspect_makes_zero_model_calls(tmp_path):
    """The provider is a fake that fails loudly; inspect still succeeds."""
    luna = FakeLuna()
    session = _session(tmp_path, provider=luna)
    entry = _captured(tmp_path, session)
    for selector in (
        {"kind": "lines", "start": 5, "end": 9},
        {"kind": "bytes", "start": 0, "end": 64},
        {"kind": "search", "needle": "value-42", "max_matches": 3},
    ):
        env = session.inspect(_request(entry, selector))
        assert env["code"] == "EXTRACTED"
    assert luna.call_count == 0


def test_a_snapshot_mismatch_is_refused_rather_than_answered_from_a_newer_snapshot(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session)
    request = _request(entry, {"kind": "lines", "start": 1, "end": 2})
    request["snapshot_id"] = "sha256:" + "0" * 64
    env = session.inspect(request)
    assert env["code"] == "SOURCE_CHANGED"
    assert CANARY_HEAD not in json.dumps(env)


def test_a_foreign_or_expired_handle_yields_nothing(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session)
    request = _request(entry, {"kind": "lines", "start": 1, "end": 2})
    request["source_id"] = "src_" + "0" * 16
    env = session.inspect(request)
    assert env["code"] == "SOURCE_EXPIRED"
    assert CANARY_HEAD not in json.dumps(env)


# -- caps -------------------------------------------------------------------


def test_a_page_never_exceeds_the_sixteen_kib_result_cap(tmp_path):
    session = _session(tmp_path)
    # 128-byte lines so a page boundary can land exactly on the cap.
    body = "".join(("x" * 127 + "\n") for _ in range(400))
    entry = _captured(tmp_path, session, body)
    env = session.inspect(
        _request(entry, {"kind": "lines", "start": 1, "end": 400}, max_scan_lines=20000)
    )
    assert env["extraction"]["result_bytes"] <= 16384
    assert env["extraction"]["complete"] is False


def test_a_byte_page_hits_the_cap_exactly(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session, "q" * 40000)
    env = session.inspect(
        _request(entry, {"kind": "bytes", "start": 0, "end": 40000}, max_scan_lines=1)
    )
    extraction = env["extraction"]
    assert extraction["result_bytes"] == 16384
    assert len(extraction["segments"][0]["text"].encode("utf-8")) == 16384
    assert extraction["complete"] is False


def test_a_requested_budget_above_the_cap_is_clamped_not_honoured(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session, "q" * 40000)
    request = _request(entry, {"kind": "bytes", "start": 0, "end": 40000})
    request["budgets"]["max_result_bytes"] = 16384
    env = session.inspect(request)
    assert env["extraction"]["result_bytes"] <= L.inspect_max_result_bytes


def test_the_scan_budget_stops_a_fruitless_search(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session)
    env = session.inspect(
        _request(
            entry,
            {"kind": "search", "needle": "no-such-token", "max_matches": 50},
            max_scan_lines=25,
        )
    )
    extraction = env["extraction"]
    assert extraction["lines_scanned"] == 25
    assert extraction["scan_budget_exhausted"] is True
    assert extraction["matches_found"] == 0
    assert env["status"] == "partial"
    assert any(
        omission["reason"] == "SCAN_BUDGET_EXHAUSTED" for omission in env["coverage"]["omitted"]
    )


# -- pagination and cursors -------------------------------------------------


def test_paging_walks_the_range_in_order_without_gaps_or_repeats(tmp_path):
    session = _session(tmp_path)
    body = _body(300)
    entry = _captured(tmp_path, session, body)
    seen: list[str] = []
    request = _request(entry, {"kind": "lines", "start": 1, "end": 300}, max_result_bytes=400)
    for _ in range(50):
        env = session.inspect(dict(request))
        extraction = env["extraction"]
        if extraction["segments"]:
            seen.append(extraction["segments"][0]["text"])
        if extraction["complete"] or not extraction["next_cursor"]:
            break
        request["cursor"] = extraction["next_cursor"]
    joined = "\n".join(seen) + "\n"
    assert joined == body


def test_a_tampered_cursor_is_refused_before_any_scan(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session)
    first = session.inspect(
        _request(entry, {"kind": "lines", "start": 1, "end": 300}, max_result_bytes=200)
    )
    cursor = first["extraction"]["next_cursor"]
    assert cursor

    flipped = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    env = session.inspect(
        _request(entry, {"kind": "lines", "start": 1, "end": 300}, cursor=flipped)
    )
    assert env["code"] == "INVALID_REQUEST"


def test_a_cursor_cannot_be_moved_to_another_selector_or_snapshot(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session)
    first = session.inspect(
        _request(entry, {"kind": "lines", "start": 1, "end": 300}, max_result_bytes=200)
    )
    cursor = first["extraction"]["next_cursor"]
    # Same handle, same snapshot, different selector: the binding no longer matches.
    env = session.inspect(_request(entry, {"kind": "lines", "start": 1, "end": 50}, cursor=cursor))
    assert env["code"] == "INVALID_REQUEST"


def test_a_cursor_from_another_store_does_not_authenticate(tmp_path):
    """The MAC key lives in the store's own metadata, so a cursor is not portable."""
    session_a = _session(tmp_path / "a")
    session_b = _session(tmp_path / "b")
    entry_a = _captured(tmp_path / "a", session_a)
    selector = {"kind": "lines", "start": 1, "end": 300}
    forged = encode_cursor(
        session_b.store.cursor_key(),
        entry_a.source_id,
        entry_a.snapshot.snapshot_id,
        selector,
        {"line": 200},
    )
    env = session_a.inspect(_request(entry_a, selector, cursor=forged))
    assert env["code"] == "INVALID_REQUEST"


def test_cursor_state_is_opaque_and_carries_no_content():
    key = b"k" * 32
    selector = {"kind": "lines", "start": 1, "end": 10}
    token = encode_cursor(key, "src_abcd1234", "sha256:" + "1" * 64, selector, {"line": 5})
    assert token.startswith("csr_")
    assert decode_cursor(key, token, "src_abcd1234", "sha256:" + "1" * 64, selector) == {"line": 5}
    with pytest.raises(ShuntError):
        decode_cursor(key, token, "src_other1234", "sha256:" + "1" * 64, selector)


# -- the cumulative ceiling -------------------------------------------------


def test_repeated_pages_cannot_refill_the_main_context(tmp_path):
    """The whole point: a per-result cap alone would just be defeated by paging."""
    session = _session(tmp_path, limits={"disclosure_max_per_source_bytes": 4096})
    entry = _captured(tmp_path, session)
    disclosed = 0
    exhausted = False
    request = _request(entry, {"kind": "lines", "start": 1, "end": 600}, max_result_bytes=1024)
    for _ in range(50):
        env = session.inspect(dict(request))
        extraction = env["extraction"]
        disclosed += extraction["result_bytes"]
        if env["code"] == "DISCLOSURE_EXHAUSTED":
            exhausted = True
            assert extraction["result_bytes"] == 0
            assert extraction["disclosure_limit_reached"] is True
            assert env["recovery"]["handles_valid"] is True
            break
        if extraction["next_cursor"]:
            request["cursor"] = extraction["next_cursor"]
        else:
            break
    assert exhausted, "paging was never stopped by the disclosure ceiling"
    assert disclosed <= 4096
    # Well short of the whole file: the payload cannot be reassembled this way.
    assert disclosed < entry.snapshot.bytes_len


def test_a_page_that_discloses_nothing_records_nothing(tmp_path):
    """Otherwise a caller paging a fruitless search grows an uncapped table for free."""
    import sqlite3

    session = _session(tmp_path)
    entry = _captured(tmp_path, session)
    for _ in range(30):
        env = session.inspect(
            _request(
                entry,
                {"kind": "search", "needle": "no-such-token", "max_matches": 5},
                max_scan_lines=1,
            )
        )
        assert env["extraction"]["result_bytes"] == 0
    connection = sqlite3.connect(session.store.root / "store.sqlite3")
    rows, total = next(
        iter(connection.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM disclosure_events"))
    )
    connection.close()
    assert (rows, total) == (0, 0)


def test_the_ceiling_is_reported_on_every_page(tmp_path):
    session = _session(tmp_path, limits={"disclosure_max_per_source_bytes": 8192})
    entry = _captured(tmp_path, session)
    env = session.inspect(
        _request(entry, {"kind": "lines", "start": 1, "end": 20}, max_result_bytes=512)
    )
    extraction = env["extraction"]
    assert extraction["disclosed_bytes_source"] == extraction["result_bytes"]
    assert extraction["disclosed_bytes_session"] == extraction["result_bytes"]
    assert extraction["disclosure_limit_reached"] is False


# -- extractor unit behaviour ----------------------------------------------


def test_a_byte_range_never_splits_a_character(tmp_path):
    data = ("héllo wörld ✓ " * 200).encode("utf-8")
    inspector = Inspector()
    offset = 0
    rebuilt = b""
    for _ in range(4000):
        result = inspector.extract(
            data,
            LineIndex(data),
            {"kind": "bytes", "start": 0, "end": len(data)},
            max_result_bytes=7,
            max_scan_lines=1,
            state={"offset": offset},
        )
        if not result.segments:
            break
        segment = result.segments[0]
        rebuilt += (
            segment["text"].encode("utf-8")
            if isinstance(segment, dict)
            else segment.text.encode("utf-8")
        )
        if result.next_cursor_state is None:
            break
        offset = result.next_cursor_state["offset"]
    assert rebuilt == data


def test_search_accepts_only_a_literal_needle(tmp_path):
    """A regex-looking needle is matched literally, so no pattern can be made to backtrack."""
    data = b"plain (a+)+b line\nliteral (a+)+b here\n"
    inspector = Inspector()
    result = inspector.extract(
        data,
        LineIndex(data),
        {"kind": "search", "needle": "(a+)+b", "max_matches": 5},
        max_result_bytes=4096,
        max_scan_lines=100,
    )
    assert result.matches_found == 2


def test_a_range_past_the_end_of_the_snapshot_is_an_empty_exact_answer(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session, "one\ntwo\n")
    env = session.inspect(_request(entry, {"kind": "lines", "start": 500, "end": 600}))
    assert env["code"] == "EXTRACTED"
    assert env["extraction"]["segments"] == []
    assert env["extraction"]["result_bytes"] == 0
