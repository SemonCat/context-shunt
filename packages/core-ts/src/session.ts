/**
 * Per-session wiring shared by both adapters.
 *
 * Adapters normalize host events into `(tool, args)` and model calls; everything below -
 * gate, capture, store, reader, inspect, stats, spill, guard, accounting - lives here so
 * the two hosts cannot drift apart on semantics.
 *
 * Only content that is *actually being withheld* is captured: a read the gate blocked, or
 * an explicitly eligible oversized post-tool candidate. Short results and ordinary file
 * reads are never pre-stored, so the store never becomes a shadow copy of the workspace.
 *
 * If the store cannot publish, the original operation stays blocked and the caller gets a
 * fixed safe error with no handle. There is no path in which a capture failure lets the raw
 * payload through instead.
 *
 * `close()` revokes handles and is reserved for a *real* session boundary. An ordinary
 * per-turn event must call `endTurn()` instead, which does nothing but an opportunistic
 * sweep - OpenClaw fires `session_end` with `reason: "compaction"` mid-conversation and
 * Hermes fires `on_session_end` every turn, so closing at either point would delete the
 * recovery handles the next turn depends on.
 */
import {
  type Baseline,
  type DeliveryBoundary,
  type OperationKind,
  type ReaderCost,
  composeRecord,
  envelopeEgress,
  hostTruncatedBaseline,
  newOperationId,
  noBaseline,
  noReaderCost,
  recordToShape,
  totalsToShape,
  withheldPayloadBaseline,
} from "./accounting.js";
import { CapabilityReport, modeEnabled } from "./capability.js";
import { Clock, monotonicClock } from "./clock.js";
import { Config, ProviderRef } from "./config.js";
import {
  Coverage,
  Envelope,
  type LegacyCompactionShape,
  type ExtractionShape,
  type SourceHandle,
  buildEnvelope,
  errorEnvelope,
  isoExpiry,
  recoveryFor,
  serializedBytes,
} from "./envelope.js";
import { ShuntError, fallbackAllowed, isShuntError } from "./errors.js";
import { GateDecision, PreReadGate, guidanceFor } from "./gate.js";
import { enforce, enforceOrFixed, fixedError } from "./guard.js";
import { CURSOR_PREFIX, Inspector, decodeCursor, encodeCursor } from "./inspect.js";
import {
  compactToolResult,
  incumbentCompactToolResult,
  DEFAULT_LEGACY_SESSION_HARD_CHARS,
} from "./legacy-compact.js";
import { MetricsSink, nullMetrics } from "./metrics.js";
import { EMITTED_SCHEMA_VERSION } from "./limits.js";
import { authorize, pathPolicy, readAuthorizedBounded } from "./paths.js";
import { fileProber } from "./probe.js";
import { deterministicProvenance, type Provenance } from "./provenance.js";
import {
  FallbackChainProvider,
  HostBridgeProvider,
  type HostBridgeCall,
  type ReaderProvider,
  UnavailableProvider,
} from "./provider.js";
import { Reader, type ReaderResult } from "./reader.js";
import { RegisteredSource, SourceRegistry } from "./registry.js";
import { INSPECT_OPERATIONS, STATS_OPERATIONS, type InspectRequest, type StatsRequest, validateRequest } from "./schema.js";
import {
  JSON_MEDIA_TYPE,
  Snapshot,
  TEXT_MEDIA_TYPE,
  assertNoSecret,
  recordCount,
  resolvePointer,
  snapshotBytes,
} from "./snapshot.js";
import { SpillEngine, SpillOutcome } from "./spill.js";
import { ScopeIdentity, SnapshotStore } from "./store.js";

/**
 * Envelope schema cap on `extraction.next_cursor`. The scaffolding measurement assumes a
 * cursor of exactly this length so a real one can never overshoot the budget it set.
 */
const MAX_CURSOR_CHARS = 512;
const INSPECT_RECOVERY_GUIDANCE =
  "Use context_shunt_inspect with the exact retained source_id/snapshot_id pair. For a "
  + 'minified one-line source, first use selector={"kind":"search","needle":"<literal>",'
  + '"max_matches":5,"context_lines":0}. Then request only needed surrounding bytes with a '
  + 'bytes selector={"kind":"bytes","start":<0-based UTF-8 boundary>,"end":<exclusive UTF-8 '
  + "boundary>}. To continue a page, resend the identical "
  + "selector plus extraction.next_cursor; do not restart lines 1..1 without its cursor.";
const SEARCH_WINDOW_GUIDANCE =
  "Oversized search hit: exact byte window only; surrounding context is omitted. "
  + INSPECT_RECOVERY_GUIDANCE;
export class ShuntSession {
  private readonly gate: PreReadGate;
  private readonly reader: Reader;
  private readonly inspector: Inspector;
  private readonly metrics: MetricsSink;
  private readonly clock: Clock;
  private readonly provider: ReaderProvider;
  readonly store: SnapshotStore;
  readonly identity: ScopeIdentity;
  readonly registry: SourceRegistry;
  readonly spill: SpillEngine;
  private readonly legacyCompactionMaxChars: number;

  constructor(
    readonly sessionId: string,
    readonly config: Config,
    readonly capability: CapabilityReport,
    opts: {
      provider?: ReaderProvider;
      clock?: Clock;
      metrics?: MetricsSink;
      store?: SnapshotStore;
      identity?: ScopeIdentity;
      /** @deprecated accepted for source compatibility; mandatory fallback ignores it. */
      legacyCompaction?: boolean;
      /** Character ceiling handed to the deterministic compactor before byte capping. */
      legacyCompactionMaxChars?: number;
    } = {},
  ) {
    this.clock = opts.clock ?? monotonicClock;
    this.metrics = opts.metrics ?? nullMetrics;
    this.store = opts.store ?? new SnapshotStore(config.spillDir, config.limits);
    this.identity =
      opts.identity
      ?? new ScopeIdentity({
        host: capability.hostName || "unknown",
        profile: capability.adapter || "default",
        principal: "local",
        session: sessionId,
      });
    this.store.openScope(this.identity);
    this.registry = new SourceRegistry(this.store, this.identity, config.limits);
    this.gate = new PreReadGate(fileProber(config.limits), config.limits, this.clock);
    this.provider = opts.provider ?? new UnavailableProvider();
    // The fallback is an availability invariant. Keep accepting the former option so
    // callers can upgrade without a config parse break, but it cannot disable fallback.
    void opts.legacyCompaction;
    this.legacyCompactionMaxChars = opts.legacyCompactionMaxChars === undefined
      ? DEFAULT_LEGACY_SESSION_HARD_CHARS
      : opts.legacyCompactionMaxChars;
    if (
      !Number.isSafeInteger(this.legacyCompactionMaxChars)
      || this.legacyCompactionMaxChars < 1_000
      || this.legacyCompactionMaxChars > 60_000
    ) {
      throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
    }
    this.reader = new Reader(
      this.registry,
      this.provider,
      config.limits,
      this.clock,
      this.metrics,
      config.readerAttributionPolicy,
    );
    this.inspector = new Inspector(config.limits);
    this.spill = new SpillEngine(
      this.registry,
      config.limits,
      config.toolResultCaptureEnabled && modeEnabled(capability, "tool_result_capture"),
      this.legacyCompactionMaxChars,
    );
  }

