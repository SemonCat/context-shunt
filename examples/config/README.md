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
| `reader.automatic_extract` | `true` | Exact extraction escape hatch after exhausted availability; also requires inspect. |
| `reader.fallback_max_bytes` | `2048` | 1–4096 source bytes, narrowed by all existing limits. |
| `inspect.enabled` | `true` | Deterministic exact extraction: zero model calls, 16 KiB per page, cumulative disclosure ceiling. |
| `stats.enabled` | `true` | Read-only session accounting. |
| `suma_post_tool.enabled` | `false` | Optional oversized post-tool spill request. Both adapters report it unsupported, so `true` does not activate the mode. |
| `artifact_import.enabled` | `false` | Adopt an oversized tool-result artifact an external producer already persisted. Supported on Hermes; the OpenClaw core has no import boundary and reports the mode unsupported. |
| `artifact_import.roots` | `[]` | Allowlist of canonical directories an artifact and its manifest may live under. Separate from `workspace_roots`, and refused if a root contains the private cache. |
| `artifact_import.accepted_manifest_schemas` | `[]` | Allowlist of producer manifest schemas. A manifest declaring a schema outside it is refused even when a translation profile for that schema exists. |
| `limits` | `{}` | Deployment caps. **May only be narrowed** — a wider value is refused at load. |

## `artifact_import`

The import boundary is the deployable answer to oversized *tool results*, and it is
deliberately not the same thing as `suma_post_tool`. That mode needs the host to hand a
plugin the complete result before truncation and accept a replacement before persistence,
which neither supported host does. An artifact a producer already wrote to disk needs
neither: the capture already happened, so all the host has to supply is a way to invoke
the import.

Enabling it is two decisions, and neither has a permissive default:

```yaml
artifact_import:
  enabled: true
  roots:
    - /var/lib/your-compactor/artifacts
  accepted_manifest_schemas:
    - context_shunt.artifact_import.v1
```

`enabled: true` with no roots, or with no accepted schema, is a configuration error rather
than an allow-all. A root that contains `cache_dir` is refused too — otherwise a manifest
could name one of the core's own immutable blobs as if it were a producer artifact.

The `config` block shown above is Hermes-only. The OpenClaw plugin's config schema is a
closed key set and its core has no import boundary, so adding `artifact_import` there is
refused at load; its capability report says `artifact_import: unsupported` with reason
`IMPORT_UNIMPLEMENTED`, which names a core gap rather than a host limitation.

Everything a manifest claims is re-proven before a handle exists — see
[`docs/security.md`](../../docs/security.md) for the checks and
[`docs/architecture.md`](../../docs/architecture.md) for where the boundary sits.

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
