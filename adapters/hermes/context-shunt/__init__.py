"""Hermes adapter for context-shunt.

Wiring (verified against hermes-agent 0.18.0):

* ``ctx.register_hook("pre_tool_call", ...)`` runs inside ``handle_function_call()``
  *before* the tool's handler, and returning ``{"action": "block", "message": ...}``
  short-circuits the call. That is what makes the large-read gate a pre-execution gate:
  a blocked read never runs.
* ``ctx.llm.complete(..., model="gpt-5.6-luna")`` is the host-owned model bridge. The
  model override is gated per plugin by ``plugins.entries.context-shunt.llm``; if the
  host refuses the override, the reader reports ``MODEL_ERROR`` rather than answering
  with whatever model the host would have picked.
* ``ctx.register_tool`` exposes the read-only reader. No writer tool is registered.

The optional Suma post-tool mode is **not** wired here. Hermes' ``transform_tool_result``
hands the plugin a result that is already post-truncation, and the host wraps the hook in
try/except so a raising handler leaves the original result in place. Neither
"complete capture before truncation" nor "no raw fallback" can be shown, so the mode is
reported unsupported and stays off. See docs/capability-matrix.md.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_VENDORED = Path(__file__).resolve().parents[3] / "packages" / "core-py" / "src"
if _VENDORED.exists() and str(_VENDORED) not in sys.path:  # editable/source checkout
    sys.path.insert(0, str(_VENDORED))

from context_shunt import __version__ as CORE_VERSION  # noqa: E402
from context_shunt.capability import (  # noqa: E402
    CapabilityReport,
    DisabledReason,
    supported,
    unsupported,
)
from context_shunt.config import load as load_config  # noqa: E402
from context_shunt.errors import ShuntError  # noqa: E402
from context_shunt.guard import enforce_or_fixed, fixed_error  # noqa: E402
from context_shunt.limits import READER_MODEL, SCHEMA_VERSION  # noqa: E402
from context_shunt.provider import HostBridgeProvider, UnavailableProvider  # noqa: E402
from context_shunt.session import ShuntSession  # noqa: E402

ADAPTER = "hermes"
PLUGIN_ID = "context-shunt"
# Hermes groups tools into toolsets; ours holds exactly one read-only tool.
TOOLSET = "context_shunt"

# Hermes tool ids this adapter claims to cover. A read tool outside this list is not
# protected, and the capability report says so rather than implying blanket coverage.
READ_TOOLS = {"read_file": "read", "read": "read", "view_file": "read"}
SEARCH_TOOLS = {"search_files": "search", "grep": "search", "search": "search"}
SHELL_TOOLS = {"terminal": "shell", "bash": "shell", "shell": "shell", "execute_command": "shell"}

_sessions: dict[str, ShuntSession] = {}
_config = None
_capability: CapabilityReport | None = None
_llm = None


# -- capability ------------------------------------------------------------


def build_capability_report(ctx: Any, *, host_version: str = "") -> CapabilityReport:
    """Probe the host, then report exactly what was proven - nothing more."""
    modes = []

    has_pre_tool = _can_register(ctx, "pre_tool_call")
    modes.append(
        supported("local_gate", evidence=("hermes:pre_tool_call blocks before dispatch",))
        if has_pre_tool
        else unsupported("local_gate", DisabledReason.HOOK_MISSING)
    )

    llm = getattr(ctx, "llm", None)
    if llm is None or not hasattr(llm, "complete"):
        modes.append(unsupported("reader", DisabledReason.MODEL_UNAVAILABLE))
    else:
        modes.append(supported("reader", evidence=(f"ctx.llm.complete pinned to {READER_MODEL}",)))

    # Suma post-tool: refused on evidence, not on absence of effort.
    modes.append(
        unsupported(
            "suma_post_tool",
            DisabledReason.CAPTURE_AFTER_TRUNCATION,
            DisabledReason.HOST_FAIL_OPEN,
            evidence=(
                "hermes-agent model_tools.py: transform_tool_result runs inside try/except "
                "and the original result survives a raising handler (fail-open)",
                "hermes-agent hooks doc: transform_tool_result receives the result "
                "post-truncation and post-ANSI-strip",
            ),
        )
    )

    return CapabilityReport(
        adapter=ADAPTER,
        adapter_version=CORE_VERSION,
        host_name="hermes-agent",
        host_version=host_version or _detect_host_version(),
        contract_version=SCHEMA_VERSION,
        reader_model=READER_MODEL,
        tools_covered=tuple(sorted(READ_TOOLS) + sorted(SEARCH_TOOLS) + sorted(SHELL_TOOLS)),
        modes=modes,
        tested_fixture_id="contracts/v1/conformance/gate-cases.json",
    )


def _can_register(ctx: Any, hook: str) -> bool:
    return callable(getattr(ctx, "register_hook", None)) and hook in _supported_hooks(ctx)


def _supported_hooks(ctx: Any) -> set[str]:
    declared = getattr(ctx, "supported_hooks", None)
    if declared:
        return set(declared)
    # Hermes does not publish the list on the context; fall back to the host constant.
    try:
        from hermes_cli.plugins import HOOK_NAMES  # type: ignore

        return set(HOOK_NAMES)
    except Exception:
        return {"pre_tool_call", "post_tool_call", "on_session_start", "on_session_end"}


def _detect_host_version() -> str:
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        return "unknown"


# -- normalization ---------------------------------------------------------


def normalize_tool_call(tool_name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a Hermes tool call onto the core's ``(tool, args)`` shape."""
    name = (tool_name or "").strip().lower()
    args = args or {}
    if name in READ_TOOLS:
        return "read", {
            "file_path": args.get("file_path") or args.get("path") or args.get("filename"),
            "offset": args.get("offset") or args.get("start_line"),
            "limit": args.get("limit") or args.get("num_lines"),
        }
    if name in SEARCH_TOOLS:
        return "search", {
            "path": args.get("path") or args.get("directory"),
            "pattern": args.get("pattern") or args.get("query"),
            "max_matches": args.get("max_matches") or args.get("max_results"),
        }
    if name in SHELL_TOOLS:
        return "shell", {"command": args.get("command") or args.get("cmd") or ""}
    return "other", {}


