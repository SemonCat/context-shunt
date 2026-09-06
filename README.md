# context-shunt

Keep large sources out of your agent's context. When a read would dump 20,000 lines into
the conversation, context-shunt stops it *before the tool runs* and offers a different
deal: ask a question about the file instead, and get back a short answer whose every
citation has been checked against the bytes it claims to quote.

v1 is read-only. It never modifies a source.

```text
        read a 20k-line file                    ask a question about it
                 │                                        │
                 ▼                                        ▼
        pre-read gate (before execution)          question-driven reader
                 │                                        │
       blocked ──┴── allowed if bounded            gpt-5.6-luna, per chunk
                 │                                        │
                 ▼                                        ▼
        bounded envelope + guidance          citation verifier → output guard
                 └───────────────┬────────────────────────┘
                                 ▼
                       ≤16 KiB back to the agent
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
- **Answers questions instead.** Every reader request issued by either core carries your
  question verbatim and requests `gpt-5.6-luna` — one call per chunk, at most two at a
  time, at most one core retry. Host-routing proof is tracked separately in the
  capability matrix.
- **Verifies every citation mechanically.** The verifier re-reads the immutable snapshot
  and checks the handle, the full SHA-256, the range and the exact quote. A claim whose
  citation fails is deleted from the answer; an answer with nothing left becomes
  `CITATION_INVALID`. A model saying `verified: true` proves nothing.
- **Never leaks the payload it intercepted.** Not into the envelope, the transcript, a
  log, a metric label, a trace, an exception, or a retry. Provider error bodies are
  dropped at the boundary. `unit no-raw-leak` plants sentinels and injects failures at
  every stage to prove it.

## Install

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e 'packages/core-py[dev]'
npm install
./scripts/verify unit all
```

Then follow [`docs/install.md`](docs/install.md) for your host. Example configuration for
both is in [`examples/config/`](examples/config/).

## Hosts and modes

| Mode | Hermes | OpenClaw | Default |
| --- | --- | --- | --- |
| Local pre-read gate | supported | supported | on |
| Question-driven reader (`gpt-5.6-luna` only) | **release proof pending** | supported | on |
| Suma oversized-result spill/pointer | **unsupported** | **unsupported** | off |
| Writer / `propose_patch` | not in v1 | not in v1 | refused at load |

The optional Suma mode needs the host to hand over the complete result *before*
truncation and accept a replacement *before* persistence. Neither host does: Hermes'
`transform_tool_result` receives post-truncation content inside a fail-open `try/except`,
and OpenClaw caps the result before it invokes the plugin's persist hook. So the mode is
reported `unsupported`, stays off, and fails closed if configuration asks for it.

The spill engine itself is implemented and passing — oversized strings, objects, arrays
and content blocks all spill to a private pointer with zero model calls and no
summarization. The engine is not the blocker; host enablement is.
[`docs/capability-matrix.md`](docs/capability-matrix.md) has the file-and-line evidence.

The Hermes 0.18.2 adapter demonstrably requests Luna through `PluginLlm`, but that host
facade owns auxiliary retries and provider fallback internally. The current integration
does not prove every upstream attempt remains on Luna, so the fixed Luna-only release
requirement remains blocked pending host-routing provenance.

## Verifying

`./scripts/verify <suite> <gate>` exits `0` on pass, `1` on failure, `2` on `NOT_RUN`. A
`NOT_RUN` gate is one whose prerequisite is absent; it is never counted as a pass.

| Command | Needs |
| --- | --- |
| `./scripts/verify unit all` | nothing |
| `./scripts/verify packaging all` | nothing |
| `./scripts/verify benchmark core` | nothing |
| `./scripts/verify integration <host> --mode unsupported` | nothing |
| `./scripts/verify integration <host> --mode local` | a real host checkout |
| `./scripts/verify eval luna` | live `gpt-5.6-luna` |
| `./scripts/verify release all` | all of the above |

Deterministic gates never skip. Opt-in gates say what is missing.

## Layout

| Path | What |
| --- | --- |
| [`contracts/v1/`](contracts/v1/) | JSON Schemas, shared caps, status/code table, and the fixture corpora both cores must satisfy |
| [`packages/core-py/`](packages/core-py/) | Python core (used by Hermes) |
| [`packages/core-ts/`](packages/core-ts/) | TypeScript core (used by OpenClaw) |
| [`adapters/hermes/`](adapters/hermes/) | Hermes plugin: `plugin.yaml` + `__init__.py` |
| [`adapters/openclaw/`](adapters/openclaw/) | OpenClaw plugin: `openclaw.plugin.json` + `index.ts` |
| [`evals/`](evals/) | the fixed 40-item reader eval corpus and its scoring rules |
| [`scripts/verify`](scripts/verify) | the acceptance entry point |

Two cores, one contract. Neither core is derived from the other: both read
`contracts/v1/conformance/*.json`, so the gate decisions, line counting, citation rules
and spill behaviour are pinned by the same cases on both sides.

## Design notes

Source content is untrusted data, never instructions. The gate, the snapshot, the spill
and the citation verifier are deterministic code; the model decides nothing about
permissions and cannot reach a file. The reader gets no shell, no network, no write tools
and no host conversation — only a fixed instruction, your question, and one authorized
excerpt.

Anything that cannot be proven is refused rather than assumed: an unfinished size probe
blocks, an unrecognised read-like command blocks, an unprovable host guarantee disables
the mode that needed it. Every cap lives in
[`contracts/v1/limits.json`](contracts/v1/limits.json) and a deployment may only narrow
them — a config file cannot widen the boundary the gates measure.

## Docs

1. [Architecture and data contract](docs/architecture.md) — the normative v1 spec
2. [Capability matrix](docs/capability-matrix.md) — what each host supports, with evidence
3. [Install and uninstall](docs/install.md)
4. [Acceptance gates](docs/acceptance.md)
5. [Implementation plan](docs/implementation-plan.md)
6. [Third-party notices](THIRD_PARTY_NOTICES.md)

## Licence

[Apache License 2.0](LICENSE). The design baseline is Spotify's Apache-2.0
`spotify/portal-ai-plugins` `plugins/shunt` at commit
`3c24ca30ff63e1f5bbad1c43fe5324daff579123`; no code was copied or adapted, and upstream's
own test results stay attributed to upstream. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
