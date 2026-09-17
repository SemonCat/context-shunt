#!/usr/bin/env python3
"""Exact-host probe for no-core-patch invocation-scoped consumer delivery.

This script intentionally uses only the Python standard library plus the selected Hermes
runtime and this repository. It is designed for an isolated copy/container of a specific
host version; it never contacts a provider and prints only bounded synthetic metadata.

Examples (environment paths are required)::

    HERMES_ROOT=/opt/hermes SHUNT_CORE=... SHUNT_ADAPTER=... \
      python probe.py --expect no-observation
    HERMES_ROOT=/opt/hermes SHUNT_CORE=... SHUNT_ADAPTER=... \
      python probe.py --expect ready

``no-observation`` is the red-capable control: the exact same unmodified host dispatches
without a correlated provider-request observation and must return no handle. ``ready``
uses Hermes' official post-middleware ``pre_api_request`` hook and unmodified tool-search
assembly to prove direct and deferred scopes end to end.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from typing import Any


EXPECTED_HERMES_VERSION = "0.21.3"
EXPECTED_MODEL_TOOLS_SHA256 = {
    "no-observation": "c99620c824ab59f341ac7d0e22cde016b0c469d0643e7a5a5a82e0d63176e4b5",
    "ready": "c99620c824ab59f341ac7d0e22cde016b0c469d0643e7a5a5a82e0d63176e4b5",
}


def _envelope(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise AssertionError("host result is not an envelope object")
    return parsed


def _handle(pointer: dict[str, Any]) -> dict[str, str]:
    value = pointer.get("pointer")
    if not isinstance(value, dict):
        raise AssertionError("SPILLED envelope has no pointer")
    return {key: str(value[key]) for key in ("source_id", "snapshot_id")}


def _call(model_tools: Any, name: str, args: dict[str, Any], *, session: str,
          tools: list[str] | None, toolsets: list[str], call_id: str,
          pre_tool_checked: bool = False) -> dict[str, Any]:
    return _envelope(
        model_tools.handle_function_call(
            name,
            args,
            task_id=session,
            session_id=session,
            tool_call_id=call_id,
            turn_id=f"turn-{call_id}",
            api_request_id=f"api-{call_id}",
            enabled_tools=None if tools is None else list(tools),
            enabled_toolsets=list(toolsets),
            disabled_toolsets=[],
            skip_pre_tool_call_hook=pre_tool_checked,
        )
    )


def _observe(tools: list[dict[str, Any]], *, session: str, call_id: str) -> None:
    """Run Hermes' real provider-request serializer and lifecycle dispatch."""
    from agent.api_request_hooks import ApiRequestHooksMixin
    from agent.turn_api_request import _fire_pre_api_request_hook

    class ProbeAgent(ApiRequestHooksMixin):
        pass

    agent = ProbeAgent()
    agent.session_id = session
    agent.platform = "cli"
    agent.model = "synthetic-public-probe"
    agent.provider = "offline"
    agent.base_url = ""
    agent.api_mode = "chat_completions"
    agent.max_tokens = 1024
    agent.tools = tools
    _fire_pre_api_request_hook(
        agent,
        {"messages": [], "tools": tools},
        [],
        [],
        messages=[],
        original_user_message="synthetic public exact-host probe",
        approx_tokens=0,
        total_chars=0,
        retry_count=0,
        api_call_count=1,
        api_request_id=f"api-{call_id}",
        api_start_time=0.0,
        effective_task_id=session,
        turn_id=f"turn-{call_id}",
    )


