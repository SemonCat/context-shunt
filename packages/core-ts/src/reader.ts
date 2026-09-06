/**
 * Question-driven read-only reader.
 *
 * Order of operations, and why:
 *
 * 1. Validate against the v1 contract. A missing or blank question stops here, so the
 *    model invocation count for such a request is provably zero.
 * 2. Resolve every source handle in this session and confirm the snapshot hash the caller
 *    named still matches. One unsafe source rejects the whole request rather than
 *    answering from the remaining ones.
 * 3. Plan chunks under the token budget before any call is made.
 * 4. Call Luna once per chunk with the original question, at most two concurrently, with
 *    at most one transient retry that spends the same shared budget.
 * 5. Verify every citation against the snapshot, delete assertions that lost their
 *    evidence, and only then decide status/coverage.
 * 6. Hand the result to the output guard.
 */
import { Chunk, planChunks } from "./chunking.js";
import { CitationVerifier, referencedIds, stripUnsupportedAssertions } from "./citations.js";
import { Clock, Deadline, monotonicClock } from "./clock.js";
import {
  Citation, Coverage, Envelope, SourceHandle, buildEnvelope, errorEnvelope, isoExpiry,
} from "./envelope.js";
import { ShuntError, isShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits, READER_MODEL } from "./limits.js";
import { MetricsSink, nullMetrics } from "./metrics.js";
import { LunaProvider, READER_SYSTEM_PROMPT, buildUserMessage } from "./provider.js";
import { SourceRegistry } from "./registry.js";
import { Snapshot, assertNoSecret } from "./snapshot.js";
import { ReaderRequest, validateRequest } from "./schema.js";
import { capBytes } from "./textindex.js";

interface ChunkOutcome {
  chunk: Chunk;
  answer: string;
  citations: Array<Record<string, unknown>>;
  failedReason: string | null;
  calls: number;
  inputTokens: number;
  outputTokens: number;
}

export class Reader {
  private readonly verifier: CitationVerifier;

  constructor(
    private readonly registry: SourceRegistry,
    private readonly provider: LunaProvider,
    private readonly limits: Limits = DEFAULT_LIMITS,
    private readonly clock: Clock = monotonicClock,
    private readonly metrics: MetricsSink = nullMetrics,
  ) {
    this.verifier = new CitationVerifier(registry, limits);
  }

  async answer(sessionId: string, request: unknown, deadline?: Deadline): Promise<Envelope> {
    const requestId = readRequestId(request);
    const budget = deadline ?? Deadline.start(this.clock, this.limits.requestDeadlineMs);
    try {
      return await this.run(sessionId, request, requestId, budget);
    } catch (err) {
      if (!isShuntError(err)) throw err;
      this.metrics.count("reader_error", { code: err.code });
      return errorEnvelope(requestId, err);
    }
  }

  private async run(
    sessionId: string,
    rawRequest: unknown,
    requestId: string,
    deadline: Deadline,
  ): Promise<Envelope> {
    const request: ReaderRequest = validateRequest(rawRequest);
    assertNoSecret(request.question, "QUESTION");

    deadline.check("RESOLVE");
    const selections: Array<{ sourceId: string; snapshot: Snapshot; selector: Record<string, unknown> }> = [];
    const handles: SourceHandle[] = [];
    for (const source of request.sources) {
      const entry = this.registry.resolve(sessionId, source.source_id);
      if (entry.snapshot.snapshotId !== source.snapshot_id) {
        throw new ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH", false);
      }
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
    });

    const coverage = new Coverage();
    coverage.plannedChunks = plan.chunks.length;
    for (const omission of plan.omitted) {
      coverage.omit(omission.source_id, omission.selector, omission.reason);
    }

    if (plan.chunks.length === 0) {
      const complete = new Coverage();
      complete.complete = true;
      complete.upstreamTruncated = false;
      return buildEnvelope({
        requestId,
        status: "ok",
        code: "NO_MATCH",
        coverage: complete,
        sources: handles,
      });
    }

    const outcomes = await this.runChunks(request.question, plan.chunks, deadline);

    const answers: string[] = [];
    const rawCitations: Array<Record<string, unknown>> = [];
    let totalCalls = 0;
    let usageIn = 0;
    let usageOut = 0;
    for (const outcome of outcomes) {
      totalCalls += outcome.calls;
      usageIn += outcome.inputTokens;
      usageOut += outcome.outputTokens;
      if (outcome.failedReason) {
        coverage.omit(outcome.chunk.sourceId, outcome.chunk.locator, outcome.failedReason);
        continue;
      }
      coverage.processedChunks += 1;
      if (outcome.answer) answers.push(outcome.answer);
      rawCitations.push(...outcome.citations);
    }
    this.metrics.observe("reader_model_calls", totalCalls, { model: READER_MODEL });
    this.metrics.observe("reader_input_tokens", usageIn, { model: READER_MODEL });
    this.metrics.observe("reader_output_tokens", usageOut, { model: READER_MODEL });

