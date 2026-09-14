# Implementation record: scoped reader reuse and structured aggregation

**Status:** implemented in this owned repository on `task/intent-driven-reader-audit`.
Unlike `docs/host-proposal-recovery-correlation.md`, both features are wholly inside the
shared reader contract and the Python/TypeScript cores. They are not deployed or installed
on Hermes. This document retains the original gap analysis and records the bounded design
that was implemented after Ruby rejected the earlier proposal-only disposition.

Both gaps trace back to session 3 of the 2026-09-14 audit (`20260913_220020_6c5f7a47`), and
both are visible in the same 102,744-byte Loki-shaped source: it was read twice by the
reader (`960297`, then again in `960308`), and the second read asked an "exact error
count/distinct trace IDs" question that timed out at 2/4 chunks, partial.

## Implemented feature 1: scoped exact-query answer reuse

`960297` and `960308` both targeted the same spilled source (same `source_id`, same
`snapshot_id`) within the same scope. `960297` completed and was marked `ANSWERED`. `960308`
re-read the source from scratch and asked a different question of it, at real reader cost
(65.91s, 2/4 chunks, `TIMEOUT`). Before this continuation, nothing would have prevented the
same waste if `960308` had asked the *identical* question `960297` already answered
completely: there was no answer cache in the reader path.

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

**Implemented bounded behavior:**

1. A session-local, process-local 32-entry/256-KiB LRU in `reader.py`/`reader.ts`, keyed by
   a fixed SHA-256 digest of the session, schema, exact question, ordered
   source/snapshot/selector set, budgets,
   refined flag, fixed reader instruction, requested provider/model and attribution policy.
   The retained digest bytes count toward the serialized-material byte bound; the full
   question and selector material are not retained again as the map key.
   The owning reader/registry is bound to the complete trusted
   host/profile/principal/session/generation scope; entries never cross reader instances.
   Every source is re-resolved and snapshot-checked before lookup. It is populated only
   after a reader call reaches `complete=True` coverage (a partial or
   `TIMEOUT` answer must never be cached and replayed as if it were final - that would be
   exactly the "claim of completeness under partial coverage" this audit already spent most
   of its effort rejecting elsewhere).
2. A hit publishes `provenance.cache_reused=true`, a fresh request/accounting id, and
   `attempts_started=0`/`attempts_usage_complete=0`; its per-call reader token fields remain
   not-applicable rather than copying the original call's spend or inventing zero usage.
   It stays a normal `read` accounting record, so the new call's envelope cost is visible
   without creating a store-schema migration solely for a cache label.
3. Exact question-string matching only, no semantic similarity matching. A caller who
   rephrases the same question would miss the cache and re-read, which is a correctness-
   safe default (a false cache hit that answers the wrong question is a worse failure mode
   than a missed cache hit that costs an extra read).

## Implemented feature 2: structured count/distinct/grouping

`960308`'s question - "exact error count, distinct trace IDs" - is a literal aggregation
task with no interesting semantic content: it's asking "how many rows match X" and "how many
distinct values does field Y take." This audit's fix to `inspect`'s `search` selector (see
the "Session 3" entry in the audit's main findings) makes literal substring counting honest
and pageable to an exact total without a reader call - but that only covers "count lines
containing this exact substring." It does not cover "count distinct values of a field," which
needs the caller (today, only the reader/LLM) to extract a field from each matching line and
deduplicate it - exactly the kind of task the product objective explicitly says should not
require an LLM to scan everything.

Before this continuation, the `inspect` selector enum was only
`["lines", "bytes", "search"]`; there was no deterministic path for "extract field Y from
every matching row and return the distinct set," so the caller had to fall back to the
reader and its partial-coverage/timeout constraints.

The additive schema-1.3 `inspect` selector is `kind="aggregate"`. It addresses a validated
JSON array with RFC 6901 `records_pointer`; optional `expand_pointer`, `record_pointer` and
`parse_json` safely cover Loki's minified `data.result[*].values[*][1]` JSON-string shape.
It supports one exact scalar/literal filter, up to four `group_by` pointers and up to four
`distinct` pointers. There is no regular expression or executable expression surface.

The whole selected record set must fit the caller's `max_scan_lines` record budget or the
operation refuses without a partial count. Embedded JSON shares the snapshot byte/node/depth
ceilings. Counts, distinct counts, group counts, and every published group's row count remain
exact after a full scan; returned distinct values and group rows are capped at 200 and carry
`values_complete`/`groups_complete` when high cardinality prevents publishing every key.
Distinct values and group keys are ordered by the UTF-8 bytes of their canonical JSON in
both ports, avoiding Python-code-point versus JavaScript-UTF-16 ordering drift. The
shared exact numeric domain for filter/distinct/group values is integers from
`-(2^53-1)` through `2^53-1`; fractional numbers and larger numeric identifiers are
rejected with `INVALID_REQUEST`/`BAD_SELECTOR` rather than rounded. Callers use JSON strings
when exact decimal or larger numeric identifiers are required. Count-only aggregation does
not compare or key record values and is unaffected. The
canonical JSON result is still charged against per-result,
wire-envelope, per-source and per-session disclosure ceilings. It makes zero model calls.
Missing grouping fields use the explicit JSON marker `{\"missing\":true}` so they cannot
collapse into genuine `null` values; missing distinct fields are omitted from that field's
distinct set.

## Deliberate non-goals

- No change to any existing selector kind's request or response shape. Both gaps are new,
  additive surface only.
- No semantic/fuzzy caching or matching in either gap - both sketches are exact-match only,
  by design, because a wrong cache hit or a wrong distinct-value extraction is a worse
  failure than falling back to the existing (slower, more expensive, but correctness-safe)
  reader path.
- No cross-session/cross-process reuse, semantic similarity matching, regular expressions,
  unbounded key publication, or host-side requery correlation.
