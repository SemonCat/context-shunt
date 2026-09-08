# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

Keep large sources out of an agent's main context without replacing them with a blind
summary. context-shunt blocks oversized or unprovable reads before the host tool runs,
captures only the withheld bytes in a private snapshot store, and lets the agent ask a
specific question. The answer is bounded, identifies partial coverage, and keeps only
citations that deterministic code can verify byte-for-byte against the immutable snapshot.

The project is read-only. It does not edit files, register a writer, provide arbitrary
cache retrieval, redact secrets and continue, or claim that a mechanically valid quote
proves the model's reasoning. `inspect` can intentionally return exact source text, but
only within per-result, per-source, and per-session byte budgets. Those are context and
disclosure controls, not a confidentiality boundary for a source small enough to fit them.

## Why query-aware reading

Heuristic summaries decide what matters before the agent has asked a question. That loses
rare details, hides negative evidence, and is difficult to audit. context-shunt instead
sends the reader the original question with bounded source chunks, preserves an explicit
`coverage` record, and verifies quoted evidence after the model answers. A weak answer can
be refined against the same snapshot or replaced by a deterministic exact inspection; it
is never treated as a trustworthy summary merely because it is short.

```text
host read request
      |
      v
pre-read gate ---- small or provably bounded ----> original host tool
      |
      | blocked before execution
      v
authorize + snapshot ----> SQLite metadata + content-addressed private blob
      |                                      |
      +---- opaque source_id + snapshot_id --+
                         |
          +--------------+----------------+
          |                               |
          v                               v
 query-aware reader                 deterministic inspect
 >=1 call per processed chunk       exact lines/bytes/search
 retries/fallback bounded           zero model calls
          |                               |
          v                               v
 citation verifier + output guard --> bounded envelope --> main context
```

## What is and is not protected

- A full text read over the current default of 350 physical lines, or over 16 KiB, is
  blocked before execution. Targeted reads, bounded searches, and small files may pass.
- Read-like shell commands are allowed only when the classifier proves their output is
  bounded. Unbounded or unclassifiable reads fail closed with `UNCLASSIFIABLE_READ`.
- Only configured `workspace_roots` can become sources. Secret paths/content, binary or
  invalid text, unsafe links, races, and non-regular files are refused.
- The reader receives a fixed instruction, the question, and an authorized excerpt. It
  receives no host conversation, shell, network, or write tools.
- Source payloads and provider error bodies cannot appear in errors, logs, metrics labels,
  retries, fallbacks, or a reader answer outside the published verified quotes. Exact text
  returned by `inspect` is the deliberate exception and is charged as disclosure.
- The gate covers only host tool identifiers listed in the capability report. Disable any
  uncontrolled read tools if complete host coverage is required.

## Quick start

### Verify locally

Requires Python 3.11 or newer and Node 22.22.3 or newer (`node:sqlite` is used).

```bash
python3 -m venv .venv
./.venv/bin/pip install -e 'packages/core-py[dev]'
npm install
./scripts/verify unit all
./scripts/verify packaging all
./scripts/verify benchmark core
```

`scripts/verify` returns 0 for pass, 1 for failure, and 2 for `NOT_RUN`. A missing live
host or model is `NOT_RUN`, never a pass. Reports are written under the gitignored
`reports/` directory and contain no source payloads.

### Hermes

```bash
/path/to/hermes/python -m pip install ./packages/core-py
cp -R adapters/hermes/context-shunt ~/.hermes/plugins/context-shunt
```

Merge [`examples/config/hermes.config.yaml`](examples/config/hermes.config.yaml) into
`~/.hermes/config.yaml`, replace `workspace_roots`, and restart Hermes. The plugin's `llm`
policy must authorize any configured model/provider override. The reader is also exposed as
the Hermes auxiliary task `context_shunt_reader`; values under
`auxiliary.context_shunt_reader` take precedence over the plugin reader defaults, while
Hermes' `auto` value means inherit.

Verify a real checkout with:

