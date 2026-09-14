# Source-backed current-payload trace — 2026-09-14 audit

This document is the other half of Milestone 1 alongside
[`docs/regression-corpus-manifest.md`](regression-corpus-manifest.md): before writing any
fix, the actual constructed provider payload for each of the five sessions was inspected
directly from read-only production evidence (`/opt/hermes-data/state.db`,
`/opt/hermes-data/context-shunt-cache/store.sqlite3`, `/opt/hermes-data/logs/agent.log`,
all opened `mode=ro`/`immutable=1`), rather than assumed from the implementation's own
description of itself. It records what the trace showed already worked, and where it
diverged from that, per session — the "document what already works versus the real cause
of over-reading" requirement.

Identifiers below (message/source/scope IDs, byte counts, timings) are opaque handles and
measured facts read from production, not reconstructions — they carry no content or PII
meaning and are the same identifiers already used in the regression-corpus manifest and
commit messages. No message body, log line, or row content is reproduced here.

Audit window: 2026-09-14 05:49:46–09:30:11 Asia/Taipei
(2026-09-13T21:49:46Z–2026-09-14T01:30:11Z), excluding release canaries. Some blobs
referenced by older handles may have since expired under TTL; where that applies below it
is stated rather than a raw replay being claimed.

## What already worked, observed directly (all five sessions)

Traced before assuming otherwise, and confirmed true in the actual payload, not merely in
the implementation's self-description:

- **Oversized tool results were actually spilled, not forwarded raw.** Every oversized
  source in all five sessions (skill reads, Loki payloads, Slack history, GBrain search
  results, bkt-rules) produced a bounded pointer envelope reaching the main model, not the
  raw bytes. This is the one invariant the whole system exists to guarantee, and it held in
  every traced case.
- **The bounded-envelope cap held.** No envelope observed in the traced window exceeded its
  configured size, regardless of how large the underlying spilled source was (up to
  313,710 bytes in session 2).
- **Deterministic paths made zero model calls when they answered fully.** Session 1's
  final correct answer, and every partial/complete deterministic `inspect` call across all
  five sessions, cost no reader/LLM call by itself — the reader was only invoked where the
  query genuinely needed it (structured extraction beyond a literal search/line/byte
  selector).
- **The main-context ledger's arithmetic itself was internally consistent** — `main_saved`,
  `reader`, and `net` figures summed correctly from the per-operation records that produced
  them. Sessions 3, 4 and 5 below are about what those figures *mean*, not arithmetic
  errors in computing them.

None of the five findings below are about the spill/envelope mechanism failing; all five
are about a specific selector, cursor, or accounting semantic drifting from what a caller
reading the ledger or the selector's own field names would reasonably conclude.

## Per-session trace

### Session 1 — `20260913_220014_87b48d48`

