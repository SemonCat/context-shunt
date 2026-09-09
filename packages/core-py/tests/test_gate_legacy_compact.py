"""unit legacy-compaction-fallback: the ported incumbent compaction algorithm.

Pinned against the live incumbent (``oversize-tool-result-compactor`` v0.3.0) read
read-only from the operator's own Hermes 0.21.1 host on 2026-09-09. These tests exercise
the text-shaping port directly (no store, no envelope, no session) - the session-level
wiring that decides *when* this fires is covered by ``test_gate_reader.py``.
"""

from __future__ import annotations

import json

import pytest

from context_shunt.legacy_compact import compact_tool_result

pytestmark = pytest.mark.gate_legacy_compact


def test_pure_function_same_input_same_output():
    text = "line one\nline two\nERROR something failed\n" * 5
    first = compact_tool_result(text)
    second = compact_tool_result(text)
    assert first == second


def test_signal_lines_surface_error_and_5xx():
    lines = [f"row {i}" for i in range(200)]
    lines[50] = "ERROR: connection failed"
    lines[120] = "upstream responded with status: 503"
    lines[150] = "Traceback (most recent call last):"
    text = "\n".join(lines)
    out = compact_tool_result(text)
    assert "High-signal exact lines:" in out
    assert "ERROR: connection failed" in out
    assert "status: 503" in out
    assert "Traceback" in out
    # non-signal middle rows are not all individually enumerated
    assert "row 100" not in out


def test_head_and_tail_sample_present_for_large_input():
    lines = [f"row {i:04d}" for i in range(1, 500)]
    text = "\n".join(lines)
    out = compact_tool_result(text)
    assert "First sample lines:" in out
    assert "Last sample lines:" in out
    assert "row 0001" in out
    assert "row 0499" in out
    # the middle is not reproduced verbatim
    assert "row 0250" not in out


def test_repeated_lines_collapsed_and_counted():
    lines = ["same line"] * 40 + ["unique tail"]
    text = "\n".join(lines)
    out = compact_tool_result(text)
    assert "[repeated" in out or "Repeated exact lines:" in out
    # the raw text is not repeated 40 times in the summary
    assert out.count("same line") < 40


def test_json_object_gets_structure_and_interesting_keys():
    payload = {
        "status": "error",
        "statusCode": 503,
        "query": "sum(rate(errors[5m]))",
        "results": [{"value": i} for i in range(200)],
    }
    out = compact_tool_result(json.dumps(payload))
    assert "JSON structure:" in out
    assert "Query/time/count/status fields:" in out
    assert "$.query" in out
    assert "$.statusCode" in out


def test_json_array_gets_first_and_last_item_snippets():
    payload = [{"id": i, "msg": f"item-{i}"} for i in range(500)]
    out = compact_tool_result(json.dumps(payload))
    assert "First array item" in out
    assert "Last array item" in out


def test_malformed_json_prefix_degrades_to_log_text_without_raising():
    text = '{"broken": [1, 2, ' + ("x" * 5000)
    out = compact_tool_result(text)
    assert isinstance(out, str)
    assert "Line count:" in out


def test_secret_values_redacted():
    text = "Authorization: Bearer " + ("a" * 40) + "\nsk-" + ("b1" * 20) + "\nordinary line"
    out = compact_tool_result(text)
    assert "[redacted secret]" in out
    assert ("a" * 40) not in out
    assert ("b1" * 20) not in out


def test_hard_cap_bounds_output_and_is_labelled():
    text = "\n".join(f"row {i}" for i in range(10_000))
    out = compact_tool_result(text, hard_chars=100)
    assert len(out) <= 100
    assert "truncated" in out


def test_hard_cap_is_a_ceiling_not_a_floor():
    # A summary that already fits comfortably under the cap is not padded to it, and is
    # not (falsely) labelled truncated.
    text = "\n".join(f"row {i}" for i in range(200))
    out = compact_tool_result(text, hard_chars=100_000)
    assert len(out) < 100_000
    assert "truncated" not in out


def test_empty_input_does_not_raise():
    assert compact_tool_result("") == compact_tool_result("")
    out = compact_tool_result("")
    assert isinstance(out, str)


def test_non_ascii_utf8_text_preserved():
    text = "héllo wörld\n" + "日本語のログ行\n" * 3 + "ERROR: échec"
    out = compact_tool_result(text)
    assert "échec" in out or "ERROR" in out
    # must not raise on encode; caller enforces byte caps separately
    out.encode("utf-8")