    const { verified, rejected } = this.verifyAll(sessionId, rawCitations);
    this.metrics.observe("citations_verified", verified.length, { result: "verified" });
    this.metrics.observe("citations_rejected", rejected, { result: "rejected" });

    let answer = stripUnsupportedAssertions(
      answers.join(" "),
      new Set(verified.map((c) => c.id)),
    );
    const usedIds = new Set(referencedIds(answer));
    const citations = verified.filter((c) => usedIds.has(c.id)).slice(0, this.limits.maxCitations);
    answer = capBytes(answer, Math.min(request.budgets.max_answer_bytes, this.limits.maxAnswerBytes));

    deadline.check("PUBLISH");
    const complete =
      coverage.omitted.length === 0 &&
      coverage.processedChunks === coverage.plannedChunks &&
      coverage.plannedChunks > 0;
    coverage.upstreamTruncated = false;

    if (answer.length === 0) {
      if (rejected > 0 && verified.length === 0 && rawCitations.length > 0) {
        throw new ShuntError("CITATION_INVALID", "NO_VALID_EVIDENCE", false);
      }
      coverage.complete = complete;
      return buildEnvelope({
        requestId,
        status: complete ? "ok" : "partial",
        code: "NO_MATCH",
        coverage,
        sources: handles,
      });
    }

    coverage.complete = complete;
    return buildEnvelope({
      requestId,
      status: complete ? "ok" : "partial",
      code: "ANSWERED",
      answer,
      citations,
      coverage,
      sources: handles,
    });
  }

  private async runChunks(
    question: string,
    chunks: Chunk[],
    deadline: Deadline,
  ): Promise<ChunkOutcome[]> {
    const results: ChunkOutcome[] = new Array(chunks.length);
    let next = 0;
    const workers = Math.min(this.limits.maxConcurrentModelCalls, Math.max(1, chunks.length));
    const runWorker = async (): Promise<void> => {
      for (;;) {
        const index = next;
        next += 1;
        if (index >= chunks.length) return;
        results[index] = await this.runChunk(question, chunks[index] as Chunk, deadline);
      }
    };
    await Promise.all(Array.from({ length: workers }, runWorker));
    return results;
  }

  private async runChunk(question: string, chunk: Chunk, deadline: Deadline): Promise<ChunkOutcome> {
    const outcome: ChunkOutcome = {
      chunk, answer: "", citations: [], failedReason: null, calls: 0, inputTokens: 0, outputTokens: 0,
    };
    const attempts = 1 + this.limits.maxTransientRetries;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        deadline.check("MODEL_CALL");
      } catch (err) {
        outcome.failedReason = isShuntError(err) && err.code === "TIMEOUT" ? "TIMEOUT" : "CANCELLED";
        return outcome;
      }
      try {
        const response = await this.provider.complete({
          system: READER_SYSTEM_PROMPT,
          user: buildUserMessage(question, chunk.text, chunk.locator),
          maxOutputTokens: this.limits.maxOutputTokensPerCall,
          timeoutMs: deadline.subBudget(this.limits.modelCallDeadlineMs),
        });
        outcome.calls += 1;
        outcome.inputTokens = response.usage.inputTokens;
        outcome.outputTokens = response.usage.outputTokens;
        const parsed = parseModelJson(response.text);
        outcome.answer = String(parsed["answer"] ?? "");
        outcome.citations = normalizeCitations(parsed["citations"], chunk);
        return outcome;
      } catch (err) {
        outcome.calls += 1;
        if (!isShuntError(err)) throw err;
        if (err.retryable && attempt + 1 < attempts && !deadline.expired()) continue;
        outcome.failedReason =
          err.code === "MODEL_ERROR" ? "MODEL_ERROR"
          : err.code === "INVALID_MODEL_OUTPUT" ? "INVALID_MODEL_OUTPUT"
          : err.code === "TIMEOUT" ? "TIMEOUT"
          : "CHUNK_FAILED";
        return outcome;
      }
    }
    outcome.failedReason = "CHUNK_FAILED";
    return outcome;
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
    if (typeof candidate === "string" && candidate.length > 0) return candidate;
  }
  return "req_unknown";
}

function parseModelJson(text: string): Record<string, unknown> {
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
    const id = String(rec["id"] ?? "");
    const quote = rec["quote"];
    if (!id || typeof quote !== "string") continue;
    const locator = locatorFor(rec, chunk);
    if (!locator) continue;
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

function locatorFor(item: Record<string, unknown>, chunk: Chunk): Record<string, unknown> | null {
  if (chunk.locator["kind"] === "lines") {
    const start = item["line_start"];
    const end = item["line_end"] ?? start;
    if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
    return { kind: "lines", start, end };
  }
  const start = item["record_start"] ?? item["line_start"];
  const end = item["record_end"] ?? item["line_end"] ?? start;
  if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
  return { kind: "records", pointer: chunk.locator["pointer"] ?? "", start, end };
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