```bash
CONTEXT_SHUNT_HERMES_ROOT=/path/to/hermes-agent \
CONTEXT_SHUNT_HERMES_PYTHON=/path/to/hermes/python \
  ./scripts/verify integration hermes --mode local
./scripts/verify integration hermes --mode unsupported
```

### OpenClaw

```bash
npm run build --workspace @context-shunt/core
openclaw plugins install --link ./adapters/openclaw --force
openclaw plugins enable context-shunt
```

Merge [`examples/config/openclaw.json`](examples/config/openclaw.json) into
`openclaw.json`, replace `workspace_roots`, authorize the reader target in the adjacent
`llm` policy, restart the Gateway, and inspect the loaded plugin:

```bash
openclaw plugins inspect context-shunt --runtime --json
CONTEXT_SHUNT_OPENCLAW_ROOT=/path/to/openclaw \
  ./scripts/verify integration openclaw --mode local
./scripts/verify integration openclaw --mode unsupported
```

Full installation, cleanup, migration, and uninstall instructions are in
[`docs/install.md`](docs/install.md).

## The three tools

The host-facing calls below match the fields registered by both adapters; adapters add the
internal `tool` discriminator before validation against
[`contracts/v1/tool-args.schema.json`](contracts/v1/tool-args.schema.json). The internal
contract also supports a read `selector` and per-inspect `max_result_bytes` /
`max_scan_lines`, but OpenClaw's current registered schema does not expose those optional
fields. Portable callers should use the common subset shown here. Opaque ids are
illustrative.

### `context_shunt_read`

Use exactly one source form: `paths` for first capture, or `handles` for another question
against snapshots already returned. The core's internal selector forms are `all`, `lines`,
`records`, and bounded literal `search`; the portable registered call currently uses `all`.

```json
{
  "question": "Which retry limit applies to transient provider failures?",
  "paths": ["/workspace/project/config/runtime.yaml"]
}
```

A successful envelope has this shape (values abbreviated only where marked):

```json
{
  "schema_version": "1.1",
  "request_id": "reader-01",
  "status": "ok",
  "code": "ANSWERED",
  "answer": "The transient retry limit is 1 [c1].",
  "citations": [{
    "id": "c1",
    "source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
    "locator": {"kind": "lines", "start": 42, "end": 42},
    "quote": "max_transient_retries: 1",
    "verified": true
  }],
  "coverage": {"complete": true, "processed_chunks": 1, "planned_chunks": 1,
    "omitted": [], "upstream_truncated": false},
  "sources": [{"source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
    "media_type": "text/plain", "bytes": 4096,
    "expires_at": "2026-09-07T12:00:00Z"}],
  "retryable": false,
  "result_kind": "model_derived",
  "provenance": {"derived": true, "label": "model_generated_answer",
    "requested_provider": "openai", "requested_model": "gpt-5.6-luna",
    "resolved_provider": "openai", "resolved_model": "gpt-5.6-luna",
    "reported_provider": null, "reported_model": null,
    "attribution_status": "resolved", "attribution_confidence": "medium",
    "attribution_policy": "allow_unverified", "attempts_started": 1,
    "usage_complete": true, "citations_mechanically_verified": true,
    "fallback_used": false},
  "accounting_id": "acc_0123456789abcdef"
}
```

Refine without recapturing:

```json
{
  "question": "Show the exact condition that makes that retry transient.",
  "handles": [{
    "source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea"
  }]
}
```

### `context_shunt_inspect`

Returns exact snapshot text with zero model calls. Lines are 1-based inclusive; bytes are
0-based half-open; search uses a literal `needle`, never a regular expression. Continue a
partial page with its opaque `next_cursor` and the same handle, snapshot, and selector.

```json
{
  "source_id": "src_01ab",
  "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
  "selector": {"kind": "lines", "start": 40, "end": 42}
}
```

