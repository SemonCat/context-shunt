"""`align: char` byte selectors: safe paging over multibyte text without weakening strict.

Synthetic multilingual fixtures only. Every case proves one of: no character lost, none
duplicated, no byte disclosed outside the effective range, no page over its cap, every
disclosed byte charged, and the strict default unchanged.
"""

from __future__ import annotations

import pytest

from context_shunt.errors import ShuntError
from context_shunt.inspect import Inspector, byte_window
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.schema import validate_request
from context_shunt.textindex import LineIndex
from tests.test_gate_inspect import _captured, _request, _session

pytestmark = pytest.mark.gate_inspect
L = DEFAULT_LIMITS

# One-, two-, three- and four-byte characters, combining marks and a ZWJ sequence.
MIXED = "aé日🎯́ß中文🇹🇼👩‍💻Ωz\n" * 3


def _starts(raw: bytes) -> list[int]:
    return [i for i in range(len(raw) + 1) if i == len(raw) or raw[i] & 0xC0 != 0x80]


def _floor(raw: bytes, offset: int) -> int:
    offset = min(offset, len(raw))
    return max(s for s in _starts(raw) if s <= offset)


def _page_all(inspector, raw, selector, budget):
    """Follow cursors to completion; return (bytes, pages)."""
    index = LineIndex(raw)
    state: dict = {}
    collected = bytearray()
    pages = []
    for _ in range(len(raw) + 2):
        page = inspector.extract(
            raw,
            index,
            selector,
            max_result_bytes=budget,
            max_scan_lines=1,
            max_wire_bytes=L.max_extended_envelope_bytes,
            state=state,
        )
        pages.append(page)
        for segment in page.segments:
            assert segment.text.encode() == raw[segment.start : segment.end]
            collected.extend(raw[segment.start : segment.end])
        assert page.result_bytes <= budget
        if page.complete or page.stalled:
            return bytes(collected), pages
        state = page.next_cursor_state
    pytest.fail("pagination did not finish")


def test_char_align_floors_both_offsets_and_reports_the_effective_range():
    raw = MIXED.encode()
    for start in range(len(raw) + 1):
        for end in range(start, len(raw) + 1):
            selector = {"kind": "bytes", "start": start, "end": end, "align": "char"}
            lo, hi, page_start = byte_window(raw, selector, {})
            assert (lo, hi, page_start) == (_floor(raw, start), _floor(raw, end), lo)
            # Never more than one character's worth of widening before `start`.
            assert start - lo <= 3
            assert hi <= end


def test_char_align_contiguous_ranges_tile_without_loss_or_duplication():
    raw = MIXED.encode()
    inspector = Inspector()
    for width in (1, 2, 3, 5, 7):
        for budget in (4, 16):
            collected = bytearray()
            for start in range(0, len(raw), width):
                selector = {
                    "kind": "bytes",
                    "start": start,
                    "end": start + width,
                    "align": "char",
                }
                got, pages = _page_all(inspector, raw, selector, budget)
                assert all(not p.stalled for p in pages)
                lo, hi = pages[0].byte_range
                assert got == raw[lo:hi]
                for page in pages:
                    for segment in page.segments:
                        assert lo <= segment.start < segment.end <= hi
                collected.extend(got)
            assert bytes(collected) == raw, (width, budget)


def test_strict_default_is_unchanged_and_never_aligns():
    raw = "é".encode()
    inspector = Inspector()
    for selector in (
        {"kind": "bytes", "start": 1, "end": 2},
        {"kind": "bytes", "start": 1, "end": 2, "align": "strict"},
    ):
        with pytest.raises(ShuntError) as caught:
            inspector.extract(
                raw,
                LineIndex(raw),
                selector,
                max_result_bytes=16,
                max_scan_lines=1,
                max_wire_bytes=L.max_extended_envelope_bytes,
            )
        assert caught.value.detail == "UTF8_RANGE_BOUNDARY"
    page = inspector.extract(
        raw,
        LineIndex(raw),
        {"kind": "bytes", "start": 0, "end": 2},
        max_result_bytes=16,
        max_scan_lines=1,
        max_wire_bytes=L.max_extended_envelope_bytes,
    )
    assert page.byte_range is None


def test_a_range_inside_one_character_is_an_honest_empty_page():
    raw = "🎯".encode()
    page = Inspector().extract(
        raw,
        LineIndex(raw),
        {"kind": "bytes", "start": 1, "end": 3, "align": "char"},
        max_result_bytes=16,
        max_scan_lines=1,
        max_wire_bytes=L.max_extended_envelope_bytes,
    )
    assert page.segments == [] and page.complete and page.result_bytes == 0
    assert page.byte_range == (0, 0)


