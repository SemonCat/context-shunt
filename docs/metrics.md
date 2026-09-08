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
- Reader usage is `exact` only when every physical attempt returned complete provider
  counts. One incomplete attempt switches the request-wide reader totals to
  `bytes_div_4`; partial exact counts are not presented as an exact aggregate.
- Under request-wide estimation, `reader_input_tokens` estimates all accumulated prompt
  bytes and `reader_output_tokens` estimates all completion bytes received by the core,
  then adds any reported output-token counts from failed attempts whose completion text
  never reached the core. Those byte-visible and unseen outputs do not overlap.
- Estimated requests set `reader_cache_tokens` to `null`, because there is no byte-derived
  cache substitute. No-call fields can also be null. A null is never rendered as zero, and
  zero means a real zero.
- `usage_complete` in provenance is true only when every started attempt returned complete
  provider usage.

## Retries and fallback attempts

Every physical provider call contributes once to `attempts_started`, including transient
retries, failed fallback candidates, invalid or over-cap responses, and a late response
whose result cannot be published. `attempts_usage_complete` counts the attempts that
returned complete usable token counts. Exact reader totals aggregate every attempt only
when all attempts are complete; otherwise the request-wide estimate above accounts for
every measured prompt/completion plus non-overlapping unseen output. A successful final
candidate never erases the cost of earlier candidates.

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

`capture` is the kind an external-artifact import records, and that is where the producer
distinction lives. It reads apart from `spill` on purpose: `spill` means this core moved an
oversized result out of the context itself, and `capture` means it adopted one a producer
had already persisted. The store deliberately holds no producer identity, so which producer
it was appears in the envelope's `import_receipt`, never in a metric.

An import credits `full_payload_counterfactual` for the whole artifact, unless the manifest
declares `origin.upstream_truncated`, in which case the baseline is
`host_truncated_observed` at the size actually read. A producer that already shortened the
payload only lets us observe the shortened size; crediting the full artifact there would be
invented.

Stats records contain no source path, question, answer, quote, payload, provider error,
model name, or provider name. They are bounded non-content metadata.

## Operational metrics and visibility limits

The cores accept a metrics sink, but shipped adapters currently use the null sink unless a
host supplies one. Allowed label keys are the closed set `adapter`, `mode`, `reason`,
`status`, `code`, `form`, `decision`, `result`, and `stage`, with bounded token values.
Paths, request/source ids, model/provider names, questions, answers, quotes, and payloads
are forbidden as labels.

### The shadow A/B report

`./scripts/verify shadow all` writes `reports/shadow-ab-latest.json` (gitignored) alongside
the ordinary verify report. It compares four lanes over the fixed synthetic corpus in
[`evals/shadow/corpus.json`](../evals/shadow/corpus.json): the raw baseline, a reference
emulation of the incumbent heuristic compactor, deterministic retrieval through the import
boundary plus `inspect`, and the question-aware reader.

Three of its eight gates measure from the repository alone — main-context reduction, no
evidence regression against the raw baseline, and bounded latency for the deterministic
retrieval lane. The reduction is measured over the items the broker actually brokered, and
the refused items' raw bytes are reported separately in `refused_raw_bytes`: the over-cap
item alone is roughly 88% of the whole-corpus baseline, so crediting its counterfactual
would make the headline a saving on a payload no lane can answer from. The whole-corpus
figure is reported alongside it.

The other five report `NOT_RUN`, and the harness will not score them from a lane that cannot
answer the question they ask:

| Gate | Why it is `NOT_RUN` |
| --- | --- |
| `task_correctness`, `semantic_evidence_support` | need a scored model lane. `scripts/verify eval luna` owns the fixed corpus, thresholds and runs-per-item that a score requires, so this harness never scores the reader — with or without a bridge configured. |
| `mechanical_citation_validity` | the retrieval lane publishes exact extracts and no citations, so scoring it there is a vacuous 1.0. |
| `net_cost_reduction` | needs reader tokens *and* a pricing table. There is no versioned price table in this repository, so this stays `NOT_RUN` even with a live reader. |
| `bounded_follow_up_rate` | only an answering lane has a follow-up rate. |

The report carries no artifact content, no question text and no repository path — it is
evidence about a run, not a copy of what the run read. Two corpus items are refused by the
core outright (a credential marker in the payload, and an artifact over the source cap);
both are scored zero in a denominator that still counts them, because a refusal is a
different fact from an answer and is reported in its own field.

Session stats do not store wall-clock latency, provider request ids, price schedules, or
currency cost. `benchmark core` measures deterministic latency, envelope size, context
reduction, and memory. The provider half of `benchmark all` measures live reader latency
and tokens only when a bridge is configured; otherwise it is `NOT_RUN`. No versioned price
table exists in this repository, so documentation must not translate tokens into money.
