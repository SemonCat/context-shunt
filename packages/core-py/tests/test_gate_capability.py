"""unit capability + Hermes adapter contract.

These exercise the adapter against a faithful stand-in for the Hermes plugin context - the
hook names, the block directive shape, ``ctx.llm`` - so the wiring, the normalization and
the capability decisions are asserted deterministically. They are not evidence about the
live host: that is the opt-in ``integration hermes`` gate, which reports not-run when the
host is absent.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from context_shunt.capability import (
    CapabilityReport,
    DisabledReason,
    Support,
    disabled_by_config,
    supported,
    unsupported,
)
from context_shunt.errors import ShuntError
from context_shunt.limits import EMITTED_SCHEMA_VERSION, READER_MODEL
from context_shunt.session import ShuntSession
from tests.support import (
    FakeLuna,
    answer_json,
    make_capability,
    make_config,
    over_old_reader_caps_fixture,
)

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
                "on_session_finalize",
                "on_session_reset",
            ]
        )
        self.registered_hooks: list[str] = []
        self.auxiliary_tasks: list[tuple[str, dict]] = []
        self.registered_tools: list[str] = []
        self.registered_toolsets: list[str] = []
        self.registered_handlers: dict[str, object] = {}
        self.messages: list[str] = []
        self._with_tools = with_tools
        self.logger = self

    def register_hook(self, name, _handler):
        self.registered_hooks.append(name)

    def register_auxiliary_task(self, key, *, display_name, description, defaults=None):
        """Mirrors PluginContext.register_auxiliary_task's real keyword-only signature."""
        assert key and all(c.isalnum() or c == "_" for c in key)
        self.auxiliary_tasks.append(
            (
                key,
                {
                    "display_name": display_name,
                    "description": description,
                    "defaults": dict(defaults or {}),
                },
            )
        )

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        """Mirrors hermes_cli.plugins.PluginContext.register_tool's real signature."""
        if not self._with_tools:
            raise AssertionError("register_tool called when unavailable")
        assert schema["name"] == name
        assert callable(handler)
        assert toolset and "override" not in kwargs
        self.registered_tools.append(name)
        self.registered_toolsets.append(toolset)
        self.registered_handlers[name] = handler

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
            unsupported("tool_result_capture", DisabledReason.HOST_FAIL_OPEN),
        ],
    )
    assert report.enabled("local_gate") is True
    assert report.enabled("tool_result_capture") is False
    assert report.mode("tool_result_capture").reasons == (DisabledReason.HOST_FAIL_OPEN,)
    assert report.enabled("nonexistent") is False
    rendered = report.to_dict()
    assert rendered["modes"][1]["reasons"] == ["HOST_FAIL_OPEN"]
    assert rendered["modes"][1]["enabled"] is False


def test_config_disabled_is_distinct_from_unsupported():
    mode = disabled_by_config("tool_result_capture")
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
    assert module.normalize_tool_call(
        "search_files",
        {
            "pattern": "a",
            "limit": 5,
            "target": "content",
            "output_mode": "content",
            "context": 0,
        },
    ) == (
        "search",
        {
            "path": None,
            "pattern": "a",
            "max_matches": 5,
            "target": "content",
            "output_mode": "content",
            "context": 0,
        },
    )
    assert module.normalize_tool_call("grep", {"pattern": "a", "max_matches": 5})[0] == "other"
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


def test_tool_result_capture_is_unsupported_by_default_with_host_evidence():
    """No operator attestation configured: the mode stays off, honestly reasoned."""
    module = _load_adapter()
    report = module.build_capability_report(FakeCtx({}, llm=FakeLlm()))
    mode = report.mode("tool_result_capture")
    assert mode.support is Support.UNSUPPORTED
    assert DisabledReason.ORDERING_UNPROVEN in mode.reasons
    assert any("no operator attestation" in e for e in mode.evidence)
    assert any("fail-open" in e for e in mode.evidence)


def test_tool_result_capture_is_supported_with_operator_attestation(tmp_path):
    """An explicit operator attestation, and only that, turns the mode on."""
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    ctx = FakeCtx(config, llm=FakeLlm())
    module.register(ctx)
    mode = module._capability.mode("tool_result_capture")
    assert mode.support is Support.SUPPORTED
    assert any("operator attestation" in e for e in mode.evidence)
    assert any("2026-09-09" in e for e in mode.evidence)
    assert "transform_tool_result" in ctx.registered_hooks


def test_tool_result_capture_hook_not_registered_without_attestation(tmp_path):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True}
    ctx = FakeCtx(config, llm=FakeLlm())
    module.register(ctx)
    assert module._capability.enabled("tool_result_capture") is False
    assert "transform_tool_result" not in ctx.registered_hooks


def test_tool_result_capture_hook_not_registered_when_attested_but_config_disabled(tmp_path):
    """The attestation alone is not enough; `enabled` still gates registration."""
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": False, "host_ordering_verified_locally": True}
    ctx = FakeCtx(config, llm=FakeLlm())
    module.register(ctx)
    assert module._capability.enabled("tool_result_capture") is True
    assert "transform_tool_result" not in ctx.registered_hooks


def test_transform_tool_result_never_returns_none_for_an_oversized_capture_failure(tmp_path):
    """The one no-raw-leak invariant that matters at this hook: never fall through raw."""
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    module.register(FakeCtx(config, llm=FakeLlm()))
    oversized = "y" * 200_000

    class _BoomSession:
        def post_tool_result(self, *args, **kwargs):
            raise RuntimeError("unexpected")

    module._sessions["tcap"] = _BoomSession()
    out = module.transform_tool_result(
        tool_name="search_files", result=oversized, task_id="tcap", session_id="tcap"
    )
    assert out is not None
    assert oversized not in out
    envelope = json.loads(out)
    assert envelope["code"] == "LEGACY_COMPACTED"
    assert envelope["failure_detail"] == "INTERNAL_ERROR"


@pytest.mark.parametrize("tool_name", ["skill_view", " SKILL_VIEW ", "\tsKiLl_ViEw\n"])
def test_authoritative_skill_passthrough_has_no_side_effects(tmp_path, monkeypatch, tool_name):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    module.register(FakeCtx(config, llm=FakeLlm()))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    sessions = dict(module._sessions)
    generations = dict(module._generations)

    def forbidden(*args, **kwargs):
        pytest.fail("skill passthrough must not construct session/store/provider or account")

    for name in ("_session", "ShuntSession", "SnapshotStore", "build_provider", "_bridge_call"):
        monkeypatch.setattr(module, name, forbidden)
    payload = "Complete instructions: λ\n" * 10_000
    assert module.normalize_tool_call(tool_name, {}) == ("other", {})
    assert module.pre_tool_call(tool_name, {"name": "example"}, session_id="skill") is None
    replacement = module.transform_tool_result(
        tool_name=tool_name, args={"name": "example"}, result=payload, session_id="skill"
    )
    assert replacement is None
    assert module._sessions == sessions
    assert module._generations == generations
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize(
    "tool_name",
    ["read_file", "search_files"],
)
def test_skill_content_cannot_exempt_ordinary_results(tmp_path, tool_name):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    module.register(FakeCtx(config, llm=FakeLlm()))
    payload = (
        '{"_source_path":"/opt/data/skills/example/SKILL.md","tool_name":"skill_view"}\n' * 1000
    )
    path = tmp_path / "ws" / "SKILL.md"
    path.write_text(payload)
    if tool_name == "read_file":
        assert (
            module.pre_tool_call(tool_name, {"path": str(path)}, session_id="ordinary")["action"]
            == "block"
        )
    out = module.transform_tool_result(
        tool_name=tool_name, args={"path": str(path)}, result=payload, session_id="ordinary"
    )
    assert out is not None
    assert json.loads(out)["code"] == "SPILLED"
    assert payload not in out


