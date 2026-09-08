/**
 * Question-driven read-only reader.
 *
 * Order of operations, and why:
 *
 * 1. Validate against the contract. A missing or blank question stops here, so the model
 *    invocation count for such a request is provably zero.
 * 2. Resolve every source handle in this scope and confirm the snapshot hash the caller
 *    named still matches. One unsafe source rejects the whole request rather than answering
 *    from the remaining ones. A refined question reuses that same immutable snapshot - it
 *    never silently recaptures the source.
 * 3. Plan chunks under the token budget before any call is made.
 * 4. Call the reader model once per chunk with the original question, at most two
 *    concurrently, with at most one transient retry that spends the same shared budget.
 * 5. Verify every citation against the snapshot, delete assertions that lost their
 *    evidence, and only then decide status/coverage.
 * 6. Attach truthful provenance and hand the result to the output guard.
 *
 * Every answer is labelled `model_derived` with `provenance.derived = true`: it is a
 * model's reading of the source, not the source. The provenance block keeps requested,
 * resolved and reported provider/model apart and states the strongest attribution the host
 * actually supports.
 *
 * A provider failure, a timeout, a malformed response, a citation failure or a provenance
 * failure leaves every handle valid. The failure envelope says so and names deterministic
 * next steps, so the caller retries or refines over the same snapshot instead of paying to
 * capture the source again.
 */
import {
  type ReaderCost,
  estimateTokens as accountingTokens,
  noReaderCost,
} from "./accounting.js";
import { Chunk, estimateTokens, planChunks } from "./chunking.js";
import {
  type Claim, CitationVerifier, normalizeClaims, referencedIds, renderClaims,
  stripUnsupportedAssertions, unpublishedMarkerIds,
} from "./citations.js";
import { Clock, Deadline, monotonicClock } from "./clock.js";
import {
  Citation, Coverage, Envelope, SourceHandle, buildEnvelope, errorEnvelope, isoExpiry,
  serializedBytes,
} from "./envelope.js";
import { ShuntError, isShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits, envelopeByteCap } from "./limits.js";
import { MetricsSink, nullMetrics } from "./metrics.js";
import {
  type Attribution,
  type Confidence,
  type ModelIdentity,
  NO_USAGE,
  type Provenance,
  type Usage,
  UNKNOWN_IDENTITY,
  enforceAttributionPolicy,
  identityKnown,
  mergeUsage,
  usageComplete,
  weakestAttribution,
  type AttributionPolicy,
} from "./provenance.js";
import {
  type CallInputBudget,
  ModelResponse, READER_SYSTEM_PROMPT, type ReaderProvider, buildUserMessage,
  providerTargetOf, responseAttribution, targetIdentity, transientProviderError,
} from "./provider.js";
import { SourceRegistry } from "./registry.js";
import { Snapshot, assertNoSecret } from "./snapshot.js";
import { type ReaderRequest, READ_OPERATIONS, validateRequest } from "./schema.js";
import { utf8Length } from "./textindex.js";

/** The coarsest legal locator, for an omission whose material had no narrower one. */
const WHOLE_SOURCE: Record<string, unknown> = { kind: "all" };

/**
 * Stands in for a legacy marker that names an id its own chunk never declared. Inside the
 * `[cN]` grammar, so it is still seen by `referencedIds`, and outside the allocated id
 * space, so it can never match a published citation.
 */
const UNMAPPABLE_MARKER = "[c0]";

/**
 * The one identity every answering call reported, or `null` when they differ.
 *
 * `null` is the honest answer for a mixed request, and it is deliberately also the answer
 * when one call named a model and another named nothing: publishing the one that did would
 * describe the whole answer by the half of it that could be identified. The caller
 * degrades the attribution status alongside it, so the envelope never carries a strong
 * status over an identity that covers only part of the work.
 */
function agreedIdentity(values: ModelIdentity[]): ModelIdentity | null {
  const first = values[0];
  if (first === undefined) return UNKNOWN_IDENTITY;
  for (const value of values.slice(1)) {
    if (value.provider !== first.provider || value.model !== first.model) return null;
  }
  return first;
}

/** `INVALID_MODEL_OUTPUT` details eligible for the one-shot format retry: a shape or
 * claims/citations *relationship* failure, never a content judgement. Retrying
 * `BAD_USAGE` would not fix a provider accounting bug, and retrying
 * `MODEL_OUTPUT_OVER_CAP` would not make the model write less - neither belongs here. */
const FORMAT_RETRY_DETAILS = new Set([
  "NOT_JSON", "NOT_OBJECT", "BAD_RESPONSE_SHAPE", "AMBIGUOUS_RESPONSE_SHAPE",
]);

interface ChunkOutcome {
  chunk: Chunk;
  /** The current contract: structurally valid `{text, citation_ids}` objects, chunk-local
   * ids. Populated only when this call's reply used the `claims` shape. */
  claims: Claim[];
  /** The legacy contract: raw prose the model marked up itself with `[cN]`. Populated
   * only when this call's reply used the `answer` shape - never both, an ambiguous reply
   * carrying both fails the call instead of guessing which one to trust. */
  legacyAnswer: string;
  citations: Array<Record<string, unknown>>;
  failedReason: string | null;
  calls: number;
  usageCompleteCalls: number;
  usage: Usage;
  /**
   * Usage reported by attempts whose output the reader never saw. Kept apart from
   * `usage` so a byte estimate can be topped up with it without ever double counting the
   * winner, whose output the reader *can* measure.
   */
  unseenUsage: Usage;
  promptBytes: number;
  completionBytes: number;
  attribution: { status: Attribution; confidence: Confidence };
  /** What the call that actually answered asked for. Not always the chain head: an
   * availability fallback answers as the candidate it advanced to, and publishing the
   * head's identity for that answer certifies a request that was never served. */
  requested: ModelIdentity;
  resolved: ModelIdentity;
  reported: ModelIdentity;
  /** Whether a provider response was ever seen for this chunk. The identity fields above
   * mean nothing without one, so aggregation reads this rather than `calls` - a call can
   * be started, billed and still return nothing. */
  responsesSeen: number;
  fallbackUsed: boolean;
  /** Claims the model wrote beyond `maxClaimsPerAnswer`. They are never read, so they are
   * material this request dropped, and the caller has to be told. */
  claimsOverCap: number;
}

/** What the session needs to finish the operation: an envelope plus its true cost. */
export interface ReaderResult {
  envelope: Envelope;
  provenance: Provenance;
  cost: ReaderCost;
  sourceIds: string[];
}

class InputTokenBudget {
  private spent = 0;

  constructor(private readonly maximum: number) {}

  spend(tokens: number): void {
    if (!Number.isSafeInteger(tokens) || tokens < 0 || this.spent + tokens > this.maximum) {
      throw new ShuntError("LIMIT_EXCEEDED", "REQUEST_OVER_TOKEN_CAP", false);
    }
    this.spent += tokens;
  }

  /**
   * A debit handle for one chunk's prompt, spendable once per physical call.
   *
   * `maxRequestInputTokens` bounds what one request may transmit, and the reader used to
   * debit it once per *invocation* - outside the provider chain. A composite provider then
   * sent the same prompt to two or three candidates on that single debit, so a chain of
   * three could transmit three times the request's ceiling. The chain debits through this
   * handle before it starts each extra candidate, so the count of debits equals the count
   * of physical calls, and a candidate whose prompt no longer fits is never started.
   */
  perCall(tokens: number): CallInputBudget {
    return { debitCall: (): void => this.spend(tokens) };
  }
}

/**
 * Accounting for exactly one physical invocation of the provider.
 *
 * Ordinary, late and cancelled outcomes all report through this object, and it takes the
 * first report and ignores every later one. Before it existed each branch did its own
 * partial bookkeeping - or none: a late response recorded its own usage but not the
 * repeated prompts or unseen billing behind it, and a cancellation that won the race
 * against the provider dropped a billed call entirely. Whether a physical call was
 * counted once, twice or not at all depended on when the deadline happened to fire.
 *
 * One physical invocation, one report. `record*` is safe to call from every path that
 * might be the one to notice the call is over.
 */
