# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

Keep oversized files and tool results out of the main model's context. context-shunt stores
an immutable snapshot and returns an opaque handle. The main model then sends the handle
and an explicit question to a cheaper reader. Answers include evidence, coverage, and
mechanically verified citations.

> **Retired live canary (2026-09-10), pre-release.** Both context-shunt plugins remain
> disabled. Hermes was rolled back to its incumbent compactor. This repair does not
> re-enable or deploy either plugin. OpenClaw automatic tool-result capture is unsupported:
> the tested middleware did not prove effective model-visible replacement.
> Live reader evaluation and provider benchmark gates remain `NOT_RUN`.

## How it works

### Local files: gate before reading

The pre-read gate and question-aware reader flow are inspired by Spotify Portal/Shunt
([design provenance](THIRD_PARTY_NOTICES.md)). By default, a full text read must fit both limits:
350 physical lines and 16 KiB. Only positively identified, large unbounded reads on allowed sources are blocked
before execution. Unknown commands, unclassifiable tools, searches, and safe bounded reads
pass through unchanged; the gate is a context optimization, not an authorization boundary.

Hermes gates `read_file`, `search_files`, and `terminal`; OpenClaw
covers `read` and `exec`, with no search tool registered for gating. This is not blanket
protection for every possible read tool; disable uncontrolled tools if complete coverage is required.

### Hermes tool results: capture first, ask afterward

`tool_result_capture` intercepts eligible oversized MCP/tool results at
`transform_tool_result`, before they enter the main-model context. A pointer is returned only
when the hook invocation carries a scoped consumer-capability descriptor proving that the
caller can reach both read and inspect, directly or through the complete deferred-tool bridge.
Without that proof, the adapter returns bounded deterministic legacy compaction, creates no
handle, and retains no pointer-only payload. Capture makes **zero model calls**.

The hook **does not receive the user's question**. Reading is a separate step: the main
model must call `context_shunt_read` with an explicit question and the artifact handle.
The reader uses `gpt-5.6-luna` in the deployed example; users can configure the model and provider.

```text
eligible oversized tool result          oversized local read
              |                                |
     tool_result_capture                  pre-read gate
              |                                |
              +---- immutable snapshot --------+
                              |
                   bounded handle → main model
                              |
              explicit question + handle → context_shunt_read
                              |
                 reader → evidence / coverage / locators
                              |
                  mechanical citation verification
                              |
              main model can inspect bounded source ranges
```

