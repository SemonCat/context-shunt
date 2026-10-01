# Runtime attempt accounting (Python/Hermes 1.1.1)

Every reader outcome carries its individual call observations into the operation's
SQLite transaction, including retries, availability fallbacks, citation repair and
late results received before the reader finalizes the operation. A response that arrives
after finalization remains unknown; no background writer revises an already-published
operation. Composite providers must supply per-call observations: an aggregate usage
report is never assigned to each constituent call.

The optional `reader_attempt_observations` table stores only numeric usage, a bounded
attribution enum, cache-inclusion semantics and SHA-256 identity digests. A digest covers
the exact JSON pair `[provider,model]` (compact separators, default JSON escaping).
Requested, host-resolved and provider-reported identities remain separate. Digests are
an equality mechanism, not encryption or authentication. Bodies, error text, questions,
source paths and raw provider/model strings are never stored in this extension.

Storage keeps at most 256 observations per operation and 1,024 recent operations with
reader attempts across the store. Original operation totals remain intact. Missing,
pre-upgrade, pruned and excess observations count as `observations_unavailable`; they
are never interpreted as zero usage. The additive table can be ignored by old Python
and TypeScript readers; the TypeScript writer does not yet produce these observations.

## Configured prices

Trusted Python plugin configuration accepts `economics_rates`, a list of at most 32
entries. There are no built-in prices. Each entry requires `provider`, `model`, `source`
and `as_of` (bounded strings). The optional fields below contain **USD per million
tokens**, preferably as decimal strings; absent rates stay unknown:

- `input_tokens`: uncached input
- `output_tokens`: generated output
- `cache_tokens`: cache reads
- `cache_write_5m_tokens`: five-minute cache writes
- `cache_write_1h_tokens`: one-hour cache writes

Entries must be unique by exact provider/model pair. Negative, nonfinite and unbounded
values are rejected. The operator must supply rates applicable to the selected deployment,
route, tier, context bracket and reporting window, including applicable multipliers.
There is no automatic price discovery or bracket/tier inference. `source` and `as_of`
identify that configured assumption; use non-sensitive provenance identifiers.

Usage comes from the bridge, not the rate configuration. Bridges may report the five
counts plus `input_includes_cache`: true means all reported cache buckets are subsets
of input, false means they are disjoint additional tokens, and absent means unknown.
An inapplicable bucket must be explicitly reported as zero to price a complete attempt.
Inconsistent counts, missing buckets, estimated usage or unknown inclusion with positive
cache usage prevent a complete attempt price. Requested identity alone never qualifies;
pricing requires provider-confirmed or host-resolved attribution.

Hermes' legacy facade treats missing and zero usage alike. The adapter preserves that
ambiguity and does not infer inclusion from a model name.

Hermes' `PluginLlmUsage` (checked against the v2026.9.24 host source) exposes
`input_tokens`, `output_tokens`, `total_tokens`, `cache_read_tokens`, one TTL-less
`cache_write_tokens` and `cost_usd`. It has no five-minute/one-hour write buckets and no
cache-inclusion flag. Earlier adapters read only the bucketed names, so a positive host
cache-write count was silently dropped. The adapter now forwards a positive
`cache_write_tokens` as `cache_write_unclassified_tokens`: persisted per attempt, never
assigned to a TTL bucket, and never priced. An attempt carrying it reports
`cache_write_ttl_unknown` instead of a guessed price.

Concrete unresolved boundary: under that host API, a Hermes attempt's full configured
price stays unknown. The bucketed write counts are always unreported (`usage_unknown`),
zeros cannot be told apart from absence, and `input_includes_cache` is never stated.
Closing that gap needs a host usage API that reports TTL buckets, explicit zeros and
inclusion semantics. This repository does not patch Hermes, reinterpret absent fields
as zero, or use `cost_usd` as an invoice.

## Stats

`context_shunt_stats` adds optional `stats.economics` for the **displayed page only**.
Existing token totals and operation records retain their meaning. The block reports
attempts started/priced, a known subset under the supplied rates, a total only when every
attempt is priced, unknown-reason counts, and the applied rates with provenance. Decimal
amounts are strings. No attempts is a genuine zero; no priced attempts among started
calls is null. The existing envelope byte ceiling still applies.

These are configured-rate costs, not verified invoices or marginal subscription cash
expense. `net_savings_usd` is always null. Main-context suppression remains a separate
token estimate, not avoided billable usage. The older aggregate scenario helpers in
`economics.py` are not used by this runtime path and cannot reconstruct historical
per-attempt cache/model attribution.

## Upgrade and rollback

Opening an existing store creates only the optional table; it does not rewrite historical
accounting rows. Old binaries can reopen the database and ignore the extension. An old
writer replacing an operation can discard its extension row through the foreign key,
which becomes explicitly unknown. Do not restore an old database over new traffic as a
routine binary rollback. Activation requires a separately approved, hash-checked wheel
and adapter deployment; the repository's retained 1.1.0 offline bootstrap bundle is a
separate historical artifact and is not updated by this release.