  get toolResultCaptureEnabled(): boolean {
    return this.spill.enabled;
  }

  /** @deprecated alias for {@link toolResultCaptureEnabled} */
  get sumaEnabled(): boolean {
    return this.spill.enabled;
  }

  // -- gate ------------------------------------------------------------------

  evaluateToolCall(tool: string, args: Record<string, unknown>): GateDecision {
    const decision = this.gate.evaluate(tool, args);
    if (!this.config.gateEnabled) {
      return { decision: "passthrough", form: decision.form, reason: "" };
    }
    this.metrics.count("gate_decision", {
      decision: decision.decision,
      form: decision.form,
      reason: decision.reason || "NONE",
    });
    return decision;
  }

  blockEnvelope(requestId: string, decision: GateDecision): Envelope {
    const operationId = newOperationId();
    const coverage = new Coverage();
    coverage.upstreamTruncated = null;
    const published = enforceOrFixed(
      buildEnvelope({
        requestId,
        status: "blocked",
        code: decision.code ?? "UNCLASSIFIABLE_READ",
        coverage,
        retryable: false,
        guidance: guidanceFor(decision),
        resultKind: "gate_decision",
        provenance: deterministicProvenance("gate_decision"),
        accountingId: operationId,
        recovery: { handles_valid: false, actions: ["INSPECT_HANDLE", "NARROW_SELECTOR"] },
      }),
      this.config.limits,
    );
    this.record({
      operationId,
      kind: "gate_block",
      envelope: published,
      baseline: noBaseline(),
      baselineCredited: false,
      reader: noReaderCost(),
      boundary: "block_message",
    });
    return published;
  }

  // -- capture ---------------------------------------------------------------

  /** Authorize, snapshot and publish one path. Only ever called for withheld content. */
  registerPath(path: string, mediaType?: string): RegisteredSource {
    return this.registerPaths([path], mediaType)[0] as RegisteredSource;
  }

  /**
   * Authorize every path, then publish all handles as one batch or none.
   *
   * The whole request is validated before anything is captured: one unsafe, secret, binary
   * or oversized path rejects the batch, so a partially authorized multi-source capture can
   * never leave usable handles behind.
   */
  registerPaths(paths: readonly string[], mediaType?: string): RegisteredSource[] {
    if (paths.length === 0) throw new ShuntError("INVALID_REQUEST", "NO_SOURCE", false);
    if (paths.length > this.config.limits.maxSourcesPerRequest) {
      throw new ShuntError("LIMIT_EXCEEDED", "TOO_MANY_SOURCES", false);
    }
    const policy = pathPolicy(this.config.workspaceRoots, this.config.denylist);
    const snapshots: Snapshot[] = paths.map((path) => {
      const authorized = authorize(path, policy);
      const data = readAuthorizedBounded(authorized, this.config.limits.maxSourceBytes);
      const hint =
        mediaType
        ?? (authorized.real.toLowerCase().endsWith(".json") ? JSON_MEDIA_TYPE : TEXT_MEDIA_TYPE);
      return snapshotBytes(data, hint, this.config.limits);
    });
    return this.registry.registerBatch(this.sessionId, snapshots);
  }

  // -- reader ----------------------------------------------------------------

  async read(request: unknown, signal?: AbortSignal): Promise<Envelope> {
    const requestId = readRequestId(request);
    if (!this.config.readerEnabled) {
      return this.publishFailure(
        requestId,
        new ShuntError("INVALID_REQUEST", "READER_DISABLED", false),
        "read",
      );
    }
    const operationId = newOperationId();
    let result: ReaderResult;
    try { result = await this.reader.answerDetailed(
      this.sessionId,
      request,
      undefined,
      signal,
      operationId,
    ); } catch (raw) {
      let failure = isShuntError(raw) ? raw : new ShuntError("STORE_FAILED", "INTERNAL_ERROR");
      try { validateRequest(request); } catch (invalid) {
        if (isShuntError(invalid)) failure = invalid;
      }
      result = { envelope: errorEnvelope(requestId, failure),
        provenance: deterministicProvenance("no_model_output"), cost: noReaderCost(), sourceIds: [] };
    }
    let candidate = result.envelope;
    if (
      !signal?.aborted
      && result.envelope.status === "error"
      && fallbackAllowed(result.envelope.code, result.envelope.failure_detail)
      && result.envelope.provenance?.attribution_status !== "mismatch"
    ) {
      try {
        candidate = enforce(this.legacyCompactionFallback(request, result, requestId, operationId), this.config.limits);
      } catch (err) {
        // A malformed/unsafe summary is no delivery. Mandatory fallback never degrades to
        // raw passthrough or the retired exact-prefix extraction path.
        candidate = result.envelope;
        if (isShuntError(err)) {
          const handlesValid = !["SOURCE_EXPIRED", "SOURCE_CHANGED", "UNSAFE_SOURCE", "STORE_FAILED"].includes(err.code);
          candidate = errorEnvelope(requestId, err, {
            accountingId: operationId,
            provenance: result.provenance,
            sources: result.envelope.sources,
            handlesValid,
          });
          candidate.coverage = result.envelope.coverage;
        } else {
          candidate = ShuntSession.prototype.legacyCompactionFallback.call(
            this, request, result, requestId, operationId, undefined, incumbentCompactToolResult,
          );
        }
      }
    }
    // The historical automatic exact-prefix extraction route is retained as an internal
    // helper for compatibility with callers that import the class, but is deliberately not
    // a secondary answer path: Shunt-owned failures must produce incumbent compaction.
    if (candidate.code === "CITATION_INVALID") {
      // Preserve evidence and cost instead of substituting a heuristic answer.
      try {
        for (const handle of candidate.sources) {
          const entry = this.registry.resolve(this.sessionId, handle.source_id);
          if (entry.snapshot.snapshotId !== handle.snapshot_id) {
            throw new ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH", false);
          }
        }
      } catch (err) {
        if (!isShuntError(err)) throw err;
        candidate = { ...candidate, recovery: recoveryFor(err.code, false) };
      }
      candidate = { ...candidate, guidance:
        "Semantic answer unavailable; evidence needs verification. "
        + INSPECT_RECOVERY_GUIDANCE
        + " No heuristic summary was substituted." };
    }
    const published = enforceOrFixed(candidate, this.config.limits);
    const refined = Boolean(
      typeof request === "object" && request !== null
        && (request as Record<string, unknown>)["refined"],
    );
    let baseline = noBaseline();
    let creditedBytes = 0;
    try { ({ baseline, creditedBytes } = this.baselineFor(result.sourceIds)); }
    catch { /* Accounting must not prevent a bounded response. */ }
    this.record({
      operationId,
      kind: refined ? "refined_read" : "read",
      envelope: published,
      baseline,
      baselineCredited: creditedBytes > 0,
      creditedBytes,
      reader: result.cost,
      boundary: published.code === "EXTRACTED" || published.code === "LEGACY_COMPACTED"
        ? "extraction" : "envelope",
    });
    return published;
  }

