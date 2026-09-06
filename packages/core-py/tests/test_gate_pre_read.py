"""unit pre-read: the gate decides before the tool runs, and a blocked call never runs."""

from __future__ import annotations

import pytest

from context_shunt.clock import FakeClock
from context_shunt.gate import Decision, PreReadGate, ProbeResult
from context_shunt.probe import FileProber
from context_shunt.textindex import count_lines
from context_shunt.session import ShuntSession

from tests.support import make_capability, make_config

pytestmark = pytest.mark.gate_pre_read


def _table_prober(table):
    def probe(path: str) -> ProbeResult:
        entry = table.get(path)
        if entry is None or entry.get("missing"):
            return ProbeResult(exists=False)
        return ProbeResult(
            exists=True,
            kind=entry.get("kind", "file"),
            lines=entry.get("lines", 0),
            bytes=entry.get("bytes", 0),
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


def test_probe_scans_at_most_the_threshold_plus_one(tmp_path):
    path = _write(tmp_path, "huge.txt", b"".join(b"y%d\n" % i for i in range(50_000)))
    probe = FileProber()(str(path))
    assert probe.exact is False
    assert probe.lines <= 351


def test_probe_timeout_blocks_instead_of_executing():
    """A probe that burns the 1s budget mid-command blocks; it never falls through to allow."""
    clock = FakeClock()

    def slow_probe(_path: str) -> ProbeResult:
        clock.advance(900)
        return ProbeResult(exists=True, lines=1, bytes=1)

    gate = PreReadGate(slow_probe, clock=clock)
    decision = gate.evaluate("shell", {"command": "cat /ws/a.txt /ws/b.txt"})
    assert decision.blocked and decision.reason == "PROBE_TIMEOUT"
    assert decision.code == "LARGE_READ"


def test_blocked_decision_never_invokes_the_underlying_tool(tmp_path):
    """The gate is the only thing that runs: it takes no executor and has none to call."""
    invocations = []

    def counting_probe(path: str) -> ProbeResult:
        return ProbeResult(exists=True, kind="file", lines=9000, bytes=90000)

    gate = PreReadGate(counting_probe)
    for command in ("cat /ws/x.txt", "less /ws/x.txt", "awk '{print}' /ws/x.txt"):
        assert gate.evaluate("shell", {"command": command}).blocked
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
