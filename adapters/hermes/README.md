# Hermes adapter

[Project README](../../README.md) | [繁體中文](../../README.zh-TW.md)

Install, uninstall, cleanup and migration instructions live in
[`docs/install.md`](../../docs/install.md). The mode matrix — including the model
attribution ceiling on this host, the session-lifecycle rule, and why the optional
oversized post-tool mode is disabled — is in
[`docs/capability-matrix.md`](../../docs/capability-matrix.md).

This adapter registers up to four read-only tools and no writer:

| Tool | Returns | Model calls |
| --- | --- | --- |
| `context_shunt_read` | a cited answer, or a labelled bounded exact prefix after exhausted availability | at least one per processed chunk; retries/fallback can add more |
| `context_shunt_inspect` | exact snapshot bytes, capped per page and cumulatively | zero |
| `context_shunt_stats` | this session's own token accounting | zero |
| `context_shunt_import` | a handle and bounded metadata for a producer's already-persisted artifact — never its bytes | zero |

`context_shunt_import` is registered only when `artifact_import.enabled` is set and the
capability probe supports the mode; registering a permanently-refusing surface in front of
the model would be worse than not offering it. This host supports the mode because the
import needs no interception ordering at all — it is not post-tool interception, and
`suma_post_tool` stays unsupported here regardless.

Run `./scripts/verify integration hermes --mode local` against a real host checkout to
check the wiring; without one it reports `NOT_RUN`, never a pass.

Tool registration follows host capability. `reader.enabled: false` controls execution and
returns a bounded refusal without calling a model; it does not require the adapter to hide
an otherwise registerable tool.

The effective reader target uses `auxiliary.context_shunt_reader` over the plugin's
`reader` defaults; Hermes' `auto` means inherit. The plugin `llm` policy must authorize the
chosen overrides. This host can report only `attribution_status: unverified`, never
provider-authoritative `actual`. See the checked
[`hermes.config.yaml`](../../examples/config/hermes.config.yaml).

The automatic exact-text escape hatch defaults on (`reader.automatic_extract: true`,
`reader.fallback_max_bytes: 2048`, range 1–4096). Disabling inspect also disables it.
It selects the first source's byte prefix, independently of the question, with all handles,
locators, omissions and disclosure accounting retained. It is never an LLM summary.
See [shared configuration](../../docs/configuration.md#automatic-exact-extraction-after-reader-unavailability)
for trigger exclusions, bounds and the 1.1 behavioral compatibility change.