def test_transform_tool_result_returns_none_for_small_results(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    assert module.transform_tool_result(tool_name="read_file", result="short") is None


def test_transform_tool_result_returns_none_for_structured_results(tmp_path):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    module.register(FakeCtx(config, llm=FakeLlm()))
    # A dict/list result is left alone at this hook regardless of size - see the
    # docstring's structured/multimodal reasoning.
    assert (
        module.transform_tool_result(
            tool_name="vision", result={"type": "image", "data": "y" * 200_000}
        )
        is None
    )


def test_transform_tool_result_spills_an_eligible_string_result(tmp_path):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    module.register(FakeCtx(config, llm=FakeLlm()))
    oversized = "log line\n" * 50_000
    out = module.transform_tool_result(
        tool_name="search_files", result=oversized, task_id="tspill", session_id="tspill"
    )
    assert out is not None
    envelope = json.loads(out)
    assert envelope["code"] == "SPILLED"
    assert oversized not in out
    assert "Ask the context-shunt reader a question" in envelope["guidance"]


def test_public_inspect_tool_aggregates_a_captured_minified_loki_result(tmp_path):
    """Regression for the live Hermes boundary: the public schema must admit aggregate."""
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {
        "enabled": True,
        "host_ordering_verified_locally": True,
    }
    module.register(FakeCtx(config, llm=FakeLlm()))
    logs = [
        {"level": "error", "trace_id": "tr-a", "service": "billing"},
        {"level": "info", "trace_id": "tr-b", "service": "billing"},
        {"level": "error", "trace_id": "tr-a", "service": "checkout"},
        {"level": "error", "trace_id": "tr-c", "service": "billing"},
        {"level": "error", "trace_id": None},
        {"level": "error", "trace_id": "tr-d", "service": None},
    ]
    logs.extend(
        {"level": "info", "trace_id": f"padding-{index}", "service": "padding"}
        for index in range(700)
    )
    body = json.dumps(
        {
            "data": {
                "result": [
                    {
                        "values": [
                            [str(index), json.dumps(row, separators=(",", ":"))]
                            for index, row in enumerate(logs)
                        ]
                    }
                ]
            }
        },
        separators=(",", ":"),
    )
    spilled = json.loads(
        module.transform_tool_result(
            tool_name="search_files",
            result=body,
            task_id="aggregate-public",
            session_id="aggregate-public",
        )
    )
    handle = spilled["sources"][0]
    envelope = json.loads(
        module.context_shunt_inspect(
            source_id=handle["source_id"],
            snapshot_id=handle["snapshot_id"],
            selector={
                "kind": "aggregate",
                "records_pointer": "/data/result",
                "expand_pointer": "/values",
                "record_pointer": "/1",
                "parse_json": True,
                "filter": {"pointer": "/level", "equals": "error"},
                "distinct": ["/trace_id"],
                "group_by": ["/service"],
            },
            task_id="aggregate-public",
            session_id="aggregate-public",
        )
    )
    assert envelope["code"] == "EXTRACTED"
    assert envelope["provenance"]["attempts_started"] == 0
    result = json.loads(envelope["extraction"]["segments"][0]["text"])
    assert result["matched_count"] == 5
    assert result["records_scanned"] == len(logs)
    assert result["distinct"][0]["count"] == 4
    assert result["groups"] == [
        {"key": ["billing"], "count": 2},
        {"key": ["checkout"], "count": 1},
        {"key": [None], "count": 1},
        {"key": [{"missing": True}], "count": 1},
    ]


def test_gate_hook_is_registered_and_no_writer_tool_is(tmp_path):
    module = _load_adapter()
    ctx = FakeCtx(_config(tmp_path), llm=FakeLlm())
    module.register(ctx)
    assert "pre_tool_call" in ctx.registered_hooks
    # Three read-only escape hatches, one toolset, no writer.
    assert ctx.registered_tools == [
        "context_shunt_read",
        "context_shunt_inspect",
        "context_shunt_stats",
    ]
    assert set(ctx.registered_toolsets) == {"context_shunt"}
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


def test_a_configured_reader_model_loads_and_is_reported_as_requested(tmp_path):
    """1.1 makes the reader model configurable; the envelope keeps it honest."""
    module = _load_adapter()
    ctx = FakeCtx({**_config(tmp_path), "reader": {"model": "gpt-5.6-sol"}}, llm=FakeLlm())
    module.register(ctx)
    assert module._config.reader.model == "gpt-5.6-sol"
    assert module.capability_report()["reader_model"] == "gpt-5.6-sol"
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\n")
    out = json.loads(
        module.context_shunt_read(question="What is it?", paths=[str(path)], task_id="taux")
    )
    assert out["provenance"]["requested_model"] == "gpt-5.6-sol"
    assert any(
        'context-shunt reader metric: {"duration_ms":' in message
        and '"status":"ok","code":"ANSWERED"' in message
        for message in ctx.messages
    )


def test_the_reader_is_registered_as_an_auxiliary_task(tmp_path):
    """Registered so it appears in `hermes model` with its own auxiliary config block."""
    module = _load_adapter()
    ctx = FakeCtx(_config(tmp_path), llm=FakeLlm())
    module.register(ctx)
    assert ctx.auxiliary_tasks
    key, payload = ctx.auxiliary_tasks[0]
    assert key == module.AUX_TASK_KEY == "context_shunt_reader"
    assert payload["display_name"] and payload["description"]
    assert payload["defaults"]["model"] == module._config.reader.model
    assert module._capability.enabled("reader_task_config")


def test_user_auxiliary_config_overrides_the_plugin_default(tmp_path, monkeypatch):
    """User config wins: the host's auxiliary.<key> block beats the plugin's own default."""
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    assert module._reader_target() == ("", module._config.reader.model)
    monkeypatch.setattr(
        module,
        "_auxiliary_task_config",
        lambda: {"provider": "openrouter", "model": "gpt-5.6-sol"},
    )
    assert module._reader_target() == ("openrouter", "gpt-5.6-sol")
    # "auto" is the host's inherit sentinel, not a literal model id.
    monkeypatch.setattr(
        module, "_auxiliary_task_config", lambda: {"provider": "auto", "model": "auto"}
    )
    assert module._reader_target() == ("", module._config.reader.model)


def test_per_turn_session_end_keeps_recovery_handles(tmp_path):
    """Hermes fires on_session_end every turn; handles must survive it."""
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\n")
    first = json.loads(
        module.context_shunt_read(question="What is it?", paths=[str(path)], task_id="tturn")
    )
    handle = first["sources"][0]
    module.on_session_end(session_id="", task_id="tturn")
    # The next turn refines the question against the same snapshot, without recapturing.
    second = json.loads(
        module.context_shunt_read(
            question="And the backoff?",
            handles=[{"source_id": handle["source_id"], "snapshot_id": handle["snapshot_id"]}],
            task_id="tturn",
        )
    )
    assert second["code"] in ("ANSWERED", "NO_MATCH")
    assert second["sources"][0]["snapshot_id"] == handle["snapshot_id"]

    # A real boundary does revoke it.
    module.on_session_finalize(session_id="", task_id="tturn")
    third = json.loads(
        module.context_shunt_read(
            question="And the backoff?",
            handles=[{"source_id": handle["source_id"], "snapshot_id": handle["snapshot_id"]}],
            task_id="tturn",
        )
    )
    assert third["code"] == "SOURCE_EXPIRED"


def test_session_reset_bumps_the_generation_so_old_handles_cannot_be_replayed(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\n")
    first = json.loads(
        module.context_shunt_read(question="What is it?", paths=[str(path)], task_id="tgen")
    )
    handle = first["sources"][0]
    module.on_session_reset(session_id="", task_id="tgen")
    replayed = json.loads(
        module.context_shunt_read(
            question="What is it?",
            handles=[{"source_id": handle["source_id"], "snapshot_id": handle["snapshot_id"]}],
            task_id="tgen",
        )
    )
    assert replayed["code"] == "SOURCE_EXPIRED"


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


def test_pre_tool_call_passes_unclassifiable_shell_and_other_tools(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    passed = module.pre_tool_call(
        tool_name="terminal",
        args={"command": f"cat {_planted(tmp_path, 400)} | grep x"},
        task_id="t1",
    )
    assert passed is None
    assert (
        module.pre_tool_call(tool_name="terminal", args={"command": "npm test"}, task_id="t1")
        is None
    )
    assert (
        module.pre_tool_call(tool_name="delegate_task", args={"prompt": "x"}, task_id="t1") is None
    )


def test_pre_tool_call_passes_hermes_file_search_without_gate_error(tmp_path):
    """Hermes' target=files search is a host-owned bounded file search."""
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    result = module.pre_tool_call(
        tool_name="search_files",
        args={
            "pattern": "*.py",
            "target": "files",
            "path": str(tmp_path),
            "limit": 50,
            "offset": 0,
        },
        task_id="t-search-files",
    )
    assert result is None


def test_pre_tool_call_fails_open_when_gate_session_cannot_be_created(tmp_path, monkeypatch):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))

    def broken_session(*_args, **_kwargs):
        raise RuntimeError("synthetic gate setup failure")

    monkeypatch.setattr(module, "_session", broken_session)
    assert (
        module.pre_tool_call(
            tool_name="read_file", args={"path": str(_planted(tmp_path, 351))}, task_id="t-gate"
        )
        is None
    )


def test_pre_tool_call_fails_open_on_malformed_host_arguments(tmp_path):
    """Normalization errors stay with Hermes' host dispatcher."""
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    assert (
        module.pre_tool_call(tool_name="read_file", args="not-a-mapping", task_id="t-bad") is None
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


def test_hermes_reader_timeout_returns_compactor_summary_and_readable_raw_path(tmp_path):
    """The adapter-visible fail-open shape includes navigation and exact recovery."""

    class SlowUnavailableLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append({"messages": messages, **kwargs})
            time.sleep(0.05)
            raise RuntimeError("synthetic unavailable")

    module = _load_adapter()
    llm = SlowUnavailableLlm()
    config = {
        **_config(tmp_path),
        # Give validation/planning enough headroom to reach the provider even when this
        # test follows the rest of the capability suite.  The model-stage deadline stays
        # deliberately tiny, while the primary's 50 ms delay still outlives the 30 ms
        # absolute request deadline and therefore forbids a late fallback attempt.
        "limits": {"request_deadline_ms": 30, "model_call_deadline_ms": 10},
        "reader": {
            "fallback_chain": [{"model": "gpt-5.6-sol"}],
            "legacy_compaction_max_chars": 1_000,
        },
    }
    module.register(FakeCtx(config, llm=llm))
    marker = "omitted-middle-runtime-readback-9f8e7d"
    rows = [f"ordinary row {index} value {index * 19}\n" for index in range(800)]
    rows[400] = marker + "\n"
    body = "".join(rows)
    path = tmp_path / "ws" / "deadline-source.txt"
    path.write_text(body)

    out = json.loads(
        module.context_shunt_read(
            question="What was retained?", paths=[str(path)], task_id="t-deadline"
        )
    )
    legacy = out["legacy_compaction"]
    assert out["status"] == "partial" and out["code"] == "LEGACY_COMPACTED"
    assert legacy["original_failure"] == "TIMEOUT"
    assert legacy["summary"] and marker not in legacy["summary"]
    artifact_path = Path(legacy["raw_artifact_path"])
    assert artifact_path.is_absolute()
    raw = artifact_path.read_bytes()
    assert raw == body.encode()
    assert len(raw) == legacy["original_bytes"]
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == legacy["snapshot_id"]
    assert body not in json.dumps(out)
    assert out["sources"][0]["source_id"] == legacy["source_id"]

    # Let the timed-out primary finish. Neither the configured Sol fallback nor the
    # reader's outer retry may start after the absolute deadline.
    time.sleep(0.08)
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == READER_MODEL


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


def test_reader_refuses_a_reported_model_that_contradicts_the_request(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm(model_override="gpt-5.6-sol")))
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\n")
    out = json.loads(
        module.context_shunt_read(question="What is it?", paths=[str(path)], task_id="t5")
    )
    assert out["status"] == "error" and out["code"] == "MODEL_ERROR"
    assert out["answer"] == ""
    assert out["provenance"]["attribution_status"] == "mismatch"
    # A model failure is not a handle failure.
    assert out["recovery"]["handles_valid"] is True


def test_old_hermes_attribution_is_unverified_and_never_claims_actual(tmp_path):
    """The compatibility facade cannot separate a provider report from an echo."""
    module = _load_adapter()
    llm = FakeLlm()
    module.register(FakeCtx(_config(tmp_path), llm=llm))
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\n")
    out = json.loads(
        module.context_shunt_read(
            question="What is the retry ceiling?", paths=[str(path)], task_id="t6"
        )
    )
    assert out["provenance"]["derived"] is True
    assert out["provenance"]["attribution_status"] == "unverified"
    assert out["provenance"]["resolved_model"] is None
    assert "task" not in llm.calls[0]
    reader = module._capability.mode("reader")
    assert any("never claims actual" in line for line in reader.evidence)


def test_task_aware_hermes_routes_the_registered_slot_and_reports_resolution(tmp_path):
    """Current Hermes returns the auxiliary router's selected route as a routing fact."""

    class TaskAwareLlm(FakeLlm):
        def complete(self, messages, *, task=None, **kwargs):
            result = super().complete(messages, **kwargs)
            self.calls[-1]["task"] = task
            result.provider = kwargs.get("provider", "")
            result.audit = {"task": task}
            return result

    module = _load_adapter()
    llm = TaskAwareLlm()
    module.register(FakeCtx(_config(tmp_path), llm=llm))
    path = tmp_path / "ws" / "conf.txt"
    path.write_text("max_retries = 3\n")
    out = json.loads(
        module.context_shunt_read(
            question="What is the retry ceiling?", paths=[str(path)], task_id="t6-current"
        )
    )
    assert llm.calls[0]["task"] == module.AUX_TASK_KEY
    assert out["provenance"]["attribution_status"] == "resolved"
    assert out["provenance"]["resolved_model"] == READER_MODEL
    assert out["provenance"]["reported_model"] is None
    reader = module._capability.mode("reader")
    assert any("post-policy route" in line for line in reader.evidence)


def test_tool_schemas_declare_no_write_surface_and_no_full_retrieval():
    module = _load_adapter()
    assert sorted(module.READER_TOOL_SCHEMA["parameters"]["properties"]) == [
        "handles",
        "paths",
        "question",
        "selector",
    ]
    assert sorted(module.INSPECT_TOOL_SCHEMA["parameters"]["properties"]) == [
        "cursor",
        "max_result_bytes",
        "max_scan_lines",
        "selector",
        "snapshot_id",
        "source_id",
    ]
    assert sorted(module.STATS_TOOL_SCHEMA["parameters"]["properties"]) == ["page", "page_size"]
    # No tool may name a mutating or full-retrieval capability in its own surface.
    names = [schema["name"] for schema, _h, _m in module.TOOLS]
    parameters = json.dumps([schema["parameters"] for schema, _h, _m in module.TOOLS]).lower()
    for forbidden in ("write", "patch", "apply", "full", "raw", "payload"):
        assert not any(forbidden in name for name in names)
        assert forbidden not in parameters


def test_registered_tool_schemas_are_strict_canonical_parameters():
    """Hermes must reject the same malformed tool arguments as the core contract."""
    module = _load_adapter()
    schemas = {schema["name"]: schema["parameters"] for schema, _h, _m in module.TOOLS}

    assert set(schemas) == {
        "context_shunt_read",
        "context_shunt_inspect",
        "context_shunt_stats",
        "context_shunt_import",
    }
    for parameters in schemas.values():
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert "$ref" not in json.dumps(parameters)

    read = Draft202012Validator(schemas["context_shunt_read"])
    valid_handle = {"source_id": "src_abcd", "snapshot_id": "sha256:" + "a" * 64}
    assert read.is_valid({"question": "What is it?", "paths": ["/tmp/a"]})
    assert read.is_valid({"question": "What is it?", "handles": [valid_handle]})
    assert not read.is_valid(
        {"question": "What is it?", "paths": ["/tmp/a"], "handles": [valid_handle]}
    )
    assert not read.is_valid({"question": "What is it?", "paths": ["/tmp/a"], "unexpected": 1})
    assert not read.is_valid(
        {
            "question": "What is it?",
            "handles": [{**valid_handle, "snapshot_id": "sha256:" + "b" * 71}],
        }
    )
    assert not read.is_valid(
        {
            "question": "What is it?",
            "paths": ["/tmp/a"],
            "selector": {"kind": "all", "extra": 1},
        }
    )

    inspect = Draft202012Validator(schemas["context_shunt_inspect"])
    valid_inspect = {
        "source_id": "src_abcd",
        "snapshot_id": "sha256:" + "a" * 64,
        "selector": {"kind": "lines", "start": 1, "end": 2},
    }
    assert inspect.is_valid(valid_inspect)
    assert inspect.is_valid(
        {
            **valid_inspect,
            "selector": {
                "kind": "aggregate",
                "records_pointer": "/data/result",
                "expand_pointer": "/values",
                "record_pointer": "/1",
                "parse_json": True,
                "filter": {"pointer": "/level", "equals": "error"},
                "distinct": ["/trace_id"],
                "group_by": ["/service"],
            },
        }
    )
    assert not inspect.is_valid({**valid_inspect, "unexpected": True})
    assert not inspect.is_valid(
        {
            **valid_inspect,
            "selector": {"kind": "lines", "start": 1, "end": 2, "needle": "x"},
        }
    )
    assert not inspect.is_valid({**valid_inspect, "max_result_bytes": 16385})

    stats = Draft202012Validator(schemas["context_shunt_stats"])
    assert stats.is_valid({})
    assert stats.is_valid({"page": 1, "page_size": 8})
    assert not stats.is_valid({"page": 0})
    assert not stats.is_valid({"unknown": 1})

    imported = Draft202012Validator(schemas["context_shunt_import"])
    assert imported.is_valid({"manifest_path": "/tmp/manifest.json"})
    assert not imported.is_valid({"manifest_path": "/tmp/manifest.json", "unknown": 1})


def test_malformed_snapshot_id_has_actionable_safe_failure(tmp_path):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    invalid_snapshot = "sha256:" + "a" * 71
    out = json.loads(
        module.context_shunt_read(
            question="What is it?",
            handles=[{"source_id": "src_abcd", "snapshot_id": invalid_snapshot}],
            task_id="bad-snapshot",
        )
    )

    assert out["status"] == "error"
    assert out["code"] == "INVALID_REQUEST"
    assert out["failure_detail"] == "INVALID_SNAPSHOT_ID"
    assert out["recovery"]["handles_valid"] is False
    assert "REUSE_POINTER_PAIR" in out["recovery"]["actions"]
    assert "exact source_id/snapshot_id pair" in out["guidance"]
    assert invalid_snapshot not in json.dumps(out)

    inspected = json.loads(
        module.context_shunt_inspect(
            source_id="src_abcd",
            snapshot_id=invalid_snapshot,
            selector={"kind": "lines", "start": 1, "end": 1},
            task_id="bad-snapshot-inspect",
        )
    )
    assert inspected["code"] == "INVALID_REQUEST"
    assert inspected["failure_detail"] == "INVALID_SNAPSHOT_ID"
    assert inspected["recovery"]["handles_valid"] is False
    assert "REUSE_POINTER_PAIR" in inspected["recovery"]["actions"]
    assert invalid_snapshot not in json.dumps(inspected)


def test_inspect_recovers_an_oversized_single_line_tool_result_through_the_real_tool_call(
    tmp_path,
):
    """Reproduces the reported symptom end-to-end at the real ``context_shunt_inspect``
    tool-call boundary, not just the ``Inspector``/``ShuntSession`` unit seam.

    A roughly 252 KiB double-encoded JSON value has only one physical line. The ordinary
    line selector must make bounded cursor progress, while search and byte selectors let a
    caller recover only the relevant window without copying the entire raw payload.
    """
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    marker = "NEEDLE_日本"
    inner = json.dumps(
        {"records": [{"id": 1, "marker": marker, "blob": "x" * 252_300}]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    body = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    assert 252_000 < len(body.encode("utf-8")) < 253_000
    path = tmp_path / "ws" / "result.json"
    path.write_text(body, encoding="utf-8")

    read_out = json.loads(
        module.context_shunt_read(
            question="What does it contain?", paths=[str(path)], task_id="t-oversized-line"
        )
    )
    handle = read_out["sources"][0]

    selector = {"kind": "lines", "start": 1, "end": 1}
    request: dict = {
        "source_id": handle["source_id"],
        "snapshot_id": handle["snapshot_id"],
        "selector": selector,
        "max_result_bytes": 4096,
        "task_id": "t-oversized-line",
    }
    first = json.loads(module.context_shunt_inspect(**request))
    assert first["code"] == "EXTRACTED"
    assert first["status"] == "partial"
    assert 0 < first["extraction"]["result_bytes"] <= 4096
    assert first["extraction"]["segments"][0]["kind"] == "bytes"
    assert first["extraction"]["next_cursor"]
    assert body not in json.dumps(first, ensure_ascii=False)

    request["cursor"] = first["extraction"]["next_cursor"]
    second = json.loads(module.context_shunt_inspect(**request))
    assert second["code"] == "EXTRACTED"
    assert second["extraction"]["result_bytes"] > 0
    assert (
        second["extraction"]["segments"][0]["start"]
        == first["extraction"]["segments"][0]["end"]
    )

    searched = json.loads(
        module.context_shunt_inspect(
            source_id=handle["source_id"],
            snapshot_id=handle["snapshot_id"],
            selector={
                "kind": "search",
                "needle": marker,
                "max_matches": 1,
                "context_lines": 0,
            },
            max_result_bytes=1024,
            max_scan_lines=10,
            task_id="t-oversized-line",
        )
    )
    assert searched["code"] == "EXTRACTED"
    assert searched["status"] == "partial"
    assert searched["extraction"]["matches_found"] == 1
    assert marker in searched["extraction"]["segments"][0]["text"]
    assert searched["extraction"]["segments"][0]["kind"] == "bytes"
    assert "0-based" in searched["guidance"]
    assert "identical selector" in searched["guidance"]

    marker_start = body.encode("utf-8").index(marker.encode("utf-8"))
    bad_cut = json.loads(
        module.context_shunt_inspect(
            source_id=handle["source_id"],
            snapshot_id=handle["snapshot_id"],
            selector={"kind": "bytes", "start": marker_start + 8, "end": marker_start + 12},
            task_id="t-oversized-line",
        )
    )
    assert bad_cut["code"] == "INVALID_REQUEST"
    assert bad_cut["failure_detail"] == "UTF8_RANGE_BOUNDARY"

    marker_end = marker_start + len(marker.encode("utf-8"))
    exact = json.loads(
        module.context_shunt_inspect(
            source_id=handle["source_id"],
            snapshot_id=handle["snapshot_id"],
            selector={"kind": "bytes", "start": marker_start, "end": marker_end},
            task_id="t-oversized-line",
        )
    )
    assert exact["code"] == "EXTRACTED"
    assert exact["extraction"]["segments"][0]["text"] == marker
    # The strict handle check still holds throughout recovery: a wrong snapshot on any page
    # is refused, not silently repaired or searched for.
    tampered = dict(request)
    tampered["snapshot_id"] = "sha256:" + "f" * 64
    tampered.pop("cursor", None)
    refused = json.loads(module.context_shunt_inspect(**tampered))
    assert refused["code"] == "SOURCE_CHANGED"
    assert refused["failure_detail"] == "SNAPSHOT_MISMATCH"


def test_capture_mode_stays_off_even_when_configuration_asks_for_it(tmp_path):
    """Regression: the deprecated `suma_post_tool` config key still works as an alias."""
    config = make_config(tmp_path, suma_post_tool={"enabled": True})
    session = ShuntSession("sess", config, make_capability(tool_result_capture=False))
    assert config.suma_post_tool.enabled is True
    assert config.tool_result_capture.enabled is True
    assert session.tool_result_capture_enabled is False
    assert session.suma_enabled is False
    assert session.post_tool_result("req_x", "y" * 200000) is None


def test_engine_spills_once_a_host_is_proven_safe(tmp_path):
    """The engine is not the blocker: given a proven host, the same call spills."""
    config = make_config(tmp_path, tool_result_capture={"enabled": True})
    session = ShuntSession("sess", config, make_capability(tool_result_capture=True))
    assert session.tool_result_capture_enabled is True
    assert session.suma_enabled is True
    outcome = session.post_tool_result("req_x", "y" * 200000)
    assert outcome.action == "spill"
    assert outcome.envelope["code"] == "SPILLED"
    assert outcome.envelope["answer"] == ""


def test_truncated_capture_stays_partial_on_a_later_real_read(tmp_path):
    config = make_config(
        tmp_path,
        tool_result_capture={"enabled": True, "host_ordering_verified_locally": True},
    )
    provider = FakeLuna(replies=[answer_json("", [])])
    session = ShuntSession(
        "sess",
        config,
        make_capability(tool_result_capture=True),
        provider=provider,
    )
    body = "observed marker value\n" + "padding line\n" * 4000
    outcome = session.post_tool_result("req_capture", body, upstream_truncated=True)
    assert outcome.action == "spill"
    pointer = outcome.envelope["pointer"]
    answered = session.read(
        {
            "schema_version": EMITTED_SCHEMA_VERSION,
            "request_id": "req_later_read",
            "operation": "read",
            "question": "Is the marker absent?",
            "sources": [
                {
                    "source_id": pointer["source_id"],
                    "snapshot_id": pointer["snapshot_id"],
                    "selector": {"kind": "lines", "start": 1, "end": 1},
                }
            ],
            "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
        }
    )
    assert answered["code"] == "NO_MATCH"
    assert answered["status"] == "partial"
    assert answered["coverage"]["complete"] is False
    assert answered["coverage"]["upstream_truncated"] is True
    assert "not a confirmed absence" in answered["guidance"]


def _config(tmp_path) -> dict:
    (tmp_path / "ws").mkdir(exist_ok=True)
    return {"workspace_roots": [str(tmp_path / "ws")], "spill_dir": str(tmp_path / "cache")}


def _planted(tmp_path, lines: int):
    path = tmp_path / "ws" / f"f{lines}.txt"
    path.write_text("".join(f"line {i}\n" for i in range(lines)))
    return path


# -- the manifest must describe what the adapter actually registers -----------


def _manifest_text() -> str:
    return (ADAPTER_PATH.parent / "plugin.yaml").read_text(encoding="utf-8")


def _declared_tools() -> list[str]:
    """The `provides_tools:` list, read without a YAML dependency."""
    tools: list[str] = []
    in_block = False
    for line in _manifest_text().splitlines():
        if line.startswith("provides_tools:"):
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if stripped.startswith("- "):
                tools.append(stripped[2:].strip())
            elif stripped and not line.startswith((" ", "\t")):
                break
    return tools


def test_the_manifest_declares_every_tool_the_adapter_registers():
    """A tool the host cannot see declared is a tool an operator cannot audit.

    The manifest listed only `context_shunt_read` while the adapter registers three
    tools. The manifest is the audit surface, so it lists every tool the adapter *can*
    register - including `context_shunt_import`, which a given deployment may leave off.
    """
    module = _load_adapter()
    registered = [schema["name"] for schema, _handler, _mode in module.TOOLS]
    assert sorted(_declared_tools()) == sorted(registered)
    assert len(registered) == 4
    # Read-only: nothing that writes is registered or declared. The import tool adopts an
    # artifact someone else wrote; it never writes to a source of its own.
    assert not any("write" in name or "patch" in name for name in registered)


def test_the_manifest_version_tracks_the_core_it_ships_with():
    """A manifest pinned at 1.0.0 while shipping the 1.1 core misreports the contract."""
    from context_shunt import __version__ as core_version

    assert f'version: "{core_version}"' in _manifest_text()


#: Tools only one adapter can offer, with the reason. A name may sit here only while the
#: capability report says the same thing - the two must not be able to disagree.
_ADAPTER_ONLY_TOOLS = {
    "context_shunt_import": "the import boundary exists only in the Python core",
}


def test_the_manifest_and_the_openclaw_plugin_declare_the_same_read_only_tools():
    """Both adapters expose the same read-only tools, except where one truthfully cannot.

    This used to be a flat equality, which would have forced the OpenClaw manifest to
    declare an import tool its core cannot implement - the manifest would have been the
    lie instead of the divergence. The exception list is explicit and is cross-checked
    against the OpenClaw capability report below, so a tool cannot be quietly dropped
    from one adapter while both reports claim parity.
    """
    openclaw = json.loads(
        (REPO / "adapters" / "openclaw" / "openclaw.plugin.json").read_text(encoding="utf-8")
    )
    hermes = set(_declared_tools())
    assert set(openclaw["contracts"]["tools"]) == hermes - set(_ADAPTER_ONLY_TOOLS)


def test_the_openclaw_capability_source_reports_the_tool_it_does_not_declare():
    """The manifest exception has to be backed by an unsupported mode, not a comment.

    A tool absent from the OpenClaw manifest is only honest if the adapter also reports
    the mode behind it as unsupported, with a reason that names a core gap rather than a
    host limitation it does not have.
    """
    source = (REPO / "adapters" / "openclaw" / "src" / "capability.ts").read_text(encoding="utf-8")
    assert 'unsupported("artifact_import", ["IMPORT_UNIMPLEMENTED"]' in source


def test_hermes_import_handler_reports_guarded_error_without_pointer_credit(tmp_path):
    """The registered Hermes handler accounts for the envelope the host receives.

    This drives the adapter's actual ``context_shunt_import`` handler with a valid
    manifest and a deliberately narrow output cap. It reproduces the host-facing path
    without requiring a live Hermes process or touching a user's configuration.
    """
    module = _load_adapter()
    import_root = tmp_path / "imports"
    import_root.mkdir()
    artifact = import_root / "artifact.log"
    body = b"synthetic adapter import\n"
    artifact.write_bytes(body)
    manifest = import_root / "artifact.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "import_contract": "context_shunt.artifact_import.v1",
                "producer": {
                    "id": "synthetic-adapter",
                    "manifest_schema": "context_shunt.artifact_import.v1",
                },
                "artifact": {
                    "path": str(artifact),
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "media_type": "text/plain",
                },
            }
        ),
        encoding="utf-8",
    )
    config = _config(tmp_path)
    config["limits"] = {"max_envelope_bytes": 1024}
    config["artifact_import"] = {
        "enabled": True,
        "roots": [str(import_root)],
        "accepted_manifest_schemas": ["context_shunt.artifact_import.v1"],
    }
    module.register(FakeCtx(config, llm=FakeLlm()))

    delivered = json.loads(
        module.context_shunt_import({"manifest_path": str(manifest)}, task_id="adapter-import")
    )
    assert delivered["status"] == "error"
    assert delivered["code"] == "LIMIT_EXCEEDED"
    assert "pointer" not in delivered
    assert delivered["sources"] == []

    session = module._session(task_id="adapter-import")
    rows = session.store.operation_page(session.identity, page=1, page_size=8)
    record = next(row for row in rows if row.kind == "capture")
    assert record.status == "error"
    assert record.code == "LIMIT_EXCEEDED"
    assert record.baseline_credit_tokens == 0
    assert record.delivery_boundary == "envelope"


