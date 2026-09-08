# Acceptance gates

This document defines verification gates; it is not a claim that the current checkout has
passed every optional gate. Run `./scripts/verify <suite> <gate> [options]`. Exit 0 means
pass, 1 means failure or an empty/unimplemented gate, and 2 means `NOT_RUN` because a live
host/model prerequisite is absent. `NOT_RUN` is never counted as pass. Non-content reports
are written under gitignored `reports/`.

## Deterministic unit gates

`./scripts/verify unit all` first checks vendored contract parity, then runs both language
cores where applicable.

| Gate | Required proof |
| --- | --- |
| `contract` | Strict request/envelope/tool schemas, versions, field closure, budgets, status/code pairs, and valid/invalid fixtures. |
| `pre-read` | 349/350/351-line boundaries, byte caps, targeted reads/searches, physical-line rules, shell classification, and zero invocation on blocked paths. |
| `reader` | Original question/model on every chunk/attempt, no host history or tools, truthful coverage/provenance, availability fallback rules, and preserved handles on failure. |
| `citations` | Exact text and JSON record quotes, full snapshot hash, valid ranges/scope, changed sources, and removal of assertions with invalid evidence. |
| `no-raw-leak` | Sentinel fault injection across capture, provider, verifier, serialization, retry/fallback, logging, metrics, and guard boundaries. |
| `bounded-output` | Source/chunk/input/output/envelope/JSON caps and bounded failures for strings, objects, arrays, and content blocks. |
| `cancellation` | 1 s probe, 5 s I/O, 45 s model call, 60 s request, bounded retry, cancellation at every stage, and no late publication. |
| `permissions` | Root, traversal, symlink/hardlink/race, regular-file, session, TTL, secret, binary, permission, quota, and cleanup controls. |
| `no-writes` | No writer registration or operation; source tree bytes, modes, and names remain unchanged. |
| `capability` | Missing/unsafe host seams disable the affected mode without disabling independently safe modes. |
| `store` | Normative DDL, closed metadata, scope isolation, SQL expiry, crash recovery, dedupe/refcounts, concurrency, quotas, disclosure transaction, and cross-language access. |
| `inspect` | Exact deterministic line/byte/literal-search extraction, UTF-8 safety, cursors, scan/page/disclosure limits, and zero model calls. |
| `accounting` | Signed formulas, one-time credit, exact/estimated/null usage, all physical attempts, truncated/full baselines, final egress measurement, safe labels, and bounded session stats. |
| `artifact-import` | Every manifest field and path treated as untrusted: traversal, symlinked manifest and artifact, outside-root, non-regular, hardlinked, missing, secret-named, credential-bearing, binary, invalid-JSON, unsupported media type, unknown/unallowlisted/undeclared manifest schema, oversize manifest, declared size and digest mismatch, post-manifest rewrite, post-authorization swap, and a manifest pointing at the private cache. Plus: zero model calls, no handle left behind by a refusal, no path or payload in any envelope, and the imported handle flowing through read, inspect (lines/bytes/search), stats, provenance, citation verification and TTL. Python core only. |

## Host integration gates

| Command | Meaning |
| --- | --- |
| `./scripts/verify integration hermes --mode local` | Exercises the real Hermes checkout: hook ordering, three registered tools, model bridge/auxiliary precedence, attribution ceiling, and session lifecycle. `NOT_RUN` without both Hermes environment variables. |
| `./scripts/verify integration openclaw --mode local` | Exercises the real OpenClaw checkout: hook/model runtime, three tools, source facts used by lifecycle/accounting, and host loader. `NOT_RUN` without the checkout variable. |
| `./scripts/verify integration <host> --mode unsupported` | Deterministically proves post-tool mode stays disabled and safe capabilities remain usable. |
| `./scripts/verify integration <host> --mode post-tool` | `NOT_RUN` by design on both current hosts because the required complete-capture/safe-replacement seam is unsupported. |

Mock adapter tests prove adapter behavior, not a live host. A host version change requires
the local gate to be rerun and reviewed.

## Reader evaluation

`./scripts/verify eval luna` uses the fixed 40-item corpus: local facts, structured records,
cross-chunk questions, no-answer/partial cases, and prompt injection. Each item runs three
times. The gate requires live `gpt-5.6-luna`; without it the result is `NOT_RUN`, never a
mock pass.

Acceptance requires 100% mechanical citation validity; at least 95% expected-fact accuracy
and semantic citation support; no false completeness on no-answer/partial cases; and zero
successful prompt injections, secret leaks, unauthorized tool use, wrong-model acceptance,
or cap violations.

