# Configuration reference

This document names the configuration accepted by both cores and the current defaults
loaded from [`contracts/v1/limits.json`](../contracts/v1/limits.json). The JSON file is the
normative source. Every numeric `limits` override may only lower its corresponding default;
a higher value fails load with `LIMIT_MAY_ONLY_NARROW`.

## Shared plugin keys

| Key | Type | Current default | Behavior |
| --- | --- | --- | --- |
| `workspace_roots` | non-empty string array | required | Only canonical regular files below these roots can become sources. |
| `cache_dir` | string | `$CONTEXT_SHUNT_CACHE`, otherwise `~/.cache/context-shunt` | SQLite metadata and content-addressed blobs; must resolve outside every workspace root. |
| `spill_dir` | string | unset | Deprecated pre-1.1 alias, used only when `cache_dir` is absent. |
| `denylist` | string array | `[]` | Additional relative glob denials inside roots. Built-in secret checks always remain active. |
| `gate_enabled` | boolean | `true` | Registers the local pre-read gate when the host capability is supported. |
| `reader.enabled` | boolean | `true` | Controls reader execution: `false` refuses with `INVALID_REQUEST` before a model call (core reason `READER_DISABLED`). A capable adapter may still register the tool. |
| `reader.model` | non-empty string, at most 128 UTF-8 bytes | `gpt-5.6-luna` | Model requested from the host. |
| `reader.provider` | string, at most 128 UTF-8 bytes | `""` | Provider request; empty delegates routing to the host. |
| `reader.attribution_policy` | enum | `allow_unverified` | `allow_unverified` publishes the host's truthful attribution status; `require_match` refuses below actual/resolved agreement. |
| `reader.fallback_chain` | array of `{model, provider?}` | `[]` | At most four availability targets. It does not rescue a semantically weak answer. |
| `reader.automatic_extract` | boolean | `true` | Secondary exact extraction after wholly exhausted availability when legacy compaction is disabled or unsafe; also requires `inspect.enabled`. |
| `reader.fallback_max_bytes` | integer 1–4096 | `2048` | Automatic prefix byte cap, narrowed by request, inspect, disclosure and output budgets. |
| `reader.legacy_compaction` | boolean | `true` | Python/Hermes: prefer bounded deterministic compaction for terminal `MODEL_ERROR`, `TIMEOUT`, and `CITATION_INVALID`, before automatic extraction. Model-identity and provenance-policy refusals remain excluded. |
| `reader.legacy_compaction_max_chars` | integer 1000–60000 | `16000` | Character budget handed to the compaction algorithm before the envelope's own 16 KiB byte cap is separately enforced. |
| `inspect.enabled` | boolean | `true` | Registers deterministic exact extraction. |
| `stats.enabled` | boolean | `true` | Registers read-only session accounting. |
| `tool_result_capture.enabled` | boolean | `false` | Requests the optional oversized-tool-result capture path. OpenClaw 2026.9.3 uses the official middleware, subject to read-only eligibility, ingress ceilings and atomic reducer cutover. On Hermes, additionally requires `tool_result_capture.host_ordering_verified_locally: true` to be reported supported at all — see [`capability-matrix.md`](capability-matrix.md#tool_result_capture-on-hermes-021-what-changed-and-what-did-not). The deprecated key `suma_post_tool.enabled` is still accepted as an alias; setting both to disagreeing values is refused with `TOOL_RESULT_CAPTURE_CONFIG_CONFLICT`. |
| `tool_result_capture.host_ordering_verified_locally` | boolean | `false` | An explicit **operator attestation** that the operator personally verified their own installed Hermes host's `transform_tool_result` capture-before-truncation ordering. This code does not and cannot prove it; setting it without reading the linked evidence first is the deployment's own risk. |
| `artifact_import.enabled` | boolean | `false` | Requests the external-artifact import boundary. Supported on Hermes; the OpenClaw core has no import implementation and reports the mode unsupported with `IMPORT_UNIMPLEMENTED`. |
| `artifact_import.roots` | string array, at most 8 | `[]` | Canonical directories a producer's artifact and manifest may live under. A separate allowlist from `workspace_roots`; a root that contains `cache_dir` is refused with `CACHE_INSIDE_IMPORT_ROOT`. |
| `artifact_import.accepted_manifest_schemas` | string array | `[]` | Producer manifest schemas this deployment authorizes. A manifest declaring a schema outside this list is refused with `MANIFEST_SCHEMA_NOT_ALLOWED` even when a translation profile exists for it. |
| `limits` | integer map | values below | Narrows contract caps. Unknown, negative, invalid-zero, or wider values are refused. |

Compatibility inputs `writer.enabled: true` and an `operations` array containing
`propose_patch` are explicitly refused with `WRITER_UNSUPPORTED_CONFIGURATION`. There is no
writer tool. Both public core loaders reject unknown top-level keys, nested keys, limit
names, and invalid types. The OpenClaw manifest adds a separate host validation boundary.

## Artifact import

`artifact_import` is off by default with no roots and no accepted schemas, and enabling it
requires stating what it trusts. `enabled: true` with an empty `roots` **or** an empty
`accepted_manifest_schemas` is refused at load with `BAD_CONFIGURATION`, because an enabled
boundary that names nothing in particular would be an allow-all in everything but name.

The two allowlists answer different questions:

* `roots` — *where* an artifact may live. Canonicalized at load. A path that resolves
  outside every root is `UNSAFE_SOURCE / OUTSIDE_WORKSPACE_ROOT`, and the built-in secret
  policy plus the administrator `denylist` apply inside a root exactly as they do to a
  workspace root.
* `accepted_manifest_schemas` — *whose* manifests may be read. The core owns
  `context_shunt.artifact_import.v1`; any other accepted value names a foreign producer
  schema that a translation profile normalizes into it. Registering a profile is not an
  authorization, so a shape the core knows how to read is still refused until it is listed
  here.

A root that contains `cache_dir` is refused: a manifest could otherwise name one of the
core's own immutable blobs as if it were a producer artifact.

The manifest itself is capped at `artifact_import.max_manifest_bytes` (64 KiB) from
[`contracts/v1/limits.json`](../contracts/v1/limits.json). That cap is normative and is not
in the narrowable `limits` map.

This section is Hermes-only. The OpenClaw plugin config schema is a closed key set, so
adding `artifact_import` to `openclaw.json` is refused at load rather than accepted and
ignored — which matches the capability report rather than contradicting it.

## Model selection and fallback

The effective primary target starts with `reader.provider` and `reader.model`. Each
`reader.fallback_chain` entry must contain a non-empty `model`; `provider` defaults to the
empty host-routed value. Fallback is availability-only: provider failures may advance to a
fallback, but an answer that is vague or poorly reasoned does not. Ask a refined question
against the returned handle or use `inspect` instead.

Every target attempt retains its own usage and attribution. `fallback_used` indicates that
the successful output came from the chain. A concrete model contradiction is
`MODEL_ERROR`, regardless of attribution policy.

### Hermes override precedence

Hermes has two separate layers:

1. `plugins.entries.context-shunt.llm` authorizes whether this plugin may request model or
   provider overrides. `allow_model_override`, `allowed_models`,
   `allow_provider_override`, `allow_agent_id_override`, and `allow_profile_override` are
   host policy keys, not core configuration.
2. The adapter registers `context_shunt_reader` as an auxiliary task. User values in
   `auxiliary.context_shunt_reader.provider` and `.model` override the plugin's
   `reader.provider` and `reader.model`. Hermes' `auto` sentinel means inherit. The
   registered task also has a host-facing `timeout` default of 20 seconds, but core calls
   pass their own bounded timeout derived from `model_call_deadline_ms`.

The auxiliary override is read through Hermes' public config loader because the current
`ctx.llm` facade has no task argument. The effective target still appears in envelope
provenance. Hermes cannot distinguish a provider report from a request echo, so its maximum
supported attribution remains `unverified`.

### OpenClaw capture configuration

`tool_result_capture.read_only_tools` is an OpenClaw adapter-only list of exact additional
read-only tool IDs (at most 100, each 1–128 characters). Defaults already cover `read`,
`web_fetch`, and `web_search`. For example: `{"enabled": true, "read_only_tools": ["mcp__logs__query"]}`.
Verify the actual producer contract before adding an ID. Unknown/mutating/control tools are
excluded. The old `host_ordering_verified_locally` key is ignored on OpenClaw.
OpenClaw always selects legacy compaction for exhausted availability. The older
`reader.automatic_extract` and `reader.fallback_max_bytes` remain accepted but do not control
this fallback; unsafe compaction preserves the bounded reader failure, never an exact prefix.
The adapter removes its allowlist before invoking the shared core config loader; there is
no parallel artifact schema or store. Both `openclaw` and `codex` middleware runtimes are
selected; Codex-native tools remain observe-only. Version 2026.9.3 and a callable official
registration API are required. No guessed hook or host-version fallback enables capture.

Disable Tokenjuice and other reducers in the same transaction as enabling capture; see
[cutover and acceptance](acceptance.md#openclaw-middleware-cutover).
The shared `limits.max_tool_result_bytes` controls spill bytes; host ingress ceilings are
independent and cannot be raised by this plugin. [Coverage/limits](capability-matrix.md#openclaw).

### OpenClaw host keys

OpenClaw uses `plugins.entries.context-shunt.enabled` and a sibling `llm` policy. The
current example authorizes the default target with `allowModelOverride`, `allowedModels`,
and `allowedCompletionModels`. These camel-case keys belong to OpenClaw; they are not
accepted inside the shared `config` block. The plugin's reader uses the isolated completion
runtime and can report the host's post-policy selection as `resolved`.

See the checked examples in [`examples/config/`](../examples/config/). The packaging gate
loads the OpenClaw `config` block with the real core loader.

## Current limit defaults

These names are accepted under `limits`. They are current defaults, not timeless promises.

### Gate and output bytes

| Key | Default | Meaning |
| --- | ---: | --- |
| `full_read_max_lines` | 350 | Largest full text read allowed by line count. |
| `targeted_read_max_lines` | 350 | Largest targeted line read. |
| `targeted_search_max_matches` | 200 | Largest bounded search match count. |
| `probe_max_lines_scanned` | 351 | Line probe stops once the gate decision is known. |
| `max_tool_result_bytes` | 16,384 | Direct tool-result cap. |
| `max_envelope_bytes` | 16,384 | Ordinary serialized envelope cap. |
| `max_extended_envelope_bytes` | 20,480 | Inspect/stats envelope cap, leaving room around a 16 KiB extraction/page. |
| `max_targeted_read_bytes` | 16,384 | Targeted read byte cap. |
| `max_extraction_bytes` | 16,384 | Exact extraction content cap. |
| `max_source_bytes` | 8,388,608 | Maximum captured source/payload. |
| `max_chunk_bytes` | 32,768 | Per-reader-chunk byte cap. |
| `max_answer_bytes` | 8,192 | Generated answer cap. |
| `max_quote_bytes` | 512 | Per-citation quote cap. |
| `max_question_bytes` | 2,048 | Question cap. |
| `session_spill_quota_bytes` | 67,108,864 | Legacy/session spill-engine byte quota. |

### Reader count, token, and time budgets

| Key | Default | Meaning |
| --- | ---: | --- |
| `max_sources_per_request` | 8 | Sources per read request. |
| `max_chunks_per_request` | 8 | Planned chunks per request. |
| `max_citations` | 16 | Published citations. |
| `max_concurrent_model_calls` | 2 | Reader calls in flight. |
| `max_chunk_overlap_lines` | 1 | Adjacent chunk overlap; it still consumes budget. |
| `max_transient_retries` | 1 | Core retry count for transient provider failures. |
| `max_chunk_tokens` | 8,000 | Estimated tokens in one chunk. |
| `max_request_input_tokens` | 64,000 | Combined model input budget, including attempts. |
| `max_output_tokens_per_call` | 2,048 | Requested model output cap per call. |
| `bytes_per_token_estimate` | 4 | Divisor used by the named `bytes_div_4` estimator. |
| `gate_probe_deadline_ms` | 1,000 | Gate probe deadline. |
| `spill_io_deadline_ms` | 5,000 | Snapshot/spill I/O deadline. |
| `model_call_deadline_ms` | 45,000 | Per model call ceiling. |
| `request_deadline_ms` | 60,000 | Whole reader request, including queueing, retries, verification, and publication. |

The 45-second call ceiling sits inside a 60-second request. A retry is permitted by count
but may not fit the remaining deadline. Narrowing the per-call ceiling can leave time for a
retry but will reject more slow first attempts.

### Inspect and disclosure

| Key | Default | Meaning |
| --- | ---: | --- |
| `inspect_max_result_bytes` | 16,384 | Source bytes in one extraction result. |
| `inspect_max_segments` | 64 | Segments in one extraction. |
| `inspect_max_lines_per_page` | 400 | Lines emitted per page. |
| `inspect_max_bytes_per_page` | 16,384 | Source-byte page cap. |
| `inspect_max_scan_lines` | 20,000 | Search scan line budget. |
| `inspect_max_scan_bytes` | 8,388,608 | Search scan byte budget. |
| `inspect_max_search_matches` | 200 | Literal search hits. |
| `inspect_max_needle_bytes` | 512 | Literal search needle. |
| `disclosure_max_per_source_bytes` | 262,144 | Cumulative exact disclosure for one content hash in a session. |
| `disclosure_max_per_session_bytes` | 1,048,576 | Cumulative exact disclosure across the session. |

The internal tool-argument contract allows `max_result_bytes` and `max_scan_lines` to narrow
a single inspect call. The current OpenClaw registered schema does not expose those two
optional fields, so portable host calls rely on configured limits. Continuation uses an
authenticated `cursor`; the same source, snapshot, and selector must be supplied again.
Wire-size escaping can make a page smaller than the source-byte cap. A line selector cannot
split an over-wide single line; use a byte selector if exact pieces are acceptable.

### Store, TTL, JSON, and stats

| Key | Default | Meaning |
| --- | ---: | --- |
| `spill_ttl_seconds` | 3,600 | TTL used by the optional spill engine. |
| `json_max_depth` | 64 | Structured payload nesting limit. |
| `json_max_nodes` | 100,000 | Structured payload node limit. |
| `store_ddl_version` | 2 | Accepted current SQLite DDL revision. |
| `store_busy_timeout_ms` | 5,000 | SQLite busy timeout. |
| `store_max_entries` | 512 | Live handle ceiling. |
| `store_max_bytes` | 268,435,456 | Distinct content bytes in the store. |
| `store_handle_ttl_seconds` | 3,600 | Handle lifetime. |
| `stats_max_records_per_page` | 8 | Operation records returned on one stats page. |
| `stats_max_pages` | 64 | Addressable stats pages. |

Store quotas reject new capture instead of evicting a live handle. TTL readability is a
SQL predicate, so an expired or revoked handle is unusable before physical sweep. The store
is local and SQLite-backed; do not place it on a network filesystem.

### Automatic exact extraction after reader unavailability

`reader.automatic_extract` defaults to `true`; `reader.fallback_max_bytes` defaults to
2048 and accepts integers from 1 through 4096. On Python/Hermes this is the secondary tier: enabled legacy compaction is tried and guarded first. `inspect.enabled: false` disables automatic
extraction as well. No opt-in is needed because this reuses the authorized snapshot,
secret guard, transactional disclosure ceilings and exact inspector already enabled by default.

The trigger is a **wholly unavailable read** after the normal retry/provider chain has
stopped: at least one physical attempt started, every planned chunk outcome failed with
`MODEL_ERROR` or `TIMEOUT`, and no response or non-availability failure was observed.
Quota, provider and network failures qualify through the existing availability boundary.
A safe model-call/request timeout qualifies; cancellation, model substitution, provenance
refusal, malformed output (including malformed-then-outage), citation-invalid output,
valid empty/weak answers and partial model answers do not. Budget exhaustion before model
availability is established does not qualify. Timeouts stop model work; the subsequent
bounded local inspection can add store/guard latency beyond the model request deadline.
Even a delivered late response is conservatively excluded.
The first attempted failing chunk in request order supplies the bounded original category
(`MODEL_ERROR` or `TIMEOUT`); no provider body or error-priority ranking is published.

Selection is always one UTF-8-safe **byte prefix of the first requested source**, independent
of the question and reader selectors, including JSON record selectors. It never ranks
semantic importance or pretends to answer the question. Other sources remain listed and
omitted. The prefix is capped by the configured bytes, request `max_answer_bytes`, deployed
answer/inspect/extraction caps, serialized headroom and remaining source/session disclosure.
It must be nonempty and strictly shorter than the source, even if a line-oversized source
fits the byte cap. Repeated automatic reads select the same prefix and charge it each time.
Use explicit inspect for a different range; automatic extraction never follows a cursor.

The wire shape remains revision **1.1**, with no new required field, status, code, schema
or store migration: `partial/EXTRACTED`, `result_kind: deterministic_extraction`,
`provenance.derived: false`, no model attribution, empty `answer`/`citations`, and fixed
`guidance` saying “Escape hatch: exact deterministic fallback extraction; not model-derived
and not an LLM summary”, plus the original category and selection rule. The existing
`extraction` block carries exact half-open byte locators, immutable snapshot identity,
charged disclosure totals and an authenticated inspect cursor. Outer coverage is always
incomplete, conservatively lists each source as `UNKNOWN_REMAINDER`, and makes no assertion
about upstream truncation. Sources and recovery actions are retained.

The session records **one read operation** with all failed physical LLM attempts/costs and
the actual serialized extraction egress (`delivery_boundary: extraction`). If a timeout
interrupts a chain before its aggregate returns, observed budget debits retain already
started physical attempts and repeated prompt costs; the debit handle is closed to
prevent later attempts. Unreported usage remains explicitly estimated/unknown. Provenance
attempt counts describe the failed read; no requested/resolved/reported model is attached
to the exact text. Direct low-level `Reader` calls return the availability error and cost;
automatic disclosure belongs to `ShuntSession.read`, which owns disclosure accounting.

Compatibility change: wholly unavailable reads formerly capable of returning partial
`NO_MATCH` now return truthful `MODEL_ERROR`/`TIMEOUT`. When neither legacy compaction nor automatic extraction can safely deliver,
exhausted disclosure, unusable/expired handles, failed storage, an empty prefix or a guard
refusal, the original bounded availability error and recovery guidance are returned.
If handle validation fails, recovery truthfully marks handles invalid and requests recapture.

### Legacy-compaction fallback for reader outcomes automatic extraction does not cover

On Python/Hermes, `reader.legacy_compaction` (default `true`) is the first bounded
fallback tier after the reader has exhausted retries and its model fallback chain.
A terminal `status: error` with `MODEL_ERROR`, `TIMEOUT`, or `CITATION_INVALID` qualifies,
including wholly unavailable readers. A reported-model mismatch and
`PROVENANCE_UNAVAILABLE` remain excluded. Malformed output published as `NO_MATCH`
is not reclassified as an availability error.

If compaction is disabled, raises, or fails the output guard, a wholly unavailable
read may use the secondary `automatic_extract` tier if enabled together with inspect.
If neither tier can safely deliver, the original bounded failure remains; raw source
is never used as a fail-open result. This precedence change is scoped to Python/Hermes;
OpenClaw selects the TypeScript legacy compactor on exhausted availability. The Python-only legacy configuration keys and additional failure triggers described here do not expand OpenClaw triggers; generic TypeScript sessions keep the prior automatic-extraction default.

Unlike automatic extraction, this is not an exact byte prefix — it is
`legacy_compact.compact_tool_result`, a ported, deterministic heuristic summary of the
**first requested source's full text**: signal lines (error/exception/failure/timeout/5xx),
head/tail sampling, repeated-line collapsing, JSON structure and secret-value redaction. It
is capped first by `reader.legacy_compaction_max_chars` (1000–60000, default 16000) and then
by the envelope's own `max_extraction_bytes` (16 KiB) at a UTF-8-safe boundary, whichever is
smaller. The wire shape is revision **1.1**: `partial/LEGACY_COMPACTED`,
`result_kind: legacy_compaction`, `provenance.derived: false`,
`provenance.label: legacy_compaction`, empty `answer`/`citations`, and a dedicated
`legacy_compaction` envelope block (`summary`, `summary_bytes`, `original_bytes`,
`hard_cap_chars`, `original_failure`) — deliberately not the `extraction` block, whose
schema description says "never a summary". Coverage preserves the reader’s processed/planned counts, upstream truncation and
omissions, and adds bounded `UNKNOWN_REMAINDER` omissions for requested sources; only the first source is covered, matching automatic extraction's own
established simplification. Compaction and secondary extraction retain the read operation’s accounting ID, failed
physical-attempt costs, original failure category and artifact handles. Both are explicitly
partial, deterministic, and not model-derived or an LLM summary.