# -- the documented fallback chain is actually wired ------------------------


def test_the_hermes_adapter_wires_the_configured_fallback_chain(tmp_path):
    """`reader.fallback_chain` parsed, validated and documented - and did nothing.

    Both adapters built a bare `HostBridgeProvider`, so a deployment that configured an
    availability fallback silently had none: the first unavailable provider ended the
    request. The core's `build_provider` has always assembled the chain; nothing called
    it.
    """
    from context_shunt.provider import FallbackChainProvider

    module = _load_adapter()
    config = _config(tmp_path)
    config["reader"] = {
        "model": "gpt-5.6-luna",
        "fallback_chain": [{"provider": "openai", "model": "gpt-5.6-sol"}],
    }
    module.register(FakeCtx(config, llm=FakeLlm()))
    session = module._session(session_id="s1")
    assert isinstance(session._provider, FallbackChainProvider)


def test_the_hermes_adapter_keeps_the_host_auxiliary_target_as_the_primary(tmp_path):
    """The host's `auxiliary.context_shunt_reader` still wins over the plugin default.

    Routing the adapter through `build_provider` must not quietly drop that precedence,
    which is why the primary target is passed explicitly rather than taken from config.
    """
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
    provider, model = module._reader_target()
    session = module._session(session_id="s2")
    assert session._provider.target.model == model
    assert session._provider.target.provider == provider


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("legacy", [True, False])
def test_hermes_automatic_extract_config_reaches_delivery(tmp_path, enabled, legacy):
    class UnavailableLlm(FakeLlm):
        def complete(self, *args, **kwargs):
            raise RuntimeError("PRIVATE_PROVIDER_BODY")

    module = _load_adapter()
    config = _config(tmp_path)
    config["reader"] = {
        "automatic_extract": enabled,
        "fallback_max_bytes": 64,
        "legacy_compaction": legacy,
    }
    module.register(FakeCtx(config, llm=UnavailableLlm()))
    path = tmp_path / "ws" / "outage.txt"
    path.write_text("source line\n" * 400)
    out = json.loads(
        module.context_shunt_read(question="What is here?", paths=[str(path)], task_id="tauto")
    )
    assert out["code"] == "LEGACY_COMPACTED"
    assert "PRIVATE_PROVIDER_BODY" not in json.dumps(out)
    assert out["legacy_compaction"]["summary_bytes"] <= module._config.limits.max_extraction_bytes
    assert out["provenance"]["derived"] is False


