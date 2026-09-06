# Capability matrix

What each supported host can actually do, and what it cannot. A mode is enabled only when
the adapter can prove the host gives it what the mode needs; where the proof does not
exist the mode is reported `unsupported`, stays off, and fails closed if configuration
asks for it. Nothing here is aspirational.

Both adapters emit this as a machine-readable capability report at startup
(`capability_report()` in Python, `capabilityJson()` in TypeScript). It carries the host
and SDK version, the tools covered, the mode decisions with reasons and evidence, and the
fixture id the adapter was tested against.

## Mode summary

| Mode | Hermes (`hermes-agent` 0.18.0) | OpenClaw (`openclaw` 2026.9.x) | Default |
| --- | --- | --- | --- |
| `local_gate` — block oversized/unprovable reads before execution | **supported** | **supported** | on |
| `reader` — question-driven answers with verified citations, `gpt-5.6-luna` only | **supported** | **supported** | on |
| `suma_post_tool` — oversized tool/MCP result spill + pointer | **unsupported** | **unsupported** | off |
| writer / `propose_patch` | **not implemented in v1** | **not implemented in v1** | refused at load |

## Why `local_gate` and `reader` are supported

| Host | Pre-tool hook | Model bridge |
| --- | --- | --- |
| Hermes | `pre_tool_call` fires inside `handle_function_call()` before the tool handler runs, and returning `{"action": "block", "message": ...}` short-circuits the call. | `ctx.llm.complete(..., model="gpt-5.6-luna")`, with the model override gated per plugin by `plugins.entries.<id>.llm`. |
| OpenClaw | `api.on("before_tool_call", ...)` runs before tool execution, can deny the call, and the host fails this hook closed on timeout. | The runtime model bridge, pinned to `gpt-5.6-luna`. |

If the host cannot serve `gpt-5.6-luna`, the reader returns `MODEL_ERROR`. It never
downgrades to another model: an answer from a different model is not the answer the
acceptance gates measure.

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
silently invalidating this table.

## What the disabled mode still gets you

The spill engine itself is implemented, exercised and passing. `unit bounded-output` runs
the shared `contracts/v1/conformance/spill-cases.json` corpus in both cores: oversized
string, object, array and content-block results all spill with **zero** model calls and no
heuristic summary; image/audio/resource blocks are refused; quota exhaustion, write
failure, readback mismatch, cycles, excessive depth and node counts all produce a bounded
`SPILL_FAILED` or `LIMIT_EXCEEDED` envelope and never the raw payload.

So the engine is not the blocker — host enablement is. Keep those two facts separate when
reading any report: *the engine passes its gates*, and *no supported host enables it*.

## Tool coverage

Only the tool ids below are gated. A read tool outside these lists is **not** protected,
and the capability report says so rather than implying blanket coverage. A deployment that
needs comprehensive protection must disable uncontrolled read tools at the host.

| Host | Read | Search | Shell |
| --- | --- | --- | --- |
| Hermes | `read_file`, `read`, `view_file` | `search_files`, `grep`, `search` | `terminal`, `bash`, `shell`, `execute_command` |
| OpenClaw | `read`, `read_file`, `fs_read` | `grep`, `search`, `fs_search` | `exec`, `bash`, `shell` |

## Gate status

| Category | Status |
| --- | --- |
| Deterministic unit gates (contract, pre-read, reader, citations, no-raw-leak, bounded-output, cancellation, permissions, no-writes, capability) | implemented, passing, no host or provider needed |
| `integration <host> --mode unsupported` | implemented, passing — deterministic fail-closed behaviour |
| `integration hermes --mode local` | implemented; runs against a real `hermes-agent` checkout, NOT_RUN without one |
| `integration openclaw --mode local` | implemented; runs against a real `openclaw` checkout, NOT_RUN without one |
| `integration <host> --mode post-tool` | NOT_RUN by design — the mode is unsupported on both hosts |
| `benchmark core` | implemented, passing — gate/spill latency, envelope caps, context savings, bounded memory |
| `benchmark all` | includes a NOT_RUN provider half: reader latency and token cost need live Luna |
| `eval luna` | implemented harness with a fixed 40-item corpus; NOT_RUN without live Luna |
| `packaging all` | implemented, passing |
| `release all` | runs everything; fails while `eval luna` and the provider benchmark are NOT_RUN |

## Future work, stated plainly

- A live runtime sentinel measurement of capture/truncation/persistence/context-insertion
  order inside a running gateway, for either host. The current gate verifies the ordering
  from host source, which is enough to keep the mode off but is not a runtime measurement.
- A controlled MCP producer wrapper for Hermes that would make `suma_post_tool`
  supportable there.
- The optional writer contract (`operation: propose_patch`). v1 refuses it; a future
  version needs its own schema version, write scopes, conflict checks and acceptance.
