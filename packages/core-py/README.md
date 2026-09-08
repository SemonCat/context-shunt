# context-shunt-core (Python)

Deterministic read-only core used by the Hermes adapter. Semantics are defined by
`contracts/v1` at the repository root; this package vendors a byte-identical copy under
`src/context_shunt/contracts/` so an installed plugin is self-contained.
Run `scripts/sync-contracts` after changing the root contracts.

Requires Python 3.11 or newer. From the repository root, install development dependencies
with `python3 -m venv .venv` and
`./.venv/bin/pip install -e 'packages/core-py[dev]'`, then run
`./scripts/verify unit all`. Public behavior, configuration, and accounting are documented
in [`README.md`](../../README.md), [`docs/configuration.md`](../../docs/configuration.md),
and [`docs/metrics.md`](../../docs/metrics.md).
