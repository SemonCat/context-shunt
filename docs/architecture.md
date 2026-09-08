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

## Components and trust boundaries

```text
Hermes adapter                         OpenClaw adapter
 pre_tool_call                          before_tool_call
 ctx.llm                                isolated runtime llm
       \                                  /
        shared contract and independent policy cores
          gate -> snapshot/store -> chunk planner
          reader -> citation verifier -> output guard
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
calls, and start at most one core retry per transient failure. All attempts share the
64,000-token input and 60-second request budgets. One model call is capped at 45 seconds and
2,048 output tokens.

Each model call receives the original question. The model returns structured assertions and
citations. The verifier independently checks handle scope, full snapshot hash, locator
range, and exact quote bytes. It deletes an assertion whose citation fails; if nothing
survives the result is `CITATION_INVALID`. Mechanical verification proves that a quote
exists, not that the quote semantically supports the assertion.

Coverage reports processed/planned chunks, omissions, and whether upstream truncation is
known. Incomplete or unknown coverage cannot be published as complete. If a serialized
answer would exceed its envelope, evidence is dropped deterministically and the assertions
that depended on it are removed; an empty result is a refusal, not a false `NO_MATCH`.

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

This engine is behind `suma_post_tool` and is not wired on either supported host. Hermes
exposes tool results only after truncation and its transform hook fails open; OpenClaw
applies its cap before the persistence hook. Neither supplies both complete capture and safe
replacement before persistence/context insertion. Capability probing therefore reports the
mode unsupported and it remains inactive even if requested in configuration.

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
metric labels, retry/fallback errors, and fixed guard failures. `inspect` segments and short
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
