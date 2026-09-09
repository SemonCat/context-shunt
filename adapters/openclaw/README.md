# OpenClaw adapter

[Project README](../../README.md) | [繁體中文](../../README.zh-TW.md)

Install, uninstall, cleanup and migration instructions live in
[`docs/install.md`](../../docs/install.md). The mode matrix — including the model
attribution ceiling on this host, the session-lifecycle rule, and why the optional
oversized post-tool mode is disabled — is in
[`docs/capability-matrix.md`](../../docs/capability-matrix.md).

This adapter registers three read-only tools and no writer. It does **not** register
`context_shunt_import`: the external-artifact boundary exists only in the Python core, so
the capability report says `artifact_import: unsupported` with reason
`IMPORT_UNIMPLEMENTED`. That names a core gap rather than a host limitation — nothing about
OpenClaw prevents the mode.

| Tool | Returns | Model calls |
| --- | --- | --- |
| `context_shunt_read` | a cited answer, or a labelled bounded exact prefix after exhausted availability | at least one per processed chunk; retries/fallback can add more |
| `context_shunt_inspect` | exact snapshot bytes, capped per page and cumulatively | zero |
| `context_shunt_stats` | this session's own token accounting | zero |

Run `./scripts/verify integration openclaw --mode local` against a real host checkout to
check the wiring; without one it reports `NOT_RUN`, never a pass.

Tool registration follows host capability. `reader.enabled: false` controls execution and
returns a bounded refusal without calling a model; it does not require the adapter to hide
an otherwise registerable tool.

The adjacent OpenClaw `llm` policy must authorize the target requested in plugin `config`.
An empty `reader.provider` passes a bare model to OpenClaw so the host owns provider routing;
an explicit provider is pinned. The isolated completion path can prove the host's
post-policy selection (`resolved`), not a provider-authoritative `actual` model. See the checked
[`openclaw.json`](../../examples/config/openclaw.json).

The automatic exact-text escape hatch defaults on (`reader.automatic_extract: true`,
`reader.fallback_max_bytes: 2048`, range 1–4096). Disabling inspect also disables it.
It selects the first source's byte prefix, independently of the question, with all handles,
locators, omissions and disclosure accounting retained. It is never an LLM summary.
See [shared configuration](../../docs/configuration.md#automatic-exact-extraction-after-reader-unavailability)
for trigger exclusions, bounds and the 1.1 behavioral compatibility change.
