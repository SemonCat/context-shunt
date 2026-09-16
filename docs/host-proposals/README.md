# Hermes host proposal (not applied by this repository)

`hermes-0.21.3-consumer-capabilities.patch` targets the exact `model_tools.py` inspected
from image `context-shunt/hermes:5492046470eb-v2026.9.14`, Hermes 0.21.3. The original
file SHA-256 was `c99620c824ab59f341ac7d0e22cde016b0c469d0643e7a5a5a82e0d63176e4b5`.
The exact patched result SHA-256 is
`738e5ede949a93ab7632da544f17422780fac7ecd7ea5a4749158a9de3519897`;
the probe refuses any other host version or source digest.

The missing seam is at the final transform dispatch. Hermes already carries
`enabled_tools`, `enabled_toolsets`, and `disabled_toolsets` through
`handle_function_call`, including recursive deferred `tool_call`; it does not carry them
into `transform_tool_result`. The proposal:

- snapshots the copied model-visible names into sorted tuples inside a read-only mapping;
- uses Hermes' own `scoped_deferrable_names(get_tool_definitions(...,
  skip_tool_search_assembly=True))` for the deferred universe;
- passes a new `consumer_capabilities` keyword only when direct scope exists;
- fails closed to an empty deferred set on derivation failure and passes no descriptor
  when `enabled_tools` is absent.

This is a proposal for upstream/operator review, not a vendored dependency or authorization
to edit the live host. Apply it only to an isolated exact-version candidate, then run:

The immutable build inputs, one-file candidate image plan, official-hook exhaustion,
isolated canary, rollback, and upgrade risks are collected in
[`hermes-0.21.3-operator-approval.md`](hermes-0.21.3-operator-approval.md).

```sh
CONTEXT_SHUNT_HERMES_ROOT=/path/to/patched/hermes-0.21.3 \
CONTEXT_SHUNT_HERMES_PYTHON=/path/to/that/runtime/python \
./scripts/verify integration hermes --mode post-tool
```

Upgrade risks: line/context drift can make the patch fail or, worse, preserve a descriptor
whose source no longer matches final model-visible scope. Re-derive the patch and rerun the
red/green probe for every Hermes version; do not forward global registry availability.
Hook consumers that reject unknown keywords are protected only because Hermes' plugin hook
dispatcher filters by callable signature—this must also be rechecked on upgrade.

Rollback: keep the previous Hermes image and incumbent compactor package intact. In one
operator-owned rollback, disable Shunt capture, re-enable the incumbent, and restore the
previous image. Never leave both transform listeners active, because Hermes accepts the
first string result and listener ordering is not a redundancy guarantee.
