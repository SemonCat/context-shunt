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

Shunt-owned read, capture/store, and eligible inspection failures automatically return bounded deterministic `partial/LEGACY_COMPACTED` output. This invariant cannot be disabled: former legacy-compaction switches are deprecated no-ops. The output preserves the original failure code and bounded `failure_detail`, marks incomplete coverage, and never claims model derivation or verified citations. Safe representative excerpts are expected; full raw passthrough is forbidden.

Caller errors, unsupported operations/versions, unsafe/binary/secret content, immutable binding or session mismatch, expired/changed sources, provenance-policy refusal, attribution mismatch, cancellation, and disclosure exhaustion remain explicit refusals. `LIMIT_EXCEEDED` qualifies only for enumerated Shunt implementation/store capacity details, never safety or disclosure caps. Citation failures receive at most one pinned, deadline- and budget-preserving repair; accounting includes both physical calls. An attribution-policy refusal spends no repair call. See [configuration](../../docs/configuration.md) for migration and limits.
