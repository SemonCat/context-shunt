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
   can actually mean. A later P0–P2 review caught that the first fix's cursor carried a
   cumulative match count and therefore could not continue after reaching the per-request
   cap. `max_matches` is now a per-page cap: the authenticated cursor advances the source
   position and resets the page allowance, allowing bounded completion even beyond 200
   total hits. Ruby rejected leaving the remaining owned-repo gaps as proposals, so both
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
cursors now treat `max_matches` as a fresh bounded per-page allowance so every advertised
cursor can make progress. Its third candidate—that PRE was sent an unsupported 1.3 request—
was rejected after checking both the archived contract and actual PRE execution: commit
`1686db6` emits envelopes at 1.2 but its `SUPPORTED_REQUEST_VERSIONS` is
`[1.0, 1.1, 1.2, 1.3]`, and all five PRE rows execute with that fact captured in the JSON
artifact.

Closed error-code lists remain synchronized in both language cores and all contract copies.

## Three-lane comparison (synthetic corpus, local, read-only)

The acceptance-facing comparison is the same five production-derived workflow shapes in
all three lanes, generated by `evals/intent-reader-audit/run.py`. The corpus contains exact
synthetic content specifications and independently defined expected outputs; it contains
no lane profile, assigned attempt count, usage, correctness, result size, or elapsed time.
The harness executes the owned incumbent compactor, imports PRE `ShuntSession`/`Reader`
from an isolated `git archive` of commit `1686db6`, and imports NEW from the working tree.
NEW calls the real spill, reader, inspect aggregation, and scoped-cache paths. A grounded,
instrumented mock provider records the actual system+user payload bytes, response bytes,
attempts, and `ModelResponse.usage` it returns.

| Lane | Correct | Main bytes (tokens est.) | Reader payload in/out bytes | Provider tokens in/out/cache* | Core accounted in/out/cache | Attempts (reported/unknown) | Cache hits | Requery | Full read | Harness ms | Mock delay configured/observed ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Incumbent legacy compactor | 3/5 | 64,667 (16,167) | unknown/unknown | unknown/unknown/unknown | unknown/unknown/unknown | 0 (0/0) | 0 | 19,912 | 17,601 | 80.558 | 0/0 |
| PRE-change Shunt (`1686db6`) | 3/5 | 60,271 (15,068) | 145,991/1,098 | 19,210/262/0 lower bound | 36,499/276/0 | 7 (5/2) | 0 | 19,912 | 17,601 | 238.495 | 14/16.545 |
| NEW implementation | 5/5 | 43,170 (10,793) | 5,132/332 | 1,284/84/0 | 1,284/84/0 | 2 (2/0) | 1 | 19,912 | 0 | 222.308 | 4/5.486 |

\* Provider token values are copied from the fixture's returned usage. The fixture's
explicit tariff is bytes/4, but the harness does not derive a "reported" total after the
fact. Calls configured without usage remain unknown; PRE's reported total is therefore a
lower bound, never scaled by a completion ratio. Main-context tokens alone are explicitly
estimated from observed serialized bytes. Harness elapsed time is measured independently
on each run and recorded in the artifact; configured and observed mock delay are separate
fields, with no arithmetic controlled-time substitute. In this frozen local run NEW took
222.308 ms versus PRE's 238.495 ms. That one controlled-fixture observation is reported as
measured, but is not presented as proof of a production wall-time gain.

Correctness is evaluated from actual emitted answers and aggregate extractions. In this
run NEW satisfies all five independent expectations; PRE and legacy each satisfy three.
As a red demonstration, clearing the NEW answer cache changes session 3 from one provider
attempt plus one cache hit to two attempts and zero hits; replacing structured aggregation
with semantic reads raises the relevant provider attempts from one to six and fails both
structured expectations. Running either sabotaged candidate makes the harness exit 1.