```json
{
  "schema_version": "1.1", "request_id": "inspect-01",
  "status": "ok", "code": "EXTRACTED", "answer": "", "citations": [],
  "coverage": {"complete": true, "processed_chunks": 0, "planned_chunks": 0,
    "omitted": [], "upstream_truncated": false},
  "sources": [{"source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
    "media_type": "text/plain", "bytes": 4096,
    "expires_at": "2026-09-07T12:00:00Z"}],
  "retryable": false,
  "result_kind": "deterministic_extraction",
  "provenance": {"derived": false, "label": "deterministic_extraction",
    "attribution_status": "not_applicable", "attribution_confidence": "none",
    "attribution_policy": "not_applicable", "attempts_started": 0,
    "usage_complete": true, "citations_mechanically_verified": true},
  "extraction": {"mode": "lines", "source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
    "deterministic": true,
    "segments": [{"kind": "lines", "start": 40, "end": 42,
      "text": "counts:\n  max_citations: 16\n  max_transient_retries: 1"}],
    "result_bytes": 54, "complete": true, "next_cursor": null,
    "lines_scanned": 3, "scan_budget_exhausted": false,
    "disclosed_bytes_source": 54, "disclosed_bytes_session": 54,
    "disclosure_limit_reached": false},
  "accounting_id": "acc_1111222233334444"
}
```

### `context_shunt_stats`

Returns this session's aggregate and a bounded page of operation records. It cannot select
another session, reset counters, change retention, or return source content.

```json
{"page": 1, "page_size": 8}
```

Important total fields are `operations`, `raw_input_bytes`, `baseline_credit_tokens`,
`main_model_envelope_tokens`, `reader_input_tokens`, `reader_output_tokens`,
`reader_cache_tokens`, `main_context_tokens_saved`, `net_tokens_saved`,
`attempts_started`, `attempts_usage_complete`, and `disclosed_bytes`. See
[`docs/metrics.md`](docs/metrics.md) for formulas and the complete record fields.

## Configuration

Shared plugin configuration uses these keys:

| Key | Current default | Meaning |
| --- | --- | --- |
| `workspace_roots` | required | Non-empty allowlist of source roots. |
| `cache_dir` | `$CONTEXT_SHUNT_CACHE` or `~/.cache/context-shunt` | Private store, outside every workspace root. |
| `spill_dir` | legacy alias | Pre-1.1 alias used only when `cache_dir` is absent. |
| `denylist` | `[]` | Extra relative path globs; built-in secret checks still apply. |
| `gate_enabled` | `true` | Enable pre-read blocking. |
| `reader.enabled` | `true` | Controls execution: `false` refuses reads without a model call, although an adapter may still register the tool. |
| `reader.model` | `gpt-5.6-luna` | Requested reader model; configurable since 1.1. |
| `reader.provider` | `""` | Optional provider pin; empty lets the host route. |
| `reader.attribution_policy` | `allow_unverified` | Publish truthful weak attribution, or use `require_match` to refuse it. |
| `reader.fallback_chain` | `[]` | Up to four availability targets; not a quality fallback. |
| `inspect.enabled` | `true` | Enable deterministic exact extraction. |
| `stats.enabled` | `true` | Enable session accounting. |
| `suma_post_tool.enabled` | `false` | Optional post-tool spill request; unsupported on both hosts and therefore never activated. |
| `limits` | contract defaults | Integer overrides may only narrow the values in `contracts/v1/limits.json`. |

`writer.enabled: true` and `operations` containing `propose_patch` are refused. Both public
core loaders reject unknown top-level keys, nested keys, and limit names, as well as invalid
types, widened limits, an empty model, or more than four fallback entries. The OpenClaw
manifest is an additional host-side validation boundary, not the core's only defense.

Current threshold, cache/store, TTL, disclosure, token, concurrency, retry, deadline, JSON,
and paging defaults—and the host-specific `llm` and Hermes auxiliary keys—are listed in
[`docs/configuration.md`](docs/configuration.md). Example configs are executable packaging
fixtures, not pseudoconfiguration.

## Failure and escape-hatch behavior

The reader can fail because the configured target is weak, absent, quota-limited, slow, or
returns malformed output or invalid citations. context-shunt does not turn any of those
into an uncited summary or raw-payload fallback.

