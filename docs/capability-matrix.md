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

| Mode | Hermes (`hermes-agent` 0.18.2) | OpenClaw (`openclaw` 2026.9.3) | Default |
| --- | --- | --- | --- |
| `local_gate` — block oversized/unprovable reads before execution | **supported** | **supported** | on |
| `reader` — question-driven answers with verified citations | **supported**, attribution ceiling `unverified` | **supported**, attribution ceiling `resolved` | on |
| `deterministic_inspect` — exact snapshot bytes, zero model calls | **supported** | **supported** | on |
| `session_stats` — this session's own token accounting | **supported** | **supported** | on |
| `session_lifecycle` — handles survive a per-turn boundary, revoked on a real one | **supported** | **supported** | on |
| `reader_task_config` — reader appears in host model configuration | **supported** | n/a (plugin config schema) | on |
| `artifact_import` — adopt an oversized tool-result artifact a producer already persisted | **supported** | **unsupported** (`IMPORT_UNIMPLEMENTED`) | off |
| `tool_result_capture` — oversized tool/MCP result capture + pointer (formerly named `suma_post_tool` internally; see [below](#the-suma_post_tool-name-is-retired)) | **unsupported by default**; **supported** with an explicit operator attestation — [see below](#tool_result_capture-on-hermes-021-what-changed-and-what-did-not) | **supported when enabled** via official middleware; eligible read-only results and ingress limits only | off |
| `legacy_compaction` — deterministic reader-failure fallback, ported from the incumbent compactor | n/a (core behavior, not a capability-gated mode; see [below](#legacy-compaction-fallback)) | n/a | on |
| writer / `propose_patch` | **not implemented** | **not implemented** | refused at load |

`deterministic_inspect` and `session_stats` need no provider at all, so they stay supported
even where the model bridge is absent or the reader is disabled.

`artifact_import` is supported and **off by default**: the mode being available is a host
fact, and whether it runs is a configuration decision that requires an explicit import root
and an explicitly allowlisted producer manifest schema.

### The `suma_post_tool` name is retired

Every mention of `suma_post_tool` in this document, in config, and in both cores is now
`tool_result_capture`. The old name was never a product name — it was this project's own
internal shorthand for "the optional oversized post-tool mode" — and it happened to collide
with the name of a real, unrelated service in this operator's own infrastructure (an
existing `suma-cloud-operation-triage` Hermes eval fixture references it). `tool_result_capture`
says what the mode does and cannot be confused with anything else. The config key
`suma_post_tool` and the Python/TypeScript `sumaPostToolEnabled`/`suma_enabled` accessors
remain as deprecated aliases so an existing deployment's config keeps working unchanged;
the **capability mode name itself** — what a capability report actually calls the mode — is
fully renamed, not aliased.

## Why `artifact_import` is supported where `tool_result_capture` is not, by default

These two modes answer the same problem — an oversized tool result — and they are reported
separately because they need different things from the host, by a wide margin.

`tool_result_capture` needs a replaceable boundary before model delivery. Complete-original
capture additionally needs a boundary before truncation: OpenClaw supports only the
middleware-visible representation because ingress sanitization runs first. The evidence
for each host and the supported limits are below; the Hermes ordering finding concerns
one live installation, not every installation.

`artifact_import` needs neither. The producer already captured the result and wrote it to a
file, so there is no interception to get right. All the host has to supply is a way to
invoke the import, which on Hermes is `ctx.register_tool`.

| Host | `artifact_import` | Evidence |
| --- | --- | --- |
| Hermes | **supported** | `ctx.register_tool` exposes `context_shunt_import`, and the boundary is implemented in `context_shunt.artifacts`. No hook ordering is involved. |
| OpenClaw | **unsupported**, reason `IMPORT_UNIMPLEMENTED` | The import boundary exists only in the Python core. This is a repository gap, not a host limitation — OpenClaw can register the tool, so the mode becomes supportable without any host change. The reason is named separately so it is not read as a host constraint OpenClaw does not have. |

Enabling `artifact_import` says nothing about `tool_result_capture`, and the envelope keeps the
two apart at the wire level too: an import publishes `IMPORTED`, never `SPILLED`.

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

## `tool_result_capture` on Hermes 0.21.1: what changed, and what did not

The mode needs two guarantees at once:

1. **Complete capture before truncation** — the adapter must see the whole result before
   the host shortens it, or a captured pointer would silently describe a truncated payload.
2. **Safe replacement before persistence and context insertion** — the pointer must take
   the raw result's place before it can reach the transcript or the model.

Prior revisions of this document reported both guarantees unmet on Hermes, citing
`hermes-agent` 0.18.2 documentation: `transform_tool_result`'s `result` argument is "the
tool's raw result string, post-truncation and post-ANSI-strip" (`CAPTURE_AFTER_TRUNCATION`),
and the host wraps the hook dispatch in `try/except` so a raising handler leaves the
original result in place (`HOST_FAIL_OPEN`).

**Direct, read-only inspection of one operator's own live Hermes 0.21.1 host, 2026-09-09**
(`ssh`, `sudo docker exec hermes sed -n ... model_tools.py`; the exact commands and output
are recorded in the implementation history, not reproduced here since they name a live
internal host) found `CAPTURE_AFTER_TRUNCATION` no longer holds at the dispatch layer that
matters:

```
handle_function_call():
    result = _execute_tool(...)
    _emit(result, ...)                          # fires post_tool_call
    return _apply_transform_tool_result_hook(function_name, function_args, result,
                                              duration_ms, ids)
```

`_apply_transform_tool_result_hook`'s own docstring: *"Runs after `post_tool_call` and
before the result enters context. Fail-open; first string return wins."* No truncation call
is visible between `_execute_tool` returning and the hook running, at that dispatch layer.
`_apply_transform_tool_result_hook` also does **not** forward `user_task` — confirmed by the
same reading — so **there is no question available at this hook under any circumstance**;
requirement #2's "capture, then ask" design is not a choice, it is the only shape this host
surface permits.

`HOST_FAIL_OPEN` is unchanged and unconditional: the hook dispatch is still wrapped in
`try/except`, and a raising handler still yields the *original* result. This adapter's own
`transform_tool_result` handler is written to never raise regardless (see
`adapters/hermes/context-shunt/__init__.py`), so this reason no longer gates the mode either
way — but it is exactly why the handler is that defensive.

**What this finding is not**: a reproducible, version-independent proof about every Hermes
installation this adapter might run against. It is evidence about one running instance,
read once, by the operator who runs it. In particular: `_execute_tool`'s internals were not
audited — an individual registry tool could self-truncate its own output before returning,
which would mean the "complete" half of guarantee 1 does not hold for that tool's results
even though the dispatch-layer ordering does. Nothing here changes `HOST_VERSION_UNVERIFIED`-
style caution about upgrades, and there is still no `scripts/verify integration hermes
--mode post-tool`-equivalent automated gate re-deriving this on every run (see
[Gate status](#gate-status)).

So: **`tool_result_capture` stays `unsupported` (`ORDERING_UNPROVEN`) by default.** It
becomes `supported` only when a deployment sets
`tool_result_capture.host_ordering_verified_locally: true` — an explicit **operator
attestation**, never inferred or assumed, that the operator personally verified their own
installed host's ordering. See [`configuration.md`](configuration.md) and the
[cutover plan](acceptance.md#tool_result_capture-cutover-on-hermes) for what setting it
means and what it does not prove.

| Reason (default, no attestation) | Evidence |
| --- | --- |
| `ORDERING_UNPROVEN` | This adapter ships generically and has no reproducible, version-independent proof of the installed host's capture-before-truncation ordering for every Hermes version it might run against. |
| `HOST_FAIL_OPEN` | `hermes-agent` `model_tools.py`: `_apply_transform_tool_result_hook` runs inside `try/except` and the original result survives a raising handler. This adapter's own hook handler never raises regardless, so this is recorded as context, not as a blocker. |

A controlled MCP producer wrapper that captures and spills before the host's fallback would
still strengthen this further (it would not depend on any per-tool self-truncation
assumption), but building one means changing how the host produces MCP results, which is
out of scope for a plugin and remains [future work](#future-work-stated-plainly).

### OpenClaw

OpenClaw 2026.9.3 / `773b6d8` supports optional capture through
`api.registerAgentToolResultMiddleware(handler, { runtimes: ["openclaw", "codex"] })`.
The manifest declares both runtimes in `contracts.agentToolResultMiddleware`; the installed
plugin must be explicitly enabled. Capture defaults off. The adapter checks the API and
verified host version, and registers once only when enabled; revalidate host upgrades.
The old Hermes `host_ordering_verified_locally` field is accepted but ignored on OpenClaw.

| Surface | Replacement coverage |
| --- | --- |
| Embedded OpenClaw tool results | Eligible read-only text/JSON, before model delivery |
| OpenClaw-owned dynamic tools in the Codex harness | Same middleware coverage |
| Codex-native PostToolUse tools | Observe-only; replacement unsupported |
| Unknown/mutating tools, messaging, `sessions_spawn`, termination/side-effect controls | Excluded; original host semantics retained |

Default eligible IDs are `read`, `web_fetch`, and `web_search`. Add exact MCP IDs through
`tool_result_capture.read_only_tools` only after verifying the producer is read-only.
A name is an operator declaration, not proof of a tool's behavior. Messaging/session and
known mutating names cannot be opted in. Results with control details or extra top-level
control fields are excluded. Captured error results retain bounded status/ok/isError/exitCode/signal
facts; a context-shunt capture error does not relabel the tool's own outcome.
Non-text/image/unknown blocks on an eligible tool are withheld with a bounded refusal.

The shared TypeScript spill engine deterministically serializes the middleware-visible
result (content and JSON details), measures UTF-8 bytes against `max_tool_result_bytes`,
and publishes an immutable artifact through the existing store/session identity before
returning a bounded envelope and handle. Short eligible results pass unchanged. Capture
makes no Luna call. The model supplies a real question to `context_shunt_read` with the
handle; exhausted availability or citation verification failure uses labelled `LEGACY_COMPACTED` / `legacy_compaction`.
Serialization, store, or handler failure never returns the original eligible oversized text.
The host runner independently fails closed to its bounded middleware error and preserves
its special successful-delivery fallback.

**Ingress ceiling:** `src/agents/harness/tool-result-middleware.ts` sanitizes before the
first handler: 200 content blocks, 100,000 UTF-16 characters per text aggregation,
100,000 details bytes, and 5,000,000 image data characters. The adapter refuses text at
99,999 characters or above (safe-surrogate truncation can leave 99,999), 200 blocks or
more, details at 100,000 bytes or above, and the host's `truncated: true` details marker.
No handle or complete snapshot is published for those inputs; the bounded 100k raw text
is withheld. Coercion can also merge/drop blocks or sanitize details without a marker.
Thus even below detectable ceilings the immutable artifact is the complete **middleware-visible
representation**, never a promise of the original producer bytes. `coverage.complete=false`
and `upstream_truncated=null`; earlier producer/reducer loss is unknown. Spill accounting
conservatively uses `host_truncated_observed` for this sanitized view and never credits an
unobserved larger original. Cap refusals publish no artifact or complete-capture credit.
Recovering complete originals above host ingress caps requires an upstream host seam/change
or producer-side bounded queries; this plugin does not patch OpenClaw.

**Ordering:** `src/plugins/agent-tool-result-middleware.ts` enumerates registry order and
`agent-tool-result-middleware-loader.ts` appends lazy-loaded handlers. The official options
have no priority field. Disable Tokenjuice and every competing result reducer in the **same
configuration transaction** that enables context-shunt capture. A reducer running first can
make complete capture impossible. This is an operator cutover prerequisite, not an ordering
claim inferred from plugin names or the ignored Hermes attestation field.

The deterministic host integration gate uses the real installed loader and runner, checks
manifest entitlement/explicit-enablement source guards, and exercises both runtimes,
oversized sentinels, ingress clipping, and host exception/invalid-output/delivery fallbacks.
It does not claim live gateway, provider, Codex-native replacement, or production cutover results.

## What the capture engine gives you regardless of the capability determination

The spill/capture engine itself is implemented and exercised by `unit bounded-output`,
which runs the shared `contracts/v1/conformance/spill-cases.json` corpus in both cores:
oversized string, object, array and content-block results all spill with **zero** model
calls and no heuristic summary; image/audio/resource blocks are refused; quota exhaustion,
write failure, readback mismatch, cycles, excessive depth and node counts all produce a
bounded `SPILL_FAILED` or `LIMIT_EXCEEDED` envelope and never the raw payload.

So the engine is not the host blocker. Keep those facts separate when reading a report: the
deterministic gate has an executable implementation regardless of whether any given host
enables the mode. On OpenClaw enabled capture uses the official middleware under the limits above. On Hermes it is off by default and requires an explicit operator attestation
to turn on — see [above](#tool_result_capture-on-hermes-021-what-changed-and-what-did-not).
Whether the gate passed a particular checkout comes from that run's result either way.

## Legacy-compaction fallback

`legacy_compaction` is core session behavior, not a capability-gated mode;
it needs the reader's snapshot store, not a host hook. After retries and the model fallback
chain are exhausted, `context_shunt_read` tries it first for terminal `MODEL_ERROR`,
`TIMEOUT`, or `CITATION_INVALID`, including wholly unavailable readers. If disabled or
unsafe, the narrower availability-only `automatic_extract` tier may run next; otherwise
the original bounded failure remains. TypeScript ports the same algorithm; OpenClaw selects it on exhausted reader availability or citation verification failure. The Python configuration/extra trigger codes below remain Hermes-specific.
It is a direct, function-for-function port of the text/JSON-shaping
half of the incumbent `oversize-tool-result-compactor` plugin (v0.3.0, read read-only from
the same live host on 2026-09-09; see `packages/core-py/src/context_shunt/legacy_compact.py`'s
module docstring for exactly what was and was not ported). It always publishes
`result_kind: legacy_compaction`, `provenance.derived: false`, and `status: partial` — a
heuristic summary, never claimed as exact bytes and never claimed as a model's own reading
of the source. `reader.legacy_compaction` (default `true`) and
`reader.legacy_compaction_max_chars` (default `16000`, range 1000–60000) control it; see
[`configuration.md`](configuration.md). A reported-model mismatch and a `require_match`
policy refusal are both deliberately excluded from this fallback — see the code comment
next to `_LEGACY_COMPACTION_TRIGGER_CODES` in `session.py` for why.

## Tool coverage

Only the tool ids below are gated. A read tool outside these lists is **not** protected,
and the capability report says so rather than implying blanket coverage. A deployment that
needs comprehensive protection must disable uncontrolled read tools at the host.

| Host | Read | Search | Shell |
| --- | --- | --- | --- |
| Hermes | `read_file` | `search_files` | `terminal` |
| OpenClaw | `read` | none | `exec` |

Tool *coverage* is about the host's own read tools. The import boundary is not a gate over
a host tool: it is a tool of its own that a deployment opts into, so it does not appear
here.

The three core read-only context-shunt tools are registered on both hosts (Hermes adds
`context_shunt_import` where a deployment configured it), but their advertised
parameter schema is currently the portable subset described in
[`limitations.md`](limitations.md): optional read selectors and per-call inspect budget
overrides exist in the internal contract but are not uniformly exposed by host registration.

## Gate status

| Category | Status |
| --- | --- |
| Deterministic unit gates (contract, pre-read, reader, citations, no-raw-leak, bounded-output, cancellation, permissions, no-writes, capability, store, inspect, accounting, artifact-import, bridge-contract) | implemented; no host or provider needed; run them on the release commit for the result |
| `integration <host> --mode unsupported` | implemented — deterministic fail-closed behaviour |
| `integration hermes --mode local` | implemented; runs against a real `hermes-agent` checkout, NOT_RUN without one |
| `integration openclaw --mode local` | implemented; runs against a real `openclaw` checkout, NOT_RUN without one |
| `integration <host> --mode post-tool` | OpenClaw runs the same deterministic host runner gate as `--mode local` (`NOT_RUN` without a checkout); `expected_unsupported` (printed `N/A`) on Hermes without an operator attestation — no environment enables it by default, and it does not block a release. There is no automated gate that re-derives the operator-attested 0.21.1 ordering finding on Hermes; that finding was a one-time, dated, read-only inspection of one live host, recorded in `docs/capability-matrix.md`, not a reproducible checkout-based gate. Setting `host_ordering_verified_locally: true` is an operator decision this gate does not and cannot verify |
| `shadow deterministic` | implemented — four-lane A/B over a fixed synthetic corpus; reports main-context reduction, evidence regression against the raw baseline, and model-free latency for real |
| `shadow reader` | `expected_unsupported` (printed `N/A`) — task correctness, semantic evidence support, mechanical citation validity and follow-up rate are scored by `eval luna`, which owns the fixed corpus, thresholds and runs-per-item; net cost reduction additionally needs a pricing table this repository does not have |
| `benchmark core` | implemented — gate/spill latency, envelope caps, context savings, bounded memory |
| `benchmark all` | includes a NOT_RUN provider half: reader latency and token cost need live Luna |
| `eval luna` | implemented harness with a fixed 40-item corpus; NOT_RUN without live Luna **or** without a route that preserves the system/user role split, forwards the reader's output cap **and** is production-equivalent. No route here is production-equivalent, so this stays NOT_RUN in every environment this repository can reach - still NOT_RUN and not `expected_unsupported`, because a production-equivalent route is a thing that can exist (see [Acceptance](acceptance.md#reader-evaluation)) |
| `release attest` | implemented — attests the commit, a clean tree, the corpus/prompt/scorer/route/provider-config hashes, and the per-physical-call model identities behind the live evidence. Live evidence must *bind* to this release (same commit, same effective provider configuration, a release-quality route) and must satisfy the required eval outcomes and identity totals; a benchmark report has to be a report rather than an empty object. Evidence that does not bind is NOT_RUN; evidence that binds and contradicts a claim is a failure |
| `packaging all` | builds and inspects archives; clean-installs/imports/uninstalls both npm packages, the Python wheel, and the Hermes copy bundle |
| `release all` | runs everything and finishes with the attestation; exits 0 exactly when every **required** gate passed. `expected_unsupported` gates do not block; `eval luna`, `benchmark provider` and `release attest` are required, so it returns `NOT_RUN` while they lack a live qualifying route |

## Shadow rollout, and what has to be true before anything is replaced

The artifact broker is additive by design. Nothing about it removes or disables an existing
compactor on its own initiative, and it should not: a broker that displaced the incumbent on
deterministic evidence alone would be trading a measured quality claim for an unmeasured one.

> **Operator-override note (2026-09-09).** This operator directed a cutover on their own
> live Hermes host that enables `tool_result_capture`/`legacy_compaction` and disables the
> incumbent `oversize-tool-result-compactor` plugin, without the shadow → score-reader →
> price-reader sequence below having run to completion (`eval luna` and `benchmark
> provider` remain `NOT_RUN` in this repository — see [Gate status](#gate-status)). That is
> the operator's own infrastructure decision to make and is recorded here rather than
> softened or hidden: **the ordering below is this project's default rollout discipline,
> not a claim that this cutover satisfied it.** See
> [`acceptance.md`](acceptance.md#tool_result_capture-cutover-on-hermes) for the cutover
> plan itself, which was prepared but not applied by the agent that wrote it.

The default order, absent an explicit operator override like the one above, is fixed:

1. **Shadow.** Run the broker alongside the incumbent. `./scripts/verify shadow all` gives
   the deterministic half today: main-context reduction, evidence recall against the raw
   baseline, and model-free latency.
2. **Score the reader half.** Task correctness, semantic evidence support, mechanical
   citation validity and follow-up rate all need live reader access. They are `NOT_RUN`
   until then, and a mock number is never a substitute.
3. **Price the reader.** Net cost reduction including retries needs a versioned price
   table. This repository has none, so the gate stays `NOT_RUN` even with a live reader —
   deriving currency from token counts is exactly the inference this project refuses.
4. **Only then consider replacement**, and only for the traffic the shadow actually
   covered.

Until every one of those gates has a real result, the honest statement is that the broker
is deployable and unproven at production equivalence — not that it is better.

## Future work, stated plainly

- A TypeScript import boundary, so `artifact_import` becomes supportable on OpenClaw. The
  reason it is unsupported there names a core gap rather than a host limitation precisely
  because closing it needs no host change.
- A live runtime sentinel measurement of capture/truncation/persistence/context-insertion
  order inside a running gateway, for either host. The OpenClaw gate exercises the installed loader and middleware runner; it is not a live gateway measurement.
- A controlled MCP producer wrapper for Hermes that would make `tool_result_capture`
  provably supportable there without depending on an unaudited per-tool
  self-truncation assumption or an operator attestation.
- A reproducible, checkout-based `integration hermes --mode post-tool` gate that
  re-derives the 0.21.1 dispatch-ordering finding automatically (against
  `CONTEXT_SHUNT_HERMES_ROOT`) on every run, the way `integration hermes --mode local`
  already does for the other modes, so this table's Hermes evidence stops depending on a
  one-time manual reading.
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
