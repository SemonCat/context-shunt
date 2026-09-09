# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

An evidence broker for oversized payloads. It keeps a large tool result or file out of the
main model context, hands back an opaque handle, and answers questions about it with
citations verified byte-for-byte against an immutable snapshot.

> **Status: pre-release and read-only.** Artifact import is supported on Hermes 0.18.2;
> pre-read interception on Hermes 0.18.2 and OpenClaw 2026.9.2. Interception of oversized
> post-tool results is unsupported on both hosts. There is no writer or `propose_patch`.
> The shadow A/B's provider-dependent gates are `NOT_RUN`, so this is not a
> production-equivalent claim.

## Why context-shunt exists

Blind truncation and heuristic summaries save space by deciding what matters in advance.
That is exactly when a rare condition, a negative result, or the answer itself can vanish —
and the middle of a log page is where heuristics cut first.

context-shunt persists the full payload before it reaches the context, gives the main model
metadata and a handle, and makes the source reachable two ways: a deterministic search that
calls no model, and a cheap reader that answers one explicit question with verified quotes.
It never generates an unasked-for summary.

## The primary path: oversized tool results

The oversized context that actually costs a session is a **tool result** — a log query
page, a cloud journal page, an issue tracker export, a wiki page — not a source file.
Holding one out of the context requires seeing the complete result before the host
truncates and persists it, and neither supported host provides that ordering. So
`suma_post_tool` is reported unsupported and stays off.

What a host *can* be handed is an artifact somebody else already wrote down. A compactor or
spooler that persists an oversized result and describes it with a manifest has already done
the capture. `context_shunt_import` adopts that artifact:

```text
producer writes artifact + manifest
                |
       context_shunt_import
                |
   every claim re-proven: allowlisted root, canonical regular file,
   no symlink/hardlink, size and digest against the bytes actually read,
   text-or-JSON, secret policy
                |
      immutable private snapshot
                |
   opaque handle + metadata ------> main model context
                |
                +-- context_shunt_inspect: exact text, zero model calls
                `-- context_shunt_read: one question, verified citations
```

Every manifest field and every path inside it is untrusted input. A manifest is a set of
claims, and nothing in it is believed until it has been re-proven against the file; a
refusal leaves no handle and never returns the payload. The import contract is
producer-agnostic — a foreign manifest reaches the core through a translation profile, and
a schema this deployment has not allowlisted is refused even when a profile for it exists.

This is **not** post-tool interception, and the envelope keeps the two apart: an import
publishes `IMPORTED`, never `SPILLED`. It is off by default and needs an explicit import
root plus an allowlisted producer manifest schema.

## The secondary path: oversized file reads

The original pre-read gate, unchanged. Raw file reads spend the main model's context on
source text before the agent knows which part it needs; the current default allows a full
text read through 350 physical lines and 16 KiB and blocks anything larger *before it
runs*, capturing what it withheld.

```text
host read request
        |
        +-- small or provably bounded ----------> original host tool
        |
        `-- oversized / unprovable ------> block, capture, same handle as above
```

This is the only path that can act before an operation happens. It covers the host tool
identifiers listed in its capability report; deployments that require complete coverage
must disable any uncontrolled raw-read tools.

## Core guarantees

- **Nothing is summarized unasked.** The reader runs only for an explicit question. There
  is no automatic generic summary anywhere in this system, and no heuristic fallback to
  produce one when the reader fails. Exhausted availability instead returns a labelled,
  deterministic exact-text escape hatch: a bounded prefix of the first source, never a summary.
- **Question-aware reading.** Every processed chunk receives the original question. The
  reader gets an authorized excerpt, not the host conversation or a set of tools.
- **Reader output is evidence, not a verdict.** An answer arrives with quotes, coverage,
  omissions and mechanically checkable locators. Verification proves a quote exists where
  it says it does; it does not prove the quote supports the claim, and the envelope says so
  rather than implying a conclusion.
- **Verified quotations and honest coverage.** Deterministic code checks each published
  quote against the snapshot and reports omitted or unprocessed chunks. This proves that a
  quotation exists at the cited location; it does not prove that the quote supports the
  model's reasoning.
