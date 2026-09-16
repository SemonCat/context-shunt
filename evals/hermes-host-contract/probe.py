#!/usr/bin/env python3
"""Exact-host probe for invocation-scoped post-tool consumer delivery.

This script intentionally uses only the Python standard library plus the selected Hermes
runtime and this repository. It is designed for an isolated copy/container of a specific
host version; it never contacts a provider and prints only bounded synthetic metadata.

Examples (environment paths are required)::

    HERMES_ROOT=/opt/hermes SHUNT_CORE=... SHUNT_ADAPTER=... \
      python probe.py --expect missing-seam
    HERMES_ROOT=/opt/hermes SHUNT_CORE=... SHUNT_ADAPTER=... \
      python probe.py --expect ready

``missing-seam`` is the red-capable control for an unmodified 0.21.3 host. ``ready`` is
the acceptance expectation after applying the source-located proposal in
``docs/host-proposals/hermes-0.21.3-consumer-capabilities.patch``.
"""

from __future__ import annotations

import argparse
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
    "missing-seam": "c99620c824ab59f341ac7d0e22cde016b0c469d0643e7a5a5a82e0d63176e4b5",
    "ready": "738e5ede949a93ab7632da544f17422780fac7ecd7ea5a4749158a9de3519897",
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
          tools: list[str] | None, toolsets: list[str], call_id: str) -> dict[str, Any]:
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
        )
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
    parser.add_argument("--expect", choices=("missing-seam", "ready"), required=True)
    args = parser.parse_args()

    for key in ("HERMES_ROOT", "SHUNT_CORE", "SHUNT_ADAPTER"):
        value = os.environ.get(key, "")
        if not value or not Path(value).is_dir():
            raise SystemExit(f"{key} must name an existing directory")
        sys.path.insert(0, value)

    from agent.plugin_llm import _TrustPolicy, make_plugin_llm_for_test
    from hermes_cli import plugins as host_plugins
    from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager
    from tools.registry import registry
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
    adapter.register(ctx)

    if not host_plugins.has_hook("transform_tool_result"):
        raise AssertionError("capture hook was not registered under both explicit attestations")

    real_dispatch = registry.dispatch

    def synthetic_dispatch(name: str, call_args: dict[str, Any], **kwargs: Any) -> str:
        if name == "search_files":
            return payload
        return real_dispatch(name, call_args, **kwargs)

    registry.dispatch = synthetic_dispatch

    direct_tools = [
        "search_files",
        "context_shunt_read",
        "context_shunt_inspect",
        "context_shunt_stats",
    ]
    bridge_tools = ["tool_search", "tool_describe", "tool_call"]

    try:
        direct = _call(
            model_tools,
            "search_files",
            {"query": "synthetic"},
            session="direct-probe",
            tools=direct_tools,
            toolsets=["context_shunt"],
            call_id="capture-direct",
        )
        deferred = _call(
            model_tools,
            "search_files",
            {"query": "synthetic"},
            session="deferred-probe",
            tools=bridge_tools,
            toolsets=["context_shunt"],
            call_id="capture-deferred",
        )
        terminal = _call(
            model_tools,
            "search_files",
            {"query": "synthetic"},
            session="terminal-probe",
            tools=["terminal"],
            toolsets=["terminal"],
            call_id="capture-terminal",
        )
        partial_direct = _call(
            model_tools,
            "search_files",
            {"query": "synthetic"},
            session="partial-direct-probe",
            tools=["search_files", "context_shunt_read"],
            toolsets=["context_shunt"],
            call_id="capture-partial-direct",
        )
        unscoped = _call(
            model_tools,
            "search_files",
            {"query": "synthetic"},
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

        if args.expect == "missing-seam":
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
                "status": "EXPECTED_MISSING_SEAM",
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

        descriptor = model_tools._invocation_consumer_capabilities(
            direct_tools, ["context_shunt"], []
        )
        if not isinstance(descriptor["direct_tools"], tuple):
            raise AssertionError("host descriptor values are not immutable tuples")
        try:
            descriptor["direct_tools"] = ("terminal",)
        except TypeError:
            pass
        else:
            raise AssertionError("host descriptor mapping is mutable")

        if direct.get("code") != "SPILLED" or deferred.get("code") != "SPILLED":
            raise AssertionError("ready host did not deliver direct and deferred consumer descriptors")

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
                "name": "context_shunt_inspect",
                "arguments": {
                    **deferred_handle,
                    "selector": {"kind": "lines", "start": 1, "end": 1},
                    "max_result_bytes": 4096,
                },
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
            "descriptor_immutable": True,
            "provider_calls": 0,
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        registry.dispatch = real_dispatch
        for session in (
            "direct-probe",
            "deferred-probe",
            "terminal-probe",
            "partial-direct-probe",
            "unscoped-probe",
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
