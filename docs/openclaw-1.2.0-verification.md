# OpenClaw 1.2.0 verification

Verified on 2026-09-10 (Asia/Taipei), on `feat/openclaw-tool-result-middleware` from clean
`main` at `7883a04`. TypeScript core and OpenClaw package versions are 1.2.0; contract
revision remains 1.1. Python/Hermes source, tests, and package version are unchanged.

The original seam evidence references OpenClaw 2026.9.3 / `773b6d8857d`. The actual installed
loader/runner tests used 2026.9.3 / `56cd6a71e40`. No host source/configuration, live plugin,
provider, or Hermes deployment was changed. No push or deployment was performed.

## Final checks

| Check | Result |
| --- | --- |
| Focused TypeScript legacy algorithm and availability fallback | 59 passed |
| Focused OpenClaw capture | 40 passed |
| `npm test` | 624 core + 85 adapter tests passed; 9 host tests skipped in this non-host invocation, separately executed below |
| `./scripts/verify unit all` | 17 gates, 1,909 case executions, zero failures; gate selections deliberately overlap |
| `./scripts/verify packaging all` | 23 checks passed, including build, manifest middleware entitlement, package/core version agreement, temporary clean installs, example config, artifact scan and teardown |
| `integration openclaw --mode post-tool` with `CONTEXT_SHUNT_OPENCLAW_ROOT` | 9 real installed loader/runner tests passed; both middleware runtimes exercised |
| `npm run typecheck` | Both TypeScript workspaces passed |
| `npm run lint --workspaces --if-present` | Passed (repository TypeScript lint command is `tsc --noEmit`) |
| Ruff lint: Python core, Hermes adapter, verification script | Passed |
| Ruff format: Python core and Hermes adapter | 58 files already formatted |
| TypeScript formatting | No formatter is configured in this repository; `git diff --check` passed |
| Autoreview | `autoreview --mode local --no-web-search`; Codex Sol/high; TruffleHog clean, no actionable findings at configured P0 threshold |
| `./scripts/verify benchmark all` deterministic half | 12 passed |

The first integrated full run exposed two adapter assertions for the superseded exact-prefix
fallback. They now assert the requested labelled legacy behavior; the final full unit and
TypeScript runs above passed. Generic TypeScript automatic extraction retains its default
behavior unless the new session option is selected. OpenClaw selects legacy compaction.

## Coverage and remaining gates

Supported when explicitly enabled and registered: eligible read-only text/JSON from embedded
OpenClaw tools and OpenClaw-owned dynamic tools in the Codex harness. Default IDs are `read`,
`web_fetch`, and `web_search`; additional verified read-only MCP IDs require
`tool_result_capture.read_only_tools`. Unknown/mutating tools and messaging/session/control
results are excluded. Capture uses the shared immutable store/session scope, returns a
bounded pointer without a model call, and reads through the existing question-aware Luna
route. Exhausted availability yields `LEGACY_COMPACTED` / `legacy_compaction`; unsafe
compaction keeps a bounded failure, never a raw or exact-prefix fallback.

Codex-native PostToolUse replacement is unsupported. OpenClaw sanitizes before middleware:
200 blocks, 100,000 text aggregation characters, 100,000 details bytes, and 5,000,000 image
characters. Detectable ambiguous boundaries are refused without a handle. Below them,
snapshots are complete only for the middleware-visible representation; original upstream
completeness stays unknown. Complete-original capture above those ceilings needs an upstream
host seam/change or a producer-side design. No host patch is needed for the supported scope.

`eval luna` and the provider half of `benchmark all` remain **NOT_RUN**: no qualifying live
bridge was enabled. Live gateway/end-to-end model delivery, production Codex dynamic-tool
traffic, live Luna answers/fallback, and the Tokenjuice cutover are also **NOT_RUN**.
The installed-host test is a deterministic loader/runner harness, not a live deployment.
Tokenjuice and all competing reducers must be disabled in the same quiesced configuration
transaction that enables capture; this API has no middleware priority option. See
[cutover instructions](acceptance.md#openclaw-middleware-cutover).

## Changed files

- [`README.md`](../README.md)
- [`README.zh-TW.md`](../README.zh-TW.md)
- [`adapters/openclaw/README.md`](../adapters/openclaw/README.md)
- [`adapters/openclaw/index.ts`](../adapters/openclaw/index.ts)
- [`adapters/openclaw/openclaw.plugin.json`](../adapters/openclaw/openclaw.plugin.json)
- [`adapters/openclaw/package.json`](../adapters/openclaw/package.json)
- [`adapters/openclaw/src/capability.ts`](../adapters/openclaw/src/capability.ts)
- [`adapters/openclaw/src/capture.ts`](../adapters/openclaw/src/capture.ts)
- [`adapters/openclaw/test/adapter.test.ts`](../adapters/openclaw/test/adapter.test.ts)
- [`adapters/openclaw/test/capture.test.ts`](../adapters/openclaw/test/capture.test.ts)
- [`adapters/openclaw/test/config.test.ts`](../adapters/openclaw/test/config.test.ts)
- [`adapters/openclaw/test/host-integration.test.ts`](../adapters/openclaw/test/host-integration.test.ts)
- [`docs/acceptance.md`](../docs/acceptance.md)
- [`docs/architecture.md`](../docs/architecture.md)
- [`docs/capability-matrix.md`](../docs/capability-matrix.md)
- [`docs/configuration.md`](../docs/configuration.md)
- [`docs/implementation-plan.md`](../docs/implementation-plan.md)
- [`docs/install.md`](../docs/install.md)
- [`docs/limitations.md`](../docs/limitations.md)
- [`docs/openclaw-1.2.0-verification.md`](../docs/openclaw-1.2.0-verification.md)
- [`examples/config/README.md`](../examples/config/README.md)
- [`examples/config/openclaw.json`](../examples/config/openclaw.json)
- [`package-lock.json`](../package-lock.json)
- [`package.json`](../package.json)
- [`packages/core-ts/package.json`](../packages/core-ts/package.json)
- [`packages/core-ts/src/capability.ts`](../packages/core-ts/src/capability.ts)
- [`packages/core-ts/src/envelope.ts`](../packages/core-ts/src/envelope.ts)
- [`packages/core-ts/src/guard.ts`](../packages/core-ts/src/guard.ts)
- [`packages/core-ts/src/index.ts`](../packages/core-ts/src/index.ts)
- [`packages/core-ts/src/legacy-compact.ts`](../packages/core-ts/src/legacy-compact.ts)
- [`packages/core-ts/src/provenance.ts`](../packages/core-ts/src/provenance.ts)
- [`packages/core-ts/src/session.ts`](../packages/core-ts/src/session.ts)
- [`packages/core-ts/src/spill.ts`](../packages/core-ts/src/spill.ts)
- [`packages/core-ts/test/automatic-extract.test.ts`](../packages/core-ts/test/automatic-extract.test.ts)
- [`packages/core-ts/test/legacy-compact.test.ts`](../packages/core-ts/test/legacy-compact.test.ts)
- [`scripts/verify`](../scripts/verify)