- A transient provider error can receive at most one retry, within the same call/input and
  request deadline budgets. The configured fallback chain is tried only for availability.
- A timeout returns `TIMEOUT`; provider failure returns `MODEL_ERROR`; malformed or
  over-cap output returns `INVALID_MODEL_OUTPUT`; no surviving citations returns
  `CITATION_INVALID`; attribution policy refusal returns `PROVENANCE_UNAVAILABLE`.
- The returned `recovery.handles_valid` says whether the snapshot can still be reused.
  Actions may include `RETRY_SAME_QUESTION`, `REFINE_QUESTION_SAME_SNAPSHOT`,
  `INSPECT_HANDLE`, `NARROW_SELECTOR`, or `WAIT_AND_RETRY`.
- Refine the question with `handles` to avoid recapture and make the information need more
  precise. For exact text, call `inspect` with a narrow selector.
- `coverage.complete: false`, `coverage.omitted`, processed/planned chunk counts, and
  `upstream_truncated` prevent a partial answer from masquerading as complete. If envelope
  pressure drops evidence, the affected assertions are removed too.
- Inspect stops at page, scan, per-source, or per-session limits. A cursor never increases
  authority. Exhausted disclosure returns no additional content.
- If capture/store/output guarding fails, no usable handle or raw fallback is published.
  The original oversized operation remains blocked.

## Provenance and accounting

`result_kind: model_derived` means answer text was generated. `deterministic_extraction`
means exact snapshot bytes, while gate decisions, pointers, stats, and failures are also
marked non-derived. Published citations are mechanically verified, but that proves only
that the quote exists at the claimed snapshot location—not that it semantically supports
the model's claim.

Model identity has three independent levels: `requested_*` is what the adapter asked for;
`resolved_*` is the host's post-policy selection; `reported_*` is a provider report when a
host exposes one. `attribution_status` is `actual`, `resolved`, `unverified`, `mismatch`,
`unknown`, or `not_applicable`. No supported adapter currently proves `actual`: Hermes is
limited to `unverified`, while OpenClaw can establish `resolved`. Missing values stay null;
the request is never copied into a stronger identity field.

Accounting is signed:

```text
main_context_tokens_saved = baseline_credit_tokens - main_model_envelope_tokens
net_tokens_saved = main_context_tokens_saved
                   - reader_input_tokens - reader_output_tokens
```

The full-payload baseline is a labelled counterfactual (`full_payload_counterfactual`) and
is credited only once per snapshot. Already-truncated host input uses
`host_truncated_observed`. Envelope and baseline token estimates use `bytes_div_4`; exact
reader usage is used only when reported. Missing provider input/output counts are estimated
from measured prompt/completion bytes and labeled `bytes_div_4`; unavailable cache usage or
no-call fields remain `null`, never a fabricated zero. Every physical retry and fallback
contributes to `attempts_started`; usage completeness records how many attempts supplied
exact usable counts. Stats do not expose currency cost or wall-clock latency; provider
benchmarks report latency and token usage when run, and never invent prices.

## Storage lifecycle and limits

SQLite stores authorization metadata, digested scope, expiry, generation, quotas,
refcounts, disclosure totals, and bounded operation records. It never stores paths,
questions, answers, quotes, previews, provider error bodies, model/provider names, or blob
paths. Immutable payloads live at internally derived content-addressed blob locations.

Directories are reasserted as `0700`, payload files as `0600`; safe opens reject symbolic
links, hard links, FIFOs, devices, and replacements. Handles are scoped to host, profile,
principal, session, and generation. They expire after the current default one-hour TTL,
are swept opportunistically and at startup, and are revoked at real reset/finalize/delete
boundaries—not ordinary turns or compaction. Deletion unlinks bytes; it is not secure erase.

Current defaults include 8 MiB per captured source, 512 live handles, 256 MiB distinct
store content, 16 KiB per inspect result, 256 KiB cumulative disclosure per source, and
1 MiB per session. Deployments may narrow, never widen, these contract caps. Exact values
and their config keys are in [`docs/configuration.md`](docs/configuration.md); security and
retention details are in [`docs/security.md`](docs/security.md).