class AttemptLedger {
  private recorded = false;
  /**
   * Bytes one candidate's prompt occupies. Set once the prompt exists; a fallback re-sends
   * the same prompt to every candidate it tries, so each extra attempt costs this again.
   */
  perCallPromptBytes = 0;

  constructor(private readonly outcome: ChunkOutcome) {}

  /** What a returned response cost, whether or not its answer can be published. */
  recordSuccess(response: ModelResponse): void {
    if (this.recorded) return;
    this.recorded = true;
    const outcome = this.outcome;
    // An availability fallback may have taken several attempts inside this one call, and
    // every one of them reached a provider and was billed. `calls` was already incremented
    // once by the caller for the attempt it started.
    const extraAttempts = Math.max(0, (response.attempts ?? 1) - 1);
    outcome.calls += extraAttempts;
    outcome.promptBytes += this.perCallPromptBytes * extraAttempts;
    outcome.completionBytes += new TextEncoder().encode(response.text).length;
    // Output the reader never saw: a failed candidate returned no text to measure, so its
    // reported tokens are the only evidence of what it produced. Held apart from the
    // winner's bytes precisely so the two are never added twice.
    const unseen = response.billedFromFailedAttempts;
    if (unseen) outcome.unseenUsage = mergeUsage(outcome.unseenUsage, unseen);
    outcome.usage = mergeUsage(outcome.usage, response.usage);
    // A composite provider reports how many of its attempts supplied complete usage; a
    // plain one supplies one attempt, so the winner alone decides.
    outcome.usageCompleteCalls +=
      response.usageCompleteAttempts ?? (usageComplete(response.usage) ? 1 : 0);
  }

  /**
   * What a returned response cost when its *usage claim* cannot be trusted.
   *
   * A response whose usage metadata is malformed still reached the provider, still
   * transmitted the prompt and still came back carrying completion bytes we can measure
   * ourselves. Rejecting it through `recordFailure` threw all of that away: the error
   * carries no `billedUsage`, so a call that produced 1,200 bytes of text was published as
   * `output_tokens: 0` - the one direction this accounting must never err in.
   *
   * The response's own numbers are still refused; only what this core measured is kept,
   * and the completion measurement is clamped to the reply ceiling so a provider cannot
   * inflate the estimate by returning an unbounded body.
   */
  recordUntrustedUsage(response: unknown, limits: Limits): void {
    if (this.recorded) return;
    const candidate = response as ModelResponse | undefined;
    if (!candidate || typeof candidate.text !== "string") return;
    this.recorded = true;
    const outcome = this.outcome;
    const extraAttempts = Math.max(0, (candidate.attempts ?? 1) - 1);
    outcome.calls += extraAttempts;
    outcome.promptBytes += this.perCallPromptBytes * extraAttempts;
    outcome.completionBytes += Math.min(
      new TextEncoder().encode(candidate.text).length,
      limits.maxToolResultBytes,
    );
    // No usage is merged and no attempt is counted as usage-complete: the claim was
    // refused, so every attempt behind this response has unknown usage. That is what
    // `attempts_started` > `attempts_usage_complete` is for.
  }

  /** What a failed call cost. A rejected reply is still a paid call. */
  recordFailure(err: unknown): void {
    if (this.recorded) return;
    this.recorded = true;
    const outcome = this.outcome;
    const billed = (err as { billedUsage?: unknown })?.billedUsage;
    if (billed && typeof billed === "object") {
      outcome.usage = mergeUsage(outcome.usage, billed as Usage);
      // Nothing came back, so every attempt here is one whose output was never seen.
      outcome.unseenUsage = mergeUsage(outcome.unseenUsage, billed as Usage);
    }
    // The same aggregate as the success path: a chain that gave up still reports how many
    // of its candidates were billed and how many of those said what they cost.
    const reportedAttempts = (err as { usageCompleteAttempts?: number })?.usageCompleteAttempts;
    outcome.usageCompleteCalls +=
      reportedAttempts
      ?? (billed && typeof billed === "object" && usageComplete(billed as Usage) ? 1 : 0);
    // A composite provider may have made several calls inside this one invocation before
    // giving up. `calls` was incremented once by the caller for the invocation; the rest
    // are the ones the chain made and was billed for.
    const extraAttempts = Math.max(
      0,
      ((err as { internalAttempts?: number })?.internalAttempts ?? 1) - 1,
    );
    outcome.calls += extraAttempts;
    outcome.promptBytes += this.perCallPromptBytes * extraAttempts;
  }
}

export class Reader {
  private readonly verifier: CitationVerifier;

  constructor(
    private readonly registry: SourceRegistry,
    private readonly provider: ReaderProvider,
    private readonly limits: Limits = DEFAULT_LIMITS,
    private readonly clock: Clock = monotonicClock,
    private readonly metrics: MetricsSink = nullMetrics,
    private readonly policy: AttributionPolicy = "allow_unverified",
  ) {
    this.verifier = new CitationVerifier(registry, limits);
  }

  /**
   * Answer a read request. Resolves to the envelope, as it always has.
   *
   * This is the published signature from before the 1.1 revision. That revision changed
   * both the arity and the return type in place: callers reading `result.status` got a
   * `ReaderResult` instead of an envelope, and a caller passing an `AbortSignal` in the
   * fourth position had it silently dropped. Neither is a compatible change, so the
   * original contract is restored here and the richer record lives on
   * {@link answerDetailed}, which is additive.
   */
  async answer(
    sessionId: string,
    request: unknown,
    deadline?: Deadline,
    signal?: AbortSignal,
  ): Promise<Envelope> {
    return (await this.answerDetailed(sessionId, request, deadline, signal)).envelope;
  }

  /**
   * {@link answer} plus what the operation cost and what produced it.
   *
   * The session needs the cost and provenance record to write its accounting; callers who
   * only ever wanted the envelope keep using `answer`.
   */
  async answerDetailed(
    sessionId: string,
    request: unknown,
    deadline?: Deadline,
    signal?: AbortSignal,
    accountingId?: string,
  ): Promise<ReaderResult> {
    return this.answerInner(sessionId, request, {
      ...(deadline ? { deadline } : {}),
      ...(signal ? { signal } : {}),
      ...(accountingId !== undefined ? { accountingId } : {}),
    });
  }

  private async answerInner(
    sessionId: string,
    request: unknown,
    opts: { deadline?: Deadline; signal?: AbortSignal; accountingId?: string } = {},
  ): Promise<ReaderResult> {
    const requestId = readRequestId(request);
    const requestedDeadline = readRequestedDeadline(request, this.limits.requestDeadlineMs);
    const budget = opts.deadline ?? Deadline.start(this.clock, requestedDeadline, opts.signal);
    // Model calls are billed the moment they complete, but the request budget is checked
    // again at PUBLISH. A request that ran its calls and then ran out of time used to
    // report `noReaderCost()` - "no attempt was made" - so real spend vanished from the
    // session's accounting and every savings figure derived from it was overstated.
    // Whatever was actually spent before the failure is carried out.
    const spent: { cost: ReaderCost } = { cost: noReaderCost() };
    try {
      return await this.run(sessionId, request, requestId, budget, opts.accountingId, spent);
    } catch (err) {
      if (!isShuntError(err)) throw err;
      this.metrics.count("reader_error", { code: err.code });
      const provenance = this.failureProvenance(err, spent.cost.attemptsStarted);
      const envelopeOpts: Parameters<typeof errorEnvelope>[2] = {
        provenance,
        handlesValid: handlesSurvive(err),
      };
      if (opts.accountingId !== undefined) envelopeOpts.accountingId = opts.accountingId;
      return {
        envelope: errorEnvelope(requestId, err, envelopeOpts),
        provenance,
        cost: spent.cost,
        sourceIds: [],
      };
    }
  }

