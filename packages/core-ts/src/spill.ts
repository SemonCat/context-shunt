/**
 * Optional oversized post-tool mode: pure spill and pointer.
 *
 * This engine has no model bridge. An eligible oversized result is validated, published
 * through the hybrid store as an internal handle, and only then replaced with a pointer
 * envelope. If a Shunt-owned capture/store/pointer failure occurs while the complete source
 * is still available, it emits the bounded incumbent compaction instead. Reading a published
 * handle later goes through the question-driven reader or deterministic inspect path like
 * any other handle.
 *
 * Failure never falls back to the raw payload. Caller and safety refusals remain explicit;
 * eligible Shunt-owned failures use the incumbent bounded compactor while bytes are private.
 *
 * Only an *explicitly eligible oversized candidate* is captured: a result that serializes
 * above `maxToolResultBytes`. A short result passes through untouched and is never stored,
 * so this path cannot become a shadow log of every tool call.
 *
 * Enabling this engine as a host post-tool mode requires proof that the host captures the
 * complete result before truncation and accepts a safe replacement before persistence and
 * context insertion. A host middleware may provide a separate, bounded visibility path; this
 * engine cannot infer completeness after an upstream sanitizer has run. The adapter owns that
 * seam-specific capability report and must refuse or label cap-boundary input conservatively.
 */
import {
  Coverage,
  Envelope,
  type LegacyCompactionShape,
  buildEnvelope,
  errorEnvelope,
  isoExpiry,
  recoveryFor,
  serializedBytes,
} from "./envelope.js";
import {
  ShuntError,
  fallbackAllowed,
  isShuntError,
  safeFailureDetail,
} from "./errors.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import {
  incumbentCompactToolResult,
  DEFAULT_LEGACY_SESSION_HARD_CHARS,
} from "./legacy-compact.js";
import { deterministicProvenance } from "./provenance.js";
import { SourceRegistry } from "./registry.js";
import {
  assertNoSecret,
  assertSupportedBlocks,
  assertText,
  canonicalJson,
  jsonDepthAndNodes,
  snapshotBytes,
} from "./snapshot.js";

export interface SpillOutcome {
  readonly action: "passthrough" | "spill" | "blocked" | "error";
  readonly envelope?: Envelope;
  readonly code?: string;
  readonly bytesMeasured: number;
  readonly sourceId?: string;
}

export class SpillEngine {
  constructor(
    private readonly registry: SourceRegistry,
    private readonly limits: Limits = DEFAULT_LIMITS,
    readonly enabled = false,
    private readonly legacyCompactionMaxChars = DEFAULT_LEGACY_SESSION_HARD_CHARS,
  ) {}

