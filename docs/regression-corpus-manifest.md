# Regression corpus manifest — 2026-09-14 audit

Provenance mapping for the five closed production sessions this audit covers. This file
is the public, committed side of that mapping: which synthetic/committed fixture and test
reproduces which finding, and which commit closed it. It intentionally does **not** carry
any production content — no merchant/user data, no log lines, no request/response bodies.
Session, cache-scope and account identifiers below are opaque handles with no content or
PII meaning (matching the convention already used throughout this repository's committed
tests and commit messages); they name *which* incident a fixture reproduces, not what was
in it.

The private provenance record — which real log lines and DB rows each synthetic fixture
was shaped from — is retained outside this repository, per the standing constraint that
only synthetic or irreversibly scrubbed examples may be committed.

Audit window: 2026-09-14 05:49:46–09:30:11 Asia/Taipei, excluding release canaries.
Source data (read-only, never committed): `/opt/hermes-data/state.db`,
`/opt/hermes-data/context-shunt-cache/store.sqlite3`, `/opt/hermes-data/logs/agent.log`.

## Coverage table

| # | Session | Finding | Closing commit(s) | Python fixture/test | TypeScript fixture/test |
|---|---|---|---|---|---|
| 1 | `20260913_220014_87b48d48` | `inspect` `search` selector's `pattern` matches one literal substring, not an OR of `\|`-joined alternatives — a caller asking for `a\|b\|c` as a single pattern silently got `NO_MATCH` instead of a hit on any alternative. Fixed additively: new `patterns` (schema 1.3) literal-OR list alongside the unchanged `pattern` field. | `97e5910` | `packages/core-py/tests/test_gate_reader_search_patterns.py` | `packages/core-ts/test/reader-and-spill.test.ts` (`"never splits a `\|`-joined pattern into alternatives"`, `"ORs several literals via `patterns` and finds the real hit"`) |
| 2 | `20260913_220019_2f41d827` | Late/unreported reader usage (`acc_d179a538c9a51b7a`: 7 attempts, 5 with reported usage) was invisible on the envelope — only a collapsed `provenance.usage_complete` boolean existed. Fixed additively: `provenance.attempts_usage_complete` exposes the real per-attempt count. | `97e5910`, `cd6f8d9` | `packages/core-py/tests/test_gate_reader_late_usage.py` | `packages/core-ts/test/reader-late-usage.test.ts` |
| 3 | `20260913_220020_6c5f7a47` | (a) Same late-usage gap, second incident (`acc_968992724b9a99d6`: 4 attempts, 2 with usage). (b) `inspect` `search` capped by `max_matches` reported `complete: true` with no continuation cursor even when most of the source was never scanned — indistinguishable from a genuine exact count. (c) Resuming a cursor whose own `max_matches` was already fully spent silently re-introduced the same false-completeness bug one page later. | `97e5910` (a), `cb298d6` (b), `ec33776` (c) | `test_gate_reader_late_usage.py`; `test_gate_search_cap_honesty.py` (all three tests) | `reader-late-usage.test.ts`; `search-cap-honesty.test.ts` (all four tests) |
| 4 | `cron_2cf04e39ace6_20260914_061013` | A tool-result pointer spilled but never read by the reader was credited as a one-time `full_payload_counterfactual` saving — honest for that pointer alone, but the specific requery that replaced it (Hermes' own retry after an abandoned call) cannot be correlated to it from inside `post_tool_result`, since a passthrough below `max_tool_result_bytes` produces no envelope and no operation record. Verified the existing credit is honest as scoped; the requery-correlation gap is out of scope for an in-repo fix and is written up as a host-side proposal instead. | `d02788d` | `test_gate_session4_unread_pointer_requery.py` | `session4-unread-pointer-requery.test.ts` |
| 5 | `20260914_010010_6cbb09e5` | A scope with two sources sharing one net accounting figure — a fully re-read Slack history (a real, negative net contribution) plus a never-reread bkt-rules spill (an uncontested one-time credit) — summed to a small *positive* total that reads as "this scope saved tokens," when it is actually a real loss partly offset by an unrelated credit. Verified the single-source case is honest in isolation, then added a composite test proving the scope total is provably smaller than the uncontested credit alone (i.e. already netted against the loss, not hiding it). | `cd6f8d9` (single-source), `cb298d6` (composite) | `test_gate_reader_payload_scope.py`, `test_gate_session5_full_coverage_inspect.py`, `test_gate_session5_composite_scope_masking.py` | `reader-payload-scope.test.ts`, `session5-full-coverage-inspect.test.ts`, `session5-composite-scope-masking.test.ts` |

## Fixture shape discipline

Every fixture above is generated data shaped like the production case (line counts, byte
sizes, JSON nesting, needle density, chunk/attempt counts, timing order) rather than a
copy of real content — e.g. `_loki_shaped_source()` in `test_gate_search_cap_honesty.py`
builds 3,000 synthetic lines with a literal `ERROR` needle recurring on a fixed period,
matching session 3's Loki-shaped payload's size and match-density class without
reproducing any real log line. Each test's own docstring states which production incident
it reproduces and why the synthetic shape is representative; that docstring is the
audit-facing documentation, this table is the index over it.

## Red-capable verification

Each fix above was confirmed red-capable before being counted closed: the fix files were
stashed out and the new test(s) were confirmed to fail against the pre-fix tree, then pass
again after unstashing. This was done per-commit at the time (recorded in each commit
message) rather than re-verified as one batch here, since stashing out an already-merged
five-commit chain would require re-deriving which lines belong to which finding — the
per-commit record is the authoritative one.

## Continuation features and remaining host boundary

Ruby rejected treating the two owned-reader gaps as proposal-only completion. They are now
implemented and exercised against the same sanitized production-derived shapes:

- Scoped exact-query answer reuse: `packages/core-py/tests/test_gate_reader_answer_cache.py`
  and `packages/core-ts/test/reader-answer-cache.test.ts` cover authorization before hit,
  snapshot/query/selector/budget/model-contract isolation, complete-result-only storage,
  per-call usage provenance, and bounded LRU eviction.
- Bounded structured count/distinct/grouping, including Loki-style embedded minified JSON:
  `packages/core-py/tests/test_gate_aggregate.py` and
  `packages/core-ts/test/aggregate.test.ts` cover exact results and whole-operation refusal
  when the record scan cap cannot be honored. Both ports also spill oversized serialized
  JSON through the real `post_tool_result` route (which stores a string capture as
  `text/plain`) and aggregate the safely bounded parsed snapshot, closing a gap discovered
  by the execution benchmark.
- The same generated bytes and workflow operations are executed across incumbent/PRE/NEW
  by [`evals/intent-reader-audit/run.py`](../evals/intent-reader-audit/run.py). PRE imports
  the Python core from an isolated `git archive` of `1686db6`; NEW uses the working-tree
  `ShuntSession`/reader/inspect/cache, and legacy calls the owned incumbent compactor port.
  The corpus declares only content and independently defined expected outputs—never lane
  attempts, correctness, result size, usage, or time. The instrumented provider records
  actual call payloads and returned usage. The machine-readable result is
  [`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json).

One gap remains outside an in-repo core fix and is a separately reviewable host proposal:

- [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md) —
  session 4's requery-correlation gap.

[`docs/proposal-deterministic-aggregation-and-reuse.md`](proposal-deterministic-aggregation-and-reuse.md)
is retained as the implementation/design record, not as an unimplemented proposal.

## What this manifest is not

This is not a claim that every possible inefficiency in the five sessions has been found;
it is the closed set of findings this audit pursued to a tested fix or a written proposal.
See [`audit-report.md`](audit-report.md) for the full account, the five-workflow three-lane comparison,
and residual blockers, and [`current-payload-trace.md`](current-payload-trace.md) for the
source-backed trace of each session's actual constructed payload that this manifest's
fixtures were shaped from.
