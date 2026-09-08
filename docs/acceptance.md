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

`./scripts/verify release all` runs unit, both hosts and all modes, eval, benchmark,
packaging, license, and dependency checks. It returns `NOT_RUN` while a required live
prerequisite is absent. A safe unsupported optional post-tool mode is not a failure, but it
must remain visibly unsupported and its live post-tool gate must not be presented as pass.
