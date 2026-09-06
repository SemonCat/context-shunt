"""Hermes adapter for context-shunt.

Wiring, verified against the hermes-agent checkout named by
``CONTEXT_SHUNT_HERMES_ROOT``:

* ``ctx.register_hook("pre_tool_call", ...)`` runs inside ``handle_function_call()``
  *before* the tool's handler, and returning ``{"action": "block", "message": ...}``
  short-circuits the call. That is what makes the large-read gate a pre-execution gate:
  a blocked read never runs.
* ``ctx.register_tool(name, toolset, schema, handler, ...)`` exposes the three read-only
  escape hatches. No writer tool is registered, and no ``override=`` is passed - these add
  a surface, they never replace a built-in.
* ``ctx.register_auxiliary_task("context_shunt_reader", ...)`` declares the reader as a
  first-class auxiliary task, so it appears in ``hermes model -> Configure auxiliary
  models`` and gets its own ``auxiliary.context_shunt_reader`` config block.

Session lifecycle: the part that used to be wrong
-------------------------------------------------
``on_session_end`` is **not** a session boundary on Hermes. ``agent/turn_finalizer.py``
fires it "at the very end of every ``run_conversation`` call", and the host's own comment
says so - it is a per-turn event. Tearing handles down there deleted exactly the recovery
state the next turn needed, so a multi-turn conversation could never re-read a shunted
source.

The real boundaries are ``on_session_finalize`` (shutdown, ``/new``) and
``on_session_reset`` (``/reset``), both in ``VALID_HOOKS`` and both fired from
``cli.py::_notify_session_boundary``. So:

* ``on_session_end``  -> ``end_turn()``: an opportunistic sweep, handles survive.
* ``on_session_finalize`` -> ``close()``: revoke this scope.
* ``on_session_reset``    -> bump the scope generation: old handles stop resolving and
  cannot be replayed into the new conversation.

Anything the hooks miss is covered by TTL.

Reader model configuration and its honest ceiling
-------------------------------------------------
``register_auxiliary_task`` makes ``auxiliary.context_shunt_reader`` the canonical place a
user pins the reader's provider/model, and user config wins over the plugin's defaults.
The adapter reads that block itself through the public ``hermes_cli.config.load_config``
and layers it over its own defaults, because ``ctx.llm`` is task-agnostic - the
``PluginLlm`` facade calls ``agent.auxiliary_client.call_llm`` with ``task=None``
(``agent/plugin_llm.py``), so registering the task alone would not route anything. No
private resolver is imported, nothing is monkeypatched, and no log is parsed.

Attribution has a real ceiling here, and the adapter reports it rather than papering over
it. ``PluginLlm._resolve_attribution`` records ``response.model`` when the provider
returned one and otherwise falls back to the plugin's own override or the host's main
model. A caller cannot tell those cases apart from the result object, so this adapter
never claims ``provider_confirms_generation``: attribution comes back ``unverified`` (or
``mismatch`` when the value contradicts the request), never ``actual``. Capture, inspect
and stats do not depend on the reader and stay fully usable either way.

The optional Suma post-tool mode is not wired. ``transform_tool_result`` hands the plugin
a result that is already post-truncation, and the host wraps the hook in try/except so a
raising handler leaves the original result in place. Neither "complete capture before
truncation" nor "no raw fallback" can be shown, so the mode is reported unsupported and
stays off. See docs/capability-matrix.md.
"""

from __future__ import annotations

import json
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
from context_shunt.limits import (  # noqa: E402
    EMITTED_SCHEMA_VERSION,
    READER_MODEL,
)
from context_shunt.provider import HostBridgeProvider, UnavailableProvider  # noqa: E402
from context_shunt.schema import validate_tool_args  # noqa: E402
from context_shunt.session import ShuntSession  # noqa: E402
from context_shunt.store import ScopeIdentity, SnapshotStore  # noqa: E402

ADAPTER = "hermes"
PLUGIN_ID = "context-shunt"
# Hermes groups tools into toolsets; ours holds only read-only tools.
TOOLSET = "context_shunt"
#: Registered through PluginContext.register_auxiliary_task, so the reader appears in
#: `hermes model` and gets its own auxiliary.<key> config block.
AUX_TASK_KEY = "context_shunt_reader"

# Hermes tool ids this adapter claims to cover. A read tool outside this list is not
# protected, and the capability report says so rather than implying blanket coverage.
READ_TOOLS = {"read_file": "read"}
SEARCH_TOOLS = {"search_files": "search"}
SHELL_TOOLS = {"terminal": "shell"}

