# Proposal: cross-request reader reuse and a distinct-value primitive

**Status:** proposal, not implemented. Nothing described here is installed. Unlike
`docs/host-proposal-recovery-correlation.md`, both gaps below are entirely inside this
repository's own contract and code - no Hermes host change is required to build either one -
but they are real feature gaps, not bugs, and each is large enough to deserve its own
reviewed design rather than being folded into this audit's bounded fix pass. This document
exists so Ruby (or whoever prioritizes the next milestone) can decide whether either is worth
building, independent of the milestone-1 deliverable this audit closes out.

Both gaps trace back to session 3 of the 2026-09-14 audit (`20260913_220020_6c5f7a47`), and
both are visible in the same 102,744-byte Loki-shaped source: it was read twice by the
reader (`960297`, then again in `960308`), and the second read asked an "exact error
count/distinct trace IDs" question that timed out at 2/4 chunks, partial.

## Gap 1: no cross-request reader-answer reuse or caching

`960297` and `960308` both targeted the same spilled source (same `source_id`, same
`snapshot_id`) within the same scope. `960297` completed and was marked `ANSWERED`. `960308`
re-read the source from scratch and asked a different question of it, at real reader cost
(65.91s, 2/4 chunks, `TIMEOUT`). Nothing in the current implementation would have prevented
this same waste even if `960308` had asked the *identical* question `960297` already
answered completely: there is no mechanism anywhere in the reader path that recognizes "this
exact question against this exact source snapshot was already answered" and returns the
prior answer instead of re-reading.

This was checked directly, not assumed: `registry.py`'s `_cache` holds only raw spilled-
snapshot bytes keyed by `source_id`/`snapshot_id` (so a second `inspect`/`read` call against
the same source doesn't re-fetch the *bytes* from storage), but nothing caches a completed
reader *answer*. Every `read` call against a source re-invokes the reader from scratch,
regardless of whether an identical question against an identical snapshot was already
answered inside the same session.

**Why this can't be a cache keyed on source alone.** Two reads of the same source asking
different questions (`960297` and `960308` are exactly this) must not collide - reusing
`960297`'s answer for `960308`'s different question would silently return a wrong answer
with no error, which is worse than the current re-read. A correct cache key needs at minimum
`(source_id, snapshot_id, question, selector, security_scope)` - the security scope matters
because two callers with different authorization in the same source could legitimately be
entitled to see different things extracted from it, so an answer computed under one scope
must never be served to a request in a different one.

**Sketch of a bounded implementation**, none of it built yet:

1. A new, session-scoped (not cross-session, not cross-process - that would need the same
   store-locking discipline `store.py`/`store.ts` already have for spilled bytes, which is a
   bigger commitment) answer cache in `session.py`/`session.ts`, keyed on the tuple above,
   populated only after a reader call reaches `complete=True` coverage (a partial or
   `TIMEOUT` answer must never be cached and replayed as if it were final - that would be
   exactly the "claim of completeness under partial coverage" this audit already spent most
   of its effort rejecting elsewhere).
2. A cache hit would need to surface honestly in `stats` as its own record kind (e.g.
   `kind="reader_cache_hit"`), carrying zero reader-model cost and crediting the full
   avoided reader spend the same way a spill's counterfactual credit works today - not
   silently absent from the ledger.
3. Exact question-string matching only, no semantic similarity matching. A caller who
   rephrases the same question would miss the cache and re-read, which is a correctness-
   safe default (a false cache hit that answers the wrong question is a worse failure mode
   than a missed cache hit that costs an extra read).

## Gap 2: no structured distinct-value or grouping cardinality primitive

`960308`'s question - "exact error count, distinct trace IDs" - is a literal aggregation
task with no interesting semantic content: it's asking "how many rows match X" and "how many
distinct values does field Y take." This audit's fix to `inspect`'s `search` selector (see
the "Session 3" entry in the audit's main findings) makes literal substring counting honest
and pageable to an exact total without a reader call - but that only covers "count lines
containing this exact substring." It does not cover "count distinct values of a field," which
needs the caller (today, only the reader/LLM) to extract a field from each matching line and
deduplicate it - exactly the kind of task the product objective explicitly says should not
require an LLM to scan everything.

The `inspect` contract's selector `kind` enum today is `["lines", "bytes", "search"]` only
(confirmed in `contracts/v1/request.schema.json`) - there is no selector kind for "extract
field Y from every matching line and return the distinct set," so a caller asking for
"distinct trace IDs" has no deterministic path at all today and must fall back to the
reader, at reader cost and reader honesty constraints (partial coverage, timeouts).

**Sketch of a bounded future selector**, none of it built yet: a fourth `inspect` selector
kind, tentatively `"distinct"`, taking a `needle` (or the existing `search` selector's match
predicate) plus a field-extraction rule (the simplest version: a fixed-position substring
lifted from each matching line by a caller-supplied delimiter or regex-lite capture, not a
general regex engine - regex introduces its own DoS/ReDoS surface this project would need to
bound separately, so the first cut should stay to something structurally simpler, like
"the token immediately following the needle" or "everything between two literal
delimiters"). It would need the same honesty discipline as the `search` fix in this audit:
`complete` false and a continuation cursor whenever the scan stops before the source is
exhausted, and a distinct-value set that is explicitly partial (with a count of how many
distinct values were seen so far, not a false claim of the true cardinality) whenever paging
is still in progress.

## What this proposal deliberately does not ask for

- No change to any existing selector kind's request or response shape. Both gaps are new,
  additive surface only.
- No semantic/fuzzy caching or matching in either gap - both sketches are exact-match only,
  by design, because a wrong cache hit or a wrong distinct-value extraction is a worse
  failure than falling back to the existing (slower, more expensive, but correctness-safe)
  reader path.
- No implementation in this pass. Both are flagged as real, bounded, in-repo feature gaps
  for a future milestone to pick up or decline, not defects blocking this audit's
  acceptance.
