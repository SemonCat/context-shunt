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
import { CitationVerifier, referencedIds, stripUnsupportedAssertions } from "./citations.js";
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
  ModelResponse, READER_SYSTEM_PROMPT, type ReaderProvider, buildUserMessage,
  providerTargetOf, responseAttribution, targetIdentity, transientProviderError,
} from "./provider.js";
import { SourceRegistry } from "./registry.js";
import { Snapshot, assertNoSecret } from "./snapshot.js";
import { type ReaderRequest, READ_OPERATIONS, validateRequest } from "./schema.js";
import { capBytes } from "./textindex.js";

interface ChunkOutcome {
  chunk: Chunk;
  answer: string;
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
  resolved: ModelIdentity;
  reported: ModelIdentity;
  fallbackUsed: boolean;
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

    const answers: string[] = [];
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
    let resolved: ModelIdentity = UNKNOWN_IDENTITY;
    let reported: ModelIdentity = UNKNOWN_IDENTITY;
    let fallbackUsed = false;
    let unseenUsage: Usage = { method: "not_applicable" };
    for (const outcome of outcomes) {
      totalCalls += outcome.calls;
      usageCompleteCalls += outcome.usageCompleteCalls;
      usage = mergeUsage(usage, outcome.usage);
      unseenUsage = mergeUsage(unseenUsage, outcome.unseenUsage);
      promptBytes += outcome.promptBytes;
      completionBytes += outcome.completionBytes;
      fallbackUsed = fallbackUsed || outcome.fallbackUsed;
      if (outcome.calls > 0) {
        attribution = weakestAttribution(attribution, outcome.attribution);
        if (!identityKnown(resolved)) resolved = outcome.resolved;
        if (!identityKnown(reported)) reported = outcome.reported;
      }
      if (outcome.failedReason) {
        coverage.omit(outcome.chunk.sourceId, outcome.chunk.locator, outcome.failedReason);
        continue;
      }
      coverage.processedChunks += 1;
      const namespaced = namespaceOutcome(outcome, nextCitation);
      nextCitation += namespaced.idsAllocated;
      if (namespaced.answer) answers.push(namespaced.answer);
      rawCitations.push(...namespaced.citations);
    }
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

    const allowed = verified.slice(0, this.limits.maxCitations);
    const allowedIds = new Set(allowed.map((c) => c.id));
    let answer = stripUnsupportedAssertions(answers.join(" "), allowedIds);
    answer = capBytes(answer, Math.min(request.budgets.max_answer_bytes, this.limits.maxAnswerBytes));
    // Byte truncation can remove a marker or split an assertion. Re-run the deterministic
    // evidence filter on the exact bytes that will be published.
    answer = stripUnsupportedAssertions(answer, allowedIds);
    const usedIds = new Set(referencedIds(answer));
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
      requested: targetIdentity(providerTargetOf(this.provider)),
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