  /** Decide what the host should do with one complete tool result. */
  evaluate(
    sessionId: string,
    requestId: string,
    result: unknown,
    internalSourceId?: string,
    accountingId?: string,
  ): SpillOutcome {
    if (!this.enabled) return { action: "passthrough", bytesMeasured: 0 };
    if (internalSourceId && this.registry.isInternal(sessionId, internalSourceId)) {
      // Store-verified internal envelope: never spill our own pointer again.
      return { action: "passthrough", bytesMeasured: 0 };
    }

    let serialized: Uint8Array;
    try {
      serialized = this.serialize(result);
    } catch (err) {
      const safe = isShuntError(err)
        ? err
        : new ShuntError("SPILL_FAILED", "INTERNAL_ERROR", false);
      return {
        action: safe.code === "BINARY_UNSUPPORTED" ? "blocked" : "error",
        envelope: errorEnvelope(
          requestId,
          safe,
          accountingId !== undefined ? { accountingId } : {},
        ),
        code: safe.code,
        bytesMeasured: 0,
      };
    }

    const size = serialized.length;
    if (size > this.limits.maxSourceBytes) {
      const err = new ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", false);
      return {
        action: "error",
        envelope: errorEnvelope(
          requestId,
          err,
          accountingId !== undefined ? { accountingId } : {},
        ),
        code: err.code,
        bytesMeasured: size,
      };
    }
    if (size <= this.limits.maxToolResultBytes) {
      // Not an eligible oversized candidate. Nothing is captured.
      return { action: "passthrough", bytesMeasured: size };
    }

    let entry;
    try {
      // Validate before persistence so a binary/secret/invalid payload cannot leave an
      // orphaned artifact after the operation is rejected.
      const snapshot = snapshotBytes(serialized, undefined, this.limits);
      entry = this.registry.register(sessionId, snapshot, true, "spilled_tool");
    } catch (err) {
      const safe = isShuntError(err)
        ? err
        : new ShuntError("STORE_FAILED", "INTERNAL_ERROR", false);
      return this.failureOutcome(
        sessionId,
        requestId,
        safe,
        serialized,
        size,
        accountingId,
      );
    }

    const expiresAt = isoExpiry(entry.expiresAtEpoch);
    const coverage = new Coverage();
    coverage.upstreamTruncated = null;
    let envelope: Envelope;
    try {
      envelope = buildEnvelope({
        requestId,
        status: "ok",
        code: "SPILLED",
        coverage,
        sources: [
          {
            source_id: entry.sourceId,
            snapshot_id: entry.snapshot.snapshotId,
            media_type: entry.snapshot.mediaType,
            bytes: size,
            expires_at: expiresAt,
          },
        ],
        retryable: false,
        pointer: {
          source_id: entry.sourceId,
          snapshot_id: entry.snapshot.snapshotId,
          bytes: size,
          expires_at: expiresAt,
          internal: true,
        },
        resultKind: "pointer",
        provenance: deterministicProvenance("pointer_only"),
        ...(accountingId !== undefined ? { accountingId } : {}),
        guidance:
          "The tool result was too large for this conversation and was moved out of it. " +
          "Ask the context-shunt reader a question about this pointer for a cited answer, " +
          "or use context_shunt_inspect for exact lines.",
      });
    } catch (err) {
      const safe = isShuntError(err)
        ? err
        : new ShuntError("SPILL_FAILED", "INTERNAL_ERROR", false);
      return this.failureOutcome(
        sessionId,
        requestId,
        safe,
        serialized,
        size,
        accountingId,
        entry.sourceId,
      );
    }
    return {
      action: "spill",
      envelope,
      code: "SPILLED",
      bytesMeasured: size,
      sourceId: entry.sourceId,
    };
  }

  /**
   * Recover an adapter/session boundary that threw after receiving a complete tool result.
   *
   * The normal evaluate path catches its own failures, but an adapter can still observe a
   * Shunt-owned exception while bootstrapping or replacing that result. Re-serializing here
   * is safe: the returned outcome contains only a bounded envelope, and the raw bytes stay
   * inside this method. A short result is left alone because it was never eligible for spill.
   */
  recoverUnexpected(
    sessionId: string,
    requestId: string,
    result: unknown,
    accountingId?: string,
  ): SpillOutcome {
    if (!this.enabled) return { action: "passthrough", bytesMeasured: 0 };
    let serialized: Uint8Array;
    try {
      serialized = this.serialize(result);
    } catch (err) {
      const safe = isShuntError(err)
        ? err
        : new ShuntError("SPILL_FAILED", "INTERNAL_ERROR", false);
      return {
        action: safe.code === "BINARY_UNSUPPORTED" ? "blocked" : "error",
        envelope: errorEnvelope(
          requestId,
          safe,
          accountingId !== undefined ? { accountingId } : {},
        ),
        code: safe.code,
        bytesMeasured: 0,
      };
    }
    const size = serialized.length;
    if (size <= this.limits.maxToolResultBytes) {
      return { action: "passthrough", bytesMeasured: size };
    }
    if (size > this.limits.maxSourceBytes) {
      const err = new ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", false);
      return {
        action: "error",
        envelope: errorEnvelope(
          requestId,
          err,
          accountingId !== undefined ? { accountingId } : {},
        ),
        code: err.code,
        bytesMeasured: size,
      };
    }
    try {
      // This boundary did not run the normal capture validation. Repeat only the safety
      // checks before allowing the compactor to see the bytes; rebuilding a line index here
      // could repeat the indexing failure we are recovering from.
      const text = assertText(serialized);
      assertNoSecret(text, "SOURCE");
    } catch (err) {
      const safe = isShuntError(err)
        ? err
        : new ShuntError("SPILL_FAILED", "INTERNAL_ERROR", false);
      return {
        action: safe.code === "BINARY_UNSUPPORTED" ? "blocked" : "error",
        envelope: errorEnvelope(
          requestId,
          safe,
          accountingId !== undefined ? { accountingId } : {},
        ),
        code: safe.code,
        bytesMeasured: size,
      };
    }
    return this.failureOutcome(
      sessionId,
      requestId,
      new ShuntError("SPILL_FAILED", "INTERNAL_ERROR", false),
      serialized,
      size,
      accountingId,
    );
  }