# -- session plumbing ------------------------------------------------------


def _session(task_id: str) -> ShuntSession:
    key = task_id or "default"
    session = _sessions.get(key)
    if session is None:
        provider = (
            HostBridgeProvider(_bridge_call, _config.limits)
            if _llm is not None
            else UnavailableProvider("HOST_LLM_UNAVAILABLE")
        )
        session = ShuntSession(key, _config, _capability, provider=provider)
        _sessions[key] = session
    return session


def _bridge_call(*, system: str, user: str, model: str, max_output_tokens: int, timeout_ms: int):
    """Call Hermes' plugin LLM facade with the model pinned.

    ``ctx.llm`` never exposes credentials, and provider exception text is dropped by
    ``HostBridgeProvider`` - only ``MODEL_ERROR`` crosses back.
    """
    result = _llm.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        max_tokens=max_output_tokens,
        timeout=max(1.0, timeout_ms / 1000.0),
        temperature=0,
        purpose="context-shunt-reader",
    )
    usage = getattr(result, "usage", None)
    return {
        "text": getattr(result, "text", "") or "",
        "model": getattr(result, "model", "") or "",
        "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
        "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
    }


# -- hooks -----------------------------------------------------------------


def pre_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "", **kwargs):
    """Veto an oversized or unprovable read before the tool runs."""
    if _config is None or not _config.gate_enabled:
        return None
    tool, normalized = normalize_tool_call(tool_name, args or {})
    if tool == "other":
        return None
    try:
        session = _session(task_id)
        decision = session.evaluate_tool_call(tool, normalized)
    except ShuntError:
        # Fail closed for a read-like call we could not evaluate.
        return {"action": "block", "message": _block_message(fixed_error("req_gate", "HOST_UNSAFE"))}
    if not decision.blocked:
        return None
    envelope = session.block_envelope(_request_id(kwargs), decision)
    return {"action": "block", "message": _block_message(envelope)}


def _request_id(kwargs: dict[str, Any]) -> str:
    raw = str(kwargs.get("tool_call_id") or kwargs.get("turn_id") or "gate")
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "_.:-")[:56]
    return f"req_{safe or 'gate'}"


def _block_message(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def on_session_end(session_id: str = "", **kwargs):
    session = _sessions.pop(session_id or "default", None)
    if session is not None:
        session.close()


# -- reader tool -----------------------------------------------------------


def context_shunt_read(**kwargs) -> str:
    """Answer a question about a registered source. Read-only; returns a bounded envelope."""
    task_id = str(kwargs.get("task_id") or "default")
    session = _session(task_id)
    question = kwargs.get("question")
    paths = kwargs.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    request_id = _request_id(kwargs)

    try:
        sources = []
        for path in paths[: _config.limits.max_sources_per_request]:
            entry = session.register_path(str(path))
            sources.append(
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": kwargs.get("selector") or {"kind": "all"},
                }
            )
    except ShuntError as exc:
        from context_shunt import envelope as E

        return _block_message(enforce_or_fixed(E.error_envelope(request_id, exc), _config.limits))

    envelope = session.read(
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "operation": "read",
            "question": question if isinstance(question, str) else "",
            "sources": sources,
            "budgets": {
                "max_chunks": _config.limits.max_chunks_per_request,
                "max_answer_bytes": _config.limits.max_answer_bytes,
                "deadline_ms": _config.limits.request_deadline_ms,
            },
        }
    )
    return _block_message(envelope)


READER_TOOL_SCHEMA = {
    "name": "context_shunt_read",
    "description": (
        "Answer a question about one or more large files without pulling them into this "
        "conversation. Returns a bounded, citation-verified answer. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to answer. Required."},
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Absolute paths inside a configured workspace root.",
            },
        },
        "required": ["question", "paths"],
    },
}


# -- registration ----------------------------------------------------------


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    global _config, _capability, _llm

    raw = dict(getattr(ctx, "plugin_config", None) or getattr(ctx, "config", None) or {})
    raw.setdefault("workspace_roots", [os.getcwd()])
    default_cache = Path(os.environ.get("CONTEXT_SHUNT_CACHE", Path.home() / ".cache" / "context-shunt"))
    _config = load_config(raw, default_spill_dir=default_cache)

    _llm = getattr(ctx, "llm", None)
    _capability = build_capability_report(ctx)

    if _capability.enabled("local_gate"):
        ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("on_session_end", on_session_end)

    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool) and _capability.enabled("reader"):
        # Hermes' PluginContext.register_tool takes (name, toolset, schema, handler, ...).
        # No override= is passed: this tool adds a surface, it never replaces a built-in.
        register_tool(
            READER_TOOL_SCHEMA["name"],
            TOOLSET,
            READER_TOOL_SCHEMA,
            context_shunt_read,
            description=READER_TOOL_SCHEMA["description"],
        )

    log = getattr(ctx, "logger", None)
    if log is not None:
        log.info("context-shunt capability report: %s", json.dumps(_capability.to_dict()))


def capability_report() -> dict[str, Any]:
    """Exposed for `scripts/verify` and for operators; contains no source content."""
    return (_capability or build_capability_report(object())).to_dict()