  private noOutputProvenance(): Provenance {
    return {
      derived: true,
      label: "no_model_output",
      attributionStatus: "not_applicable",
      attributionConfidence: "none",
      attributionPolicy: "not_applicable",
      attemptsStarted: 0,
      usageComplete: true,
      citationsMechanicallyVerified: true,
      requested: targetIdentity(providerTargetOf(this.provider)),
    };
  }

  /**
   * Provenance for a request that published nothing.
   *
   * `attemptsStarted` is not always zero: a request can complete its model calls and then
   * fail at PUBLISH, and reporting no attempts there would contradict the cost the same
   * envelope carries.
   */
  private failureProvenance(err: ShuntError, attemptsStarted = 0): Provenance {
    const unknownAttribution =
      err.code === "MODEL_ERROR"
      || err.code === "INVALID_MODEL_OUTPUT"
      || err.code === "PROVENANCE_UNAVAILABLE";
    return {
      derived: false,
      label: "no_model_output",
      attributionStatus: unknownAttribution ? "unknown" : "not_applicable",
      attributionConfidence: "none",
      attributionPolicy: this.policy,
      attemptsStarted,
      usageComplete: false,
      citationsMechanicallyVerified: true,
      requested: targetIdentity(providerTargetOf(this.provider)),
    };
  }

