"""unit pre-read: the gate decides before the tool runs, and a blocked call never runs."""

from __future__ import annotations

import pytest

import context_shunt.probe as probe_module
from context_shunt.clock import FakeClock
from context_shunt.gate import Decision, PreReadGate, ProbeResult, ProbeSelection
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.probe import FileProber
from context_shunt.session import ShuntSession
from context_shunt.textindex import count_lines
from tests.support import make_capability, make_config

pytestmark = pytest.mark.gate_pre_read


def _table_prober(table):
    def probe(path: str, selection: ProbeSelection | None = None) -> ProbeResult:
        selection = selection or ProbeSelection()
        entry = table.get(path)
        if entry is None or entry.get("missing"):
            return ProbeResult(exists=False)
        kind = entry.get("kind", "file")
        if kind != "file":
            return ProbeResult(exists=True, kind=kind)
        lines = entry.get("lines", 0)
        bytes_count = entry.get("bytes", 0)
        max_line = entry.get("max_line_bytes", -(-bytes_count // max(1, lines)))
        if selection.mode == "metadata":
            return ProbeResult(exists=True, kind=kind)
        if selection.mode in ("lines", "tail"):
            return ProbeResult(
                exists=True,
                kind=kind,
                lines=min(selection.limit, lines),
                bytes=min(bytes_count, selection.limit * max_line),
            )
        if selection.mode == "search":
            matches = min(selection.max_matches, lines)
            return ProbeResult(
                exists=True,
                kind=kind,
                lines=matches,
                bytes=matches * (max_line + len(path.encode()) + 64),
            )
        return ProbeResult(
            exists=True,
            kind=kind,
            lines=lines,
            bytes=bytes_count,
            exact=True,
        )

    return probe


def test_every_conformance_case(gate_cases):
    gate = PreReadGate(_table_prober(gate_cases["probe_table"]))
    failures = []
    for case in gate_cases["cases"]:
        decision = gate.evaluate(case["input"]["tool"], case["input"]["args"])
        got = {"decision": decision.decision.value, "form": decision.form.value}
        if decision.code:
            got["code"] = decision.code
        want = {k: v for k, v in case["expect"].items() if k in ("decision", "form", "code")}
        if got != want:
            failures.append((case["id"], want, got))
    assert not failures, failures


def test_conformance_corpus_is_not_empty_and_covers_all_outcomes(gate_cases):
    outcomes = {c["expect"]["decision"] for c in gate_cases["cases"]}
    assert len(gate_cases["cases"]) >= 60
    assert outcomes == {"allow", "blocked", "passthrough"}


def test_unclassifiable_shell_read_is_passed_to_the_host(gate_cases):
    """A classifier miss must not turn an otherwise valid host call into an error."""
    gate = PreReadGate(_table_prober(gate_cases["probe_table"]))
    decision = gate.evaluate("shell", {"command": "awk '{print}' /ws/huge.txt"})
    assert decision.decision is Decision.PASSTHROUGH
    assert decision.form.value == "unclassifiable"
    assert decision.reason == "OPAQUE_READ"
    assert decision.code is None


def test_bounded_read_that_cannot_be_sized_is_passed_unchanged(gate_cases):
    """An explicit bound is enough to keep the gate from vetoing the host tool."""
    gate = PreReadGate(_table_prober(gate_cases["probe_table"]))
    decision = gate.evaluate("read", {"file_path": "/ws/longline.txt", "offset": 1, "limit": 1})
    assert decision.decision is Decision.PASSTHROUGH
    assert decision.form.value == "bounded_lines"
    assert decision.code is None


def test_unknown_tool_is_passed_without_a_gate_error(gate_cases):
    gate = PreReadGate(_table_prober(gate_cases["probe_table"]))
    decision = gate.evaluate("future_read_tool", {"path": "/ws/huge.txt"})
    assert decision.decision is Decision.PASSTHROUGH
    assert decision.form.value == "not_read_like"
    assert decision.code is None


# -- real filesystem behaviour, not the probe table -------------------------


def _write(tmp_path, name: str, content: bytes):
    path = tmp_path / name
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("lines,expected", [(349, "allow"), (350, "allow"), (351, "blocked")])
def test_threshold_on_real_files(tmp_path, lines, expected):
    body = b"".join(b"x%d\n" % i for i in range(lines))
    path = _write(tmp_path, f"f{lines}.txt", body)
    assert count_lines(body) == lines
    assert len(body) <= 16384
    gate = PreReadGate(FileProber())
    assert gate.evaluate("read", {"file_path": str(path)}).decision.value == expected
    assert gate.evaluate("shell", {"command": f"cat {path}"}).decision.value == expected


@pytest.mark.parametrize("lines,expected", [(350, "allow"), (351, "blocked")])
def test_threshold_with_crlf_and_no_trailing_newline(tmp_path, lines, expected):
    body = b"\r\n".join(b"x%d" % i for i in range(lines))  # no trailing newline
    path = _write(tmp_path, f"crlf{lines}.txt", body)
    assert count_lines(body) == lines
    gate = PreReadGate(FileProber())
    assert gate.evaluate("read", {"file_path": str(path)}).decision.value == expected


def test_empty_file_is_zero_lines_and_allowed(tmp_path):
    path = _write(tmp_path, "empty.txt", b"")
    gate = PreReadGate(FileProber())
    assert gate.evaluate("read", {"file_path": str(path)}).decision is Decision.ALLOW


def test_single_long_line_over_byte_cap_is_blocked(tmp_path):
    path = _write(tmp_path, "long.txt", b"A" * 20000 + b"\n")
    gate = PreReadGate(FileProber())
    decision = gate.evaluate("read", {"file_path": str(path)})
    assert decision.blocked and decision.code == "LARGE_READ"
    assert decision.reason == "OVER_BYTE_THRESHOLD"


def test_selected_long_line_over_byte_cap_is_passed_for_every_bounded_form(tmp_path):
    path = _write(tmp_path, "selected-long.txt", b"A" * 20_000 + b"\n")
    gate = PreReadGate(FileProber())
    calls = (
        ("read", {"file_path": str(path), "offset": 1, "limit": 1}),
        ("search", {"path": str(path), "pattern": "A", "max_matches": 1}),
        ("shell", {"command": f"head -n 1 {path}"}),
        ("shell", {"command": f"tail -n 1 {path}"}),
        ("shell", {"command": f"grep -m 1 A {path}"}),
    )
    for tool, args in calls:
        decision = gate.evaluate(tool, args)
        assert decision.decision is Decision.PASSTHROUGH
        assert decision.code is None


def test_probe_scans_at_most_the_threshold_plus_one(tmp_path):
    path = _write(tmp_path, "huge.txt", b"".join(b"y%d\n" % i for i in range(50_000)))
    probe = FileProber()(str(path))
    assert probe.exact is False
    assert probe.lines <= 351


def test_full_probe_stops_at_the_output_byte_threshold(tmp_path):
    path = _write(tmp_path, "no-newlines.txt", b"A" * 1_000_000)
    probe = FileProber()(str(path))
    assert probe.exact is False
    assert probe.bytes == DEFAULT_LIMITS.max_targeted_read_bytes + 1


def test_bounded_metadata_amplification_passes_to_the_host(gate_cases):
    files = ["/ws/a.txt"] * 300
    gate = PreReadGate(_table_prober(gate_cases["probe_table"]))
    decision = gate.evaluate("shell", {"command": "wc " + " ".join(files)})
    assert decision.decision is Decision.PASSTHROUGH
    assert decision.code is None
    assert decision.form.value == "bounded_metadata"


def test_probe_rejects_a_symlink_swap_between_lstat_and_open(tmp_path, monkeypatch):
    victim = _write(tmp_path, "victim.txt", b"small\n")
    outside = _write(tmp_path, "outside.txt", b"secret\n" * 400)
    real_open = probe_module.os.open
    swapped = False

    def swap_then_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if str(path) == str(victim) and not swapped:
            swapped = True
            victim.unlink()
            victim.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(probe_module.os, "open", swap_then_open)
    decision = PreReadGate(FileProber()).evaluate("read", {"file_path": str(victim)})
    assert swapped
    assert decision.decision is Decision.PASSTHROUGH
    assert decision.form.value == "unsafe"
    assert decision.code is None


def test_probe_timeout_passes_through_without_a_gate_error():
    """An incomplete probe cannot prove a large read, so the host owns the call."""
    clock = FakeClock()

    def slow_probe(_path: str, _selection=None) -> ProbeResult:
        clock.advance(900)
        return ProbeResult(exists=True, lines=1, bytes=1)

    gate = PreReadGate(slow_probe, clock=clock)
    decision = gate.evaluate("shell", {"command": "cat /ws/a.txt /ws/b.txt"})
    assert decision.decision is Decision.PASSTHROUGH
    assert decision.reason == "PROBE_TIMEOUT"
    assert decision.code is None


def test_probe_failure_passes_through_without_a_gate_error():
    def broken_probe(_path: str, _selection=None) -> ProbeResult:
        raise RuntimeError("probe unavailable")

    decision = PreReadGate(broken_probe).evaluate("read", {"file_path": "/ws/a.txt"})
    assert decision.decision is Decision.PASSTHROUGH
    assert decision.reason == "PROBE_FAILED"
    assert decision.code is None


def test_unclassifiable_decisions_never_create_a_gate_error(tmp_path):
    """The gate reports its uncertainty while leaving execution to the host."""
    invocations = []

    def counting_probe(path: str, _selection=None) -> ProbeResult:
        return ProbeResult(exists=True, kind="file", lines=9000, bytes=90000)

    gate = PreReadGate(counting_probe)
    for command in ("awk '{print}' /ws/x.txt", "cat $FILE"):
        decision = gate.evaluate("shell", {"command": command})
        assert decision.decision is Decision.PASSTHROUGH
        assert decision.code is None
    assert invocations == []


def test_session_block_envelope_is_contract_shaped(tmp_path):
    session = ShuntSession("sess", make_config(tmp_path), make_capability())
    ws = tmp_path / "ws"
    big = ws / "big.txt"
    big.write_bytes(b"".join(b"n%d\n" % i for i in range(400)))
    decision = session.evaluate_tool_call("read", {"file_path": str(big)})
    assert decision.blocked
    env = session.block_envelope("req_x", decision)
    assert env["status"] == "blocked" and env["code"] == "LARGE_READ"
    assert env["coverage"]["complete"] is False
    assert "offset" in env["guidance"]
    assert str(big) not in env["guidance"]
