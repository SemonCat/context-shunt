"""unit bridge-contract: what each live reader route actually does, verified not asserted.

A live gate's number describes the route it was measured on. Two properties decide whether
that number describes the product at all:

**Separate roles.** The reader's fixed instruction is a *system* message and the authorized
excerpt is a *user* message. Production adapters send them that way and the OpenClaw
isolated runtime refuses anything else. A route that concatenates them sends a different
prompt, and a different prompt can move a result in either direction - it is not a
pessimistic approximation, it is a measurement of something else.

**A forwarded output cap.** The reader budgets `max_output_tokens` per call. A route that
drops it lets the provider produce an unbounded completion, so neither the latency nor the
token cost measured through it is the bounded call the product makes.

Each bridge declares both in a ``BRIDGE`` descriptor, and the release gates refuse to score
a live number through a route whose descriptor says no. This module is why that refusal can
be trusted: it drives each route against a stub and checks the descriptor against observed
behaviour. A descriptor nobody verifies is just a comment.

No live model is involved and none is needed. What is being verified here is the *protocol*
between this repository and the host, which is deterministic.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from context_shunt.limits import DEFAULT_LIMITS, READER_MODEL
from context_shunt.provenance import TokenMethod
from context_shunt.provider import HostBridgeProvider
from context_shunt.reader import Reader
from context_shunt.snapshot import snapshot_bytes
from tests.support import claims_json, make_registry

pytestmark = pytest.mark.bridge_contract

REPO = Path(__file__).resolve().parents[3]
BRIDGES = REPO / "evals" / "bridges"

#: The two properties a required live gate will not score without.
#: The two protocol properties this gate verifies against each route's own behaviour.
#: `scripts/verify` requires a third - `production_equivalent` - before a live number may
#: be reported as this project's. The in-host route earns it by driving the same
#: `runtime.llm.complete` isolated-agent path as the shipped OpenClaw adapter.
REQUIRED_PROPERTIES = ("preserves_roles", "enforces_output_cap")

#: What `scripts/verify` actually gates a release-quality live number on. Duplicated as a
#: literal rather than imported: `scripts/verify` is a script, not a module, and a test
#: that reads the value it is checking cannot catch the value changing.
RELEASE_QUALITY_PROPERTIES = (
    "preserves_roles",
    "enforces_output_cap",
    "enforces_usd_budget",
    "production_equivalent",
)


def _bridges_module(name: str):
    """Import a bridge the way an operator does: ``PYTHONPATH=evals``."""
    if str(REPO / "evals") not in sys.path:
        sys.path.insert(0, str(REPO / "evals"))
    import importlib

    return importlib.import_module(f"bridges.{name}")


#: A stand-in for the in-host NDJSON server. It prints the host's kind of log noise first,
#: so the bridge's "locate the JSON, do not assume stdout is the protocol" rule is
#: exercised, then records every request it is given and answers it.
_STUB_SERVER = """
import json, sys
record = open(sys.argv[1], "w", encoding="utf-8")
print("Config warnings: plugins.entries.example: plugin disabled", flush=True)
print("[plugins] something failed to load, which is not the protocol", flush=True)
print(json.dumps({"ready": True, "identity": {
    "transport": "stub", "resolved_provider": "stub-provider",
    "resolved_model": "%(model)s",
    "pricing": {
        "route": "stub-provider/%(model)s", "currency": "USD",
        "unit": "per_million_tokens", "source": "openclaw.resolveModelCostConfig",
        "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "rates": {"input": 1, "output": 6, "cacheRead": 0.1, "cacheWrite": 0},
        "tieredPricing": [],
    },
}}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    record.write(json.dumps(request) + "\\n")
    record.flush()
    reply = {
        "id": request["id"],
        "ok": True,
        "text": %(text)r,
        "resolved_provider": "stub-provider",
        "resolved_model": "%(model)s",
        "requested_route": "stub-provider/%(model)s",
        "transport": "runtime.llm.complete/isolated-agent-runtime",
        "execution": {
            "mode": "isolated-agent-runtime",
            "owner": {"kind": "harness", "id": "stub"},
        },
        "usage": %(usage)s,
    }
    print(json.dumps(reply), flush=True)
"""

_EXPENSIVE_PRICING_SERVER = _STUB_SERVER.replace('"input": 1,', '"input": 1000000,')

_REFUSING_SERVER = """
import json, sys
print(json.dumps({"ready": True, "identity": {"transport": "stub"}}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    print(json.dumps({"id": request["id"], "ok": False, "error_kind": "AUTH_FAILED"}), flush=True)
"""

_WRONG_ROUTE_SERVER = """
import json, sys
print(json.dumps({"ready": True, "identity": {"transport": "stub"}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({
        "id": request["id"], "ok": True, "text": "{}",
        "requested_route": "stub-provider/%(model)s",
        "transport": "runtime.llm.complete/isolated-agent-runtime",
        "resolved_provider": "stub-provider", "resolved_model": "not-luna",
        "execution": {"mode": "isolated-agent-runtime", "owner": {"kind": "harness", "id": "stub"}},
    }), flush=True)
"""

_SLOW_SERVER = """
import json, sys, time
print(json.dumps({"ready": True, "identity": {"transport": "stub"}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    time.sleep(1)
"""

_NO_READ_SERVER = """
import json, time
print(json.dumps({"ready": True, "identity": {"transport": "stub"}}), flush=True)
time.sleep(5)
"""

_SLOW_START_SERVER = """
import json, time
time.sleep(1)
print(json.dumps({"ready": True, "identity": {"transport": "stub"}}), flush=True)
"""

_OUT_OF_ORDER_SERVER = """
import json, sys
print(json.dumps({"ready": True, "identity": {"transport": "stub"}}), flush=True)
requests = []
for line in sys.stdin:
    requests.append(json.loads(line))
    if len(requests) < 2:
        continue
    for request in reversed(requests):
        print(json.dumps({
            "id": request["id"], "ok": True, "text": "{}",
            "requested_route": "stub-provider/%(model)s",
            "transport": "runtime.llm.complete/isolated-agent-runtime",
            "resolved_provider": "stub-provider", "resolved_model": "%(model)s",
            "execution": {"mode": "isolated-agent-runtime", "owner": {"kind": "harness", "id": "stub"}},
        }), flush=True)
    requests.clear()
"""


@pytest.fixture
def inhost(tmp_path, monkeypatch):
    """The in-host bridge, wired to a stub server whose received requests are readable."""
    module = _bridges_module("openclaw_inhost")

    def start(*, text: str = "{}", usage: str = "None", server: str = _STUB_SERVER):
        # A full release run exports the live-eval budget variables for the later Luna
        # gate. This fixture drives a synthetic route with synthetic pricing, so inheriting
        # the live ledger would either contaminate it or fail on its route binding. The one
        # budget-specific test below opts back in explicitly after constructing the stub.
        monkeypatch.delenv("CONTEXT_SHUNT_LUNA_EVAL", raising=False)
        monkeypatch.delenv("CONTEXT_SHUNT_LUNA_BUDGET_DB", raising=False)
        script = tmp_path / "server.py"
        script.write_text(server % {"model": READER_MODEL, "text": text, "usage": usage})
        received = tmp_path / "received.jsonl"
        monkeypatch.setenv("CONTEXT_SHUNT_OPENCLAW_ROOT", str(tmp_path))
        monkeypatch.setenv("CONTEXT_SHUNT_OPENCLAW_ROUTE", f"stub-provider/{READER_MODEL}")
        monkeypatch.setenv("CONTEXT_SHUNT_OPENCLAW_TSX", sys.executable)
        # `argv[1]` of the stub is where it writes what it was asked. The bridge passes
        # only the server path, so the record path rides on the environment.
        wrapper = tmp_path / "wrapper.py"
        wrapper.write_text(
            "import runpy, sys\n"
            f"sys.argv = [{str(script)!r}, {str(received)!r}]\n"
            f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
        )
        monkeypatch.setenv("CONTEXT_SHUNT_OPENCLAW_SERVER", str(wrapper))
        module.shutdown()
        return module, received

    yield start
    module.shutdown()


def _requests(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- the in-host route: both required properties, verified ------------------------------


def test_the_in_host_route_sends_the_system_prompt_in_its_own_role(inhost):
    """The property the CLI route cannot have, observed rather than claimed."""
    module, received = inhost()
    module.complete(
        system="SYSTEM RULES",
        user="USER EXCERPT",
        provider="",
        model=READER_MODEL,
        max_output_tokens=2048,
        timeout_ms=30000,
    )
    sent = _requests(received)
    assert len(sent) == 1
    assert sent[0]["system"] == "SYSTEM RULES"
    assert sent[0]["user"] == "USER EXCERPT"
    # The whole point: the instruction is not folded into the user turn.
    assert "SYSTEM RULES" not in sent[0]["user"]
    assert module.BRIDGE["preserves_roles"] is True


def test_the_in_host_route_forwards_the_readers_output_cap(inhost):
    """`max_output_tokens` reaches the host, so the provider is given the reader's ceiling."""
    module, received = inhost()
    module.complete(
        system="s",
        user="u",
        provider="",
        model=READER_MODEL,
        max_output_tokens=DEFAULT_LIMITS.max_output_tokens_per_call,
        timeout_ms=30000,
    )
    sent = _requests(received)
    assert sent[0]["max_output_tokens"] == DEFAULT_LIMITS.max_output_tokens_per_call
    assert 0 < sent[0]["timeout_ms"] <= 30000
    assert sent[0]["deadline_unix_ms"] >= int(__import__("time").time() * 1000)
    assert module.BRIDGE["enforces_output_cap"] is True


def test_live_eval_reserves_dollars_before_the_host_can_receive_a_frame(
    inhost, tmp_path, monkeypatch
):
    """An unaffordable first request is refused with zero provider frames written."""
    module, received = inhost(server=_EXPENSIVE_PRICING_SERVER)
    monkeypatch.setenv("CONTEXT_SHUNT_LUNA_EVAL", "1")
    monkeypatch.setenv("CONTEXT_SHUNT_LUNA_BUDGET_DB", str(tmp_path / "budget.sqlite3"))
    with pytest.raises(Exception, match="USD 10"):
        module.complete(
            system="s",
            user="u",
            provider="",
            model=READER_MODEL,
            max_output_tokens=2048,
            timeout_ms=30000,
        )
    assert _requests(received) == []


def test_the_in_host_route_forwards_reported_usage_and_only_when_complete(inhost):
    """Exact counts when the host reports both directions; nothing at all otherwise.

    A partial report must not be labelled exact: ``usage_exact`` is the claim that these
    numbers came from the provider, and half of them did not.
    """
    module, received = inhost(usage='{"inputTokens": 11, "outputTokens": 7}')
    result = module.complete(
        system="s",
        user="u",
        provider="",
        model=READER_MODEL,
        max_output_tokens=2048,
        timeout_ms=30000,
    )
    assert result["input_tokens"] == 11
    assert result["output_tokens"] == 7
    assert result["usage_exact"] is True

    module, _ = inhost(usage='{"inputTokens": 11}')
    partial = module.complete(
        system="s",
        user="u",
        provider="",
        model=READER_MODEL,
        max_output_tokens=2048,
        timeout_ms=30000,
    )
    assert partial["input_tokens"] == 11
    assert "usage_exact" not in partial

    module, _ = inhost(usage="None")
    absent = module.complete(
        system="s",
        user="u",
        provider="",
        model=READER_MODEL,
        max_output_tokens=2048,
        timeout_ms=30000,
    )
    # Absence stays absence. A zero here would be laundered into the accounting as a
    # measurement of nothing spent.
    assert "input_tokens" not in absent and "output_tokens" not in absent
    assert "usage_exact" not in absent


def test_a_host_refusal_carries_no_prompt_text(inhost):
    """A host error message can quote the prompt back. Only its bounded kind may cross."""
    module, _ = inhost(server=_REFUSING_SERVER)
    with pytest.raises(Exception) as raised:
        module.complete(
            system="SECRET-SYSTEM-CANARY",
            user="SECRET-USER-CANARY",
            provider="",
            model=READER_MODEL,
            max_output_tokens=2048,
            timeout_ms=30000,
        )
    message = str(raised.value)
    assert "AUTH_FAILED" in message
    assert "CANARY" not in message


def test_the_in_host_route_fails_closed_on_model_substitution(inhost):
    module, _ = inhost(server=_WRONG_ROUTE_SERVER)
    with pytest.raises(module.BridgeError, match="route or execution attestation"):
        module.complete(
            system="s",
            user="u",
            provider="",
            model=READER_MODEL,
            max_output_tokens=2048,
            timeout_ms=30000,
        )


def test_the_protocol_readline_cannot_outlive_the_call_deadline(inhost):
    module, _ = inhost(server=_SLOW_SERVER)
    started = __import__("time").monotonic()
    with pytest.raises(TimeoutError):
        module.complete(
            system="s",
            user="u",
            provider="",
            model=READER_MODEL,
            max_output_tokens=2048,
            timeout_ms=20,
        )
    assert __import__("time").monotonic() - started < 0.5


def test_server_startup_cannot_refresh_an_expired_call_deadline(inhost):
    module, _ = inhost(server=_SLOW_START_SERVER)
    started = __import__("time").monotonic()
    with pytest.raises(TimeoutError):
        module.complete(
            system="s",
            user="u",
            provider="",
            model=READER_MODEL,
            max_output_tokens=2048,
            timeout_ms=20,
        )
    assert __import__("time").monotonic() - started < 0.5


def test_an_expired_request_waiting_for_the_write_lock_is_never_dispatched(inhost):
    module, received = inhost()
    server = module._server()
    server._write_lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="before dispatch"):
            module.complete(
                system="s",
                user="u",
                provider="",
                model=READER_MODEL,
                max_output_tokens=2048,
                timeout_ms=20,
            )
    finally:
        server._write_lock.release()
    assert not received.exists() or received.read_text() == ""


def test_pipe_backpressure_cannot_outlive_the_dispatch_deadline(inhost):
    module, _ = inhost(server=_NO_READ_SERVER)
    module.identity()  # isolate pipe backpressure from the separately tested startup bound
    started = __import__("time").monotonic()
    with pytest.raises(TimeoutError, match="during dispatch"):
        module.complete(
            system="s" * 200_000,
            user="u",
            provider="",
            model=READER_MODEL,
            max_output_tokens=2048,
            timeout_ms=20,
        )
    assert __import__("time").monotonic() - started < 0.5


def test_the_protocol_multiplexes_concurrent_chunks_by_request_id(inhost):
    from concurrent.futures import ThreadPoolExecutor

    module, _ = inhost(server=_OUT_OF_ORDER_SERVER)

    def invoke(value: str):
        return module.complete(
            system="s",
            user=value,
            provider="",
            model=READER_MODEL,
            max_output_tokens=2048,
            timeout_ms=1000,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ("first", "second")))
    assert len(results) == 2
    assert all(result["route_attested"] is True for result in results)


@pytest.mark.parametrize(
    ("field", "value"),
    (("max_output_tokens", 2049), ("timeout_ms", 60001)),
)
def test_the_in_host_route_refuses_transport_bounds_before_starting(inhost, field, value):
    module, _ = inhost()
    kwargs = {
        "system": "s",
        "user": "u",
        "provider": "",
        "model": READER_MODEL,
        "max_output_tokens": 2048,
        "timeout_ms": 30000,
    }
    kwargs[field] = value
    with pytest.raises(module.BridgeError, match="outside the bridge bound"):
        module.complete(**kwargs)


def test_the_reader_drives_the_in_host_route_end_to_end(tmp_path, inhost):
    """The whole path: reader -> HostBridgeProvider -> bridge -> server -> envelope.

    This is the deterministic half of the release evidence for this route. It proves the
    protocol carries an answer, a resolved identity and exact usage; what it deliberately
    does not prove is anything about a real model, which only a live gate can.
    """
    reply = claims_json(
        [{"text": "Retries stop after three attempts.", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 2, "line_end": 2, "quote": "max_retries = 3"}],
    )
    module, received = inhost(text=reply, usage='{"inputTokens": 40, "outputTokens": 12}')
    registry = make_registry(tmp_path / "store", session_id="sess")
    entry = registry.register(
        "sess", snapshot_bytes(b"# config\nmax_retries = 3\nbackoff = exponential\n")
    )
    provider = HostBridgeProvider(module.complete, DEFAULT_LIMITS, READER_MODEL)
    result = Reader(registry, provider).answer(
        "sess",
        {
            "schema_version": "1.0",
            "request_id": "req_bridge",
            "operation": "read",
            "question": "What is the maximum number of retries?",
            "sources": [
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": {"kind": "all"},
                }
            ],
            "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
        },
    )
    envelope = result.envelope
    assert envelope["status"] == "ok" and envelope["code"] == "ANSWERED"
    assert envelope["answer"] == "Retries stop after three attempts [c1]."
    # The host's post-policy selection, published as a routing fact and nothing stronger.
    assert envelope["provenance"]["attribution_status"] == "resolved"
    assert envelope["provenance"]["resolved_model"] == READER_MODEL
    assert envelope["provenance"]["reported_model"] is None
    # Reported usage is used as reported, and the method says so.
    assert result.cost.method is TokenMethod.EXACT
    assert (result.cost.input_tokens, result.cost.output_tokens) == (40, 12)
    # And the reader's own ceiling reached the host on that call.
    assert _requests(received)[0]["max_output_tokens"] == (
        DEFAULT_LIMITS.max_output_tokens_per_call
    )


# -- the CLI route: the descriptor's "no" is verified too --------------------------------


def test_the_cli_route_really_does_collapse_the_roles_and_drop_the_cap(tmp_path, monkeypatch):
    """The CLI bridge's descriptor claims two failures. Both are observed here.

    Verifying a negative matters as much as verifying a positive: if the CLI route were
    quietly fixed, or quietly broken further, the release gates' decision to refuse it
    would be resting on a stale comment.
    """
    module = _bridges_module("openclaw_cli")
    assert module.BRIDGE["preserves_roles"] is False
    assert module.BRIDGE["enforces_output_cap"] is False

    argv_path = tmp_path / "argv.json"
    stub = tmp_path / "openclaw-stub"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(argv_path)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        'print(json.dumps({"ok": True, "provider": "p", "model": "m", '
        '"outputs": [{"text": "{}"}]}))\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CONTEXT_SHUNT_OPENCLAW_BIN", str(stub))
    module.complete(
        system="SYSTEM RULES",
        user="USER EXCERPT",
        provider="",
        model=READER_MODEL,
        max_output_tokens=2048,
        timeout_ms=30000,
    )
    argv = json.loads(argv_path.read_text())
    prompt = argv[argv.index("--prompt") + 1]
    # One turn carrying both roles - exactly what the descriptor says.
    assert "SYSTEM RULES" in prompt and "USER EXCERPT" in prompt
    # And no output ceiling anywhere on the command line.
    assert not [flag for flag in argv if "token" in flag.lower() or flag == "2048"]


def test_every_bridge_declares_the_properties_the_release_gates_read():
    """A bridge with no descriptor, or a partial one, must not be silently scored."""
    for path in sorted(BRIDGES.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = _bridges_module(path.stem)
        descriptor = getattr(module, "BRIDGE", None)
        assert isinstance(descriptor, dict), path.name
        for key in ("id", "host", "route_kind", "production_equivalent", "production_gap"):
            assert key in descriptor, (path.name, key)
        for key in REQUIRED_PROPERTIES:
            assert isinstance(descriptor.get(key), bool), (path.name, key)
        assert descriptor["production_gap"], path.name
        if descriptor["production_equivalent"] is True:
            assert descriptor.get("production_equivalence_evidence"), path.name


def test_the_in_host_server_uses_the_shipped_isolated_runtime_path():
    """Production equivalence is tied to the real server source, not descriptor prose."""
    server = (BRIDGES / "openclaw_inhost_server.mts").read_text()
    adapter = (REPO / "adapters" / "openclaw" / "index.ts").read_text()
    host_runtime = (
        Path(os.environ.get("CONTEXT_SHUNT_OPENCLAW_ROOT", ""))
        / "src/plugins/runtime/runtime-llm.runtime.ts"
    )
    for needle in (
        "createRuntimeLlm",
        "resolveCommandConfigWithSecrets",
        "getModelsCommandSecretTargetIds",
        "resolveModelCostConfig",
        "resolveModelCostConfigFingerprint",
        'commandName: "context-shunt isolated reader evaluation"',
        "autoEnable: false",
        'caller: { kind: "plugin", id: "context-shunt-eval" }',
        "allowedCompletionModels: [ROUTE]",
        'execution: { mode: "isolated-agent-runtime", timeoutMs: Math.max(1, remainingMs) }',
        "maxTokens: req.max_output_tokens",
        "systemPrompt: req.system",
        'messages: [{ role: "user", content: req.user }]',
        "void handle(line)",
    ):
        assert needle in server
    for needle in (
        'execution: { mode: "isolated-agent-runtime", timeoutMs }',
        "maxTokens: maxOutputTokens",
        "systemPrompt: system",
        'messages: [{ role: "user", content: user }]',
    ):
        assert needle in adapter
    if host_runtime.is_file():
        source = host_runtime.read_text()
        assert "export function createRuntimeLlm" in source
        assert "runIsolatedAgentRuntimeCompletion" in source


def test_exactly_one_route_preserves_roles_and_enforces_the_cap():
    """The protocol half: one route here makes the call the reader actually budgets for.

    The production-dispatch claim is checked separately below. The CLI route folds the
    system prompt into the user turn and passes no output ceiling, so a number measured
    through it describes a different prompt under no cap.
    """
    qualifying = []
    for path in sorted(BRIDGES.glob("*.py")):
        if path.name.startswith("_"):
            continue
        descriptor = getattr(_bridges_module(path.stem), "BRIDGE", {})
        if all(descriptor.get(key) for key in REQUIRED_PROPERTIES):
            qualifying.append(path.stem)
    assert qualifying == ["openclaw_inhost"], qualifying


def test_only_the_isolated_in_host_route_is_release_quality():
    """The CLI remains disqualified; the runtime-isolated route satisfies every gate."""
    release_quality = []
    for path in sorted(BRIDGES.glob("*.py")):
        if path.name.startswith("_"):
            continue
        descriptor = getattr(_bridges_module(path.stem), "BRIDGE", {})
        if all(descriptor.get(key) is True for key in RELEASE_QUALITY_PROPERTIES):
            release_quality.append(path.stem)
    assert release_quality == ["openclaw_inhost"], release_quality
    # And the gate script must actually be asking for it, or the assertion above is
    # checking a property nothing reads.
    verify = (REPO / "scripts" / "verify").read_text()
    assert (
        "REQUIRED_BRIDGE_PROPERTIES = (\n"
        '    "preserves_roles",\n'
        '    "enforces_output_cap",\n'
        '    "enforces_usd_budget",\n'
        '    "production_equivalent",\n'
        ")" in verify
    )


def test_the_verifier_propagates_its_builtin_bridge_path_to_pytest():
    """A descriptor import that passes in the parent must also import in the gate worker."""
    verify = (REPO / "scripts" / "verify").read_text()
    assert 'pythonpath = [str(ROOT / "evals")]' in verify
    assert 'env["PYTHONPATH"] = os.pathsep.join(pythonpath)' in verify
    assert "env=env" in verify


def test_the_bridge_env_names_are_documented_where_an_operator_looks():
    """Every environment name the in-host route reads is named in its own docstring."""
    module = _bridges_module("openclaw_inhost")
    source = Path(module.__file__).read_text()
    doc = module.__doc__ or ""
    names = sorted(
        {
            name
            for name in (
                "CONTEXT_SHUNT_OPENCLAW_ROOT",
                "CONTEXT_SHUNT_OPENCLAW_ROUTE",
                "CONTEXT_SHUNT_OPENCLAW_TSX",
                "CONTEXT_SHUNT_OPENCLAW_SERVER",
                "CONTEXT_SHUNT_LUNA_BUDGET_DB",
            )
            if name in source
        }
    )
    assert names, "the bridge reads no environment at all?"
    for name in names:
        assert name in doc, name
    assert os.environ is not None  # the module reads the environment, not a global config


# -- the release attestation refuses evidence that does not describe this release --------


#: Loaded once. `scripts/verify` is a script, not a package module, so it is executed by
#: path - and registered in ``sys.modules`` *before* execution, because its dataclasses
#: resolve their own module by name while they are being defined.
_VERIFY_MODULE_NAME = "context_shunt_verify_script"


def _verify_module():
    """Load ``scripts/verify`` by path. It is a script, so it has no importable name."""
    import importlib.machinery
    import importlib.util

    cached = sys.modules.get(_VERIFY_MODULE_NAME)
    if cached is not None:
        return cached
    path = REPO / "scripts" / "verify"
    loader = importlib.machinery.SourceFileLoader(_VERIFY_MODULE_NAME, str(path))
    spec = importlib.util.spec_from_loader(_VERIFY_MODULE_NAME, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_VERIFY_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_VERIFY_MODULE_NAME, None)
        raise
    return module


def _sound_eval_report(commit: str, config_hash: str) -> dict:
    """An eval report that binds and passes, as a baseline for the negative cases."""
    return {
        "reviewed_commit": commit,
        "reviewed_source_manifest_sha256": "e" * 64,
        "effective_provider_config_sha256": config_hash,
        "route": {
            "spec": "bridges.example:complete",
            "bridge": {
                "id": "example",
                "preserves_roles": True,
                "enforces_output_cap": True,
                "enforces_usd_budget": True,
                "production_equivalent": True,
            },
        },
        "invalid_published_citations": 0,
        "answer_correctness": 0.97,
        "citation_semantic_support": 0.96,
        "successful_injections": 0,
        "secret_reflections": 0,
        "false_complete_claims": 0,
        "output_cap_breaches": 0,
        "wrong_model_calls": 0,
        "identity": {
            "physical_calls": 120,
            "certified_calls": 120,
            "uncertified_identity_calls": 0,
            "unacceptable_attribution_calls": 0,
            "identity_record_gaps": 0,
        },
        "usd_budget": {
            "this_run_reservations": 120,
            "after": {
                "limit_usd": "10",
                "reserved_usd": "1.5",
                "statuses": {"completed": 120},
            },
        },
    }


def test_a_fabricated_eval_report_does_not_bind_to_this_release():
    """The release blocker: an attestation accepted a report that named another commit.

    A file called ``reports/eval-luna.json`` was evidence enough. It could have been
    produced at another commit, through a route that is not the product, or under caps
    this tree does not set - and the attestation would still have called itself complete.
    Each of those is now a binding fault, which is NOT_RUN rather than a pass: evidence
    that does not describe this release is not weaker evidence about it.
    """
    verify = _verify_module()
    core = {
        "effective_provider_config_sha256": "a" * 64,
        "source_manifest_sha256": "e" * 64,
    }
    head = "b" * 40
    assert verify._eval_binding_faults(_sound_eval_report(head, "a" * 64), core, head) == []

    # An artifact-only commit changes HEAD but not the reviewed source manifest. This is
    # the non-circular binding that lets authentic generated evidence be committed.
    artifact_commit = _sound_eval_report("c" * 40, "a" * 64)
    assert verify._eval_binding_faults(artifact_commit, core, head) == []

    wrong_source = _sound_eval_report(head, "a" * 64)
    wrong_source["reviewed_source_manifest_sha256"] = "f" * 64
    faults = verify._eval_binding_faults(wrong_source, core, head)
    assert any("source manifest" in f for f in faults), faults

    no_commit = _sound_eval_report(head, "a" * 64)
    no_commit["reviewed_commit"] = "unknown"
    assert verify._eval_binding_faults(no_commit, core, head)

    wrong_config = _sound_eval_report(head, "d" * 64)
    faults = verify._eval_binding_faults(wrong_config, core, head)
    assert any("effective provider configuration" in f for f in faults), faults

    no_route = _sound_eval_report(head, "a" * 64)
    del no_route["route"]
    assert verify._eval_binding_faults(no_route, core, head)


def test_a_route_that_is_not_release_quality_cannot_produce_binding_evidence():
    """Production equivalence is one of the properties a live number is gated on."""
    verify = _verify_module()
    core = {
        "effective_provider_config_sha256": "a" * 64,
        "source_manifest_sha256": "e" * 64,
    }
    head = "b" * 40
    report = _sound_eval_report(head, "a" * 64)
    report["route"]["bridge"]["production_equivalent"] = False
    faults = verify._eval_binding_faults(report, core, head)
    assert any("not release-quality" in f and "production_equivalent" in f for f in faults), faults


def test_an_eval_report_that_binds_and_fails_is_a_failure_not_a_missing_prerequisite():
    """The two verdicts mean different things and must not be reported as one.

    Evidence that does not bind means this release has no evidence yet. Evidence that
    binds and contradicts a claim means the release claim is false - a failure. Reporting
    the second as NOT_RUN would let a false claim look like an unfinished one.
    """
    verify = _verify_module()
    head = "b" * 40
    assert verify._eval_outcome_failures(_sound_eval_report(head, "a" * 64)) == []

    below = _sound_eval_report(head, "a" * 64)
    below["answer_correctness"] = 0.80
    assert any("answer_correctness" in f for f in verify._eval_outcome_failures(below))

    leaked = _sound_eval_report(head, "a" * 64)
    leaked["secret_reflections"] = 1
    assert any("secret_reflections" in f for f in verify._eval_outcome_failures(leaked))

    invalid_citation = _sound_eval_report(head, "a" * 64)
    invalid_citation["invalid_published_citations"] = 1
    assert any(
        "invalid_published_citations" in f for f in verify._eval_outcome_failures(invalid_citation)
    )

    wrong_model = _sound_eval_report(head, "a" * 64)
    wrong_model["wrong_model_calls"] = 1
    assert any("wrong_model_calls" in f for f in verify._eval_outcome_failures(wrong_model))

    # Legacy aliases must not mask the absence of the fields the current gate writes.
    stale_schema = _sound_eval_report(head, "a" * 64)
    del stale_schema["successful_injections"]
    stale_schema["successful_prompt_injections"] = 0
    assert any("successful_injections" in f for f in verify._eval_outcome_failures(stale_schema))

    # Per-call identity totals are release claims too: one uncertified call withdraws the
    # statement that the release names the model behind every measured answer.
    partial = _sound_eval_report(head, "a" * 64)
    partial["identity"]["certified_calls"] = 119
    assert any("certified" in f for f in verify._eval_outcome_failures(partial))

    gapped = _sound_eval_report(head, "a" * 64)
    gapped["identity"]["identity_record_gaps"] = 2
    assert any("identity_record_gaps" in f for f in verify._eval_outcome_failures(gapped))

    silent = _sound_eval_report(head, "a" * 64)
    silent["identity"]["physical_calls"] = 0
    assert any("no physical calls" in f for f in verify._eval_outcome_failures(silent))

    shapeless = _sound_eval_report(head, "a" * 64)
    del shapeless["identity"]
    assert verify._eval_outcome_failures(shapeless)


def test_an_empty_benchmark_report_is_not_a_benchmark():
    """The release blocker: ``{}`` satisfied "the file exists" and measured nothing."""
    verify = _verify_module()
    assert verify._benchmark_faults({}) == [
        "reports/benchmark-provider.json is empty, so it measures nothing"
    ]
    assert verify._benchmark_faults(None)
    assert verify._benchmark_faults([])
    # Every field a benchmark claim rests on has to be present and of the right type.
    sound = {
        "samples": 40,
        "latency_ms_p95": 1234.5,
        "attempts_started_total": 40,
        "attempts_usage_complete_total": 40,
    }
    assert verify._benchmark_faults(sound) == []
    for key in sound:
        partial = dict(sound)
        del partial[key]
        assert verify._benchmark_faults(partial), key
    zero_samples = dict(sound, samples=0)
    assert any("no samples" in f for f in verify._benchmark_faults(zero_samples))
    # A boolean is an int in Python, and `True` samples is not one sample.
    assert verify._benchmark_faults(dict(sound, samples=True))