  private async run(
    sessionId: string,
    rawRequest: unknown,
    requestId: string,
    deadline: Deadline,
    accountingId: string | undefined,
    spent: { cost: ReaderCost } = { cost: noReaderCost() },
  ): Promise<ReaderResult> {
    const request = validateRequest(rawRequest, READ_OPERATIONS) as ReaderRequest;
    assertNoSecret(request.question, "QUESTION");

    deadline.check("RESOLVE");
    const selections: Array<{ sourceId: string; snapshot: Snapshot; selector: Record<string, unknown> }> = [];
    const handles: SourceHandle[] = [];
    const sourceIds: string[] = [];
    for (const source of request.sources) {
      const entry = this.registry.resolve(sessionId, source.source_id);
      if (entry.snapshot.snapshotId !== source.snapshot_id) {
        // A refined question must address the snapshot it was given. Recapturing here
        // would answer a new question about a different file under the old hash.
        throw new ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH", false);
      }
      sourceIds.push(entry.sourceId);
      const selector =
        source.selector["kind"] === "search"
          ? searchSelectorToLines(entry.snapshot, source.selector)
          : source.selector;
      selections.push({ sourceId: entry.sourceId, snapshot: entry.snapshot, selector });
      handles.push({
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        media_type: entry.snapshot.mediaType,
        bytes: entry.snapshot.bytesLen,
        expires_at: isoExpiry(entry.expiresAtEpoch),
      });
    }

    deadline.check("PLAN");
    // A search that matched nothing contributes no range; it must become NO_MATCH, not an
    // out-of-range planning error.
    const planned = selections.filter(
      (s) => !(s.selector["kind"] === "lines" && Number(s.selector["end"]) < Number(s.selector["start"])),
    );
    const plan = planChunks(planned, {
      maxChunks: request.budgets.max_chunks,
      limits: this.limits,
      question: request.question,
    });

    const coverage = new Coverage();
    coverage.plannedChunks = plan.chunks.length;
    for (const omission of plan.omitted) {
      coverage.omit(omission.source_id, omission.selector, omission.reason);
    }

    if (plan.chunks.length === 0) {
      // Nothing to read means nothing was generated: the answer is empty and the
      // provenance says no model output rather than claiming a derived answer.
      const complete = new Coverage();
      complete.complete = true;
      complete.upstreamTruncated = false;
      const provenance = this.noOutputProvenance();
      return {
        envelope: buildEnvelope({
          requestId,
          status: "ok",
          code: "NO_MATCH",
          coverage: complete,
          sources: handles,
          resultKind: "model_derived",
          provenance,
          ...(accountingId !== undefined ? { accountingId } : {}),
        }),
        provenance,
        cost: noReaderCost(),
        sourceIds,
      };
    }

    const outcomes = await this.runChunks(
      request.question,
      plan.chunks,
      deadline,
      new InputTokenBudget(this.limits.maxRequestInputTokens),
    );

    const allClaims: Claim[] = [];
    const legacyParts: string[] = [];
    const rawCitations: Array<Record<string, unknown>> = [];
    let totalCalls = 0;
    let usageCompleteCalls = 0;
    let usage: Usage = { method: "not_applicable" };
    let promptBytes = 0;
    let completionBytes = 0;
    let nextCitation = 1;
    let attribution: { status: Attribution; confidence: Confidence } = {
      status: "not_applicable",
      confidence: "none",
    };
    // Every answering call's own identity, kept apart until aggregation: one shared slot
    // filled by whichever chunk happened to report first hid divergence, which is exactly
    // what a provenance block must not do.
    const requestedSeen: ModelIdentity[] = [];
    const resolvedSeen: ModelIdentity[] = [];
    const reportedSeen: ModelIdentity[] = [];
    let fallbackUsed = false;
    let unseenUsage: Usage = { method: "not_applicable" };
    // Material this request produced and then dropped against a ceiling. Counted so an
    // answer that ends up empty can say *why* it is empty.
    let capDropped = 0;
    for (const outcome of outcomes) {
      totalCalls += outcome.calls;
      usageCompleteCalls += outcome.usageCompleteCalls;
      usage = mergeUsage(usage, outcome.usage);
      unseenUsage = mergeUsage(unseenUsage, outcome.unseenUsage);
      promptBytes += outcome.promptBytes;
      completionBytes += outcome.completionBytes;
      fallbackUsed = fallbackUsed || outcome.fallbackUsed;
      if (outcome.calls > 0) attribution = weakestAttribution(attribution, outcome.attribution);
      if (outcome.responsesSeen > 0) {
        requestedSeen.push(outcome.requested);
        resolvedSeen.push(outcome.resolved);
        reportedSeen.push(outcome.reported);
      }
      if (outcome.claimsOverCap > 0) {
        capDropped += outcome.claimsOverCap;
        coverage.omitOnce(outcome.chunk.sourceId, outcome.chunk.locator, "BUDGET_EXCEEDED");
      }
      if (outcome.failedReason) {
        coverage.omit(outcome.chunk.sourceId, outcome.chunk.locator, outcome.failedReason);
        continue;
      }
      coverage.processedChunks += 1;
      const namespaced = namespaceOutcome(outcome, nextCitation);
      nextCitation += namespaced.idsAllocated;
      allClaims.push(...namespaced.claims);
      if (namespaced.legacyAnswer) legacyParts.push(namespaced.legacyAnswer);
      rawCitations.push(...namespaced.citations);
    }
    // One answer, one identity - or none. Each side is published only when every answering
    // call agreed on it; a request whose calls disagree cannot be described by any single
    // value, and picking one would certify a model that produced part of the answer as the
    // model that produced all of it. Divergence also drops the attribution to `unknown`,
    // because a status is a claim *about* the requested identity and there is no longer one
    // to make it about.
    const agreedRequested = agreedIdentity(requestedSeen);
    const agreedResolved = agreedIdentity(resolvedSeen);
    const agreedReported = agreedIdentity(reportedSeen);
    if (agreedRequested === null || agreedResolved === null || agreedReported === null) {
      attribution = weakestAttribution(attribution, { status: "unknown", confidence: "none" });
    }
    // No answering call at all: the strongest truthful statement is what was asked for,
    // which is what the pre-1.1 envelope always published.
    const requested =
      requestedSeen.length === 0
        ? targetIdentity(providerTargetOf(this.provider))
        : (agreedRequested ?? UNKNOWN_IDENTITY);
    const resolved = agreedResolved ?? UNKNOWN_IDENTITY;
    const reported = agreedReported ?? UNKNOWN_IDENTITY;
    this.metrics.observe("reader_model_calls", totalCalls);
    this.metrics.observe("reader_attempts_usage_complete", usageCompleteCalls);

    const cost = readerCostOf({
      usage,
      unseenUsage,
      attempts: totalCalls,
      usageCompleteCalls,
      promptBytes,
      completionBytes,
      limits: this.limits,
    });
    // Visible to the error path from here on: a failure at PUBLISH must still report what
    // the completed calls cost.
    spent.cost = cost;

    const { verified, rejected } = this.verifyAll(sessionId, rawCitations);
    this.metrics.observe("citations_verified", verified.length, { result: "verified" });
    this.metrics.observe("citations_rejected", rejected, { result: "rejected" });

    // The citation ceiling is applied to a *prioritized* list, not to whatever order the
    // model happened to emit. Truncating arbitrarily lost twice over: the citation went,
    // and then every claim that referenced it went with it - so an answer could lose
    // material that would have fitted had the surviving citations been the ones anything
    // actually cited.
    const legacyAll = legacyParts.join(" ");
    const wanted = new Set<string>(referencedIds(legacyAll));
    for (const c of allClaims) for (const id of c.citation_ids) wanted.add(id);
    const prioritized = [
      ...verified.filter((c) => wanted.has(c.id)),
      ...verified.filter((c) => !wanted.has(c.id)),
    ];
    const allowed = prioritized.slice(0, this.limits.maxCitations);
    for (const citation of prioritized.slice(this.limits.maxCitations)) {
      capDropped += 1;
      coverage.omitOnce(
        String(citation.source_id ?? ""),
        (citation.locator as Record<string, unknown>) ?? WHOLE_SOURCE,
        "BUDGET_EXCEEDED",
      );
    }
    const allowedIds = new Set(allowed.map((c) => c.id));
    const byId = new Map(allowed.map((c) => [c.id, c] as const));
    let keptClaims = allClaims.filter(
      (c) => c.citation_ids.length > 0 && c.citation_ids.every((id) => allowedIds.has(id)),
    );
    let legacyAnswer = stripUnsupportedAssertions(legacyAll, allowedIds);
    let answer = renderAnswer(keptClaims, legacyAnswer);
    // Drop whole claims/sentences from the end until the render fits, rather than
    // truncating raw bytes: a byte cut can split a marker or a multi-byte character,
    // which is why the old pipeline had to strip a second time after truncating. Dropping
    // structured units instead never produces a half-written marker.
    //
    // Each drop is recorded. Silently shrinking the answer to fit `max_answer_bytes` and
    // then reporting `complete: true` told the caller the whole selection had been read
    // when part of the reading had just been deleted.
    const maxAnswer = Math.min(request.budgets.max_answer_bytes, this.limits.maxAnswerBytes);
    while (utf8Length(answer) > maxAnswer && (keptClaims.length > 0 || legacyAnswer.length > 0)) {
      let orphaned: string[];
      if (keptClaims.length > 0) {
        orphaned = [...(keptClaims[keptClaims.length - 1] as Claim).citation_ids];
        keptClaims = keptClaims.slice(0, -1);
      } else {
        const shorter = dropLastSentence(legacyAnswer);
        const stillThere = new Set(referencedIds(shorter));
        orphaned = referencedIds(legacyAnswer).filter((id) => !stillThere.has(id));
        legacyAnswer = shorter;
      }
      capDropped += 1;
      for (const id of [...new Set(orphaned)].sort()) {
        const citation = byId.get(id);
        if (!citation) continue;
        coverage.omitOnce(
          String(citation.source_id ?? ""),
          (citation.locator as Record<string, unknown>) ?? WHOLE_SOURCE,
          "BUDGET_EXCEEDED",
        );
      }
      answer = renderAnswer(keptClaims, legacyAnswer);
    }
    const usedIds = new Set<string>(referencedIds(legacyAnswer));
    for (const c of keptClaims) for (const id of c.citation_ids) usedIds.add(id);
    const citations = allowed.filter((c) => usedIds.has(c.id));

    const provenance: Provenance = {
      derived: true,
      label: totalCalls > 0 ? "model_generated_answer" : "no_model_output",
      attributionStatus: attribution.status,
      attributionConfidence: attribution.confidence,
      attributionPolicy: totalCalls > 0 ? this.policy : "not_applicable",
      attemptsStarted: totalCalls,
      usageComplete: totalCalls > 0 && usageCompleteCalls === totalCalls,
      citationsMechanicallyVerified: true,
      requested,
      resolved,
      reported,
      ...(totalCalls > 0 ? { fallbackUsed } : {}),
    };
    // Policy runs before publication so a refused attribution never ships an answer. The
    // failure keeps the provenance it was judged on: an operator needs to see the value
    // that contradicted the request, not a blank "unknown".
    try {
      enforceAttributionPolicy(provenance, this.policy);
    } catch (err) {
      if (!isShuntError(err)) throw err;
      this.metrics.count("reader_error", { code: err.code });
      const refused: Provenance = { ...provenance, derived: false, label: "no_model_output" };
      const envelopeOpts: Parameters<typeof errorEnvelope>[2] = {
        provenance: refused,
        sources: handles,
        handlesValid: true,
      };
      if (accountingId !== undefined) envelopeOpts.accountingId = accountingId;
      return {
        envelope: errorEnvelope(requestId, err, envelopeOpts),
        provenance: refused,
        cost,
        sourceIds,
      };
    }

    deadline.check("PUBLISH");
    const complete =
      coverage.omitted.length === 0 &&
      coverage.processedChunks === coverage.plannedChunks &&
      coverage.plannedChunks > 0;
    coverage.upstreamTruncated = false;

    // Publication invariant: every marker in the answer names a citation this envelope
    // publishes. `renderClaims` only ever writes ids the model supplied *and* the verifier
    // confirmed, and `normalizeClaims` drops a claim that wrote its own marker - so a
    // violation here is a program bug, not a model one, and it is refused rather than
    // published. Without it a forged `[c999]` in claim text shipped inside an answer whose
    // provenance said every citation had been mechanically verified.
    if (unpublishedMarkerIds(answer, new Set(citations.map((c) => c.id))).length > 0) {
      const failure = new ShuntError("CITATION_INVALID", "MARKER_NOT_PUBLISHED", false);
      this.metrics.count("reader_error", { code: failure.code });
      const failed: Provenance = { ...provenance, derived: false, label: "no_model_output" };
      const opts: Parameters<typeof errorEnvelope>[2] = {
        provenance: failed,
        sources: handles,
        handlesValid: true,
      };
      if (accountingId !== undefined) opts.accountingId = accountingId;
      return {
        envelope: errorEnvelope(requestId, failure, opts),
        provenance: failed,
        cost,
        sourceIds,
      };
    }

    if (answer.length === 0) {
      if (rejected > 0 && verified.length === 0 && rawCitations.length > 0) {
        // The handles are still valid and the caller is told so, so they have to be listed
        // too: "recovery.handles_valid: true" is only actionable if the envelope still says
        // which handles survived.
        const failure = new ShuntError("CITATION_INVALID", "NO_VALID_EVIDENCE", false);
        // Nothing survived verification, so nothing model-generated is published: the
        // failure is labelled not-derived while keeping the attribution facts.
        const failed: Provenance = { ...provenance, derived: false, label: "no_model_output" };
        const failureOpts: Parameters<typeof errorEnvelope>[2] = {
          provenance: failed,
          sources: handles,
          handlesValid: true,
        };
        if (accountingId !== undefined) failureOpts.accountingId = accountingId;
        return {
          envelope: errorEnvelope(requestId, failure, failureOpts),
          provenance: failed,
          cost,
          sourceIds,
        };
      }
      if (capDropped > 0) {
        // The sources did answer, and every piece of the answer hit a ceiling. NO_MATCH
        // would report that the sources held nothing, which is a different and untrue
        // statement; `LIMIT_EXCEEDED` names the real cause, and the coverage omissions
        // above say which source lost what. Checked *after* the verification branch, so a
        // request whose evidence never verified is still reported as a citation failure
        // rather than as a size one - the cap is not what emptied that answer.
        const failure = new ShuntError("LIMIT_EXCEEDED", "ANSWER_OVER_CAP", false);
        this.metrics.count("reader_error", { code: failure.code });
        const failed: Provenance = { ...provenance, derived: false, label: "no_model_output" };
        const opts: Parameters<typeof errorEnvelope>[2] = {
          provenance: failed,
          sources: handles,
          handlesValid: true,
        };
        if (accountingId !== undefined) opts.accountingId = accountingId;
        return {
          envelope: errorEnvelope(requestId, failure, opts),
          provenance: failed,
          cost,
          sourceIds,
        };
      }
      coverage.complete = complete;
      return {
        envelope: buildEnvelope({
          requestId,
          status: complete ? "ok" : "partial",
          code: "NO_MATCH",
          coverage,
          sources: handles,
          resultKind: "model_derived",
          provenance,
          ...(accountingId !== undefined ? { accountingId } : {}),
        }),
        provenance,
        cost,
        sourceIds,
      };
    }

    const answered = (text: string, cited: Citation[], ok: boolean): Envelope => {
      coverage.complete = ok;
      return buildEnvelope({
        requestId,
        status: ok ? "ok" : "partial",
        code: "ANSWERED",
        answer: text,
        citations: cited,
        coverage,
        sources: handles,
        resultKind: "model_derived",
        provenance,
        ...(accountingId !== undefined ? { accountingId } : {}),
      });
    };

    const fitted = this.fitToEnvelope(keptClaims, legacyAnswer, citations, coverage, answered);
    // The fit loop rewrites both halves, so the invariant is re-established on what is
    // actually published rather than on what was measured before trimming. A violation here
    // is a program bug and is reported as the citation failure it is - calling it
    // `ANSWER_OVER_ENVELOPE` would blame a size ceiling for a marker that names evidence
    // the envelope does not carry.
    const published = new Set(fitted.citations.map((c) => c.id));
    if (
      fitted.answer.length > 0
      && unpublishedMarkerIds(fitted.answer, published).length > 0
    ) {
      const failure = new ShuntError("CITATION_INVALID", "MARKER_NOT_PUBLISHED", false);
      this.metrics.count("reader_error", { code: failure.code });
      const failed: Provenance = { ...provenance, derived: false, label: "no_model_output" };
      const opts: Parameters<typeof errorEnvelope>[2] = {
        provenance: failed,
        sources: handles,
        handlesValid: true,
      };
      if (accountingId !== undefined) opts.accountingId = accountingId;
      return {
        envelope: errorEnvelope(requestId, failure, opts),
        provenance: failed,
        cost,
        sourceIds,
      };
    }
    if (fitted.answer.length === 0) {
      // Every piece of evidence had to go, so there is no supported answer left to publish.
      // Saying NO_MATCH here would claim the sources held nothing, which is a different and
      // untrue statement; the honest report is that it would not fit.
      const failure = new ShuntError("LIMIT_EXCEEDED", "ANSWER_OVER_ENVELOPE", false);
      this.metrics.count("reader_error", { code: failure.code });
      const failed: Provenance = { ...provenance, derived: false, label: "no_model_output" };
      const opts: Parameters<typeof errorEnvelope>[2] = {
        provenance: failed,
        sources: handles,
        handlesValid: true,
      };
      if (accountingId !== undefined) opts.accountingId = accountingId;
      return { envelope: errorEnvelope(requestId, failure, opts), provenance: failed, cost, sourceIds };
    }

    return {
      envelope: answered(fitted.answer, fitted.citations, complete && fitted.dropped === 0),
      provenance,
      cost,
      sourceIds,
    };
  }

