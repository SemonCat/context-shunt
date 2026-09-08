# Development status

This file replaces the historical unchecked implementation checklist. Current behavior is
defined by [`architecture.md`](architecture.md), executable acceptance by
[`acceptance.md`](acceptance.md), and host support by
[`capability-matrix.md`](capability-matrix.md). An unchecked old plan is not evidence about
today's code.

## Implemented in the repository

- Strict revision 1.0/1.1 contracts, shared fixtures, status pairs, and limit source.
- Independent Python and TypeScript cores with contract parity checks.
- Pre-execution local read gates and bounded shell-read classification.
- Immutable snapshots, content-addressed blobs, normative SQLite authorization metadata,
  TTL/sweep/recovery, scope isolation, and cumulative disclosure accounting.
- Query-aware reader orchestration, bounded concurrency/retry/deadlines, availability
  fallback, mechanical citation verification, partial coverage, and output guards.
- Deterministic line/byte/literal-search inspection and read-only session stats.
- An external-artifact import boundary (Python core, Hermes adapter): a versioned
  producer-agnostic manifest contract, producer translation profiles, allowlisted import
  roots, and re-proof of every manifest claim against the bytes actually read.
- A deterministic shadow A/B harness over a fixed synthetic corpus, with the
  provider-dependent gates reported `NOT_RUN`.
- Hermes and OpenClaw adapters registering read-only tools and no writer: three on both,
  plus `context_shunt_import` on Hermes where a deployment configured it.
- Deterministic unit, unsupported-mode, core benchmark, packaging, live-host, live-model,
  and aggregate release gate entry points.

Implemented means code and a gate exist. It does not mean every live prerequisite was
available on the current checkout; consult the most recent verification output.

## Intentionally unavailable

- `suma_post_tool` is unsupported on Hermes and OpenClaw because neither exposes the
  required complete-capture and safe-replacement seam. The core engine exists behind the
  capability gate, but the host feature is off and its live post-tool tests are `NOT_RUN`.
- `artifact_import` is unsupported on OpenClaw: the TypeScript core has no import
  boundary. That is a repository gap rather than a host limitation, so closing it needs no
  host change.
- The shadow A/B's task-correctness, semantic-support, mechanical-citation-validity,
  follow-up-rate and net-cost gates are `NOT_RUN`. The first four need live reader access;
  net cost additionally needs a versioned price table that does not exist here. No live
  compactor is replaced on deterministic evidence alone.
- Writer / `propose_patch` is not implemented and is refused at configuration/contract
  boundaries.
- No supported adapter can prove provider-authoritative `actual` model identity. Hermes
  reports `unverified`; OpenClaw reports the host routing fact `resolved`.
- No cross-machine/network-filesystem store, arbitrary cached-original retrieval, binary
  extraction, OCR, archive expansion, or semantic proof of citation support.

## Remaining release evidence

The recorded real-host integration run is no longer outstanding: Hermes and OpenClaw local
plus unsupported-mode gates executed 122 cases with 0 failures. The two post-tool gates
truthfully remained `NOT_RUN`, because both hosts lack the required seam; they are not
counted as passes.

A production release signal still requires the production-equivalent 40-item Luna
evaluation and provider benchmark. Both remain `NOT_RUN` without live model access and
must never be described as passing on the strength of deterministic or host integration
tests.

Future host support for post-tool spill needs a new capability proof and runtime sentinel
measurement before activation. A future writer requires a new contract revision, explicit
write scopes, conflict handling, and dedicated acceptance gates.
