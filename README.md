# context-shunt

Keep large sources out of your agent's context. When a read would dump 20,000 lines into
the conversation, context-shunt stops it *before the tool runs* and offers a different
deal: ask a question about the file instead, and get back a short answer whose every
citation has been checked against the bytes it claims to quote.

It is read-only. It never modifies a source, and **no tool it registers can retrieve a
full payload** — everything the agent can reach is either a cited model-derived answer or
a capped, cumulatively-limited exact extract.

```text
        read a 20k-line file                    ask a question about it
                 │                                        │
                 ▼                                        ▼
        pre-read gate (before execution)          question-driven reader
                 │                                        │
       blocked ──┴── allowed if bounded            one call per chunk
                 │                                        │
                 ▼                                        ▼
        capture what was withheld ─────────►  citation verifier → output guard
        (SQLite authorization +                           │
         content-addressed private file)                  ▼
                 │                              ≤16 KiB back to the agent
                 └──► handle ──► inspect: exact bytes, zero model calls,
                                 capped per page *and* cumulatively
```

## What it does

- **Blocks oversized full reads before they happen.** A full read of a text file over
  **350 physical lines**, or over 16 KiB, is refused at the pre-tool hook — so the payload
  never exists, rather than being trimmed after the fact. Targeted reads
  (`offset`+`limit`), bounded searches and small files pass straight through.
- **Refuses read-like shell commands it cannot prove safe.** `cat big.txt`,
  `awk '{print}' f`, `cat $FILE`, `cat *.md`, `cat f | grep x` — anything reading a file
  that can't be shown bounded gets `UNCLASSIFIABLE_READ`. `head -n 100 /abs/path`,
  `sed -n '10,60p' /abs/path`, `grep -m 20 pat /abs/path` and `wc -l /abs/path` pass.
  `npm test` and `git status` are none of its business.
- **Captures only what it withholds.** A blocked read is snapshotted into a private,
  content-addressed file with a SQLite row that owns its authorization; an ordinary small
  read is never stored. Storage failure keeps the original operation blocked and returns a
  fixed safe error with no handle — it never degrades into letting the raw payload through.
- **Answers questions instead.** Every reader request carries your question verbatim —
  one call per chunk, at most two at a time, at most one core retry.
- **Verifies every citation mechanically.** The verifier re-reads the immutable snapshot
  and checks the handle, the full SHA-256, the range and the exact quote. A claim whose
  citation fails is deleted from the answer; an answer with nothing left becomes
  `CITATION_INVALID`. A model saying `verified: true` proves nothing.
- **Says where the answer came from.** Every envelope is labelled `model_derived` or
  `deterministic_extraction`, and the provenance block keeps *requested*, *resolved* and
  *reported* provider/model apart. `actual_model` is never synthesized from
  `requested_model`: where a host cannot prove which model generated the tokens, the
  envelope says `unverified` rather than claiming otherwise.
- **Never leaks the payload it intercepted.** Not into the envelope, the transcript, a
  log, a metric label, a trace, an exception, or a retry. Provider error bodies are
  dropped at the boundary. `unit no-raw-leak` plants sentinels and injects failures at
  every stage to prove it.

## The three tools

All three are read-only. None of them can return a whole file.

| Tool | What it returns | Model calls |
| --- | --- | --- |
| `context_shunt_read` | a cited answer about a source — **generated text, not source bytes** | one per chunk |
| `context_shunt_inspect` | **exact snapshot bytes**: a line range, a byte range, or literal-search hits | zero |
| `context_shunt_stats` | this session's own token accounting | zero |

`context_shunt_read` takes exactly one source form: `paths` for a first look, or `handles`
to ask a sharper question about a snapshot you already hold. A refined question re-uses the
named immutable snapshot and never silently recaptures the source.

