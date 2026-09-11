# Hermes adapter

[Project README](../../README.md) | [繁體中文](../../README.zh-TW.md)

Install, uninstall, cleanup and migration instructions live in
[`docs/install.md`](../../docs/install.md). The mode matrix — including the model
attribution ceiling on this host, the session-lifecycle rule, and what `tool_result_capture`
does and does not require — is in
[`docs/capability-matrix.md`](../../docs/capability-matrix.md).

Both live canaries were retired on 2026-09-10. This repair does not re-enable Hermes.
The pre-read gate passes unknown/unclassifiable calls and searches unchanged; it only blocks
positively established large unbounded reads on allowed sources.

This adapter registers up to four read-only tools, no writer, and — only with an explicit
operator attestation (`tool_result_capture.host_ordering_verified_locally: true`) — one
`transform_tool_result` hook that captures an eligible oversized tool result and replaces it
with a bounded pointer envelope before it reaches context. That hook never answers a
question (Hermes does not forward one to it); a captured result is answered afterward
through `context_shunt_read` like any other handle. See
[`docs/acceptance.md`](../../docs/acceptance.md#tool_result_capture-cutover-on-hermes) for
the cutover plan.

| Tool | Returns | Model calls |
| --- | --- | --- |
| `context_shunt_read` | a cited answer, or mandatory bounded legacy compaction for Shunt-owned failures | at least one per processed chunk; retries/fallback can add more |
| `context_shunt_inspect` | exact snapshot bytes, capped per page and cumulatively | zero |
| `context_shunt_stats` | this session's own token accounting | zero |
| `context_shunt_import` | a handle and bounded metadata for a producer's already-persisted artifact — never its bytes | zero |

`context_shunt_import` is registered only when `artifact_import.enabled` is set and the
capability probe supports the mode; registering a permanently-refusing surface in front of
the model would be worse than not offering it. This host supports the mode because the
import needs no interception ordering at all — it is not post-tool interception, and
`tool_result_capture` (formerly `suma_post_tool`) is a separate mode, off by default and
supported only with an explicit operator attestation — see
[`docs/capability-matrix.md`](../../docs/capability-matrix.md#tool_result_capture-on-hermes-021-what-changed-and-what-did-not).

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

Shunt-owned read, capture/store, and eligible inspection failures automatically return bounded deterministic `partial/LEGACY_COMPACTED` output. This invariant cannot be disabled: former legacy-compaction switches are deprecated no-ops. The output preserves the original failure code and bounded `failure_detail`, marks incomplete coverage, and never claims model derivation or verified citations. Safe representative excerpts are expected; full raw passthrough is forbidden.

Caller errors, unsupported operations/versions, unsafe/binary/secret content, immutable binding or session mismatch, expired/changed sources, provenance-policy refusal, attribution mismatch, cancellation, and disclosure exhaustion remain explicit refusals. `LIMIT_EXCEEDED` qualifies only for enumerated Shunt implementation/store capacity details, never safety or disclosure caps. Citation failures receive at most one pinned, deadline- and budget-preserving repair; accounting includes both physical calls. An attribution-policy refusal spends no repair call. See [configuration](../../docs/configuration.md) for migration and limits.


Hermes result capture uses an exact identity classifier before session, artifact,
provider, or accounting work. Names are trimmed and lowercased consistently with the
pre-read gate. Protected results pass verbatim: `skill_view`, `skills_list`, `clarify`,
`todo`, every registered `context_shunt_*` tool, and full generated MCP identities
`mcp__<server>__list_resources`, `list_prompts`, and `get_prompt`. Catalog/prompt
identities stay protected even when explicitly allowlisted.

Only `read_file`, `search_files`, and additional identities in the Hermes-only
`capture_tool_allowlist` configuration are eligible. Unknown, interaction, control,
and write tools (including unrestricted `terminal`) default to passthrough. An exact
operator allowlist entry can opt an additional tool into capture, but cannot override
protected identities. Similar names such as `context_shunt_read_fake` have neither
protected status nor default capture eligibility. Paths, `SKILL.md`, payload text,
and arbitrary name substrings never determine classification.

MCP `read_resource` requires an explicit exact allowlist entry, for example
`capture_tool_allowlist: [mcp__docs__read_resource]`. Hermes can register a server-native
tool under the same identity. Its registry exposes the current handler, but the result
hook does not supply the executed handler or immutable utility provenance. Looking up
the current registry after execution cannot prove which handler produced the result.
The allowlist is an operator assertion about the intended identity, not automatic
provenance verification; verify server configuration/collisions before adding an entry
and recheck when that configuration changes. No Hermes/package patch is required.

Small and structured/multimodal results remain unchanged. Once an eligible string is
measured oversized, a Shunt-owned internal capture failure returns bounded `LEGACY_COMPACTED` after independent source safety checks.
`skill_view` also remains exempt from the pre-read gate; a generic `read_file` of a large
`SKILL.md` remains subject to the normal gate and capture. OpenClaw behavior is unchanged.