The Hermes hook currently receives oversized **string** results; structured/multimodal
blocks pass through. It cannot restore content a producer already truncated. Exact Hermes
0.21.3 source and the running image were inspected on 2026-09-16. The unmodified host does
not forward its session tool scope to `transform_tool_result`, so current invocations cannot
prove pointer consumability and take the no-handle legacy-compaction path. A source-located
host proposal and exact-image probe show the required immutable per-invocation descriptor,
but that third-party change is not made by this repository. The adapter never widens the
caller's tool scope or treats global registration as proof. Capture remains off by default
and requires separate ordering and consumer-scope operator attestations.
Unsafe sources remain explicit refusals, and raw results are never returned as fallback.
See the [capability evidence](docs/capability-matrix.md) and [cutover procedure](docs/acceptance.md#tool_result_capture-cutover-on-hermes).

`suma_post_tool` is only a deprecated configuration migration alias, never the product name.
Use `tool_result_capture` in public configuration.

### Existing artifacts: import without interception

On Hermes, `context_shunt_import` can adopt a text/JSON artifact another producer already
persisted. It validates the manifest, allowlisted root, regular file, size, digest, and
secret policy before creating a private snapshot. It returns `IMPORTED`, not `SPILLED`;
import does not imply post-tool interception. It is off by default and requires explicit
`artifact_import.roots` and allowlisted producer schemas. OpenClaw has no import implementation yet.

## Ask and inspect

Call `context_shunt_read` with the handle returned by capture or import, replacing these
illustrative identifiers with the actual values:

```json
{
  "question": "What is the retry ceiling?",
  "handles": [{
    "source_id": "src_example1234",
    "snapshot_id": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }]
}
```

For an initial local capture, use `"paths": ["/workspace/service/retry.py"]` instead of
`handles`. Every processed chunk receives the question and an authorized excerpt, without
the host conversation or tools. The returned answer includes quotes, coverage, omissions,
and locators. Code verifies quotes against the immutable snapshot byte-for-byte; it does
**not** prove that a quote supports the reader's reasoning. Check partial coverage and
upstream truncation before relying on an answer. When coverage is incomplete, the main
`answer` is deterministically prefixed as reviewed-subset-only; exact totals, exhaustive
negatives, and source-wide conclusions in the model prose remain scoped by that prefix even
if a consumer ignores the envelope's separate coverage and guidance fields. Trusted
capture/import origin truncation is stored with the handle and aggregated across every
source in later reads; legacy handles whose origin is unknown remain incomplete.

| Tool | Purpose | Model calls |
| --- | --- | --- |
| `context_shunt_read` | Ask about authorized paths or snapshot handles. | Per processed chunk; retries and model fallbacks can add attempts. |
| `context_shunt_inspect` | Exact lines/bytes/search, or bounded JSON count/distinct/grouping. | Zero |
| `context_shunt_stats` | Bounded session token and disclosure accounting. | Zero |
| `context_shunt_import` | Adopt a producer's persisted artifact (Hermes only, when configured). | Zero |

`inspect` is independent of the reader and remains available without a provider, subject
to configuration, per-call and cumulative disclosure budgets, and handle validity. Snapshots
are immutable and session-scoped; TTL and session cleanup limit their lifetime. Workspace
and import roots are separate allowlists. Unsafe, secret, or binary sources are refused;
cleanup is not a promise of secure erase. See the [tool schema](contracts/v1/tool-args.schema.json).
For minified one-line payloads, use a literal `search` selector to locate a stable term,
then request only the needed 0-based, half-open UTF-8 `bytes` range. Continue a partial page
by resending the identical selector with `extraction.next_cursor`; restarting `lines: 1..1`
without the cursor restarts the same bounded first page.

For structured minified JSON, schema 1.3 adds `selector.kind: "aggregate"` with a JSON-array
`records_pointer`, optional `expand_pointer`/`record_pointer`/`parse_json`, an exact scalar
or literal filter, and bounded `group_by`/`distinct` pointer lists. It scans only within the
declared record budget and returns deterministic canonical JSON with exact counts and
explicit completeness flags for capped value/group samples. Fully covered reader answers
are reused only for an exact session/snapshot/query/selector/budget/model-contract match;
authorization is rechecked before every hit and partial answers are never cached.

## When the reader fails

Context Shunt is an availability-preserving optimization layer. When Shunt owns a failure and the authorized source bytes or immutable snapshot are available, both cores automatically return the incumbent bounded deterministic compactor output. This is mandatory: `reader.legacy_compaction` and the TypeScript `legacyCompaction` option are deprecated compatibility no-ops, including when set to `false`.

Eligible failures include `MODEL_ERROR`, `TIMEOUT`, `INVALID_MODEL_OUTPUT`, `CITATION_INVALID`, capture/store failures, and unexpected safe internal errors. `LIMIT_EXCEEDED` is classified by detail: store capacity and implementation output/page capacity qualify; source/input safety caps and disclosure policy caps do not. Invalid arguments, unsupported versions/operations, unsafe/binary/secret sources, cross-session or snapshot mismatch, expired/changed sources, provenance-policy refusal, attribution mismatch, cancellation, and disclosure exhaustion remain explicit refusals. Fallback never authorizes a handle that the store cannot authorize.

The response is always `partial/LEGACY_COMPACTED`, `result_kind: legacy_compaction`, and `provenance.derived: false`, with empty `answer` and `citations`. `legacy_compaction.original_failure` retains the failure code; the bounded explicit `failure_detail` enum distinguishes verifier, argument, and capacity failures without carrying arbitrary exception text. Coverage is incomplete, question-independent, and limited to the first requested source. After successful capture, the delivered `source_id`/`snapshot_id` pair resolves the immutable retained source through `context_shunt_inspect`. Python capture also prepares a private exact-byte `.txt` mirror under an HMAC-derived name; Hermes reveals its absolute `legacy_compaction.raw_artifact_path` only after successful fallback disclosure, so timeout publication performs no raw-payload read, write, or fsync. Preparation failure preserves mandatory summary and handle recovery without a path. The mirror is scope/TTL cleanup-bound, while TypeScript/OpenClaw currently exposes the handle interface only. Capture failure before handle publication returns no source handles, no artifact path, and `handles_valid: false`. The compactor retains the incumbent signal lines, head/tail samples, repetition collapsing, and JSON shaping, within character, byte, and envelope caps. Its summary is navigation only: never a question answer, exhaustive coverage, exact count, or citation evidence. The output guard enforces those non-authoritative fields. Inspection fallback also obeys cumulative disclosure limits.

Citation generation gets at most one bounded repair attempt per request, using fixed safe verifier feedback and already-authorized chunks. The same deadline, input/output budgets, provenance checks, and usage ledger apply. A repair that still fails quote-to-snapshot verification uses mandatory legacy compaction; no answer with unmatched citation quotes is published. This mechanical check does not prove the answer's prose. Genuine valid empty answers remain `NO_MATCH`.

Hermes tool schemas are derived from the canonical tool-argument contract. Malformed or mismatched snapshot IDs are refused with fixed diagnostics and guidance to reuse the exact `source_id`/`snapshot_id` pair from the original pointer; hashes are never guessed or repaired. A well-formed mismatch is distinguished from an actual source recapture condition: retry the original pair first, and recapture only when that pair is expired or the source was intentionally refreshed.

## Quick start

Use Python 3.11+ and Node 22.22.3+. From an existing checkout:

```bash
python3 -m venv .venv
./.venv/bin/pip install -e 'packages/core-py[dev]'
npm install
./scripts/verify unit all
```

### Hermes

```bash
/path/to/hermes/python -m pip install ./packages/core-py
cp -R adapters/hermes/context-shunt ~/.hermes/plugins/context-shunt
```

Merge the [Hermes example](examples/config/hermes.config.yaml) into `~/.hermes/config.yaml`,
set `workspace_roots`, authorize the reader model/provider in the plugin `llm` policy, and
restart Hermes. `auxiliary.context_shunt_reader` overrides plugin reader defaults; `auto`
means inherit. The example leaves capture off until you verify local hook ordering.
For migration, follow the atomic cutover procedure linked above so capture is active when
the standalone compactor is disabled.

### OpenClaw

```bash
npm run build --workspace @context-shunt/core
openclaw plugins install --link ./adapters/openclaw --force
openclaw plugins enable context-shunt
```

Merge the [OpenClaw example](examples/config/openclaw.json) into `openclaw.json`, set
`workspace_roots`, authorize the reader target in the adjacent `llm` policy, then restart
the Gateway and inspect the loaded plugin:

```bash
openclaw plugins inspect context-shunt --runtime --json
```

OpenClaw automatic capture is unsupported, even when configured. The retired live canary
showed a pointer accounting record while raw tool content remained visible and the model
had no usable handle. Middleware registration alone is insufficient proof of replacement.
Explicit local capture, reader, inspect, and stats remain available.
See [capability evidence](docs/capability-matrix.md#openclaw).

## Host support and honest accounting

| Capability | Hermes | OpenClaw 2026.9.3 |
| --- | --- | --- |
| Local pre-read gate, reader, exact inspect, session stats/lifecycle | Supported (compatibility baseline 0.18.2) | Supported |
| Tool-result capture | Supported with verified host ordering; live deployment retired | Unsupported; pass-through |
| External artifact import | Supported; off until configured | Unsupported (`IMPORT_UNIMPLEMENTED`) |
| Internal legacy compaction | Supported, default reader-failure fallback | Mandatory for Shunt-owned failures |
| Reader attribution ceiling | `unverified` | `resolved` |
| Writer / `propose_patch` | Not implemented | Not implemented |

Neither adapter proves provider-authoritative `actual` model identity. Requested models,
resolved routes, and provider-confirmed identities are distinct; a request echo is not
proof of model identity. Model/provider configuration and availability fallbacks are
explicit, and a model mismatch is refused.

Token accounting separates main-context savings from reader input/output, marks exact
versus estimated counts, and includes retries and fallback attempts. Token reductions are
not currency savings. Spotify's reported savings are inspiration, **not this project's
measured guarantee**. The deterministic shadow corpus measures a limited retrieval lane;
production-equivalent Luna evaluation and provider benchmarks remain `NOT_RUN`. The deployed
capture path has not satisfied those gates. See [metrics](docs/metrics.md),
[capability matrix](docs/capability-matrix.md), and [acceptance gates](docs/acceptance.md).

## Documentation and contributing

- [Architecture](docs/architecture.md), [security](docs/security.md), and [limitations](docs/limitations.md)
- [Configuration](docs/configuration.md), [examples](examples/config/README.md), and [installation](docs/install.md)
- [Versioned contracts](contracts/v1/), [Python core](packages/core-py/), and [TypeScript core](packages/core-ts/)
- [Hermes adapter](adapters/hermes/), [OpenClaw adapter](adapters/openclaw/), and [evaluation corpus](evals/)

Run the deterministic checks before opening a change:

```bash
./scripts/verify unit all
./scripts/verify packaging all
./scripts/verify benchmark core
./scripts/verify shadow deterministic
npm run typecheck --workspaces --if-present
git diff --check
```

Licensed under [Apache-2.0](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md)
for design provenance and dependency licenses.
