# Security and retention

What context-shunt stores, where it puts it, how long it keeps it, and what it deliberately
cannot do. This is a disclosure document: if something here surprises you, that is the point
of writing it down.

## What is stored, and what is not

When the gate blocks a read, the content that was withheld has to go somewhere or the gate
is just a refusal rather than an alternative. It goes to a **private cache root outside
every workspace**, split in two:

| Where | What | What it never contains |
| --- | --- | --- |
| `<cache>/store.sqlite3` | authorization only: opaque handle ids, digested session scope, TTL, quotas, content refcounts, disclosure totals, cleanup state, bounded operation metrics | a source path, a question, an answer, a quote, a payload preview, a provider error body, a model or provider name, any filesystem path |
| `<cache>/blobs/<aa>/<bb>/<sha256>.bin` | the immutable payload bytes, content-addressed | — |
| `<cache>/tmp/` | in-flight temp files only; no live handle ever references this directory | — |

The blob location is *derived* from the SHA-256 at call time. It is not stored in the
database and is never returned to a caller.

The five scope components (host, profile, principal, session, generation) are SHA-256
digested before storage, so no session name, account id or profile label is retained. The
gate asserts this: a store written for a session called `a-readable-session-name` must not
contain that string anywhere in the database or its WAL.

**Only withheld content is captured.** An ordinary small read that the gate lets through is
never stored. The optional post-tool mode captures only results that serialize above
`max_tool_result_bytes`. An external artifact is captured only when a deployment has
authorized the import boundary and a caller explicitly asks for that artifact. The store
does not become a shadow copy of the workspace, and it does not become a mirror of a
producer's artifact directory.

## Permissions

Every directory is `0700` and every payload file is `0600`, re-asserted on each use rather
than assumed from creation. Payload files are opened with `O_NOFOLLOW`; a symlink, FIFO,
directory or device where a blob belongs is treated as a path-replacement attempt and fails
closed rather than being followed. A hardlinked payload file is refused for the same
reason.

The cache root is refused if it resolves inside any configured workspace root, and also if
it resolves inside any configured `artifact_import.roots` entry — a manifest could
otherwise name one of the core's own immutable blobs as if it were a producer artifact.

## The external-artifact trust boundary

The import boundary is the only place where a *third party* decides what this system reads,
so it is worth being explicit about what is trusted there. The answer is nothing.

A manifest is a set of claims. Each one is re-proven before a handle exists:

| The manifest claims | What is actually checked | Refusal |
| --- | --- | --- |
| its own shape | a registered translation profile exists for the declared schema, and this deployment allowlisted that schema | `MANIFEST_SCHEMA_UNKNOWN`, `MANIFEST_SCHEMA_NOT_ALLOWED` |
| its own structure | validated against the import contract, which has `additionalProperties: false` and no free-text field anywhere | `MANIFEST_SCHEMA_VIOLATION` |
| where the artifact is | absolute, canonicalized, inside an import root, regular file, not a symlink, not hardlinked, not a FIFO/directory/device, not secret-named, not denylisted | `RELATIVE_PATH`, `OUTSIDE_WORKSPACE_ROOT`, `SYMLINK`, `HARDLINKED`, `NOT_REGULAR_FILE`, `SECRET_PATH`, `NOT_FOUND` |
| how big it is | compared against the pinned stat *and* against the bytes actually read | `ARTIFACT_SIZE_MISMATCH` |
| its digest | recomputed from the bytes actually read | `ARTIFACT_HASH_MISMATCH` |
| its media type | one of the two the snapshot layer can index, and valid JSON when it says JSON | `MANIFEST_SCHEMA_VIOLATION`, `INVALID_JSON` |
| nothing about its content | the same UTF-8 and content secret policy as any capture | `BINARY_UNSUPPORTED`, `SECRET_IN_SOURCE` |

Two details are load-bearing rather than incidental:

* **The manifest file itself goes through the path policy.** Reading it with a plain `open`
  would follow a symlink out of the import roots and read whatever it pointed at, before a
  single artifact check ran. It is authorized, then read through the same bounded reader,
  capped at 64 KiB.
* **The bytes are read through a pinned descriptor.** `O_NOFOLLOW`, with a stat identity
  re-check on both sides of the read. A file swapped between authorization and the read is
  `SOURCE_CHANGED`, not a snapshot of the substitute. The digest check cannot see that race
  on its own, which is why both exist.

Validation runs entirely before publication, so a refused import leaves no handle and no
orphaned blob. No refusal returns the artifact's bytes, and no refusal carries the manifest,
the path, or any producer-supplied text: the receipt in a *successful* envelope carries
bounded tokens only, and its digest is the one derived from the bytes read.

What this boundary does **not** do: it does not vouch for the producer. An authorized
producer that writes a misleading artifact gets a faithfully imported misleading artifact.
The defences that still apply are the ones that always apply — the reader sees one excerpt
and a fixed instruction, citations are verified against the snapshot, and the content secret
policy runs on the payload regardless of who wrote it.

## Retention and deletion

| | Default | Configurable |
| --- | --- | --- |
| Handle TTL | 3600 s (one hour, roughly session scale) | `limits.store_handle_ttl_seconds` (may only be lowered) |
| Store entries | 512 live handles | `limits.store_max_entries` |
| Store bytes | 256 MiB of distinct content | `limits.store_max_bytes` |
| Per-snapshot capture | 8 MiB | `limits.max_source_bytes` |
| Disclosure per source | 256 KiB | `limits.disclosure_max_per_source_bytes` |
| Disclosure per session | 1 MiB | `limits.disclosure_max_per_session_bytes` |

