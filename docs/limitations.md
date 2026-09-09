# Limitations

What context-shunt does not do, what it cannot prove, and where a claim stops. Everything
here is a deliberate boundary rather than a to-do list; where a boundary exists because of a
host, the evidence is in [`capability-matrix.md`](capability-matrix.md).

## It is not a shell sandbox

The gate classifies read-like shell commands and refuses the ones it cannot prove bounded.
It does not claim to understand every custom script, wrapper or interpreter. A command that
reads a file through a path it cannot analyse gets `UNCLASSIFIABLE_READ` — refused, not
allowed — but a deployment that needs a total guarantee has to disable uncontrolled shell
access at the host, not rely on this.

Non-read commands are none of its business and are left to the host's own policy.

## It only protects the tools it names

Each adapter declares exactly which host tool ids it covers, and the capability report lists
them. A read tool outside that list is not protected. If your host gains a new raw read
tool, this does not automatically cover it, and the capability report will not pretend
otherwise.

## The registered tool schemas expose a portable subset

The shared internal tool-argument contract supports an optional selector for
`context_shunt_read` and per-call `max_result_bytes` / `max_scan_lines` on
`context_shunt_inspect`. The current adapter registration schemas omit those optional
fields. In particular, OpenClaw declares `additionalProperties: false`, so an agent cannot
portably send them even though the handler/core understands them internally. The documented
host-facing examples therefore use full-source reader selection and configured inspect
limits. Aligning the registered schemas with the shared contract remains an adapter release
task, not a capability this documentation assumes.

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

## The oversized post-tool mode is off on both hosts

The engine is implemented and tested. It stays disabled because neither supported host can
show the two things the mode needs: complete capture *before* truncation, and safe
replacement *before* persistence and context insertion. Hermes' `transform_tool_result`
receives post-truncation content inside a fail-open `try/except`; OpenClaw caps the result
before invoking the plugin's persist hook.

The tests that exist for it are core tests behind a capability gate. They are not evidence
about a host, and turning the flag on does not turn the mode on: the unsupported capability
decision takes precedence over the configuration request.

What is *not* proven either way: a live runtime sentinel measurement of capture / truncation
/ persistence ordering inside a running gateway. The host-integration gate reads the
ordering out of the host source instead, which is weaker but honest, and it fails if a host
upgrade changes that source.

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

An answer whose citations all fail verification becomes `CITATION_INVALID` with no answer
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

One case is a hard stop rather than a smaller page. A `lines` selector cannot split a line,
so a *single* line whose escaped width exceeds the envelope headroom cannot be returned at
all: that is `LIMIT_EXCEEDED` with detail `UNIT_OVER_WIRE_BUDGET`, and nothing is charged
against the disclosure ceiling for it. The same bytes are reachable with a `bytes` selector,
which may split anywhere. This is deliberately not reported as `DISCLOSURE_EXHAUSTED` —
that would point at waiting for allowance, which never helps here.

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

`model_call_deadline_ms` is 45 seconds, inside a 60-second `request_deadline_ms`. A
deployment may only narrow these normative defaults, not raise them. The longer per-call
budget accommodates slow reasoning-model calls but leaves little request time for recovery.

The consequence to know about: one call can occupy 45s of a 60s request budget, so the
reader's single permitted retry will usually not fit. A transient provider failure
therefore tends to surface as the failure itself rather than as a successful retry. That
is the honest trade at these latencies; a deployment that would rather have the retry can
narrow `model_call_deadline_ms`, at the cost of aborting slow-but-fine calls.

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

Revision 1.1 accepts 1.0 requests and validates 1.0 envelopes. It does not accept a request
that declares 1.0 while carrying a 1.1 field — that is refused rather than ignored. A store
created by a different DDL revision is refused rather than migrated by guesswork; see
[`install.md`](install.md) for the supported path.

The reader model became configurable in 1.1, where 1.0 refused anything but the default.
That is a real behaviour change, recorded as such: what replaces the old hard refusal is
truthful provenance, not a silent loosening.

## Node and Python floors

The store needs `node:sqlite`, so the TypeScript side requires Node ≥22.22.3 (which is also
OpenClaw's own floor). Python requires ≥3.11.

## Automatic extraction is a prefix, not an answer

After wholly exhausted reader availability, the default escape hatch returns at most
2 KiB of the first source's exact bytes, independently of question/reader selectors.
It never returns the entire source. Useful evidence may be elsewhere; all other sources
and remaining bytes are explicitly omitted. This does not rescue malformed/citation-invalid
or weak valid answers. Disabling `reader.automatic_extract` or inspect restores error-only
recovery. Existing disclosure, output, scope and secret checks may also prevent extraction.
The bounded local inspection can add latency after a model timeout; failed attempt costs
remain accounted even when usage is unknown. See [full semantics](configuration.md#automatic-exact-extraction-after-reader-unavailability).