_sessions: dict[str, ShuntSession] = {}
_generations: dict[str, int] = {}
_config = None
_capability: CapabilityReport | None = None
_llm = None
_store: SnapshotStore | None = None


# -- capability ------------------------------------------------------------


def build_capability_report(ctx: Any, *, host_version: str = "") -> CapabilityReport:
    """Probe the host, then report exactly what was proven - nothing more."""
    modes = []
    hooks = _supported_hooks(ctx)

    has_pre_tool = _can_register(ctx, "pre_tool_call")
    modes.append(
        supported(
            "local_gate", evidence=("hermes:pre_tool_call blocks before dispatch",)
        )
        if has_pre_tool
        else unsupported("local_gate", DisabledReason.HOOK_MISSING)
    )

    llm = getattr(ctx, "llm", None)
    if llm is None or not hasattr(llm, "complete"):
        modes.append(unsupported("reader", DisabledReason.MODEL_UNAVAILABLE))
    else:
        modes.append(
            supported(
                "reader",
                evidence=(
                    f"ctx.llm.complete requested with model={_reader_target()[1]}",
                    "attribution ceiling: PluginLlm._resolve_attribution cannot separate a "
                    "provider report from an echo of the request, so this adapter reports "
                    "attribution_status=unverified and never claims actual",
                ),
            )
        )

    # Deterministic extraction and stats need no provider at all.
    modes.append(
        supported(
            "deterministic_inspect",
            evidence=("no provider reference on the inspect path; zero model calls",),
        )
    )
    modes.append(
        supported("session_stats", evidence=("store-backed, session-scoped only",))
    )

    # Real lifecycle boundaries, so recovery handles survive an ordinary turn.
    finalize = "on_session_finalize" in hooks
    reset = "on_session_reset" in hooks
    modes.append(
        supported(
            "session_lifecycle",
            evidence=(
                "on_session_end is per-turn (agent/turn_finalizer.py) and only sweeps",
                "on_session_finalize revokes; on_session_reset bumps the scope generation",
            ),
        )
        if finalize and reset
        else unsupported(
            "session_lifecycle",
            DisabledReason.HOOK_MISSING,
            evidence=("no finalize/reset boundary; handle teardown falls back to TTL",),
        )
    )

    aux = callable(getattr(ctx, "register_auxiliary_task", None))
    modes.append(
        supported(
            "reader_task_config",
            evidence=(
                f"ctx.register_auxiliary_task({AUX_TASK_KEY!r}) surfaces the reader in "
                "hermes model configuration",
                "user auxiliary.<key> config is layered over the plugin defaults",
            ),
        )
        if aux
        else unsupported("reader_task_config", DisabledReason.HOOK_MISSING)
    )

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
        contract_version=EMITTED_SCHEMA_VERSION,
        reader_model=_reader_target()[1],
        tools_covered=tuple(
            sorted(READ_TOOLS) + sorted(SEARCH_TOOLS) + sorted(SHELL_TOOLS)
        ),
        modes=modes,
        tested_fixture_id="contracts/v1/conformance/gate-cases.json",
    )


def _can_register(ctx: Any, hook: str) -> bool:
    return callable(getattr(ctx, "register_hook", None)) and hook in _supported_hooks(
        ctx
    )


def _supported_hooks(ctx: Any) -> set[str]:
    declared = getattr(ctx, "supported_hooks", None)
    if declared:
        return set(declared)
    # Hermes does not publish the list on the context; fall back to the host constant.
    try:
        from hermes_cli.plugins import VALID_HOOKS

        return set(VALID_HOOKS)
    except Exception:
        return {
            "pre_tool_call",
            "post_tool_call",
            "on_session_start",
            "on_session_end",
            "on_session_finalize",
            "on_session_reset",
        }


def _detect_host_version() -> str:
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        return "unknown"


# -- reader routing --------------------------------------------------------


def _auxiliary_task_config() -> dict[str, Any]:
    """Read ``auxiliary.context_shunt_reader`` from the host config.

    Only the public ``hermes_cli.config.load_config`` is used. The host layers plugin
    defaults under user config for task-aware calls; ``ctx.llm`` does not take a task, so
    the adapter performs the same layering itself and keeps the precedence identical:
    **user config wins over the plugin defaults.**
    """
    try:
        from hermes_cli.config import load_config as load_host_config

        config = load_host_config() or {}
    except Exception:
        return {}
    auxiliary = config.get("auxiliary") if isinstance(config, dict) else None
    entry = auxiliary.get(AUX_TASK_KEY) if isinstance(auxiliary, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}