def test_hermes_adapter_delivers_over_old_caps_only_from_trusted_plugin_config(tmp_path):
    source, reply = over_old_reader_caps_fixture()
    path = tmp_path / "ws" / "uncapped.txt"
    config = _config(tmp_path)
    config["reader"] = {"enforce_output_caps": False}
    path.write_bytes(source)
    module = _load_adapter()
    module.register(FakeCtx(config, llm=FakeLlm(reply=reply)))

    delivered = module.context_shunt_read(
        {"question": "List every documented fact.", "paths": [str(path)]},
        task_id="trusted-output-config",
        session_id="trusted-output-config",
    )
    env = json.loads(delivered)

    assert len(delivered.encode("utf-8")) > module._config.limits.max_envelope_bytes
    assert env["status"] == "ok" and env["code"] == "ANSWERED"
    assert len(env["citations"]) == 25
    assert env["answer"].count("Fact ") == 25

    attempted_toggle = json.loads(
        module.context_shunt_read(
            {
                "question": "List every documented fact.",
                "paths": [str(path)],
                "enforce_output_caps": True,
            },
            task_id="caller-toggle",
            session_id="caller-toggle",
        )
    )
    assert attempted_toggle["status"] == "error"
    assert attempted_toggle["failure_detail"] == "TOOL_ARGS_VIOLATION"


