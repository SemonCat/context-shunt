# Configuration reference

This document names shared configuration plus explicitly identified host/runtime-only
settings. Current numeric defaults are loaded from
[`contracts/v1/limits.json`](../contracts/v1/limits.json); that JSON file is their normative
source. Every numeric `limits` override may only lower its corresponding default; a higher
value fails load with `LIMIT_MAY_ONLY_NARROW`.

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
| `reader.enforce_output_caps` | boolean | `true` | **Python/Hermes only, trusted deployment configuration.** When `false`, removes only reader answer/claim/quote/count limits and the raw-result/final `ANSWERED` envelope byte limits. It is not a request or tool argument. Input/source/spill/disclosure, citation structure and mechanical verification, secret detection, generation-token, deadline, concurrency, and fallback controls remain enforced. TypeScript/OpenClaw and the shared public envelope schema remain bounded. |
| `reader.automatic_extract` | boolean | `true` | Compatibility setting for exact extraction; does not disable or replace mandatory legacy fallback. |
| `reader.fallback_max_bytes` | integer 1–4096 | `2048` | Automatic prefix byte cap, narrowed by request, inspect, disclosure and output budgets. |
| `reader.legacy_compaction` | deprecated boolean (ignored) | n/a | Accepted only so old configuration keeps loading; remove it when convenient. Mandatory fallback cannot be disabled. |
| `reader.legacy_compaction_max_chars` | integer 1000–60000 | `16000` | Character budget handed to the compaction algorithm before the envelope's own 16 KiB byte cap is separately enforced. |
| `inspect.enabled` | boolean | `true` | Registers deterministic exact extraction. |
| `stats.enabled` | boolean | `true` | Registers read-only session accounting. |
| `tool_result_capture.enabled` | boolean | `false` | Requests the optional oversized-tool-result capture path. OpenClaw automatic capture remains retired. On Hermes, both operator attestations below are required before the hooks register. Pointer delivery additionally requires an exact match to the post-middleware provider request that visibly offered both consumers, directly or through an explicitly listed deferred catalog. Missing/truncated/partial/stale scope produces bounded no-handle legacy compaction. Unmodified Hermes 0.21.3 supplies the necessary official observer; see [`capability-matrix.md`](capability-matrix.md#tool_result_capture-on-hermes-021-what-changed-and-what-did-not). The deprecated key `suma_post_tool.enabled` remains an alias; conflicting sections are refused with `TOOL_RESULT_CAPTURE_CONFIG_CONFLICT`. |
| `tool_result_capture.host_ordering_verified_locally` | boolean | `false` | An explicit **operator attestation** that the exact-host canary verified the installed Hermes `tool_execution` middleware wraps the untruncated authorized result before context insertion. This remains an upgrade-time interlock; setting it without the linked evidence is the deployment's own risk. |
| `tool_result_capture.host_consumer_scope_verified_locally` | boolean | `false` | An explicit **operator rollout attestation** that the exact-image canary proved the installed host's post-middleware provider-request observer and request-id correlation. It remains an interlock so a code upgrade cannot activate capture merely because tools are globally configured. The flag is never inferred or set by the adapter; ordering attestation alone cannot register capture. |
| `artifact_import.enabled` | boolean | `false` | Requests the external-artifact import boundary. Supported on Hermes; the OpenClaw core has no import implementation and reports the mode unsupported with `IMPORT_UNIMPLEMENTED`. |
| `artifact_import.roots` | string array, at most 8 | `[]` | Canonical directories a producer's artifact and manifest may live under. A separate allowlist from `workspace_roots`; a root that contains `cache_dir` is refused with `CACHE_INSIDE_IMPORT_ROOT`. |
| `artifact_import.accepted_manifest_schemas` | string array | `[]` | Producer manifest schemas this deployment authorizes. A manifest declaring a schema outside this list is refused with `MANIFEST_SCHEMA_NOT_ALLOWED` even when a translation profile exists for it. |
| `limits` | integer map | values below | Narrows contract caps. Unknown, negative, invalid-zero, or wider values are refused. |

Compatibility inputs `writer.enabled: true` and an `operations` array containing
`propose_patch` are explicitly refused with `WRITER_UNSUPPORTED_CONFIGURATION`. There is no
writer tool. Both public core loaders reject unknown top-level keys, nested keys, limit
names, and invalid types, except for the explicitly Python/Hermes-only key above. The
OpenClaw loader and manifest reject `reader.enforce_output_caps` rather than ignoring it.

### Hermes reader answer-output compatibility switch

Set the switch only under
`plugins.entries.context-shunt.config.reader.enforce_output_caps`. Its default is `true`,
which preserves the existing bounded behavior. Setting it to `false` allows a mechanically
verified `ANSWERED` result to exceed the historical answer, claim text, quote, claim count,
citations-per-claim, citation count, model-result byte, and final answer-envelope byte
ceilings. It does not make model generation unbounded: `max_output_tokens_per_call` still
applies, as do every source/input and disclosure control listed above.

The checked-in shared envelope schema remains compatibility-bounded for portable/public
validation. The Python final guard derives an internal schema view with only `answer`,
`citations`, and citation `quote` schema ceilings removed, and uses it only for `ANSWERED`
delivery when this trusted switch is false. Error, pointer, extraction, stats, and mandatory
legacy-compaction envelopes remain bounded. This is why no TypeScript or OpenClaw change is
required: neither runtime consumes the Python-only setting or its internal validator.

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
   registered task also has a host-facing `timeout` default of 45 seconds, and core calls
   pass their own bounded timeout derived from `model_call_deadline_ms`.

The auxiliary override is read through Hermes' public config loader to compute the target
the core requests. Current Hermes also receives `task="context_shunt_reader"`, owns the
route selection, and returns that post-policy route as `resolved` envelope provenance.
Older facades without an explicit `task` parameter use the same config value as a guarded
provider/model override and remain `unverified`, because that result may echo the request.

### OpenClaw capture configuration

OpenClaw automatic capture is unsupported after the retired canary disproved effective
model-visible replacement. `enabled`, exact `read_only_tools` IDs (including `mcp__`
prefixes), and legacy ordering fields remain validated for configuration compatibility,
but cannot enable capture. No middleware handler is installed and raw results pass through
without pointer accounting. See [current evidence](capability-matrix.md#openclaw).

The synthetic engine classifies exact trim/lowercase identities before capture work.
Defaults are `read`, `web_fetch`, `web_search`, and `read_mcp_resource`; exact configured
IDs such as `mcp__docs__read_resource` extend them. Generated MCP resource-read names
have no automatic provenance and require operator configuration. Protected host
instructions, controls, resource/prompt catalogs and prompt retrieval override that
configuration. Repeated underscores remain intact, and payload/path text never grants
eligibility. The existing 100-entry and 1–128-character ID schema remains unchanged.
See the [synthetic classifier contract](../adapters/openclaw/README.md#synthetic-trust-boundary-classification).
These settings cannot enable live capture.

Both cores use the mandatory fallback described below.

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
| `request_deadline_ms` | 240,000 | Provisional whole-reader request cap, including queueing, retries, verification, and publication. |

The 45-second call ceiling sits inside a provisional 240-second request. A retry is permitted by count
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
Wire-size escaping can make a page smaller than the source-byte cap. Oversized lines page
as exact byte segments. Concatenate page text without inserting separators: LF bytes
between selected lines are included, but the final selected line’s terminating LF is excluded.
Nonempty byte selectors must start and end on UTF-8 boundaries (`INVALID_REQUEST` otherwise).
A budget too small for one code point is a Shunt page-capacity failure and returns bounded
`LEGACY_COMPACTED` within the requested byte budget, with incomplete coverage and charged disclosure.
Use a larger budget for exact inspection; no extraction cursor is advanced by compaction. Exhausted disclosure allowance remains
`DISCLOSURE_EXHAUSTED`.

### Store, TTL, JSON, and stats

| Key | Default | Meaning |
| --- | ---: | --- |
| `spill_ttl_seconds` | 3,600 | TTL used by the optional spill engine. |
| `json_max_depth` | 64 | Structured payload nesting limit. |
| `json_max_nodes` | 100,000 | Structured payload node limit. |
| `store_ddl_version` | 3 | Accepted current SQLite DDL revision. |
| `store_busy_timeout_ms` | 5,000 | SQLite busy timeout. |
| `store_max_entries` | 512 | Live handle ceiling. |
| `store_max_bytes` | 268,435,456 | Distinct content bytes plus one exact mirror reservation per live Python handle. |
| `store_handle_ttl_seconds` | 3,600 | Handle lifetime. |
| `stats_max_records_per_page` | 8 | Operation records returned on one stats page. |
| `stats_max_pages` | 64 | Addressable stats pages. |

Store quotas reject new capture instead of evicting a live handle. TTL readability is a
SQL predicate, so an expired or revoked handle is unusable before physical sweep. The store
is local and SQLite-backed; do not place it on a network filesystem.

### Automatic exact extraction after reader unavailability

The historical `reader.automatic_extract` and `reader.fallback_max_bytes` settings remain accepted for configuration compatibility. They do not replace, disable, or narrow mandatory legacy compaction. Explicit `context_shunt_inspect` remains the zero-model exact-range tool, with its existing selector, cursor, and disclosure contracts. The internal exact-prefix helper remains covered by regression tests for compatibility with existing integrations.

### Legacy-compaction fallback for reader outcomes automatic extraction does not cover

Context Shunt is an availability-preserving optimization layer. When Shunt owns a failure and the authorized source bytes or immutable snapshot are available, both cores automatically return the incumbent bounded deterministic compactor output. This is mandatory: `reader.legacy_compaction` and the TypeScript `legacyCompaction` option are deprecated compatibility no-ops, including when set to `false`.

Eligible failures include `MODEL_ERROR`, `TIMEOUT`, `INVALID_MODEL_OUTPUT`, `CITATION_INVALID`, capture/store failures, and unexpected safe internal errors. `LIMIT_EXCEEDED` is classified by detail: store capacity and implementation output/page capacity qualify; source/input safety caps and disclosure policy caps do not. Invalid arguments, unsupported versions/operations, unsafe/binary/secret sources, cross-session or snapshot mismatch, expired/changed sources, provenance-policy refusal, attribution mismatch, cancellation, and disclosure exhaustion remain explicit refusals. Fallback never authorizes a handle that the store cannot authorize.

The response is always `partial/LEGACY_COMPACTED`, `result_kind: legacy_compaction`, and `provenance.derived: false`, with empty `answer` and `citations`. `legacy_compaction.original_failure` retains the failure code; the bounded explicit `failure_detail` enum distinguishes verifier, argument, and capacity failures without carrying arbitrary exception text. Coverage is incomplete, question-independent, and limited to the first requested source. Python capture prepares the private exact-byte `.txt` mirror under an HMAC-derived name before reader work; after successful fallback disclosure Hermes includes its absolute `raw_artifact_path` without post-deadline raw-payload I/O. The original bytes are not inlined. Preparation failure preserves mandatory summary/handle fallback without the optional path. TypeScript/OpenClaw currently exposes the retained handles only. Capture failure before handle publication returns no source handles or path and `handles_valid: false`. The compactor retains the incumbent signal lines, head/tail samples, repetition collapsing, and JSON shaping, within character, byte, and envelope caps. `context_shunt_inspect` obeys cumulative disclosure limits; host reads of the fallback artifact path are the explicit full-source recovery route and fall outside that ledger.

Citation generation gets at most one bounded repair attempt per request, using fixed safe verifier feedback and already-authorized chunks. The same deadline, input/output budgets, provenance checks, and usage ledger apply. A repair that still fails quote-to-snapshot verification uses mandatory legacy compaction; no answer with unmatched citation quotes is published. This mechanical check does not prove the answer's prose. Genuine valid empty answers remain `NO_MATCH`.

Hermes tool schemas are derived from the canonical tool-argument contract. Malformed handles are refused with fixed diagnostics and guidance to reuse the exact `source_id`/`snapshot_id` pair from the pointer; hashes are never guessed or repaired.

Hermes authoritative skill loading is exempt from both the pre-read gate and result
capture: the host-supplied tool name, after whitespace trimming and lowercasing, must
be exactly `skill_view`. Its complete result passes unchanged to the main model before
capture session/provider construction, with no spill artifact or accounting event.
Neither an auxiliary-model summary nor deterministic compaction substitutes for skill
instructions. This trusts the host tool identity only: `/skills/`, `SKILL.md`,
`_source_path`, and claimed tool names inside output or arguments confer no exemption.
A generic `read_file` of a large `SKILL.md` remains subject to the normal gate and capture.
Other oversized results retain bounded failure handling; OpenClaw behavior is unchanged.

### Migration and rollout

Existing 1.0/1.1 requests and valid envelopes remain accepted. The `reader.legacy_compaction` boolean is accepted and type-checked as a deprecated no-op; both `true` and `false` select the mandatory invariant. No SQLite migration is required. Contract 1.2 negotiates the 240-second request ceiling and optional `legacy_compaction.raw_artifact_path`; 1.0/1.1 retain their 60-second ceiling and reject that locator. The fixed `failure_detail`, additional explicit Shunt-owned `original_failure` values, and handle-free legacy block are available in current envelopes. Older cores reject 1.2 explicitly instead of receiving a widened object mislabeled as 1.1, so deploy each adapter with its matching core and synchronized first-party contract copies. `scripts/sync-contracts --check` proves repository parity; it does not certify an installed host. Ruby owns live drift checks, session drain/restart approval, deployment, and canaries after code acceptance.
