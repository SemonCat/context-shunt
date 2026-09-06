"""unit capability + Hermes adapter contract.

These exercise the adapter against a faithful stand-in for the Hermes plugin context - the
hook names, the block directive shape, ``ctx.llm`` - so the wiring, the normalization and
the capability decisions are asserted deterministically. They are not evidence about the
live host: that is the opt-in ``integration hermes`` gate, which reports not-run when the
host is absent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from context_shunt.capability import (
    CapabilityReport,
    DisabledReason,
    Support,
    disabled_by_config,
    supported,
    unsupported,
)
from context_shunt.errors import ShuntError
from context_shunt.limits import READER_MODEL
from context_shunt.session import ShuntSession
from tests.support import make_capability, make_config

pytestmark = pytest.mark.gate_capability

REPO = Path(__file__).resolve().parents[3]
ADAPTER_PATH = REPO / "adapters" / "hermes" / "context-shunt" / "__init__.py"


def _load_adapter():
    spec = importlib.util.spec_from_file_location("context_shunt_hermes", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeLlm:
    def __init__(self, model_override: str | None = None, reply: str | None = None):
        self.calls: list[dict] = []
        self._model_override = model_override
        self._reply = reply

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        text = self._reply or json.dumps(
            {
                "answer": "The retry ceiling is three [c1].",
                "citations": [
                    {"id": "c1", "line_start": 1, "line_end": 1, "quote": "max_retries = 3"}
                ],
            }
        )

        class Usage:
            input_tokens = 12
            output_tokens = 8

        class Result:
            pass

        result = Result()
        result.text = text
        result.model = self._model_override or kwargs.get("model", "")
        result.usage = Usage()
        return result


class FakeCtx:
    def __init__(self, config: dict, *, llm=None, hooks=None, with_tools=True):
        self.plugin_config = config
        self.llm = llm
        self.supported_hooks = (
            hooks
            if hooks is not None
            else [
                "pre_tool_call",
                "post_tool_call",
                "transform_tool_result",
                "on_session_end",
            ]
        )
        self.registered_hooks: list[str] = []
        self.registered_tools: list[str] = []
        self.messages: list[str] = []
        self._with_tools = with_tools
        self.logger = self

    def register_hook(self, name, _handler):
        self.registered_hooks.append(name)

    def register_tool(self, schema, _handler):
        if not self._with_tools:
            raise AssertionError("register_tool called when unavailable")
        self.registered_tools.append(schema["name"])

    def info(self, msg, *args):
        self.messages.append(msg % args if args else msg)


# -- core capability model --------------------------------------------------


def test_supported_and_unsupported_modes():
    report = CapabilityReport(
        adapter="a",
        adapter_version="1",
        host_name="h",
        host_version="1",
        contract_version="1.0",
        reader_model=READER_MODEL,
        modes=[
            supported("local_gate"),
            unsupported("suma_post_tool", DisabledReason.HOST_FAIL_OPEN),
        ],
    )
    assert report.enabled("local_gate") is True
    assert report.enabled("suma_post_tool") is False
    assert report.mode("suma_post_tool").reasons == (DisabledReason.HOST_FAIL_OPEN,)
    assert report.enabled("nonexistent") is False
    rendered = report.to_dict()
    assert rendered["modes"][1]["reasons"] == ["HOST_FAIL_OPEN"]
    assert rendered["modes"][1]["enabled"] is False


def test_config_disabled_is_distinct_from_unsupported():
    mode = disabled_by_config("suma_post_tool")
    assert mode.support is Support.DISABLED_BY_CONFIG
    assert mode.enabled is False


def test_capability_report_contains_no_paths_or_content():
    module = _load_adapter()
    report = module.build_capability_report(FakeCtx({}, llm=FakeLlm()))
    blob = json.dumps(report.to_dict())
    assert "/Users" not in blob and "/home/" not in blob


# -- Hermes adapter --------------------------------------------------------


def test_normalization_covers_read_search_and_shell():
    module = _load_adapter()
    assert module.normalize_tool_call("read_file", {"file_path": "/x", "limit": 5}) == (
        "read",
        {"file_path": "/x", "offset": None, "limit": 5},
    )
    assert module.normalize_tool_call("grep", {"pattern": "a", "max_matches": 5})[0] == "search"
    assert module.normalize_tool_call("terminal", {"command": "cat /x"}) == (
        "shell",
        {"command": "cat /x"},
    )
    assert module.normalize_tool_call("delegate_task", {"prompt": "x"})[0] == "other"


def test_request_id_is_sanitized_and_bounded():
    module = _load_adapter()
    assert module._request_id({"tool_call_id": "call/../../etc/passwd"}) == "req_call....etcpasswd"
    assert module._request_id({}) == "req_gate"
    assert len(module._request_id({"tool_call_id": "x" * 500})) <= 60


def test_suma_post_tool_is_unsupported_with_host_evidence():
    module = _load_adapter()
    report = module.build_capability_report(FakeCtx({}, llm=FakeLlm()))
    mode = report.mode("suma_post_tool")
    assert mode.support is Support.UNSUPPORTED
    assert DisabledReason.CAPTURE_AFTER_TRUNCATION in mode.reasons
    assert DisabledReason.HOST_FAIL_OPEN in mode.reasons
    assert any("try/except" in e for e in mode.evidence)
    assert any("post-truncation" in e for e in mode.evidence)


def test_gate_hook_is_registered_and_no_writer_tool_is(tmp_path):
    module = _load_adapter()
    ctx = FakeCtx(_config(tmp_path), llm=FakeLlm())
    module.register(ctx)
    assert "pre_tool_call" in ctx.registered_hooks
    assert ctx.registered_tools == ["context_shunt_read"]
    assert not any("writ" in t or "patch" in t for t in ctx.registered_tools)
    assert "capability report" in ctx.messages[0]
    assert "transform_tool_result" not in ctx.registered_hooks


def test_gate_hook_is_not_registered_when_the_host_lacks_it(tmp_path):
    module = _load_adapter()
    ctx = FakeCtx(_config(tmp_path), llm=FakeLlm(), hooks=["post_tool_call"])
    module.register(ctx)
    assert "pre_tool_call" not in ctx.registered_hooks


def test_writer_enabled_configuration_refuses_to_load(tmp_path):
    module = _load_adapter()
    ctx = FakeCtx({**_config(tmp_path), "writer": {"enabled": True}}, llm=FakeLlm())
    with pytest.raises(ShuntError):
        module.register(ctx)


def test_non_luna_model_configuration_refuses_to_load(tmp_path):
    module = _load_adapter()
    ctx = FakeCtx({**_config(tmp_path), "reader": {"model": "gpt-5.6-sol"}}, llm=FakeLlm())
    with pytest.raises(ShuntError):
        module.register(ctx)


def test_pre_tool_call_blocks_a_large_read_with_a_contract_envelope(tmp_path):
    module = _load_adapter()
    ctx = FakeCtx(_config(tmp_path), llm=FakeLlm())
    module.register(ctx)
    path = _planted(tmp_path, 351)
    result = module.pre_tool_call(
        tool_name="read_file", args={"file_path": str(path)}, task_id="t1", tool_call_id="tc1"
    )
    assert result["action"] == "block"
    envelope = json.loads(result["message"])
    assert envelope["status"] == "blocked" and envelope["code"] == "LARGE_READ"
    assert envelope["coverage"]["complete"] is False
    assert envelope["request_id"] == "req_tc1"
    assert str(path) not in result["message"]


def test_pre_tool_call_allows_the_threshold_and_bounded_reads(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    assert (
        module.pre_tool_call(
            tool_name="read_file", args={"file_path": str(_planted(tmp_path, 350))}, task_id="t1"
        )
        is None
    )
    assert (
        module.pre_tool_call(
            tool_name="read_file",
            args={"file_path": str(_planted(tmp_path, 351)), "offset": 1, "limit": 50},
            task_id="t1",
        )
        is None
    )


def test_pre_tool_call_blocks_unprovable_shell_and_passes_others(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    blocked = module.pre_tool_call(
        tool_name="terminal",
        args={"command": f"cat {_planted(tmp_path, 400)} | grep x"},
        task_id="t1",
    )
    assert json.loads(blocked["message"])["code"] == "UNCLASSIFIABLE_READ"
    assert (
        module.pre_tool_call(tool_name="terminal", args={"command": "npm test"}, task_id="t1")
        is None
    )
    assert (
        module.pre_tool_call(tool_name="delegate_task", args={"prompt": "x"}, task_id="t1") is None
    )


def test_reader_tool_answers_with_luna_and_verified_citations(tmp_path):
    module = _load_adapter()
    llm = FakeLlm()
    module.register(FakeCtx(_config(tmp_path), llm=llm))
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\nbackoff = fixed\n")
    out = json.loads(
        module.context_shunt_read(
            question="What is the retry ceiling?", paths=[str(path)], task_id="t2"
        )
    )
    assert out["code"] == "ANSWERED"
    assert out["citations"][0]["verified"] is True
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == READER_MODEL
    assert "What is the retry ceiling?" in llm.calls[0]["messages"][1]["content"]


def test_reader_tool_rejects_a_source_outside_the_roots(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    outside = tmp_path / "outside.txt"
    outside.write_text("classified\n")
    out = json.loads(
        module.context_shunt_read(question="What is in it?", paths=[str(outside)], task_id="t3")
    )
    assert out["status"] == "blocked" and out["code"] == "UNSAFE_SOURCE"
    assert str(outside) not in json.dumps(out)


def test_reader_tool_makes_zero_model_calls_without_a_question(tmp_path):
    module = _load_adapter()
    llm = FakeLlm()
    module.register(FakeCtx(_config(tmp_path), llm=llm))
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\n")
    out = json.loads(module.context_shunt_read(paths=[str(path)], task_id="t4"))
    assert llm.calls == []
    assert out["status"] == "error"


def test_reader_reports_model_error_rather_than_substituting(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm(model_override="gpt-5.6-sol")))
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\n")
    out = json.loads(
        module.context_shunt_read(question="What is it?", paths=[str(path)], task_id="t5")
    )
    assert out["coverage"]["omitted"][0]["reason"] == "MODEL_ERROR"
    assert out["answer"] == ""


def test_reader_tool_schema_declares_no_write_surface():
    module = _load_adapter()
    props = module.READER_TOOL_SCHEMA["parameters"]["properties"]
    assert sorted(props) == ["paths", "question"]


def test_suma_mode_stays_off_even_when_configuration_asks_for_it(tmp_path):
    config = make_config(tmp_path, suma_post_tool={"enabled": True})
    session = ShuntSession("sess", config, make_capability(suma=False))
    assert config.suma_post_tool.enabled is True
    assert session.suma_enabled is False
    assert session.post_tool_result("req_x", "y" * 200000) is None


def test_engine_spills_once_a_host_is_proven_safe(tmp_path):
    """The engine is not the blocker: given a proven host, the same call spills."""
    config = make_config(tmp_path, suma_post_tool={"enabled": True})
    session = ShuntSession("sess", config, make_capability(suma=True))
    assert session.suma_enabled is True
    outcome = session.post_tool_result("req_x", "y" * 200000)
    assert outcome.action == "spill"
    assert outcome.envelope["code"] == "SPILLED"
    assert outcome.envelope["answer"] == ""


def _config(tmp_path) -> dict:
    (tmp_path / "ws").mkdir(exist_ok=True)
    return {"workspace_roots": [str(tmp_path / "ws")], "spill_dir": str(tmp_path / "cache")}


def _planted(tmp_path, lines: int):
    path = tmp_path / "ws" / f"f{lines}.txt"
    path.write_text("".join(f"line {i}\n" for i in range(lines)))
    return path