def test_openclaw_post_tool_release_gate_cannot_certify_a_retired_seam(monkeypatch):
    from tests.test_gate_bridge_contract import _verify_module

    verify = _verify_module()
    monkeypatch.setattr(verify, "_openclaw_prereq", lambda: (True, ""))
    monkeypatch.setattr(verify, "_vitest", lambda *_: ("pass", 10, ""))
    result = verify.run_integration("openclaw", "post-tool")[0]
    assert result.status == verify.STATUS_EXPECTED_UNSUPPORTED
    assert result.cases == 0
    assert "effective" in result.detail


PROTECTED_RESULTS = [
    "skill_view",
    "skills_list",
    "clarify",
    "todo",
    "context_shunt_read",
    "context_shunt_inspect",
    "context_shunt_import",
    "context_shunt_stats",
    "mcp__x__list_resources",
    "mcp__x__list_prompts",
    "mcp__x__get_prompt",
    "mcp__team__docs__get_prompt",
]
UNKNOWN_RESULTS = [
    "skills_list_extra",
    "context_shunt_read_fake",
    "mcp__x__read_resource_extra",
    "mcp__x__read_resource",
    "skill_view_extra",
    "mcp_skill_view",
    "skill_view.file",
    "mcp_result",
    "write_file",
    "terminal",
    "some_read_tool",
    "",
    None,
]


