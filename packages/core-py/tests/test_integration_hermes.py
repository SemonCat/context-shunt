"""integration hermes --mode local: the real host, or nothing.

This drives Hermes' own ``handle_function_call`` through the real plugin hook registry, so
what it proves is that a blocked read is blocked *by the host* before the tool executes -
not that our mock behaves. It needs a hermes-agent checkout and an interpreter that can
import it:

    CONTEXT_SHUNT_HERMES_ROOT=/path/to/hermes-agent \
    CONTEXT_SHUNT_HERMES_PYTHON=/path/to/venv/bin/python \
    scripts/verify integration hermes --mode local

Without those, ``scripts/verify`` reports NOT_RUN (exit 2). It is never a pass, and the
deterministic adapter-contract gate (``unit capability``) is not a substitute for it.

The post-tool mode has no test here on purpose: Hermes' ``transform_tool_result`` receives
a result that is already truncated and is wrapped in try/except by the host, so complete
capture before truncation cannot be shown. See docs/capability-matrix.md.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ADAPTER_DIR = REPO / "adapters" / "hermes" / "context-shunt"
ROOT_ENV = "CONTEXT_SHUNT_HERMES_ROOT"
PYTHON_ENV = "CONTEXT_SHUNT_HERMES_PYTHON"


def _prereq() -> tuple[Path, Path]:
    root = os.environ.get(ROOT_ENV, "")
    interpreter = os.environ.get(PYTHON_ENV, "")
    if not root or not Path(root).is_dir():
        pytest.fail(f"{ROOT_ENV} must point at a hermes-agent checkout")
    if not interpreter or not Path(interpreter).exists():
        pytest.fail(f"{PYTHON_ENV} must point at an interpreter with hermes-agent installed")
    return Path(root), Path(interpreter)


pytestmark = [
    pytest.mark.integration_hermes,
    pytest.mark.skipif(
        not (os.environ.get(ROOT_ENV) and os.environ.get(PYTHON_ENV)),
        reason=f"set {ROOT_ENV} and {PYTHON_ENV}; scripts/verify reports NOT_RUN otherwise",
    ),
]

# Runs inside the host interpreter so the hook registry, the tool dispatcher and the
# plugin context are all the host's own. Output is a single JSON line.
HOST_SCRIPT = r"""
import importlib.util, json, os, pathlib, sys, tempfile, re
from types import SimpleNamespace

sys.path.insert(0, os.environ["HERMES_ROOT"])
sys.path.insert(0, os.environ["SHUNT_CORE"])
sys.path.insert(0, os.environ["SHUNT_ADAPTER"])

out = {"host_import": False, "hook_names": [], "registered_hooks": [],
       "registered_tools": [], "tool_invocations": 0, "errors": [], "model_calls": []}

try:
    from hermes_cli import plugins as host_plugins
    from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager
    from agent.plugin_llm import _TrustPolicy, make_plugin_llm_for_test
    from tools.registry import registry
    import model_tools
    out["host_import"] = True
    out["hook_names"] = sorted(host_plugins.VALID_HOOKS)
except Exception as exc:
    out["errors"].append(f"host import failed: {type(exc).__name__}")
    print(json.dumps(out)); sys.exit(0)

spec = importlib.util.spec_from_file_location(
    "context_shunt_hermes", os.path.join(os.environ["SHUNT_ADAPTER"], "__init__.py")
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

temp = pathlib.Path(tempfile.mkdtemp(prefix="context-shunt-hermes-host-"))
workspace = temp / "workspace"
workspace.mkdir()
big = workspace / "big.txt"
big.write_text("".join("line %d\n" % i for i in range(400)))
small = workspace / "small.txt"
small.write_text("max_retries = 3\n")

# Exercise the adapter's real config lookup without touching the user's Hermes home.
hermes_home = temp / "hermes-home"
hermes_home.mkdir()
(hermes_home / "config.yaml").write_text(json.dumps({
    "plugins": {"entries": {"context-shunt": {
        "config": {"workspace_roots": [str(workspace)],
                   "spill_dir": str(temp / "spill"),
                   "suma_post_tool": {"enabled": False}},
        "llm": {"allow_model_override": True,
                "allowed_models": ["gpt-5.6-luna"]}
    }}}
}))
os.environ["HERMES_HOME"] = str(hermes_home)

manager = PluginManager()
host_plugins._plugin_manager = manager
manifest = PluginManifest(name="context-shunt", key="context-shunt", source="test")
ctx = PluginContext(manifest, manager)

def fake_response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, role="assistant"),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8, total_tokens=20),
    )

def fake_caller(**kwargs):
    out["model_calls"].append({
        "model": kwargs.get("model_override"),
        "provider": kwargs.get("provider_override"),
        "messages": kwargs.get("messages"),
    })
    answer = json.dumps({
        "answer": "The retry ceiling is three [c1].",
        "citations": [{"id": "c1", "line_start": 1, "line_end": 1,
                       "quote": "max_retries = 3"}],
    })
    return "openai", "gpt-5.6-luna", fake_response(answer)