def _reader_target() -> tuple[str, str]:
    """``(provider, model)`` for the reader: user auxiliary config over plugin defaults."""
    provider = _config.reader.provider if _config is not None else ""
    model = _config.reader.model if _config is not None else READER_MODEL
    task = _auxiliary_task_config()
    task_provider = str(task.get("provider", "") or "").strip()
    task_model = str(task.get("model", "") or "").strip()
    # "auto" is the host's sentinel for "inherit", not a literal model id.
    if task_provider and task_provider.lower() != "auto":
        provider = task_provider
    if task_model and task_model.lower() != "auto":
        model = task_model
    return provider, model


AUX_TASK_DEFAULTS: dict[str, Any] = {
    "provider": "auto",
    "model": READER_MODEL,
    "timeout": 20,
}


# -- normalization ---------------------------------------------------------


def normalize_tool_call(
    tool_name: str, args: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Map a Hermes tool call onto the core's ``(tool, args)`` shape."""
    name = (tool_name or "").strip().lower()
    args = args or {}
    if name in READ_TOOLS:
        return "read", {
            "file_path": args.get("file_path")
            or args.get("path")
            or args.get("filename"),
            "offset": args.get("offset") or args.get("start_line"),
            "limit": args.get("limit") or args.get("num_lines"),
        }
    if name in SEARCH_TOOLS:
        return "search", {
            "path": args.get("path") or args.get("directory"),
            "pattern": args.get("pattern") or args.get("query"),
            "max_matches": args.get("limit", 50),
            "target": args.get("target", "content"),
            "output_mode": args.get("output_mode", "content"),
            "context": args.get("context", 0),
        }
    if name in SHELL_TOOLS:
        return "shell", {"command": args.get("command") or args.get("cmd") or ""}
    return "other", {}


# -- session plumbing ------------------------------------------------------


def _session(task_id: str = "", session_id: str = "") -> ShuntSession:
    key = session_id or task_id or "unbound"
    session = _sessions.get(key)
    if session is None:
        provider = (
            HostBridgeProvider(
                _bridge_call,
                _config.limits,
                _reader_target()[1],
                provider=_reader_target()[0],
            )
            if _llm is not None
            else UnavailableProvider("HOST_LLM_UNAVAILABLE")
        )
        session = ShuntSession(
            key,
            _config,
            _capability,
            provider=provider,
            store=_store,
            identity=_identity(key),
        )
        _sessions[key] = session
    return session


def _identity(key: str) -> ScopeIdentity:
    """Scope handles to the trusted host identity plus a generation counter."""
    return ScopeIdentity(
        host="hermes-agent",
        profile=PLUGIN_ID,
        principal="local",
        session=key,
        generation=_generations.get(key, 1),
    )


def _bridge_call(
    *,
    system: str,
    user: str,
    provider: str,
    model: str,
    max_output_tokens: int,
    timeout_ms: int,
):
    """Call Hermes' plugin LLM facade and report only what it actually tells us.

    ``ctx.llm`` never exposes credentials, and provider exception text is dropped by
    ``HostBridgeProvider`` - only ``MODEL_ERROR`` crosses back.

    ``result.provider``/``result.model`` come from ``PluginLlm._resolve_attribution``,
    which records ``response.model`` when the provider supplied one and otherwise the
    plugin's own override or the host's main model. Those cases are indistinguishable from
    here, so they are reported as ``reported_*`` with
    ``provider_confirms_generation=False``: the truthful outcome is ``unverified``.
    """
    kwargs: dict[str, Any] = {
        "max_tokens": max_output_tokens,
        "timeout": max(1.0, timeout_ms / 1000.0),
        "temperature": 0,
        "purpose": "context-shunt-reader",
    }
    if model:
        kwargs["model"] = model
    if provider:
        kwargs["provider"] = provider
    result = _llm.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        **kwargs,
    )
    usage = getattr(result, "usage", None)
    reported_model = getattr(result, "model", "") or ""
    reported_provider = getattr(result, "provider", "") or ""
    return {
        "text": getattr(result, "text", "") or "",
        "reported_provider": reported_provider or None,
        "reported_model": reported_model or None,
        # Deliberately absent: the facade exposes no separate resolved selection.
        "resolved_provider": None,
        "resolved_model": None,
        "provider_confirms_generation": False,
        "input_tokens": _usage_field(usage, "input_tokens"),
        "output_tokens": _usage_field(usage, "output_tokens"),
        "cache_tokens": _usage_field(usage, "cache_read_tokens"),
        "usage_exact": usage is not None
        and _usage_field(usage, "input_tokens") is not None
        and _usage_field(usage, "output_tokens") is not None,
    }


