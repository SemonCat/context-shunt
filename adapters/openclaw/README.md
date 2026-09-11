# OpenClaw adapter

[Project README](../../README.md) | [繁體中文](../../README.zh-TW.md)

Install, uninstall, cleanup and migration instructions live in
[`docs/install.md`](../../docs/install.md). The mode matrix — including the model
attribution ceiling on this host, the session-lifecycle rule, and the official middleware capture boundary — is in
[`docs/capability-matrix.md`](../../docs/capability-matrix.md).

This adapter registers three read-only tools and no writer. It does **not** register
`context_shunt_import`: the external-artifact boundary exists only in the Python core, so
the capability report says `artifact_import: unsupported` with reason
`IMPORT_UNIMPLEMENTED`. That names a core gap rather than a host limitation — nothing about
OpenClaw prevents the mode.

| Tool | Returns | Model calls |
| --- | --- | --- |
| `context_shunt_read` | a cited answer, or mandatory bounded legacy compaction for Shunt-owned failures | at least one per processed chunk; retries/fallback can add more |
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

Shunt-owned read, capture/store, and eligible inspection failures automatically return bounded deterministic `partial/LEGACY_COMPACTED` output. This invariant cannot be disabled: former legacy-compaction switches are deprecated no-ops. The output preserves the original failure code and bounded `failure_detail`, marks incomplete coverage, and never claims model derivation or verified citations. Safe representative excerpts are expected; full raw passthrough is forbidden.

Caller errors, unsupported operations/versions, unsafe/binary/secret content, immutable binding or session mismatch, expired/changed sources, provenance-policy refusal, attribution mismatch, cancellation, and disclosure exhaustion remain explicit refusals. `LIMIT_EXCEEDED` qualifies only for enumerated Shunt implementation/store capacity details, never safety or disclosure caps. Citation failures receive at most one pinned, deadline- and budget-preserving repair; accounting includes both physical calls. An attribution-policy refusal spends no repair call. See [configuration](../../docs/configuration.md) for migration and limits.

## OpenClaw tool-result capture is retired

OpenClaw 2026.9.3 / `773b6d8` exposes
`api.registerAgentToolResultMiddleware(handler, { runtimes: ["openclaw", "codex"] })`,
and the manifest keeps that entitlement for compatibility. The adapter deliberately does not
register the callback. A callback return is only a candidate replacement; it does not prove
what the host persisted or what the model received.

The retirement canary observed accounting labelled a result `SPILLED` with a pointer boundary
but no usable handle, while the reader was never called and the producer receipt remained
effective in run history. Enabling this mode would therefore make accounting and delivery
disagree. `tool_result_capture.enabled` and the deprecated `suma_post_tool` alias are accepted
so existing configuration validates, but OpenClaw reports `tool_result_capture: unsupported`
with `ORDERING_UNPROVEN` and passes results through unchanged.

The deterministic capture engine remains covered by unit tests behind a synthetic capability;
that test path is not host support. Re-enabling capture requires a host seam that proves, for
the same run, that the model-visible result is the pointer envelope, that it contains at least
one usable `source_id`/`snapshot_id` handle, that raw producer bytes are absent from effective
history, and that accounting records the same boundary. This adapter has no such proof channel.

The `read_only_tools` configuration field remains validated for migration compatibility. It
does not opt any OpenClaw tool into capture while this mode is retired. The local gate,
question-driven reader, deterministic inspector and session stats continue to use their own
supported seams.

### Synthetic trust-boundary classification

`classifyToolResult` is independently tested and runs before result access or
session/store/provider/accounting work in the synthetic engine. Identities are trimmed
and lowercased; punctuation and repeated underscores are preserved. Payload text,
paths (including `SKILL.md`), and details text never grant identity or eligibility.

Protected identities override even mistaken `tool_result_capture.read_only_tools`
entries: `skill_view`, `skills_list`, `ask_user`, `clarify`, `todo`, the three
`context_shunt_*` tools listed above, `message`, `list_mcp_resources`, and
`list_mcp_resource_templates`. Full generated MCP catalog/prompt identities
`mcp__<server>__list_resources`, `mcp__<server>__list_prompts`, and
`mcp__<server>__get_prompt` are protected for nonempty normalized server names using
letters, digits, or underscores. Session identities beginning `sessions_` and control
families with an underscore-delimited `send`, `spawn`, `write`, `edit`, `delete`,
`remove`, `update`, `create`, or `terminate` token are also protected. Arbitrary
substrings such as `rewrite` do not establish control identity.

Default eligible identities are exactly `read`, `web_fetch`, `web_search`, and
`read_mcp_resource` (the resource-read identity in this adapter's OpenClaw/Codex
synthetic contract). Additional exact configured identities are eligible after
normalization. In particular, `mcp__docs__read_resource` requires operator opt-in:
a native MCP tool may shadow a generated utility, and this adapter has no immutable
executed-handler provenance seam. Unknown, interactive, control and write results
pass through by default, regardless of size. Spoofs such as `skills_list_extra`,
`context_shunt_read_fake`, and `mcp__x__read_resource_extra` have neither protected
status nor default eligibility; an operator can explicitly configure these unknown IDs.

Eligible candidates retain the existing small-result and structured/multimodal rules,
control-details veto, ingress ceilings, and bounded failure with no raw fallback.
Synthetic tests cover oversized resource capture, inspect/read recovery, and one-time
baseline credit. None of this proves live protection or capture: the real middleware
remains **unregistered**, capability remains **unsupported / ORDERING_UNPROVEN**, and
activation still requires the proof-bearing host seam described above.
