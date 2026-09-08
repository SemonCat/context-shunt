"""A role-preserving, cap-enforcing reader bridge over the OpenClaw host's own model access.

This is the route the release gates require, and it exists because the CLI bridge cannot be
one. ``openclaw infer model run`` takes a single ``--prompt`` and has no output-token flag,
so :mod:`bridges.openclaw_cli` concatenates the reader's system prompt into the user turn
and silently drops ``max_output_tokens``. A score obtained that way describes neither the
prompt production sends nor the ceiling production imposes, in either direction: removing a
role boundary changes what the model sees, and an unbounded completion is not the bounded
call the reader budgets for. Both are recorded on that module; neither is fixable there.

What this route does instead
----------------------------
It drives :mod:`openclaw_inhost_server` (``openclaw_inhost_server.mts``) over
newline-delimited JSON. That server calls the host's own completion runtime with

* ``context.systemPrompt`` and a single user message - the shape
  ``adapters/openclaw/index.ts`` uses, and the shape the host's isolated-runtime policy
  *requires* (``runtime-llm-isolated.ts`` rejects system instructions passed any other
  way); and
* ``options.maxTokens`` set from the reader's own per-call ceiling, so the cap the reader
  budgets for is the cap the provider is given.

It also forwards the host's ``usage`` block when the host reports one, which is what makes
the token half of ``benchmark provider`` measurable at all. Absent usage stays absent: no
key is sent, the reader records ``usage_complete: false`` with null counts, and nothing is
laundered into a zero.

What it is *not*
----------------
Not production-equivalent, and this module does not claim to be. Production reaches the
model through the agent-harness isolated runtime (``runIsolatedCompletion``); this server
uses the simple-completion transport, which resolves the same credentials through the
host's own profile store but is a different code path. ``BRIDGE`` says so in a field the
release attestation records verbatim, so no report can imply the harness path was
exercised. Role preservation and cap enforcement are the two properties the release gates
require and the two this route actually provides.

Secrets
-------
Nothing here reads, stores, logs or forwards a credential. The server resolves auth inside
the host process; this module speaks only prompts and completions to it, writes no prompt
to any log, and reduces every host error to a bounded type string.

Usage
-----
    CONTEXT_SHUNT_LUNA_EVAL=1 \
    CONTEXT_SHUNT_OPENCLAW_ROOT=/path/to/openclaw \
    CONTEXT_SHUNT_LUNA_BRIDGE=bridges.openclaw_inhost:complete \
      ./scripts/verify eval luna

Environment:
    CONTEXT_SHUNT_OPENCLAW_ROOT      the OpenClaw checkout (required)
    CONTEXT_SHUNT_OPENCLAW_ROUTE     "<provider>/<model>" (default: the Luna route below)
    CONTEXT_SHUNT_OPENCLAW_AGENT     agent whose auth store is used (default: "main")
    CONTEXT_SHUNT_OPENCLAW_TSX       the TypeScript loader (default: the host's own tsx)
    CONTEXT_SHUNT_OPENCLAW_SERVER    the NDJSON server to drive (default: the .mts beside
                                     this file). Overridden by the ``bridge-contract``
                                     gate, which drives this protocol against a stub so
                                     the route's two claimed properties - separate roles
                                     and a forwarded output cap - are *verified* rather
                                     than asserted in a descriptor nobody checks.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

#: The host route that serves gpt-5.6-luna. `sub2api-openai` is a plain
#: openai-completions provider; the `openai/` route for the same model is bound to a
#: different agent runtime, so it is not interchangeable.
DEFAULT_ROUTE = "sub2api-openai/gpt-5.6-luna"

#: Loading the host's config and model catalogue costs tens of seconds, once. The server is
#: spawned on the first call and kept, rather than paying that per call.
STARTUP_TIMEOUT_S = 180.0

#: Populated with one entry per call so a benchmark can report real latency. Never holds a
#: prompt, a completion, or any source text - only timings.
LATENCIES_MS: list[int] = []

#: What this route can and cannot do, read by the release gates and recorded verbatim in
#: the attestation. `preserves_roles` and `enforces_output_cap` are the two properties a
#: required live gate refuses to score without: a route that fails either is measuring
#: something other than what the reader does in production.
BRIDGE: dict[str, Any] = {
    "id": "openclaw_inhost",
    "host": "openclaw",
    "route_kind": "in_host_simple_completion",
    "preserves_roles": True,
    "enforces_output_cap": True,
    "forwards_usage": True,
    # The honest remaining gap, stated in the descriptor rather than in prose only:
    # production uses the isolated agent runtime, this route uses simple-completion.
    "production_equivalent": False,
    "production_gap": (
        "production reaches the model through the isolated agent runtime "
        "(runIsolatedCompletion); this route uses the host's simple-completion transport"
    ),
}


class BridgeError(RuntimeError):
    """The host could not serve the call. Carries no prompt or completion text."""


def _root() -> str:
    root = os.environ.get("CONTEXT_SHUNT_OPENCLAW_ROOT", "")
    if not root or not Path(root).is_dir():
        raise BridgeError("set CONTEXT_SHUNT_OPENCLAW_ROOT to an OpenClaw checkout")
    return root


def _loader(root: str) -> str:
    """The TypeScript loader. The host's own by default, because the server imports host
    modules by their ``.js`` specifiers, which only a TS-aware resolver maps to ``.ts``."""
    configured = os.environ.get("CONTEXT_SHUNT_OPENCLAW_TSX", "")
    if configured:
        return configured
    return str(Path(root) / "node_modules" / ".bin" / "tsx")


class _Server:
    """One long-lived NDJSON conversation with the in-host completion server.

    The server is spawned with the host checkout as its working directory. That is not
    incidental: the host is a pnpm workspace, and its own packages resolve only from
    inside it - started from anywhere else the import of ``@openclaw/normalization-core``
    fails before any model call.
    """

    def __init__(self) -> None:
        root = _root()
        server = Path(
            os.environ.get("CONTEXT_SHUNT_OPENCLAW_SERVER")
            or Path(__file__).resolve().parent / "openclaw_inhost_server.mts"
        )
        env = dict(os.environ)
        env["CONTEXT_SHUNT_OPENCLAW_ROOT"] = root
        env.setdefault(
            "CONTEXT_SHUNT_OPENCLAW_ROUTE",
            os.environ.get("CONTEXT_SHUNT_OPENCLAW_ROUTE", DEFAULT_ROUTE),
        )
        loader = _loader(root)
        if not Path(loader).exists():
            raise BridgeError(f"TypeScript loader not found: {loader}")
        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [loader, str(server)],
                cwd=root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise BridgeError(
                f"could not start the in-host server ({type(exc).__name__})"
            ) from None
        self._lock = threading.Lock()
        self._next_id = 0
        self.identity: dict[str, Any] = {}
        ready = self._read_json(
            deadline=time.monotonic() + STARTUP_TIMEOUT_S, want_ready=True
        )
        if not ready.get("ready"):
            raise BridgeError("in-host server did not report ready")
        identity = ready.get("identity")
        self.identity = identity if isinstance(identity, dict) else {}

    def _read_json(
        self, *, deadline: float, want_ready: bool = False
    ) -> dict[str, Any]:
        """The next JSON object on stdout, skipping the host's own log lines.

        The host prints config warnings and plugin traces to the same stream, so a line is
        located by parsing it rather than by assuming stdout carries only the protocol.
        """
        stdout = self._process.stdout
        assert stdout is not None
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("in-host server exceeded the reader deadline")
            line = stdout.readline()
            if not line:
                # The server exited. Its stderr is the only diagnosis available, and it
                # can quote host configuration, so only a bounded tail crosses.
                raise BridgeError(f"in-host server exited: {self._stderr_tail()}")
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and (not want_ready or "ready" in value):
                return value

    def _stderr_tail(self, limit: int = 300) -> str:
        stderr = self._process.stderr
        if stderr is None:
            return "no stderr"
        try:
            return stderr.read()[-limit:].replace("\n", " ").strip() or "no stderr"
        except OSError:
            return "no stderr"

    def complete(
        self, *, system: str, user: str, max_output_tokens: int, timeout_ms: int
    ) -> dict:
        """One request/response exchange. Serialized: the protocol is a single stream."""
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            stdin = self._process.stdin
            assert stdin is not None
            payload = json.dumps(
                {
                    "id": request_id,
                    "system": system,
                    "user": user,
                    # The reader's ceiling, passed through as the provider's ceiling.
                    "max_output_tokens": max_output_tokens,
                }
            )
            deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
            try:
                stdin.write(payload + "\n")
                stdin.flush()
            except OSError:
                raise BridgeError(
                    f"in-host server closed its input: {self._stderr_tail()}"
                ) from None
            while True:
                result = self._read_json(deadline=deadline)
                if result.get("id") == request_id:
                    return result

    def close(self) -> None:
        """Stop the server. A daemon-like child must not outlive the run that spawned it."""
        process = self._process
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


_SERVER: _Server | None = None
_SERVER_LOCK = threading.Lock()


def _server() -> _Server:
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is None:
            _SERVER = _Server()
        return _SERVER


def identity() -> dict[str, Any]:
    """What the host said it resolved, recorded once at startup. For the attestation."""
    return dict(_server().identity)


def shutdown() -> None:
    """Stop the server, if one was started. Idempotent."""
    global _SERVER
    with _SERVER_LOCK:
        server, _SERVER = _SERVER, None
    if server is not None:
        server.close()


def complete(
    *,
    system: str,
    user: str,
    provider: str,
    model: str,
    max_output_tokens: int,
    timeout_ms: int,
) -> dict:
    """Run one reader call through the host and return the bridge mapping."""
    route = os.environ.get("CONTEXT_SHUNT_OPENCLAW_ROUTE") or DEFAULT_ROUTE
    if provider:
        route = f"{provider}/{model}"
    elif model and not route.endswith(f"/{model}"):
        raise BridgeError(f"route {route!r} does not serve requested model {model!r}")
    os.environ["CONTEXT_SHUNT_OPENCLAW_ROUTE"] = route

    started = time.monotonic()
    result = _server().complete(
        system=system,
        user=user,
        max_output_tokens=max_output_tokens,
        timeout_ms=timeout_ms,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if result.get("ok") is not True:
        # The host's message can quote the prompt back, so only its bounded kind crosses.
        raise BridgeError(
            f"host refused the call ({result.get('error_kind') or 'unknown'})"
        )
    text = result.get("text")
    if not isinstance(text, str):
        raise BridgeError("host returned no output text")

    LATENCIES_MS.append(elapsed_ms)

    mapping: dict[str, Any] = {"text": text}
    # The host's post-policy selection. Not a provider confirmation of what generated the
    # tokens, so `reported_*` and `provider_confirms_generation` are deliberately absent
    # and the envelope's attribution comes back `resolved` - the honest ceiling here.
    for key in ("resolved_provider", "resolved_model"):
        value = result.get(key)
        if isinstance(value, str) and value:
            mapping[key] = value
    mapping.update(_usage(result.get("usage")))
    return mapping


#: Host usage field -> bridge usage key. Only counts the bridge contract defines; a field
#: the host does not report is not sent, because absence is not zero.
_USAGE_FIELDS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("cacheReadTokens", "cache_tokens"),
)


def _usage(usage: Any) -> dict[str, Any]:
    """The reported counts, or nothing at all.

    ``usage_exact`` is only set when the host actually reported both directions: it is the
    claim that these numbers came from the provider, and setting it over a partial report
    would label an estimate as a measurement.
    """
    if not isinstance(usage, dict):
        return {}
    out: dict[str, Any] = {}
    for host_key, bridge_key in _USAGE_FIELDS:
        value = usage.get(host_key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        out[bridge_key] = value
    if "input_tokens" in out and "output_tokens" in out:
        out["usage_exact"] = True
    return out
