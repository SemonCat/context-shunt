# Hermes adapter

[Project README](../../README.md) | [繁體中文](../../README.zh-TW.md)

Install, uninstall, cleanup and migration instructions live in
[`docs/install.md`](../../docs/install.md). The mode matrix — including the model
attribution ceiling on this host, the session-lifecycle rule, and why the optional
oversized post-tool mode is disabled — is in
[`docs/capability-matrix.md`](../../docs/capability-matrix.md).

This adapter registers three read-only tools and no writer:

| Tool | Returns | Model calls |
| --- | --- | --- |
| `context_shunt_read` | a cited answer — generated text, not source bytes | at least one per processed chunk; retries/fallback can add more |
| `context_shunt_inspect` | exact snapshot bytes, capped per page and cumulatively | zero |
| `context_shunt_stats` | this session's own token accounting | zero |

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
