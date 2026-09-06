# context-shunt-core (Python)

Deterministic read-only core used by the Hermes adapter. Semantics are defined by
`contracts/v1` at the repository root; this package vendors a byte-identical copy under
`src/context_shunt/contracts/` so an installed plugin is self-contained.
Run `scripts/sync-contracts` after changing the root contracts.