def _usage_field(usage: Any, name: str) -> int | None:
    """Absent usage stays ``None``. Hermes reports 0 for "not provided", and 0 tokens is
    not a fact we can assert, so a zero is treated as unknown rather than as exact."""
    if usage is None:
        return None
    value = getattr(usage, name, None)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


# -- hooks -----------------------------------------------------------------


def pre_tool_call(
    tool_name: str = "",
    args: dict | None = None,
    task_id: str = "",
    session_id: str = "",
    **kwargs,
):
    """Veto an oversized or unprovable read before the tool runs."""
    if _config is None or not _config.gate_enabled:
        return None
    tool, normalized = normalize_tool_call(tool_name, args or {})
    if tool == "other":
        return None
    try:
        session = _session(task_id, session_id)
        decision = session.evaluate_tool_call(tool, normalized)
    except Exception:
        # Fail closed for a read-like call we could not evaluate.
        return {
            "action": "block",
            "message": _block_message(fixed_error("req_gate", "HOST_UNSAFE")),
        }
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
    """Per-turn, not a session boundary.

    ``agent/turn_finalizer.py`` fires this at the end of every ``run_conversation`` call.
    Closing the scope here would delete the recovery handles the next turn needs, so this
    only takes the opportunistic sweep. Real teardown is ``on_session_finalize`` /
    ``on_session_reset``, plus TTL.
    """
    key = session_id or str(kwargs.get("task_id") or "unbound")
    session = _sessions.get(key)
    if session is not None:
        session.end_turn()


def on_session_finalize(session_id: str = "", **kwargs):
    """A real session boundary: revoke this scope's handles."""
    key = session_id or str(kwargs.get("task_id") or "unbound")
    session = _sessions.pop(key, None)
    if session is not None:
        session.close()


def on_session_reset(session_id: str = "", **kwargs):
    """``/reset`` and ``/new``: bump the generation so old handles cannot be replayed."""
    key = session_id or str(kwargs.get("task_id") or "unbound")
    session = _sessions.pop(key, None)
    _generations[key] = _generations.get(key, 1) + 1
    if session is not None:
        session.close()


# -- tools -----------------------------------------------------------------


def context_shunt_read(args: dict[str, Any] | None = None, **kwargs) -> str:
    """Answer a question about a source. Read-only; returns a bounded envelope.

    Exactly one source form: ``paths`` for an initial capture, or ``handles`` for a
    refined question over snapshots the caller already holds. A refined question reuses
    the named immutable snapshot and never recaptures the source.
    """
    params = {**(args or {}), **kwargs}
    session = _session(
        str(params.get("task_id") or ""), str(params.get("session_id") or "")
    )
    request_id = _request_id(params)

    try:
        tool_args = validate_tool_args(_read_args(params))
    except ShuntError as exc:
        return _error(request_id, exc)

    try:
        if "paths" in tool_args:
            entries = session.register_paths([str(p) for p in tool_args["paths"]])
            sources = [
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": tool_args.get("selector") or {"kind": "all"},
                }
                for entry in entries
            ]
            refined = False
        else:
            sources = [
                {
                    "source_id": handle["source_id"],
                    "snapshot_id": handle["snapshot_id"],
                    "selector": tool_args.get("selector") or {"kind": "all"},
                }
                for handle in tool_args["handles"]
            ]
            refined = True
    except ShuntError as exc:
        return _error(request_id, exc)
    except Exception:
        return _block_message(fixed_error(request_id, "STORE_FAILED"))

    request = {
        "schema_version": EMITTED_SCHEMA_VERSION,
        "request_id": request_id,
        "operation": "read",
        "question": tool_args["question"],
        "sources": sources,
        "budgets": {
            "max_chunks": _config.limits.max_chunks_per_request,
            "max_answer_bytes": _config.limits.max_answer_bytes,
            "deadline_ms": _config.limits.request_deadline_ms,
        },
    }
    if refined:
        request["refined"] = True
    try:
        return _block_message(session.read(request))
    except Exception:
        return _block_message(fixed_error(request_id, "HOST_UNSAFE"))


