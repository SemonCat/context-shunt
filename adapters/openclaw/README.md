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
| `context_shunt_read` | a cited answer, or labelled bounded legacy compaction after exhausted availability; citation failure preserves a bounded error and handles | at least one per processed chunk; retries/fallback can add more |
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

OpenClaw selects the shared TypeScript legacy compactor after exhausted availability.
It returns `partial/LEGACY_COMPACTED`, `result_kind: legacy_compaction`, and
`provenance.derived: false`, with an empty answer/citations and a dedicated bounded summary.
It is independent of the question and covers only the first requested source, retaining
all handles and omissions. A citation verification failure remains an explicit bounded
`CITATION_INVALID` error with its recovery handles; it does not become a semantic answer or
an availability fallback. If compaction cannot be published safely, the bounded reader
failure remains; it does not downgrade to an exact prefix. The older `automatic_extract`
and `fallback_max_bytes` fields remain accepted for configuration compatibility, but do not
control this OpenClaw availability fallback. Generic TypeScript sessions retain the old
automatic-extraction default unless the legacy session option is selected.

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
