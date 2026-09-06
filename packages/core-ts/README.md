# @context-shunt/core (TypeScript)

Deterministic read-only core used by the OpenClaw adapter. Semantics are defined by
`contracts/v1` at the repository root; this package vendors a byte-identical copy under
`contracts/` so an installed plugin is self-contained, and its tests read those fixtures
directly. Run `scripts/sync-contracts` after changing the root contracts.