@pytest.mark.parametrize("tool_name", PROTECTED_RESULTS + UNKNOWN_RESULTS)
def test_classifier_passthrough_has_zero_effects(tmp_path, monkeypatch, tool_name):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    llm = FakeLlm()
    module.register(FakeCtx(config, llm=llm))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("passthrough attempted session/store/provider/accounting work")

    for name in ("_session", "ShuntSession", "SnapshotStore", "build_provider", "_bridge_call"):
        monkeypatch.setattr(module, name, forbidden)
    payload = json.dumps(
        [
            {
                "id": f"item-{i}",
                "status": status,
                "description": "human answer SKILL.md read_resource " * 250,
            }
            for i, status in enumerate(["pending", "in_progress", "completed"])
        ]
    )
    if tool_name == "skills_list":
        payload = json.dumps(
            {
                "skills": [
                    {
                        "name": f"workflow-{i}",
                        "description": "Use for repository maintenance and review.",
                        "path": f"/skills/workflow-{i}/SKILL.md",
                    }
                    for i in range(200)
                ]
            }
        )
    elif tool_name == "clarify":
        payload = "My answer: " + "Please preserve these requirements. " * 600
    assert len(payload.encode()) > 20 * 1024
    replacement = module.transform_tool_result(tool_name=tool_name, result=payload, session_id="p")
    delivered = payload if replacement is None else replacement
    assert delivered == payload
    assert not module._sessions and not module._generations and not llm.calls
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("tool_name", PROTECTED_RESULTS)
def test_protected_classification_wins_over_allowlist(tool_name):
    module = _load_adapter()
    assert module.classify_tool_result(" " + tool_name.upper() + " ", {tool_name}) == "protected"


