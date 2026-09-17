# Capability matrix

What each supported host can actually do, and what it cannot. A mode is enabled only when
the adapter can prove the host gives it what the mode needs; where the proof does not
exist the mode is reported `unsupported` and stays off even if configuration requests it.
Nothing here is aspirational. Both live context-shunt canaries were retired on 2026-09-10;
supported core behavior below is not a claim of an enabled live deployment.

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
| `local_gate` — block proven large unbounded reads on allowed sources | **supported** | **supported** | on |
| `reader` — question-driven answers with mechanically matched citation quotes | **supported**, attribution ceiling `unverified` | **supported**, attribution ceiling `resolved` | on |
| `deterministic_inspect` — exact snapshot bytes, zero model calls | **supported** | **supported** | on |
| `session_stats` — this session's own token accounting | **supported** | **supported** | on |
| `session_lifecycle` — handles survive a per-turn boundary, revoked on a real one | **supported** | **supported** | on |
| `reader_task_config` — reader appears in host model configuration | **supported** | n/a (plugin config schema) | on |
| `artifact_import` — adopt an oversized tool-result artifact a producer already persisted | **supported** | **unsupported** (`IMPORT_UNIMPLEMENTED`) | off |
| `tool_result_capture` — oversized tool/MCP result capture + pointer (formerly named `suma_post_tool` internally; see [below](#the-suma_post_tool-name-is-retired)) | **unsupported by default**; candidate support requires the 0.21.3 host seam, exact-host probe, and both operator attestations — [see below](#tool_result_capture-on-hermes-021-what-changed-and-what-did-not) | **unsupported**; effective replacement unproven; pass-through | off |
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
two apart at the wire level too: a successful import publishes `IMPORTED`, never `SPILLED`.
If the output guard rejects its pointer envelope, the effective result is an error with
`delivery_boundary=envelope` and zero baseline credit. Adoption alone does not prove delivery.

## Why `local_gate` and `reader` are supported

| Host | Pre-tool hook | Model bridge |
| --- | --- | --- |
| Hermes | `pre_tool_call` fires inside `handle_function_call()` before the tool handler runs, and returning `{"action": "block", "message": ...}` short-circuits the call. | Current hosts: `ctx.llm.complete(task="context_shunt_reader", ...)`, using the plugin-owned auxiliary slot; older supported hosts use the provider/model compatibility path. Overrides remain gated per plugin by `plugins.entries.<id>.llm`. |
| OpenClaw | `api.on("before_tool_call", ...)` runs before tool execution, can deny the call, and the host fails this hook closed on timeout. | `api.runtime.llm.complete` with `execution.mode: "isolated-agent-runtime"` — a fresh, literal-zero-tool completion with no replayed chat history. |

## Model attribution: the ceiling on each host

Neither host proves which model generated the tokens. Rather than a release blocker, this is
now a reported capability boundary: the envelope states what can be proven and no more, and
`actual_model` is never synthesized from `requested_model`.

| Host | Best attainable `attribution_status` | Evidence |
| --- | --- | --- |
| Hermes | `resolved` on the current task-aware host; `unverified` on the compatibility path | With `task=`, `PluginLlm` asks `auxiliary_client.call_llm` for `route_info`, returns that post-policy provider/model, and records the task in `result.audit`; the adapter reports those fields as `resolved_*`. Older task-agnostic builds cannot separate a provider report from an override echo, so they stay `unverified`. Neither path claims `actual`. |
| OpenClaw | `resolved` | The isolated path returns `selection.provider` / `selection.modelId` through `runIsolatedAgentRuntimeCompletion` (`src/plugins/runtime/runtime-llm.runtime.ts`). That is the host's own post-policy selection — a routing fact, not a provider confirmation — so it is reported as `resolved_*`. |

A value that *contradicts* the request is a hard `MODEL_ERROR` on both hosts under either
policy: a different model is a wrong answer, not a weakly attributed one.

`reader.attribution_policy: require_match` refuses anything below `actual`/`resolved`. It
works on the current task-aware Hermes route and disables the reader on the older
task-agnostic compatibility path; `inspect` and `stats` keep working either way.

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

<a id="tool_result_capture-on-hermes-021-what-changed-and-what-did-not"></a>
## `tool_result_capture` on Hermes 0.21.3: exact host boundary

The mode needs three guarantees at once:

1. **Complete capture before truncation** — the adapter must see the whole result before
   the host shortens it.
2. **Safe replacement before context insertion** — the pointer must replace the raw result
   before the transcript/model consumes it.
3. **Invocation-scoped consumer delivery** — the hook must receive an immutable snapshot
   proving that this caller, not merely the process-global registry, can consume read and
   inspect directly or through the scoped deferred bridge.

Read-only inspection of the running image
`context-shunt/hermes:5492046470eb-v2026.9.14` (Hermes `0.21.3`) on 2026-09-16 found that
the first two dispatch-layer conditions hold: `model_tools.py` executes the tool, emits
`post_tool_call`, then calls `_apply_transform_tool_result_hook`, with no intervening
truncation. The hook remains fail-open, so the owned handler never raises. Individual tools
can still self-truncate before returning; the ordering attestation remains operator-owned.

The same source inspection initially appeared to show a missing transform-hook seam:

- `agent/agent_init.py:1061-1066` produces the final model-visible tool names.
- `agent/tool_executor.py:1536-1551` copies those names and passes them, plus enabled and
  disabled toolsets, to `handle_function_call`.
- `model_tools.py:858-909` preserves those values across normal and deferred `tool_call`
  dispatch.
- `model_tools.py:834-848,941-945` does not attach them directly to `transform_tool_result`.
- `tools/tool_search.py:524-530` already exposes the scoped pre-assembly deferred universe.

The exhausted official API search found a simpler existing route:
`agent/turn_api_request.py:139-158` applies every `llm_request` middleware and then fires
`pre_api_request`; `agent/api_request_hooks.py:134-144` serializes the provider-bound body;
the hook carries `session_id`, `task_id`, `turn_id`, and `api_request_id`. Tool dispatch
later carries those same ids. The adapter observes only this post-middleware array, parses
deferred names only from complete deterministic Hermes catalog groups, and correlates an
exact request under a bounded one-hour/512-entry cache. Truncation, malformed groups,
missing ids, partial consumers, expiry, reset, and finalize all narrow to no pointer.
Global registration and configured platform toolsets are never capability evidence.

Why “just guarantee the plugin tools are loaded” is not the authorization boundary:

- plugin toolsets are default-on only while platform resolution allows them; an explicit
  platform list can omit them and `agent.disabled_toolsets` is applied last;
- cron jobs carry their own `enabled_toolsets` and may intentionally be terminal-only;
- delegated children inherit or intersect the parent scope and cannot gain an omitted
  toolset;
- `tools.tool_search.enabled: off` makes selected plugin tools eager/direct but does not
  select the toolset for a restricted agent; with normal tool search, plugin tools are
  deferred and only a complete model-visible catalog listing proves their names;
- therefore forcing `context_shunt` into every agent would widen intentionally restricted
  scopes. The adapter instead observes each provider request and leaves unproven callers on
  bounded fallback.

The standard-library probe
[`evals/hermes-host-contract/probe.py`](../evals/hermes-host-contract/probe.py) runs inside
the exact image without network or providers:

- unmodified 0.21.3 without a provider-request observation: every caller receives
  `LEGACY_COMPACTED` (`EXPECTED_NO_OBSERVATION`), the red-capable control;
- the same unmodified image with the official observer: direct and deferred calls receive
  `SPILLED`, direct read returns `ANSWERED`, direct and deferred inspect return `EXTRACTED`,
  a per-turn end preserves the handle, finalization returns `SOURCE_EXPIRED`, concurrent
  capable/restricted scopes remain isolated, and terminal-only/partial/unobserved callers
  still receive no-handle `LEGACY_COMPACTED`.

So `tool_result_capture` remains unsupported by default. Registration requires both
`host_ordering_verified_locally: true` and
`host_consumer_scope_verified_locally: true`; the second flag is retained as an explicit
operator canary/rollout interlock. `enabled: true` or the ordering attestation alone
registers no capture middleware. Even after registration, every invocation fails closed to
bounded no-handle compaction when correlated provider evidence is absent or incomplete.

| Reason (default, missing attestation) | Evidence |
| --- | --- |
| `ORDERING_UNPROVEN` | No version-independent proof covers every installed host or per-tool self-truncation path. |
| `CONSUMER_SCOPE_UNPROVEN` | The operator has not attested the exact-image post-middleware observer canary. Configured/global tools alone are insufficient. |
| `HOST_FAIL_OPEN` | The host preserves the original result if a transform handler raises. The adapter therefore converts every owned oversized failure to a bounded envelope and never relies on host fail-open. |

### Hermes authoritative skill boundary

Exact identity evidence was inspected in the already-present local `hermes-agent-skill-insights`
checkout at `a4c6b23ba1be20aa469921b59b9abede80053191`:
`tools/skills_tool.py` registers `name="skill_view"` with `SKILL_VIEW_SCHEMA` and
`_skill_view_with_bump`; `website/docs/user-guide/features/skills.md` identifies
`skill_view(name)` as full content plus metadata and `skill_view(name, path)` as a
specific reference file. The dispatch evidence above forwards `function_name` to the
result hook. The exemption therefore uses tool identity, not a payload path heuristic.

Hermes result capture uses an exact identity classifier before session, artifact,
provider, or accounting work. Names are trimmed and lowercased consistently with the
pre-read gate. Protected results pass verbatim: `skill_view`, `skills_list`, `clarify`,
`todo`, every registered `context_shunt_*` tool, and full generated MCP identities
`mcp__<server>__list_resources`, `list_prompts`, and `get_prompt`. Catalog/prompt
identities stay protected even when explicitly allowlisted.

Only `read_file`, `search_files`, and additional identities in the Hermes-only
`capture_tool_allowlist` configuration are eligible. Unknown, interaction, control,
and write tools (including unrestricted `terminal`) default to passthrough. An exact
operator allowlist entry can opt an additional tool into capture, but cannot override
protected identities. Similar names such as `context_shunt_read_fake` have neither
protected status nor default capture eligibility. Paths, `SKILL.md`, payload text,
and arbitrary name substrings never determine classification.

MCP `read_resource` requires an explicit exact allowlist entry, for example
`capture_tool_allowlist: [mcp__docs__read_resource]`. Hermes can register a server-native
tool under the same identity. Its registry exposes the current handler, but the result
hook does not supply the executed handler or immutable utility provenance. Looking up
the current registry after execution cannot prove which handler produced the result.
The allowlist is an operator assertion about the intended identity, not automatic
provenance verification; verify server configuration/collisions before adding an entry
and recheck when that configuration changes. No Hermes/package patch is required.

Small and structured/multimodal results remain unchanged. Once an eligible string is
measured oversized, a Shunt-owned internal capture failure returns bounded `LEGACY_COMPACTED` after source safety checks.
`skill_view` also remains exempt from the pre-read gate; a generic `read_file` of a large
`SKILL.md` remains subject to the normal gate and capture. OpenClaw behavior is unchanged.

### OpenClaw

The 2026-09-10 retirement evidence supersedes the earlier middleware capability claim.
The embedded live canary recorded `SPILLED` / `delivery_boundary=pointer` with no usable
model-visible handle while the producer raw receipt remained visible. Exercising a handler
or host middleware runner does not prove the effective model-input boundary.

Automatic capture is **unsupported**, even when requested in config. The adapter does not
install a capture handler on this seam and cannot credit pointer delivery or saved bytes
for results that remain raw. Existing local capture/read/inspect/stats tools remain supported.
The Hermes ordering attestation cannot enable OpenClaw capture.

Re-enabling requires a supported host seam and end-to-end proof: the model-visible result
must contain a resolvable `source_id`/`snapshot_id`, the reader must resolve it in the same
session, and raw sentinel bytes must be absent from effective input and persisted tool
history. Provider/canary proof is `NOT_RUN`; this repair does not deploy or restart anything.

## What the capture engine gives you regardless of the capability determination

The spill/capture engine itself is implemented and exercised by `unit bounded-output`,
which runs the shared `contracts/v1/conformance/spill-cases.json` corpus in both cores:
oversized string, object, array and content-block results all spill with **zero** model
calls and no heuristic summary; image/audio/resource blocks are refused; quota exhaustion,
write failure, readback mismatch, cycles, excessive depth and node counts all produce a
bounded `SPILL_FAILED` or `LIMIT_EXCEEDED` envelope and never the raw payload.

So the engine is not the host blocker. Keep those facts separate when reading a report: the
deterministic gate has an executable implementation regardless of whether any given host
enables the mode. OpenClaw does not enable the engine on its unproven middleware seam. On
Hermes it is off by default and requires the 0.21.3 host seam plus both explicit operator
attestations to turn on — see
[above](#tool_result_capture-on-hermes-021-what-changed-and-what-did-not).
Whether the gate passed a particular checkout comes from that run's result either way.

## Legacy-compaction fallback

Legacy compaction is mandatory core behavior in both languages. Shunt-owned capture, store, reader, citation, inspection, and output-capacity failures use the incumbent bounded deterministic compactor when authorized source bytes are available. The deprecated `reader.legacy_compaction` key cannot disable it. `reader.legacy_compaction_max_chars` still narrows the character ceiling; all byte and wire caps remain enforced. Python prepares a private exact-byte mirror under an HMAC-derived name during capture, and Hermes reveals its absolute `raw_artifact_path` after successful fallback disclosure without post-deadline raw-payload I/O; preparation failure preserves pathless summary/handle fallback. That explicit full-source route is outside `inspect` disclosure accounting. TypeScript/OpenClaw currently retains only the handle recovery route.

Caller errors, unsafe/binary/secret sources, unsupported operations/versions, binding mismatches, expired/changed sources, provenance-policy refusal, attribution mismatch, cancellation, and disclosure exhaustion remain explicit refusals. See [configuration](configuration.md#legacy-compaction-fallback-for-reader-outcomes-automatic-extraction-does-not-cover) for diagnostics, repair limits, and wire semantics. No live capability attestation is inferred from this core behavior.

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
| `integration <host> --mode post-tool` | OpenClaw remains `expected_unsupported`. Hermes is a required exact-host gate: `NOT_RUN` without its checkout/interpreter and `PASS` only when the unmodified exact source matches and the official observer probe proves direct/deferred consumption, restricted/concurrent isolation, lifecycle, no full-raw publication, and accounting correlation. The two operator attestations remain deployment decisions; the probe does not set them in production. |
| `shadow deterministic` | implemented — four-lane A/B over a fixed synthetic corpus; reports main-context reduction, evidence regression against the raw baseline, and model-free latency for real |
| `shadow reader` | `expected_unsupported` (printed `N/A`) — task correctness, semantic evidence support, mechanical citation validity and follow-up rate are scored by `eval luna`, which owns the fixed corpus, thresholds and runs-per-item; net cost reduction additionally needs a pricing table this repository does not have |
| `benchmark core` | implemented — gate/spill latency, envelope caps, context savings, bounded memory |
| `benchmark all` | includes a NOT_RUN provider half: reader latency and token cost need live Luna |
| `eval luna` | implemented harness with a fixed 40-item corpus; NOT_RUN without live Luna **or** without a route that preserves the system/user role split, forwards the reader's output cap **and** is production-equivalent. `bridges.openclaw_inhost` qualifies by using OpenClaw's host-owned `runtime.llm.complete` isolated-agent-runtime path; it still needs a configured host checkout and resolvable Luna route (see [Acceptance](acceptance.md#reader-evaluation)) |
| `release attest` | implemented — attests the commit, a clean tree, the corpus/prompt/scorer/route/provider-config hashes, and the per-physical-call model identities behind the live evidence. Live evidence must *bind* to this release (same commit, same effective provider configuration, a release-quality route) and must satisfy the required eval outcomes and identity totals; a benchmark report has to be a report rather than an empty object. Evidence that does not bind is NOT_RUN; evidence that binds and contradicts a claim is a failure |
| `packaging all` | builds and inspects archives; clean-installs/imports/uninstalls both npm packages, the Python wheel, and the Hermes copy bundle |
| `release all` | runs everything and finishes with the attestation; exits 0 exactly when every **required** gate passed. `expected_unsupported` gates do not block; `eval luna`, `benchmark provider` and `release attest` are required, so it returns `NOT_RUN` until a qualifying route such as `bridges.openclaw_inhost` can resolve live Luna access |

## Shadow rollout, and what has to be true before anything is replaced

The artifact broker is additive by design. Nothing about it removes or disables an existing
compactor on its own initiative, and it should not: a broker that displaced the incumbent on
deterministic evidence alone would be trading a measured quality claim for an unmeasured one.

> **Historical operator-override note (2026-09-09; retired 2026-09-10).** This operator directed a cutover on their own
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
- The optional writer contract (`operation: propose_patch`). It is refused today; a future
  version needs its own schema version, write scopes, conflict checks and acceptance.
- A host that lets a plugin observe the provider's own report of which model generated the
  tokens. Until one exists, `attribution_status: actual` stays reachable by the contract and
  unclaimed by every supported adapter.
- Adapter registration schemas that expose the shared optional read selector and inspect
  per-call budget fields consistently on both hosts.

## OpenClaw synthetic trust-boundary classifier (2026-09-11)

The owned capture engine now classifies normalized exact identities before result,
session, store, provider or accounting work. Protected instruction/control identities
and MCP resource/prompt catalogs override configured eligibility. Defaults include
`read`, `web_fetch`, `web_search`, and `read_mcp_resource`; generated MCP resource
reads require exact operator configuration because native tools can shadow utilities.
Unknown identities pass through regardless of payload size. See the
[full classification contract](../adapters/openclaw/README.md#synthetic-trust-boundary-classification).

Deterministic regressions cover protected oversized verbatim passthrough with zero
capture side effects, spoof names, repeated server underscores, and resource
capture/inspect/read with truthful accounting. This is synthetic coverage for a future
proof-bearing seam. Live capture remains retired, unsupported (`ORDERING_UNPROVEN`),
and unregistered; no new live protection or effective model-visible replacement is
claimed. The existing host-proof gaps and retirement evidence remain unchanged.
