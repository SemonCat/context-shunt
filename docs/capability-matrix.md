# Capability matrix

What each supported host can actually do, and what it cannot. A mode is enabled only when
the adapter can prove the host gives it what the mode needs; where the proof does not
exist the mode is reported `unsupported` and stays off even if configuration requests it.
Nothing here is aspirational.

Both adapters emit this as a machine-readable capability report at startup
(`capability_report()` in Python, `capabilityJson()` in TypeScript). It carries the host
and SDK version, the tools covered, the mode decisions with reasons and evidence, and the
fixture id the adapter was tested against.

The adapter assertions and live integration gates target the exact host versions below.
Runtime compatibility on a particular checkout is proven only by that checkout's local
integration result. An upgrade is unverified until the gate is rerun and reviewed.

## Mode summary

| Mode | Hermes (`hermes-agent` 0.18.2) | OpenClaw (`openclaw` 2026.9.2) | Default |
| --- | --- | --- | --- |
| `local_gate` — block oversized/unprovable reads before execution | **supported** | **supported** | on |
| `reader` — question-driven answers with verified citations | **supported**, attribution ceiling `unverified` | **supported**, attribution ceiling `resolved` | on |
| `deterministic_inspect` — exact snapshot bytes, zero model calls | **supported** | **supported** | on |
| `session_stats` — this session's own token accounting | **supported** | **supported** | on |
| `session_lifecycle` — handles survive a per-turn boundary, revoked on a real one | **supported** | **supported** | on |
| `reader_task_config` — reader appears in host model configuration | **supported** | n/a (plugin config schema) | on |
| `suma_post_tool` — oversized tool/MCP result spill + pointer | **unsupported** | **unsupported** | off |
| writer / `propose_patch` | **not implemented** | **not implemented** | refused at load |

`deterministic_inspect` and `session_stats` need no provider at all, so they stay supported
even where the model bridge is absent or the reader is disabled.

## Why `local_gate` and `reader` are supported

| Host | Pre-tool hook | Model bridge |
| --- | --- | --- |
| Hermes | `pre_tool_call` fires inside `handle_function_call()` before the tool handler runs, and returning `{"action": "block", "message": ...}` short-circuits the call. | `ctx.llm.complete(...)`, with provider/model override gated per plugin by `plugins.entries.<id>.llm`. |
| OpenClaw | `api.on("before_tool_call", ...)` runs before tool execution, can deny the call, and the host fails this hook closed on timeout. | `api.runtime.llm.complete` with `execution.mode: "isolated-agent-runtime"` — a fresh, literal-zero-tool completion with no replayed chat history. |

## Model attribution: the ceiling on each host

Neither host proves which model generated the tokens. Rather than a release blocker, this is
now a reported capability boundary: the envelope states what can be proven and no more, and
`actual_model` is never synthesized from `requested_model`.

| Host | Best attainable `attribution_status` | Evidence |
| --- | --- | --- |
| Hermes | `unverified` | `agent/plugin_llm.py::_resolve_attribution` records `response.model` when the provider returned one, and otherwise the plugin's own override or `_read_main_model()`. A caller cannot tell those cases apart from the result object, so the adapter passes `provider_confirms_generation=false` and never claims `actual`. |
| OpenClaw | `resolved` | The isolated path returns `selection.provider` / `selection.modelId` through `runIsolatedAgentRuntimeCompletion` (`src/plugins/runtime/runtime-llm.runtime.ts`). That is the host's own post-policy selection — a routing fact, not a provider confirmation — so it is reported as `resolved_*`. |

A value that *contradicts* the request is a hard `MODEL_ERROR` on both hosts under either
policy: a different model is a wrong answer, not a weakly attributed one.

`reader.attribution_policy: require_match` refuses anything below `actual`/`resolved`. On
Hermes that disables the reader entirely, which is a legitimate deployment choice but never
the silent default; `inspect` and `stats` keep working either way.

