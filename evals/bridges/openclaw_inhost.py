"""A production-path reader bridge over OpenClaw's host-owned model runtime.

The CLI bridge cannot qualify: ``openclaw infer model run`` collapses the system prompt
into one user turn and exposes no output-token flag. This bridge instead drives
``openclaw_inhost_server.mts`` over newline-delimited JSON. The server constructs the same
``runtime.llm.complete`` facade supplied as ``api.runtime.llm.complete`` and uses the
shipped adapter's exact isolated request shape:

* the fixed reader instruction in ``systemPrompt`` and one user excerpt;
* ``execution.mode: isolated-agent-runtime``;
* the reader's ``max_output_tokens`` as ``maxTokens`` and its deadline as ``timeoutMs``;
* ``temperature: 0``, a dedicated plugin-evaluation identity, a bound agent and a closed
  model allowlist.

``createRuntimeLlm`` dispatches that branch through ``runIsolatedAgentRuntimeCompletion``
and ``runIsolatedCompletion``. The server therefore uses the same model path as
``adapters/openclaw/index.ts``, not the simple-completion transport this bridge formerly
used. A dedicated caller keeps the evaluation independent of any disabled or stale plugin
entry in the local host config; the authority still fails closed to the one requested route.
It neither enables the plugin nor opens a conversation or registers tools.

The host's resolved provider/model, isolated execution owner and usage block are returned.
Absent usage stays absent; it is never projected as zero. Credentials remain inside the
host runtime: OpenClaw's command-scoped resolver materializes registered model-provider
references into the in-memory runtime config, and this bridge never inspects or returns
their values. Host errors cross the bridge only as a bounded type chain.

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
                                     the route's role/cap claims are *verified* rather than
                                     asserted in a descriptor nobody checks; source checks
                                     pin the production-dispatch claim.
"""

from __future__ import annotations

import json
import os
import queue
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

# The core applies tighter per-request limits before reaching this bridge. These outer
# bounds keep the separately callable evaluation transport narrow and fail closed.
MAX_ROLE_CONTENT_BYTES = 262_144
MAX_OUTPUT_TOKENS = 2_048
MAX_TIMEOUT_MS = 60_000

#: Populated with one entry per call so a benchmark can report real latency. Never holds a
#: prompt, a completion, or any source text - only timings.
LATENCIES_MS: list[int] = []

