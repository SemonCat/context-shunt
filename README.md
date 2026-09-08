# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

Keep large reads out of the main model context by routing question-specific reading to a
cheaper or configurable reader, with citations verified against immutable snapshots.

> **Status: pre-release and read-only.** Pre-read interception is supported on Hermes
> 0.18.2 and OpenClaw 2026.9.2. Interception of oversized post-tool results is unsupported
> on both hosts. There is no writer or `propose_patch`, and this is not a production-ready
> claim.

## Why context-shunt exists

Raw file reads spend the main model's context on source text before the agent knows which
part it needs. The current default gate allows full text reads through 350 physical lines
and 16 KiB; larger reads can displace the conversation, instructions, and useful working
state.

Blind truncation and heuristic summaries save space by deciding what matters in advance.
That is exactly when a rare condition, a negative result, or the answer itself can vanish.
context-shunt keeps the source available and asks a narrower question instead.

## How it works

```text
host read request
        |
        +-- small or provably bounded ----------> original host tool
        |
        `-- oversized / unprovable
                    |
             block before execution
                    |
        authorize under workspace_roots
                    |
         immutable private snapshot
                    |
         question-specific reader
                    |
          citation verification
                    |
            bounded answer ------> main model context
                    |
                    `------------> inspect exact text when needed
```

The gate covers the host tool identifiers listed in its capability report. Deployments
that require complete coverage must disable any uncontrolled raw-read tools.

## Core guarantees

- **Question-aware reading.** Every processed chunk receives the original question. The
  reader gets an authorized excerpt, not the host conversation or a set of tools.
- **Verified quotations and honest coverage.** Deterministic code checks each published
  quote against the snapshot and reports omitted or unprocessed chunks. This proves that a
  quotation exists at the cited location; it does not prove that the quote supports the
  model's reasoning.
- **An exact-text escape hatch.** `context_shunt_inspect` returns bounded line, byte, or
  literal-search results without a model call.
- **Immutable, scoped snapshots.** `workspace_roots` is an allowlist. Secret, binary, and
  unsafe sources are refused. Private blobs expire through TTL and session cleanup.
  Inspection budgets and deletion are disclosure controls, not a confidentiality guarantee
  or secure erase.
- **Truthful accounting.** Session records separate main-context tokens saved from reader
  input/output, label exact versus estimated counts, and include physical retry and fallback
  attempts. The project does not infer currency savings from token counts.

## Three read-only tools

| Tool | Use it for | Model calls |
| --- | --- | --- |
| `context_shunt_read` | Ask a question about one or more authorized paths or existing snapshot handles. | At least one per processed chunk; retries and availability fallbacks can add calls. |
| `context_shunt_inspect` | Retrieve exact lines, UTF-8-safe byte ranges, or literal-search matches from a snapshot. | Zero |
| `context_shunt_stats` | View bounded token and disclosure accounting for the current session. | Zero |

Adapters may keep the reader tool registered when `reader.enabled` is false; execution is
then refused before any model call.

## One read, end to end

Ask the registered tool a concrete question:

```json
{
  "question": "What is the retry ceiling?",
  "paths": ["/workspace/service/retry.py"]
}
```

Selected fields from the returned envelope might look like this:

```json
{
  "status": "ok",
  "code": "ANSWERED",
  "answer": "Retries stop after three attempts [c1].",
  "citations": [
    {
      "id": "c1",
      "locator": {"kind": "lines", "start": 41, "end": 41},
      "quote": "max_retries = 3",
      "verified": true
    }
  ],
  "coverage": {
    "complete": true,
    "processed_chunks": 1,
    "planned_chunks": 1,
    "omitted": [],
    "upstream_truncated": false
  }
}
```

This display omits opaque source and snapshot identifiers, provenance, recovery, and
accounting fields. See the [tool argument schema](contracts/v1/tool-args.schema.json) and
[versioned contracts](contracts/v1/) for complete shapes.

## Quick start

Use Python 3.11 or newer and Node 22.22.3 or newer. From an existing checkout:

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