Pinning the model on Hermes requires `plugins.entries.context-shunt.llm.allow_model_override:
true`. Without it `_check_overrides` raises and the reader runs on whatever the host picks —
which the envelope then reports truthfully rather than hides.

## Session lifecycle: why a per-turn event must not tear down

Both hosts fire something that *looks* like a session boundary while the conversation is
still going. Treating either as teardown deletes exactly the recovery state the next turn
needs.

| Host | The trap | What the adapter does |
| --- | --- | --- |
| Hermes | `on_session_end` fires at the end of **every** `run_conversation` call — `agent/turn_finalizer.py` says so in its own comment, and `cli.py` notes that "run_conversation() already fires this per-turn on normal completion". | `on_session_end` → sweep only. Teardown uses `on_session_finalize` (shutdown, `/new`) and `on_session_reset` (`/reset`), both in `VALID_HOOKS` and both fired from `cli.py::_notify_session_boundary`. A reset bumps the scope generation so old handles cannot be replayed. |
| OpenClaw | `session_end` carries a `reason` enum (`src/plugins/hook-types.ts`) that includes **`compaction`**, which rotates the session id mid-conversation, plus `idle`, `daily`, `shutdown`, `restart`. | Handles are scoped by `sessionKey` (stable across the rotation) with `sessionId` as the generation. Only `new` / `reset` / `deleted` revoke; every other reason keeps the handles and lets TTL bound them. |

Both host-integration gates assert this directly: a handle captured before the per-turn event
must still answer a refined question after it, and a real boundary must make it
`SOURCE_EXPIRED`.

## Why `suma_post_tool` is unsupported on both hosts

The mode needs two guarantees at once:

1. **Complete capture before truncation** — the adapter must see the whole result before
   the host shortens it, or a spilled pointer would silently describe a truncated payload.
2. **Safe replacement before persistence and context insertion** — the pointer must take
   the raw result's place before it can reach the transcript or the model.

### Hermes

| Reason | Evidence |
| --- | --- |
| `CAPTURE_AFTER_TRUNCATION` | The plugin-hook documentation for `transform_tool_result` states the `result` argument is "the tool's raw result string, post-truncation and post-ANSI-strip". The only earlier hook, `transform_terminal_output`, is scoped to the `terminal` tool and does not cover MCP or other tool results. |
| `HOST_FAIL_OPEN` | In `model_tools.py`, the `transform_tool_result` dispatch is wrapped in `try/except` around the whole block: a handler that raises is logged at debug level and the **original** `result` is returned unchanged. A plugin-side `try/except` cannot fix this — the fail-open path is the host's. |

A controlled MCP producer wrapper that captures and spills before the host's fallback
could satisfy both guarantees, but building one means changing how the host produces MCP
results. That is out of scope for a plugin, so the mode stays off.

### OpenClaw

| Reason | Evidence |
| --- | --- |
| `CAPTURE_AFTER_TRUNCATION` | In `src/agents/session-tool-result-guard.ts` the persist path runs `capToolResultForPersistence(...)` and only then `persistToolResult(capped, ...)`, which is what invokes the `tool_result_persist` hook. The plugin therefore receives already-capped content. |
| `OBSERVE_ONLY_HOOK` | `docs/plugins/hooks.md` lists `after_tool_call` as **Observe** — it can see a result but cannot replace one. The one hook positioned early enough cannot perform the replacement. |
| `HOST_FAIL_OPEN` | `docs/plugins/hooks.md` documents `tool_result_persist` and `before_message_write` as synchronous with no async timeout, where "synchronous errors are logged; failed results are ignored" — the original message survives a failing handler. |

`scripts/verify integration openclaw --mode local` re-checks that ordering against the
installed host on every run, so a host upgrade that changes it fails the gate instead of
silently invalidating this table. The same gate now also pins the two host facts the 1.1
lifecycle and accounting decisions rest on: that `PluginHookSessionEndReason` still contains
`compaction` (and that `session_end` still carries `nextSessionId`), and that
`src/agents/isolated-completion.ts` still says "absence must not be projected as zero" about
token usage. If either changes, the decision that depends on it has to be re-derived rather
than inherited.