  private legacyCompactionFallback(
    request: unknown,
    result: ReaderResult,
    requestId: string,
    operationId: string,
    byteCap?: number,
    compactor = compactToolResult,
  ): Envelope {
    const validated = validateRequest(request);
    const input = validated as {
      sources: Array<{ source_id: string; snapshot_id: string }>;
      question?: unknown;
    };
    if (typeof input.question === "string") assertNoSecret(input.question, "QUESTION");
    // Revalidate every handle after the provider wait, before the summary reads any bytes.
    const entries: RegisteredSource[] = [];
    for (const source of input.sources) {
      const selector = (source as { selector?: Record<string, unknown> }).selector;
      if (selector?.["kind"] === "lines" && Number(selector["end"]) < Number(selector["start"]))
        throw new ShuntError("INVALID_REQUEST", "BAD_RANGE");
      const entry = this.registry.resolve(this.sessionId, source.source_id);
      if (selector?.["kind"] === "lines" && Number(selector["start"]) > Math.min(Number(selector["end"]), entry.snapshot.lineCount))
        throw new ShuntError("INVALID_REQUEST", "LINE_OUT_OF_RANGE");
      if (selector?.["kind"] === "records") {
        const node = resolvePointer(entry.snapshot.jsonValue, String(selector["pointer"]));
        if (Number(selector["end"]) < Number(selector["start"]) || Number(selector["end"]) > recordCount(node))
          throw new ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE");
      }
      if (entry.snapshot.snapshotId !== source.snapshot_id) {
        throw new ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH", false);
      }
      entries.push(entry);
    }
    const entry = entries[0];
    if (!entry) throw new ShuntError("INVALID_REQUEST", "NO_SOURCE", false);
    let text: string;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(entry.snapshot.data);
    } catch {
      throw new ShuntError("UNSAFE_SOURCE", "INVALID_ENCODING", false);
    }

    const originalFailure = result.envelope.code;
    if (!fallbackAllowed(originalFailure, result.envelope.failure_detail)) {
      throw new ShuntError("STORE_FAILED", "LEGACY_COMPACTION_FAILURE_UNKNOWN", false);
    }
    const allowance = this.store.disclosureAllowance(this.identity, entry.sourceId);
    const remaining = Math.min(
      allowance.perSourceRemaining,
      allowance.perSessionRemaining,
    );
    if (remaining <= 0) throw new ShuntError("DISCLOSURE_EXHAUSTED");
    const compacted = utf8SafeCap(
      compactor(text, { hardChars: this.legacyCompactionMaxChars }),
      Math.min(this.config.limits.maxExtractionBytes, remaining, byteCap ?? Infinity),
    );

    // A reader failure can legitimately carry an empty source list. Rebuild the handles
    // from the revalidated immutable entries so the fallback still identifies and charges
    // the authorized snapshots it discloses.
    const sourceHandles = entries.map((item) => sourceHandle(item));
    const sourceCoverage = result.envelope.coverage;
    const coverage = new Coverage();
    coverage.complete = false;
    coverage.processedChunks = sourceCoverage.processed_chunks;
    coverage.plannedChunks = sourceCoverage.planned_chunks;
    coverage.upstreamTruncated = sourceCoverage.upstream_truncated;
    for (const omission of sourceCoverage.omitted) {
      coverage.omitOnce(omission.source_id, { ...omission.selector }, omission.reason);
    }
    for (const handle of sourceHandles) {
      coverage.omitOnce(handle.source_id, { kind: "all" }, "UNKNOWN_REMAINDER");
    }

    const buildLegacyEnvelope = (summary: string): Envelope => buildEnvelope({
      requestId,
      status: "partial",
      code: "LEGACY_COMPACTED",
      coverage,
      sources: sourceHandles,
      retryable: false,
      resultKind: "legacy_compaction",
      // Preserve provider identity, attempts and usage truth from the failed call while
      // making the deterministic replacement's non-model status explicit.
      provenance: legacyFallbackProvenance(
        result.provenance,
        result.cost.attemptsStarted,
        result.cost.attemptsUsageComplete,
      ),
      guidance:
        "Escape hatch: deterministic legacy-shaped compaction of the source, ported from "
        + "the incumbent tool-result compactor; not model-derived and not an LLM summary. "
        + "Original reader failure: " + originalFailure
        + ". Covers only the first requested source, independent of the question; other "
        + "sources and structure the heuristic dropped are omitted. Treat the summary only "
        + "as navigation: never as the question's answer, exhaustive coverage, an exact "
        + "count, or citation evidence. Use the retained handles with context_shunt_inspect "
        + "for exact bounded evidence.",
      recovery: recoveryFor(originalFailure, true),
      accountingId: operationId,
      ...(result.envelope.failure_detail !== undefined
        ? { failureDetail: result.envelope.failure_detail }
        : {}),
      legacyCompaction: {
        deterministic: true,
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        summary,
        summary_bytes: new TextEncoder().encode(summary).length,
        original_bytes: entry.snapshot.bytesLen,
        hard_cap_chars: this.legacyCompactionMaxChars,
        original_failure: originalFailure as LegacyCompactionShape["original_failure"],
      },
    });