def _assert_accounted(stats: dict[str, Any], envelope: dict[str, Any]) -> None:
    records = stats.get("stats", {}).get("records", [])
    accounting_id = envelope.get("accounting_id")
    if not accounting_id or not any(
        row.get("operation_id") == accounting_id for row in records if isinstance(row, dict)
    ):
        observed = [
            row.get("operation_id") for row in records if isinstance(row, dict)
        ]
        raise AssertionError(
            "published envelope has no correlated accounting record: "
            f"wanted={accounting_id!r}, observed={observed!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=("no-observation", "ready"), required=True)
    args = parser.parse_args()

    for key in ("HERMES_ROOT", "SHUNT_CORE", "SHUNT_ADAPTER"):
        value = os.environ.get(key, "")
        if not value or not Path(value).is_dir():
            raise SystemExit(f"{key} must name an existing directory")
        sys.path.insert(0, value)

    from agent.plugin_llm import _TrustPolicy, make_plugin_llm_for_test
    from hermes_cli import plugins as host_plugins
    from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager
    import model_tools

    host_version = importlib.metadata.version("hermes-agent")
    if host_version != EXPECTED_HERMES_VERSION:
        raise AssertionError(
            f"probe targets Hermes {EXPECTED_HERMES_VERSION}, found {host_version}"
        )
    model_tools_sha256 = hashlib.sha256(Path(model_tools.__file__).read_bytes()).hexdigest()
    if model_tools_sha256 != EXPECTED_MODEL_TOOLS_SHA256[args.expect]:
        raise AssertionError(
            "model_tools.py does not match the exact inspected control/proposal source"
        )

    temp = Path(tempfile.mkdtemp(prefix="context-shunt-hermes-contract-"))
    workspace = temp / "workspace"
    workspace.mkdir()
    cache = temp / "cache"
    hermes_home = temp / "hermes-home"
    hermes_home.mkdir()

    marker = "PUBLIC_PROBE_MARKER_4fbc0f8a"
    payload = "".join(
        f"synthetic public row {index:05d} value={index * 17}\n"
        for index in range(5000)
    )
    payload = payload[: len(payload) // 2] + marker + "\n" + payload[len(payload) // 2 :]
    (hermes_home / "config.yaml").write_text(
        json.dumps(
            {
                "plugins": {
                    "entries": {
                        "context-shunt": {
                            "config": {
                                "workspace_roots": [str(workspace)],
                                "cache_dir": str(cache),
                                "capture_tool_allowlist": ["probe_source", "terminal"],
                                "tool_result_capture": {
                                    "enabled": True,
                                    "host_ordering_verified_locally": True,
                                    "host_consumer_scope_verified_locally": True,
                                },
                            },
                            "llm": {
                                "allow_model_override": True,
                                "allowed_models": ["gpt-5.6-luna"],
                            },
                        }
                    }
                },
                "auxiliary": {
                    "context_shunt_reader": {
                        "provider": "auto",
                        "model": "auto",
                    }
                },
                "toolsets": {"enabled": ["context_shunt"]},
            }
        ),
        encoding="utf-8",
    )
    os.environ["HERMES_HOME"] = str(hermes_home)

    spec = importlib.util.spec_from_file_location(
        "context_shunt_hermes_probe",
        str(Path(os.environ["SHUNT_ADAPTER"]) / "__init__.py"),
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load owned Hermes adapter")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)

    manager = PluginManager()
    host_plugins._plugin_manager = manager
    ctx = PluginContext(
        PluginManifest(name="context-shunt", key="context-shunt", source="probe"), manager
    )

    def fake_response(text: str) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text, role="assistant"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=19, completion_tokens=11, total_tokens=30),
        )

    def fake_caller(**_kwargs: Any) -> tuple[str, str, SimpleNamespace]:
        answer = json.dumps(
            {
                "answer": "The first row has value zero [c1].",
                "citations": [
                    {
                        "id": "c1",
                        "line_start": 1,
                        "line_end": 1,
                        "quote": "synthetic public row 00000 value=0",
                    }
                ],
            }
        )
        return "openai", "gpt-5.6-luna", fake_response(answer)

    ctx._llm = make_plugin_llm_for_test(
        plugin_id="context-shunt",
        policy=_TrustPolicy(
            plugin_id="context-shunt",
            allow_model_override=True,
            allowed_models=frozenset({"gpt-5.6-luna"}),
        ),
        sync_caller=fake_caller,
    )

    ctx.register_tool(
        "probe_source",
        "context_shunt",
        {
            "name": "probe_source",
            "description": "Return a bounded synthetic public host-probe payload.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        lambda _args, **_kwargs: payload,
        description="Return a bounded synthetic public host-probe payload.",
    )
    adapter.register(ctx)

    if host_plugins.has_hook("transform_tool_result"):
        raise AssertionError("current host should use middleware, not the bounded transform hook")
    if not host_plugins.has_hook("pre_api_request") or not host_plugins.has_middleware("tool_execution"):
        raise AssertionError("official observer/execution middleware route was not registered")

    direct_tools = ["probe_source", "context_shunt_read", "context_shunt_inspect", "context_shunt_stats"]
    bridge_tools = ["tool_search", "tool_describe", "tool_call"]

    raw_capable_defs = model_tools.get_tool_definitions(
        enabled_toolsets=["context_shunt"], disabled_toolsets=[], quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    from tools.tool_search import ToolSearchConfig, assemble_tool_defs
    deferred_defs = assemble_tool_defs(
        raw_capable_defs,
        context_length=128_000,
        config=ToolSearchConfig(
            enabled="on", threshold_pct=5.0, search_default_limit=5,
            max_search_limit=25, listing="on", listing_max_tokens=4000,
        ),
    ).tool_defs
    terminal_defs = model_tools.get_tool_definitions(
        enabled_toolsets=["terminal"], disabled_toolsets=[], quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    partial_defs = [
        definition for definition in raw_capable_defs
        if definition.get("function", {}).get("name") != "context_shunt_inspect"
    ]
    from tools.delegate_tool_toolsets import _resolve_child_toolsets
    child_toolsets, child_disabled = _resolve_child_toolsets(
        SimpleNamespace(
            enabled_toolsets=["terminal", "context_shunt"],
            disabled_toolsets=[],
        ),
        ["terminal"],
        "worker",
    )
    if child_toolsets != ["terminal"] or "kanban" not in child_disabled:
        raise AssertionError("official subagent toolset resolver did not preserve narrowing")

    try:
        if args.expect == "ready":
            _observe(raw_capable_defs, session="direct-probe", call_id="capture-direct")
        direct = _call(
            model_tools,
            "probe_source",
            {},
            session="direct-probe",
            tools=direct_tools,
            toolsets=["context_shunt"],
            call_id="capture-direct",
        )
        if args.expect == "ready":
            _observe(deferred_defs, session="deferred-probe", call_id="capture-deferred")
        deferred = _call(
            model_tools,
            "tool_call",
            {"calls": [{"name": "probe_source", "arguments": {}}]},
            session="deferred-probe",
            tools=bridge_tools,
            toolsets=["context_shunt"],
            call_id="capture-deferred",
        )
        if args.expect == "ready":
            _observe(terminal_defs, session="terminal-probe", call_id="capture-terminal")
        terminal = _call(
            model_tools,
            "terminal",
            {"command": "python -c \"print('synthetic terminal row\\n' * 3000)\""},
            session="terminal-probe",
            tools=["terminal"],
            toolsets=["terminal"],
            call_id="capture-terminal",
        )
        if args.expect == "ready":
            _observe(partial_defs, session="partial-direct-probe", call_id="capture-partial-direct")
        partial_direct = _call(
            model_tools,
            "probe_source",
            {},
            session="partial-direct-probe",
            tools=["search_files", "context_shunt_read"],
            toolsets=["context_shunt"],
            call_id="capture-partial-direct",
        )
        unscoped = _call(
            model_tools,
            "probe_source",
            {},
            session="unscoped-probe",
            tools=None,
            toolsets=["context_shunt"],
            call_id="capture-unscoped",
        )

        for bounded in (direct, deferred, terminal, partial_direct, unscoped):
            encoded = json.dumps(bounded, ensure_ascii=False)
            if payload in encoded:
                raise AssertionError("raw payload leaked in an envelope")

        for restricted in (terminal, partial_direct, unscoped):
            if restricted.get("code") != "LEGACY_COMPACTED":
                raise AssertionError("restricted caller received a pointer")
            if restricted.get("pointer") or restricted.get("sources"):
                raise AssertionError("restricted fallback retained a consumer-inaccessible handle")
            if restricted.get("failure_detail") != "CONSUMER_UNAVAILABLE":
                raise AssertionError("restricted fallback did not explain consumer unavailability")

        if args.expect == "no-observation":
            if direct.get("code") != "LEGACY_COMPACTED" or deferred.get("code") != "LEGACY_COMPACTED":
                raise AssertionError("control host unexpectedly delivered a consumer descriptor")
            direct_stats = _call(
                model_tools,
                "context_shunt_stats",
                {},
                session="direct-probe",
                tools=direct_tools,
                toolsets=["context_shunt"],
                call_id="stats-direct-control",
            )
            _assert_accounted(direct_stats, direct)
            result = {
                "status": "EXPECTED_NO_OBSERVATION",
                "host_version": host_version,
                "model_tools_sha256": model_tools_sha256,
                "direct": direct.get("code"),
                "deferred": deferred.get("code"),
                "terminal_only": terminal.get("code"),
                "partial_direct": partial_direct.get("code"),
                "unscoped_direct": unscoped.get("code"),
                "payload_bytes": len(payload.encode()),
                "raw_payload_published": False,
                "provider_calls": 0,
            }
            print(json.dumps(result, sort_keys=True))
            return 0

        if direct.get("code") != "SPILLED" or deferred.get("code") != "SPILLED":
            raise AssertionError("official observer did not prove direct and deferred consumer scope")

        _observe(raw_capable_defs, session="concurrent-capable", call_id="concurrent-a")
        _observe(terminal_defs, session="concurrent-restricted", call_id="concurrent-b")
        with ThreadPoolExecutor(max_workers=2) as executor:
            capable_future = executor.submit(
                _call, model_tools, "probe_source", {}, session="concurrent-capable",
                tools=direct_tools, toolsets=["context_shunt"], call_id="concurrent-a",
                pre_tool_checked=True,
            )
            restricted_future = executor.submit(
                _call, model_tools, "terminal",
                {"command": "python -c \"print('concurrent restricted row\\n' * 3000)\""},
                session="concurrent-restricted", tools=["terminal"],
                toolsets=child_toolsets, call_id="concurrent-b",
                pre_tool_checked=True,
            )
            concurrent_capable = capable_future.result()
            concurrent_restricted = restricted_future.result()
        if concurrent_capable.get("code") != "SPILLED":
            raise AssertionError("concurrent capable scope lost its consumer evidence")
        if concurrent_restricted.get("code") != "LEGACY_COMPACTED":
            raise AssertionError("concurrent restricted scope inherited another request's capability")

        direct_handle = _handle(direct)
        direct_read = _call(
            model_tools,
            "context_shunt_read",
            {
                "question": "What is the value in the first row?",
                # Exercise the adapter's bounded JSON repair for a commonly stringified handle.
                "handles": json.dumps([direct_handle]),
            },
            session="direct-probe",
            tools=direct_tools,
            toolsets=["context_shunt"],
            call_id="read-direct",
        )
        if direct_read.get("code") != "ANSWERED" or not direct_read.get("citations"):
            raise AssertionError("same-session direct pointer was not readable")

        host_plugins.invoke_hook(
            "on_session_end", session_id="direct-probe", task_id="direct-probe", completed=True
        )
        direct_inspect = _call(
            model_tools,
            "context_shunt_inspect",
            {
                **direct_handle,
                "selector": {"kind": "search", "needle": marker, "max_matches": 1},
                "max_result_bytes": 4096,
            },
            session="direct-probe",
            tools=direct_tools,
            toolsets=["context_shunt"],
            call_id="inspect-direct",
        )
        if direct_inspect.get("code") != "EXTRACTED" or marker not in json.dumps(direct_inspect):
            raise AssertionError(
                "pointer did not survive the per-turn boundary for exact inspect: "
                f"code={direct_inspect.get('code')!r}, detail={direct_inspect.get('failure_detail')!r}"
            )

        deferred_handle = _handle(deferred)
        deferred_inspect = _call(
            model_tools,
            "tool_call",
            {
                "calls": [{
                    "name": "context_shunt_inspect",
                    "arguments": {
                        **deferred_handle,
                        "selector": {"kind": "lines", "start": 1, "end": 1},
                        "max_result_bytes": 4096,
                    },
                }],
            },
            session="deferred-probe",
            tools=bridge_tools,
            toolsets=["context_shunt"],
            call_id="inspect-deferred",
        )
        if deferred_inspect.get("code") != "EXTRACTED":
            raise AssertionError("scoped tool_call could not consume the deferred pointer")

        direct_stats = _call(
            model_tools,
            "context_shunt_stats",
            {},
            session="direct-probe",
            tools=direct_tools,
            toolsets=["context_shunt"],
            call_id="stats-direct",
        )
        _assert_accounted(direct_stats, direct)
        _assert_accounted(direct_stats, direct_read)
        _assert_accounted(direct_stats, direct_inspect)

        host_plugins.invoke_hook(
            "on_session_finalize",
            session_id="direct-probe",
            task_id="direct-probe",
            platform="cli",
            reason="probe-boundary",
        )
        expired = _call(
            model_tools,
            "context_shunt_inspect",
            {
                **direct_handle,
                "selector": {"kind": "lines", "start": 1, "end": 1},
            },
            session="direct-probe",
            tools=direct_tools,
            toolsets=["context_shunt"],
            call_id="inspect-expired",
        )
        if expired.get("code") != "SOURCE_EXPIRED":
            raise AssertionError("finalize did not revoke the captured pointer")

        result = {
            "status": "PASS",
            "host_version": host_version,
            "model_tools_sha256": model_tools_sha256,
            "direct": direct.get("code"),
            "deferred": deferred.get("code"),
            "terminal_only": terminal.get("code"),
            "partial_direct": partial_direct.get("code"),
            "unscoped_direct": unscoped.get("code"),
            "direct_read": direct_read.get("code"),
            "direct_inspect_after_turn": direct_inspect.get("code"),
            "deferred_inspect_via_tool_call": deferred_inspect.get("code"),
            "after_finalize": expired.get("code"),
            "payload_bytes": len(payload.encode()),
            "raw_payload_published": False,
            "accounting_correlated": True,
            "request_scope_immutable": True,
            "host_core_modified": False,
            "concurrent_capable": concurrent_capable.get("code"),
            "concurrent_restricted": concurrent_restricted.get("code"),
            "subagent_toolsets_narrowed": True,
            "provider_calls": 0,
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        for session in (
            "direct-probe",
            "deferred-probe",
            "terminal-probe",
            "partial-direct-probe",
            "unscoped-probe",
            "concurrent-capable",
            "concurrent-restricted",
        ):
            try:
                host_plugins.invoke_hook(
                    "on_session_finalize",
                    session_id=session,
                    task_id=session,
                    platform="cli",
                    reason="probe-cleanup",
                )
            except Exception:
                pass
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