A 56,887-byte skill read spilled to a pointer. The follow-up `inspect` `search` call asked
for `merchant_auto_suspend|third monday|third Monday|auto suspend` as a single `pattern`
with `max_matches=30`, seeking a merchant-auto-suspend/third-Monday rule. **Traced result:**
`NO_MATCH`, zero model calls — cheap, but wrong: the selector's `_search_selector_to_lines`
implementation matched `pattern` as one literal substring, so the `|`-joined text never
matched anything at all, regardless of whether any of the four alternatives was present.
The caller then fell back to a path-based read, correctly blocked
(`UNSAFE_SOURCE`/`OUTSIDE_WORKSPACE_ROOT`) since the target lived outside the workspace
root, and eventually reached a correct answer via `search_files`/terminal/`read_file` with
an explicit offset+limit. **Root cause:** a search-contract gap (literal-only matching
advertised implicitly by a selector whose only visible field was named `pattern`, with no
signal that `|` had no special meaning), not a security-boundary problem — the safe-routing
block worked exactly as intended and is not touched by this audit's fix. Scope
`scp_6c06f67d4e8b6a7c5f872e265df969e7`; ledger `main_saved`/`net` figure `13712` correctly
excludes the blocked 663-byte response and the rest of the recovery path. Closed by adding
`patterns` (schema 1.3) as an additive literal-OR alternative — see
[`regression-corpus-manifest.md`](regression-corpus-manifest.md#coverage-table) row 1.

### Session 2 — `20260913_220019_2f41d827`

The same 56,887-byte skill plus two Loki-shaped payloads (313,710 and 112,106 bytes) all
spilled. Three deterministic `inspect` calls were partial, due to an oversized/minified
single line in one source. One reader `read` call took 121.08s and returned partial
coverage (5/7 chunks, `BUDGET_EXCEEDED` and `TIMEOUT`) against a query asking for structured
errors/counts/timestamps while excluding per-bill data. Sources `src_c5ad209900a6f0ac` and
`src_d4f30ef548d8edcd`; scope `scp_5f5e77766b26a29b804d4a3cb138ac30`; ledger
`main_saved=101269`, `reader=60772`, `net=40497` — the trace confirms this net figure is
itself labeled estimated/incomplete usage, not a silent full accounting. **What the trace
showed already working:** the partial 5/7-chunk answer was never presented as a confirmed
whole-source result — the reader path's existing coverage/provenance discipline held here.
**Root cause actually found:** not the timeout/partial-coverage handling itself, but that
the reader's own attempt-level usage accounting collapsed "5 of 7 attempts reported usage"
into a single `usage_complete` boolean, discarding the more precise count a caller would
need to judge how much of the ledger figure to trust. Closed via `97e5910`'s
`provenance.attempts_usage_complete` — see manifest row 2.

### Session 3 — `20260913_220020_6c5f7a47`

A 45,050-byte skill source's `read` took 30.39s and resolved to `LEGACY_COMPACTED` with
`NO_VALID_EVIDENCE` after 3 attempts (21,789 in / 3,993 out tokens) — the mandatory
fallback engaged correctly when the reader could not produce citable evidence, and the
trace confirms no answer with unmatched citations was ever published, matching
[`docs/acceptance.md`](acceptance.md#mandatory-fallback-acceptance)'s contract. Two further
Loki sources (85,677 and 102,744 bytes) were read: one `NO_MATCH` in 3 chunks (5.43s), one
`ANSWERED` complete in 4 chunks (66.55s). A follow-up `inspect` call was partial. Then the
*same* 102,744-byte source was re-read with a question asking for an exact error
count/distinct trace IDs — 65.91s, partial 2/4 chunks, `TIMEOUT`. Scope
`scp_7d192066f5f8a36c88d5e4779812b56b`; ledger `main_saved=44910`, `reader=116237`,
`net=-71327` — the trace shows this negative net is real and correctly computed, not a
labeling artifact. **What already worked:** the reader path already refused to report the
2/4-chunk partial result as a confirmed exact count — the falsehood this session's stated
concern actually points at was never present in the *reader* path. **Root cause actually
found:** the product objective is explicitly broader than "the reader must not lie about a
partial scan" — "exact counts ... should use deterministic processing where feasible," and
`inspect`'s `search` selector, the deterministic alternative for exactly this literal-count
question, had never been audited for the same honesty property. Tracing it directly showed
it *would* have made the identical false-completeness claim the reader path already avoids
(`complete: true` on a `max_matches`-capped page with most of the source unscanned), and a
second, subtler recurrence of the same falsehood one page later on cursor resumption. Both
closed by `cb298d6` and `ec33776` — see manifest row 3, and
[`docs/limitations.md`](limitations.md) for the one known narrow residual left in this area.

### Session 4 — `cron_2cf04e39ace6_20260914_061013`

Two GBrain searches (34,896 and 17,560 bytes) produced pointers; the caller reissued
smaller, targeted searches (10,781 and 9,131 wire bytes) against the same sources, and the
reader was never invoked for this scope. Ledger `net=12516`. **Trace finding:** the ledger
figure, read at face value, omits the requery recovery entirely — it reads as a clean
saving on the original two spills, when the actual sequence included a caller reissuing
work against the same source, which the ledger has no way to represent because a
passthrough result below `max_tool_result_bytes` produces no envelope and no operation
record for `post_tool_result` to see. **What already worked:** the unread-pointer credit
itself (`full_payload_counterfactual`) is honest for the pointer it describes, verified in
isolation. **Root cause:** the requery-correlation gap is structural — it needs Hermes' own
tool-call event stream, invisible from inside the plugin boundary — so it is written up as
[`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md)
rather than patched. Closed (the honest-credit half) by `d02788d` — manifest row 4.

### Session 5 — `20260914_010010_6cbb09e5`

A 17,601-byte Slack history spilled to a pointer, then was read back in full via two
`inspect` calls (byte ranges `0:10000` and `10000:17601`) — a genuine full re-read, which
the trace shows *increases* the estimated main-context cost by 973 tokens once envelope
overhead is counted, not a saving. A separate 18,347-byte bkt-rules source also spilled to
a pointer and was never read back — an honest, uncontested one-time credit. Scope
`scp_f3696d76d007b9847949d529bbaf0860`; the combined scope ledger reads `net=3312` — a
small *positive* figure. **Trace finding:** read at face value, that positive net implies
"this scope saved tokens," when the trace shows it is actually a real loss on the Slack
history, partly offset by an unrelated credit on a different source that happened to share
the same scope. **What already worked:** each source's own accounting, taken in isolation,
was individually correct — the Slack-history full-read cost and the bkt-rules one-time
credit were each computed honestly on their own. **Root cause:** the composite scope total
had no test proving it stayed honestly netted (i.e., provably smaller than the uncontested
credit alone) rather than merely happening to land positive by coincidence of scope
grouping. Closed by `cd6f8d9` (single-source verification) and `cb298d6` (composite test) —
manifest row 5. Two further, unrelated gaps noticed while investigating this session
(cross-request reader-answer reuse, and a structured grouping/cardinality selector) are
now implemented in both owned cores and documented in
[`docs/proposal-deterministic-aggregation-and-reuse.md`](proposal-deterministic-aggregation-and-reuse.md),
with bounded cross-port regression coverage.

## Whole-window figures, traced and labeled

The main ledger's window total (excluding canary) reads `main_saved=175719`,
`reader=177009`, `net=-1290` — traced directly from the same accounting records as the
per-session figures above, all computed as raw/envelope byte-count estimates (`bytes/4`),
explicitly **not** tokenizer-exact or billed-usage figures, and benchmarked against a full
raw baseline, not the old compactor. Separately, the provider's own completion log for
06:00–06:06 TPE recorded 18 Luna completions (164,179 total tokens) plus 3 Sol completions
(32,002 tokens) — 196,181 actually-reported tokens — which is larger than the ledger's own
number, because some calls finish after their tool deadline and the ledger cannot credit
usage it had not yet received at ledger-close time; the trace confirms this is an
understatement direction (ledger `net` is a floor, not an exact total), never invented as a
precise per-session attribution since `session_model_usage` carries no per-session reader
usage rows for this window. Recorded tool-elapsed sums across the five sessions:
`#1 0.03s`, `#2 121.15s`, `#3 168.30s`, `#4` unrecorded in this trace, `#5 0.03s` — these
are traced tool elapsed-time sums, not a controlled A/B wall-time comparison; see the
[three-lane comparison](audit-report.md#three-lane-comparison-synthetic-corpus-local-read-only)
in the main report for the controlled five-workflow replay this document does not attempt
to substitute for.

## Why this document exists separately from the manifest and the report

[`docs/regression-corpus-manifest.md`](regression-corpus-manifest.md) maps each finding to
its synthetic fixture and closing commit — it is about what was *tested*.
[`docs/audit-report.md`](audit-report.md) is the acceptance-facing summary — what was
*measured and decided*. This document is the evidentiary link between the two: the actual
constructed payload each finding was read from, traced directly against read-only
production state before any test was written, so that the shape each synthetic fixture
reproduces (sizes, chunk counts, timing, boundary conditions) is traceable back to a real
observation rather than an assumption about how the implementation was believed to behave.
