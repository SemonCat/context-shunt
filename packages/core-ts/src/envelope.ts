/**
 * The single bounded reply shape.
 *
 * Every gate, reader and spill path returns this object. Construction goes through
 * `buildEnvelope` so an illegal status/code pairing or a coverage claim that outruns what
 * actually happened cannot be expressed.
 */
import { RETRYABLE_CODES, ShuntError } from "./errors.js";
import { SCHEMA_VERSION, legalPair } from "./limits.js";

export const OMISSION_REASONS = new Set([
  "BUDGET_EXCEEDED", "TIMEOUT", "CANCELLED", "CHUNK_FAILED", "MODEL_ERROR",
  "INVALID_MODEL_OUTPUT", "CITATION_INVALID", "UPSTREAM_TRUNCATED", "UNKNOWN_REMAINDER",
]);

export interface Omission {
  source_id: string;
  selector: Record<string, unknown>;
  reason: string;
}

export interface CoverageShape {
  complete: boolean;
  processed_chunks: number;
  planned_chunks: number;
  omitted: Omission[];
  upstream_truncated: boolean | null;
}

export interface Citation {
  id: string;
  source_id: string;
  snapshot_id: string;
  locator: Record<string, unknown>;
  quote: string;
  verified: true;
}

export interface SourceHandle {
  source_id: string;
  snapshot_id: string;
  media_type: string;
  bytes: number;
  expires_at: string;
}

export interface SpillPointer {
  source_id: string;
  snapshot_id: string;
  bytes: number;
  expires_at: string;
  internal: true;
}

export interface Envelope {
  schema_version: string;
  request_id: string;
  status: "ok" | "partial" | "blocked" | "error";
  code: string;
  answer: string;
  citations: Citation[];
  coverage: CoverageShape;
  sources: SourceHandle[];
  retryable: boolean;
  guidance?: string;
  pointer?: SpillPointer;
}

export class Coverage {
  complete = false;
  processedChunks = 0;
  plannedChunks = 0;
  upstreamTruncated: boolean | null = null;
  readonly omitted: Omission[] = [];

  omit(sourceId: string, selector: Record<string, unknown>, reason: string): void {
    if (!OMISSION_REASONS.has(reason)) throw new Error(`unknown omission reason: ${reason}`);
    this.omitted.push({ source_id: sourceId, selector, reason });
  }

  toShape(): CoverageShape {
    return {
      complete: this.complete,
      processed_chunks: this.processedChunks,
      planned_chunks: this.plannedChunks,
      omitted: [...this.omitted],
      upstream_truncated: this.upstreamTruncated,
    };
  }
}

export function isoExpiry(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export interface BuildOptions {
  requestId: string;
  status: Envelope["status"];
  code: string;
  answer?: string;
  citations?: Citation[];
  coverage?: Coverage;
  sources?: SourceHandle[];
  retryable?: boolean;
  guidance?: string;
  pointer?: SpillPointer;
}

export function buildEnvelope(opts: BuildOptions): Envelope {
  if (!legalPair(opts.status, opts.code)) {
    throw new Error(`illegal status/code pairing: ${opts.status}/${opts.code}`);
  }
  const coverage = opts.coverage ?? new Coverage();
  if (opts.status !== "ok" && coverage.complete) {
    throw new Error("only an ok result may claim complete coverage");
  }
  const answer = opts.answer ?? "";
  const citations = opts.citations ?? [];
  if (opts.code === "SPILLED") {
    if (answer.length > 0 || citations.length > 0) {
      throw new Error("SPILLED must carry no answer and no citations");
    }
    if (!opts.pointer) throw new Error("SPILLED requires a pointer");
  }
  const envelope: Envelope = {
    schema_version: SCHEMA_VERSION,
    request_id: opts.requestId,
    status: opts.status,
    code: opts.code,
    answer,
    citations,
    coverage: coverage.toShape(),
    sources: opts.sources ?? [],
    retryable: opts.retryable ?? RETRYABLE_CODES.has(opts.code),
  };
  if (opts.guidance) envelope.guidance = opts.guidance;
  if (opts.pointer) envelope.pointer = opts.pointer;
  return envelope;
}

const BLOCKED_CODES = new Set([
  "LARGE_READ", "UNCLASSIFIABLE_READ", "UNSAFE_SOURCE", "BINARY_UNSUPPORTED", "HOST_UNSAFE",
]);

/** Map a bounded failure to an envelope. The exception message never rides along. */
export function errorEnvelope(requestId: string, err: ShuntError, guidance?: string): Envelope {
  const opts: BuildOptions = {
    requestId,
    status: BLOCKED_CODES.has(err.code) ? "blocked" : "error",
    code: err.code,
    retryable: err.retryable,
  };
  if (guidance) opts.guidance = guidance;
  return buildEnvelope(opts);
}

export function serializedBytes(envelope: unknown): number {
  return new TextEncoder().encode(JSON.stringify(envelope)).length;
}