`context_shunt_inspect` is the escape hatch for "I need to see the actual text", and it is
deliberately not a retrieval tool. Each page is capped at 16 KiB, continuation uses an
opaque HMAC-tagged cursor bound to the handle, snapshot and selector, and — the part that
matters — **every page is charged against a cumulative per-source and per-session
disclosure ceiling before a byte is returned.** A per-result cap alone would just be
defeated by paging; the cumulative ceiling is what makes "this cannot be reassembled into
the whole file" true rather than aspirational.

## Where the payload lives

SQLite owns authorization and nothing else: opaque handle identity, session scope and
generation, TTL, quotas, content refcounts, disclosure totals and bounded operation
metrics. The immutable payload lives in a content-addressed private file whose location is
derived from its SHA-256 internally.

**No source path, question, answer, quote, payload preview or provider error body is ever
written to the database**, and no filesystem path is stored or exposed. The schema is
normative and lives in [`contracts/store/v1.sql`](contracts/store/v1.sql) — both language
cores execute that file verbatim rather than embedding their own `CREATE TABLE`, and a
cross-language test opens one store with both.

Readability is a SQL predicate, never file existence, so an expired, revoked,
closed-scope or stale-generation handle is unreadable the instant the predicate stops
holding — whether or not physical cleanup has run. A clock rollback cannot revive an
expired handle.

