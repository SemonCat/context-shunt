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
| `inspect.enabled` | boolean | `true` | Registers deterministic exact extraction. |
| `stats.enabled` | boolean | `true` | Registers read-only session accounting. |
| `suma_post_tool.enabled` | boolean | `false` | Requests the optional oversized post-tool path. Both current adapters report it unsupported, so it is not activated. |
| `limits` | integer map | values below | Narrows contract caps. Unknown, negative, invalid-zero, or wider values are refused. |

Compatibility inputs `writer.enabled: true` and an `operations` array containing
`propose_patch` are explicitly refused with `WRITER_UNSUPPORTED_CONFIGURATION`. There is no
writer tool. Both public core loaders reject unknown top-level keys, nested keys, limit
names, and invalid types. The OpenClaw manifest adds a separate host validation boundary.

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