  /**
   * Shrink an over-large answer until the output guard will accept it.
   *
   * Every field can be individually within its cap while the assembled envelope is not: a
   * full answer plus the maximum number of maximum-length quotes already exceeds the 16 KiB
   * envelope cap before JSON escaping is counted, and quotes are copied from source text, so
   * quote-dense sources escape wide. Without this the guard converts a good, fully verified
   * answer into a bare `LIMIT_EXCEEDED` - after the model call has been paid for, and with
   * no indication of what went wrong.
   *
   * Evidence is dropped largest-first rather than last-first: the model's citation order is
   * arbitrary, so trimming by position would make the surviving set depend on it, while
   * trimming by cost is deterministic and converges fastest. Each drop re-strips the
   * assertions it orphaned, which shrinks the answer too, so the loop re-measures between
   * drops and stops as soon as it fits.
   */
  private fitToEnvelope(
    claims: Claim[],
    legacyAnswer: string,
    citations: Citation[],
    coverage: Coverage,
    build: (text: string, cited: Citation[], ok: boolean) => Envelope,
  ): { answer: string; citations: Citation[]; dropped: number } {
    let keptClaims = claims;
    let legacy = legacyAnswer;
    let kept = citations;
    let dropped = 0;
    // One drop per pass, so this cannot run longer than there are citations.
    for (let pass = 0; pass <= citations.length; pass += 1) {
      const text = renderAnswer(keptClaims, legacy);
      const candidate = build(text, kept, false);
      // The same function the guard uses, not a constant: if a later revision moves
      // model_derived to a different cap, trimming must move with it rather than quietly
      // dropping evidence that would have fit.
      const cap = envelopeByteCap(candidate.result_kind, this.limits);
      if (serializedBytes(candidate) <= cap) {
        return { answer: text, citations: kept, dropped };
      }
      if (kept.length === 0) return { answer: "", citations: [], dropped };
      let victim = kept[0] as Citation;
      for (const candidate of kept) {
        if (serializedBytes(candidate) > serializedBytes(victim)) victim = candidate;
      }
      coverage.omit(
        String(victim.source_id ?? ""),
        (victim.locator as Record<string, unknown>) ?? { kind: "all" },
        "BUDGET_EXCEEDED",
      );
      dropped += 1;
      const keptIds = new Set(kept.filter((entry) => entry.id !== victim.id).map((e) => e.id));
      keptClaims = keptClaims.filter((c) => c.citation_ids.every((id) => keptIds.has(id)));
      legacy = stripUnsupportedAssertions(legacy, keptIds);
      const used = new Set<string>(referencedIds(legacy));
      for (const c of keptClaims) for (const id of c.citation_ids) used.add(id);
      kept = kept.filter((entry) => entry.id !== victim.id && used.has(entry.id));
    }
    return { answer: "", citations: [], dropped };
  }

  private async runChunks(
    question: string,
    chunks: Chunk[],
    deadline: Deadline,
    inputBudget: InputTokenBudget,
  ): Promise<ChunkOutcome[]> {
    const results: ChunkOutcome[] = new Array(chunks.length);
    let next = 0;
    const workers = Math.min(this.limits.maxConcurrentModelCalls, Math.max(1, chunks.length));
    const runWorker = async (): Promise<void> => {
      for (;;) {
        const index = next;
        next += 1;
        if (index >= chunks.length) return;
        results[index] = await this.runChunk(
          question,
          chunks[index] as Chunk,
          deadline,
          inputBudget,
        );
      }
    };
    await Promise.all(Array.from({ length: workers }, runWorker));
    return results;
  }

