/**
 * Optional Suma oversized-result mode: pure spill and pointer.
 *
 * This engine never summarizes. It has no model bridge at all, which is the point: the
 * old heuristic compactor is replaced, not chained. An oversized result is written to a
 * private cache, read back and hash-verified, registered as an internal source, and only
 * then replaced with a pointer envelope. Reading it later goes through the question-driven
 * reader like any other source.
 *
 * Failure never falls back to the raw payload: quota exhaustion, a full disk, a permission
 * error, an unserializable value and a readback mismatch all produce the same bounded
 * `SPILL_FAILED` envelope.
 *
 * Enabling this on a host additionally requires proof that the host captures the complete
 * result before truncation and accepts a safe replacement before persistence and context
 * insertion. On OpenClaw that proof does not exist - see `docs/capability-matrix.md` - so
 * the mode stays disabled there.
 */
import { createHash, randomBytes } from "node:crypto";
import { chmodSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, rmdirSync, unlinkSync, writeSync } from "node:fs";
import { join } from "node:path";

import { Coverage, Envelope, buildEnvelope, errorEnvelope, isoExpiry } from "./envelope.js";
import { ShuntError, isShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { SourceRegistry } from "./registry.js";
import { assertSupportedBlocks, canonicalJson, jsonDepthAndNodes, snapshotBytes } from "./snapshot.js";

const DIR_MODE = 0o700;
const FILE_MODE = 0o600;

export interface SpillOutcome {
  readonly action: "passthrough" | "spill" | "blocked" | "error";
  readonly envelope?: Envelope;
  readonly code?: string;
  readonly bytesMeasured: number;
}

/** Private, quota'd, TTL'd cache outside every workspace root. */
export class SpillStore {
  private readonly usedBySession = new Map<string, number>();

  constructor(
    readonly root: string,
    readonly limits: Limits = DEFAULT_LIMITS,
  ) {
    mkdirSync(root, { recursive: true, mode: DIR_MODE });
    chmodSync(root, DIR_MODE);
  }

  usedBytes(sessionId: string): number {
    return this.usedBySession.get(sessionId) ?? 0;
  }

  seedUsage(sessionId: string, used: number): void {
    this.usedBySession.set(sessionId, used);
  }

  private sessionDir(sessionId: string): string {
    const safe = createHash("sha256").update(sessionId).digest("hex").slice(0, 32);
    const dir = join(this.root, safe);
    mkdirSync(dir, { recursive: true, mode: DIR_MODE });
    chmodSync(dir, DIR_MODE);
    return dir;
  }

  /** Atomically publish, then read back and verify before the caller may use it. */
  write(sessionId: string, data: Uint8Array): string {
    if (this.usedBytes(sessionId) + data.length > this.limits.sessionSpillQuotaBytes) {
      throw new ShuntError("SPILL_FAILED", "QUOTA_EXCEEDED", false);
    }
    const dir = this.sessionDir(sessionId);
    const hex = createHash("sha256").update(data).digest("hex");
    const final = join(dir, `${hex}.spill`);
    const tmp = join(dir, `${randomBytes(8).toString("hex")}.part`);
    try {
      const fd = openSync(tmp, "wx", FILE_MODE);
      try {
        writeSync(fd, data);
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      chmodSync(tmp, FILE_MODE);
      renameSync(tmp, final);
    } catch {
      unlinkQuiet(tmp);
      throw new ShuntError("SPILL_FAILED", "WRITE_FAILED", false);
    }
    let readback: Buffer;
    try {
      readback = readFileSync(final);
    } catch {
      unlinkQuiet(final);
      throw new ShuntError("SPILL_FAILED", "READBACK_FAILED", false);
    }
    if (createHash("sha256").update(readback).digest("hex") !== hex) {
      unlinkQuiet(final);
      throw new ShuntError("SPILL_FAILED", "READBACK_MISMATCH", false);
    }
    this.usedBySession.set(sessionId, this.usedBytes(sessionId) + data.length);
    return final;
  }

  /** Remove a session's artifacts. This is deletion, not secure erasure. */
  purgeSession(sessionId: string): number {
    const safe = createHash("sha256").update(sessionId).digest("hex").slice(0, 32);
    const dir = join(this.root, safe);
    let removed = 0;
    if (existsSync(dir)) {
      for (const name of readdirSync(dir)) {
        unlinkQuiet(join(dir, name));
        removed += 1;
      }
      try {
        rmdirSync(dir);
      } catch {
        /* a concurrent write may repopulate it; the TTL sweep gets it next */
      }
    }
    this.usedBySession.delete(sessionId);
    return removed;
  }
}

function unlinkQuiet(path: string): void {
  try {
    unlinkSync(path);
  } catch {
    /* already gone */
  }
}

export class SumaSpillEngine {
  constructor(
    private readonly store: SpillStore,
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
    if (internalSourceId && this.registry.isInternal(sessionId, internalSourceId)) {
      // Registry-verified internal envelope: never spill our own pointer again.
      return { action: "passthrough", bytesMeasured: 0 };
    }

    let serialized: Uint8Array;
    try {
      serialized = this.serialize(result);
    } catch (err) {
      if (!isShuntError(err)) throw err;
      return {
        action: err.code === "BINARY_UNSUPPORTED" ? "blocked" : "error",
        envelope: errorEnvelope(requestId, err),
        code: err.code,
        bytesMeasured: 0,
      };
    }

    const size = serialized.length;
    if (size > this.limits.maxSourceBytes) {
      const err = new ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", false);
      return { action: "error", envelope: errorEnvelope(requestId, err), code: err.code, bytesMeasured: size };
    }
    if (size <= this.limits.maxToolResultBytes) {
      return { action: "passthrough", bytesMeasured: size };
    }

    let entry;
    try {
      this.store.write(sessionId, serialized);
      entry = this.registry.register(sessionId, snapshotBytes(serialized), true);
    } catch (err) {
      const code = isShuntError(err) && err.code === "UNSAFE_SOURCE" ? "UNSAFE_SOURCE" : "SPILL_FAILED";
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
      guidance:
        "The tool result was too large for this conversation and was moved out of it. " +
        "Ask the context-shunt reader a question about this pointer to get a cited answer.",
    });
    return { action: "spill", envelope, code: "SPILLED", bytesMeasured: size };
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
  const isBlockList = (value: unknown): value is unknown[] =>
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((b) => typeof b === "object" && b !== null && "type" in (b as object));
  if (isBlockList(result)) return result;
  if (typeof result === "object" && result !== null) {
    const content = (result as Record<string, unknown>)["content"];
    if (isBlockList(content)) return content;
  }
  return null;
}
