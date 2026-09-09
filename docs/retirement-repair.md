# Retirement canary implementation repair

The private 2026-09-10 retirement archive is authoritative. It was read only; no logs,
questions, source contents, credentials, or runtime configs are copied into this repository.
The fixtures use invented text and reproduce structural failure conditions only.
Both live context-shunt plugins remain disabled. No deployment is part of this repair.

## Evidence and root causes

The archived Hermes SQLite accounting snapshot contains 470 events: 175
`UNCLASSIFIABLE_READ`, 61 `LARGE_READ`, 11 `UNSAFE_SOURCE`, 67 `SPILLED`, 27
`LEGACY_COMPACTED`, 38 `LIMIT_EXCEEDED`, 1 `MODEL_ERROR`, 23 `ANSWERED`, 53
`EXTRACTED`, 11 `NO_MATCH`, and 3 `STATS`. The error log independently contains repeated
`search_files` blocks and 38 inspect-limit errors.

The pre-read classifier treated absence of a boundedness proof as grounds to refuse
execution. This confused a context optimization with authorization. Unknown/unclassifiable
calls must pass unchanged; only positively established large unbounded reads on allowed
sources may shunt. Strict authorization remains at snapshot/read/import boundaries.

All 19 retained archived blobs contain one physical line and exceed 16 KiB (16,440 to
164,010 bytes). Inspection treated a line as indivisible, so these ordinary serialized
receipts could not make progress. Its nominal result cap and the space actually available
in its envelope also differ.
Default page selection must account for metadata, escaped text, and continuation rather
than fail because the source has more records. Disclosure ceilings and indivisible units
remain real limits, not reasons to promise unlimited paging. Oversized search-hit windows
are explicitly partial with omitted context and byte-range guidance; they cannot claim full
matching-line coverage.

Citation verification correctly rejected invalid evidence, but the session converted
`CITATION_INVALID` into a question-independent heuristic compaction. Both cores now preserve
the explicit error, empty answer/citations, source handles, and paid-attempt accounting.
The caller can verify exact evidence through bounded lines/bytes/search inspection.
Availability-only compatibility fallbacks remain partial, non-semantic, and distinct from
successful semantic summarization.

OpenClaw's archived log records `SPILLED`, `delivery_boundary=pointer`, `handle_count=null`,
and `reader_called=false`; the retirement report records the raw receipt still visible in
run history. Handler/runner tests proved only a local return value, not the effective
model-input replacement. Automatic capture on that seam is unsupported. Both shared sessions also normalize a
rejected pointer envelope to an error action and envelope boundary, without consuming the
undelivered handle's baseline credit. A synthetic oversized request ID reproduced the old
`action=spill` result after guard rejection in both cores. Neither the
presence of the registration API nor configuration is proof of replacement.

## Regression provenance and remaining proof

Gate conformance cases, inspect tests, citation recovery tests, and OpenClaw adapter tests
use synthetic values. The citation regression was observed failing in both cores before
the fix: expected `CITATION_INVALID`, received `LEGACY_COMPACTED`. It now additionally
resolves the retained handle through exact inspection and checks error accounting.

Deterministic tests cannot certify Luna's semantic evidence support or live host ordering.
Before any separately authorized live re-enable, prove legitimate Hermes workflows,
inspection continuation, semantic citation quality, and the effective host delivery path.
For any future OpenClaw capture seam, a model-visible pointer must carry a usable handle,
the reader must resolve that handle in the same session, and raw sentinel bytes must be
absent from effective model input and persisted tool history. Until then capture stays
unsupported. Live-only gates remain `NOT_RUN`.

## P1 follow-up to `fbabdae`

Synthetic regressions reproduced four defects from independent review:

- UTF-8 byte extraction computed its length before moving the start boundary. This could
  disclose beyond the selector or silently skip a character that did not fit. Both cores
  now reject nonempty ranges with split-code-point endpoints and preserve the exact cursor
  when a page cannot fit a character. Session errors disclose nothing and consume no budget.
- Oversized-line continuation omitted LF bytes before advancing to the next line. Both
  cores now include every actual LF between selected lines, including page boundaries and
  empty lines. Concatenated pages equal the selected source bytes; the final selected
  line’s terminating LF remains excluded. CRLF and exact-budget boundaries are covered.
- Python treated a nonempty model answer with an empty citation array as successful
  `NO_MATCH`. It now returns `CITATION_INVALID`, matching TypeScript, while retaining source
  handles, paid provider cost, and post-provider TTL checks. Unverified answer text is absent.
- Python artifact import recorded pointer delivery and consumed baseline credit after the
  output guard replaced the pointer with an error. Accounting and metrics now follow the
  effective guarded envelope. Core and registered Hermes handler regressions require an
  envelope boundary and zero baseline credit on rejection. TypeScript artifact import is
  unimplemented; OpenClaw continues to report `IMPORT_UNIMPLEMENTED`.

All fixtures contain invented content. This follow-up changes no live capability or
configuration and does not supply the remaining live proof described above.