  /**
   * Turn a Shunt-owned capture/store failure into the incumbent bounded compaction while
   * the serialized candidate is still in memory. The bytes never travel in the public
   * outcome, so a host cannot accidentally pass them through as a recovery payload.
   */
  private failureOutcome(
    sessionId: string,
    requestId: string,
    failure: ShuntError,
    serialized: Uint8Array,
    size: number,
    accountingId?: string,
    sourceId?: string,
  ): SpillOutcome {
    if (!fallbackAllowed(failure.code, failure.detail)) {
      return {
        action: failure.code === "BINARY_UNSUPPORTED" ? "blocked" : "error",
        envelope: errorEnvelope(
          requestId,
          failure,
          accountingId !== undefined ? { accountingId } : {},
        ),
        code: failure.code,
        bytesMeasured: size,
      };
    }

    let entry: ReturnType<SourceRegistry["resolve"]> | undefined;
    if (sourceId !== undefined) {
      try {
        entry = this.registry.resolve(sessionId, sourceId);
      } catch (err) {
        // Handle expiry, snapshot/content mismatch and unknown-handle errors are safety
        // boundaries. The raw candidate may still be in memory, but publishing a new
        // summary would hide the failed binding rather than preserve it.
        if (isShuntError(err) && !fallbackAllowed(err.code, err.detail)) {
          return {
            action: "error",
            envelope: errorEnvelope(
              requestId,
              err,
              accountingId !== undefined ? { accountingId } : {},
            ),
            code: err.code,
            bytesMeasured: size,
          };
        }
        // The serialized candidate remains available even if the just-created handle
        // cannot be reloaded. The fallback is therefore valid but intentionally carries
        // no handle pair and tells the caller that recovery handles are unavailable.
        entry = undefined;
      }
    }

    try {
      const text = assertText(serialized);
      assertNoSecret(text, "SOURCE");
      const allowance = entry === undefined
        ? undefined
        : this.registry.store.disclosureAllowance(this.registry.identity, entry.sourceId);
      const remaining = allowance === undefined
        ? this.limits.maxExtractionBytes
        : Math.min(allowance.perSourceRemaining, allowance.perSessionRemaining);
      if (remaining <= 0) {
        const exhausted = new ShuntError("DISCLOSURE_EXHAUSTED");
        return {
          action: "error",
          envelope: errorEnvelope(
            requestId,
            exhausted,
            {
              ...(accountingId !== undefined ? { accountingId } : {}),
              ...(entry === undefined ? {} : {
                sources: [sourceHandle(entry)],
                handlesValid: true,
              }),
            },
          ),
          code: exhausted.code,
          bytesMeasured: size,
          ...(entry === undefined ? {} : { sourceId: entry.sourceId }),
        };
      }
      const summary = utf8SafeCap(
        incumbentCompactToolResult(text, { hardChars: this.legacyCompactionMaxChars }),
        Math.min(this.limits.maxExtractionBytes, remaining),
      );
      const sourceHandles = entry === undefined ? [] : [sourceHandle(entry)];
      const coverage = new Coverage();
      coverage.upstreamTruncated = null;
      if (entry !== undefined) coverage.omit(entry.sourceId, { kind: "all" }, "UNKNOWN_REMAINDER");
      const legacy: LegacyCompactionShape = {
        deterministic: true,
        summary,
        summary_bytes: new TextEncoder().encode(summary).length,
        original_bytes: size,
        hard_cap_chars: this.legacyCompactionMaxChars,
        original_failure: failure.code as LegacyCompactionShape["original_failure"],
        ...(entry === undefined
          ? {}
          : { source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId }),
      };
      const build = (boundedSummary: string): Envelope => buildEnvelope({
        requestId,
        status: "partial",
        code: "LEGACY_COMPACTED",
        coverage,
        sources: sourceHandles,
        retryable: false,
        resultKind: "legacy_compaction",
        provenance: { ...deterministicProvenance("legacy_compaction"), citationsMechanicallyVerified: false },
        ...(accountingId !== undefined ? { accountingId } : {}),
        failureDetail: safeFailureDetail(failure.detail),
        guidance:
          "Escape hatch: deterministic legacy-shaped compaction of the source, ported from "
          + "the incumbent tool-result compactor; not model-derived and not an LLM summary. "
          + "Original capture failure: " + failure.code
          + ". Covers only the retained middleware-visible result; omitted structure and "
          + "bytes are not complete source coverage.",
        recovery: recoveryFor(failure.code, entry !== undefined),
        legacyCompaction: {
          ...legacy,
          summary: boundedSummary,
          summary_bytes: new TextEncoder().encode(boundedSummary).length,
        },
      });
      const candidate = fitLegacyEnvelope(build, summary, this.limits.maxEnvelopeBytes);
      const publishedSummaryBytes = candidate.legacy_compaction?.summary_bytes ?? 0;
      if (entry !== undefined) {
        const charge = this.registry.store.chargeDisclosure(
          this.registry.identity,
          entry.sourceId,
          "bytes",
          publishedSummaryBytes,
        );
        if (!charge.granted) {
          const exhausted = new ShuntError("DISCLOSURE_EXHAUSTED");
          return {
            action: "error",
            envelope: errorEnvelope(
              requestId,
              exhausted,
              {
                ...(accountingId !== undefined ? { accountingId } : {}),
                sources: [sourceHandle(entry)],
                handlesValid: true,
              },
            ),
            code: exhausted.code,
            bytesMeasured: size,
            sourceId: entry.sourceId,
          };
        }
      }
      return {
        action: "error",
        envelope: candidate,
        code: candidate.code,
        bytesMeasured: size,
        ...(entry === undefined ? {} : { sourceId: entry.sourceId }),
      };
    } catch (err) {
      // A malformed/binary candidate or a compactor failure is never a reason to pass the
      // original result through. Preserve the bounded failure that caused this path.
      const safe = isShuntError(err) && !fallbackAllowed(err.code, err.detail)
        ? err
        : failure;
      return {
        action: "error",
        envelope: errorEnvelope(
          requestId,
          safe,
          accountingId !== undefined ? { accountingId } : {},
        ),
        code: safe.code,
        bytesMeasured: size,
      };
    }
  }

