# Architecture and contract revision 1.1

This is the normative design for the current implementation. Numeric values below are
current defaults; [`contracts/v1/limits.json`](../contracts/v1/limits.json) is authoritative
and deployments may only narrow them. The entire public surface is read-only.

## Contract compatibility

Revision 1.1 accepts 1.0 requests and validates 1.0 envelopes. An envelope declaring 1.1
must include `result_kind`, `provenance`, and `accounting_id`. A request that declares 1.0
but carries a 1.1 field or operation is refused rather than silently ignoring it. Emitted
and accepted versions are separate constants in both cores.

Revision 1.1 added deterministic `inspect`, session `stats`, the hybrid snapshot store,
recovery guidance, model provenance, and signed token accounting. It also made the reader
model/provider configurable while retaining `gpt-5.6-luna` as the default.

Revision 1.1 additionally carries the `IMPORTED` code and the optional `import_receipt`
block for the external-artifact boundary. Both are additive: a 1.0 envelope still validates
unchanged, and a 1.0 envelope carrying `import_receipt` is refused rather than accepted with
the block ignored. `IMPORTED` is deliberately not `SPILLED`. `SPILLED` belongs to the
capability-gated post-tool mode that no supported host enables, so reusing it for an import
would read as a claim of post-tool interception. The **store** schema is unchanged: DDL
revision 2, no migration. `handles.kind` records the capture category and is shared with
the local spill path because [`contracts/store/v1.sql`](../contracts/store/v1.sql)
deliberately stores no producer identity; the producer distinction lives in the envelope
receipt and in the `capture` accounting kind, which was already legal in the DDL and unused.

## Components and trust boundaries

```text
Hermes adapter                         OpenClaw adapter
 pre_tool_call                          before_tool_call
 ctx.llm                                isolated runtime llm
       \                                  /
        shared contract and independent policy cores
          gate -> snapshot/store -> chunk planner
          reader -> citation verifier -> output guard
          exhausted availability -> bounded exact inspect -> output guard
          inspect/stats -> output guard
                         |
                         v
                    bounded envelope
```

Adapters normalize host calls, establish scope and host capabilities, and bridge model
calls. Gate, snapshot, store, inspection, citations, accounting, and envelope semantics live
in the Python and TypeScript cores. The cores are independent implementations pinned by the
same schemas, fixtures, limits, status pairs, and SQLite DDL.

Source files and tool payloads are untrusted data. The reader gets a fixed instruction,
the caller's question, and one authorized excerpt at a time. It receives no host history,
shell, network, writer, or other tools. Model output cannot grant permissions or create a
valid citation.

## The intake paths

There are three ways content becomes a snapshot, and they answer different problems.

```text
  primary: an oversized TOOL RESULT                          secondary: an oversized
  ------------------------------------                       FILE READ
  a producer already            the host's own hook          the agent is about
  persisted a file               captures it inline          to make
             |                          |                              |
   manifest + artifact file    transform_tool_result-shaped    host read request
             |                  hook (operator-attested            |
   artifact import boundary     only; Hermes only)              pre-read gate
   (allowlisted roots,                  |                  (blocked before execution)
    re-proven claims)          capture-then-pointer                   |
             |                  (SpillEngine)                         |
             +----------------------+----------------> immutable <----+
                                                          snapshot
                                                             |
                                       opaque handle, internal or ordinary
                                                             |
            +---------------------------+---------------------------+
            |                           |                           |
   deterministic inspect        question-aware reader        session accounting
   (zero model calls)           (citation-verified)          (signed, bounded)
```

Both tool-result sub-paths exist because the oversized context that actually costs a
session is a tool result - a log query page, a cloud journal page, an issue tracker
export, a wiki page - not a source file, and answering a question about one afterward
always goes through the same question-aware reader either way (never the hook itself:
neither hook nor import receives a question, so "capture" and "answer" stay two steps on
every path).

**Artifact import** needs a producer that already wrote the result to a file and described
it with a manifest; the import boundary re-proves every claim the manifest makes and adopts
it as an ordinary handle. It needs no interception ordering at all, which is why it is
supported wherever a host can register the tool.