@pytest.mark.parametrize("tool_name", UNKNOWN_RESULTS)
def test_unknown_classification_is_exact(tool_name):
    module = _load_adapter()
    assert module.classify_tool_result(tool_name) == "passthrough"
    if tool_name:
        assert module.classify_tool_result(tool_name, {tool_name}) == "eligible"


def test_mcp_allowlist_configuration_resets(tmp_path):
    module = _load_adapter()
    config = _config(tmp_path)
    config["capture_tool_allowlist"] = [" MCP__X__READ_RESOURCE "]
    module.register(FakeCtx(config))
    assert module._capture_tool_allowlist == frozenset({"mcp__x__read_resource"})
    module.register(FakeCtx(_config(tmp_path)))
    assert not module._capture_tool_allowlist


@pytest.mark.parametrize("bad", ["read_file", [None], [" "], {"read_file": True}])
def test_capture_allowlist_rejects_non_exact_configuration(tmp_path, bad):
    module = _load_adapter()
    config = _config(tmp_path)
    config["capture_tool_allowlist"] = bad
    with pytest.raises(ValueError, match="exact tool identities"):
        module.register(FakeCtx(config))


def test_allowlisted_mcp_resource_capture_recovery_and_accounting(tmp_path):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    config["capture_tool_allowlist"] = [" MCP__X__READ_RESOURCE "]
    llm = FakeLlm()
    module.register(FakeCtx(config, llm=llm))
    payload = "max_retries = 3\n" + "resource data line\n" * 2000
    pointer = json.loads(
        module.transform_tool_result(
            tool_name="mcp__x__read_resource", result=payload, session_id="resource"
        )
    )
    assert pointer["code"] == "SPILLED"
    assert not llm.calls
    handle = {key: pointer["sources"][0][key] for key in ("source_id", "snapshot_id")}
    inspected = json.loads(
        module.context_shunt_inspect(
            **handle, selector={"kind": "lines", "start": 1, "end": 1}, session_id="resource"
        )
    )
    assert "max_retries = 3" in json.dumps(inspected)
    assert not llm.calls
    answer = json.loads(
        module.context_shunt_read(
            question="What is the retry ceiling?", handles=[handle], session_id="resource"
        )
    )
    assert answer["code"] == "ANSWERED"
    assert llm.calls
    assert answer["sources"][0]["snapshot_id"] == handle["snapshot_id"]
    records = json.loads(module.context_shunt_stats(session_id="resource"))["stats"]["records"]
    assert len(records) == 3
    credits = [row["baseline_credit_tokens"] for row in records]
    assert sum(credit > 0 for credit in credits) == 1
    spill = next(row for row in records if row["kind"] == "spill")
    assert spill["baseline_credit_tokens"] > 0


@pytest.mark.parametrize("tool_name", ["read_file", "mcp__x__read_resource"])
def test_eligible_session_construction_failure_is_bounded(tmp_path, monkeypatch, tool_name):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}
    config["capture_tool_allowlist"] = ["mcp__x__read_resource"]
    module.register(FakeCtx(config))

    def broken(*args, **kwargs):
        raise RuntimeError("capture construction failed")

    monkeypatch.setattr(module, "_session", broken)
    for value in ("small", {"image": "x" * 30_000}):
        assert module.transform_tool_result(tool_name=tool_name, result=value) is None
    out = module.transform_tool_result(tool_name=tool_name, result="x" * 30_000)
    assert json.loads(out)["code"] == "LEGACY_COMPACTED"
    assert json.loads(out)["failure_detail"] == "INTERNAL_ERROR"
    assert json.loads(out)["sources"] == []
    assert len(out.encode()) <= module._config.limits.max_envelope_bytes


def test_store_bootstrap_failure_keeps_capture_fallback_registered(tmp_path, monkeypatch):
    module = _load_adapter()
    config = _config(tmp_path)
    config["tool_result_capture"] = {"enabled": True, "host_ordering_verified_locally": True}

    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_STORE_BOOTSTRAP")

    monkeypatch.setattr(module, "SnapshotStore", broken)
    module.register(FakeCtx(config))
    monkeypatch.setattr(module, "_session", broken)
    out = json.loads(module.transform_tool_result(tool_name="read_file", result="x" * 40000))
    assert out["code"] == "LEGACY_COMPACTED"
    assert "PRIVATE_STORE_BOOTSTRAP" not in str(out)
    unsafe = "aws_secret_access_key=" + "x" * 40000
    out = json.loads(module.transform_tool_result(tool_name="read_file", result=unsafe))
    assert out["code"] == "UNSAFE_SOURCE"
    assert "legacy_compaction" not in out