def _read_args(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tool": "context_shunt_read",
        "question": params.get("question"),
    }
    paths = params.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    if paths is not None:
        out["paths"] = paths
    if params.get("handles") is not None:
        out["handles"] = params["handles"]
    if params.get("selector") is not None:
        out["selector"] = params["selector"]
    return out


def context_shunt_inspect(args: dict[str, Any] | None = None, **kwargs) -> str:
    """Exact lines, bytes or literal-search hits from a handle. Zero model calls."""
    params = {**(args or {}), **kwargs}
    session = _session(
        str(params.get("task_id") or ""), str(params.get("session_id") or "")
    )
    request_id = _request_id(params)
    try:
        tool_args = validate_tool_args(
            {
                "tool": "context_shunt_inspect",
                **{
                    key: params[key]
                    for key in ("source_id", "snapshot_id", "selector", "cursor")
                    if params.get(key) is not None
                },
            }
        )
    except ShuntError as exc:
        return _error(request_id, exc)

    request: dict[str, Any] = {
        "schema_version": EMITTED_SCHEMA_VERSION,
        "request_id": request_id,
        "operation": "inspect",
        "source_id": tool_args["source_id"],
        "snapshot_id": tool_args["snapshot_id"],
        "selector": tool_args["selector"],
        "budgets": {
            "max_result_bytes": min(
                int(
                    params.get("max_result_bytes")
                    or _config.limits.inspect_max_result_bytes
                ),
                _config.limits.inspect_max_result_bytes,
            ),
            "max_scan_lines": min(
                int(
                    params.get("max_scan_lines")
                    or _config.limits.inspect_max_scan_lines
                ),
                _config.limits.inspect_max_scan_lines,
            ),
        },
    }
    if "cursor" in tool_args:
        request["cursor"] = tool_args["cursor"]
    try:
        return _block_message(session.inspect(request))
    except Exception:
        return _block_message(fixed_error(request_id, "HOST_UNSAFE"))


def context_shunt_stats(args: dict[str, Any] | None = None, **kwargs) -> str:
    """This session's own bounded accounting. Read-only; resets nothing."""
    params = {**(args or {}), **kwargs}
    session = _session(
        str(params.get("task_id") or ""), str(params.get("session_id") or "")
    )
    request_id = _request_id(params)
    request: dict[str, Any] = {
        "schema_version": EMITTED_SCHEMA_VERSION,
        "request_id": request_id,
        "operation": "stats",
    }
    for key in ("page", "page_size"):
        if params.get(key) is not None:
            try:
                request[key] = int(params[key])
            except (TypeError, ValueError):
                return _error(
                    request_id,
                    ShuntError("INVALID_REQUEST", "BAD_PAGE", retryable=False),
                )
    try:
        return _block_message(session.stats(request))
    except Exception:
        return _block_message(fixed_error(request_id, "HOST_UNSAFE"))


def _error(request_id: str, exc: ShuntError) -> str:
    from context_shunt import envelope as E

    return _block_message(
        enforce_or_fixed(E.error_envelope(request_id, exc), _config.limits)
    )


READER_TOOL_SCHEMA = {
    "name": "context_shunt_read",
    "description": (
        "Answer a question about one or more large files without pulling them into this "
        "conversation. Returns a bounded, citation-verified answer that is generated by a "
        "reader model, not raw source text. Read-only. Pass paths for a first look, or "
        "handles to ask a sharper question about a snapshot you already hold."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to answer. Required.",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Absolute paths inside a configured workspace root. Use this OR handles, "
                    "never both."
                ),
            },
            "handles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "snapshot_id": {"type": "string"},
                    },
                    "required": ["source_id", "snapshot_id"],
                },
                "description": (
                    "Handles from an earlier reply, to refine the question against the same "
                    "immutable snapshot. Use this OR paths, never both."
                ),
            },
        },
        "required": ["question"],
    },
}

INSPECT_TOOL_SCHEMA = {
    "name": "context_shunt_inspect",
    "description": (
        "Return exact text from a snapshot you already hold: a line range, a byte range, or "
        "literal-search hits. Deterministic - no model is involved, so the result is source "
        "bytes rather than a summary. Each page is capped at 16 KiB and counts against a "
        "cumulative disclosure budget, so this cannot be paged into a full copy of the file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "Handle from an earlier reply.",
            },
            "snapshot_id": {
                "type": "string",
                "description": "The snapshot hash you were given. A mismatch is refused.",
            },
            "selector": {
                "type": "object",
                "description": (
                    'Exactly one of {"kind":"lines","start":N,"end":N}, '
                    '{"kind":"bytes","start":N,"end":N}, or '
                    '{"kind":"search","needle":"...","max_matches":N}.'
                ),
            },
            "cursor": {
                "type": "string",
                "description": "Opaque next_cursor from a previous inspect result.",
            },
        },
        "required": ["source_id", "snapshot_id", "selector"],
    },
}