    // Legacy compaction remains under the ordinary wire cap. Trim by the measured UTF-8
    // excess, matching the Python incumbent fallback when JSON escaping consumes headroom.
    const candidate = fitLegacyEnvelope(
      buildLegacyEnvelope,
      compacted,
      this.config.limits.maxEnvelopeBytes,
    );
    // A legacy summary still discloses source bytes. Enforce the same per-source and
    // per-session disclosure ceilings as inspect, and charge only after the complete
    // envelope has passed the output guard so a rejected delivery cannot consume budget.
    let published: Envelope;
    try {
      published = enforce(candidate, this.config.limits);
    } catch {
      throw new ShuntError("LIMIT_EXCEEDED", "NO_ENVELOPE_HEADROOM", false);
    }
    const summaryBytes = published.legacy_compaction?.summary_bytes ?? 0;
    const charge = this.store.chargeDisclosure(
      this.identity,
      entry.sourceId,
      "bytes",
      summaryBytes,
    );
    if (!charge.granted) throw new ShuntError("DISCLOSURE_EXHAUSTED");
    return published;
  }

  /** Compact a successfully captured snapshot when its pointer cannot be published. */
  private legacyCompactionFallbackForHandle(
    sourceId: string | undefined,
    requestId: string,
    operationId: string,
  ): Envelope {
    if (sourceId === undefined) throw new ShuntError("STORE_FAILED", "UNKNOWN_HANDLE", false);
    const entry = this.registry.resolve(this.sessionId, sourceId);
    const handle: SourceHandle = {
      source_id: entry.sourceId,
      snapshot_id: entry.snapshot.snapshotId,
      media_type: entry.snapshot.mediaType,
      bytes: entry.snapshot.bytesLen,
      expires_at: new Date(entry.expiresAtEpoch * 1000).toISOString().replace(/\.\d{3}Z$/, "Z"),
    };
    const failure = new ShuntError("SPILL_FAILED", "INTERNAL_ERROR", false);
    const failed = errorEnvelope(requestId, failure, {
      accountingId: operationId,
      sources: [handle],
      handlesValid: true,
    });
    const result: ReaderResult = {
      envelope: failed,
      provenance: deterministicProvenance("no_model_output"),
      cost: noReaderCost(),
      sourceIds: [entry.sourceId],
    };
    return this.legacyCompactionFallback(
      { schema_version: "1.1", operation: "read", request_id: requestId, question: "Bounded compaction", budgets: {max_chunks: 1, max_answer_bytes: 8192, deadline_ms: 60000}, sources: [{ source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId, selector: {kind: "all"} }] },
      result,
      requestId,
      operationId,
    );
  }

  /**
   * The withheld-payload baseline, and how many of its bytes this read may claim.
   *
   * The two differ for a mixed selection: the measurement covers every selected source,
   * while the credit covers only those this read newly withheld.
   */
  private baselineFor(sourceIds: readonly string[]): {
    baseline: Baseline;
    creditedBytes: number;
  } {
    let total = 0;
    let creditedBytes = 0;
    for (const sourceId of sourceIds) {
      let bytes: number;
      try {
        bytes = this.registry.handle(this.sessionId, sourceId).bytesLen;
      } catch {
        continue;
      }
      // The measurement is every selected source, so a read that claims nothing still
      // reports what the payload was worth.
      total += bytes;
      // The credit is only what this read newly withholds. `creditBaseline` records the
      // claim against the content and returns true exactly once, so a source an earlier
      // read already credited contributes nothing here. Folding the results into a single
      // OR credited the *whole* selection whenever any part of it was new, which inflated
      // the saving on every mixed-source read.
      if (this.store.creditBaseline(this.identity, sourceId)) creditedBytes += bytes;
    }
    if (total === 0) return { baseline: noBaseline(), creditedBytes: 0 };
    return { baseline: withheldPayloadBaseline(total, this.config.limits), creditedBytes };
  }

  // -- inspect ---------------------------------------------------------------

  /** Deterministic extraction. No provider is consulted on this path at all. */
  inspect(request: unknown): Envelope {
    const requestId = readRequestId(request);
    if (!this.config.inspectEnabled) {
      return this.publishFailure(
        requestId,
        new ShuntError("INVALID_REQUEST", "INSPECT_DISABLED", false),
        "inspect",
      );
    }
    const operationId = newOperationId();
    try {
      return this.runInspect(request, requestId, operationId);
    } catch (raw) {
      let failure = isShuntError(raw) ? raw : new ShuntError("STORE_FAILED", "INTERNAL_ERROR");
      if (fallbackAllowed(failure.code, failure.detail)) {
        try {
          const args = validateRequest(request, INSPECT_OPERATIONS) as InspectRequest;
          const entry = this.registry.resolve(this.sessionId, args.source_id);
          if (entry.snapshot.snapshotId !== args.snapshot_id) throw new ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH");
          const sel = args.selector;
          if ((sel["kind"] === "lines" || sel["kind"] === "bytes") && Number(sel["end"]) < Number(sel["start"]))
            throw new ShuntError("INVALID_REQUEST", "BAD_RANGE");
          if (sel["kind"] === "bytes") {
            for (const offset of [Number(sel["start"]), Number(sel["end"])]) {
              const byte = entry.snapshot.data[offset];
              if (byte !== undefined && (byte & 0xc0) === 0x80) throw new ShuntError("INVALID_REQUEST", "BAD_RANGE");
            }
          }
          if (sel["kind"] === "search" && Buffer.byteLength(String(sel["needle"])) > this.config.limits.inspectMaxNeedleBytes)
            throw new ShuntError("INVALID_REQUEST", "NEEDLE_OVER_CAP");
          const state = args.cursor === undefined ? {} : decodeCursor(this.store.cursorKey(), args.cursor, args.source_id, args.snapshot_id, sel);
          if (sel["kind"] === "bytes") {
            const offset = Math.max(Number(sel["start"]), Number(state["offset"] ?? sel["start"]));
            const byte = entry.snapshot.data[offset];
            if (byte !== undefined && (byte & 0xc0) === 0x80) throw new ShuntError("INVALID_REQUEST", "UTF8_RANGE_BOUNDARY");
          }
          const failed = errorEnvelope(requestId, failure);
          const result: ReaderResult = { envelope: failed, provenance: deterministicProvenance("no_model_output"), cost: noReaderCost(), sourceIds: [entry.sourceId] };
          const synthetic = { schema_version: "1.1", operation: "read", request_id: requestId, question: "Bounded compaction", sources: [{source_id: args.source_id, snapshot_id: args.snapshot_id, selector: {kind: "all"}}], budgets: {max_chunks: 1, max_answer_bytes: 8192, deadline_ms: 60000} };
          const envelope = this.legacyCompactionFallback(synthetic, result, requestId, operationId, args.budgets.max_result_bytes);
          this.record({operationId, kind: "inspect", envelope, baseline: noBaseline(), baselineCredited: false, reader: noReaderCost(), boundary: "extraction"});
          return envelope;
        } catch (err) { failure = isShuntError(err) ? err : new ShuntError("STORE_FAILED", "INTERNAL_ERROR"); }
      }
      return this.publishFailure(requestId, failure, "inspect", operationId);
    }
  }

  private runInspect(request: unknown, requestId: string, operationId: string, fallback?: ReaderResult): Envelope {
    const validated = validateRequest(request, INSPECT_OPERATIONS) as InspectRequest;
    const sourceId = validated.source_id;
    const snapshotId = validated.snapshot_id;
    const selector = validated.selector;

    const entry = this.registry.resolve(this.sessionId, sourceId);
    if (entry.snapshot.snapshotId !== snapshotId) {
      throw new ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH", false);
    }

    const key = this.store.cursorKey();
    const state =
      validated.cursor !== undefined
        ? decodeCursor(key, validated.cursor, sourceId, snapshotId, selector)
        : {};

    const allowance = this.store.disclosureAllowance(this.identity, sourceId);
    const remaining = Math.max(
      0,
      Math.min(allowance.perSourceRemaining, allowance.perSessionRemaining),
    );
    const requestedBudget = validated.budgets.max_result_bytes;
    const budget = Math.min(requestedBudget, remaining);
    const clippedByAllowance = remaining < requestedBudget;
    let handles: SourceHandle[] = [
      {
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        media_type: entry.snapshot.mediaType,
        bytes: entry.snapshot.bytesLen,
        expires_at: isoExpiry(entry.expiresAtEpoch),
      },
    ];

    if (remaining <= 0 || budget <= 0) {
      if (fallback) throw new ShuntError("DISCLOSURE_EXHAUSTED");
      return this.disclosureExhausted(requestId, operationId, entry, selector, handles, allowance);
    }

    // Reserve room for all handles, omissions and escape-hatch guidance. The full
    // composed envelope is still guarded before any disclosure is charged.
    const extraction = this.inspector.extract(
      entry.snapshot.data,
      entry.snapshot.lineIndex,
      selector,
      {
        maxResultBytes: budget,
        maxScanLines: validated.budgets.max_scan_lines,
        maxWireBytes: this.extractionWireBudget(requestId, operationId, entry, selector, handles)
          - (fallback ? 4096 : 0),
        state,
      },
    );
    if (extraction.stalled) {
      // The page emitted nothing *and* the cursor did not move, so continuing would loop
      // forever. A scan-budget stop is not this case: it emits nothing but does advance.
      if (extraction.stallReason === "wire") {
        // This unit cannot fit any envelope, whatever the allowance says. Calling it a
        // disclosure problem would send the caller to a remedy that never works.
        throw new ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_WIRE_BUDGET", false);
      }
      if (clippedByAllowance) {
        return this.disclosureExhausted(
          requestId, operationId, entry, selector, handles, allowance,
        );
      }
      throw new ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_PAGE_BUDGET", false);
    }

    if (fallback && !extraction.resultBytes) throw new ShuntError("LIMIT_EXCEEDED", "EMPTY_FALLBACK");
    const nextCursor =
      extraction.nextCursorState !== undefined
        ? encodeCursor(key, sourceId, snapshotId, selector, extraction.nextCursorState)
        : null;
    let coverage = new Coverage();
    coverage.upstreamTruncated = false;
    coverage.complete = extraction.complete;
    if (!extraction.complete) {
      coverage.omit(
        sourceId,
        omissionSelector(selector),
        extraction.scanBudgetExhausted ? "SCAN_BUDGET_EXHAUSTED" : "UNKNOWN_REMAINDER",
      );
    }
    if (fallback) {
      coverage = new Coverage();
      for (const handle of fallback.envelope.sources) {
        coverage.omit(handle.source_id, { kind: "all" }, "UNKNOWN_REMAINDER");
      }
      handles = fallback.envelope.sources;
    }
    const compose = (sourceUsed: number, sessionUsed: number, limitReached: boolean): Envelope => {
      const block: ExtractionShape = {
        mode: extraction.mode,
        source_id: sourceId,
        snapshot_id: snapshotId,
        deterministic: true,
        segments: extraction.segments.map((segment) => ({ ...segment })),
        result_bytes: extraction.resultBytes,
        complete: extraction.complete,
        next_cursor: nextCursor,
        lines_scanned: extraction.linesScanned,
        scan_budget_exhausted: extraction.scanBudgetExhausted,
        disclosed_bytes_source: sourceUsed,
        disclosed_bytes_session: sessionUsed,
        disclosure_limit_reached: limitReached,
        ...(extraction.matchesFound !== undefined
          ? { matches_found: extraction.matchesFound }
          : {}),
      };
      return buildEnvelope({
        requestId,
        status: extraction.complete && !fallback ? "ok" : "partial",
        code: "EXTRACTED",
        coverage,
        sources: handles,
        retryable: false,
        resultKind: "deterministic_extraction",
        provenance: {
          ...deterministicProvenance("deterministic_extraction"),
          attemptsStarted: fallback?.cost.attemptsStarted ?? 0,
          usageComplete: fallback ? fallback.cost.attemptsUsageComplete === fallback.cost.attemptsStarted : true,
        },
        ...(fallback ? {
          guidance: "Escape hatch: exact deterministic fallback extraction; not model-derived "
            + "and not an LLM summary. Original failure: " + fallback.availabilityFailure
            + ". Selection: byte prefix of first requested source, independent of question "
            + "and reader selectors; other sources and unreturned bytes omitted.",
          recovery: recoveryFor(fallback.availabilityFailure!),
        } : selector["kind"] === "search" && extraction.segments.some((segment) => segment.kind === "bytes")
          ? { guidance: SEARCH_WINDOW_GUIDANCE } : {}),
        accountingId: operationId,
        extraction: block,
      });
    };

    // Guard the page *before* charging for it, so a refusal cannot consume allowance the
    // caller never receives. The probe carries the widest values the three disclosure
    // counters can legally take, and `false` for the flag because it is the longer of the
    // two literals; every other field is the one that will actually be published. The
    // published envelope is therefore never larger than the probe and never differs from it
    // anywhere the guard looks, so a probe that passes cannot become a failure below.
    const limits = this.config.limits;
    const probe = compose(limits.disclosureMaxPerSourceBytes, limits.disclosureMaxPerSessionBytes, false);
    if (serializedBytes(probe) > limits.maxExtendedEnvelopeBytes)
      throw new ShuntError("LIMIT_EXCEEDED", "NO_ENVELOPE_HEADROOM");
    try {
      enforce(probe, limits);
    } catch {
      throw new ShuntError("LIMIT_EXCEEDED", "EXTRACTION_REFUSED", false);
    }

    // Check-and-increment before a byte is returned: a concurrent inspect that consumed
    // the allowance in the meantime causes this page to disclose nothing.
    const charge = this.store.chargeDisclosure(
      this.identity, sourceId, extraction.mode, extraction.resultBytes,
    );
    if (!charge.granted) {
      if (fallback) throw new ShuntError("DISCLOSURE_EXHAUSTED");
      return this.disclosureExhausted(requestId, operationId, entry, selector, handles, allowance);
    }

    const published = enforceOrFixed(
      compose(charge.disclosedBytesSource, charge.disclosedBytesSession, charge.limitReached),
      this.config.limits,
    );
    if (fallback) return published;
    this.record({
      operationId,
      kind: "inspect",
      envelope: published,
      // An inspect page discloses rather than withholds, so it claims no baseline saving
      // and its envelope shows up as pure overhead.
      baseline: noBaseline(),
      baselineCredited: false,
      reader: noReaderCost(),
      boundary: "extraction",
    });
    return published;
  }

  /**
   * Serialized room left for segment text once the envelope around it is paid for.
   *
   * Measured rather than reserved as a constant. The scaffolding is not fixed: an omission
   * echoes the caller's selector, and a `search` selector carries a caller-supplied needle,
   * so a constant sized against a short needle would under-budget a long one. Everything
   * here is set to its most expensive legal shape - incomplete coverage with an omission, a
   * match count present, a maximum-length cursor, both counters at their caps - so the real
   * envelope is never larger than what this measured.
   */
  private extractionWireBudget(
    requestId: string,
    operationId: string,
    entry: RegisteredSource,
    selector: Record<string, unknown>,
    handles: SourceHandle[],
  ): number {
    const limits = this.config.limits;
    const coverage = new Coverage();
    coverage.upstreamTruncated = false;
    coverage.complete = false;
    coverage.omit(entry.sourceId, omissionSelector(selector), "SCAN_BUDGET_EXHAUSTED");
    const skeleton = buildEnvelope({
      requestId,
      status: "partial",
      code: "EXTRACTED",
      coverage,
      sources: handles,
      retryable: false,
      ...(selector["kind"] === "search" ? { guidance: SEARCH_WINDOW_GUIDANCE } : {}),
      resultKind: "deterministic_extraction",
      provenance: deterministicProvenance("deterministic_extraction"),
      accountingId: operationId,
      extraction: {
        mode: (selector["kind"] as ExtractionShape["mode"]) ?? "lines",
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        deterministic: true,
        segments: [],
        result_bytes: limits.maxExtractionBytes,
        complete: false,
        next_cursor: CURSOR_PREFIX + "c".repeat(MAX_CURSOR_CHARS - CURSOR_PREFIX.length),
        lines_scanned: limits.inspectMaxScanLines,
        scan_budget_exhausted: true,
        matches_found: limits.inspectMaxSearchMatches,
        disclosed_bytes_source: limits.disclosureMaxPerSourceBytes,
        disclosed_bytes_session: limits.disclosureMaxPerSessionBytes,
        disclosure_limit_reached: false,
      },
    });
    return limits.maxExtendedEnvelopeBytes - serializedBytes(skeleton);
  }

  private disclosureExhausted(
    requestId: string,
    operationId: string,
    entry: RegisteredSource,
    selector: Record<string, unknown>,
    handles: SourceHandle[],
    allowance: { perSourceRemaining: number; perSessionRemaining: number },
  ): Envelope {
    const coverage = new Coverage();
    coverage.upstreamTruncated = false;
    coverage.omit(entry.sourceId, omissionSelector(selector), "DISCLOSURE_EXHAUSTED");
    const block: ExtractionShape = {
      mode: (selector["kind"] as ExtractionShape["mode"]) ?? "lines",
      source_id: entry.sourceId,
      snapshot_id: entry.snapshot.snapshotId,
      deterministic: true,
      segments: [],
      result_bytes: 0,
      complete: false,
      next_cursor: null,
      lines_scanned: 0,
      scan_budget_exhausted: false,
      disclosed_bytes_source:
        this.config.limits.disclosureMaxPerSourceBytes - allowance.perSourceRemaining,
      disclosed_bytes_session:
        this.config.limits.disclosureMaxPerSessionBytes - allowance.perSessionRemaining,
      disclosure_limit_reached: true,
    };
    const published = enforceOrFixed(
      buildEnvelope({
        requestId,
        status: "partial",
        code: "DISCLOSURE_EXHAUSTED",
        coverage,
        sources: handles,
        retryable: false,
        resultKind: "deterministic_extraction",
        provenance: deterministicProvenance("deterministic_extraction"),
        accountingId: operationId,
        extraction: block,
        recovery: recoveryFor("DISCLOSURE_EXHAUSTED", true),
      }),
      this.config.limits,
    );
    this.record({
      operationId,
      kind: "inspect",
      envelope: published,
      baseline: noBaseline(),
      baselineCredited: false,
      reader: noReaderCost(),
      boundary: "extraction",
    });
    return published;
  }

  // -- stats -----------------------------------------------------------------

  /** Read-only session aggregate. It cannot reset, retain, widen or cross sessions. */
  stats(request: unknown): Envelope {
    const requestId = readRequestId(request);
    if (!this.config.statsEnabled) {
      return this.publishFailure(
        requestId,
        new ShuntError("INVALID_REQUEST", "STATS_DISABLED", false),
        "stats",
      );
    }
    const operationId = newOperationId();
    let validated: StatsRequest;
    try {
      validated = validateRequest(request, STATS_OPERATIONS) as StatsRequest;
    } catch (err) {
      if (!isShuntError(err)) throw err;
      return this.publishFailure(requestId, err, "stats", operationId);
    }

    const page = validated.page ?? 1;
    const pageSize = Math.min(
      validated.page_size ?? this.config.limits.statsMaxRecordsPerPage,
      this.config.limits.statsMaxRecordsPerPage,
    );
    const total = this.store.operationCount(this.identity);
    const records = this.store.operationPage(this.identity, { page, pageSize });
    const consumed = (page - 1) * pageSize + records.length;
    const nextPage =
      consumed < total && page < this.config.limits.statsMaxPages ? page + 1 : null;

    const published = enforceOrFixed(
      buildEnvelope({
        requestId,
        status: "ok",
        code: "STATS",
        coverage: completeCoverage(),
        retryable: false,
        resultKind: "stats",
        provenance: deterministicProvenance("session_metrics"),
        accountingId: operationId,
        stats: {
          scope: "session",
          totals: totalsToShape(this.store.operationTotals(this.identity)),
          records: records.map(recordToShape),
          page,
          page_size: pageSize,
          total_records: total,
          next_page: nextPage,
        },
      }),
      this.config.limits,
    );
    this.record({
      operationId,
      kind: "stats",
      envelope: published,
      baseline: noBaseline(),
      baselineCredited: false,
      reader: noReaderCost(),
      boundary: "envelope",
    });
    return published;
  }

  // -- optional oversized-tool-result capture --------------------------------

  /** Only ever consulted when the capability probe reports tool_result_capture supported. */
  postToolResult(
    requestId: string,
    result: unknown,
    opts: { internalSourceId?: string; upstreamTruncated?: boolean } = {},
  ): SpillOutcome | null {
    if (!this.toolResultCaptureEnabled) return null;
    const operationId = newOperationId();
    let outcome: SpillOutcome;
    try {
      outcome = this.spill.evaluate(
        this.sessionId,
        requestId,
        result,
        opts.internalSourceId,
        operationId,
        opts.upstreamTruncated ?? false,
      );
    } catch (err) {
      if (isShuntError(err)) {
        outcome = {
          action: "error",
          envelope: errorEnvelope(
            requestId,
            new ShuntError("SPILL_FAILED", err.detail, false),
            { accountingId: operationId },
          ),
          code: "SPILL_FAILED",
          bytesMeasured: 0,
        };
      } else {
        // The adapter boundary can throw after handing us a complete result (for example
        // while bootstrapping the capture/store). Give the spill engine one private retry
        // over the original bytes so a Shunt-owned failure retains incumbent availability.
        outcome = this.spill.recoverUnexpected(
          this.sessionId,
          requestId,
          result,
          operationId,
        );
      }
    }
    let guarded: SpillOutcome = outcome;
    if (outcome.envelope) {
      let envelope = enforceOrFixed(outcome.envelope, this.config.limits);
      const pointerDelivered = outcome.action === "spill" && envelope.code === "SPILLED"
        && Boolean(envelope.pointer)
        && envelope.sources.some((handle) => handle.source_id === outcome.sourceId);
      // Guard rejection cannot retain a spill action or consume an undelivered handle's credit.
      if (outcome.action === "spill" && !pointerDelivered) {
        // The complete serialized result is no longer in this method, but a successful
        // capture left an immutable snapshot behind. Compact that snapshot before exposing
        // the guard failure; a raw post-tool result must never reach the host because the
        // pointer envelope itself was rejected.
        try {
          envelope = enforce(
            this.legacyCompactionFallbackForHandle(
              outcome.sourceId,
              requestId,
              operationId,
            ),
            this.config.limits,
          );
        } catch {
          envelope = fixedError(requestId, "SPILL_FAILED");
        }
      }
      guarded = outcome.action === "spill" && !pointerDelivered
        ? {
          action: "error",
          envelope,
          code: envelope.code,
          bytesMeasured: outcome.bytesMeasured,
          ...(envelope.code === "LEGACY_COMPACTED" && outcome.sourceId !== undefined
            ? { sourceId: outcome.sourceId }
            : {}),
        }
        : { ...outcome, envelope };
      const baseline = opts.upstreamTruncated
        // A host that already truncated the upstream result only lets us observe the
        // truncated size; crediting the full payload there would be invented.
        ? hostTruncatedBaseline(outcome.bytesMeasured, this.config.limits)
        : withheldPayloadBaseline(outcome.bytesMeasured, this.config.limits);
      const credited = Boolean(
        guarded.sourceId && this.store.creditBaseline(this.identity, guarded.sourceId),
      );
      this.record({
        operationId,
        kind: "spill",
        envelope,
        baseline,
        baselineCredited: credited,
        reader: noReaderCost(),
        boundary: pointerDelivered
          ? "pointer"
          : envelope.code === "LEGACY_COMPACTED" ? "extraction" : "envelope",
      });
    }
    this.metrics.count("tool_result_capture_outcome", { result: guarded.action });
    return guarded;
  }

  // -- accounting ------------------------------------------------------------

  /**
   * Measure the exact serialized egress, then write the record.
   *
   * The envelope already carries only the opaque `accounting_id`, so measuring it here
   * cannot be self-referential: the numbers derived from the measurement live in the store,
   * never inside the thing being measured.
   */
  private record(input: {
    operationId: string;
    kind: OperationKind;
    envelope: Envelope;
    baseline: Baseline;
    baselineCredited: boolean;
    creditedBytes?: number | undefined;
    reader: ReaderCost;
    boundary: DeliveryBoundary;
  }): void {
    const record = composeRecord({
      operationId: input.operationId,
      kind: input.kind,
      status: input.envelope.status,
      code: input.envelope.code,
      baseline: input.baseline,
      baselineCredited: input.baselineCredited,
      creditedBytes: input.creditedBytes,
      reader: input.reader,
      egress: envelopeEgress(input.boundary, serializedBytes(input.envelope)),
      limits: this.config.limits,
    });
    try {
      this.store.recordOperation(this.identity, record);
    } catch {
      // Losing a metric must never fail the caller's operation, and it must never be
      // papered over as a zero: the operation simply has no record.
      this.metrics.count("accounting_dropped", { stage: input.kind });
    }
  }

  private publishFailure(
    requestId: string,
    err: ShuntError,
    kind: OperationKind,
    operationId?: string,
  ): Envelope {
    const id = operationId ?? newOperationId();
    const published = enforceOrFixed(
      errorEnvelope(requestId, err, { accountingId: id }),
      this.config.limits,
    );
    this.record({
      operationId: id,
      kind,
      envelope: published,
      baseline: noBaseline(),
      baselineCredited: false,
      reader: noReaderCost(),
      boundary: "envelope",
    });
    return published;
  }

  /** The fixed reply for a failure the session could not classify. Carries no handle. */
  safeError(requestId: string, code = "STORE_FAILED"): Envelope {
    return fixedError(requestId, code);
  }

  // -- lifecycle -------------------------------------------------------------

  /**
   * An ordinary turn boundary. Handles survive; TTL and the sweep do the work.
   *
   * This is what a per-turn host event must call. OpenClaw fires `session_end` with
   * `reason: "compaction"` while the conversation continues, and Hermes fires
   * `on_session_end` at the end of every `run_conversation` call - destroying handles at
   * either point would delete exactly the recovery state the next turn needs.
   */
  endTurn(): void {
    try {
      this.store.sweep();
    } catch (err) {
      if (!isShuntError(err)) throw err;
    }
  }

  /** A real session boundary: revoke this scope's handles and drop its artifacts. */
  close(): void {
    try {
      this.registry.expireSession(this.sessionId);
    } catch (err) {
      if (!isShuntError(err)) throw err;
    }
  }

  /** Start a new generation. Every handle from the old one stops resolving. */
  reset(generation: number): ShuntSession {
    this.close();
    return new ShuntSession(this.sessionId, this.config, this.capability, {
      provider: this.provider,
      clock: this.clock,
      metrics: this.metrics,
      store: this.store,
      identity: this.identity.withGeneration(generation),
      legacyCompactionMaxChars: this.legacyCompactionMaxChars,
    });
  }
}