This is inspired by the Compress-Cache-Retrieve pattern popularized by
[Headroom](https://github.com/headroom-dev/headroom). No Headroom code is used or adapted.
One difference is deliberate and load-bearing: **Headroom offers full-original retrieval
back into the main model context, and this does not.** See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Install

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e 'packages/core-py[dev]'
npm install
./scripts/verify unit all
```

Needs Python ≥3.11 and **Node ≥22.22.3** (the store uses `node:sqlite`). Then follow
[`docs/install.md`](docs/install.md) for your host. Example configuration for both is in
[`examples/config/`](examples/config/).

## Hosts and modes

| Mode | Hermes | OpenClaw | Default |
| --- | --- | --- | --- |
| Local pre-read gate | supported | supported | on |
| Question-driven reader | supported, attribution `unverified` | supported, attribution `resolved` | on |
| Deterministic `inspect` (zero model calls) | supported | supported | on |
| Session `stats` | supported | supported | on |
| Handle lifecycle (finalize/reset, or reason enum) | supported | supported | on |
| Oversized post-tool spill/pointer | **unsupported** | **unsupported** | off |
| Writer / `propose_patch` | not implemented | not implemented | refused at load |

The optional post-tool mode needs the host to hand over the complete result *before*
truncation and accept a replacement *before* persistence. Neither host does: Hermes'
`transform_tool_result` receives post-truncation content inside a fail-open `try/except`,
and OpenClaw caps the result before it invokes the plugin's persist hook. So the mode is
reported `unsupported`, stays off, and fails closed if configuration asks for it. The
engine itself is implemented and tested behind that capability gate — the engine is not
the blocker; host enablement is.
[`docs/capability-matrix.md`](docs/capability-matrix.md) has the file-and-line evidence.

**Attribution has a real ceiling, and it is reported rather than papered over.** On Hermes,
`PluginLlm._resolve_attribution` records `response.model` when the provider returned one
and otherwise the plugin's own override — a caller cannot tell those apart, so the adapter
never claims `actual` and the envelope says `unverified`. On OpenClaw, the isolated
completion path returns the host's own post-policy selection, which is a genuine routing
fact but still not a provider confirmation, so the envelope says `resolved`. Capture,
inspect and stats do not depend on the reader and stay fully usable either way.

## Verifying

`./scripts/verify <suite> <gate>` exits `0` on pass, `1` on failure, `2` on `NOT_RUN`. A
`NOT_RUN` gate is one whose prerequisite is absent; it is never counted as a pass.

| Command | Needs |
| --- | --- |
| `./scripts/verify unit all` | both cores installed |
| `./scripts/verify packaging all` | nothing |
| `./scripts/verify benchmark core` | nothing |
| `./scripts/verify integration <host> --mode unsupported` | nothing |
| `./scripts/verify integration <host> --mode local` | a real host checkout |
| `./scripts/verify eval luna` | live `gpt-5.6-luna` |
| `./scripts/verify release all` | all of the above |

Deterministic gates never skip. Opt-in gates say what is missing.

`unit all` needs no host and no provider, but it does need both cores: the store gate
writes a store with one core and reads it back with the other, which is the only real
proof that the two agree on the normative DDL. Install both, per
[`install.md`](docs/install.md), before running it.

## Layout

| Path | What |
| --- | --- |
| [`contracts/v1/`](contracts/v1/) | JSON Schemas, shared caps, status/code table, tool-argument contract, and the fixture corpora both cores must satisfy |
| [`contracts/store/`](contracts/store/) | the normative SQLite DDL both cores execute verbatim |
| [`packages/core-py/`](packages/core-py/) | Python core (used by Hermes) |
| [`packages/core-ts/`](packages/core-ts/) | TypeScript core (used by OpenClaw) |
| [`adapters/hermes/`](adapters/hermes/) | Hermes plugin: `plugin.yaml` + `__init__.py` |
| [`adapters/openclaw/`](adapters/openclaw/) | OpenClaw plugin: `openclaw.plugin.json` + `index.ts` |
| [`evals/`](evals/) | the fixed 40-item reader eval corpus and its scoring rules |
| [`scripts/verify`](scripts/verify) | the acceptance entry point |

Two cores, one contract. Neither core is derived from the other: both read
`contracts/v1/conformance/*.json` and both execute `contracts/store/v1.sql`, so the gate
decisions, line counting, citation rules, store semantics and spill behaviour are pinned by
the same files on both sides.

## Design notes

Source content is untrusted data, never instructions. The gate, the snapshot, the store,
the inspector and the citation verifier are deterministic code; the model decides nothing
about permissions and cannot reach a file. The reader gets no shell, no network, no write
tools and no host conversation — only a fixed instruction, your question, and one
authorized excerpt.

Anything that cannot be proven is refused rather than assumed: an unfinished size probe
blocks, an unrecognised read-like command blocks, an unprovable host guarantee disables the
mode that needed it, and an unprovable model attribution is labelled as such instead of
being asserted. Every cap lives in
[`contracts/v1/limits.json`](contracts/v1/limits.json) and a deployment may only narrow
them — a config file cannot widen the boundary the gates measure.

Token accounting is signed and credited once. `main_context_tokens_saved` and
`net_tokens_saved` can both be negative, and for a refined question or an inspect page they
should be: those cost context without withholding anything new. A token count the provider
did not report is stored as null and rendered as null — **zero is never used to mean
unknown** — and any substitute is labelled `bytes_div_4` rather than passed off as exact.

## Docs

1. [Architecture and data contract](docs/architecture.md) — the normative spec
2. [Capability matrix](docs/capability-matrix.md) — what each host supports, with evidence
3. [Install, uninstall, cleanup and migration](docs/install.md)
4. [Security and retention](docs/security.md) — what is stored, where, and for how long
5. [Acceptance gates](docs/acceptance.md)
6. [Limitations](docs/limitations.md) — what this does not do, and what is unproven
7. [Implementation plan](docs/implementation-plan.md)
8. [Third-party notices](THIRD_PARTY_NOTICES.md)

## Licence

[Apache License 2.0](LICENSE). The design baseline is Spotify's Apache-2.0
`spotify/portal-ai-plugins` `plugins/shunt` at commit
`3c24ca30ff63e1f5bbad1c43fe5324daff579123`; no code was copied or adapted, and upstream's
own test results stay attributed to upstream. The cache-and-retrieve shape is inspired by
Headroom's Compress-Cache-Retrieve pattern, again with no code copied. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