@pytest.mark.parametrize(
    "tool,args",
    [
        ("context_shunt_read", {"question": "q", "paths": ["/untrusted"], "extra": True}),
        ("context_shunt_stats", {"page": "1"}),
        ("context_shunt_stats", {"extra": 1}),
        ("context_shunt_import", {"manifest_path": "/untrusted", "extra": 1}),
        (
            "context_shunt_inspect",
            {
                "source_id": "src_abcd",
                "snapshot_id": "sha256:" + "a" * 64,
                "selector": {"kind": "lines", "start": 1, "end": 2},
                "max_result_bytes": 0,
            },
        ),
    ],
)
def test_direct_tool_handler_preserves_canonical_caller_errors(tmp_path, tool, args):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path)))
    env = json.loads(getattr(module, tool)(args, task_id="strict"))
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "TOOL_ARGS_VIOLATION"
    assert env["recovery"] == {"handles_valid": True, "actions": ["NONE"]}
    assert "legacy_compaction" not in env


def test_handler_owned_invalid_arguments_are_accounted_exactly_once(tmp_path):
    """A host may invoke the handler directly even though its outer schema is strict."""
    module = _load_adapter()
    ctx = FakeCtx(_config(tmp_path))
    module.register(ctx)

    rejected = json.loads(
        ctx.registered_handlers["context_shunt_stats"](
            {"page": "1"}, task_id="accounted-task", session_id="accounted-session"
        )
    )
    assert rejected["code"] == "INVALID_REQUEST"
    assert rejected["failure_detail"] == "TOOL_ARGS_VIOLATION"
    assert rejected["accounting_id"] != "acc_" + "0" * 16

    stats = json.loads(
        ctx.registered_handlers["context_shunt_stats"](
            {}, task_id="accounted-task", session_id="accounted-session"
        )
    )
    # The stats operation records itself only after composing this response, so the one
    # visible record is exactly the one rejected handler invocation above.
    assert stats["stats"]["total_records"] == 1
    records = stats["stats"]["records"]
    assert [record["operation_id"] for record in records] == [rejected["accounting_id"]]
    assert records[0]["code"] == "INVALID_REQUEST"
    assert records[0]["kind"] == "stats"

    # Hermes' outer JSON-schema validator can reject before invoking this function. That
    # host-owned event cannot appear in plugin accounting; the strict schema makes the
    # ownership boundary testable instead of claiming otherwise.
    stats_schema = next(
        schema["parameters"] for schema, _handler, _mode in module.TOOLS
        if schema["name"] == "context_shunt_stats"
    )
    assert not Draft202012Validator(stats_schema).is_valid({"page": "1"})


@pytest.mark.parametrize("user_task", [None, "HOST_USER_TASK_MUST_NOT_LEAK"])
def test_hermes_registry_metadata_stays_outside_all_public_tool_args(
    tmp_path, monkeypatch, user_task
):
    """Hermes 0.21.2 passes these kwargs beside the model's argument dictionary."""
    module = _load_adapter()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_text("max_retries = 3\n", encoding="utf-8")

    import_root = tmp_path / "imports"
    import_root.mkdir()
    artifact = import_root / "artifact.log"
    body = b"imported adapter artifact\n"
    artifact.write_bytes(body)
    manifest = import_root / "artifact.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "import_contract": "context_shunt.artifact_import.v1",
                "producer": {
                    "id": "synthetic-adapter",
                    "manifest_schema": "context_shunt.artifact_import.v1",
                },
                "artifact": {
                    "path": str(artifact),
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "media_type": "text/plain",
                },
            }
        ),
        encoding="utf-8",
    )
    config = _config(tmp_path)
    config["artifact_import"] = {
        "enabled": True,
        "roots": [str(import_root)],
        "accepted_manifest_schemas": ["context_shunt.artifact_import.v1"],
    }
    llm = FakeLlm()
    ctx = FakeCtx(config, llm=llm)

    read_requests = []
    real_read = module.ShuntSession.read

    def capture_read_request(session, request):
        read_requests.append(request)
        return real_read(session, request)

    monkeypatch.setattr(module.ShuntSession, "read", capture_read_request)
    module.register(ctx)

    def invoke(tool, args):
        # Faithful Hermes registry shape: the model arguments remain positional while
        # _execute_tool supplies trusted runtime metadata as keyword arguments.
        return json.loads(
            ctx.registered_handlers[tool](
                args,
                task_id="registry-task",
                session_id="registry-session",
                user_task=user_task,
            )
        )

    first = invoke(
        "context_shunt_read",
        {"question": "What is the retry ceiling?", "paths": [str(source)]},
    )
    assert first["code"] == "ANSWERED"
    handle = {key: first["sources"][0][key] for key in ("source_id", "snapshot_id")}

    refined = invoke(
        "context_shunt_read",
        {
            "question": "Is this the same snapshot?",
            "handles": [handle],
            "selector": {"kind": "lines", "start": 1, "end": 1},
        },
    )
    assert refined["code"] in ("ANSWERED", "NO_MATCH")
    assert {key: refined["sources"][0][key] for key in handle} == handle

    inspected = invoke(
        "context_shunt_inspect",
        {
            **handle,
            "selector": {"kind": "lines", "start": 1, "end": 1},
        },
    )
    assert inspected["code"] == "EXTRACTED"
    assert inspected["extraction"]["source_id"] == handle["source_id"]
    assert inspected["extraction"]["snapshot_id"] == handle["snapshot_id"]

    searched = invoke(
        "context_shunt_inspect",
        {
            **handle,
            "selector": {
                "kind": "search",
                "needle": "max_retries",
                "max_matches": 1,
                "context_lines": 0,
            },
            "max_result_bytes": 1024,
            "max_scan_lines": 50,
        },
    )
    assert searched["code"] == "EXTRACTED"
    assert searched["extraction"]["matches_found"] == 1

    stats = invoke("context_shunt_stats", {})
    assert stats["code"] == "STATS"
    imported = invoke("context_shunt_import", {"manifest_path": str(manifest)})
    assert imported["code"] == "IMPORTED"

    assert [request["question"] for request in read_requests] == [
        "What is the retry ceiling?",
        "Is this the same snapshot?",
    ]
    private_metadata = "HOST_USER_TASK_MUST_NOT_LEAK"
    assert private_metadata not in json.dumps(read_requests)
    assert private_metadata not in json.dumps(llm.calls)
    assert private_metadata not in json.dumps(ctx.messages)


@pytest.mark.parametrize(
    "metadata", ["task_id", "session_id", "tool_call_id", "turn_id", "user_task"]
)
def test_caller_cannot_impersonate_trusted_hermes_metadata(tmp_path, metadata):
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path)))
    env = json.loads(
        module.context_shunt_stats(
            {metadata: "caller-controlled"},
            task_id="trusted-task",
            session_id="trusted-session",
            user_task=None,
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "TOOL_ARGS_VIOLATION"
