"""Hermes adapter for context-shunt.

Wiring, verified against the hermes-agent checkout named by
``CONTEXT_SHUNT_HERMES_ROOT``:

* ``ctx.register_hook("pre_tool_call", ...)`` runs inside ``handle_function_call()``
  *before* the tool's handler, and returning ``{"action": "block", "message": ...}``
  short-circuits the call. That is what makes the large-read gate a pre-execution gate:
  a blocked read never runs.
* ``ctx.register_tool(name, toolset, schema, handler, ...)`` exposes the read-only escape
  hatches and, where a deployment authorized it, the external-artifact import route. No
  writer tool is registered, and no ``override=`` is passed - these add a surface, they
  never replace a built-in.
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
Current Hermes exposes ``ctx.llm.complete(task=...)``; the adapter uses its own registered
task key so the host owns that routing and returns its selected route. Older supported
Hermes builds have the same facade without ``task``. The adapter detects the public method
signature before any provider call and retains its compatibility path: it reads the same
auxiliary block through public ``hermes_cli.config.load_config`` and supplies the resolved
provider/model overrides itself. It never retries a provider call to discover capability.
No private resolver is imported, nothing is monkeypatched, and no log is parsed.

Attribution has a real ceiling here, and the adapter reports it rather than papering over
it. On the task-aware surface, ``PluginLlm`` requests ``route_info`` from the auxiliary
router and returns that post-policy provider/model; the adapter reports it as ``resolved``.
On an older task-agnostic surface, the result can still be an echo of the override, so the
compatibility path remains ``unverified``. Neither path claims
``provider_confirms_generation`` or ``actual``. A contradiction is still ``mismatch``.
Capture, inspect and stats do not depend on the reader and stay fully usable either way.

The optional oversized-tool-result capture mode (``tool_result_capture``, formerly
documented under the internal name ``suma_post_tool``) is wired but not claimed
unconditionally. On Hermes 0.21.3, the two existing operator attestations register the
official ``tool_execution`` middleware plus post-middleware ``pre_api_request`` observer.
The adapter binds the actual provider-visible direct/deferred tool surface to immutable
request ids and replaces results inside the authorized execution chain. It never
infers reachability from global plugin registration. See ``_tool_result_capture_mode`` and
docs/capability-matrix.md for the exact evidence. The handler also never depends on the
host's fail-open behavior: every oversized owned failure becomes a bounded envelope.

The middleware receives no question, so it only ever captures and points; answering happens
through a separate ``context_shunt_read`` call, the same two-step shape the artifact-import
boundary already uses.

What else is wired for oversized tool results
-----------------------------------------------
The artifact import route, which needs no interception at all. A compactor or spooler that
already persisted an oversized tool result to a file, plus a manifest describing it, can
hand that artifact over through ``context_shunt_import``; the core proves the file matches
the manifest and returns an opaque handle. This has never depended on ``tool_result_capture``
and remains a separate mode, off unless a deployment configures import roots and allowlists
a producer manifest schema.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any

_VENDORED = Path(__file__).resolve().parents[3] / "packages" / "core-py" / "src"
if _VENDORED.exists() and str(_VENDORED) not in sys.path:  # editable/source checkout
    sys.path.insert(0, str(_VENDORED))

from context_shunt import __version__ as CORE_VERSION  # noqa: E402
from context_shunt.capability import (  # noqa: E402
    CapabilityReport,
    DisabledReason,
    ModeCapability,
    supported,
    unsupported,
)
from context_shunt.config import load as load_config  # noqa: E402
from context_shunt.errors import ShuntError  # noqa: E402
from context_shunt.guard import enforce_or_fixed, fixed_error  # noqa: E402
from context_shunt.limits import (  # noqa: E402
    CONTRACTS_DIR,
    EMITTED_SCHEMA_VERSION,
    READER_MODEL,
)
from context_shunt.provider import UnavailableProvider  # noqa: E402
from context_shunt.session import build_provider  # noqa: E402
from context_shunt.fallback import compact_failure, compact_paths_failure  # noqa: E402
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


def normalize_tool_identity(tool_name: Any) -> str:
    """Use the same exact host identity for the pre-read gate and result classifier."""
    return tool_name.strip().lower() if isinstance(tool_name, str) else ""


def classify_tool_result(
    tool_name: Any, capture_allowlist: frozenset[str] = frozenset()
) -> str:
    """Classify identity only; never inspect arguments, paths, or result content.

    Additional capture identities require operator configuration. In particular, an MCP
    read_resource name alone cannot prove generated-utility provenance: the hook does not
    carry the executed handler and a server-native tool can occupy that identity. Registry
    metadata queried after execution cannot establish which handler produced the result.
    Protected identities take precedence over configuration; unknown tools fail closed.
    """
    name = normalize_tool_identity(tool_name)
    if name in {"skill_view", "skills_list", "clarify", "todo"} or name in {
        schema["name"] for schema, _handler, _mode in TOOLS
    }:
        return "protected"
    # Match the full generated MCP identity, not an arbitrary substring or read suffix.
    server, separator, utility = name.removeprefix("mcp__").rpartition("__")
    if (
        name.startswith("mcp__")
        and separator
        and server
        and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in server)
        and utility in {"list_resources", "list_prompts", "get_prompt"}
    ):
        return "protected"
    if name and (
        name in READ_TOOLS or name in SEARCH_TOOLS or name in capture_allowlist
    ):
        return "eligible"
    return "passthrough"


_capture_tool_allowlist: frozenset[str] = frozenset()
_sessions: dict[str, ShuntSession] = {}
_generations: dict[str, int] = {}
_config = None
_capability: CapabilityReport | None = None
_llm = None
_store: SnapshotStore | None = None
_metrics = None

# Provider-request capability evidence is deliberately short-lived and bounded. Hermes
# generates one api_request_id per model request and forwards that same id to every tool
# execution selected from the response. Keeping the full four-part key prevents two
# concurrent sessions/turns from sharing evidence even if a test double reuses an id.
_REQUEST_SCOPE_MAX = 512
_REQUEST_SCOPE_TTL_SECONDS = 60 * 60
_REQUEST_TOOLS_MAX = 4096
_REQUEST_DESCRIPTION_MAX_CHARS = 256_000
_request_scopes: "OrderedDict[tuple[str, str, str, str], tuple[float, frozenset[str], frozenset[str]]]" = OrderedDict()
_request_scopes_lock = threading.RLock()


def _request_scope_key(kwargs: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """Return an immutable host correlation key, or ``None`` when it is incomplete."""
    session_id = str(kwargs.get("session_id") or "")
    task_id = str(kwargs.get("task_id") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    api_request_id = str(kwargs.get("api_request_id") or "")
    if not session_id or not task_id or not turn_id or not api_request_id:
        return None
    if any(len(value) > 512 for value in (session_id, task_id, turn_id, api_request_id)):
        return None
    return session_id, task_id, turn_id, api_request_id


def _provider_tool_name(tool: Any) -> str:
    """Extract a direct tool name from OpenAI/Anthropic-style provider schemas."""
    if not isinstance(tool, Mapping):
        return ""
    function = tool.get("function")
    raw = function.get("name") if isinstance(function, Mapping) else tool.get("name")
    return normalize_tool_identity(raw)


def _provider_tool_description(tool: Any) -> str:
    if not isinstance(tool, Mapping):
        return ""
    function = tool.get("function")
    raw = function.get("description") if isinstance(function, Mapping) else tool.get("description")
    return raw if isinstance(raw, str) and len(raw) <= _REQUEST_DESCRIPTION_MAX_CHARS else ""


_CATALOG_HEADER = "Deferred tool catalog (call schemas via `tool_describe`, invoke via `tool_call`):"
_CATALOG_GROUP = re.compile(r"^(.+?) tools \((\d+)\):$")
_CATALOG_NAME = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


def _explicit_deferred_names(description: str) -> frozenset[str]:
    """Parse only names explicitly rendered by Hermes' bounded deferred catalog.

    Tier-2 summaries (``names not listed``), malformed/truncated groups, and arbitrary
    prose prove nothing. Each accepted group must contain exactly its declared number of
    syntactically valid names, so a clipped hook payload cannot accidentally grant a
    partial catalog broader meaning.
    """
    marker = description.find(_CATALOG_HEADER)
    if marker < 0:
        return frozenset()
    lines = description[marker + len(_CATALOG_HEADER):].splitlines()
    names: set[str] = set()
    index = 0
    while index < len(lines):
        heading = _CATALOG_GROUP.fullmatch(lines[index].strip())
        if heading is None:
            index += 1
            continue
        declared = int(heading.group(2))
        index += 1
        body: list[str] = []
        while index < len(lines) and _CATALOG_GROUP.fullmatch(lines[index].strip()) is None:
            # A collapsed group is not a ``... tools (N):`` heading and is ignored. Stop
            # at it rather than allowing its prose to be interpreted as a names line.
            if "names not listed" in lines[index]:
                break
            if lines[index].strip():
                body.append(lines[index].strip())
            index += 1
        parsed: list[str] = []
        if body and all(line.startswith("- ") for line in body):
            parsed = [line[2:].split(":", 1)[0].strip() for line in body]
        elif len(body) == 1:
            parsed = [part.strip() for part in body[0].split(",")]
        if (
            0 < declared <= _REQUEST_TOOLS_MAX
            and len(parsed) == declared
            and len(names) + len(parsed) <= _REQUEST_TOOLS_MAX
            and all(_CATALOG_NAME.fullmatch(name) for name in parsed)
        ):
            names.update(parsed)
    return frozenset(names)


def _provider_request_tools(request: Any) -> tuple[frozenset[str], frozenset[str]] | None:
    """Return actual direct/deferred names from a post-middleware hook payload."""
    if not isinstance(request, Mapping) or request.get("_truncated") is True:
        return None
    body = request.get("body", request)
    if not isinstance(body, Mapping) or body.get("_truncated") is True:
        return None
    tools = body.get("tools")
    if not isinstance(tools, list) or len(tools) > _REQUEST_TOOLS_MAX:
        return None
    direct: set[str] = set()
    deferred: set[str] = set()
    for tool in tools:
        name = _provider_tool_name(tool)
        if not name:
            return None
        direct.add(name)
        if name == "tool_search":
            deferred.update(_explicit_deferred_names(_provider_tool_description(tool)))
    return frozenset(direct), frozenset(deferred)


def observe_provider_request(request: Any = None, **kwargs: Any) -> None:
    """Record exact model-visible capability for this one Hermes API request.

    ``pre_api_request`` runs after all official request middleware. It is observer-only;
    this callback never rewrites provider input and never derives authority from global
    plugin registration or configuration. Missing/truncated evidence is stored as no
    capability, preventing stale evidence for a reused synthetic id.
    """
    key = _request_scope_key(kwargs)
    if key is None:
        return None
    observed = _provider_request_tools(request)
    now = time.monotonic()
    with _request_scopes_lock:
        cutoff = now - _REQUEST_SCOPE_TTL_SECONDS
        while _request_scopes and next(iter(_request_scopes.values()))[0] < cutoff:
            _request_scopes.popitem(last=False)
        _request_scopes.pop(key, None)
        if observed is not None:
            _request_scopes[key] = (now, observed[0], observed[1])
        while len(_request_scopes) > _REQUEST_SCOPE_MAX:
            _request_scopes.popitem(last=False)
    return None


def observe_llm_execution(
    request: Any = None, next_call: Any = None, **kwargs: Any
) -> Any:
    """Record the full provider request at Hermes' official execution boundary.

    ``pre_api_request`` receives a bounded/sanitized copy and is therefore only a
    provisional observation. ``llm_execution`` receives the effective request after
    request middleware; recording after its downstream call preserves the host's normal
    middleware/transport path and makes this evidence available before any tool result
    can be dispatched.
    """
    if not callable(next_call):
        raise TypeError("llm execution middleware requires next_call")
    result = next_call(request)
    observe_provider_request(request, **kwargs)
    return result


def _forget_request_scopes(session_id: str) -> None:
    if not session_id:
        return
    with _request_scopes_lock:
        for key in [key for key in _request_scopes if key[0] == session_id]:
            _request_scopes.pop(key, None)


class _ReaderMetrics:
    """Forward only the new bounded reader outcome timer to the host logger."""

    def __init__(self, logger: Any):
        self._logger = logger

    def count(self, _name: str, _labels=None, _value: int = 1) -> None:
        return None

    def observe(self, name: str, value: float, labels=None) -> None:
        if name != "reader_duration_ms":
            return
        bounded = labels or {}
        self._logger.info(
            "context-shunt reader metric: %s",
            json.dumps(
                {
                    "duration_ms": max(0, round(value)),
                    "status": str(bounded.get("status", "error")),
                    "code": str(bounded.get("code", "INTERNAL_ERROR")),
                },
                separators=(",", ":"),
            ),
        )


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
        task_aware = _llm_accepts_task(llm)
        modes.append(
            supported(
                "reader",
                evidence=(
                    (
                        f"ctx.llm.complete(task={AUX_TASK_KEY}) requested with "
                        f"model={_reader_target()[1]}"
                        if task_aware
                        else "ctx.llm.complete compatibility path requested with "
                        f"model={_reader_target()[1]}"
                    ),
                    (
                        "task-aware PluginLlm returns the auxiliary router's post-policy "
                        "route as resolved attribution; never claims actual"
                        if task_aware
                        else "task-agnostic PluginLlm cannot separate a provider report "
                        "from an echo, so attribution is unverified; never claims actual"
                    ),
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

    # External artifact import. Needs no interception ordering, only a tool surface and
    # a core implementation - both of which exist here. Whether it *runs* is still a
    # configuration decision: with no import roots and no allowlisted producer schema the
    # session refuses every import.
    register_tool = callable(getattr(ctx, "register_tool", None))
    modes.append(
        supported(
            "artifact_import",
            evidence=(
                "ctx.register_tool exposes context_shunt_import; the import boundary is "
                "implemented in context_shunt.artifacts",
                "an already-persisted artifact needs no pre-truncation capture, so this "
                "mode makes no claim about post-tool interception",
            ),
        )
        if register_tool
        else unsupported("artifact_import", DisabledReason.HOOK_MISSING)
    )

    # Oversized tool-result capture: see _tool_result_capture_mode's docstring for why
    # this is not a blanket claim even though the ordering was directly verified on one
    # live host.
    modes.append(_tool_result_capture_mode(ctx))

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


def _tool_result_capture_mode(ctx: Any = None) -> ModeCapability:
    """Whether ``tool_result_capture`` (oversized-tool-result capture) is supported.

    Deliberately not a blanket claim. A prior version of this adapter reported the mode
    unsupported unconditionally, citing hermes-agent 0.18.2 documentation:
    ``transform_tool_result`` receives the result "post-truncation and post-ANSI-strip"
    (``CAPTURE_AFTER_TRUNCATION``), and the host wraps the hook dispatch in try/except, so
    a raising handler leaves the original result in place (``HOST_FAIL_OPEN``).

    Direct, read-only inspection and an isolated exact-image probe of the operator's
    Hermes 0.21.3 host on 2026-09-17 found a stronger official route: authorized dispatch
    runs inside ``tool_execution`` middleware before the result reaches context. The
    middleware is concurrency-safe, unlike the host's bounded transform-hook dispatcher.
    This remains evidence about one exact host version: a registry tool could still
    self-truncate before returning. The compatibility transform route remains fail-safe,
    and the middleware wrapper converts unexpected oversized-transform failures to a fixed
    envelope because Hermes otherwise preserves a completed downstream result.

    So: unsupported by default (``ORDERING_UNPROVEN`` - a narrower, honest reason than the
    stale ``CAPTURE_AFTER_TRUNCATION`` claim), *unless* the deployment sets both existing
    attestations. The consumer attestation is retained as an explicit rollout interlock:
    it says the operator ran the exact-host observer canary, not merely that plugin tools
    appeared in global config. Hermes 0.21.3 exposes the provider-bound request and
    immutable request ids through official hooks, so no core patch is needed.
    """
    ordering_attested = bool(
        _config is not None
        and _config.tool_result_capture.host_ordering_verified_locally
    )
    consumer_scope_attested = bool(
        _config is not None
        and _config.tool_result_capture.host_consumer_scope_verified_locally
    )
    hooks = _supported_hooks(ctx) if ctx is not None else set()
    provider_request_observer = "pre_api_request" in hooks
    middleware_route = provider_request_observer and callable(
        getattr(ctx, "register_middleware", None)
    )
    legacy_transform_route = "transform_tool_result" in hooks
    fail_open_evidence = (
        "hermes-agent model_tools.py: _apply_transform_tool_result_hook runs inside "
        "try/except and the original result survives a raising handler (fail-open); "
        "this adapter's own hook handler never raises regardless"
    )
    if ordering_attested and consumer_scope_attested and (
        middleware_route or legacy_transform_route
    ):
        scope_evidence = (
            "official Hermes pre_api_request observer supplies the post-middleware "
            "provider-bound tools array plus immutable session/turn/api_request ids; "
            "official tool_execution middleware wraps authorized dispatch without the "
            "bounded-hook single-flight suppression and publishes a pointer only on an "
            "exact id match"
            if middleware_route
            else
            "operator attestation: host_consumer_scope_verified_locally=true - the "
            "installed host forwards a fresh invocation-scoped direct/deferred tool "
            "descriptor to transform_tool_result"
        )
        replacement_evidence = (
            "official tool_execution middleware runs inside the authorized dispatch "
            "chain before the result reaches context; Hermes otherwise returns a "
            "completed downstream result when middleware raises, so this adapter catches "
            "unexpected oversized-transform failures and emits a fixed bounded envelope"
            if middleware_route
            else fail_open_evidence
        )
        return supported(
            "tool_result_capture",
            evidence=(
                "operator attestation: tool_result_capture.host_ordering_verified_locally="
                "true - this adapter does not independently verify the installed host's "
                "hook ordering; the operator has",
                scope_evidence + "; absent, truncated, stale, partial, or incomplete "
                "evidence remains no-handle legacy compaction",
                "read-only inspection and the isolated exact-image probe on live Hermes "
                "0.21.3 (2026-09-17) found authorized dispatch inside tool_execution "
                "middleware with no host truncation before replacement "
                "(docs/capability-matrix.md); per-tool self-truncation upstream of that "
                "layer was not audited",
                "capture classifier: read_file/search_files plus operator exact "
                "capture_tool_allowlist only; protected instructions/control/catalogs "
                "and unknown tools pass verbatim; MCP read_resource requires allowlisting "
                "because executed-utility provenance is unavailable at the hook",
                replacement_evidence,
            ),
        )
    reasons = []
    if not ordering_attested:
        reasons.append(DisabledReason.ORDERING_UNPROVEN)
    if not consumer_scope_attested:
        reasons.append(DisabledReason.CONSUMER_SCOPE_UNPROVEN)
    if not middleware_route and not legacy_transform_route:
        reasons.append(DisabledReason.HOOK_MISSING)
    return unsupported(
        "tool_result_capture",
        *reasons,
        evidence=(
            "missing capture-ordering and/or consumer-scope rollout attestation: "
            "enabled=true alone never registers the transform hook; each invocation "
            "still needs the official correlated request evidence or a legacy immutable "
            "descriptor",
            fail_open_evidence,
        ),
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
            "transform_tool_result",
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

    Only the public ``hermes_cli.config.load_config`` is used. This computes the target the
    core requests and validates against the returned route. Current task-aware hosts apply
    the same block themselves; on older hosts it also supplies the compatibility override.
    In both cases **user config wins over the plugin defaults.**
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
    "timeout": 45,
}


def _llm_accepts_task(llm: Any) -> bool:
    """Whether the public bound ``complete`` signature explicitly offers ``task``.

    Do not probe by calling with the keyword: an internal ``TypeError`` after dispatch
    would otherwise make a compatibility retry duplicate a billable provider call.
    A wrapper exposing only ``**kwargs`` is treated as the older ceiling, fail-closed.
    """
    complete = getattr(llm, "complete", None)
    if not callable(complete):
        return False
    try:
        return "task" in inspect.signature(complete).parameters
    except (TypeError, ValueError):
        return False


# -- normalization ---------------------------------------------------------


def normalize_tool_call(
    tool_name: str, args: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Map a Hermes tool call onto the core's ``(tool, args)`` shape."""
    name = normalize_tool_identity(tool_name)
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
        # `build_provider` assembles the primary *and* the configured availability
        # fallback chain. Constructing `HostBridgeProvider` directly here meant
        # `reader.fallback_chain` parsed, validated and documented - and then did
        # nothing at all, so a deployment that configured a fallback silently had none.
        provider = (
            build_provider(
                _config,
                _bridge_call,
                provider=_reader_target()[0],
                model=_reader_target()[1],
            )
            if _llm is not None
            else UnavailableProvider("HOST_LLM_UNAVAILABLE")
        )
        session = ShuntSession(
            key,
            _config,
            _capability,
            provider=provider,
            metrics=_metrics,
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

    Current ``PluginLlm.complete(task=...)`` populates its result from the auxiliary
    router's ``route_info`` and provider response; a matching ``audit.task`` proves this
    call used that surface, so provider/model are reported as host-resolved routing facts.
    The task-agnostic compatibility result stays ``reported_*`` because it cannot separate
    a provider report from an echo of the requested override. Neither is provider proof.
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
    task_aware = _llm_accepts_task(_llm)
    if task_aware:
        kwargs["task"] = AUX_TASK_KEY
    result = _llm.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        **kwargs,
    )
    usage = getattr(result, "usage", None)
    reported_model = getattr(result, "model", "") or ""
    reported_provider = getattr(result, "provider", "") or ""
    audit = getattr(result, "audit", None)
    task_routed = (
        task_aware
        and isinstance(audit, dict)
        and audit.get("task") == AUX_TASK_KEY
    )
    return {
        "text": getattr(result, "text", "") or "",
        "reported_provider": None if task_routed else reported_provider or None,
        "reported_model": None if task_routed else reported_model or None,
        "resolved_provider": (reported_provider or None) if task_routed else None,
        "resolved_model": (reported_model or None) if task_routed else None,
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
    """Veto only a provably oversized unbounded read before the tool runs.

    The gate is advisory. Any classification, probe or session failure leaves the
    original host call in charge, so a plugin defect cannot become a synthetic tool error.
    """
    if _config is None or not _config.gate_enabled:
        return None
    try:
        # Normalization is part of the advisory boundary too. Hermes can hand plugins
        # malformed values, and such a value must stay a host-owned call rather than
        # becoming a plugin exception/error.
        tool, normalized = normalize_tool_call(tool_name, args or {})
        if tool == "other":
            return None
        session = _session(task_id, session_id)
        decision = session.evaluate_tool_call(tool, normalized)
    except Exception:
        # The gate must never manufacture an error when it cannot evaluate a call. The
        # host tool owns malformed arguments, unavailable paths, and its own failures.
        return None
    if not decision.blocked:
        return None
    try:
        envelope = session.block_envelope(_request_id(kwargs), decision)
        return {"action": "block", "message": _block_message(envelope)}
    except Exception:
        # A positive gate decision is useful only if its bounded block envelope can be
        # published. Keep the host call in charge when this final advisory step fails.
        return None


def _request_id(kwargs: dict[str, Any]) -> str:
    raw = str(kwargs.get("tool_call_id") or kwargs.get("turn_id") or "gate")
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "_.:-")[:56]
    return f"req_{safe or 'gate'}"


