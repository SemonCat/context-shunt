/**
 * Per-session wiring shared by both adapters.
 *
 * Adapters normalize host events into `(tool, args)` and model calls; everything below -
 * gate, registration, reader, spill, guard, metrics - lives here so the two hosts cannot
 * drift apart on semantics.
 */
import { CapabilityReport, modeEnabled } from "./capability.js";
import { Clock, monotonicClock } from "./clock.js";
import { Config } from "./config.js";
import { Coverage, Envelope, buildEnvelope, errorEnvelope } from "./envelope.js";
import { ShuntError, isShuntError } from "./errors.js";
import { GateDecision, PreReadGate, guidanceFor } from "./gate.js";
import { enforceOrFixed } from "./guard.js";
import { MetricsSink, nullMetrics } from "./metrics.js";
import { authorize, pathPolicy, readAuthorizedBounded } from "./paths.js";
import { fileProber } from "./probe.js";
import { LunaProvider, UnavailableProvider } from "./provider.js";
import { Reader } from "./reader.js";
import { RegisteredSource, SourceRegistry } from "./registry.js";
import { JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE, snapshotBytes } from "./snapshot.js";
import { SpillOutcome, SpillStore, SumaSpillEngine } from "./spill.js";

export class ShuntSession {
  private readonly gate: PreReadGate;
  private readonly reader: Reader;
  private readonly spillStore: SpillStore;
  readonly registry: SourceRegistry;
  readonly spill: SumaSpillEngine;

  constructor(
    readonly sessionId: string,
    readonly config: Config,
    readonly capability: CapabilityReport,
    opts: {
      provider?: LunaProvider;
      clock?: Clock;
      metrics?: MetricsSink;
      registry?: SourceRegistry;
    } = {},
  ) {
    const clock = opts.clock ?? monotonicClock;
    const metrics = opts.metrics ?? nullMetrics;
    this.registry = opts.registry ?? new SourceRegistry(config.limits);
    this.gate = new PreReadGate(fileProber(config.limits), config.limits, clock);
    const provider = opts.provider ?? new UnavailableProvider();
    this.reader = new Reader(this.registry, provider, config.limits, clock, metrics);
    this.spillStore = new SpillStore(config.spillDir, config.limits);
    this.spill = new SumaSpillEngine(
      this.spillStore,
      this.registry,
      config.limits,
      config.sumaPostToolEnabled && modeEnabled(capability, "suma_post_tool"),
    );
    this.metrics = metrics;
  }

  private readonly metrics: MetricsSink;

  get sumaEnabled(): boolean {
    return this.spill.enabled;
  }

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
    const coverage = new Coverage();
    coverage.upstreamTruncated = null;
    return enforceOrFixed(
      buildEnvelope({
        requestId,
        status: "blocked",
        code: decision.code ?? "UNCLASSIFIABLE_READ",
        coverage,
        retryable: false,
        guidance: guidanceFor(decision),
      }),
      this.config.limits,
    );
  }

  registerPath(path: string, mediaType?: string): RegisteredSource {
    const authorized = authorize(path, pathPolicy(this.config.workspaceRoots, this.config.denylist));
    const data = readAuthorizedBounded(authorized, this.config.limits.maxSourceBytes);
    const hint = mediaType ?? (authorized.real.toLowerCase().endsWith(".json") ? JSON_MEDIA_TYPE : TEXT_MEDIA_TYPE);
    return this.registry.register(this.sessionId, snapshotBytes(data, hint, this.config.limits));
  }

  async read(request: unknown, signal?: AbortSignal): Promise<Envelope> {
    if (!this.config.readerEnabled) {
      const err = new ShuntError("INVALID_REQUEST", "READER_DISABLED", false);
      const requestId =
        typeof request === "object" && request !== null
          ? String((request as Record<string, unknown>)["request_id"] ?? "req_unknown")
          : "req_unknown";
      return enforceOrFixed(errorEnvelope(requestId, err), this.config.limits);
    }
    const envelope = await this.reader.answer(this.sessionId, request, undefined, signal);
    return enforceOrFixed(envelope, this.config.limits);
  }

  /** Only ever consulted when the capability probe proved the host order is safe. */
  postToolResult(requestId: string, result: unknown, internalSourceId?: string): SpillOutcome | null {
    if (!this.sumaEnabled) return null;
    let outcome: SpillOutcome;
    try {
      outcome = this.spill.evaluate(this.sessionId, requestId, result, internalSourceId);
    } catch (err) {
      if (!isShuntError(err)) throw err;
      outcome = {
        action: "error",
        envelope: errorEnvelope(requestId, new ShuntError("SPILL_FAILED", err.detail, false)),
        code: "SPILL_FAILED",
        bytesMeasured: 0,
      };
    }
    const guarded: SpillOutcome = outcome.envelope
      ? { ...outcome, envelope: enforceOrFixed(outcome.envelope, this.config.limits) }
      : outcome;
    this.metrics.count("suma_outcome", { result: guarded.action });
    return guarded;
  }

  /** Session teardown removes handles and private artifacts. */
  close(): void {
    this.registry.expireSession(this.sessionId);
    this.spillStore.purgeSession(this.sessionId);
  }
}
