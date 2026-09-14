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
   can actually mean. A second, subtler instance of the identical falsehood was then found
   one page later: because a cursor is bound to the selector that produced it (`max_matches`
   included), resuming a capped cursor exactly as instructed handed the exhausted cap
   straight back and silently re-triggered the same bug via a different code path. Fixed by
   raising a distinct `LIMIT_EXCEEDED`/`SEARCH_MAX_MATCHES_EXHAUSTED` error naming the exact
   remedy (reissue with a larger `max_matches`), deliberately *not* routed through legacy
   compaction. Ruby rejected leaving the remaining owned-repo gaps as proposals, so both
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
selector field (`patterns`), a corrected `complete`/cursor semantics for a pre-existing
selector, one new closed-enum failure detail (`SEARCH_MAX_MATCHES_EXHAUSTED`), scoped
exact-query answer reuse, and schema-1.3 `inspect.selector.kind="aggregate"`. Aggregate
supports bounded count/distinct/grouping over validated JSON arrays, optional array
expansion and embedded-JSON parsing for Loki/minified shapes, and no regex/expression
surface. It refuses a scan that exceeds the caller record budget rather than returning a
partial value that could be mistaken for exact.

No sprawling host refactor was attempted. Only session-4 recovery-call correlation remains
a host-side proposal; the reuse and aggregation features are implemented in both owned
language cores and are not installed on the live host.

Reaching each new error code onto the wire correctly required three independent closed
lists to move together in both language cores: the wire-scrubbing allowlist
(`SAFE_FAILURE_DETAILS`), the fallback-eligibility set (`CAPACITY_FAILURE_DETAILS` —
deliberately left unchanged for `SEARCH_MAX_MATCHES_EXHAUSTED`, since that failure is
exactly and cheaply resolvable by the caller and routing it through legacy compaction
would trade an exact count for a sampled approximation), and the `failure_detail` enum in
all three copies of `contracts/v1/envelope.schema.json` (root, `core-ts`, `core-py`).

## Three-lane comparison (synthetic corpus, local, read-only)

The acceptance-facing comparison is the same five production-derived workflow shapes in
all three lanes, generated by `evals/intent-reader-audit/run.py`. The committed corpus is
synthetic metadata only. The legacy lane executes the owned, golden-tested port of the
incumbent v0.3.0 compactor. PRE parameters are frozen from branch `1686db6` and the
sanitized pre-change trace; NEW parameters apply the tested reuse/aggregate routes. This
is a deterministic replay, not a claim that a provider or either git tree was executed
live. Token values are labeled bytes/4 estimates; no price table is required for the
requested token/time comparison.

| Lane | Main tokens (est.) | Reader in/out (est. total) | Attempts (usage complete) | Unknown/late | Requery bytes | Full-read bytes | Accuracy | Controlled wall ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Incumbent legacy compactor | 24,553 | 0/0 | 0 (0) | 0 | 23,112 | 17,601 | 0.400 | 18.043 |
| PRE-change Shunt (`1686db6`) | 18,017 | 157,826/3,350 | 15 (11) | 4 | 23,112 | 17,601 | 0.800 | 405.123 |
| NEW implementation | 7,888 | 25,686/600 | 4 (4) | 0 | 23,112 | 0 | 1.000 | 119.332 |

Controlled wall time uses a fixed 25 ms mock-provider latency per attempt plus a fixed
50 MB/s processing rate. The JSON also carries measured local harness time, but that is
not used as provider latency. Reader attempts with incomplete usage retain reported
lower-bound and estimated-total fields; unknown/late calls are not converted to zero.
Provider cache tokens remain `null` because no provider ran. The NEW exact-answer cache
hit is reported separately and has zero attempts for that call, rather than fabricated
provider-token fields.

The losses remain visible: session 4's 19,912 recovery bytes are present in every lane
because host-side requery correlation is still unavailable; all-lane requery total is
23,112 bytes. The 17,601-byte session-5 full read is present in legacy and PRE, and removed
only in NEW by bounded structured retrieval. Deterministic results have citation validity
`null` because model citations are inapplicable, not a vacuous pass.

