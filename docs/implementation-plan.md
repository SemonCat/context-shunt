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
- Hermes and OpenClaw adapters registering three read-only tools and no writer.
- Deterministic unit, unsupported-mode, core benchmark, packaging, live-host, live-model,
  and aggregate release gate entry points.

Implemented means code and a gate exist. It does not mean every live prerequisite was
available on the current checkout; consult the most recent verification output.

## Intentionally unavailable

- `suma_post_tool` is unsupported on Hermes and OpenClaw because neither exposes the
  required complete-capture and safe-replacement seam. The core engine exists behind the
  capability gate, but the host feature is off and its live post-tool tests are `NOT_RUN`.
- Writer / `propose_patch` is not implemented and is refused at configuration/contract
  boundaries.
- No supported adapter can prove provider-authoritative `actual` model identity. Hermes
  reports `unverified`; OpenClaw reports the host routing fact `resolved`.
- No cross-machine/network-filesystem store, arbitrary cached-original retrieval, binary
  extraction, OCR, archive expansion, or semantic proof of citation support.

## Remaining release evidence

A production release signal still requires successful runs of both real host local
integration gates, the live 40-item Luna evaluation, and the provider benchmark. These
remain `NOT_RUN` whenever their checkout/model prerequisites are absent and must never be
described as passing on the strength of unit tests.

Future host support for post-tool spill needs a new capability proof and runtime sentinel
measurement before activation. A future writer requires a new contract revision, explicit
write scopes, conflict handling, and dedicated acceptance gates.
