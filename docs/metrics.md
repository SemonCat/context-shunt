# Metrics and token accounting

context-shunt separates main-context savings from reader-model spending. It does not fold
them into an unlabeled percentage, and it does not claim currency savings or provider
latency that the current runtime surface does not record.

## Per-operation formulas

```text
main_context_tokens_saved = baseline_credit_tokens - main_model_envelope_tokens
net_tokens_saved = main_context_tokens_saved
                   - reader_input_tokens - reader_output_tokens
```

Both results are signed. An `inspect`, `stats`, or refined read normally has no new
baseline credit, so its output and reader costs can make the result negative. That is the
intended accounting rather than an error.

`reader_cache_tokens` is reported separately and is not subtracted again by the current
formula; provider cache accounting is not sufficiently uniform to reinterpret it as an
additional charge or saving.

## Baseline and egress

`raw_input_bytes` is the measured source/payload size. `raw_input_baseline_tokens` records
its token baseline when one exists. `baseline_kind` says what was measured:

- `full_payload_counterfactual`: the full snapshot was measured, and the baseline is what
  would have entered the context without the gate.
- `host_truncated_observed`: only already-truncated host input was observable, so only that
  smaller value is credited.
- `none`: this operation withheld nothing new.

`baseline_credit_tokens` is non-zero only the first time a session operation receives
credit for a snapshot. Reusing a handle does not claim the same saving again. Mixed-source
requests credit only newly credited bytes.

`main_model_envelope_bytes` is measured from the final serialized output after guarding.
`main_model_envelope_tokens` uses its stated `envelope_token_method`. The envelope carries
an opaque `accounting_id`; its database record is written after serialization so a record
does not have to measure a document that contains itself.

## Exact, estimated, and unknown

The method fields are `baseline_method`, `envelope_token_method`, and
`reader_token_method`. Contract values are `exact`, `bytes_div_4`, `unknown`, and where
applicable `not_applicable`.

- Source and envelope estimates are reproducible `ceil(UTF-8 bytes / 4)` calculations and
  are labeled `bytes_div_4`.
- Reader usage is `exact` only when the provider returned counts. If the core must estimate
  a returned attempt from prompt/completion bytes, it says `bytes_div_4`.
- When a started attempt lacks provider input/output counts, the core estimates from the
  prompt/completion bytes it measured and labels the result `bytes_div_4`. Cache usage has
  no byte-derived substitute and can remain `null`; no-call fields can also be null. A null
  is never rendered as zero, and zero means a real zero.
- `usage_complete` in provenance is true only when every started attempt returned complete
  provider usage.

## Retries and fallback attempts

Every physical provider call contributes once to `attempts_started`, including transient
retries, failed fallback candidates, invalid or over-cap responses, and a late response
whose result cannot be published. `attempts_usage_complete` counts the attempts that
returned complete usable token counts. Reader input, output, and cache totals aggregate
all attempts whose usage is available; a successful final candidate does not erase the
cost of earlier candidates.

`fallback_used` belongs to provenance and reports whether an availability fallback
produced the published result. It is not a statement about quality.

## `context_shunt_stats` fields

The stats result has `scope: "session"`, a `totals` object, and one page of `records`.
Totals contain:

| Field | Meaning |
| --- | --- |
| `operations` | Accounting records in this session scope. |
| `raw_input_bytes` | Sum of measured raw baselines. |
| `baseline_credit_tokens` | One-time baseline credits. |
| `main_model_envelope_tokens` | Tokens estimated for delivered boundaries. |
| `reader_input_tokens` | Aggregated reader input, or `null`. |
| `reader_output_tokens` | Aggregated reader output, or `null`. |
| `reader_cache_tokens` | Aggregated provider cache tokens, or `null`. |
| `main_context_tokens_saved` | Signed main-context total. |
| `net_tokens_saved` | Signed total after reader input/output. |
| `attempts_started` | Physical model attempts. |
| `attempts_usage_complete` | Attempts with complete usage. |
| `disclosed_bytes` | Exact bytes returned by inspect in this session. |

Each operation record adds `operation_id`, `kind`, `status`, `code`,
`raw_input_baseline_tokens`, `baseline_kind`, the three method fields,
`main_model_envelope_bytes`, `delivery_boundary`, and its per-operation versions of the
token/attempt fields. `kind` is one of `gate_block`, `capture`, `read`, `refined_read`,
`inspect`, `stats`, or `spill`. A page contains at most eight records and only the caller's
current session; the tool accepts no foreign session id or arbitrary label.

Stats records contain no source path, question, answer, quote, payload, provider error,
model name, or provider name. They are bounded non-content metadata.

## Operational metrics and visibility limits

The cores accept a metrics sink, but shipped adapters currently use the null sink unless a
host supplies one. Allowed label keys are the closed set `adapter`, `mode`, `reason`,
`status`, `code`, `form`, `decision`, `result`, and `stage`, with bounded token values.
Paths, request/source ids, model/provider names, questions, answers, quotes, and payloads
are forbidden as labels.

Session stats do not store wall-clock latency, provider request ids, price schedules, or
currency cost. `benchmark core` measures deterministic latency, envelope size, context
reduction, and memory. The provider half of `benchmark all` measures live reader latency
and tokens only when a bridge is configured; otherwise it is `NOT_RUN`. No versioned price
table exists in this repository, so documentation must not translate tokens into money.
