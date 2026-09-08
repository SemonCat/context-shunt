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
                "on_session_finalize",
                "on_session_reset",
            ]
        )
        self.registered_hooks: list[str] = []
        self.auxiliary_tasks: list[tuple[str, dict]] = []
        self.registered_tools: list[str] = []
        self.registered_toolsets: list[str] = []
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


def test_hermes_attribution_is_unverified_and_never_claims_actual(tmp_path):
    """The facade cannot separate a provider report from an echo, so we do not pretend."""
    module = _load_adapter()
    module.register(FakeCtx(_config(tmp_path), llm=FakeLlm()))
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
    reader = module._capability.mode("reader")
    assert any("never claims actual" in line for line in reader.evidence)


def test_tool_schemas_declare_no_write_surface_and_no_full_retrieval():
    module = _load_adapter()
    assert sorted(module.READER_TOOL_SCHEMA["parameters"]["properties"]) == [
        "handles",
        "paths",
        "question",
    ]
    assert sorted(module.INSPECT_TOOL_SCHEMA["parameters"]["properties"]) == [
        "cursor",
        "selector",
        "snapshot_id",
        "source_id",
    ]
    assert sorted(module.STATS_TOOL_SCHEMA["parameters"]["properties"]) == ["page", "page_size"]
    # No tool may name a mutating or full-retrieval capability in its own surface.
    names = [schema["name"] for schema, _h, _m in module.TOOLS]
    parameters = json.dumps([schema["parameters"] for schema, _h, _m in module.TOOLS]).lower()
    for forbidden in ("write", "patch", "apply", "content", "full", "all", "raw", "payload"):
        assert not any(forbidden in name for name in names)
        assert forbidden not in parameters


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