    const fitted = this.fitToEnvelope(answer, citations, coverage, answered);
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
    answer: string,
    citations: Citation[],
    coverage: Coverage,
    build: (text: string, cited: Citation[], ok: boolean) => Envelope,
  ): { answer: string; citations: Citation[]; dropped: number } {
    let text = answer;
    let kept = citations;
    let dropped = 0;
    // One drop per pass, so this cannot run longer than there are citations.
    for (let pass = 0; pass <= citations.length; pass += 1) {
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
      const survivors = kept.filter((entry) => entry.id !== victim.id);
      text = stripUnsupportedAssertions(text, new Set(survivors.map((entry) => entry.id)));
      const used = new Set(referencedIds(text));
      kept = survivors.filter((entry) => used.has(entry.id));
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
      answer: "",
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
      resolved: UNKNOWN_IDENTITY,
      reported: UNKNOWN_IDENTITY,
      fallbackUsed: false,
    };
    const attempts = 1 + this.limits.maxTransientRetries;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        deadline.check("MODEL_CALL");
      } catch (err) {
        outcome.failedReason = isShuntError(err) && err.code === "TIMEOUT" ? "TIMEOUT" : "CANCELLED";
        return outcome;
      }
      // Hoisted so the failure path can charge the same per-call prompt for every attempt
      // a composite provider made before giving up.
      let perCallPromptBytes = 0;
      try {
        const user = buildUserMessage(question, chunk.text, chunk.locator);
        inputBudget.spend(
          estimateTokens(READER_SYSTEM_PROMPT, this.limits) + estimateTokens(user, this.limits),
        );
        outcome.calls += 1;
        // The same prompt is sent again by every candidate a fallback tries, so this is
        // per physical call, not per invocation. Counting it once per invocation halved
        // the input estimate of any two-candidate chain: four calls transmitting 3,448
        // bytes were estimated from 1,724.
        perCallPromptBytes =
          new TextEncoder().encode(READER_SYSTEM_PROMPT).length
          + new TextEncoder().encode(user).length;
        outcome.promptBytes += perCallPromptBytes;
        const response = await this.completeWithinDeadline({
          system: READER_SYSTEM_PROMPT,
          user,
          maxOutputTokens: this.limits.maxOutputTokensPerCall,
          outcome,
        }, deadline);
        validateModelResponse(response, this.limits);
        // An availability fallback may have taken several attempts inside this one call,
        // and every one of them reached a provider and was billed. `calls` was already
        // incremented once above for the attempt we started.
        const extraAttempts = Math.max(0, (response.attempts ?? 1) - 1);
        outcome.calls += extraAttempts;
        outcome.promptBytes += perCallPromptBytes * extraAttempts;
        outcome.completionBytes += new TextEncoder().encode(response.text).length;
        // Output the reader never saw: a failed candidate returned no text to measure, so
        // its reported tokens are the only evidence of what it produced. Held apart from
        // the winner's bytes precisely so the two are never added twice.
        const unseen = response.billedFromFailedAttempts;
        if (unseen) outcome.unseenUsage = mergeUsage(outcome.unseenUsage, unseen);
        outcome.usage = mergeUsage(outcome.usage, response.usage);
        // A composite provider reports how many of its attempts supplied complete usage;
        // a plain one supplies one attempt, so the winner alone decides.
        outcome.usageCompleteCalls +=
          response.usageCompleteAttempts ?? (usageComplete(response.usage) ? 1 : 0);
        outcome.attribution = responseAttribution(response);
        outcome.resolved = response.resolved;
        outcome.reported = response.reported;
        outcome.fallbackUsed = outcome.fallbackUsed || response.fallbackUsed;
        if (outcome.attribution.status === "mismatch") {
          throw new ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", false);
        }
        const parsed = parseModelJson(response.text, this.limits.maxToolResultBytes);
        if (typeof parsed["answer"] !== "string" || !Array.isArray(parsed["citations"])) {
          throw new ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", false);
        }
        assertNoSecret(parsed["answer"], "ANSWER");
        outcome.answer = parsed["answer"];
        outcome.citations = normalizeCitations(parsed["citations"], chunk);
        return outcome;
      } catch (err) {
        const safe = isShuntError(err) ? err : transientProviderError();
        // A rejected reply is still a paid call. When the bridge could say what it was
        // billed, that travels on the error and is recorded here, so an unusable answer
        // costs the truth rather than an estimate.
        const billed = (safe as { billedUsage?: unknown }).billedUsage;
        if (billed && typeof billed === "object") {
          outcome.usage = mergeUsage(outcome.usage, billed as Usage);
        }
        // The same aggregate as the success path: a chain that gave up still reports how
        // many of its candidates were billed and how many of those said what they cost.
        const reportedAttempts = (safe as { usageCompleteAttempts?: number })
          .usageCompleteAttempts;
        outcome.usageCompleteCalls +=
          reportedAttempts
          ?? (billed && typeof billed === "object" && usageComplete(billed as Usage) ? 1 : 0);
        // Nothing came back, so every attempt here is one whose output was never seen.
        if (billed && typeof billed === "object") {
          outcome.unseenUsage = mergeUsage(outcome.unseenUsage, billed as Usage);
        }
        outcome.promptBytes +=
          perCallPromptBytes
          * Math.max(0, ((safe as { internalAttempts?: number }).internalAttempts ?? 1) - 1);
        // A composite provider may have made several calls inside this one invocation
        // before giving up. `calls` was incremented once above for the invocation; the
        // rest are the ones the chain made and was billed for.
        const internal = (safe as { internalAttempts?: number }).internalAttempts ?? 1;
        outcome.calls += Math.max(0, internal - 1);
        if (
          safe.code === "MODEL_ERROR"
          && safe.retryable
          && attempt + 1 < attempts
          && !deadline.expired()
        ) continue;
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
      /** Receives the cost of a call that finished too late to publish. */
      outcome: ChunkOutcome;
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
    try {
      const response = await Promise.race([
        this.provider.complete({
          system: opts.system,
          user: opts.user,
          maxOutputTokens: opts.maxOutputTokens,
          timeoutMs,
          signal: controller.signal,
        }),
        timeout,
        cancelled,
      ]);
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
        // beside it. The same metadata the ordinary success path consumes is consumed
        // here.
        opts.outcome.usage = mergeUsage(opts.outcome.usage, response.usage);
        opts.outcome.usageCompleteCalls +=
          response.usageCompleteAttempts ?? (usageComplete(response.usage) ? 1 : 0);
        opts.outcome.calls += Math.max(0, (response.attempts ?? 1) - 1);
        opts.outcome.completionBytes += new TextEncoder().encode(response.text).length;
        throw err;
      }
      return response;
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

function namespaceOutcome(
  outcome: ChunkOutcome,
  firstId: number,
): { answer: string; citations: Array<Record<string, unknown>>; idsAllocated: number } {
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
  const answer = outcome.answer.replace(/\[(c\d{1,3})\]/g, (whole, local: string) => {
    const global = names.get(local);
    return global ? `[${global}]` : whole;
  });
  return { answer, citations, idsAllocated: names.size };
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
