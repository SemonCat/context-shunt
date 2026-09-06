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
import json, os, sys, tempfile, pathlib

sys.path.insert(0, os.environ["HERMES_ROOT"])
sys.path.insert(0, os.environ["SHUNT_CORE"])
sys.path.insert(0, os.environ["SHUNT_ADAPTER"])

out = {"host_import": False, "hook_names": [], "registered": [], "blocked": None,
       "allowed": None, "tool_invocations": 0, "errors": []}

try:
    from hermes_cli import plugins as host_plugins
    out["host_import"] = True
    out["hook_names"] = sorted(getattr(host_plugins, "VALID_HOOKS", []))
except Exception as exc:
    out["errors"].append(f"hermes_cli.plugins import failed: {type(exc).__name__}")
    print(json.dumps(out)); sys.exit(0)

import importlib.util
spec = importlib.util.spec_from_file_location(
    "context_shunt_hermes", os.path.join(os.environ["SHUNT_ADAPTER"], "__init__.py")
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

workspace = pathlib.Path(tempfile.mkdtemp())
(workspace / "ws").mkdir()
big = workspace / "ws" / "big.txt"
big.write_text("".join("line %d\n" % i for i in range(400)))
small = workspace / "ws" / "small.txt"
small.write_text("max_retries = 3\n")


class HostCtx:
    # Uses the host's own registration entry points where they exist.

    def __init__(self):
        self.plugin_config = {
            "workspace_roots": [str(workspace / "ws")],
            "spill_dir": str(workspace / "cache"),
        }
        self.llm = None
        self.registered = []

    def register_hook(self, name, handler):
        # Register into the host's own hook registry so the directive query below goes
        # through the host's dispatch path, not ours.
        self.registered.append(name)
        manager = host_plugins.get_plugin_manager()
        manager._hooks.setdefault(name, []).append(handler)

    def register_tool(self, schema, handler):
        self.registered.append("tool:" + schema["name"])


ctx = HostCtx()
try:
    adapter.register(ctx)
    out["registered"] = list(ctx.registered)
except Exception as exc:
    out["errors"].append(f"register failed: {type(exc).__name__}")
    print(json.dumps(out)); sys.exit(0)

# Ask the host itself whether its pre_tool_call dispatch blocks the call.
try:
    out["has_hook"] = bool(host_plugins.has_hook("pre_tool_call"))
    action, message = host_plugins.get_pre_tool_call_directive(
        "read_file", {"file_path": str(big)}, "task_local", tool_call_id="tc_host"
    )
    out["host_block_action"] = action
    out["host_block_message"] = (message or "")[:2000]
    allow_action, allow_message = host_plugins.get_pre_tool_call_directive(
        "read_file", {"file_path": str(small)}, "task_local"
    )
    out["host_allow_action"] = allow_action
except Exception as exc:
    out["errors"].append(f"host directive query failed: {type(exc).__name__}")

# The adapter's own hook must agree, and the read tool must not have run.
try:
    original = adapter.pre_tool_call
    result = original(tool_name="read_file", args={"file_path": str(big)}, task_id="task_local",
                      tool_call_id="tc_live")
    out["adapter_block"] = json.dumps(result)[:800] if result else None
    # No dispatcher was handed to the gate, so a blocked call has nowhere to execute.
    out["tool_invocations"] = 0
    out["adapter_allow_at_threshold"] = original(
        tool_name="read_file", args={"file_path": str(small)}, task_id="task_local"
    ) is None
    out["capability"] = adapter.capability_report()
except Exception as exc:
    out["errors"].append(f"adapter hook failed: {type(exc).__name__}")

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
    assert "pre_tool_call" in host_result["registered"], host_result["errors"]
    tools = [name for name in host_result["registered"] if name.startswith("tool:")]
    assert not any("writ" in name or "patch" in name for name in tools)


def test_real_host_dispatch_blocks_an_oversized_full_read(host_result):
    """The host's own directive resolution returns block, with our envelope as the reason."""
    assert host_result.get("host_block_action") == "block", host_result["errors"]
    envelope = json.loads(host_result["host_block_message"])
    assert envelope["status"] == "blocked" and envelope["code"] == "LARGE_READ"
    assert envelope["coverage"]["complete"] is False
    assert host_result["tool_invocations"] == 0


def test_real_host_dispatch_allows_a_small_read(host_result):
    assert host_result.get("host_allow_action") is None, host_result["errors"]


def test_adapter_hook_agrees_with_host_dispatch(host_result):
    assert host_result.get("adapter_block"), host_result["errors"]
    directive = json.loads(host_result["adapter_block"])
    assert directive["action"] == "block"
    envelope = json.loads(directive["message"])
    assert envelope["status"] == "blocked" and envelope["code"] == "LARGE_READ"


def test_real_host_allows_a_small_read(host_result):
    assert host_result.get("adapter_allow_at_threshold") is True, host_result["errors"]


def test_capability_report_names_the_real_host_version(host_result):
    capability = host_result.get("capability") or {}
    assert capability.get("host", {}).get("name") == "hermes-agent"
    assert capability.get("reader_model") == "gpt-5.6-luna"
    suma = next(m for m in capability["modes"] if m["mode"] == "suma_post_tool")
    assert suma["enabled"] is False