  private async runChunk(
    question: string,
    chunk: Chunk,
    deadline: Deadline,
    inputBudget: InputTokenBudget,
  ): Promise<ChunkOutcome> {
    const outcome: ChunkOutcome = {
      chunk,
      claims: [],
      legacyAnswer: "",
      citations: [],
      failedReason: null,
      calls: 0,
      usageCompleteCalls: 0,
      // An empty accumulator, not an attempt that reported nothing. `NO_USAGE` is
      // `unknown`, which is right for a *bridge* that returned no counts - but as a
      // starting value it poisoned the merge: `unknown + exact` is `unknown`, so exact
      // provider usage was downgraded to a byte estimate on *every* request, and the
      // `usageExact` distinction the accounting layer exists to make never survived.
      usage: { method: "not_applicable" },
      unseenUsage: { method: "not_applicable" },
      promptBytes: 0,
      completionBytes: 0,
      attribution: { status: "unknown", confidence: "none" },
      requested: UNKNOWN_IDENTITY,
      resolved: UNKNOWN_IDENTITY,
      reported: UNKNOWN_IDENTITY,
      responsesSeen: 0,
      fallbackUsed: false,
      claimsOverCap: 0,
    };
    // Two independent, separately bounded retry budgets: a transient provider failure and
    // a schema failure on the same chunk can each spend their own allotted retry, and both
    // may fire for the same chunk. "Independent" means neither budget can borrow the
    // other's slot - it does not mean only one of them may ever fire. The combined worst
    // case for one chunk is bounded by the sum of the two limits
    // (1 + maxTransientRetries + maxFormatRetries), never more.
    let transientUsed = 0;
    let formatUsed = 0;
    const maxAttempts = 1 + this.limits.maxTransientRetries + this.limits.maxFormatRetries;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      try {
        deadline.check("MODEL_CALL");
      } catch (err) {
        outcome.failedReason = isShuntError(err) && err.code === "TIMEOUT" ? "TIMEOUT" : "CANCELLED";
        return outcome;
      }
      // Hoisted so the failure path can charge the same per-call prompt for every attempt
      // a composite provider made before giving up.
      let perCallPromptBytes = 0;
      // One ledger per physical invocation, created before anything can fail. Ordinary,
      // late and cancelled outcomes all report through it, and it counts the first only.
      const ledger = new AttemptLedger(outcome);
      try {
        const user = buildUserMessage(question, chunk.text, chunk.locator);
        const perCallTokens =
          estimateTokens(READER_SYSTEM_PROMPT, this.limits) + estimateTokens(user, this.limits);
        // This debit covers the physical call this frame is about to start. A composite
        // provider that advances to another candidate re-sends the same prompt, and debits
        // again through the handle below before it does.
        inputBudget.spend(perCallTokens);
        outcome.calls += 1;
        // The same prompt is sent again by every candidate a fallback tries, so this is
        // per physical call, not per invocation. Counting it once per invocation halved
        // the input estimate of any two-candidate chain: four calls transmitting 3,448
        // bytes were estimated from 1,724.
        perCallPromptBytes =
          new TextEncoder().encode(READER_SYSTEM_PROMPT).length
          + new TextEncoder().encode(user).length;
        outcome.promptBytes += perCallPromptBytes;
        ledger.perCallPromptBytes = perCallPromptBytes;
        const response = await this.completeWithinDeadline({
          system: READER_SYSTEM_PROMPT,
          user,
          maxOutputTokens: this.limits.maxOutputTokensPerCall,
          ledger,
          inputBudget: inputBudget.perCall(perCallTokens),
        }, deadline);
        try {
          validateModelResponse(response, this.limits);
        } catch (err) {
          // The reply is refused, but it was delivered and billed. Its own numbers are
          // untrustworthy; the bytes this core measured are not.
          ledger.recordUntrustedUsage(response, this.limits);
          throw err;
        }
        ledger.recordSuccess(response);
        outcome.responsesSeen += 1;
        outcome.attribution = responseAttribution(response);
        outcome.requested = response.requested;
        outcome.resolved = response.resolved;
        outcome.reported = response.reported;
        outcome.fallbackUsed = outcome.fallbackUsed || response.fallbackUsed;
        if (outcome.attribution.status === "mismatch") {
          throw new ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", false);
        }
        const parsed = parseModelJson(response.text, this.limits.maxToolResultBytes);
        const hasClaims = "claims" in parsed;
        const hasLegacyAnswer = "answer" in parsed;
        if (hasClaims && hasLegacyAnswer) {
          // Both shapes at once is not "prefer one" - it is a response the program cannot
          // trust to say which one the model meant, so it is refused rather than silently
          // picking a side.
          throw new ShuntError("INVALID_MODEL_OUTPUT", "AMBIGUOUS_RESPONSE_SHAPE", false);
        }
        if (!Array.isArray(parsed["citations"])) {
          throw new ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", false);
        }
        const citationsLocal = normalizeCitations(parsed["citations"], chunk);
        if (hasClaims) {
          const rawClaims = parsed["claims"];
          if (!Array.isArray(rawClaims)) {
            throw new ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", false);
          }
          // Scan every claim the model wrote, not only the ones that survive structural
          // validation: a malformed claim (bad citation_ids) can still carry a secret in
          // its text, and a claim dropped later must still have been scanned first.
          for (const item of rawClaims.slice(0, this.limits.maxClaimsPerAnswer)) {
            if (typeof item === "object" && item !== null) {
              const text = (item as Record<string, unknown>)["text"];
              if (typeof text === "string") assertNoSecret(text, "ANSWER");
            }
          }
          // Claims past the ceiling are never read. That is dropped material, so it is
          // carried out and reported as an omission rather than silently disappearing
          // behind a `complete: true`.
          outcome.claimsOverCap = Math.max(
            0,
            rawClaims.length - this.limits.maxClaimsPerAnswer,
          );
          const validLocalIds = new Set(citationsLocal.map((c) => String(c["id"])));
          outcome.claims = normalizeClaims(rawClaims, validLocalIds, this.limits);
          outcome.citations = citationsLocal;
          return outcome;
        }
        if (!hasLegacyAnswer || typeof parsed["answer"] !== "string") {
          throw new ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", false);
        }
        assertNoSecret(parsed["answer"], "ANSWER");
        outcome.legacyAnswer = parsed["answer"];
        outcome.citations = citationsLocal;
        return outcome;
      } catch (err) {
        const safe = isShuntError(err) ? err : transientProviderError();
        // A rejected reply is still a paid call, and the ledger is where that is recorded.
        // It is a no-op when the call was already accounted for on the way out - a late
        // response, or a cancellation that won the race against the provider.
        ledger.recordFailure(safe);
        const canRetryTransient =
          safe.code === "MODEL_ERROR"
          && safe.retryable
          && transientUsed < this.limits.maxTransientRetries;
        const canRetryFormat =
          safe.code === "INVALID_MODEL_OUTPUT"
          && FORMAT_RETRY_DETAILS.has(safe.detail ?? "")
          && formatUsed < this.limits.maxFormatRetries;
        if ((canRetryTransient || canRetryFormat) && !deadline.expired()) {
          if (canRetryTransient) transientUsed += 1;
          else formatUsed += 1;
          continue;
        }
        outcome.failedReason =
          safe.code === "MODEL_ERROR" ? "MODEL_ERROR"
          : safe.code === "INVALID_MODEL_OUTPUT" ? "INVALID_MODEL_OUTPUT"
          : safe.code === "LIMIT_EXCEEDED" ? "BUDGET_EXCEEDED"
          : safe.code === "TIMEOUT" ? "TIMEOUT"
          : safe.code === "CANCELLED" ? "CANCELLED"
          : safe.code === "PROVENANCE_UNAVAILABLE" ? "PROVENANCE_UNAVAILABLE"
          : "CHUNK_FAILED";
        return outcome;
      }
    }
    outcome.failedReason = "CHUNK_FAILED";
    return outcome;
  }

  private async completeWithinDeadline(
    opts: {
      system: string;
      user: string;
      maxOutputTokens: number;
      /** Receives the cost of this physical call, whichever path notices it finished. */
      ledger: AttemptLedger;
      /** Debits the request's shared input budget for each extra candidate a composite
       * provider starts, so no chain transmits more than the request's ceiling. */
      inputBudget?: CallInputBudget | undefined;
    },
    deadline: Deadline,
  ): Promise<ModelResponse> {
    deadline.check("MODEL_CALL");
    const timeoutMs = deadline.subBudget(this.limits.modelCallDeadlineMs);
    if (timeoutMs <= 0) throw new ShuntError("TIMEOUT", "MODEL_CALL", true);

    const controller = new AbortController();
    const cancel = (): void => controller.abort();
    deadline.signal.addEventListener("abort", cancel, { once: true });
    let timer: ReturnType<typeof setTimeout> | undefined;
    let rejectCancelled: ((reason: ShuntError) => void) | undefined;
    const onDeadlineCancelled = (): void => {
      rejectCancelled?.(new ShuntError("CANCELLED", "MODEL_CALL", false));
    };
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new ShuntError("TIMEOUT", "MODEL_CALL", true));
      }, timeoutMs);
    });
    const cancelled = new Promise<never>((_resolve, reject) => {
      rejectCancelled = reject;
      if (deadline.signal.aborted) {
        reject(new ShuntError("CANCELLED", "MODEL_CALL", false));
        return;
      }
      deadline.signal.addEventListener("abort", onDeadlineCancelled, { once: true });
    });
    // The call's own settlement, captured whether or not it wins the race below. A
    // timeout or a cancellation can settle first and discard the provider's result
    // unread - and with it the attempts, prompts and billing of everything the chain
    // tried. The call was made and was billed either way, so its outcome is kept.
    let settled: { ok: true; value: ModelResponse } | { ok: false; err: unknown } | undefined;
    const call = this.provider
      .complete({
        system: opts.system,
        user: opts.user,
        maxOutputTokens: opts.maxOutputTokens,
        timeoutMs,
        signal: controller.signal,
        // Only a provider that fans out reads this. It debits the request's shared input
        // budget before every extra candidate it starts, so the number of debits equals
        // the number of physical calls rather than the number of invocations.
        ...(opts.inputBudget ? { inputBudget: opts.inputBudget } : {}),
      })
      .then(
        (value) => {
          settled = { ok: true, value };
          return value;
        },
        (err: unknown) => {
          settled = { ok: false, err };
          throw err;
        },
      );
    // An unobserved rejection is a process-level warning in Node; this arm exists only so
    // the promise is always handled. The real handling is `settled` above.
    call.catch(() => undefined);
    try {
      const response = await Promise.race([call, timeout, cancelled]);
      try {
        deadline.check("MODEL_CALL");
      } catch (err) {
        // Too late to publish, but the provider was already paid. Record what the call
        // cost before refusing its answer; dropping the response wholesale made real
        // spend disappear from the session's accounting.
        //
        // A composite response is several physical calls, and lateness does not merge
        // them: counting it as one attempt reported a two-call fallback as one started
        // and one usage-complete attempt, contradicting the aggregate usage recorded
        // beside it. The ordinary path's accounting is the accounting used here.
        opts.ledger.recordSuccess(response);
        throw err;
      }
      return response;
    } catch (err) {
      // The race was lost to a timeout or a cancellation. Give the call one macrotask to
      // deliver a result it has already produced - enough for a settled promise, never
      // enough to wait on one still in flight, which is the same rule Python's
      // non-blocking queue read follows: delivered but unread is counted, in flight is
      // not. Without it a cancellation landing between the provider failing and this
      // frame seeing it reported zero usage-complete attempts for a call billed 5/3.
      if (settled === undefined) await new Promise<void>((resolve) => setTimeout(resolve, 0));
      if (settled?.ok === true) opts.ledger.recordSuccess(settled.value);
      else if (settled?.ok === false) opts.ledger.recordFailure(settled.err);
      throw err;
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      deadline.signal.removeEventListener("abort", cancel);
      deadline.signal.removeEventListener("abort", onDeadlineCancelled);
    }
  }

  private verifyAll(
    sessionId: string,
    citations: Array<Record<string, unknown>>,
  ): { verified: Citation[]; rejected: number } {
    const verified: Citation[] = [];
    const seen = new Set<string>();
    let rejected = 0;
    for (const citation of citations) {
      const result = this.verifier.verify(sessionId, citation);
      if (!result.verified) {
        rejected += 1;
        continue;
      }
      const id = String(citation["id"]);
      if (seen.has(id)) continue;
      seen.add(id);
      verified.push({ ...(citation as unknown as Citation), verified: true });
    }
    return { verified, rejected };
  }
}

