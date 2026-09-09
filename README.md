# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

Keep oversized files and tool results out of the main model's context. context-shunt stores
an immutable snapshot and returns an opaque handle. The main model then sends the handle
and an explicit question to a cheaper reader. Answers include evidence, coverage, and
mechanically verified citations.

> **Pre-release, read-only.** The operator-reported Hermes deployment uses
> `tool_result_capture` and the internal legacy fallback after an atomic cutover; the
> standalone `oversize-tool-result-compactor` plugin is disabled.
> Capture remains off by default for new installations and requires verified host ordering.
> OpenClaw 2026.9.3 supports optional read-only tool-result capture through its official middleware, with ingress and runtime limits.
> Live reader evaluation and provider benchmark gates remain `NOT_RUN`.

## How it works

### Local files: gate before reading

The pre-read gate and question-aware reader flow are inspired by Spotify Portal/Shunt
([design provenance](THIRD_PARTY_NOTICES.md)). By default, a full text read must fit both limits:
350 physical lines and 16 KiB. Oversized or unprovably bounded reads are blocked
before execution; safe sources are captured for a question-aware read. Small or provably
bounded reads can use the original host tool.

Hermes gates `read_file`, `search_files`, and `terminal`; OpenClaw
covers `read` and `exec`, with no search tool registered for gating. This is not blanket
protection for every possible read tool; disable uncontrolled tools if complete coverage is required.

### Hermes tool results: capture first, ask afterward

`tool_result_capture` intercepts eligible oversized MCP/tool
results at `transform_tool_result`, before they enter the main-model context. It stores the
full content received as an immutable artifact and replaces the result with a bounded opaque
handle/pointer and metadata. Capture makes **zero model calls** and produces no heuristic summary.

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

The Hermes hook currently captures oversized **string** results; structured/multimodal
blocks pass through. It cannot restore content a producer already truncated. Hook ordering
was inspected on one Hermes 0.21.1 host, not proven for every installation. New deployments
must verify their own ordering and set both `tool_result_capture.enabled: true` and
`tool_result_capture.host_ordering_verified_locally: true`. The adapter returns a bounded
failure if an eligible oversized capture fails, never the raw result as a fail-open fallback.
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
upstream truncation before relying on an answer.

| Tool | Purpose | Model calls |
| --- | --- | --- |
| `context_shunt_read` | Ask about authorized paths or snapshot handles. | Per processed chunk; retries and model fallbacks can add attempts. |
| `context_shunt_inspect` | Exact lines, UTF-8-safe byte ranges, or literal-search matches. | Zero |
| `context_shunt_stats` | Bounded session token and disclosure accounting. | Zero |
| `context_shunt_import` | Adopt a producer's persisted artifact (Hermes only, when configured). | Zero |

`inspect` is independent of the reader and remains available without a provider, subject
to configuration, per-call and cumulative disclosure budgets, and handle validity. Snapshots
are immutable and session-scoped; TTL and session cleanup limit their lifetime. Workspace
and import roots are separate allowlists. Unsafe, secret, or binary sources are refused;
cleanup is not a promise of secure erase. See the [tool schema](contracts/v1/tool-args.schema.json).

## When the reader fails

On Python/Hermes, exhausted reader retries/model fallbacks ending in eligible `MODEL_ERROR`,
`TIMEOUT`, or `CITATION_INVALID` trigger the ported bounded legacy compactor **inside
context-shunt** (`reader.legacy_compaction: true` by default). Model-identity mismatches and
provenance-policy refusals do not qualify. This is a deterministic heuristic summary of the
first requested source, using signal lines, head/tail sampling, repetition collapsing, and
JSON shaping—not a Luna answer or an exact source range.

The response envelope reports `status: partial`, `code: LEGACY_COMPACTED`,
`result_kind: legacy_compaction`, and `provenance.derived: false`. Its summary lives in
`legacy_compaction`, with empty `answer` and `citations`; coverage stays partial and failed
model attempts remain in accounting. This internal fallback replaces the standalone legacy
plugin after the Hermes cutover.

If compaction is disabled or cannot safely return output, a wholly unavailable reader may
use the secondary, guarded exact-prefix extraction tier when enabled. If neither tier can
safely return output, the response contains only a bounded pointer/failure and recovery
guidance, never raw oversized content. Reuse a valid handle with a narrower question or
inspect a bounded range.
OpenClaw also uses the TypeScript legacy-compactor port after exhausted reader availability or citation verification failure; its trigger set remains narrower than Hermes’s additional terminal-failure triggers.
See [fallback semantics and limits](docs/configuration.md#legacy-compaction-fallback-for-reader-outcomes-automatic-extraction-does-not-cover).

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

OpenClaw 2026.9.3 uses `api.registerAgentToolResultMiddleware` for eligible read-only text/JSON
in embedded tools and OpenClaw-owned Codex dynamic tools. Codex-native results are observe-only.
Capture is off by default; configure exact read-only MCP IDs and disable Tokenjuice/other reducers
atomically when enabling. Host ingress sanitization happens first: ambiguous text/block/details
ceilings are withheld without a handle; snapshots claim only middleware-visible content, never
complete producer bytes. No Luna call occurs at capture. Handles feed the question-aware reader,
with labelled `LEGACY_COMPACTED` fallback after exhausted availability or citation verification failure.
See [coverage and limits](docs/capability-matrix.md#openclaw), [cutover](docs/acceptance.md#openclaw-middleware-cutover),
and [installation and cleanup](docs/install.md).

## Host support and honest accounting

| Capability | Hermes | OpenClaw 2026.9.3 |
| --- | --- | --- |
| Local pre-read gate, reader, exact inspect, session stats/lifecycle | Supported (compatibility baseline 0.18.2) | Supported |
| Tool-result capture | Enabled in the reported 0.21.1 deployment; off by default, requires local attestation | Supported when enabled; eligible middleware-visible results only |
| External artifact import | Supported; off until configured | Unsupported (`IMPORT_UNIMPLEMENTED`) |
| Internal legacy compaction | Supported, default reader-failure fallback | Supported after exhausted availability or citation verification failure |
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
