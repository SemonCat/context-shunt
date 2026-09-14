# Limitations

What context-shunt does not do, what it cannot prove, and where a claim stops. Everything
here is a deliberate boundary rather than a to-do list; where a boundary exists because of a
host, the evidence is in [`capability-matrix.md`](capability-matrix.md).

## It is not a shell sandbox

The gate blocks only a positively established large unbounded read on an allowed source.
Unknown tools, custom scripts, unclassifiable shell combinations, and search calls pass
through unchanged. Probe/authorization uncertainty also passes through with diagnostic
telemetry; it is not converted to a tool error. Source registration and inspection retain
strict authorization, integrity, and disclosure checks. The host owns execution policy.

## It only protects the tools it names

Each adapter declares exactly which host tool ids it covers, and the capability report lists
them. A read tool outside that list is not protected. If your host gains a new raw read
tool, this does not automatically cover it, and the capability report will not pretend
otherwise.

## The registered tool schemas expose a portable subset

The shared internal tool-argument contract supports an optional selector for
`context_shunt_read` and per-call `max_result_bytes` / `max_scan_lines` on
`context_shunt_inspect`. Hermes derives its registered schemas from that contract and
exposes those fields. OpenClaw's registration still omits the optional reader selector and
per-call inspect limits while declaring `additionalProperties: false`, so those controls are
not portable across both hosts even though its handler/core understands them internally.
Portable recovery uses the required inspect selector and returned cursor; per-call limit
overrides remain a Hermes-only registered surface until the OpenClaw schema is aligned.

## The artifact broker depends on a producer, and only imports one shape of thing

The primary path for oversized tool results is an *import*, not an interception. That
choice buys deployability and costs coverage, and the costs are worth naming.

- **Something else has to persist the artifact first.** If no compactor or spooler is
  writing artifacts and manifests, there is nothing to import, and this path does nothing
  for you. The broker does not produce artifacts and does not reach out to fetch one.
- **It cannot act before a result reaches the context.** By the time an artifact exists,
  the host has already done whatever it does with the original result. The import keeps the
  *full* payload reachable out of context; it does not undo a truncation the host already
  performed and wrote into the transcript. Where a manifest declares
  `origin.upstream_truncated`, the accounting says so and credits only the observed size.
- **It does not vouch for the producer.** Every claim a manifest makes is re-proven against
  the file, but an authorized producer that writes a misleading artifact gets a faithfully
  imported misleading artifact.
- **Text and JSON only.** Anything the snapshot layer cannot index — binary, an unsupported
  media type, invalid JSON — is refused rather than partially handled.
- **A realistic artifact can trip the secret policy.** A cloud journal page or a startup log
  containing `aws_secret_access_key`, `AKIA`, or a PEM header is refused outright with
  `SECRET_IN_SOURCE`. That refusal is the designed defence and the strongest available
  outcome — the credential never reaches a provider — but it is also a real coverage gap on
  the primary path, not a theoretical one. The shadow corpus includes such an item and
  scores it zero in a denominator that still counts it.
- **Over 8 MiB is refused, not chunked.** The import contract caps `artifact.bytes` at
  `max_source_bytes`, so an oversized artifact is refused at the contract boundary. A
  producer that wants very large results brokered has to page them itself.

## The import boundary exists in one core only

`artifact_import` is implemented in the Python core and supported on Hermes. The TypeScript
core has no import boundary, so OpenClaw reports the mode `unsupported` with reason
`IMPORT_UNIMPLEMENTED`.

That reason is named separately on purpose. It is a repository gap, not a host limitation:
OpenClaw can register the tool, so closing it needs no host change. The OpenClaw plugin
config schema is a closed key set, so `artifact_import` in `openclaw.json` is refused at
load rather than accepted and ignored — the config surface and the capability report agree.

## The shadow A/B is real for three gates and `NOT_RUN` for five

`./scripts/verify shadow all` compares the broker against a raw baseline and against a
reference emulation of the incumbent heuristic compactor. What it proves and what it does
not:

**Measured from the repository alone:** main-context reduction against the raw baseline, no
evidence regression against that baseline, and latency for the deterministic retrieval
lane. The reduction is measured over the items the broker actually brokered; the item it
refuses for exceeding the source cap is roughly 88% of the whole-corpus baseline, and
crediting that counterfactual would turn the headline into a saving on a payload no lane
can answer from. Both figures are reported, and the gate uses the brokered one.