def test_session_char_align_charges_exactly_what_it_discloses(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session, MIXED)
    raw = MIXED.encode()
    request = _request(
        entry, {"kind": "bytes", "start": 2, "end": 9, "align": "char"}, max_result_bytes=4
    )
    collected = bytearray()
    for _ in range(16):
        env = session.inspect(dict(request))
        assert env["code"] == "EXTRACTED", env.get("failure_detail")
        page = env["extraction"]
        assert page["byte_range"] == {"start": _floor(raw, 2), "end": _floor(raw, 9)}
        assert page["result_bytes"] <= 4
        for segment in page["segments"]:
            collected.extend(segment["text"].encode())
        if page["complete"]:
            break
        request["cursor"] = page["next_cursor"]
    else:
        pytest.fail("pagination did not finish")
    assert bytes(collected) == raw[_floor(raw, 2) : _floor(raw, 9)]
    assert page["disclosed_bytes_source"] == len(collected)
    assert session.store.disclosure_allowance(
        session.identity, entry.source_id
    ).per_source_remaining == L.disclosure_max_per_source_bytes - len(collected)


def test_session_strict_refusal_now_names_the_char_alignment_remedy(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session, MIXED)
    env = session.inspect(_request(entry, {"kind": "bytes", "start": 2, "end": 9}))
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "UTF8_RANGE_BOUNDARY"
    assert '"align": "char"' in env["guidance"]
    assert "extraction" not in env
    assert (
        session.store.disclosure_allowance(session.identity, entry.source_id).per_source_remaining
        == L.disclosure_max_per_source_bytes
    )


def test_a_cursor_cannot_cross_between_strict_and_char_selectors(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session, MIXED)
    aligned = {"kind": "bytes", "start": 0, "end": 30, "align": "char"}
    first = session.inspect(_request(entry, aligned, max_result_bytes=4))
    cursor = first["extraction"]["next_cursor"]
    assert cursor
    strict = {"kind": "bytes", "start": 0, "end": 30}
    crossed = session.inspect(_request(entry, strict, max_result_bytes=4, cursor=cursor))
    assert crossed["code"] == "INVALID_REQUEST"
    assert crossed["failure_detail"] == "BAD_CURSOR"


def test_a_cursor_is_isolated_to_its_source_and_generation(tmp_path):
    session = _session(tmp_path)
    entry = _captured(tmp_path, session, MIXED)
    selector = {"kind": "bytes", "start": 0, "end": 30, "align": "char"}
    cursor = session.inspect(_request(entry, selector, max_result_bytes=4))["extraction"][
        "next_cursor"
    ]
    other = tmp_path / "ws" / "other.txt"
    other.write_text(MIXED)
    second = session.register_path(str(other))
    foreign = session.inspect(_request(second, selector, max_result_bytes=4, cursor=cursor))
    assert foreign["code"] == "INVALID_REQUEST"
    fresh = session.reset(2)
    expired = fresh.inspect(_request(entry, selector, max_result_bytes=4, cursor=cursor))
    assert expired["code"] in ("SOURCE_EXPIRED", "INVALID_REQUEST")
    assert "extraction" not in expired


def test_char_align_cannot_exceed_the_cumulative_disclosure_ceiling(tmp_path):
    session = _session(tmp_path, limits={"disclosure_max_per_source_bytes": 12})
    entry = _captured(tmp_path, session, MIXED)
    request = _request(
        entry, {"kind": "bytes", "start": 1, "end": 200, "align": "char"}, max_result_bytes=8
    )
    collected = bytearray()
    codes = []
    for _ in range(16):
        env = session.inspect(dict(request))
        codes.append(env["code"])
        if env["code"] != "EXTRACTED":
            break
        for segment in env["extraction"]["segments"]:
            collected.extend(segment["text"].encode())
        if env["extraction"]["complete"] or not env["extraction"]["next_cursor"]:
            break
        request["cursor"] = env["extraction"]["next_cursor"]
    raw = MIXED.encode()
    assert 0 < len(collected) <= 12
    assert bytes(collected) == raw[1 : 1 + len(collected)]
    assert codes[-1] != "EXTRACTED" or not env["extraction"]["complete"]
    assert session.store.disclosure_allowance(
        session.identity, entry.source_id
    ).per_source_remaining == 12 - len(collected)


def test_align_requires_schema_1_3():
    request = {
        "schema_version": "1.2",
        "request_id": "req_a",
        "operation": "inspect",
        "source_id": "src_" + "a" * 24,
        "snapshot_id": "sha256:" + "0" * 64,
        "selector": {"kind": "bytes", "start": 0, "end": 1, "align": "char"},
        "budgets": {"max_result_bytes": 16, "max_scan_lines": 1},
    }
    inspect_only = frozenset({"inspect"})
    with pytest.raises(ShuntError) as caught:
        validate_request(request, operations=inspect_only)
    assert caught.value.detail == "SCHEMA_VIOLATION"
    request["schema_version"] = "1.3"
    assert validate_request(request, operations=inspect_only) is request
    for align in ("strict", "char"):
        selector = {**request["selector"], "align": align}
        validate_request({**request, "selector": selector}, operations=inspect_only)
    with pytest.raises(ShuntError):
        validate_request(
            {**request, "selector": {**request["selector"], "align": "loose"}},
            operations=inspect_only,
        )
