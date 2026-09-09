# OpenClaw citation fallback fix — 1.2.1

Verified 2026-09-10, reusing the current clean `main` checkout at `3279b78` (the merged
middleware implementation). The TypeScript core and OpenClaw packages are now 1.2.1;
contract schema and Python/Hermes implementation are unchanged.

## Confirmed cause and precedence

The live symptom was a valid-handle `CITATION_INVALID` failure instead of labelled legacy
compaction. Eight deterministic adapter regressions failed before the fix: invalid quotes,
empty citations, empty answer/citations, and empty claims/citations, each for a fresh path
and an existing handle.

The session's legacy guard required `availabilityFailure`, which citation verification
failures intentionally do not set. Empty parsed model replies additionally reached
`NO_MATCH` without evidence. The regressions inspect the original ReaderResult to separate
these causes from invalid handles or a failed artifact store.

The reader still performs the same mechanical verification. Parsed model replies without
verifiable citations now return `CITATION_INVALID`; deterministic no-hit searches without
a model call still return `NO_MATCH`. An enabled legacy session now handles terminal
`CITATION_INVALID` as well as exhausted-availability `MODEL_ERROR`/`TIMEOUT`. Citation
failure never masquerades as provider unavailability or advances the provider chain.

Legacy fallback revalidates all handles, compacts the first source, retains the original
reader failure code and all source handles, marks all remaining coverage omitted, and
publishes partial/not-model-derived provenance with the original paid-attempt accounting.
The complete envelope still fits the existing 16 KiB ceiling. A failed or unsafe compactor
retains the bounded reader failure without raw output or exact-prefix downgrade.
Cancellation, model identity/policy refusals, and unrelated format/source/budget failures
are not new fallback triggers. See [precise precedence](configuration.md#openclaw-reader-fallback-precedence-121).

## Checks

| Check | Result |
| --- | --- |
| Adapter citation regression before fix | 8 failed (red) |
| Adapter citation regression after fix | 8 passed |
| Focused TS reader, legacy algorithm and session fallback | 190 passed |
| Full `npm test` | 628 core + 93 adapter passed; 9 host cases skipped here and separately executed below |
| `./scripts/verify unit all` | 17 gates, 1,925 case executions, zero failures; gate selections overlap |
| `./scripts/verify packaging all` | 23 checks passed |
| Official OpenClaw integration `--mode post-tool` with the installed checkout | 9 passed |
| `npm run typecheck` | Both workspaces passed |
| `npm run lint --workspaces --if-present` | Passed (configured TypeScript lint is typecheck) |
| Python/Hermes Ruff lint and formatting | Passed; 58 files already formatted |
| `git diff --check` | Passed; no TypeScript formatter is configured |
| `autoreview --mode local --no-web-search` | Sol/high; TruffleHog clean; no actionable P0 findings |

The tests check rejection before fallback, retained source/snapshot identities, original
`CITATION_INVALID`, partial coverage, non-derived provenance, empty model answer/citations,
exact paid attempt/token accounting recorded once, envelope byte bounds, and no unverified
model sentinel or raw-source fail-open. Injected compactor failure preserves the original
citation error. Generic sessions without legacy enabled still return the bounded citation
failure; existing deterministic no-hit and citation-cap behavior remain tested.

Live provider/gateway verification after deployment is **NOT_RUN**. The installed-host
integration is a deterministic official loader/runner harness, not a live restart test.
No OpenClaw config, host source, restart, deployment, push, or merge was performed. Legacy
compaction remains question-independent and first-source-only; it cannot promise a cited
model answer, and unsafe/unavailable snapshots or an impossible output budget still produce
a bounded failure.

## Changed files

- [`README.md`](../README.md)
- [`README.zh-TW.md`](../README.zh-TW.md)
- [`adapters/openclaw/README.md`](../adapters/openclaw/README.md)
- [`adapters/openclaw/index.ts`](../adapters/openclaw/index.ts)
- [`adapters/openclaw/package.json`](../adapters/openclaw/package.json)
- [`adapters/openclaw/src/capability.ts`](../adapters/openclaw/src/capability.ts)
- [`adapters/openclaw/test/adapter.test.ts`](../adapters/openclaw/test/adapter.test.ts)
- [`docs/architecture.md`](../docs/architecture.md)
- [`docs/capability-matrix.md`](../docs/capability-matrix.md)
- [`docs/citation-fallback-verification.md`](../docs/citation-fallback-verification.md)
- [`docs/configuration.md`](../docs/configuration.md)
- [`docs/limitations.md`](../docs/limitations.md)
- [`package-lock.json`](../package-lock.json)
- [`package.json`](../package.json)
- [`packages/core-ts/package.json`](../packages/core-ts/package.json)
- [`packages/core-ts/src/reader.ts`](../packages/core-ts/src/reader.ts)
- [`packages/core-ts/src/session.ts`](../packages/core-ts/src/session.ts)
- [`packages/core-ts/test/automatic-extract.test.ts`](../packages/core-ts/test/automatic-extract.test.ts)
- [`packages/core-ts/test/reader-and-spill.test.ts`](../packages/core-ts/test/reader-and-spill.test.ts)