**`tool_result_capture`** uses each host's supported replacement seam. OpenClaw 2026.9.3
uses the official runtime-neutral middleware for eligible read-only text/JSON in embedded
and OpenClaw-owned Codex dynamic tools. It captures the immutable middleware-visible
representation, not necessarily complete producer output: the host sanitizes before the
handler. Ambiguous cap boundaries are withheld and refused. Codex-native results are
observe-only. Hermes still requires its existing local operator attestation.
See [capability evidence](capability-matrix.md#openclaw).

The secondary path is the original pre-read gate, unchanged. It remains the answer for an
oversized *file* read, and it is the only path that can act before an operation runs. A
full read that gets past it uses the question-aware reader exactly like a captured tool
result does; a reader failure on either kind of source uses the same automatic-extraction
and legacy-compaction fallback tiers described below.

## Artifact import boundary

`context_shunt_import` takes a manifest path and returns a pointer envelope. It never
returns the artifact's bytes, and it makes no model call.

**Every manifest field and every path inside it is untrusted.** The manifest is a set of
claims, and nothing in it is believed until it has been re-proven:

| Claim | How it is proven |
| --- | --- |
| the manifest is a manifest | authorized through the same path policy as the artifact, then read with the same bounded reader, capped at 64 KiB, and parsed as JSON. An unauthorized `open` here would follow a symlink out of the import roots before any artifact check ran. |
| the declared shape | validated against [`artifact-import.schema.json`](../contracts/v1/artifact-import.schema.json). A shape with no registered translation profile is refused before its fields are read; a shape this deployment has not allowlisted is refused before its translator runs. |
| the artifact path | canonicalized; must resolve inside a configured import root; must be a regular, non-symlinked, un-hardlinked file; must survive the secret-path and administrator denylist. |
| the artifact bytes | read through a pinned descriptor with `O_NOFOLLOW` and a stat identity re-check on both sides of the read, so a swap mid-read fails closed instead of mixing versions. |
| the declared size and digest | compared against the bytes *actually read*. A disagreement is `SOURCE_CHANGED` rather than trusting either side. |
| the content | UTF-8 text or parseable JSON, and subject to the same content secret policy as any capture. |

None of that logic is re-implemented. The boundary is a sequencer over `paths.authorize`,
`snapshot.snapshot_file` and the store; its own contribution is the manifest contract, the
producer-profile translation, and the ordering guarantee that nothing is published until
every check has passed. A refusal leaves no handle and no orphaned blob.

The contract is producer-agnostic. `context_shunt.artifact_import.v1` is the only shape the
core owns; a foreign manifest reaches it through a translation profile in a lookup table of
*data*, so no particular producer's identifiers appear in the core API or in any signature.
A profile is a translator, never an authorization: registering one teaches the core to read
a shape, and `artifact_import.accepted_manifest_schemas` decides whether it may.

An imported handle is published `internal`, the same store-verified recursion guard the
spill engine uses, and its baseline is `host_truncated_observed` when the manifest declares
upstream truncation - a payload a producer already shortened can only be credited at its
observed size.

There is no free-text field anywhere in the manifest contract, so nothing an untrusted
producer writes can ride into an envelope, a log line or a metric label. The receipt carries
bounded tokens only, and its digest is the one derived from the bytes read.

## Pre-read gate

The gate classifies supported read, search, and shell tools before invocation. Under the
current defaults, a full text read is allowed through 350 physical lines if it also fits
the byte boundary, and blocked at 351. LF defines physical lines; a trailing LF does not
create an extra line, CRLF counts as one line, and an empty file has zero lines. The probe
stops after 351 lines and has its own one-second deadline.

A targeted read must have a valid offset and limit and remain within 350 lines and 16 KiB.
A bounded search must cap matches and output bytes. Supported shell forms such as bounded
`head`, `sed`, and `grep -m` are structurally parsed. Dynamic paths, globs, unsafe
pipelines/substitutions, or other read-like commands whose output cannot be proved bounded
return `UNCLASSIFIABLE_READ`. Non-read commands remain the host's responsibility.

The gate only protects the tool ids listed by the adapter's capability report. A deployment
that needs complete coverage must disable unlisted raw-read tools.

## Snapshot and store

Capture validates every path and source before publishing anything. Sources must be regular
UTF-8 text or supported JSON under an allowed canonical root and outside the built-in and
administrator denylists. Symlinks, hard links, devices, FIFOs, sockets, invalid encodings,
binary content, identity races, and sources over 8 MiB are refused.

Payload publication writes a private temporary file with safe flags, syncs it, renames it
to a SHA-256-derived immutable blob path, and then publishes all handles for the request in
one SQLite transaction. Multi-source capture is all-or-nothing. Crash recovery removes
staged files and blobs with no metadata row.

SQLite stores only opaque handles, digested scope, generation, TTL, quotas, refcounts,
disclosure totals, cleanup state, and bounded operation metrics. It never stores source
paths, questions, answers, quotes, previews, provider error bodies, model/provider names,
or filesystem paths. The normative schema is
[`contracts/store/v1.sql`](../contracts/store/v1.sql), executed verbatim by both cores.

Handle readability is a SQL predicate over unrevoked state, expiry, an open scope, and the
current generation. A clock high-water mark prevents wall-clock rollback from reviving a
handle. Handles are bound to digests of host, profile, principal, session, and generation.
Possession of an id alone is insufficient.

Content is deduplicated by full hash and refcounted. Removal marks metadata first, unlinks
outside the transaction, then confirms no reference remains. A content mismatch fails
closed and does not delete the suspect blob.

## Query-aware reader and citations

`context_shunt_read` accepts either authorized paths for capture or existing
`source_id`/`snapshot_id` pairs. Selectors are `all`, 1-based inclusive `lines`, 1-based
inclusive JSON `records` under an RFC 6901 pointer, or bounded literal `search`. Paths never
enter the internal request schema or output envelope.

Text chunks preserve physical-line locations and UTF-8 boundaries. JSON snapshots have a
deterministic record index; citations identify stable record ordinals rather than pretty
printed lines. A request may plan at most eight chunks, use at most two concurrent model
calls, and start at most one core retry per transient failure, plus at most one further
retry for a schema or claims/citations relationship failure. The two budgets are
independent - each may spend its own retry on the same chunk, so both can fire together -
and the combined worst case for one chunk is bounded by the sum of the two limits, never
more. All attempts share the 64,000-token input and 60-second request budgets. One model
call is capped at 45 seconds and 2,048 output tokens.

Each model call receives the original question. The model returns structured `claims`
(`{"text", "citation_ids"}`) plus the `citations` array those ids reference, never a
hand-placed inline marker: the reader validates every `citation_ids` entry against the same
reply's own `citations` (unknown, duplicate, or missing ids drop that one claim, never the
whole answer, and never guessed at) and mechanically verifies each surviving id against the
snapshot exactly as before. Only after that does it deterministically render the public
`answer` string, placing every `[cN]` marker itself - the model never writes one. This
removes a formatting task the model previously had to get right twice (once as a marker in
prose, once as a citation object); a reply already shaped the old way - hand-placed markers
in free-form prose - is still accepted only when it already satisfies that older contract,
never inferred from markerless prose. A reply carrying both shapes at once is refused as
ambiguous rather than guessed at. The verifier independently checks handle scope, full
snapshot hash, locator range, and exact quote bytes. It deletes an assertion whose citation
fails; if nothing survives the result is `CITATION_INVALID`. Mechanical verification proves
that a quote exists, not that the quote semantically supports the assertion.