function readRequestId(request: unknown): string {
  if (typeof request === "object" && request !== null) {
    const candidate = (request as Record<string, unknown>)["request_id"];
    if (typeof candidate === "string" && /^[A-Za-z0-9_.:-]{1,64}$/.test(candidate)) {
      return candidate;
    }
  }
  return "req_unknown";
}

function readRequestedDeadline(request: unknown, maximum: number): number {
  if (typeof request !== "object" || request === null) return maximum;
  const budgets = (request as Record<string, unknown>)["budgets"];
  if (typeof budgets !== "object" || budgets === null) return maximum;
  const value = (budgets as Record<string, unknown>)["deadline_ms"];
  return Number.isSafeInteger(value) && Number(value) > 0
    ? Math.min(Number(value), maximum)
    : maximum;
}

function parseModelJson(text: string, maxBytes: number): Record<string, unknown> {
  if (new TextEncoder().encode(text).length > maxBytes) {
    throw new ShuntError("INVALID_MODEL_OUTPUT", "MODEL_OUTPUT_OVER_CAP", false);
  }
  let stripped = text.trim();
  if (stripped.startsWith("```")) {
    stripped = stripped.slice(stripped.indexOf("\n") + 1);
    if (stripped.endsWith("```")) stripped = stripped.slice(0, -3);
  }
  let value: unknown;
  try {
    value = JSON.parse(stripped);
  } catch {
    throw new ShuntError("INVALID_MODEL_OUTPUT", "NOT_JSON", false);
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ShuntError("INVALID_MODEL_OUTPUT", "NOT_OBJECT", false);
  }
  return value as Record<string, unknown>;
}

/**
 * Rebuild each citation from trusted chunk metadata. Only the id, the addressed range and
 * the quote come from the model; source and snapshot always come from the chunk, and
 * `verified` is never taken from the model.
 */
function normalizeCitations(raw: unknown, chunk: Chunk): Array<Record<string, unknown>> {
  if (!Array.isArray(raw)) return [];
  const out: Array<Record<string, unknown>> = [];
  for (const item of raw.slice(0, 64)) {
    if (typeof item !== "object" || item === null) continue;
    const rec = item as Record<string, unknown>;
    const id = rec["id"];
    const quote = rec["quote"];
    if (typeof id !== "string" || !/^c\d{1,3}$/.test(id)) continue;
    if (typeof quote !== "string" || quote.length === 0 || !chunk.text.includes(quote)) {
      out.push(invalidCitation(id, chunk));
      continue;
    }
    const locator = locatorFor(rec, chunk);
    if (!locator) {
      out.push(invalidCitation(id, chunk));
      continue;
    }
    out.push({
      id,
      source_id: chunk.sourceId,
      snapshot_id: chunk.snapshotId,
      locator,
      quote,
    });
  }
  return out;
}

function invalidCitation(id: string, chunk: Chunk): Record<string, unknown> {
  return {
    id,
    source_id: chunk.sourceId,
    snapshot_id: chunk.snapshotId,
    locator: chunk.locator,
    quote: "",
  };
}

function locatorFor(item: Record<string, unknown>, chunk: Chunk): Record<string, unknown> | null {
  if (chunk.locator["kind"] === "lines") {
    const start = item["line_start"];
    const end = item["line_end"] ?? start;
    if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
    if (
      Number(start) < Number(chunk.locator["start"])
      || Number(end) > Number(chunk.locator["end"])
      || Number(end) < Number(start)
    ) return null;
    return { kind: "lines", start, end };
  }
  const start = item["record_start"] ?? item["line_start"];
  const end = item["record_end"] ?? item["line_end"] ?? start;
  if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
  if (
    Number(start) < Number(chunk.locator["start"])
    || Number(end) > Number(chunk.locator["end"])
    || Number(end) < Number(start)
  ) return null;
  return { kind: "records", pointer: chunk.locator["pointer"] ?? "", start, end };
}