The losses remain visible: session 4 executes 10,781- and 9,131-byte recovery results in
every lane (19,912 total) because host-side requery correlation is unavailable. Session 5
executes the 17,601-byte full read in legacy and PRE; NEW executes bounded search instead.
These are observed operation bytes, not credited constants. Model citations are verified
where model output is used; deterministic-only outputs retain citation validity `null`
rather than receiving a vacuous pass.

Artifacts: [`docs/five-workflow-benchmark.md`](five-workflow-benchmark.md),
[`evals/intent-reader-audit/corpus.json`](../evals/intent-reader-audit/corpus.json), and
[`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json)
(corpus SHA-256 `e732ae0bf0101de6a81073c5fc876f8d8518fca0e76d62ff3c39833e97a1fc7c`).

The existing 12-item `./scripts/verify shadow deterministic` result remains useful
supplementary regression evidence (97.3% main-context reduction and 1.0 evidence recall),
but it is not evidence for the new reader capabilities and is not used as their acceptance
proof.

## Test and review results

- **Python core:** 978 passed, 19 skipped (`packages/core-py`, `.venv/bin/python -m
  pytest -q`).
- **TypeScript/adapter suite:** 948 passed, 10 skipped across 23 files (`npx vitest run`);
  `npx tsc --noEmit -p packages/core-ts/tsconfig.json` clean.
- **Five-workflow execution benchmark:** all 15 lane/workflow rows executed and the
  acceptance assertions passed. Its opt-in regression test also passed in the Python
  suite. Both internal red checks passed, and explicit `--candidate-new-variant no-cache`
  and `no-aggregation` runs each exited 1 with the expected feature-loss errors. NEW
  records one bounded answer-cache hit, exact structured results, zero unknown usage
  attempts, and the known requery loss. See the committed JSON/Markdown artifacts.
- **`./scripts/verify shadow deterministic`:** PASS — `main_context_reduction` 0.9734
  (≥0.6), `no_evidence_regression_vs_raw` 1.0 (≥1.0), `bounded_latency` 55.219ms
  (≤2000ms). This is supplementary, not the new-feature benchmark.
- **`./scripts/verify benchmark core`:** PASS, 13 cases, no live provider required.
- **`./scripts/verify unit all`:** PASS — all 17 deterministic gates, 2,514 cases, 0
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
- **External review (`autoreview` skill):** the prior P0-only result was discarded because
  it did not inspect P1/P2 findings. Two branch-wide reviews were then run explicitly with
  `--max-priority P2`. The first produced five accepted findings, all fixed in `2d319a2`;
  the second produced three candidates, of which the two aggregate/cursor findings above
  were reproduced and fixed and the PRE-version claim was rejected using the archived
  contract plus executed lane evidence. A final post-fix P0–P2 review is recorded in the
  closeout commit after these fixes pass the full suites.

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
4. **Schema-version note for host operators:** the first explicit P0–P2 review correctly found that
   emitting new provenance/aggregate fields while still labelling the envelope 1.2 would
   break a consumer pinned to the closed 1.2 schema. The owned cores now emit envelope 1.3;
   1.0–1.2 envelope schemas remain closed, and pre-1.3 requests cannot select `patterns` or
   aggregate. Any host pinned to the prior contract must explicitly sync 1.3 before it can
   accept these envelopes. No live-host change was made here.
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
  by continuation feature commit `7a96e7a` and its evidence closeout commits. Use
  `git log --oneline main..HEAD` for the authoritative set.
- Remaining host-side proposal: [`docs/host-proposal-recovery-correlation.md`](host-proposal-recovery-correlation.md).
- Implemented reuse/aggregation record:
  [`docs/proposal-deterministic-aggregation-and-reuse.md`](proposal-deterministic-aggregation-and-reuse.md).
- Limitations record: [`docs/limitations.md`](limitations.md).
- Five-workflow comparison: [`docs/five-workflow-benchmark.md`](five-workflow-benchmark.md)
  and [`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json).
- Supplementary 12-item raw report: `reports/shadow-ab-latest.json` (gitignored, local only).
- Acceptance-gate definitions (unchanged by this audit): [`docs/acceptance.md`](acceptance.md).