Coverage reports processed/planned chunks, omissions, and whether upstream truncation is
known. Incomplete or unknown coverage cannot be published as complete. If a serialized
answer would exceed its envelope, evidence is dropped deterministically and the assertions
that depended on it are removed; an empty result is a refusal, not a false `NO_MATCH`.

## Automatic availability escape hatch

`ShuntSession.read` uses the existing inspector only after a wholly unavailable reader
result. It extracts a nonempty, strictly shorter byte prefix of the first source (2 KiB by
default, at most 4 KiB), revalidates every handle, guards before charging disclosure, and
records one delivery with both failed LLM cost and extraction egress. It publishes
`partial/EXTRACTED`, explicit escape-hatch guidance, deterministic provenance and conservative
omissions, never a summary. The 1.1 envelope and store schemas are unchanged. Wholly failed
reads now report `MODEL_ERROR`/`TIMEOUT` rather than partial `NO_MATCH` when extraction cannot
run. See [configuration](configuration.md#automatic-exact-extraction-after-reader-unavailability)
for exact triggers, selector-independent prefix selection, limits and compatibility.

## Legacy-compaction fallback

On Python/Hermes, `reader.legacy_compaction` (default on) is tried before automatic
extraction for terminal `CITATION_INVALID`, `MODEL_ERROR`, and `TIMEOUT`, including
wholly unavailable readers after retries and model fallback. It publishes a deterministic, ported heuristic summary of the
first requested source - signal lines, head/tail sampling, repeated-line collapsing, JSON
structure, secret redaction - never exact bytes and never model output, as
`partial/LEGACY_COMPACTED` with `result_kind: legacy_compaction`,
`provenance.derived: false`, and a dedicated `legacy_compaction` block distinct from
`extraction`'s "never a summary" contract. A reported-model mismatch is deliberately
excluded: `enforce_policy` already treats that as a wrong answer, not a weak one, and a
heuristic summary is not a remedy for it. If compaction is disabled or unsafe, wholly unavailable reads may use secondary exact
extraction; otherwise the original bounded failure remains, never raw. Coverage and
failed-attempt accounting are preserved. OpenClaw selects the TypeScript port on exhausted availability; generic TypeScript sessions retain automatic extraction unless this session option is selected. See
[configuration](configuration.md#legacy-compaction-fallback-for-reader-outcomes-automatic-extraction-does-not-cover)
for the exact trigger set, caps and wire shape.

The algorithm itself (`context_shunt.legacy_compact`) is a function-for-function port of
the text/JSON-shaping half of the incumbent tool-result compactor plugin this project
displaces on hosts where it is deployed - read read-only from a live operator host, not
reconstructed from memory or from the deterministic-shadow corpus's reference emulation
(which is explicitly documented as a narrower stand-in, not the incumbent's real
algorithm). See the module's own docstring for exactly what was and was not ported.

## Exact inspection and disclosure

`context_shunt_inspect` returns exact text with no provider call. `lines` uses 1-based
inclusive coordinates, `bytes` uses 0-based half-open coordinates adjusted to safe UTF-8
boundaries, and `search` takes a literal needle. A page is limited by both source bytes and
serialized envelope headroom. Search also has line/byte scan budgets.

Every returned source byte is charged transactionally before publication against cumulative
per-content and per-session disclosure ceilings (currently 256 KiB and 1 MiB). The
continuation cursor is HMAC-authenticated and bound to store, handle, snapshot, and selector;
it cannot expand permission or skip accounting. When a ceiling is exhausted, no more content
is returned. A small source can still fit entirely inside the budget.

## Spill/pointer engine

The core contains a pure spill/pointer path for oversized serialized tool results. It does
not summarize or call a model. It only publishes a pointer after a complete safe capture;
failure returns a bounded error without the raw result. Internal-pointer recursion bypass is
verified against store state, not trusted from payload data.

This engine is behind `tool_result_capture` (formerly documented under the internal name
`suma_post_tool`). OpenClaw wires the official middleware to `ShuntSession.postToolResult`, sharing the existing `ScopeIdentity`, immutable store, pointer schema and accounting. A missing scope fails closed; middleware does not use a shared unbound scope. Host-clipped candidates receive no handle. Tokenjuice and other result reducers must be disabled atomically at cutover. On Hermes, direct
read-only inspection of one live 0.21.1 host found the tool executes and `post_tool_call`
fires before `transform_tool_result` runs, with no truncation call visible between - so the
mode is wired there, but stays reported unsupported by default: that finding is evidence
about one running instance, not a reproducible, host-version-independent proof, and per-tool
self-truncation upstream of that dispatch layer was not audited. It becomes supported, and
the `transform_tool_result` hook registered, only when a deployment sets an explicit
operator attestation (`tool_result_capture.host_ordering_verified_locally: true`) - this
code does not and cannot prove the ordering for itself. See
[capability-matrix.md](capability-matrix.md#tool_result_capture-on-hermes-021-what-changed-and-what-did-not)
for the exact evidence and [acceptance.md](acceptance.md#tool_result_capture-cutover-on-hermes)
for the cutover plan.

## Output, security, and no-raw-leak boundary

Ordinary envelopes are at most 16 KiB. Inspect/stats use the explicit 20 KiB extended
envelope cap to contain at most a 16 KiB extraction/page plus metadata. Answers are at most
8 KiB, quotes 512 bytes, citations 16, and questions 2 KiB. JSON is bounded to depth 64 and
100,000 nodes. These are current defaults, not values to duplicate into integrations.

Secret policy applies to paths, snapshot content, questions, generated answers, and quotes.
It is a denylist and cannot prove a source is non-secret; operators must still approve the
reader provider and constrain roots. The system refuses instead of redacting and presenting
modified text as an original quote.

Raw payloads and provider exception bodies are excluded from envelopes, logs, traces,
metric labels, retry/fallback errors, and fixed guard failures. `inspect` segments (including automatic availability escape hatches) and short
verified citation quotes are the only deliberate source-text disclosures to the main
context. The no-raw-leak gates inject sentinels and failures across every stage.

## Provenance and accounting

Every 1.1 envelope distinguishes model-derived from deterministic results. Model provenance
separates requested, host-resolved, and provider-reported identities and classifies the
strongest evidence as `actual`, `resolved`, `unverified`, `mismatch`, `unknown`, or
`not_applicable`. Requested identity is never promoted into a stronger field. See
[`configuration.md`](configuration.md) for policy and host routing.

Accounting measures the final serialized delivery boundary, credits a measured baseline
once per snapshot, and includes all physical retry/fallback attempts. Missing provider
input/output counts use a measured-byte estimate labeled `bytes_div_4`; unavailable cache
usage remains null. It separates signed main-context savings from reader input/output cost.
See [`metrics.md`](metrics.md) for the exact fields and formulas.

## Lifecycle, compatibility, and release proof

Current handle TTL is one hour. Startup and ordinary turn boundaries sweep expired rows;
real reset/finalize/delete events revoke a scope. Hermes' ordinary `on_session_end` and
OpenClaw `compaction` are not destructive boundaries. Directories are `0700`, payload files
`0600`, and cache deletion is unlink rather than secure erasure.

Host upgrades invalidate evidence until real integration gates are rerun. Deterministic
gates cannot substitute for host ordering, live provider behavior, semantic citation
quality, latency, or token reporting. Current verified/unsupported/`NOT_RUN` distinctions
are maintained in [`capability-matrix.md`](capability-matrix.md) and
[`acceptance.md`](acceptance.md).