STATS_TOOL_SCHEMA = {
    "name": "context_shunt_stats",
    "description": (
        "Report this session's context-shunt accounting: bytes withheld, tokens saved or "
        "spent, and per-operation records. Read-only - it cannot reset a counter, change "
        "retention, see another session, or reveal any source content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "page": {
                "type": "integer",
                "description": "1-based page of operation records.",
            },
            "page_size": {
                "type": "integer",
                "description": "Records per page, at most 8.",
            },
        },
        "required": [],
    },
}

TOOLS = (
    (READER_TOOL_SCHEMA, context_shunt_read, "reader"),
    (INSPECT_TOOL_SCHEMA, context_shunt_inspect, "deterministic_inspect"),
    (STATS_TOOL_SCHEMA, context_shunt_stats, "session_stats"),
)


# -- registration ----------------------------------------------------------


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    global _config, _capability, _llm, _store

    for session in _sessions.values():
        session.close()
    _sessions.clear()
    raw = _load_plugin_config(ctx)
    import os

    default_cache = Path(
        os.environ.get("CONTEXT_SHUNT_CACHE", Path.home() / ".cache" / "context-shunt")
    )
    _config = load_config(raw, default_spill_dir=default_cache)

    _llm = getattr(ctx, "llm", None)
    _capability = build_capability_report(ctx)

    # Declare the auxiliary task before anything can call the reader, so the task exists
    # in `hermes model` even on a run that never reads.
    register_aux = getattr(ctx, "register_auxiliary_task", None)
    if callable(register_aux):
        try:
            register_aux(
                AUX_TASK_KEY,
                display_name="Context shunt reader",
                description="answers questions about withheld large sources",
                defaults={**AUX_TASK_DEFAULTS, "model": _config.reader.model},
            )
        except Exception:
            # A host that refuses the registration still gets a working gate and the
            # plugin's own defaults; it just loses the config surface.
            pass

    _store = SnapshotStore(_config.cache_root, _config.limits)
    # Deterministic recovery first: clear staged temps and unreferenced content from a
    # previous crash before any new handle is published.
    try:
        _store.recover()
    except ShuntError:
        pass

    if _capability.enabled("local_gate"):
        ctx.register_hook("pre_tool_call", pre_tool_call)
    hooks = _supported_hooks(ctx)
    ctx.register_hook("on_session_end", on_session_end)
    if "on_session_finalize" in hooks:
        ctx.register_hook("on_session_finalize", on_session_finalize)
    if "on_session_reset" in hooks:
        ctx.register_hook("on_session_reset", on_session_reset)

    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        for schema, handler, mode in TOOLS:
            if not _capability.enabled(mode):
                continue
            if mode == "deterministic_inspect" and not _config.tools.inspect_enabled:
                continue
            if mode == "session_stats" and not _config.tools.stats_enabled:
                continue
            # Hermes' PluginContext.register_tool takes (name, toolset, schema, handler, ...).
            # No override= is passed: these add a surface, they never replace a built-in.
            register_tool(
                schema["name"],
                TOOLSET,
                schema,
                handler,
                description=schema["description"],
            )

    log = getattr(ctx, "logger", None)
    if log is not None:
        log.info(
            "context-shunt capability report: %s", json.dumps(_capability.to_dict())
        )


def capability_report() -> dict[str, Any]:
    """Exposed for `scripts/verify` and for operators; contains no source content."""
    return (_capability or build_capability_report(object())).to_dict()


def _load_plugin_config(ctx: Any) -> dict[str, Any]:
    """Read the real Hermes ``plugins.entries.<id>.config`` block."""
    direct = getattr(ctx, "plugin_config", None)
    if isinstance(direct, dict):
        return dict(direct)
    try:
        from hermes_cli.config import load_config as load_host_config

        config = load_host_config() or {}
        plugins = config.get("plugins", {})
        entries = plugins.get("entries", {}) if isinstance(plugins, dict) else {}
        entry = entries.get(PLUGIN_ID, {}) if isinstance(entries, dict) else {}
        plugin_config = entry.get("config", {}) if isinstance(entry, dict) else {}
        return dict(plugin_config) if isinstance(plugin_config, dict) else {}
    except Exception:
        return {}
