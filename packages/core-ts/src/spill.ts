/**
 * Optional oversized post-tool mode: pure spill and pointer.
 *
 * This engine never summarizes. It has no model bridge at all, which is the point: the old
 * heuristic compactor is replaced, not chained. An eligible oversized result is validated,
 * published through the hybrid store as an internal handle, and only then replaced with a
 * pointer envelope. Reading it later goes through the question-driven reader or the
 * deterministic inspect path like any other handle.
 *
 * Failure never falls back to the raw payload: quota exhaustion, a full disk, a permission
 * error, an unserializable value and a content mismatch all produce the same bounded
 * failure envelope with no handle.
 *
 * Only an *explicitly eligible oversized candidate* is captured: a result that serializes
 * above `maxToolResultBytes`. A short result passes through untouched and is never stored,
 * so this path cannot become a shadow log of every tool call.
 *
 * Enabling this on a host additionally requires proof that the host captures the complete
 * result before truncation and accepts a safe replacement before persistence and context
 * insertion. On OpenClaw that proof does not exist - see `docs/capability-matrix.md` - so
 * the mode stays disabled there. The engine remains present and tested behind that
 * capability gate rather than being deleted, so the day a host does provide the ordering
 * there is a tested implementation to enable.
 */
import { Coverage, Envelope, buildEnvelope, errorEnvelope, isoExpiry } from "./envelope.js";
import { ShuntError, isShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { deterministicProvenance } from "./provenance.js";
import { SourceRegistry } from "./registry.js";
import {
  assertSupportedBlocks, canonicalJson, jsonDepthAndNodes, snapshotBytes,
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
  ) {}

  /** Decide what the host should do with one complete tool result. */
  evaluate(
    sessionId: string,
    requestId: string,
    result: unknown,
    internalSourceId?: string,
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
        : new ShuntError("SPILL_FAILED", "SERIALIZE_FAILED", false);
      return {
        action: safe.code === "BINARY_UNSUPPORTED" ? "blocked" : "error",
        envelope: errorEnvelope(requestId, safe),
        code: safe.code,
        bytesMeasured: 0,
      };
    }

    const size = serialized.length;
    if (size > this.limits.maxSourceBytes) {
      const err = new ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", false);
      return { action: "error", envelope: errorEnvelope(requestId, err), code: err.code, bytesMeasured: size };
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
      const passthroughCodes = ["SPILL_FAILED", "UNSAFE_SOURCE", "STORE_FAILED", "LIMIT_EXCEEDED"];
      const code = isShuntError(err) && passthroughCodes.includes(err.code)
        ? err.code
        : "SPILL_FAILED";
      const detail = isShuntError(err) ? err.detail : undefined;
      const safe = new ShuntError(code, detail, false);
      return { action: "error", envelope: errorEnvelope(requestId, safe), code, bytesMeasured: size };
    }

    const expiresAt = isoExpiry(entry.expiresAtEpoch);
    const coverage = new Coverage();
    coverage.upstreamTruncated = null;
    const envelope = buildEnvelope({
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
      guidance:
        "The tool result was too large for this conversation and was moved out of it. " +
        "Ask the context-shunt reader a question about this pointer for a cited answer, " +
        "or use context_shunt_inspect for exact lines.",
    });
    return {
      action: "spill",
      envelope,
      code: "SPILLED",
      bytesMeasured: size,
      sourceId: entry.sourceId,
    };
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