## Capability and release status

| Capability | Hermes 0.18.2 | OpenClaw 2026.9.2 |
| --- | --- | --- |
| Pre-read gate | supported | supported |
| Query-aware reader | supported; attribution `unverified` | supported; attribution `resolved` |
| Deterministic inspect | supported | supported |
| Session stats/lifecycle | supported | supported |
| Oversized post-tool spill/pointer | unsupported | unsupported |
| Writer / `propose_patch` | not implemented | not implemented |

This matrix is code/source capability evidence, not a claim that every live release gate
passed on this checkout. Deterministic unit, unsupported-mode, core benchmark, and packaging
gates are implemented. The recorded real-host integration evidence executed 122 cases with
0 failures; fresh runs still require user-supplied Hermes and OpenClaw checkouts. The two
post-tool gates remain `NOT_RUN` because the required host seams are unsupported. The 40-item
production-equivalent Luna eval and provider benchmark also remain `NOT_RUN`; no live model
evidence is being presented as a pass. Therefore `release all` is not yet a passing
production release signal. See
[`docs/capability-matrix.md`](docs/capability-matrix.md) and
[`docs/acceptance.md`](docs/acceptance.md).

## Development, compatibility, and support

Two independent cores share the same JSON schemas, conformance fixtures, caps, status-code
table, and normative SQLite DDL:

| Path | Purpose |
| --- | --- |
| [`contracts/v1/`](contracts/v1/) | Request/envelope/tool schemas, limits, status pairs, fixtures |
| [`contracts/store/v1.sql`](contracts/store/v1.sql) | Normative local store schema |
| [`packages/core-py/`](packages/core-py/) | Python core used by Hermes |
| [`packages/core-ts/`](packages/core-ts/) | TypeScript core used by OpenClaw |
| [`adapters/hermes/`](adapters/hermes/) | Hermes adapter |
| [`adapters/openclaw/`](adapters/openclaw/) | OpenClaw adapter |
| [`evals/`](evals/) | Fixed reader evaluation corpus and bridges |
| [`scripts/verify`](scripts/verify) | Verification entry point |

Run `./scripts/sync-contracts --check` after reviewing contract parity; use
`./scripts/sync-contracts` only when intentionally updating root contracts. Contract 1.1
accepts 1.0 requests/envelopes but rejects 1.1 fields disguised as 1.0. DDL revision 1 is
migrated additively; unknown store revisions fail closed and should be cleared. Host
upgrades invalidate compatibility evidence until the local integration gate is rerun.

Before contributing, run `./scripts/verify unit all`, `./scripts/verify packaging all`,
and the relevant integration gate. Do not weaken deterministic tests to compensate for a
missing host or provider. Troubleshooting and safe cleanup are in
[`docs/install.md`](docs/install.md) and [`docs/limitations.md`](docs/limitations.md).

To uninstall, disable/uninstall the OpenClaw plugin or remove the Hermes plugin directory,
uninstall the corresponding core package, remove the host config entry, then stop the host
and delete the configured `cache_dir` if its snapshots are no longer needed. Cache deletion
is irreversible at the application level and is not secure erasure; verify the exact custom
path before removing it. Exact commands and DDL-revision migration guidance are in
[`docs/install.md`](docs/install.md).

The documentation set also includes the normative
[`architecture`](docs/architecture.md), [`security model`](docs/security.md),
[`acceptance gates`](docs/acceptance.md), [`capability matrix`](docs/capability-matrix.md),
[`configuration reference`](docs/configuration.md), [`accounting reference`](docs/metrics.md),
[`limitations`](docs/limitations.md), and current
[`development status`](docs/implementation-plan.md).

## License and notices

[Apache License 2.0](LICENSE). Spotify Shunt and Headroom are credited as design baselines;
no code was copied or adapted. Runtime dependencies and exact third-party relationships are
documented in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