  /** Deterministic serialization of string, object, array or content-block results. */
  private serialize(result: unknown): Uint8Array {
    if (result instanceof Uint8Array) return result;
    if (typeof result === "string") return new TextEncoder().encode(result);
    const blocks = contentBlocks(result);
    if (blocks) assertSupportedBlocks(blocks);
    jsonDepthAndNodes(result, this.limits);
    const text = canonicalJson(result);
    if (text === undefined) throw new ShuntError("LIMIT_EXCEEDED", "UNSERIALIZABLE_RESULT", false);
    return new TextEncoder().encode(text);
  }
}

function sourceHandle(entry: ReturnType<SourceRegistry["resolve"]>): {
  source_id: string;
  snapshot_id: string;
  media_type: string;
  bytes: number;
  expires_at: string;
} {
  return {
    source_id: entry.sourceId,
    snapshot_id: entry.snapshot.snapshotId,
    media_type: entry.snapshot.mediaType,
    bytes: entry.snapshot.bytesLen,
    expires_at: isoExpiry(entry.expiresAtEpoch),
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
    bounded = new TextDecoder().decode(data.subarray(0, keep));
    if (bounded === summary && data.length > 0) {
      bounded = new TextDecoder().decode(data.subarray(0, data.length - 1));
    }
    candidate = build(bounded);
  }
  if (serializedBytes(candidate) > maxEnvelopeBytes) {
    throw new ShuntError("LIMIT_EXCEEDED", "NO_ENVELOPE_HEADROOM", false);
  }
  return candidate;
}

/** Bound UTF-8 output without splitting a multibyte code point. */
function utf8SafeCap(text: string, maxBytes: number): string {
  const data = new TextEncoder().encode(text);
  if (data.length <= maxBytes) return text;
  let end = Math.max(0, maxBytes);
  while (end > 0 && (data[end] as number) >= 0x80 && (data[end] as number) <= 0xbf) end -= 1;
  return new TextDecoder().decode(data.subarray(0, end));
}

function contentBlocks(result: unknown): unknown[] | null {
  if (typeof result === "object" && result !== null) {
    const content = (result as Record<string, unknown>)["content"];
    if (Array.isArray(content)) return content;
  }
  if (
    Array.isArray(result) &&
    result.some((b) => typeof b === "object" && b !== null && "type" in (b as object))
  ) {
    return result;
  }
  return null;
}

/** Historical name kept so existing adapters and gates keep importing successfully. */
export const SumaSpillEngine = SpillEngine;
export type SumaSpillEngine = SpillEngine;