Artifacts: [`docs/five-workflow-benchmark.md`](five-workflow-benchmark.md),
[`evals/intent-reader-audit/corpus.json`](../evals/intent-reader-audit/corpus.json), and
[`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json)
(corpus SHA-256 `ad322d962eafd159d484965e20b8c24bf15c90dafc1f88b17ca26e9c25e0ba07`).

The existing 12-item `./scripts/verify shadow deterministic` result remains useful
supplementary regression evidence (97.3% main-context reduction and 1.0 evidence recall),
but it is not evidence for the new reader capabilities and is not used as their acceptance
proof.

## Test and review results

- **Python core:** 969 passed, 19 skipped (`packages/core-py`, `.venv/bin/python -m
  pytest -q`).
- **TypeScript core:** 778 passed across 19 files (`packages/core-ts`, `npx vitest run`);
  `npx tsc --noEmit` clean.
- **Five-workflow benchmark:** all 15 lane/workflow rows passed the harness assertions;
  NEW records one bounded answer-cache hit, exact structured results, zero unknown/late
  attempts, and retains the known requery loss. See the committed JSON/Markdown artifacts.
- **`./scripts/verify shadow deterministic`:** PASS — `main_context_reduction` 0.9734
  (≥0.6), `no_evidence_regression_vs_raw` 1.0 (≥1.0), `bounded_latency` 35.941ms
  (≤2000ms). This is supplementary, not the new-feature benchmark.
- **`./scripts/verify benchmark core`:** PASS, 12 cases, no live provider required.
- **`./scripts/verify unit all`:** PASS — all 17 deterministic gates, 2,498 cases, 0
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
- **External review (`autoreview` skill):** final branch-wide review ran after continuation
  commit `7a96e7a` with `--mode branch --base main` (Codex `gpt-5.6-sol`, high reasoning).
  TruffleHog pre-scan was clean; result: `autoreview clean: no accepted/actionable findings
  reported`, overall `patch is correct (0.98)`. Only synthetic/redacted repository material
  was in the review bundle.

## Residual blockers

1. **The bounded real-provider run remains `NOT_RUN`.** The installed OpenClaw CLI lists
   `openai/gpt-5.6-luna`, but this shell has neither the explicit
   `CONTEXT_SHUNT_LUNA_EVAL=1` opt-in nor a qualifying role-preserving bridge. The only
   repository bridge available from the installed CLI is deliberately disqualified: it
   collapses roles and cannot prove the production-equivalent payload cap. No adjacent
   OpenClaw source checkout was found to use with `openclaw_inhost`, so
   `./scripts/verify eval luna` correctly exited 2/`NOT_RUN`. No credential was printed and
   no provider call was attempted. This blocks a real-provider correctness/citation run,
   not the provider-free token/time comparison above.
2. **One host-side proposal remains unimplemented by design:**
   [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md)
   (session 4's requery-correlation gap). It requires Hermes' tool-call event stream and
   is outside the owned cores. The reuse and aggregation document is now an implementation
   record; those features are code in both language ports, not residual proposals.
3. **One known, narrow, previously-documented residual** in the search-cap honesty fix
   itself: the oversized-single-line byte-window search fallback always reports
   `complete: false` once it has emitted a window, even on the source's last line, because
   that flag conflates "more matches may exist" with "surrounding context was omitted." A
   caller relying on `complete` alone in that one narrow path may page one extra, empty
   time. Documented in [`docs/limitations.md`](limitations.md); not a false-completeness
   claim, just an imprecise one, and deliberately out of scope for this pass.
4. **Schema-version note for host operators:** the new `SEARCH_MAX_MATCHES_EXHAUSTED`
   detail plus the additive aggregate/cache envelope fields and enum values follow this
   project's compatibility convention without an envelope `schema_version` bump. Any host
   validating against a separately pinned `envelope.schema.json` must sync that contract
   before it can accept these values. This does not require touching the live host now.
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
  `task/intent-driven-reader-audit`, all five plus the resume-after-cap follow-on), followed
  by the continuation implementation commit at branch HEAD. Use `git log --oneline main..HEAD`
  for the authoritative set.
- Remaining host-side proposal: [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md).
- Implemented reuse/aggregation record:
  [`docs/proposal-deterministic-aggregation-and-reuse.md`](proposal-deterministic-aggregation-and-reuse.md).
- Limitations record: [`docs/limitations.md`](limitations.md).
- Five-workflow comparison: [`docs/five-workflow-benchmark.md`](five-workflow-benchmark.md)
  and [`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json).
- Supplementary 12-item raw report: `reports/shadow-ab-latest.json` (gitignored, local only).
- Acceptance-gate definitions (unchanged by this audit): [`docs/acceptance.md`](acceptance.md).