## What the disabled mode still gets you

The spill engine itself is implemented and exercised by `unit bounded-output`, which runs
the shared `contracts/v1/conformance/spill-cases.json` corpus in both cores: oversized
string, object, array and content-block results all spill with **zero** model calls and no
heuristic summary; image/audio/resource blocks are refused; quota exhaustion, write
failure, readback mismatch, cycles, excessive depth and node counts all produce a bounded
`SPILL_FAILED` or `LIMIT_EXCEEDED` envelope and never the raw payload.

So the engine is not the host blocker. Keep those facts separate when reading a report:
the deterministic gate has an executable implementation, while no supported host enables
the mode. Whether the gate passed a particular checkout comes from that run's result.

## Tool coverage

Only the tool ids below are gated. A read tool outside these lists is **not** protected,
and the capability report says so rather than implying blanket coverage. A deployment that
needs comprehensive protection must disable uncontrolled read tools at the host.

| Host | Read | Search | Shell |
| --- | --- | --- | --- |
| Hermes | `read_file` | `search_files` | `terminal` |
| OpenClaw | `read` | none | `exec` |

The three context-shunt tools themselves are registered on both hosts, but their advertised
parameter schema is currently the portable subset described in
[`limitations.md`](limitations.md): optional read selectors and per-call inspect budget
overrides exist in the internal contract but are not uniformly exposed by host registration.

## Gate status

| Category | Status |
| --- | --- |
| Deterministic unit gates (contract, pre-read, reader, citations, no-raw-leak, bounded-output, cancellation, permissions, no-writes, capability, store, inspect, accounting) | implemented; no host or provider needed; run them on the release commit for the result |
| `integration <host> --mode unsupported` | implemented — deterministic fail-closed behaviour |
| `integration hermes --mode local` | implemented; runs against a real `hermes-agent` checkout, NOT_RUN without one |
| `integration openclaw --mode local` | implemented; runs against a real `openclaw` checkout, NOT_RUN without one |
| `integration <host> --mode post-tool` | NOT_RUN by design — the mode is unsupported on both hosts |
| `benchmark core` | implemented — gate/spill latency, envelope caps, context savings, bounded memory |
| `benchmark all` | includes a NOT_RUN provider half: reader latency and token cost need live Luna |
| `eval luna` | implemented harness with a fixed 40-item corpus; NOT_RUN without live Luna |
| `packaging all` | builds and inspects archives; clean-installs/imports/uninstalls both npm packages, the Python wheel, and the Hermes copy bundle |
| `release all` | runs everything; returns `NOT_RUN` while `eval luna` and the provider benchmark lack live Luna access |

## Future work, stated plainly

- A live runtime sentinel measurement of capture/truncation/persistence/context-insertion
  order inside a running gateway, for either host. The current gate verifies the ordering
  from host source, which is enough to keep the mode off but is not a runtime measurement.
- A controlled MCP producer wrapper for Hermes that would make `suma_post_tool`
  supportable there.
- The optional writer contract (`operation: propose_patch`). It is refused today; a future
  version needs its own schema version, write scopes, conflict checks and acceptance.
- A host that lets a plugin observe the provider's own report of which model generated the
  tokens. Until one exists, `attribution_status: actual` stays reachable by the contract and
  unclaimed by every supported adapter.
- A task-aware plugin LLM surface on Hermes. `ctx.llm` calls `call_llm(task=None)`
  (`agent/plugin_llm.py`), so the registered auxiliary task would route nothing on its own;
  the adapter reads `auxiliary.context_shunt_reader` itself through the public
  `hermes_cli.config.load_config` and applies the same user-over-plugin precedence. If the
  facade gains a `task=` parameter, that indirection can go away.
- Adapter registration schemas that expose the shared optional read selector and inspect
  per-call budget fields consistently on both hosts.