def _block_message(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def transform_tool_result(
    tool_name: str = "",
    args: dict | None = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **kwargs,
) -> str | None:
    """Replace an eligible oversized result with a bounded consumable representation.

    Registered only when the capability probe reports ``tool_result_capture`` supported -
    which requires both explicit operator attestations (see ``_tool_result_capture_mode``),
    never assumed. Runs at Hermes' ``transform_tool_result`` hook: after the tool executed
    and after ``post_tool_call`` fired, before the result enters context.

    Two-step by design, not by choice: the host does not forward the conversation's
    ``user_task`` to this hook at all (verified against the same live host this adapter's
    ordering evidence comes from), so there is no question here to answer with. When
    caller reachability is proven, this handler captures and points;
    ``context_shunt_read`` - which does carry an explicit question - answers it afterward,
    exactly like the artifact-import boundary's own capture-then-ask shape. Without that
    proof, it publishes bounded deterministic compaction and retains no pointer payload.

    Protected and unclassified identities pass verbatim before any capture work. Only
    explicitly eligible results reach measurement. For these candidates, never raises,
    and never returns ``None`` for an internal failure after measurement as oversized:
    Hermes wraps this dispatch in try/except and lets a raising handler's *original, raw*
    result through unchanged (host-level fail-open, confirmed at 0.21.3) - this handler
    must never depend on that safety net. The size check below is done directly, before
    touching the session, specifically so an internal failure on a small, ineligible result
    can cheaply return ``None`` (nothing was ever at risk of leaking), while a genuinely
    oversized result that then fails internally still returns a bounded failure envelope,
    never falls through to the host's raw passthrough.
    """
    if classify_tool_result(tool_name, _capture_tool_allowlist) != "eligible":
        return None
    if _config is None or not _capability.enabled("tool_result_capture"):
        return None
    if not isinstance(result, str):
        # Structured/multimodal results (images, tool content blocks) are left alone at
        # this hook - the same reasoning the incumbent compactor documented: replacing a
        # content block here persists the replacement before the main model can inspect it
        # once. SpillEngine's own conformance corpus (spill-cases.json) still exercises
        # structured-result handling for the paths that do reach it (context_shunt_import
        # today; a future controlled producer wrapper could reach it here too).
        return None
    try:
        oversized = len(result.encode("utf-8")) > _config.limits.max_tool_result_bytes
    except Exception:
        oversized = False
    if not oversized:
        # Not an eligible candidate. SpillEngine would reach the same passthrough verdict
        # itself; checking it here first avoids session/store construction on the
        # overwhelmingly common small-result path, and means nothing was ever at risk of
        # leaking raw if the check below happens to fail.
        return None

    request_id = _request_id({"tool_call_id": tool_call_id, **kwargs})
    consumer_route = _consumer_route(kwargs.get("consumer_capabilities"))
    if consumer_route is None:
        consumer_route = _consumer_route_from_request(
            tool_name,
            {
                "session_id": session_id,
                "task_id": task_id,
                "turn_id": kwargs.get("turn_id"),
                "api_request_id": kwargs.get("api_request_id"),
            },
        )
    if consumer_route is None:
        try:
            return _block_message(
                _session(task_id, session_id).compact_tool_result_without_consumer(
                    request_id,
                    result,
                    upstream_truncated=bool(kwargs.get("upstream_truncated", False)),
                )
            )
        except Exception:
            return _block_message(
                compact_failure(
                    request_id,
                    result.encode("utf-8"),
                    ShuntError("SPILL_FAILED", "CONSUMER_UNAVAILABLE", retryable=False),
                    limits=_config.limits,
                    hard_chars=_config.reader.legacy_compaction_max_chars,
                )
            )
    try:
        session = _session(task_id, session_id)
        outcome = session.post_tool_result(
            request_id, result, consumer_route=consumer_route
        )
    except Exception as raw_exc:
        failure = (
            raw_exc
            if isinstance(raw_exc, ShuntError)
            else ShuntError("HOST_UNSAFE", "INTERNAL_ERROR")
        )
        return _block_message(
            compact_failure(
                request_id,
                result.encode("utf-8"),
                failure,
                limits=_config.limits,
                hard_chars=_config.reader.legacy_compaction_max_chars,
            )
        )
    if outcome is None or outcome.action == "passthrough":
        # `None` means the mode is disabled at the session level (config.enabled=false,
        # already excluded above via the capability check, kept as defense in depth);
        # `passthrough` means the session's own SpillEngine independently judged this
        # ineligible - its check is authoritative, this hook's own pre-check above is only
        # a cheap way to skip session construction for the common case.
        return None
    if outcome.envelope is None:
        # Every non-passthrough SpillOutcome carries a bounded envelope; this should be
        # unreachable, but refuse rather than pass anything unbounded through if it isn't.
        return _block_message(
            compact_failure(
                request_id,
                result.encode("utf-8"),
                ShuntError("HOST_UNSAFE", "INTERNAL_ERROR"),
                limits=_config.limits,
                hard_chars=_config.reader.legacy_compaction_max_chars,
            )
        )
    return _block_message(outcome.envelope)


def capture_tool_execution(
    tool_name: str = "",
    args: dict | None = None,
    next_call: Any = None,
    **kwargs: Any,
) -> Any:
    """Official Hermes tool-execution middleware for concurrency-safe capture.

    Hermes invokes this after authorization and supplies the same immutable request ids
    observed at ``pre_api_request``. Unlike bounded lifecycle hooks, execution middleware
    does not suppress a concurrent invocation of the same callback. The downstream call is
    made exactly once. A Shunt transform failure after an oversized eligible result is
    converted by ``transform_tool_result`` to a bounded envelope, never returned raw.
    """
    if not callable(next_call):
        raise TypeError("tool execution middleware requires next_call")
    result = next_call(args if isinstance(args, dict) else {})
    must_bound = bool(
        _config is not None
        and _capability is not None
        and _capability.enabled("tool_result_capture")
        and classify_tool_result(tool_name, _capture_tool_allowlist) == "eligible"
        and isinstance(result, str)
        and len(result.encode("utf-8", errors="replace"))
        > _config.limits.max_tool_result_bytes
    )
    try:
        replacement = transform_tool_result(
            tool_name=tool_name,
            args=args,
            result=result,
            task_id=str(kwargs.get("task_id") or ""),
            session_id=str(kwargs.get("session_id") or ""),
            tool_call_id=str(kwargs.get("tool_call_id") or ""),
            turn_id=kwargs.get("turn_id"),
            api_request_id=kwargs.get("api_request_id"),
        )
    except Exception:
        # Hermes intentionally returns the already-completed downstream result when
        # execution middleware raises. For an oversized owned candidate that would be a
        # raw leak, so collapse even an unexpected adapter bug to the smallest legal
        # envelope inside this callback.
        if must_bound:
            return _block_message(
                fixed_error(_request_id(kwargs), code="HOST_UNSAFE")
            )
        raise
    if must_bound and replacement is None:
        return _block_message(fixed_error(_request_id(kwargs), code="HOST_UNSAFE"))
    return result if replacement is None else replacement


def _consumer_route(raw: Any) -> str | None:
    """Return a proven direct/deferred route from bounded host-supplied facts.

    This is the compatibility path for a host that supplies a descriptor directly.
    Unmodified Hermes 0.21.3 instead uses ``_consumer_route_from_request`` and the official
    provider-request observer below. Neither path reconstructs authority from global
    registration, and both validate bounded immutable facts.
    """
    if not isinstance(raw, Mapping) or set(raw) - {"direct_tools", "deferred_tools"}:
        return None

    def names(value: Any) -> frozenset[str] | None:
        if not isinstance(value, (list, tuple)) or len(value) > 128:
            return None
        normalized = []
        for item in value:
            name = normalize_tool_identity(item)
            if not name or len(name) > 128:
                return None
            normalized.append(name)
        return frozenset(normalized)

    direct = names(raw.get("direct_tools"))
    deferred = names(raw.get("deferred_tools"))
    if direct is None or deferred is None:
        return None
    consumers = {"context_shunt_read", "context_shunt_inspect"}
    if consumers <= direct:
        return "direct"
    bridges = {"tool_search", "tool_describe", "tool_call"}
    if bridges <= direct and consumers <= deferred:
        return "deferred"
    return None


def _consumer_route_from_request(tool_name: Any, ids: Mapping[str, Any]) -> str | None:
    """Resolve a route from exact provider-request evidence, never global availability."""
    key = _request_scope_key(ids)
    if key is None:
        return None
    now = time.monotonic()
    with _request_scopes_lock:
        evidence = _request_scopes.get(key)
        if evidence is None:
            return None
        observed_at, direct, deferred = evidence
        if observed_at < now - _REQUEST_SCOPE_TTL_SECONDS:
            _request_scopes.pop(key, None)
            return None
    executed = normalize_tool_identity(tool_name)
    if not executed or executed not in direct | deferred:
        # The ids alone are insufficient if this tool was not actually offered in the
        # correlated request. This also rejects direct host/test calls that borrow ids.
        return None
    consumers = {"context_shunt_read", "context_shunt_inspect"}
    if consumers <= direct:
        return "direct"
    bridges = {"tool_search", "tool_describe", "tool_call"}
    if bridges <= direct and consumers <= deferred:
        return "deferred"
    return None


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
    _forget_request_scopes(session_id)
    session = _sessions.pop(key, None)
    if session is not None:
        session.close()


def on_session_reset(session_id: str = "", **kwargs):
    """``/reset`` and ``/new``: bump the generation so old handles cannot be replayed."""
    key = session_id or str(kwargs.get("task_id") or "unbound")
    _forget_request_scopes(session_id)
    session = _sessions.pop(key, None)
    _generations[key] = _generations.get(key, 1) + 1
    if session is not None:
        session.close()


# -- tools -----------------------------------------------------------------


def context_shunt_read(args: dict[str, Any] | None = None, **kwargs) -> str:
    """Answer a question about a source. Read-only; returns a verified envelope.

    Exactly one source form: ``paths`` for an initial capture, or ``handles`` for a
    refined question over snapshots the caller already holds. A refined question reuses
    the named immutable snapshot and never recaptures the source.
    """
    public_args, host_kwargs = _tool_invocation_args(args, kwargs, "context_shunt_read")
    request_id = _request_id(host_kwargs)

    try:
        tool_args = _validate_tool_args(public_args)
    except ShuntError as exc:
        return _accounted_tool_error(request_id, exc, "context_shunt_read", host_kwargs)

    try:
        session = _session(
            str(host_kwargs.get("task_id") or ""),
            str(host_kwargs.get("session_id") or ""),
        )
    except Exception as raw_exc:
        failure = (
            raw_exc
            if isinstance(raw_exc, ShuntError)
            else ShuntError("STORE_FAILED", "INTERNAL_ERROR")
        )
        if "paths" in tool_args:
            return _block_message(
                compact_paths_failure(request_id, tool_args["paths"], failure, _config)
            )
        return _error(request_id, failure)

    try:
        if "paths" in tool_args:
            entries = session.capture_read_paths(
                request_id, [str(p) for p in tool_args["paths"]]
            )
            if isinstance(entries, dict):
                return _block_message(entries)
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


_TRUSTED_HOST_TOOL_KWARGS = frozenset(
    {"task_id", "session_id", "tool_call_id", "turn_id", "user_task"}
)


def _tool_invocation_args(
    args: dict[str, Any] | None, kwargs: dict[str, Any], tool: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep Hermes runtime metadata separate from strict caller arguments.

    Hermes passes the model-produced argument dictionary positionally and adds its own
    routing context as keyword arguments. Reserved names inside the positional dictionary
    remain caller input and therefore fail the public schema instead of impersonating host
    metadata.
    """
    public = dict(args or {})
    host = {}
    for key, value in kwargs.items():
        if key in _TRUSTED_HOST_TOOL_KWARGS:
            host[key] = value
        else:
            public[key] = value
    if tool == "context_shunt_read":
        public = _repair_stringified_handles(public)
    return {"tool": tool, **public}, host


def _repair_stringified_handles(public: dict[str, Any]) -> dict[str, Any]:
    """Repair only bounded JSON serialization accidents; strict validation follows.

    Python literals, executable syntax and guessed identifiers are never accepted. The
    ordinary public schema remains authoritative after this transport normalization.
    """
    raw = public.get("handles")
    if isinstance(raw, str):
        try:
            raw_bytes = len(raw.encode("utf-8"))
        except UnicodeEncodeError:
            return public
        if raw_bytes > 16_384:
            return public
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return public
        if isinstance(decoded, dict):
            decoded = [decoded]
        if not isinstance(decoded, list):
            return public
        return {**public, "handles": decoded}
    if isinstance(raw, list) and any(isinstance(item, str) for item in raw):
        if len(raw) > 8:
            return public
        repaired = []
        try:
            for item in raw:
                if isinstance(item, str):
                    try:
                        item_bytes = len(item.encode("utf-8"))
                    except UnicodeEncodeError:
                        return public
                    if item_bytes > 4096:
                        return public
                    item = json.loads(item)
                repaired.append(item)
        except (TypeError, ValueError):
            return public
        return {**public, "handles": repaired}
    return public


def _validate_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    return validate_tool_args(args)


def context_shunt_inspect(args: dict[str, Any] | None = None, **kwargs) -> str:
    """Exact bounded extraction or JSON aggregation from a handle. Zero model calls."""
    public_args, host_kwargs = _tool_invocation_args(
        args, kwargs, "context_shunt_inspect"
    )
    request_id = _request_id(host_kwargs)
    try:
        tool_args = _validate_tool_args(public_args)
    except ShuntError as exc:
        return _accounted_tool_error(request_id, exc, "context_shunt_inspect", host_kwargs)
    session = _session(
        str(host_kwargs.get("task_id") or ""),
        str(host_kwargs.get("session_id") or ""),
    )

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
                    tool_args.get("max_result_bytes")
                    or _config.limits.inspect_max_result_bytes
                ),
                _config.limits.inspect_max_result_bytes,
            ),
            "max_scan_lines": min(
                int(
                    tool_args.get("max_scan_lines")
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
    public_args, host_kwargs = _tool_invocation_args(
        args, kwargs, "context_shunt_stats"
    )
    request_id = _request_id(host_kwargs)
    try:
        tool_args = _validate_tool_args(public_args)
    except ShuntError as exc:
        return _accounted_tool_error(request_id, exc, "context_shunt_stats", host_kwargs)
    session = _session(
        str(host_kwargs.get("task_id") or ""),
        str(host_kwargs.get("session_id") or ""),
    )
    request: dict[str, Any] = {
        "schema_version": EMITTED_SCHEMA_VERSION,
        "request_id": request_id,
        "operation": "stats",
    }
    for key in ("page", "page_size"):
        if key in tool_args:
            request[key] = tool_args[key]
    try:
        return _block_message(session.stats(request))
    except Exception:
        return _block_message(fixed_error(request_id, "HOST_UNSAFE"))


def context_shunt_import(args: dict[str, Any] | None = None, **kwargs) -> str:
    """Adopt an external producer's already-persisted tool-result artifact.

    Takes a manifest path, not a payload: the agent never has to hold the artifact, and
    the core re-proves every claim the manifest makes before a handle exists. Returns a
    pointer envelope - never the artifact's bytes.
    """
    public_args, host_kwargs = _tool_invocation_args(
        args, kwargs, "context_shunt_import"
    )
    request_id = _request_id(host_kwargs)
    try:
        tool_args = _validate_tool_args(public_args)
    except ShuntError as exc:
        return _accounted_tool_error(request_id, exc, "context_shunt_import", host_kwargs)
    session = _session(
        str(host_kwargs.get("task_id") or ""),
        str(host_kwargs.get("session_id") or ""),
    )
    try:
        return _block_message(
            session.import_artifact(
                request_id, manifest_path=str(tool_args["manifest_path"]).strip()
            )
        )
    except ShuntError as exc:
        return _error(request_id, exc)
    except Exception:
        return _block_message(fixed_error(request_id, "HOST_UNSAFE"))


def _error(request_id: str, exc: ShuntError) -> str:
    from context_shunt import envelope as E

    return _block_message(
        enforce_or_fixed(
            E.error_envelope(request_id, exc), _config.limits
        )
    )


def _accounted_tool_error(
    request_id: str,
    exc: ShuntError,
    tool: str,
    host_kwargs: dict[str, Any],
) -> str:
    """Best-effort accounting for a handler invocation the plugin itself rejected."""
    try:
        session = _session(
            str(host_kwargs.get("task_id") or ""),
            str(host_kwargs.get("session_id") or ""),
        )
        return _block_message(session.reject_tool_call(request_id, exc, tool))
    except Exception:
        # Store/bootstrap/accounting failure must not obscure the bounded caller error.
        return _error(request_id, exc)


@cache
def _tool_args_contract() -> dict[str, Any]:
    """Load the synchronized canonical tool contract used by both cores."""
    with (CONTRACTS_DIR / "tool-args.schema.json").open("rb") as fh:
        return json.load(fh)


def _inline_contract_refs(
    value: Any, definitions: dict[str, Any], active: tuple[str, ...] = ()
) -> Any:
    """Inline local ``$defs`` references for hosts that validate parameters in isolation."""
    if isinstance(value, list):
        return [_inline_contract_refs(item, definitions, active) for item in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if ref is not None:
        prefix = "#/$defs/"
        if not isinstance(ref, str) or not ref.startswith(prefix):
            raise ValueError("tool-args contract contains an unsupported reference")
        name = ref[len(prefix) :]
        if name not in definitions or name in active:
            raise ValueError("tool-args contract contains an invalid reference")
        resolved = _inline_contract_refs(
            deepcopy(definitions[name]), definitions, (*active, name)
        )
        if len(value) > 1:
            if not isinstance(resolved, dict):
                raise ValueError(
                    "tool-args contract reference has unsupported siblings"
                )
            resolved.update(
                {
                    key: _inline_contract_refs(item, definitions, active)
                    for key, item in value.items()
                    if key != "$ref"
                }
            )
        return resolved
    return {
        key: _inline_contract_refs(item, definitions, active)
        for key, item in value.items()
    }


def _registered_tool_parameters(definition_name: str) -> dict[str, Any]:
    """Project one canonical tool definition onto Hermes' arguments-only schema."""
    contract = _tool_args_contract()
    definitions = contract.get("$defs")
    if not isinstance(definitions, dict) or definition_name not in definitions:
        raise ValueError("tool-args contract is missing the registered tool definition")
    parameters = _inline_contract_refs(
        deepcopy(definitions[definition_name]), definitions
    )
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or "tool" not in properties:
        raise ValueError("tool-args contract tool definition has no discriminator")
    properties.pop("tool")
    parameters["properties"] = properties
    required = parameters.get("required")
    if isinstance(required, list):
        required = [name for name in required if name != "tool"]
        if required:
            parameters["required"] = required
        else:
            parameters.pop("required", None)
    return parameters


READER_TOOL_SCHEMA = {
    "name": "context_shunt_read",
    "description": (
        "Answer a question about one or more large files without pulling them into this "
        "conversation. Returns a generated answer with citation quotes mechanically "
        "matched to snapshot bytes; that check does not prove the prose. Uses a "
        "reader model. Shunt-owned failures automatically return labelled bounded deterministic "
        "legacy compaction with incomplete coverage. Read-only. Pass paths for a first look, or "
        "handles to ask a sharper semantic question about a snapshot you already hold. For "
        "structured logs, metrics, exact counts, distinct values, or grouping, prefer "
        "context_shunt_inspect aggregation so no semantic reader fan-out is required."
    ),
    "parameters": _registered_tool_parameters("readArgs"),
}

INSPECT_TOOL_SCHEMA = {
    "name": "context_shunt_inspect",
    "description": (
        "Return exact text or bounded JSON aggregation from a snapshot you already hold: a "
        "line range, byte range, literal-search hits, or deterministic count/distinct/grouping. "
        "Use aggregation for structured logs, metrics, and exact counting; use the reader for "
        "genuinely semantic questions. No model is involved, so the result is source evidence "
        "rather than a summary. Each "
        "page is capped at 16 KiB and counts against a "
        "cumulative disclosure budget, so a large file cannot be paged into a full copy; a "
        "file small enough to fit that budget can be returned in full. For minified one-line "
        "sources, use literal search, then a 0-based half-open UTF-8 byte range; continue only "
        "with the identical selector and returned next_cursor."
    ),
    "parameters": _registered_tool_parameters("inspectArgs"),
}

STATS_TOOL_SCHEMA = {
    "name": "context_shunt_stats",
    "description": (
        "Report this session's context-shunt accounting: bytes withheld, tokens saved or "
        "spent, and per-operation records. Read-only - it cannot reset a counter, change "
        "retention, see another session, or reveal any source content."
    ),
    "parameters": _registered_tool_parameters("statsArgs"),
}

IMPORT_TOOL_SCHEMA = {
    "name": "context_shunt_import",
    "description": (
        "Adopt a large tool-result artifact that a producer already wrote to disk, so it "
        "never enters this conversation. Takes the path of the producer's manifest, "
        "inside a configured import root; the manifest's size and digest claims are "
        "re-proven against the file before anything is accepted. Returns an opaque handle "
        "and metadata - never the artifact's contents. Read it afterwards with "
        "context_shunt_read for a cited answer, or context_shunt_inspect for exact lines."
    ),
    "parameters": _registered_tool_parameters("importArgs"),
}

TOOLS = (
    (READER_TOOL_SCHEMA, context_shunt_read, "reader"),
    (INSPECT_TOOL_SCHEMA, context_shunt_inspect, "deterministic_inspect"),
    (STATS_TOOL_SCHEMA, context_shunt_stats, "session_stats"),
    (IMPORT_TOOL_SCHEMA, context_shunt_import, "artifact_import"),
)


# -- registration ----------------------------------------------------------


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    global _config, _capability, _llm, _store, _capture_tool_allowlist, _metrics

    for session in _sessions.values():
        session.close()
    _sessions.clear()
    with _request_scopes_lock:
        _request_scopes.clear()
    raw = dict(_load_plugin_config(ctx))
    _capture_tool_allowlist = frozenset()
    allowlist = raw.pop("capture_tool_allowlist", [])
    if not isinstance(allowlist, list) or any(
        not isinstance(name, str) or not normalize_tool_identity(name)
        for name in allowlist
    ):
        raise ValueError(
            "capture_tool_allowlist must be a list of nonempty exact tool identities"
        )
    _capture_tool_allowlist = frozenset(
        normalize_tool_identity(name) for name in allowlist
    )
    import os

    default_cache = Path(
        os.environ.get("CONTEXT_SHUNT_CACHE", Path.home() / ".cache" / "context-shunt")
    )
    _config = load_config(raw, default_spill_dir=default_cache)

    _llm = getattr(ctx, "llm", None)
    log = getattr(ctx, "logger", None)
    _metrics = _ReaderMetrics(log) if log is not None else None
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

    # A store outage must not prevent registration of the capture fallback hook.
    try:
        _store = SnapshotStore(_config.cache_root, _config.limits)
        _store.recover()
    except Exception:
        _store = None

    if _capability.enabled("local_gate"):
        ctx.register_hook("pre_tool_call", pre_tool_call)
    hooks = _supported_hooks(ctx)
    ctx.register_hook("on_session_end", on_session_end)
    if "on_session_finalize" in hooks:
        ctx.register_hook("on_session_finalize", on_session_finalize)
    if "on_session_reset" in hooks:
        ctx.register_hook("on_session_reset", on_session_reset)
    if (
        _capability.enabled("tool_result_capture")
        and _config.tool_result_capture.enabled
    ):
        register_middleware = getattr(ctx, "register_middleware", None)
        if callable(register_middleware) and "pre_api_request" in hooks:
            ctx.register_hook("pre_api_request", observe_provider_request)
            ctx.register_middleware("llm_execution", observe_llm_execution)
            register_middleware("tool_execution", capture_tool_execution)
        elif "transform_tool_result" in hooks:
            # Compatibility route for an older/operator-attested host that supplies an
            # immutable consumer_capabilities descriptor directly to the transform hook.
            ctx.register_hook("transform_tool_result", transform_tool_result)

    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        for schema, handler, mode in TOOLS:
            if not _capability.enabled(mode):
                continue
            if mode == "deterministic_inspect" and not _config.tools.inspect_enabled:
                continue
            if mode == "session_stats" and not _config.tools.stats_enabled:
                continue
            if mode == "artifact_import" and not _config.artifact_import.enabled:
                # Registering an import tool a deployment never authorized would put a
                # permanently-refusing surface in front of the model.
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