#: Properties read by the release gates and recorded verbatim in the attestation. The
#: server constructs the same host-owned runtime facade and isolated request shape the
#: adapter uses; it does not bypass the production model-dispatch path.
BRIDGE: dict[str, Any] = {
    "id": "openclaw_inhost",
    "host": "openclaw",
    "route_kind": "in_host_plugin_runtime_isolated_agent",
    "preserves_roles": True,
    "enforces_output_cap": True,
    "forwards_usage": True,
    "production_equivalent": True,
    "production_gap": "none for model dispatch",
    "production_equivalence_evidence": (
        "the server constructs createRuntimeLlm (the api.runtime.llm.complete owner) and "
        "uses execution.mode=isolated-agent-runtime with the adapter's request shape; "
        "evaluation caller authority is independently restricted to the requested route"
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
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._next_id = 0
        self._startup_messages: queue.Queue[
            tuple[dict[str, Any] | None, int]
        ] = queue.Queue()
        self._pending: dict[
            int, queue.Queue[tuple[dict[str, Any] | None, int]]
        ] = {}
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="context-shunt-openclaw-protocol",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            name="context-shunt-openclaw-stderr",
            daemon=True,
        )
        self._stderr_reader.start()
        self.identity: dict[str, Any] = {}
        ready = self._read_json(
            deadline=time.monotonic() + STARTUP_TIMEOUT_S, want_ready=True
        )
        if not ready.get("ready"):
            raise BridgeError("in-host server did not report ready")
        identity = ready.get("identity")
        self.identity = identity if isinstance(identity, dict) else {}

    def _read_stdout(self) -> None:
        """Parse host output without letting a blocking ``readline`` defeat deadlines."""
        stdout = self._process.stdout
        assert stdout is not None
        for line in stdout:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                message = (value, len(line.encode("utf-8")))
                request_id = value.get("id")
                if isinstance(request_id, int) and not isinstance(request_id, bool):
                    with self._pending_lock:
                        recipient = self._pending.get(request_id)
                    # A result after its caller's deadline is intentionally discarded.
                    # The caller has already recorded that started attempt with unknown
                    # usage; publishing this response into another request would be worse.
                    if recipient is not None:
                        try:
                            recipient.put_nowait(message)
                        except queue.Full:
                            pass
                else:
                    self._startup_messages.put(message)
        self._startup_messages.put((None, 0))
        with self._pending_lock:
            recipients = tuple(self._pending.values())
        for recipient in recipients:
            try:
                recipient.put_nowait((None, 0))
            except queue.Full:
                pass

    def _drain_stderr(self) -> None:
        """Prevent host diagnostics filling the pipe; retain and publish none of them."""
        stderr = self._process.stderr
        if stderr is None:
            return
        for _line in stderr:
            pass

    def _read_json(
        self, *, deadline: float, want_ready: bool = False
    ) -> dict[str, Any]:
        """The next JSON object on stdout, skipping the host's own log lines.

        The host prints config warnings and plugin traces to the same stream, so a line is
        located by parsing it rather than by assuming stdout carries only the protocol.
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("in-host server exceeded the reader deadline")
            try:
                value, wire_bytes = self._startup_messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("in-host server exceeded the reader deadline") from None
            if value is None:
                # Host stderr can contain configuration and credential diagnostics. Only
                # process state crosses this boundary.
                raise BridgeError(
                    f"in-host server exited (status {self._process.poll()})"
                )
            if not want_ready or "ready" in value:
                value["_transport_response_bytes"] = wire_bytes
                return value

    def complete(
        self, *, system: str, user: str, max_output_tokens: int, timeout_ms: int
    ) -> dict:
        """One multiplexed request/response exchange, correlated by protocol id."""
        deadline = time.monotonic() + max(0.001, timeout_ms / 1000.0)
        responses: queue.Queue[tuple[dict[str, Any] | None, int]] = queue.Queue(maxsize=1)
        with self._write_lock:
            self._next_id += 1
            request_id = self._next_id
            with self._pending_lock:
                self._pending[request_id] = responses
            stdin = self._process.stdin
            assert stdin is not None
            payload = json.dumps(
                {
                    "id": request_id,
                    "system": system,
                    "user": user,
                    # The reader's ceiling, passed through as the provider's ceiling.
                    "max_output_tokens": max_output_tokens,
                    "timeout_ms": timeout_ms,
                }
            )
            try:
                stdin.write(payload + "\n")
                stdin.flush()
            except OSError:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise BridgeError("in-host server closed its input") from None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("in-host server exceeded the reader deadline")
            try:
                result, wire_bytes = responses.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("in-host server exceeded the reader deadline") from None
            if result is None:
                raise BridgeError(
                    f"in-host server exited (status {self._process.poll()})"
                )
            result["_transport_response_bytes"] = wire_bytes
            result["_transport_request_bytes"] = len(
                (payload + "\n").encode("utf-8")
            )
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

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
    if not isinstance(system, str) or not isinstance(user, str):
        raise BridgeError("reader roles must be strings")
    if len(system.encode("utf-8")) + len(user.encode("utf-8")) > MAX_ROLE_CONTENT_BYTES:
        raise BridgeError("reader role content exceeds the bridge input bound")
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 0 < max_output_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise BridgeError("reader output cap is outside the bridge bound")
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or not 0 < timeout_ms <= MAX_TIMEOUT_MS
    ):
        raise BridgeError("reader deadline is outside the bridge bound")
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
        chain = result.get("error_chain")
        kinds = (
            "/".join(str(item)[:64] for item in chain[:6])
            if isinstance(chain, list)
            else str(result.get("error_kind") or "unknown")
        )
        raise BridgeError(
            f"host refused the call ({kinds or 'unknown'})"
        )
    text = result.get("text")
    if not isinstance(text, str):
        raise BridgeError("host returned no output text")

    expected_provider, expected_model = route.split("/", 1)
    execution = result.get("execution")
    owner = execution.get("owner") if isinstance(execution, dict) else None
    if (
        result.get("requested_route") != route
        or result.get("resolved_provider") != expected_provider
        or result.get("resolved_model") != expected_model
        or result.get("transport") != "runtime.llm.complete/isolated-agent-runtime"
        or not isinstance(execution, dict)
        or execution.get("mode") != "isolated-agent-runtime"
        or not isinstance(owner, dict)
        or owner.get("kind") not in {"cli", "harness"}
        or not isinstance(owner.get("id"), str)
        or not owner.get("id")
    ):
        # Missing identity is not a weaker success: it is how a substitution could be
        # mislabeled Luna. Refuse the answer before HostBridgeProvider can publish it.
        raise BridgeError("host response failed route or execution attestation")

    LATENCIES_MS.append(elapsed_ms)

    mapping: dict[str, Any] = {
        "text": text,
        "route_attested": True,
        "execution_mode": execution["mode"],
        "execution_owner_kind": owner["kind"],
        "execution_owner_id": owner["id"],
    }
    # The host's post-policy selection. Not a provider confirmation of what generated the
    # tokens, so `reported_*` and `provider_confirms_generation` are deliberately absent
    # and the envelope's attribution comes back `resolved` - the honest ceiling here.
    for key in ("resolved_provider", "resolved_model"):
        value = result.get(key)
        if isinstance(value, str) and value:
            mapping[key] = value
    for source_key, target_key in (
        ("elapsed_ms", "host_elapsed_ms"),
        ("_transport_request_bytes", "transport_input_bytes"),
        ("_transport_response_bytes", "transport_output_bytes"),
    ):
        value = result.get(source_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            mapping[target_key] = value
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
