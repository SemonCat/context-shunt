# @context-shunt/core (TypeScript)

Deterministic read-only core used by the OpenClaw adapter. Semantics are defined by
`contracts/v1` at the repository root; this package vendors a byte-identical copy under
`contracts/` so an installed plugin is self-contained, and its tests read those fixtures
directly. Run `scripts/sync-contracts` after changing the root contracts.

Requires Node 22.22.3 or newer. From the repository root run `npm install`,
`npm run build --workspace @context-shunt/core`, and `./scripts/verify unit all`. Public
behavior, configuration, and accounting are documented in
[`README.md`](../../README.md), [`docs/configuration.md`](../../docs/configuration.md), and
[`docs/metrics.md`](../../docs/metrics.md).
