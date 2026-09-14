# Intent-driven reader audit — report for Ruby

**Report path:** `docs/audit-report.md` at commit `ec33776` on branch
`task/intent-driven-reader-audit` (HEAD as of 2026-09-14 12:10 +0800). This is the exact
path and commit Ruby should review for acceptance.

**Scope of this document:** what was audited, what was found and fixed, what was measured,
what remains open, and where to look for each supporting artifact. It does not authorize
any production change — see [Residual blockers](#residual-blockers) and
[`docs/acceptance.md`](acceptance.md), which remains the acceptance-gate source of truth.

## Constraints this work stayed inside (unviolated throughout)

- All Hermes/AWS access was read-only, via `hermes-ssh`, for inspection only. No deploy,
  restart, drain, plugin/config change, credential change, production model request,
  Slack message, or database mutation was made at any point in this audit.
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
3. **Same late-usage gap (second incident), plus two coupled search-cap honesty bugs** —
   a `search` page that stopped only because it hit its own `max_matches` cap reported
   `complete: true` with no continuation cursor even when most of the source went unscanned
   — indistinguishable from a genuine exact count. Fixed by scoping `complete` to what it
   can actually mean. A second, subtler instance of the identical falsehood was then found
   one page later: because a cursor is bound to the selector that produced it (`max_matches`
   included), resuming a capped cursor exactly as instructed handed the exhausted cap
   straight back and silently re-triggered the same bug via a different code path. Fixed by
   raising a distinct `LIMIT_EXCEEDED`/`SEARCH_MAX_MATCHES_EXHAUSTED` error naming the exact
   remedy (reissue with a larger `max_matches`), deliberately *not* routed through legacy
   compaction — see the rationale in `packages/core-ts/src/errors.ts` and
   `packages/core-py/src/context_shunt/errors.py`.
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
   netted against the loss, not hiding it. Two further gaps found while investigating this
   session (cross-request reader-answer reuse, and a structured distinct-value/grouping
   selector) are written up, not implemented, in
   [`docs/proposal-deterministic-aggregation-and-reuse.md`](proposal-deterministic-aggregation-and-reuse.md).

## Bounded implementation: what changed and what didn't

All five findings above were closed as owned-repo, additive, backward-compatible changes:
new optional envelope fields (`provenance.attempts_usage_complete`), a new optional
selector field (`patterns`), a corrected `complete`/cursor semantics for a pre-existing
selector, and one new closed-enum failure detail
(`SEARCH_MAX_MATCHES_EXHAUSTED`). None required a `schema_version` bump — that convention
is reserved for new mandatory/structural fields (e.g. `raw_artifact_path` in a prior
commit), and every change here is an additive optional field or a new value in an already-
open-ended enum, matching the precedent already used for `OTHER`/`UNSPECIFIED` graceful
degradation.

No sprawling host refactor was attempted. The two gaps that would require a host-side
change are proposal documents, reviewable independently of this branch, not installed
patches — see [Residual blockers](#residual-blockers).

Reaching each new error code onto the wire correctly required three independent closed
lists to move together in both language cores: the wire-scrubbing allowlist
(`SAFE_FAILURE_DETAILS`), the fallback-eligibility set (`CAPACITY_FAILURE_DETAILS` —
deliberately left unchanged for `SEARCH_MAX_MATCHES_EXHAUSTED`, since that failure is
exactly and cheaply resolvable by the caller and routing it through legacy compaction
would trade an exact count for a sampled approximation), and the `failure_detail` enum in
all three copies of `contracts/v1/envelope.schema.json` (root, `core-ts`, `core-py`).

## Three-lane comparison (synthetic corpus, local, read-only)

Measured via `./scripts/verify shadow deterministic` against the fixed synthetic corpus at
[`evals/shadow/corpus.json`](../evals/shadow/corpus.json) — 12 items, none touching
production data. This is the baseline-legacy-vs-current-Shunt comparison the audit asked
for, scoped to what is honestly measurable without live model access:

| Lane | Main-context bytes | Main-context tokens | Reduction vs. raw (brokered items) | Evidence recall |
|---|---|---|---|---|
| Raw baseline | 9,525,040 | 2,381,266 | — | 1.0 (reference) |
| Legacy compactor (incumbent, reference emulation) | 77,761 | 19,445 | 93.8% | 0.25 |
| Deterministic retrieval (current Shunt path: import boundary + `inspect`) | 30,872 | 7,722 | 97.3% | 1.0 |

Deterministic-retrieval-lane p95-class latency: 35.1 ms (threshold 2000 ms). 2 of 12 corpus
items are refused outright by the core (one credential-marker source, one over
`max_source_bytes`) and scored zero in a denominator that still counts them, not dropped.

**What this table does and doesn't prove:** it is a real, reproducible measurement of
main-context byte/token reduction and evidence recall on synthetic data, and it shows the
deterministic path recovering the legacy compactor's evidence-recall shortfall (0.25 → 1.0)
while using fewer bytes, not more. It says nothing about reader-lane task correctness,
citation validity, or net cost including model tokens — those require a live, price-tabled
reader lane and are explicitly out of scope for this pass; see the `NOT_RUN` rows below.

| Gate (reader-scored, not run here) | Status | Why |
|---|---|---|
| Task correctness (≥95%) | `NOT_RUN` | needs `scripts/verify eval luna` with live reader access |
| Semantic evidence support (≥95%) | `NOT_RUN` | same |
| Mechanical citation validity (100%) | `NOT_RUN` | same; the deterministic lane publishes no citations, so scoring it here would be a vacuous pass |
| Net cost reduction (≥30%) | `NOT_RUN` | needs a live reader lane **and** a versioned price table; this repository has neither |
| Bounded follow-up rate (≤25%) | `NOT_RUN` | needs live reader access |

These five are not silently skipped — they are `NOT_RUN` by construction (see
[`docs/acceptance.md`](acceptance.md#shadow-ab)), each naming exactly what live
prerequisite is missing, per the same discipline the repository already applies elsewhere.
Manufacturing a number for any of them without that prerequisite would misrepresent an
unmeasured quantity as measured, which this audit does not do.

Raw report: `reports/shadow-ab-latest.json` (gitignored; corpus SHA-256
`696f07fc8e5d7bc6a3cb0919002ed196fab18e25ac1a297912063c2a1f980202`, contract version 1.2).

## Test and review results

- **Python core:** 960 passed, 19 skipped (`packages/core-py`, `.venv/bin/python -m
  pytest -q`), verified at HEAD (`ec33776`).
- **TypeScript core:** 769 passed across 17 files (`packages/core-ts`, `npx vitest run`);
  `npx tsc --noEmit` clean.
- **`./scripts/verify shadow deterministic`:** PASS — `main_context_reduction` 0.9734
  (≥0.6), `no_evidence_regression_vs_raw` 1.0 (≥1.0), `bounded_latency` 35.1ms (≤2000ms).
  Five reader-scored gates correctly `NOT_RUN` (see above).
- **`./scripts/verify benchmark core`:** PASS, 12 cases, no live provider required.
- **`./scripts/verify unit all`:** PASS — all 17 deterministic gates, 2,480 cases, 0
  failed/not_run/expected_unsupported. This is the audit's output-cap/security/injection/
  forbidden-source invariant coverage: `no-raw-leak` (sentinel fault injection across
  capture, provider, verifier, serialization, retry/fallback, logging, metrics, and guard
  boundaries), `bounded-output` (source/chunk/input/output/envelope/JSON caps), `permissions`
  (root, traversal, symlink/hardlink/race, secret, binary, quota controls), `cancellation`
  (probe/I/O/model/request deadlines, no late publication), and `capability` (missing/unsafe
  host seams disable only the affected mode). None of these gates were modified by this
  audit's fixes; running them confirms the five findings closed above did not regress any
  existing invariant.
- Every fix in this audit was verified red-capable at the time it was made (fix files
  stashed out, new test confirmed to fail, then pass again after unstashing) — recorded
  per-commit in each commit message rather than re-run as one batch; see
  [`docs/regression-corpus-manifest.md`](regression-corpus-manifest.md#red-capable-verification).
- **External review (`autoreview` skill):** run against this branch (`--mode branch --base
  main`, engine `codex`/`gpt-5.6-sol`, `high` reasoning). Per the standing constraint, only
  this branch's own diff was reviewed — no production data ever entered this repository,
  so nothing beyond synthetic/redacted materials was exposed to the external reviewer.
  TruffleHog pre-scan clean; result: `autoreview clean: no accepted/actionable findings
  reported`, overall assessment "patch is correct (0.98)" — bounded contract, provenance,
  and search-completeness updates with corresponding cross-language tests, no P0 defect at
  the required reporting threshold.

## Residual blockers

1. **Reader-lane acceptance gates remain `NOT_RUN`** (task correctness, semantic evidence
   support, citation validity, net cost, bounded follow-up rate) — all require live model
   access this audit was not authorized to spend, and net cost additionally requires a
   price table this repository does not have. This is a measurement gap, not a finding
   against the change; closing it is a live-eval exercise for whoever holds reader-model
   budget, following `./scripts/verify eval luna`.
2. **Two host-side proposals are unreviewed and unimplemented by design:**
   [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md)
   (session 4's requery-correlation gap) and
   [`docs/proposal-deterministic-aggregation-and-reuse.md`](proposal-deterministic-aggregation-and-reuse.md)
   (cross-request reader-answer reuse, and a grouping/cardinality selector). Both need a
   human decision before any implementation is attempted; neither is on this branch as
   code.
3. **One known, narrow, previously-documented residual** in the search-cap honesty fix
   itself: the oversized-single-line byte-window search fallback always reports
   `complete: false` once it has emitted a window, even on the source's last line, because
   that flag conflates "more matches may exist" with "surrounding context was omitted." A
   caller relying on `complete` alone in that one narrow path may page one extra, empty
   time. Documented in [`docs/limitations.md`](limitations.md); not a false-completeness
   claim, just an imprecise one, and deliberately out of scope for this pass.
4. **Schema-version note for host operators:** the new `SEARCH_MAX_MATCHES_EXHAUSTED`
   failure detail was added without a `schema_version` bump (consistent with this
   project's convention for additive enum values). Any host validating envelopes against
   its own separately-pinned copy of `envelope.schema.json` will reject that value as
   unrecognized until it syncs its copy of the contract. This is worth a line in any
   host-facing changelog; it does not block this commit and does not require touching the
   live host to confirm.
5. **This report does not itself constitute deployment authorization.** Per
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
  `task/intent-driven-reader-audit`, all five plus the resume-after-cap follow-on).
  `git log --oneline main..task/intent-driven-reader-audit` lists exactly this set.
  This tree is clean (no uncommitted changes) at the time this report was written.
- Host-side proposals: [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md),
  [`docs/proposal-deterministic-aggregation-and-reuse.md`](proposal-deterministic-aggregation-and-reuse.md).
- Limitations record: [`docs/limitations.md`](limitations.md).
- Three-lane comparison raw data: `reports/shadow-ab-latest.json` (gitignored, local only).
- Acceptance-gate definitions (unchanged by this audit): [`docs/acceptance.md`](acceptance.md).
