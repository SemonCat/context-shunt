# Intent-driven reader audit — report for Ruby

**Report path:** `docs/audit-report.md` on branch `task/intent-driven-reader-audit`.
Use the branch HEAD from `git rev-parse HEAD` when Ruby reviews it; this avoids a stale
self-referential hash after the report itself is committed.

**Scope of this document:** what was audited, what was found and fixed, what was measured,
what remains open, and where to look for each supporting artifact. It does not authorize
any production change — see [Residual blockers](#residual-blockers) and
[`docs/acceptance.md`](acceptance.md), which remains the acceptance-gate source of truth.

## Constraints this work stayed inside (unviolated throughout)

- All Hermes/AWS access was read-only, via `hermes-ssh`, for inspection only. No deploy,
  restart, drain, plugin/config change, credential change, production model request,
  Slack message, or database mutation was made at any point in this audit.
- The only real-provider calls were the explicitly authorized, opt-in Luna evaluation over
  the five committed synthetic fixtures. Existing OpenClaw runtime auth was resolved in
  memory; no secret value or raw production artifact was inspected, printed, or retained.
- The 09:58 rollback (Shunt disabled, `oversize-tool-result-compactor` 0.3.0 enabled,
  gateway-default PID 10880, existing container unchanged) was left exactly as found.
  **Shunt was never re-enabled in production.**
- No dependency or vendored code was changed; all work is owned-repo source, tests,
  schemas, and docs.
- Every committed test and doc uses synthetic or shape-matched fixtures only. Opaque
  session/account/scope identifiers (e.g. `20260913_220020_6c5f7a47`,
  `acc_968992724b9a99d6`) are referenced directly, matching prior committed work, because
  they carry no content or PII meaning — they name *which* incident, not what was in it.
  No real merchant/user data, log line, or credential is committed anywhere in this branch.
- The private provenance mapping (which real rows/log lines each fixture was shaped from)
  is retained outside this repository; only the synthetic side is published — see
  [`docs/regression-corpus-manifest.md`](regression-corpus-manifest.md).
- All SQLite access used `mode=ro`/`immutable=1` throughout the read-only investigation
  phase (per the standing constraint); no store file was opened for write.

## Audit window and source data

2026-09-14 05:49:46–09:30:11 Asia/Taipei, excluding release canaries. Read-only source
data: `/opt/hermes-data/state.db`, `/opt/hermes-data/context-shunt-cache/store.sqlite3`,
`/opt/hermes-data/logs/agent.log`.

## Five sessions audited, all closed

| # | Session | Status |
|---|---|---|
| 1 | `20260913_220014_87b48d48` | Closed — `97e5910` |
| 2 | `20260913_220019_2f41d827` | Closed — `97e5910` |
| 3 | `20260913_220020_6c5f7a47` | Closed — `cb298d6`, `ec33776` |
| 4 | `cron_2cf04e39ace6_20260914_061013` | Closed — `d02788d` |
| 5 | `20260914_010010_6cbb09e5` | Closed — `cd6f8d9`, `cb298d6` |

Full finding-by-finding detail, the exact fixture/test that reproduces each one, and the
red-capable verification discipline used are in
[`docs/regression-corpus-manifest.md`](regression-corpus-manifest.md) — that file is the
sanitized regression-corpus manifest this audit's Milestone-1 requirement calls for.

### Summary of what each session found

1. **Literal-OR search bug** — `inspect`'s `search` selector treated a `\|`-joined
   pattern as one literal string instead of an OR of alternatives, silently returning
   `NO_MATCH` for a caller who reasonably expected any-of semantics. Fixed additively via a
   new `patterns` field (schema 1.3); `pattern`'s exact prior meaning is unchanged.
2. **Late reader usage was invisible** — a `read` envelope only ever exposed a collapsed
   `provenance.usage_complete` boolean, unable to distinguish "one late attempt of seven"
   from "almost nothing measured." Fixed additively via
   `provenance.attempts_usage_complete`.
3. **Late usage, search-cap honesty, exact aggregation, and scoped reuse** —
   a `search` page that stopped only because it hit its own `max_matches` cap reported
   `complete: true` with no continuation cursor even when most of the source went unscanned
   — indistinguishable from a genuine exact count. Fixed by scoping `complete` to what it
   can actually mean. A later P0–P2 review caught that the first fix's cursor carried a
   cumulative match count and therefore could not continue after reaching the per-request
   cap. In schema 1.3, `max_matches` is now a per-page cap: the authenticated cursor advances
   the source position and resets the page allowance, allowing bounded completion even
   beyond 200 total hits. Schema 1.1/1.2 preserve their prior cumulative request cap. Ruby
   rejected leaving the remaining owned-repo gaps as proposals, so both
   ports now also provide bounded JSON count/distinct/grouping and a 32-entry/256-KiB
   exact-query answer LRU. Cache lookup re-authorizes handles first and never stores
   partial results.
4. **Unread-pointer credit is honest; the requery gap is a host-side proposal** — verified
   that a spilled-but-never-read pointer's one-time `full_payload_counterfactual` credit is
   honest as scoped. The specific gap that motivated the question — correlating an
   abandoned pointer with the recovery call that replaced it — needs Hermes' own tool-call
   event stream, invisible from inside `post_tool_result`; that is written up as
   [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md),
   not installed.
5. **Composite scope masking** — a scope combining a fully re-read source (a real loss)
   with an unrelated never-reread source (an honest one-time credit) summed to a small
   *positive* net figure that reads as "this scope saved tokens," obscuring the real loss
   underneath an unrelated credit. Fixed by proving (and locking with a test) that the
   composite total is provably smaller than the uncontested credit alone — i.e. already
   netted against the loss, not hiding it. The new structured selector gives this class of
   JSON workflow a bounded selected-result route instead of requiring full two-page
   readback.

## Bounded implementation: what changed and what didn't

The findings were closed as owned-repo, additive, backward-compatible changes:
new optional envelope fields (`provenance.attempts_usage_complete`), a new optional
selector field (`patterns`), corrected `complete`/page-cursor semantics for a pre-existing
selector, scoped
exact-query answer reuse, and schema-1.3 `inspect.selector.kind="aggregate"`. Aggregate
supports bounded count/distinct/grouping over validated JSON arrays, optional array
expansion and embedded-JSON parsing for Loki/minified shapes, and no regex/expression
surface. It refuses a scan that exceeds the caller record budget rather than returning a
partial value that could be mistaken for exact.

No sprawling host refactor was attempted. Only session-4 recovery-call correlation remains
a host-side proposal; the reuse and aggregation features are implemented in both owned
language cores and are not installed on the live host.

The first explicit P0–P2 review reproduced four additional boundary defects and they were corrected
in both ports: fallback-produced answers are not admitted to the cache; a cache hit is
rechecked against the serialized envelope cap after request/accounting metadata changes;
outer elements (including empty Loki expansions) consume the aggregate scan budget; and
`patterns` is rejected on requests declaring a pre-1.3 schema. The same review's response-
version finding resulted in a real 1.3 emitted envelope contract instead of placing new
fields under the closed 1.2 label.

A second explicit P0–P2 review found two more valid boundary defects, also corrected in both
ports: aggregate group/distinct targets that resolve to objects or arrays now fail with
`INVALID_REQUEST`/`BAD_SELECTOR` instead of being conflated with a missing field, and search
cursors under schema 1.3 treat `max_matches` as a fresh bounded per-page allowance so every
advertised cursor can make progress. Its third candidate—that PRE was sent an unsupported 1.3 request—
was rejected after checking both the archived contract and actual PRE execution: commit
`1686db6` emits envelopes at 1.2 but its `SUPPORTED_REQUEST_VERSIONS` is
`[1.0, 1.1, 1.2, 1.3]`, and all five PRE rows execute with that fact captured in the JSON
artifact.

The third explicit P0–P2 review found two further valid boundedness/determinism defects.
Both are fixed and covered in both ports: the cache now retains a fixed SHA-256 query-key
digest and includes those digest bytes in its 256-KiB serialized-material eviction total,
and aggregate distinct/group keys sort by canonical JSON's UTF-8 bytes rather than relying
on Python code-point versus JavaScript UTF-16 ordering. The cross-runtime regression uses
`U+E000` and `U+1F600`, whose relative order exposed the mismatch.

The first post-fix verification then found one more valid P2: unrestricted JSON numbers can
lose integer precision in JavaScript and silently merge distinct/group keys. A later pass
caught that an apparently integral runtime value may itself come from a rounded fractional
lexeme. Aggregate filter/distinct/group numeric identities are therefore rejected in the
request contract and both runtimes; callers encode the original numeric lexeme as a string.
This is the only provably exact boundary once host parsing may already have occurred.

The next post-fix verification found a final boundedness P2: `filter.equals` accepted an
unbounded string and reserialized it per scanned record. The request contract now caps it
at 512 characters, both runtimes enforce the existing 512-byte canonical scalar ceiling,
and the expected comparison key is computed once before the bounded scan.

Closed error-code lists remain synchronized in both language cores and all contract copies.

## Five-workflow execution evidence

The comparison uses the same five production-derived workflow shapes in
all three lanes, generated by `evals/intent-reader-audit/run.py`. The corpus contains exact
synthetic content specifications and independently defined expected outputs; it contains
no lane profile, assigned attempt count, usage, correctness, result size, or elapsed time.
The harness executes the owned incumbent compactor, imports PRE `ShuntSession`/`Reader`
from an isolated `git archive` of commit `1686db6`, and imports NEW from the working tree.
NEW calls the real spill, reader, inspect aggregation, and scoped-cache paths. A grounded,
instrumented fixture records actual system+user payload bytes, response bytes, attempts,
and the `ModelResponse.usage` it returns. This deterministic run is regression and red-check
evidence, not proof of real reader quality. Clearing the NEW answer cache changes session 3
from one provider attempt plus one cache hit to two attempts and zero hits; bypassing
structured aggregation raises the relevant attempts from one to six and fails both exact
structured expectations. The refreshed artifact passes both sabotage checks.

The losses remain visible: session 4 executes 10,781- and 9,131-byte recovery results in
every lane (19,912 total) because host-side requery correlation is unavailable. Session 5
executes the 17,601-byte full read in legacy and PRE; NEW executes bounded search instead.
These are observed operation bytes, not credited constants. Model citations are verified
where model output is used; deterministic-only outputs retain citation validity `null`
rather than receiving a vacuous pass.

Deterministic artifacts: [`docs/five-workflow-benchmark.md`](five-workflow-benchmark.md),
[`evals/intent-reader-audit/corpus.json`](../evals/intent-reader-audit/corpus.json), and
[`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json)
(corpus SHA-256 `0e5ba0c6ccd20c9f777f72c07b815903496527d7995cb9ebe78a5501755f5039`).

### Opt-in real Luna validation

The same fixtures were then executed through the qualifying OpenClaw in-host bridge with
explicit opt-in. PRE came from the isolated `1686db6` archive and NEW used the working
tree; legacy executed the owned compactor and made no model call. Every completed response
reported the required post-policy route `sub2api-openai/gpt-5.6-luna` and
`isolated-agent-runtime`; the bridge rejects an absent or different provider/model,
transport, execution mode, or execution owner before an answer can be published.

| Lane | Correct | Verified citations | Real attempts (usage reported/unknown) | Provider tokens in/out/cache | Role bytes | Wall ms | Cache hits |
|---|---:|---:|---:|---:|---:|---:|---:|
| Incumbent legacy compactor | 3/5 | N/A | 0 | unknown | N/A | 80.065 | 0 |
| PRE-change Shunt (`1686db6`) | 3/5 | 3/3 | 8 (8/0) | 10,012/2,857/15,360 | 180,834 | 82,542.618 | 0 |
| NEW implementation | 5/5 | 2/2 | 2 (2/0) | 1,280/138/0 | 5,132 | 30,216.672 | 1 |

The accepted run started ten real attempts, all of which completed with reported usage
and resolved Luna identity. No attempt timed out, failed, or remained late/in flight when
the lane closed. Missing usage would remain unknown; no token field is
derived from payload bytes or a completion ratio.
Main-context token estimates remain separately labeled byte/4 estimates in the JSON.
Wall time is reported per independently launched lane, including initialization, and is
not claimed as comparable end-to-end time savings.

The machine-readable binding identifies NEW as commit
`d05781ebe9c2c31678d23632cf0b1980e4ddcb4b`, PRE as commit `1686db6` with tree
`bbc1b43f9f1959e1ae0b9a82d0658dfaa11d16da`, and the
OpenClaw host as commit `f695db5fde256be60e1d6d76960a81842e400299`. It also binds the
corpus hash, complete committed Git tree for both source checkouts, and an exact digest of
the named evaluation/bridge/core files. Both checkouts must be clean before a provider call
can start. The redacted per-lane artifact is output-only and cannot be resumed or imported
as real-provider evidence; every accepted invocation must freshly execute all lanes.

Each physical call retains a redacted payload attestation only: roles, byte counts,
SHA-256 digests, exact system-contract and user-template checks, locator kind, bounded
deadline/output cap, resolved identity/execution, call status and reported usage. Every
call contained exactly the reader system role plus a user role reconstructible from only
locator, selected source excerpt and question. A synthetic parent-context canary was absent
from all calls; prompt, source, question, answer, quote, host stderr and credentials are
not retained. Each semantic answer instead has a redacted SHA-256/byte/citation record;
every answer independently satisfies its expectation, every citation verifies, and NEW's
cached repeat digest exactly matches its earlier uncached verified answer. NEW's
minified-Loki aggregation and bounded-search workflows made no model call. The real
evidence and exact reproduction command are in
[`docs/five-workflow-real-luna.md`](five-workflow-real-luna.md) and
[`evals/intent-reader-audit/real-luna-latest.json`](../evals/intent-reader-audit/real-luna-latest.json).

The existing 12-item `./scripts/verify shadow deterministic` result remains useful
supplementary regression evidence (97.3% main-context reduction and 1.0 evidence recall),
but it is not evidence for the new reader capabilities and is not used as their acceptance
proof.

## Test and review results

- **Python core:** 1,011 passed, 19 skipped (`.venv/bin/python -m
  pytest -q`).
- **TypeScript core suite:** 806 passed across 19 files (`npm --prefix packages/core-ts
  test`); `npm --prefix packages/core-ts run typecheck` clean. Adapter coverage is also
  exercised by the canonical capability gate below.
- **Five-workflow execution benchmark:** all 15 lane/workflow rows executed and the
  acceptance assertions passed. Its opt-in regression test also passed in the Python
  suite. Both internal red checks passed, and explicit `--candidate-new-variant no-cache`
  and `no-aggregation` runs each exited 1 with the expected feature-loss errors. NEW
  records one bounded answer-cache hit, exact structured results, zero unknown usage
  attempts, and the known requery loss. See the committed JSON/Markdown artifacts.
- **`./scripts/verify shadow deterministic`:** PASS — `main_context_reduction` 0.9734
  (≥0.6), `no_evidence_regression_vs_raw` 1.0 (≥1.0), `bounded_latency` 44.865ms
  (≤2000ms). This is supplementary, not the new-feature benchmark.
- **`./scripts/verify benchmark core`:** PASS, 13 cases, no live provider required.
- **`./scripts/verify unit all`:** PASS — all 17 deterministic gates, 2,573 cases, 0
  failed/not_run/expected_unsupported. This is the audit's output-cap/security/injection/
  forbidden-source invariant coverage: `no-raw-leak` (sentinel fault injection across
  capture, provider, verifier, serialization, retry/fallback, logging, metrics, and guard
  boundaries), `bounded-output` (source/chunk/input/output/envelope/JSON caps), `permissions`
  (root, traversal, symlink/hardlink/race, secret, binary, quota controls), `cancellation`
  (probe/I/O/model/request deadlines, no late publication), and `capability` (missing/unsafe
  host seams disable only the affected mode). The gate definitions were not weakened;
  running them confirms the audit changes did not regress an existing invariant. Machine
  report: `reports/verify-20260914T111917Z-312128000-68351.json`.
- The original regression fixes were verified red-capable at the time they were made
  (recorded per commit), the execution harness has explicit cache/aggregation sabotage
  modes that fail acceptance, and every accepted review finding has a direct regression
  covering its reproduced pre-fix behavior; see
  [`docs/regression-corpus-manifest.md`](regression-corpus-manifest.md#red-capable-verification).
- **External review (`autoreview` skill):** the prior P0-only result was discarded because
  it did not inspect P1/P2 findings. Successive branch-wide reviews were then run with
  `--max-priority P2`. The first produced five accepted findings, all fixed in `2d319a2`;
  the second produced three candidates, of which the two aggregate/cursor findings above
  were reproduced and fixed and the PRE-version claim was rejected using the archived
  contract plus executed lane evidence. The third produced the two cache-key/Unicode-order
  P2 findings above; both were reproduced and fixed. The first post-fix verification found
  the numeric-precision P2 above; it too was reproduced and fixed. The next verification
  found the equality-string bound above; it was also reproduced and fixed. The following
  pass found the rounded-lexeme hole in the initial safe-integer remedy, which was closed by
  rejecting numeric identities entirely. The final command
  `autoreview --mode branch --base main --max-priority P2` against `495a20d` completed with
  a clean secret scan and **no accepted/actionable P0–P2 findings** (`patch is correct`,
  confidence 0.87).

  The real-Luna continuation was reviewed separately with the same explicit command. Four
  P2 findings were accepted and fixed in `f7375f1`: pre-1.3 response schemas now reject
  1.3-only count fields; unknown benchmark usage stays `null`; evaluation evidence is
  bound to the exact provider/route/corpus/PRE/NEW/host implementation; and the TypeScript
  answer cache deep-clones nested provenance. The verification review reported one further
  P2 candidate asking that `max_matches` be cumulative across all search continuations.
  That broad candidate was initially rejected because schema 1.3 deliberately defines a
  per-page cap and the cross-port regressions prove bounded `20 + 20 + 9` traversal; source
  and session disclosure quotas remain cumulative independently. A later review narrowed
  the compatibility concern to still-accepted 1.1/1.2 requests, which was valid and is
  corrected below.

  A final branch review against `3fa92b6` found three additional valid P2s. All are fixed
  in `ca61ad2`: reported input/output totals now remain `null` when attempts exist but none
  reports usage; real runs bind complete clean Git trees for the Shunt and host
  checkouts rather than trusting a hand-maintained transitive file list; and Python and
  TypeScript failure provenance now reports `usage_complete: true` when every started
  attempt returned usage, even if a later publication deadline prevents the answer from
  shipping. Direct regressions cover all three cases, and changing the bound implementation
  made the prior Luna artifact fail its digest check until the real run was repeated.

  The next verification pass found one more valid P2 in the verification surface itself:
  six new TypeScript regression files were not registered in the canonical unit gates.
  `scripts/verify` now runs late-usage and payload-scope tests under `reader`, search-cap
  honesty under `inspect`, and the session-4/session-5 loss-accounting tests under
  `accounting`. The expanded matrix increased from 2,542 to 2,555 executed cases and all
  17 gates pass.

  The following explicit P0–P2 pass found five more valid contract/evidence P2s, all fixed
  in `1d9e75f`: input and output usage reports are aggregated independently; a transport
  total is `null` unless every attempt has that measurement (with measured-attempt counts
  retained separately); the mandatory guards in both ports reject
  a usage-completeness boolean/count contradiction or a measured count above attempts; and
  the 1.3 schema requires aggregate counters/segments only with aggregate mode and forbids
  those shapes in other modes. The artifact was regenerated against the new clean Git tree.

  The subsequent pass found three valid P2s, fixed in `37f5359`: the OpenClaw bridge now
  carries one absolute deadline across first-call startup, server-lock contention,
  nonblocking pipe dispatch, and a final host-side pre-provider clamp; per-workflow input,
  output and cache usage lower bounds are aggregated independently; and real acceptance
  requires every published semantic answer to have verified citations rather than treating
  a missing citation set as inapplicable. Red tests cover slow startup, a contended writer,
  pipe backpressure, partial provider usage fields, and a citationless semantic answer.
  Those bridge tests brought the canonical matrix to 2,560 cases across all 17 passing
  gates. Its first expanded run also exposed a pre-existing scheduler race in one
  multi-chunk Python test: a shared reply list could attach valid fixture citations to the
  wrong concurrently executing source. Commit `9972bbd` makes the fixture select its reply
  from the chunk payload; the reader gate and the repeated full matrix are clean.

  The next explicit P0–P2 pass found four further valid P2s. Both aggregate ports now
  reject escaped lone-surrogate keys before canonicalization, and TypeScript uses a fatal
  UTF-8 decoder for direct text/plain aggregation instead of replacement decoding. The
  execution harness now evaluates every repeated semantic answer independently, retains a
  redacted digest/citation record per answer, requires every answer's citations to verify,
  and proves a cache hit reproduces an earlier uncached verified answer. Direct red tests
  cover each boundary. Because these changes alter the bound implementation and evidence
  schema, the real-Luna artifacts were regenerated only after this state was committed and
  clean. The resulting run passed with ten completed real attempts, no failed/timed-out/
  late attempts, and a per-answer verified citation ledger; the two added aggregate tests
  brought the canonical matrix to 2,563 passing cases.

  The next P0–P2 pass found two more valid P2s and repeated the broad search-cap proposal.
  The evaluator no longer accepts any resume input: its per-lane JSON is an
  output-only redacted artifact, so an editable file cannot stand in for fresh provider
  calls. Both ports also reject lone surrogates in emitted `distinct`/`group_by` pointer
  names, closing the remaining canonical-output exception. That pass's broad `max_matches`
  form was rechecked against the 1.3 contract and rejected. The following review identified
  the narrower backward-compatibility defect: the new per-page behavior had also reached
  accepted 1.1/1.2 requests. Both ports now gate the reset to 1.3, preserve cumulative
  match state for older cursors, and fail a spent legacy cursor explicitly rather than
  granting more results. Signed cursor state also binds the request version, preventing a
  legacy cursor from being relabeled as 1.3 to gain a fresh allowance. Cross-port
  regressions cover 1.1, 1.2, and 1.3 behavior; the six added cases bring the final
  canonical matrix to 2,573 passing cases.

  The following P0–P2 pass found two valid evidence/parser P2s. Embedded JSON aggregation
  now rejects a lone surrogate before UTF-8 byte accounting in both ports, closing an
  internal-exception/parity gap. Generated evidence commands retain only the literal
  `$CONTEXT_SHUNT_OPENCLAW_ROOT` variable reference; the operator's absolute checkout path
  is absent from JSON and Markdown artifacts. Direct tests cover both boundaries, and the
  real run is repeated against the resulting clean commit rather than reusing prior lane
  output.

## Residual blockers

1. **One host-side proposal remains unimplemented by design:**
   [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md)
   (session 4's requery-correlation gap). It requires Hermes' tool-call event stream and
   is outside the owned cores. The reuse and aggregation document is now an implementation
   record; those features are code in both language ports, not residual proposals.
2. **One known, narrow, previously-documented residual** in the search-cap honesty fix
   itself: the oversized-single-line byte-window search fallback always reports
   `complete: false` once it has emitted a window, even on the source's last line, because
   that flag conflates "more matches may exist" with "surrounding context was omitted." A
   caller relying on `complete` alone in that one narrow path may page one extra, empty
   time. Documented in [`docs/limitations.md`](limitations.md); not a false-completeness
   claim, just an imprecise one, and deliberately out of scope for this pass.
3. **Schema-version note for host operators:** the first explicit P0–P2 review correctly found that
   emitting new provenance/aggregate fields while still labelling the envelope 1.2 would
   break a consumer pinned to the closed 1.2 schema. The owned cores now emit envelope 1.3;
   1.0–1.2 envelope schemas remain closed, and pre-1.3 requests cannot select `patterns` or
   aggregate. Any host pinned to the prior contract must explicitly sync 1.3 before it can
   accept these envelopes. No live-host change was made here.
4. **This report does not itself constitute deployment authorization.** Per
   [`docs/acceptance.md`](acceptance.md#what-has-to-be-true-before-the-live-compactor-is-replaced),
   nothing in this branch authorizes replacing the incumbent compactor in production. Ruby
   owns live drift checks, session drain/restart approval, deployment, and canaries.

## Where to look

- This report: `docs/audit-report.md` (this file).
- Regression-corpus manifest: [`docs/regression-corpus-manifest.md`](regression-corpus-manifest.md).
- Source-backed current-payload trace: [`docs/current-payload-trace.md`](current-payload-trace.md)
  — what the actual constructed payload showed per session, traced from read-only
  production evidence before any fix was written.
- Fix commits: `97e5910`, `cd6f8d9`, `d02788d`, `cb298d6`, `ec33776` (branch
  `task/intent-driven-reader-audit`, all five plus the resume-after-cap follow-on), followed
  by continuation feature commit `7a96e7a` and its evidence closeout commits. Use
  `git log --oneline main..HEAD` for the authoritative set.
- Remaining host-side proposal: [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md).
- Implemented reuse/aggregation record:
  [`docs/proposal-deterministic-aggregation-and-reuse.md`](proposal-deterministic-aggregation-and-reuse.md).
- Limitations record: [`docs/limitations.md`](limitations.md).
- Five-workflow comparison: [`docs/five-workflow-benchmark.md`](five-workflow-benchmark.md)
  and [`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json).
- Real Luna validation: [`docs/five-workflow-real-luna.md`](five-workflow-real-luna.md),
  [`evals/intent-reader-audit/real-luna-latest.json`](../evals/intent-reader-audit/real-luna-latest.json),
  and the redacted output-only per-lane evidence
  [`evals/intent-reader-audit/real-luna-lanes-latest.json`](../evals/intent-reader-audit/real-luna-lanes-latest.json).
- Supplementary 12-item raw report: `reports/shadow-ab-latest.json` (gitignored, local only).
- Acceptance-gate definitions (unchanged by this audit): [`docs/acceptance.md`](acceptance.md).