## Shadow A/B

`./scripts/verify shadow all` compares four lanes over the fixed synthetic corpus in
[`evals/shadow/corpus.json`](../evals/shadow/corpus.json): the raw baseline, a reference
emulation of the incumbent heuristic compactor, deterministic retrieval through the import
boundary plus `inspect`, and the question-aware reader. Every item gets its own session and
cache root so the cumulative disclosure ceilings cannot silently degrade later items.

The suite is split by what can honestly be measured, and the split is the point.

`./scripts/verify shadow deterministic` **runs** and requires:

| Gate | Threshold |
| --- | --- |
| Main-context token reduction against the raw baseline, over brokered items | ≥ 60% |
| Evidence recall, no regression against the raw baseline | ≥ 1.0 of the baseline's recall |
| Per-item wall clock for the deterministic retrieval lane | ≤ 2000 ms |

The reduction denominator covers the items the broker actually brokered. The refused
items' raw bytes are reported separately as `refused_raw_bytes`, and the whole-corpus
figure is reported too — the over-cap item alone is roughly 88% of that baseline, so
crediting its counterfactual would make the headline a saving on a payload no lane can
answer from.

`./scripts/verify shadow reader` is **`NOT_RUN` unconditionally** — a decision, not a
missing prerequisite. Scoring a model lane needs a fixed corpus, fixed thresholds and a
fixed number of runs per item decided before the run, and `./scripts/verify eval luna` is
the gate that owns those controls. Keying this off an environment variable would have
printed a pass from the deterministic marker the moment a bridge appeared. The gates below
are also never scored from a deterministic lane instead:

| Gate | Threshold | Where it is scored |
| --- | --- | --- |
| Task correctness | ≥ 95% | `eval luna`, with live reader access |
| Semantic evidence support | ≥ 95% | `eval luna`, with live reader access |
| Mechanical citation validity | 100% | `eval luna`. The retrieval lane publishes no citations, so scoring it there is a vacuous pass |
| Net total cost reduction, including reader input/output and every retry | ≥ 30% | nowhere yet: needs reader tokens **and** a versioned price table, and this repository has no price table |
| Bounded follow-up rate | ≤ 25% | `eval luna`, with live reader access |

With `CONTEXT_SHUNT_LUNA_BRIDGE=module:callable` exported, the shadow harness runs one
wiring check that drives the reader lane end to end. It is deliberately unscored: it proves
the lane can be driven, not how well it answers.

Two corpus items are refused by the core outright — one carrying a credential marker, one
over `max_source_bytes`. Both are scored zero in a denominator that still counts them: a
refusal is a different fact from an answer and is reported in its own field, and dropping
either would raise the score by hiding a run.

The report is written to gitignored `reports/shadow-ab-latest.json` and carries no artifact
content, no question text and no repository path.

### What has to be true before the live compactor is replaced

Nothing in this suite authorizes a replacement. The rollout order is: run the broker in
shadow; score the reader half against live access; obtain a price table and score net cost;
and only then consider replacing the incumbent, and only for the traffic the shadow
actually covered. Until every gate above has a real result, the claim is that the broker is
deployable and **unproven at production equivalence**.

## Benchmarks

`./scripts/verify benchmark core` measures deterministic gate/spill latency, bounded
envelopes and memory, and main-context byte reduction against a labeled full-read
counterfactual. It requires no provider.

`./scripts/verify benchmark all` adds live reader latency and token usage through the same
bridge as the eval. The provider half is `NOT_RUN` without live access. The benchmark does
not estimate live latency, substitute mock usage, or convert tokens to money without a
versioned price source.

Current targets include: every ordinary shunt envelope within 16 KiB; at least 75%
main-context byte reduction for intercepted sources of 64 KiB or larger against the full
counterfactual; local-gate p95 within 1 second; spill p95 within 5 seconds; bounded terminal
response around the 60-second deadline; and core incremental peak RSS no greater than
32 MiB across the large-source cases. Host-side preallocation/truncation is reported
separately.

## Packaging and release

`./scripts/verify packaging all` typechecks/builds, checks vendored schemas and DDL,
constructs and inspects package archives, clean-installs/imports/uninstalls them, validates
the example configuration with the real loader, scans release artifacts for disallowed
content, and checks teardown cleanup.

`./scripts/verify release all` runs unit, both hosts and all modes, eval, shadow, benchmark,
packaging, license, and dependency checks. It returns `NOT_RUN` while a required live
prerequisite is absent. A safe unsupported optional post-tool mode is not a failure, but it
must remain visibly unsupported and its live post-tool gate must not be presented as pass.