ctx._llm = make_plugin_llm_for_test(
    plugin_id="context-shunt",
    policy=_TrustPolicy(plugin_id="context-shunt", allow_model_override=True,
                        allowed_models=frozenset({"gpt-5.6-luna"})),
    sync_caller=fake_caller,
)

try:
    adapter.register(ctx)
    out["registered_hooks"] = sorted(manager._hooks)
    out["registered_tools"] = sorted(manager._plugin_tool_names)
    out["has_hook"] = bool(host_plugins.has_hook("pre_tool_call"))
except Exception as exc:
    out["errors"].append(f"register failed: {type(exc).__name__}")
    print(json.dumps(out)); sys.exit(0)

# Drive Hermes' real dispatcher. The wrapper counts only actual read_file dispatches;
# a pre-hook veto must return before it reaches this function.
real_dispatch = registry.dispatch
def counting_dispatch(name, args, **kwargs):
    if name == "read_file":
        out["tool_invocations"] += 1
        return json.dumps({"executed": True})
    return real_dispatch(name, args, **kwargs)
registry.dispatch = counting_dispatch

try:
    out["blocked_result"] = model_tools.handle_function_call(
        "read_file", {"path": str(big)}, task_id="task-local", session_id="session-local",
        tool_call_id="tc-blocked"
    )
    out["invocations_after_block"] = out["tool_invocations"]
    out["allowed_result"] = model_tools.handle_function_call(
        "read_file", {"path": str(small)}, task_id="task-local", session_id="session-local",
        tool_call_id="tc-allowed"
    )
    out["invocations_after_allow"] = out["tool_invocations"]
    out["reader_result"] = model_tools.handle_function_call(
        "context_shunt_read",
        {"question": "What is the retry ceiling?", "paths": [str(small)]},
        task_id="task-local", session_id="session-local", tool_call_id="tc-reader"
    )
    out["capability"] = adapter.capability_report()
except Exception as exc:
    out["errors"].append(f"host dispatch failed: {type(exc).__name__}")

print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def host_result() -> dict:
    root, interpreter = _prereq()
    env = {
        **os.environ,
        "HERMES_ROOT": str(root),
        "SHUNT_CORE": str(REPO / "packages" / "core-py" / "src"),
        "SHUNT_ADAPTER": str(ADAPTER_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = subprocess.run(
        [str(interpreter), "-c", HOST_SCRIPT], capture_output=True, text=True, env=env, timeout=300
    )
    if proc.returncode != 0:
        pytest.fail(f"host harness exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def test_host_module_imports_and_exposes_pre_tool_call(host_result):
    assert host_result["host_import"], host_result["errors"]
    assert "pre_tool_call" in host_result["hook_names"]
    assert host_result.get("has_hook") is True, "the host registry did not accept the hook"


def test_adapter_registers_the_pre_execution_gate_and_no_writer(host_result):
    assert "pre_tool_call" in host_result["registered_hooks"], host_result["errors"]
    tools = host_result["registered_tools"]
    assert tools == ["context_shunt_read"]
    assert not any("writ" in name or "patch" in name for name in tools)


def test_real_host_dispatch_blocks_an_oversized_full_read(host_result):
    """The host returns the block envelope without invoking its tool dispatcher."""
    outer = json.loads(host_result["blocked_result"])
    envelope = json.loads(outer["error"])
    assert envelope["status"] == "blocked" and envelope["code"] == "LARGE_READ"
    assert envelope["coverage"]["complete"] is False
    assert host_result["invocations_after_block"] == 0


def test_real_host_dispatch_allows_a_small_read(host_result):
    assert json.loads(host_result["allowed_result"])["executed"] is True
    assert host_result["invocations_after_allow"] == 1


def test_real_host_reader_requests_luna_and_the_original_question(host_result):
    envelope = json.loads(host_result["reader_result"])
    assert envelope["code"] == "ANSWERED"
    assert envelope["citations"][0]["verified"] is True
    assert len(host_result["model_calls"]) == 1
    call = host_result["model_calls"][0]
    assert call["model"] == "gpt-5.6-luna"
    assert "What is the retry ceiling?" in call["messages"][1]["content"]


def test_capability_report_names_the_real_host_version(host_result):
    capability = host_result.get("capability") or {}
    assert capability.get("host", {}).get("name") == "hermes-agent"
    assert capability.get("host", {}).get("version") == "0.18.2"
    assert capability.get("reader_model") == "gpt-5.6-luna"
    suma = next(m for m in capability["modes"] if m["mode"] == "suma_post_tool")
    assert suma["enabled"] is False
