# Example configuration

Merge these into your host's config; do not replace the file.

- [`hermes.config.yaml`](hermes.config.yaml) — Hermes (`~/.hermes/config.yaml`)
- [`openclaw.json`](openclaw.json) — OpenClaw (`openclaw.json`)

Both files are required by `./scripts/verify packaging all`; the OpenClaw `config` block is
also loaded through the real core loader. Hermes-specific outer keys belong to the host and
are exercised by the opt-in Hermes integration gate. Comments carry explanation without
adding fake keys to either plugin `config` block.

## The keys

| Key | Default | What it does |
| --- | --- | --- |
| `workspace_roots` | *(required)* | The only roots that may become sources. Everything else is `UNSAFE_SOURCE`. |
| `cache_dir` | `~/.cache/context-shunt` | Private cache: SQLite authorization metadata plus content-addressed payload files. Refused if it resolves inside a workspace root. `spill_dir` is accepted as a pre-1.1 alias. |
| `denylist` | `[]` | Extra administrator denials, relative globs inside a root. The built-in secret policy applies regardless. |
| `gate_enabled` | `true` | Block oversized and unprovable reads before they run. |
| `reader.enabled` | `true` | Controls reader execution; `false` refuses without a model call even if an adapter still registers the tool. |
| `reader.model` | `gpt-5.6-luna` | Configurable since contract revision 1.1. What keeps a substitution from going unnoticed is the envelope's provenance block, not a hardcoded value. |
| `reader.provider` | `""` | Optional provider to pin. Empty lets the host route. |
| `reader.attribution_policy` | `allow_unverified` | What to do when the host cannot prove which model answered. See below. |
| `reader.fallback_chain` | `[]` | Availability-only fallback targets, at most four. |
| `inspect.enabled` | `true` | Deterministic exact extraction: zero model calls, 16 KiB per page, cumulative disclosure ceiling. |
| `stats.enabled` | `true` | Read-only session accounting. |
| `suma_post_tool.enabled` | `false` | Optional oversized post-tool spill request. Both adapters report it unsupported, so `true` does not activate the mode. |
| `limits` | `{}` | Deployment caps. **May only be narrowed** — a wider value is refused at load. |

## `attribution_policy`

Neither supported host proves which model generated the tokens
([`capability-matrix.md`](../../docs/capability-matrix.md) has the per-host ceiling and the
source evidence).

- `allow_unverified` **(default)** — publish the answer with the truthful
  `attribution_status`. On Hermes that is `unverified`; on OpenClaw, `resolved`.
- `require_match` — refuse to answer unless the host reported a selection that agrees with
  the request, returning `PROVENANCE_UNAVAILABLE` instead.

`require_match` is a legitimate choice, but on a host that cannot prove attribution it
disables the reader entirely. `inspect` and `stats` keep working either way — neither
touches a model. A value that *contradicts* the request is refused under both policies.

## Host model overrides

Both hosts gate plugin model overrides. Without the `llm` block shown in each example, the
host refuses the override and the reader runs on whatever the host would have picked. That
is not silently accepted: the envelope reports the requested model and the attribution
status regardless.

On Hermes the reader is additionally registered as an auxiliary task, so it appears in
`hermes model → Configure auxiliary models` under `auxiliary.context_shunt_reader`. Anything
set there wins over the plugin's own defaults.

## Narrowing caps

Every cap lives in [`contracts/v1/limits.json`](../../contracts/v1/limits.json) and a
deployment may only lower it. Some worth knowing about:

```yaml
limits:
  full_read_max_lines: 200              # block smaller reads too
  store_handle_ttl_seconds: 900         # shorter handle lifetime
  disclosure_max_per_source_bytes: 65536  # tighter inspect budget
  store_max_bytes: 33554432             # smaller cache
```

A wider value is refused at load with `LIMIT_MAY_ONLY_NARROW`, so a config file cannot widen
the boundary the acceptance gates measure.

The complete list of accepted keys and current defaults, including concurrency, retry,
deadline, store, TTL, JSON, inspect, disclosure, and stats limits, is in
[`docs/configuration.md`](../../docs/configuration.md). Token and attempt accounting is in
[`docs/metrics.md`](../../docs/metrics.md).