Deletion happens on four paths:

1. **TTL.** A handle past `expires_at_ms` is unreadable immediately — readability is a SQL
   predicate, not a check for whether the file still exists. The sweep removes the row and
   the content afterwards.
2. **A real session boundary.** Hermes' `on_session_finalize` / `on_session_reset`, or an
   OpenClaw `session_end` whose reason is `new`, `reset` or `deleted`, revokes the scope's
   handles and drops the content they held.
3. **The opportunistic sweep**, run on every ordinary turn boundary and at startup.
4. **Startup recovery**, which additionally clears staged temp files and content files that
   have no row (the residue of a crash between the rename and the commit).

A per-turn event never deletes anything. That is deliberate: Hermes fires `on_session_end`
at the end of *every* `run_conversation` call, and OpenClaw fires `session_end` with
`reason: "compaction"` while the conversation continues. Deleting there removes exactly the
recovery state the next turn needs.

**Deletion is `unlink`, not secure erasure.** The bytes may remain recoverable from the
underlying storage until overwritten. If that matters for your data, put the cache root on
an encrypted volume and treat disposal as a filesystem-level concern.

Content is refcounted and deduplicated by hash, so two handles over identical bytes share
one file. Dropping one handle does not delete content the other still references.

## The disclosure ceiling, and why it exists

`context_shunt_inspect` returns real source bytes. A 16 KiB per-page cap alone would be
close to meaningless, because an agent can page. So every page is charged against a
cumulative per-source and per-session budget **in the same transaction that authorizes the
read, before a byte is returned**. Two concurrent inspects cannot overshoot between them.

Once the ceiling is reached, further pages return no content and say
`DISCLOSURE_EXHAUSTED`. There is no configuration in which repeated small reads reassemble a
*large* payload into the main model context. The ceiling is a byte budget, not a promise
that no source can ever be returned in full: `inspect` can return a source small enough to
fit the per-page, per-source, and per-session limits. The 350-line pre-read gate controls
context cost; it is not a confidentiality boundary.

Continuation cursors are HMAC-tagged with a per-store key held in `store_metadata` and bound
to the handle, the snapshot hash and the canonical selector. A cursor cannot be edited to
jump the scan budget, repointed at another snapshot, or replayed into a different store.

## What never leaves

Outside two explicit disclosure forms—short mechanically verified citation quotes and
bounded `inspect` segments—the intercepted raw payload does not appear in an envelope, the
transcript, tool history, persistence, a trace, a log, a metric label, an exception, a
fallback, or a retry. Provider error bodies are dropped at the provider boundary; only a
bounded `MODEL_ERROR` crosses it, because an exception text can contain the prompt or a
payload echo.

Error details are bounded `UPPER_SNAKE` tokens, not free text, so no payload can ride along
inside one. The output guard is the final fixed boundary; if the guard itself fails it emits
a fixed small error envelope rather than the input it was handed.

Metric labels are a closed enum. `model` and `provider` are deliberately **not** in it: a
model name is unbounded vendor-controlled text that changes with configuration, and as a
metric dimension it is both a cost problem and a way to fingerprint a deployment. Which
model was requested belongs in the envelope's provenance block, where it is bounded and
attributed.

The `unit no-raw-leak` gate plants sentinels at the beginning, middle and end of a payload
and injects a failure at every stage to check all of this.

## Secrets

Paths matching the secret policy (`.env*`, private keys, credential stores, `.ssh`,
`.aws`, `.kube`, an administrator denylist) are refused outright. Content markers are
checked on snapshot bytes, on the question, on the model's answer and on every quote; a hit
refuses the affected operation without echoing the matched value.

**This is a denylist, not a proof of secrecy.** It cannot recognise every secret. That is
why the reader provider must independently be an approved data destination, why chunks stay
minimal, and why an allowlist remains necessary. context-shunt does not redact-and-continue:
it refuses, because a redacted snapshot passed off as an original quote would be worse than
a refusal.

## What the reader sees

A fixed instruction, your question verbatim, and one authorized excerpt. No shell, no
network, no write tools, no host conversation, no tool definitions. On OpenClaw this is
enforced by the host's isolated-agent-runtime path, which the host documents as a "fresh,
literal-zero-tool completion" that accepts "one fresh user prompt, not a replayed chat
history".

Source text inside an excerpt is data, never instructions. The fixed instruction says so,
and nothing the model returns can change a permission decision: the gate, the snapshot, the
store, the inspector and the citation verifier are all deterministic code.

## Model attribution, stated honestly

The envelope does not claim to know which model generated an answer when it cannot. See
[`capability-matrix.md`](capability-matrix.md) for the per-host ceiling and the source
evidence behind it. The short version: on Hermes the plugin LLM facade cannot be
distinguished from an echo of the request, so attribution is reported `unverified`; on
OpenClaw the host exposes its own post-policy selection, so attribution is `resolved` — a
routing fact, not a provider confirmation. Neither is reported as `actual`.

A deployment that would rather fail closed than accept an unproven attribution sets
`reader.attribution_policy: require_match`. Be aware that on a host which cannot prove
attribution, this disables the reader entirely; `inspect` and `stats` keep working, because
neither touches a model.

## Reporting

Security issues in this repository: open a private report rather than a public issue. Issues
in a *host* (Hermes, OpenClaw) belong upstream; where a host limitation forces a capability
here to stay off, it is recorded with file-and-line evidence in
[`capability-matrix.md`](capability-matrix.md) rather than worked around.