function completeCoverage(): Coverage {
  const coverage = new Coverage();
  coverage.complete = true;
  coverage.upstreamTruncated = false;
  return coverage;
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

/**
 * Map an inspect selector onto the envelope's locator union.
 *
 * The envelope locator has no `bytes` or `needle` form - deliberately, because an omission
 * record is metadata and must not carry a caller's search string. A byte or search selector
 * is reported as the scope it addressed, never as its text.
 */
function omissionSelector(selector: Record<string, unknown>): Record<string, unknown> {
  if (selector["kind"] === "lines") {
    return { kind: "lines", start: Number(selector["start"]), end: Number(selector["end"]) };
  }
  return { kind: "all" };
}

/**
 * Assemble the configured provider, including an availability-only fallback chain.
 *
 * `call` is the host bridge. `fallbackCalls` maps a `"provider/model"` key to a bridge for
 * that target when the host needs a different callable per target; when it is absent the
 * same bridge is reused with a different requested target, which is the normal case for a
 * host that owns its own routing.
 */
/**
 * Assemble the configured provider, including the availability-only fallback chain.
 *
 * `overrides` replaces the *primary* target only, for a host whose own configuration
 * takes precedence over the plugin's. The fallback chain is not overridable that way,
 * because a host with such a block has no equivalent for the chain.
 */
export function buildProvider(
  config: Config,
  call: HostBridgeCall,
  fallbackCalls: Record<string, HostBridgeCall> = {},
  overrides: { model?: string; provider?: string } = {},
): ReaderProvider {
  const primary = new HostBridgeProvider(
    call,
    config.limits,
    overrides.model ?? config.readerModel,
    overrides.provider ?? config.readerProvider,
  );
  if (config.readerFallbackChain.length === 0) return primary;
  const alternatives = config.readerFallbackChain.map((ref: ProviderRef) =>
    new HostBridgeProvider(
      fallbackCalls[`${ref.provider}/${ref.model}`] ?? call,
      config.limits,
      ref.model,
      ref.provider,
    ));
  // The chain shares the same scheduling and generation budgets as its candidates.
  return new FallbackChainProvider(primary, alternatives, config.limits);
}

export { EMITTED_SCHEMA_VERSION };

/** Bound UTF-8 output without splitting a multibyte code point. */
function utf8SafeCap(text: string, maxBytes: number): string {
  const data = new TextEncoder().encode(text);
  if (data.length <= maxBytes) return text;
  let end = Math.max(0, maxBytes);
  // A UTF-8 continuation byte cannot begin a character. Drop the partial suffix rather
  // than letting TextDecoder insert U+FFFD into a deterministic summary.
  while (end > 0 && (data[end] as number) >= 0x80 && (data[end] as number) <= 0xbf) end -= 1;
  return new TextDecoder().decode(data.subarray(0, end));
}

function sourceHandle(entry: RegisteredSource): SourceHandle {
  return {
    source_id: entry.sourceId,
    snapshot_id: entry.snapshot.snapshotId,
    media_type: entry.snapshot.mediaType,
    bytes: entry.snapshot.bytesLen,
    expires_at: isoExpiry(entry.expiresAtEpoch),
  };
}

function legacyFallbackProvenance(
  base: Provenance,
  attemptsStarted: number,
  usageCompleteAttempts: number,
): Provenance {
  return {
    ...base,
    derived: false,
    label: "legacy_compaction",
    citationsMechanicallyVerified: false,
    attemptsStarted,
    usageComplete: attemptsStarted === 0 || usageCompleteAttempts === attemptsStarted,
  };
}

function fitLegacyEnvelope(
  build: (summary: string) => Envelope,
  summary: string,
  maxEnvelopeBytes: number,
): Envelope {
  let candidate = build(summary);
  if (serializedBytes(candidate) <= maxEnvelopeBytes) return candidate;
  const empty = build("");
  if (serializedBytes(empty) > maxEnvelopeBytes) {
    throw new ShuntError("LIMIT_EXCEEDED", "NO_ENVELOPE_HEADROOM", false);
  }
  // Match the incumbent fallback's wire-fit behavior: remove the measured excess in
  // UTF-8 bytes, then repeat because JSON escaping can make the reduction smaller than the
  // first estimate. This keeps summary bytes and charged disclosure in sync.
  let bounded = summary;
  while (serializedBytes(candidate) > maxEnvelopeBytes && bounded.length > 0) {
    const excess = serializedBytes(candidate) - maxEnvelopeBytes;
    const data = new TextEncoder().encode(bounded);
    const keep = Math.max(0, data.length - excess);
    const next = new TextDecoder().decode(data.subarray(0, keep));
    bounded = next === bounded && data.length > 0
      ? new TextDecoder().decode(data.subarray(0, data.length - 1))
      : next;
    candidate = build(bounded);
  }
  if (serializedBytes(candidate) > maxEnvelopeBytes) {
    throw new ShuntError("LIMIT_EXCEEDED", "NO_ENVELOPE_HEADROOM", false);
  }
  return candidate;
}
