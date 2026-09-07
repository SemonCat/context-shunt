# Third-party notices

## Spotify Shunt (design baseline)

| Field | Value |
| --- | --- |
| Source | `spotify/portal-ai-plugins`, path `plugins/shunt` |
| Exact revision inspected | `3c24ca30ff63e1f5bbad1c43fe5324daff579123` (default branch `main`, committed 2026-08-17) |
| Upstream licence | Apache License 2.0 (repository `LICENSE`; the upstream tree contains no `NOTICE` file at that revision) |
| Relationship | Design baseline only. **No code was copied or adapted.** |

The upstream plugin at that revision is a Claude Code plugin built from Bash: two
`PreToolUse` hooks (`hooks/check-file-size`, `hooks/check-bash-read`), delegation scripts
(`scripts/bulk-read`, `scripts/code-write`) over a shared `scripts/lib/aika.sh`, and two
skills. This repository shares three uncopyrightable ideas with it and nothing else:

1. a pre-read gate that refuses a full read above a line threshold (350),
2. letting an explicitly targeted read through rather than gating it, and
3. asking a cheap model a question with the corpus attached, out of the main context.

Everything here is independently written to a different design, and the differences are
substantive rather than cosmetic: shared versioned JSON contracts, a quote-aware shell
classifier with a tri-state outcome instead of string matching, immutable SHA-256
snapshots with line and JSON-record indexes, session-scoped capability handles,
deterministic citation verification, bounded envelopes behind an output guard, and a pure
spill/pointer mode with no summarization. Upstream's threshold check uses `wc -l`, which
undercounts a file with no trailing newline; this repository defines and tests its own
physical-line counter instead.

Upstream reported 51 passing tests of its own at the revision above. **That is upstream's
result, about upstream's code.** It is not a result of this repository and is never
reported as one. This repository's own results come from `scripts/verify` and are recorded
per run under `reports/`.

## Import record

No third-party code has been imported. The table stays empty until that changes.

| Component / version or commit | Original path → local path | Licence and original notices | Local modifications and date |
| --- | --- | --- | --- |
| _(none)_ | | | |

If code is ever imported, the importer must, at first import: fill in the actual commit
SHA, the upstream file paths and the local paths; keep every original copyright,
attribution and licence header intact; mark modified files as modified with a date; and
copy the `NOTICE` content applicable to the imported revision. Rights holders and notices
must never be filled in by guesswork.

## Runtime dependencies

Pinned or bounded, with their licences:

| Package | Version | Licence | Used by |
| --- | --- | --- | --- |
| `jsonschema` | `>=4.25,<4.26` | MIT | Python core — contract validation |
| `ajv` | `8.17.1` | MIT | TypeScript core — contract validation |
| `ajv-formats` | `3.0.1` | MIT | TypeScript core — `date-time` format |

The snapshot store uses `node:sqlite` and Python's `sqlite3`, both of which are standard
library modules of their respective runtimes. No third-party database driver is vendored or
distributed, and no new runtime dependency was added for the store.

Development-only, not distributed: `pytest`, `ruff`, `typescript`, `vitest`,
`@types/node`. None of these ship inside either plugin package.

Neither package bundles or redistributes host code. `openclaw` and `hermes-agent` are
integration targets resolved at the user's install; their licences are their own.

## Headroom (Compress-Cache-Retrieve, design inspiration)

| Field | Value |
| --- | --- |
| Source | Headroom's Compress-Cache-Retrieve pattern for keeping oversized tool output out of an agent's context |
| Relationship | Design inspiration only. **No code was copied, adapted, translated or read into this implementation.** |

Contract revision 1.1 adopts the *shape* of that pattern: intercepted content is cached
out of the conversation behind an opaque handle, and the agent works against the handle
rather than the payload. The idea that a cache-and-handle indirection is the right way to
keep large tool output out of a context window is not this project's invention, and saying
so is more useful than pretending otherwise.

What is implemented here is independently written and differs in ways that matter:

- **No cached-original retrieval tool.** This is the deliberate divergence. Headroom offers
  retrieval of the cached original back into the main model context; this project registers
  no such tool. Everything reachable is either a cited model-derived answer or a capped,
  cumulatively-limited exact extract, and the cumulative ceiling exists precisely so that
  repeated small extracts cannot reconstitute a *large* original. It is a byte budget, not a
  promise that a source can never come back whole: one small enough to fit the ceilings can
  be returned in full by `inspect`, and the pre-read gate is a context-cost control rather
  than a confidentiality boundary.
- Authorization is a SQL predicate over a trusted (host, profile, principal, session,
  generation) scope, not possession of a cache key.
- The cache is split: SQLite holds authorization only and is forbidden by its own normative
  DDL from holding paths, questions, answers or previews; payloads live in content-addressed
  private files whose location is derived rather than stored.
- Every reply is a bounded, schema-validated envelope with mechanically verified citations
  and truthful model provenance.

No Headroom licence obligations attach to this repository, because no Headroom material is
present in it. This notice exists to attribute an idea, not to satisfy a licence.

## Host and service names

Hermes, OpenClaw, Headroom, Suma and `gpt-5.6-luna` appear here as integration interfaces,
project names and service names. This repository ships none of their SDKs, code or model
weights, and makes no claim about their licences.

This file does not replace any third party's own licence, nor any `NOTICE` that a third
party requires to be preserved.