/**
 * Shape and bounds only. *Which* model answered is a provenance question, not a validation
 * one: it is classified truthfully and then judged by the configured policy, rather than
 * being asserted here from what we happened to request.
 */
function validateModelResponse(response: ModelResponse, limits: Limits): void {
  if (typeof response !== "object" || response === null || typeof response.text !== "string") {
    throw new ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", false);
  }
  const usage = response.usage;
  if (typeof usage !== "object" || usage === null) {
    throw new ShuntError("INVALID_MODEL_OUTPUT", "BAD_USAGE", false);
  }
  // Ceilings are per *call*, and this usage may be the sum of several. Each physical call
  // is already bounded where it is unpacked - `HostBridgeProvider` rejects an out-of-range
  // count against the same limits before it ever reaches an aggregate - so applying the
  // single-call ceiling again to the sum rejected valid work: two attempts of 1,500 output
  // tokens each are individually legal and totalled 3,000 against a 2,048 ceiling, and the
  // fallback winner was refused as `BAD_USAGE` with no answer returned. Aggregate
  // bookkeeping must not change availability.
  //
  // The bound scales with the attempts the total covers, so it still catches a count no
  // sequence of legal calls could have produced. Per-call validation is untouched.
  const attempts = Math.max(1, response.attempts ?? 1);
  const bounds: Array<[number | undefined, number]> = [
    [usage.inputTokens, limits.maxRequestInputTokens * attempts],
    [usage.outputTokens, limits.maxOutputTokensPerCall * attempts],
    [usage.cacheTokens, limits.maxRequestInputTokens * attempts],
  ];
  for (const [value, maximum] of bounds) {
    if (value === undefined) continue;
    if (!Number.isSafeInteger(value) || value < 0 || value > maximum) {
      throw new ShuntError("INVALID_MODEL_OUTPUT", "BAD_USAGE", false);
    }
  }
}

/** Only a failure of the handle itself invalidates it. */
function handlesSurvive(err: ShuntError): boolean {
  return !["SOURCE_EXPIRED", "SOURCE_CHANGED", "STORE_FAILED", "UNSAFE_SOURCE"].includes(err.code);
}

/** Exact provider usage wins; otherwise a named deterministic estimate. */
function readerCostOf(input: {
  usage: Usage;
  /** Usage reported by attempts whose output was never seen; see {@link ChunkOutcome}. */
  unseenUsage?: Usage;
  attempts: number;
  usageCompleteCalls: number;
  promptBytes: number;
  completionBytes: number;
  limits: Limits;
}): ReaderCost {
  if (input.attempts === 0) return noReaderCost();
  // `exact` is a claim about the whole request, not about whichever attempt won. A chain
  // whose first candidate failed without reporting and whose second succeeded with exact
  // counts merged that winner's usage into an empty accumulator, so the total looked
  // complete and came back `exact` - while the same record said one of two attempts had
  // reported. Every started attempt has to have reported for the total to be exact, which
  // is the rule the Python core already applied to the same schedule.
  if (usageComplete(input.usage) && input.usageCompleteCalls === input.attempts) {
    return {
      inputTokens: input.usage.inputTokens,
      outputTokens: input.usage.outputTokens,
      cacheTokens: input.usage.cacheTokens,
      method: "exact",
      attemptsStarted: input.attempts,
      attemptsUsageComplete: input.usageCompleteCalls,
    };
  }
  // The estimate covers every physical call: `promptBytes` already includes each fallback
  // attempt's prompt, and the output side adds what attempts the reader never saw reported
  // they produced. Reporting zero output for an all-failure chain that was billed for it
  // understated real spend, which is the direction this accounting must never err in.
  // The two populations are disjoint by construction - `completionBytes` is text the
  // reader received, `unseenUsage` is attempts it did not - so nothing is counted twice.
  const unseenOutput = input.unseenUsage?.outputTokens ?? 0;
  return {
    inputTokens: accountingTokens(input.promptBytes, input.limits),
    outputTokens: accountingTokens(input.completionBytes, input.limits) + unseenOutput,
    cacheTokens: undefined,
    method: "bytes_div_4",
    attemptsStarted: input.attempts,
    attemptsUsageComplete: input.usageCompleteCalls,
  };
}

/**
 * Give one chunk's local `cN` ids a slice of the answer's global id space.
 *
 * A claim's `citation_ids` are remapped through the same table built from this chunk's own
 * `citations` array - the same table {@link normalizeClaims} already checked them against,
 * so every id here is guaranteed present and the remap can never drop one.
 */
function namespaceOutcome(
  outcome: ChunkOutcome,
  firstId: number,
): {
  claims: Claim[];
  legacyAnswer: string;
  citations: Array<Record<string, unknown>>;
  idsAllocated: number;
} {
  const names = new Map<string, string>();
  const citations: Array<Record<string, unknown>> = [];
  for (const citation of outcome.citations) {
    const local = String(citation["id"]);
    let global = names.get(local);
    if (!global) {
      global = `c${firstId + names.size}`;
      names.set(local, global);
    }
    citations.push({ ...citation, id: global });
  }
  const claims: Claim[] = outcome.claims.map((claim) => ({
    text: claim.text,
    citation_ids: claim.citation_ids.map((id) => names.get(id) as string),
  }));
  const legacyAnswer = outcome.legacyAnswer.replace(/\[(c\d{1,3})\]/g, (_whole, local: string) => {
    const global = names.get(local);
    // A marker naming an id this chunk never declared cannot be remapped, and leaving it
    // alone let it collide with a *different* chunk's global id: chunk 2 writing `[c2]`
    // for evidence it never declared was published as chunk 1's verified citation c2.
    // Global ids are allocated from c1 upwards, so `c0` can never be one - the sentence
    // carrying it fails the subset test in `stripUnsupportedAssertions` and is dropped,
    // which is the fail-closed answer.
    return global ? `[${global}]` : UNMAPPABLE_MARKER;
  });
  return { claims, legacyAnswer, citations, idsAllocated: names.size };
}

/**
 * The one place the two published shapes are joined into the public `answer` field.
 *
 * Order is deliberate: rendered claims first, then whatever legacy prose survived - a
 * request mixing both shapes across its chunks (a stale cached response alongside a
 * current one, say) still reads as one coherent answer rather than interleaving.
 */
function renderAnswer(claims: Claim[], legacyAnswer: string): string {
  const parts = [renderClaims(claims), legacyAnswer].filter((p) => p.length > 0);
  return parts.join(" ").trim();
}

/** Drop the last legacy sentence, for the same byte-fit loop that drops claims. */
function dropLastSentence(text: string): string {
  if (text.trim().length === 0) return "";
  const parts = text.trim().split(/(?<=[.!?。！？\n])\s+/);
  return parts.slice(0, -1).join(" ").trim();
}

/**
 * Resolve a bounded literal search to the line range that actually matched. Only literal
 * patterns are accepted, so match time is linear and no supplied regex can be made to
 * backtrack.
 */
function searchSelectorToLines(
  snapshot: Snapshot,
  selector: Record<string, unknown>,
): Record<string, unknown> {
  const pattern = String(selector["pattern"] ?? "");
  const limit = Number(selector["max_matches"] ?? 0);
  const hits: number[] = [];
  for (let ordinal = 1; ordinal <= snapshot.lineCount; ordinal += 1) {
    let line: string;
    try {
      line = snapshot.lineIndex.lineText(ordinal);
    } catch {
      continue;
    }
    if (line.includes(pattern)) {
      hits.push(ordinal);
      if (hits.length >= limit) break;
    }
  }
  if (hits.length === 0) return { kind: "lines", start: 1, end: 0 };
  return { kind: "lines", start: Math.min(...hits), end: Math.max(...hits) };
}