**`NOT_RUN`, and not scored from a substitute:**

- *task correctness* and *semantic evidence support* need a *scored* model lane, and
  scoring one needs a fixed corpus, fixed thresholds and a fixed number of runs per item
  decided before the run. `scripts/verify eval luna` owns those controls, so this harness
  reports the reader lane `NOT_RUN` unconditionally rather than producing a number that
  would look like a score;
- *mechanical citation validity* would be a vacuous 1.0 against the retrieval lane, which
  publishes exact extracts and no citations at all;
- *net cost reduction including retries* needs reader tokens **and** a versioned price
  table. This repository has no price table, so this gate stays `NOT_RUN` even with a live
  reader — deriving currency from token counts is the inference this project refuses;
- *follow-up rate* only exists for an answering lane.

So the honest claim is: the broker is deployable, it holds evidence the incumbent drops on
a fixed synthetic corpus, and it is **unproven at production equivalence**. Nothing here
replaces a live compactor, and the deterministic result is not a licence to.

## OpenClaw automatic capture is unsupported

The retired live canary contradicted the middleware-only tests: accounting claimed a pointer
but the model could not use a handle and raw producer content remained visible. Automatic
capture is disabled at this host seam, even if configured. Results pass through, with no
pointer-delivery or savings claim. A future host seam needs end-to-end replacement proof;
see the [capability matrix](capability-matrix.md#openclaw).

## Model attribution has a ceiling, and it is not `actual`

Neither host proves which model generated the tokens.

- **Hermes**: `PluginLlm._resolve_attribution` records `response.model` when the provider
  returned one, and otherwise the plugin's own override or the host's main model. From the
  caller's side those cases are indistinguishable, so the adapter never claims
  `provider_confirms_generation` and the envelope reports `unverified`.
- **OpenClaw**: the isolated completion path returns the host's own post-policy selection.
  That is a genuine routing fact, so the envelope reports `resolved` — but it is still not a
  provider confirmation, so it is not `actual`.

`attribution_status: actual` is reachable by the contract and by the core, and no supported
adapter currently asserts it. That is the honest state, not an oversight.

Pinning the model on Hermes additionally requires
`plugins.entries.context-shunt.llm.allow_model_override: true`. Without it the host's trust
gate refuses the override and the reader runs on whatever the host would have picked —
which the envelope will then report truthfully rather than hide.

## Citation verification is mechanical, not semantic

The verifier proves that a quote exists byte-for-byte at the location the citation names, in
the snapshot whose full SHA-256 it names, through a handle valid in this scope. It does
**not** prove the quote supports the claim attached to it. Semantic support is what the
opt-in `eval luna` gate measures, and that gate needs live model access.

An answer whose citations all fail verification becomes `CITATION_INVALID` internally and, after at most one repair, mandatory `LEGACY_COMPACTED` fallback with no model answer
text, so a wrong claim cannot survive with fabricated evidence — but a true-looking claim
paired with a real quote that does not actually support it can pass mechanical verification.

## `inspect` returns real bytes, on purpose

`context_shunt_inspect` is the deliberate hole in "the payload never enters the context": it
exists because sometimes you genuinely need the exact text. It is bounded per page (16 KiB)
*and* cumulatively (256 KiB per source, 1 MiB per session by default), so a *large* payload
cannot be paged into a full copy. A source that fits inside those budgets can be returned in
full, and often in a single page — the pre-read gate blocks a 351-line file on context cost,
not on confidentiality, and `inspect` will hand that same file back. Within those budgets it
discloses source content into the main model context, and the accounting records that as
negative savings rather than hiding it.

If that trade is wrong for your deployment, set `inspect.enabled: false`. The reader and the
gate keep working.

## A page is bounded twice, so quote-dense sources page smaller

The 16 KiB per-result cap counts source bytes, because that is what the disclosure ceiling
is a statement about. The output guard counts *serialized* bytes, and JSON escaping puts
those two apart: a `"` costs one byte in the file and two on the wire, a C0 control
character one and six. A page therefore stops at whichever bound it reaches first, so a
quote-dense file — any code, any JSON — yields pages well under 16 KiB. Paging continues
normally; only the page size changes.

Oversized physical lines (including single-line JSON receipts) now page as exact byte
segments with an authenticated continuation bound to the original selector. The segment's
`kind`, `start`, and `end` identify byte offsets; do not treat a partial line as a complete
record. Concatenate page text directly: every LF between selected lines is included once;
the final selected line’s terminating LF is excluded. UTF-8 characters are never split.
Nonempty byte selectors with endpoints inside a code point return `INVALID_REQUEST`. A
budget that cannot fit one code point cannot advance the cursor or consume disclosure.
Oversized search hits may instead return an exact byte
window around the literal hit, with partial coverage and omitted context. Use the returned
byte offsets with a bytes selector for surrounding evidence; a search cursor visits later
hits, not the omitted context. Deliberately tiny budgets that cannot fit one
character or envelope metadata can still fail; exhausted cumulative allowance remains
`DISCLOSURE_EXHAUSTED`, and failed delivery consumes no disclosure bytes.

A `search` page that stops only because it hit its own requested `max_matches` — not
because the remaining source ran out — reports `complete: false` with a continuation cursor,
exactly like a byte- or scan-budget cutoff already did; it no longer claims a whole-source
count under partial coverage. `max_matches` is a per-page cap: resuming the authenticated
cursor resets that bounded allowance and continues from the next unscanned line, so even a
source with more than 200 matches can be exhausted over multiple pages without raising the
one-page cap. One narrower case is not yet covered by this fix: the oversized-single-line byte-window fallback
above always reports `complete: false` once it has emitted a window, even on the source's
last line, because that flag conflates "more matches may exist" with "surrounding context was
omitted" — a caller relying on `complete` alone to know whether another cursor visit is
worthwhile in that specific narrow path may page one extra, empty time. It is a known,
narrow residual, not a false completeness claim, and out of scope for this pass.

Aggregate filter/distinct/group numeric identities are deliberately rejected; encode their
original lexemes as JSON strings when exact identity is required. A host JSON parser can
round a fractional or large value before the core sees it, so even an apparently safe
runtime integer cannot prove the source lexeme was exact. This prevents JavaScript number
rounding from silently merging keys that Python would keep distinct. Count-only aggregation
does not key record values and is unaffected. `filter.equals` strings are limited to 512 contract characters and 512
serialized bytes; use a narrower stable identifier rather than a larger equality literal.

## An answer may lose evidence to fit the envelope

The per-field caps are not jointly satisfiable: the maximum answer plus the maximum number
of maximum-length quotes exceeds the 16 KiB envelope cap before escaping is counted. When an
assembled answer does not fit, evidence is dropped largest-first, the assertions that lose
their citation are stripped with it, and each drop is recorded as a `BUDGET_EXCEEDED`
omission with `status: partial`. An answer that fits is published untouched.

So a `partial` answer may be a *narrower* true answer rather than a complete one — the
omissions say which evidence went. If every citation had to be dropped, the result is an
explicit refusal rather than `NO_MATCH`, which would wrongly claim the sources held nothing.
A model reply that is itself larger than the tool-result cap is refused earlier and shows up
as an `INVALID_MODEL_OUTPUT` omission.

## Deletion is not secure erasure

Removing a payload unlinks the file. The bytes may remain recoverable from the underlying
storage until overwritten. Put the cache root on an encrypted volume if that matters.

## The secret policy is a denylist

It recognises common secret filenames, extensions, directories and content markers. It
cannot recognise every secret. The reader provider must independently be an approved data
destination; minimal chunks and an allowlist remain necessary defences.

## The reader's per-call deadline is sized for a reasoning model

`model_call_deadline_ms` is 45 seconds, inside a provisional 240-second
`request_deadline_ms`. A deployment may narrow these normative caps. Reader-specific
production spans are not yet available, so 240 seconds is deliberately not presented as a
latency percentile: it bounds four 45-second waves for the maximum eight chunks at
concurrency two, plus 60 seconds for bounded retries, verification, and publication.

The provisional cap follows bounded live 228 KiB / eight-chunk checks: one workload
completed in 8.403 seconds; a harder eight-section workload was censored at 60.005 seconds
with six calls completed and two still in flight under the old cap; and the hard workload
later completed all eight chunks in 47.752 seconds under the 240-second cap. This is sparse,
variable workload evidence, not a p95 or p99. Operators can narrow the whole-request or
per-call ceiling when their own reader spans support it.

Every retry and availability candidate receives only the remaining absolute request
deadline. Once it expires, no later provider candidate starts; eligible failure still
degrades to the bounded local compactor, which performs no provider work.

## Benchmarks are partial by construction

`benchmark core` measures what needs no provider: gate latency, envelope sizes, main-context
byte savings against a full direct-read baseline, and the incremental peak RSS of refusing an
oversized source. Reader latency and token cost are the provider half and report `NOT_RUN`
until live model access exists. They are never estimated and never presented as measured.

The main-context reduction figure compares the blocked path against a *full direct read*. A
host that truncates on its own has a smaller real baseline; that case is recorded separately
as `host_truncated_observed` and never mixed with the counterfactual.

## Cross-process, not cross-machine

The store uses SQLite with WAL and a busy timeout, and the gates exercise it with real
concurrent subprocesses. It is a local store. It is not designed for a network filesystem,
where SQLite's locking assumptions may not hold, and nothing here coordinates across
machines.

## Version compatibility

Revision 1.2 accepts 1.0/1.1 requests and validates earlier envelopes. It does not accept a
request that declares an older revision while carrying a newer field or limit — that is
refused rather than ignored. A store
created by an unsupported DDL revision is refused rather than migrated by guesswork;
revisions 1 and 2 have explicit additive migrations to revision 3. See
[`install.md`](install.md) for the supported path.

The reader model became configurable in 1.1, where 1.0 refused anything but the default.
That is a real behaviour change, recorded as such: what replaces the old hard refusal is
truthful provenance, not a silent loosening.

## Node and Python floors

The store needs `node:sqlite`, so the TypeScript side requires Node ≥22.22.3 (which is also
OpenClaw's own floor). Python requires ≥3.11.

## Mandatory fallback limitations

Context Shunt is an availability-preserving optimization layer. When Shunt owns a failure and the authorized source bytes or immutable snapshot are available, both cores automatically return the incumbent bounded deterministic compactor output. This is mandatory: `reader.legacy_compaction` and the TypeScript `legacyCompaction` option are deprecated compatibility no-ops, including when set to `false`.

Eligible failures include `MODEL_ERROR`, `TIMEOUT`, `INVALID_MODEL_OUTPUT`, `CITATION_INVALID`, capture/store failures, and unexpected safe internal errors. `LIMIT_EXCEEDED` is classified by detail: store capacity and implementation output/page capacity qualify; source/input safety caps and disclosure policy caps do not. Invalid arguments, unsupported versions/operations, unsafe/binary/secret sources, cross-session or snapshot mismatch, expired/changed sources, provenance-policy refusal, attribution mismatch, cancellation, and disclosure exhaustion remain explicit refusals. Fallback never authorizes a handle that the store cannot authorize.

The response is always `partial/LEGACY_COMPACTED`, `result_kind: legacy_compaction`, and `provenance.derived: false`, with empty `answer` and `citations`. `legacy_compaction.original_failure` retains the failure code; the bounded explicit `failure_detail` enum distinguishes verifier, argument, and capacity failures without carrying arbitrary exception text. Coverage is incomplete, question-independent, and limited to the first requested source. Python capture prepares a private exact-byte `.txt` mirror under an HMAC-derived name before reader work; after successful fallback disclosure Hermes returns its absolute `raw_artifact_path` without post-deadline raw-payload I/O. It is removed by revocation, scope teardown, TTL sweep, or orphan recovery, but host file-tool reads are outside `inspect` disclosure accounting and can recover the complete source. Preparation failure produces pathless mandatory summary/handle fallback. Mirror copies are reserved against `store_max_bytes` per live handle. TypeScript/OpenClaw currently provides handles without this path. Capture failure before handle publication returns no source handles or path and `handles_valid: false`. The compactor retains the incumbent signal lines, head/tail samples, repetition collapsing, and JSON shaping, within character, byte, and envelope caps.

Citation generation gets at most one bounded repair attempt per request, using fixed safe verifier feedback and already-authorized chunks. The same deadline, input/output budgets, provenance checks, and usage ledger apply. A repair that still fails quote-to-snapshot verification uses mandatory legacy compaction; no answer with unmatched citation quotes is published. This mechanical check does not prove the answer's prose. Genuine valid empty answers remain `NO_MATCH`.

Hermes tool schemas are derived from the canonical tool-argument contract. Malformed handles are refused with fixed diagnostics and guidance to reuse the exact `source_id`/`snapshot_id` pair from the pointer; hashes are never guessed or repaired.
