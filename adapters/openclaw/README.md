# OpenClaw adapter

[Project README](../../README.md) | [繁體中文](../../README.zh-TW.md)

Install, uninstall, cleanup and migration instructions live in
[`docs/install.md`](../../docs/install.md). The mode matrix — including the model
attribution ceiling on this host, the session-lifecycle rule, and the official middleware capture boundary — is in
[`docs/capability-matrix.md`](../../docs/capability-matrix.md).

This adapter registers three read-only tools and no writer. It does **not** register
`context_shunt_import`: the external-artifact boundary exists only in the Python core, so
the capability report says `artifact_import: unsupported` with reason
`IMPORT_UNIMPLEMENTED`. That names a core gap rather than a host limitation — nothing about
OpenClaw prevents the mode.

| Tool | Returns | Model calls |
| --- | --- | --- |
| `context_shunt_read` | a cited answer, or labelled bounded legacy compaction after exhausted availability or citation verification failure | at least one per processed chunk; retries/fallback can add more |
| `context_shunt_inspect` | exact snapshot bytes, capped per page and cumulatively | zero |
| `context_shunt_stats` | this session's own token accounting | zero |

Run `./scripts/verify integration openclaw --mode local` against a real host checkout to
check the wiring; without one it reports `NOT_RUN`, never a pass.

Tool registration follows host capability. `reader.enabled: false` controls execution and
returns a bounded refusal without calling a model; it does not require the adapter to hide
an otherwise registerable tool.

The adjacent OpenClaw `llm` policy must authorize the target requested in plugin `config`.
An empty `reader.provider` passes a bare model to OpenClaw so the host owns provider routing;
an explicit provider is pinned. The isolated completion path can prove the host's
post-policy selection (`resolved`), not a provider-authoritative `actual` model. See the checked
[`openclaw.json`](../../examples/config/openclaw.json).

OpenClaw selects the shared TypeScript legacy compactor after exhausted availability or citation verification failure.
It returns `partial/LEGACY_COMPACTED`, `result_kind: legacy_compaction`, and
`provenance.derived: false`, with an empty answer/citations and a dedicated bounded summary.
It is independent of the question and covers only the first requested source, retaining
all handles and omissions. If compaction cannot be published safely, the bounded reader
failure remains; it does not downgrade to an exact prefix. The older `automatic_extract`
and `fallback_max_bytes` fields remain accepted for configuration compatibility, but do not
control this OpenClaw availability fallback. Generic TypeScript sessions retain the old
automatic-extraction default unless the legacy session option is selected.

## Official tool-result middleware

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
and `upstream_truncated=null`; earlier producer/reducer loss is unknown.
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