- **An exact-text escape hatch.** `context_shunt_inspect` returns bounded line, byte, or
  literal-search results without a model call. A wholly unavailable reader automatically
  uses the same guarded disclosure path for a 2 KiB prefix (`reader.automatic_extract`,
  `reader.fallback_max_bytes`; [semantics and limits](docs/configuration.md#automatic-exact-extraction-after-reader-unavailability)).
- **Immutable, scoped snapshots.** `workspace_roots` and `artifact_import.roots` are
  separate allowlists, so brokering a producer's artifacts never widens what an ordinary
  read may capture. Secret, binary, and unsafe sources are refused. Private blobs expire through TTL and session cleanup.
  Inspection budgets and deletion are disclosure controls, not a confidentiality guarantee
  or secure erase.
- **Truthful accounting.** Session records separate main-context tokens saved from reader
  input/output, label exact versus estimated counts, and include physical retry and fallback
  attempts. The project does not infer currency savings from token counts.

## Four read-only tools

| Tool | Use it for | Model calls |
| --- | --- | --- |
| `context_shunt_import` | Adopt an oversized tool-result artifact a producer already persisted. Returns a handle and metadata, never the artifact's bytes. | Zero |
| `context_shunt_read` | Ask a question about one or more authorized paths or existing snapshot handles. | At least one per processed chunk; retries and availability fallbacks can add calls. |
| `context_shunt_inspect` | Retrieve exact lines, UTF-8-safe byte ranges, or literal-search matches from a snapshot. | Zero |
| `context_shunt_stats` | View bounded token and disclosure accounting for the current session. | Zero |

The deterministic escape hatch is first-class: `inspect` needs no provider, no reader
configuration and no model call, and it stays available when the reader is disabled or its
bridge is absent. Adapters may keep the reader tool registered when `reader.enabled` is
false; execution is then refused before any model call. `context_shunt_import` is registered
only where `artifact_import` is configured and the capability probe supports it.

## One import, end to end

Hand over an artifact a producer already wrote:

```json
{ "manifest_path": "/var/lib/your-compactor/artifacts/q-8412.manifest.json" }
```

Selected fields from the returned envelope:

```json
{
  "status": "ok",
  "code": "IMPORTED",
  "answer": "",
  "citations": [],
  "pointer": {
    "source_id": "src_9f2c41b7e0d3a86e",
    "snapshot_id": "sha256:2a97...5aea",
    "bytes": 1048576,
    "internal": true
  },
  "import_receipt": {
    "producer": "your-compactor",
    "manifest_schema": "context_shunt.artifact_import.v1",
    "origin_tool": "log_query",
    "artifact_sha256": "2a97...5aea",
    "bytes": 1048576,
    "upstream_truncated": false
  }
}
```

`artifact_sha256` is the digest of the bytes actually read, not the one the manifest
claimed — by the time the receipt exists the two have been proven equal.

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
| External artifact import | Supported; off until configured | Unsupported (`IMPORT_UNIMPLEMENTED`: Python core only) |
| Oversized pre-read gate | Supported | Supported |
| Question-aware reader | Supported; attribution ceiling `unverified` | Supported; attribution ceiling `resolved` |
| Exact inspect and session stats | Supported | Supported |
| Oversized post-tool interception | Unsupported | Unsupported |
| Writer / `propose_patch` | Not implemented | Not implemented |

Neither adapter can prove provider-authoritative `actual` model identity. OpenClaw can
report the host's resolved route; Hermes cannot distinguish a provider report from a
request echo.

`artifact_import` is unsupported on OpenClaw because the TypeScript core has no import
boundary — a repository gap, not a host limitation, so closing it needs no host change.

Deterministic gates are implemented and require no live provider. Previously recorded
real-host integration evidence covered 122 cases with 0 failures. The production-equivalent
40-item Luna evaluation and provider benchmark remain `NOT_RUN`: a live number is only
reported as this project's when it comes from a route that preserves message roles,
enforces the output cap **and** is production-equivalent, and no route in this repository
is the third of those.

The two non-pass statuses mean different things and the harness prints them differently:

* `NOT_RUN` - a required gate whose prerequisite is absent *here*. Supply it and the gate
  runs. It blocks the release, and it is never counted as a pass.
* `expected_unsupported`, printed `N/A` - a gate that will not run *anywhere*, because the
  seam it needs does not exist. Both post-tool gates and the shadow reader lane are this,
  not `NOT_RUN`; no checkout or credential changes the answer, so they do not block.

See the [capability matrix](docs/capability-matrix.md) and
[acceptance gates](docs/acceptance.md).

## Shadow A/B, and what it does not prove

`./scripts/verify shadow all` compares four lanes over a fixed synthetic corpus: the raw
baseline, a reference emulation of a heuristic head/tail compactor, deterministic retrieval
through the import boundary, and the question-aware reader.

Three gates are measured from the repository alone — main-context token reduction (≥ 60%),
no evidence regression against the raw baseline, and latency for the deterministic
retrieval lane. On the current corpus the retrieval lane keeps every expected quote the
compactor drops from the middle of a page. The reduction is measured over the items the
broker actually brokered; the item it refuses for exceeding the source cap is most of the
whole-corpus baseline, and crediting that counterfactual would make the headline a saving
on a payload no lane can answer from.

Five gates report `NOT_RUN`, and the harness will not score them from a lane that cannot
answer the question they ask: task correctness, semantic evidence support, mechanical
citation validity (the retrieval lane publishes no citations, so scoring it there would be
a vacuous 100%), bounded follow-up rate, and net cost reduction — which additionally needs
a versioned price table this repository does not have. The reader lane is
`expected_unsupported` *unconditionally*, bridge or no bridge - a decision rather than a
missing prerequisite, which is why it is not `NOT_RUN`: scoring a model lane needs a fixed
corpus and fixed thresholds, and [`eval luna`](docs/acceptance.md) is the gate that owns
them.

Nothing here authorizes replacing a live compactor. The broker is additive; the rollout
order and the evidence each step requires are in
[acceptance gates](docs/acceptance.md#what-has-to-be-true-before-the-live-compactor-is-replaced).

## When the reader cannot answer

Model errors, quota limits, timeouts, malformed output, and invalid citations fail closed.
The handle and the bounded inspect path survive; the oversized original does not fall back
into the main context, and there is no heuristic summary to fall back to.

- Reuse the returned handle and ask a narrower question; the immutable snapshot need not be
  captured again.
- Use `context_shunt_inspect` for an exact range or literal search.
- Read `coverage` before relying on the answer. Partial work stays marked partial, including
  upstream truncation and chunks omitted by deadlines or limits.

## Configuration and accounting

The reader defaults to `gpt-5.6-luna`; model and provider are configurable, and an empty
provider delegates routing to the host. A fallback chain handles availability only. It does
not replace a weak answer. Numeric limits may be narrowed for a deployment but never widened.

`artifact_import` is off by default with no roots and no accepted producer schemas; enabling
it without both is a configuration error rather than an allow-all.

See the [configuration reference](docs/configuration.md) for host policy, deadlines, TTL,
store, disclosure, concurrency, retry, import, and envelope limits. The [metrics guide](docs/metrics.md)
defines main-context savings, reader usage, estimates, retries, and session scope.

## Documentation map

| Topic | Reference |
| --- | --- |
| Design and trust boundaries | [Architecture](docs/architecture.md), [security](docs/security.md), and [known limitations](docs/limitations.md) |
| Current host support and release evidence | [Capability matrix](docs/capability-matrix.md), [acceptance gates](docs/acceptance.md), and [development status](docs/implementation-plan.md) |
| Configuration and operations | [Configuration](docs/configuration.md), [metrics](docs/metrics.md), and [installation](docs/install.md) |
| Import contract | [`artifact-import.schema.json`](contracts/v1/artifact-import.schema.json) and its [conformance corpus](contracts/v1/conformance/artifact-import-cases.json) |
| Public contracts and storage | [Versioned contracts](contracts/v1/) and [SQLite DDL](contracts/store/v1.sql) |
| Implementations | [Python core](packages/core-py/), [TypeScript core](packages/core-ts/), [Hermes adapter](adapters/hermes/), and [OpenClaw adapter](adapters/openclaw/) |
| Evaluation and verification | [Evaluation corpus](evals/), [shadow A/B corpus](evals/shadow/corpus.json), and [`scripts/verify`](scripts/verify) |

## Contributing

Run the deterministic checks before opening a change:

```bash
./scripts/verify unit all
./scripts/verify packaging all
./scripts/verify benchmark core
./scripts/verify shadow deterministic
npm run typecheck --workspaces --if-present
git diff --check
```

These commands do not turn missing live-model evidence into a pass. Host integration and
live evaluation requirements are defined in the [acceptance guide](docs/acceptance.md).

## License and credits

Licensed under [Apache-2.0](LICENSE). Dependency licenses and bundled notices are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