Merge the executable [Hermes example](examples/config/hermes.config.yaml) into
`~/.hermes/config.yaml`, set `workspace_roots`, and restart Hermes. Its plugin `llm` policy
must authorize the requested model/provider. `auxiliary.context_shunt_reader` overrides the
plugin reader defaults; Hermes value `auto` means inherit.

### OpenClaw

```bash
npm run build --workspace @context-shunt/core
openclaw plugins install --link ./adapters/openclaw --force
openclaw plugins enable context-shunt
```

Merge the executable [OpenClaw example](examples/config/openclaw.json) into
`openclaw.json`, set `workspace_roots`, and authorize the reader target in the adjacent `llm`
policy. Then restart the Gateway and confirm what loaded:

```bash
openclaw plugins inspect context-shunt --runtime --json
```

[Installation, upgrade, cleanup, and uninstall](docs/install.md) covers both hosts in
detail.

## What works today

| Capability | Hermes 0.18.2 | OpenClaw 2026.9.2 |
| --- | --- | --- |
| Oversized pre-read gate | Supported | Supported |
| Question-aware reader | Supported; attribution ceiling `unverified` | Supported; attribution ceiling `resolved` |
| Exact inspect and session stats | Supported | Supported |
| Oversized post-tool interception | Unsupported | Unsupported |
| Writer / `propose_patch` | Not implemented | Not implemented |

Neither adapter can prove provider-authoritative `actual` model identity. OpenClaw can
report the host's resolved route; Hermes cannot distinguish a provider report from a
request echo.

Deterministic gates are implemented and require no live provider. Previously recorded
real-host integration evidence covered 122 cases with 0 failures. The production-equivalent
40-item Luna evaluation and provider benchmark remain `NOT_RUN`. Both post-tool gates also
remain `NOT_RUN` because the required host seams are unsupported. `NOT_RUN` is never counted
as a pass; see the [capability matrix](docs/capability-matrix.md) and
[acceptance gates](docs/acceptance.md).

## When the reader cannot answer

Model errors, quota limits, timeouts, malformed output, and invalid citations fail closed.
The oversized original does not fall back into the main context.

- Reuse the returned handle and ask a narrower question; the immutable snapshot need not be
  captured again.
- Use `context_shunt_inspect` for an exact range or literal search.
- Read `coverage` before relying on the answer. Partial work stays marked partial, including
  upstream truncation and chunks omitted by deadlines or limits.

## Configuration and accounting

The reader defaults to `gpt-5.6-luna`; model and provider are configurable, and an empty
provider delegates routing to the host. A fallback chain handles availability only. It does
not replace a weak answer. Numeric limits may be narrowed for a deployment but never widened.

See the [configuration reference](docs/configuration.md) for host policy, deadlines, TTL,
store, disclosure, concurrency, retry, and envelope limits. The [metrics guide](docs/metrics.md)
defines main-context savings, reader usage, estimates, retries, and session scope.

## Documentation map

| Topic | Reference |
| --- | --- |
| Design and trust boundaries | [Architecture](docs/architecture.md), [security](docs/security.md), and [known limitations](docs/limitations.md) |
| Current host support and release evidence | [Capability matrix](docs/capability-matrix.md), [acceptance gates](docs/acceptance.md), and [development status](docs/implementation-plan.md) |
| Configuration and operations | [Configuration](docs/configuration.md), [metrics](docs/metrics.md), and [installation](docs/install.md) |
| Public contracts and storage | [Versioned contracts](contracts/v1/) and [SQLite DDL](contracts/store/v1.sql) |
| Implementations | [Python core](packages/core-py/), [TypeScript core](packages/core-ts/), [Hermes adapter](adapters/hermes/), and [OpenClaw adapter](adapters/openclaw/) |
| Evaluation and verification | [Evaluation corpus](evals/) and [`scripts/verify`](scripts/verify) |

## Contributing

Run the deterministic checks before opening a change:

```bash
./scripts/verify unit all
./scripts/verify packaging all
./scripts/verify benchmark core
npm run typecheck --workspaces --if-present
git diff --check
```

These commands do not turn missing live-model evidence into a pass. Host integration and
live evaluation requirements are defined in the [acceptance guide](docs/acceptance.md).

## License and credits

Licensed under [Apache-2.0](LICENSE). Dependency licenses and bundled notices are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
