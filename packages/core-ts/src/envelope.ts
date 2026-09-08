/**
 * The single bounded reply shape.
 *
 * Every gate, reader, inspect, stats and spill path returns this object. Construction goes
 * through `buildEnvelope` so an illegal status/code pairing, a coverage claim that outruns
 * what actually happened, or a 1.1 envelope missing its mandatory provenance cannot be
 * expressed.
 *
 * Revision 1.1 adds three mandatory fields to every envelope this core emits:
 * `result_kind` (what the envelope is, so a model-derived answer can never be mistaken for
 * raw source), `provenance` (where the content came from, including the truthful
 * attribution status) and `accounting_id` (an opaque pointer to the operation's metrics
 * record, written *after* the envelope is serialized so the envelope never measures
 * itself).
 *
 * A 1.0 envelope stays exactly a 1.0 envelope: `buildEnvelope` will not attach a 1.1 block
 * to one, because declaring the older revision while carrying newer fields is a version lie
 * rather than a compatible extension.
 */
import { RETRYABLE_CODES, ShuntError } from "./errors.js";
import { EMITTED_SCHEMA_VERSION, legalPair } from "./limits.js";
import {
  type Provenance,
  type ProvenanceLabel,
  type ProvenanceShape,
  type ResultKind,
  deterministicProvenance,
  provenanceToShape,
} from "./provenance.js";
import type { OperationRecordShape, StatsTotalsShape } from "./accounting.js";

/** The contract's ceiling on `coverage.omitted` (envelope.schema.json, maxItems). */
export const MAX_OMISSIONS = 32;

export const OMISSION_REASONS = new Set([
  "BUDGET_EXCEEDED", "TIMEOUT", "CANCELLED", "CHUNK_FAILED", "MODEL_ERROR",
  "INVALID_MODEL_OUTPUT", "CITATION_INVALID", "UPSTREAM_TRUNCATED", "UNKNOWN_REMAINDER",
  "DISCLOSURE_EXHAUSTED", "SCAN_BUDGET_EXHAUSTED", "PROVENANCE_UNAVAILABLE",
]);

export const RECOVERY_ACTIONS = new Set([
  "RETRY_SAME_QUESTION", "REFINE_QUESTION_SAME_SNAPSHOT", "INSPECT_HANDLE", "NARROW_SELECTOR",
  "WAIT_AND_RETRY", "RECAPTURE_SOURCE", "CONFIGURE_READER_MODEL", "REVIEW_DISCLOSURE_BUDGET",
  "NONE",
]);

/**
 * Failures that leave the caller's handles intact, so recovery is a retry or a refined
 * question over the same snapshot - never a recapture of the source.
 */
const HANDLES_SURVIVE = new Set([
  "MODEL_ERROR", "INVALID_MODEL_OUTPUT", "CITATION_INVALID", "TIMEOUT", "CANCELLED",
  "LIMIT_EXCEEDED", "DISCLOSURE_EXHAUSTED", "PROVENANCE_UNAVAILABLE", "INVALID_REQUEST",
]);

const RECOVERY_BY_CODE: Record<string, string[]> = {
  MODEL_ERROR: ["RETRY_SAME_QUESTION", "REFINE_QUESTION_SAME_SNAPSHOT", "INSPECT_HANDLE"],
  INVALID_MODEL_OUTPUT: ["RETRY_SAME_QUESTION", "INSPECT_HANDLE"],
  CITATION_INVALID: ["REFINE_QUESTION_SAME_SNAPSHOT", "INSPECT_HANDLE"],
  TIMEOUT: ["WAIT_AND_RETRY", "NARROW_SELECTOR", "INSPECT_HANDLE"],
  CANCELLED: ["RETRY_SAME_QUESTION"],
  LIMIT_EXCEEDED: ["NARROW_SELECTOR", "INSPECT_HANDLE"],
  DISCLOSURE_EXHAUSTED: ["REVIEW_DISCLOSURE_BUDGET", "REFINE_QUESTION_SAME_SNAPSHOT"],
  PROVENANCE_UNAVAILABLE: ["CONFIGURE_READER_MODEL", "INSPECT_HANDLE"],
  SOURCE_EXPIRED: ["RECAPTURE_SOURCE"],
  SOURCE_CHANGED: ["RECAPTURE_SOURCE"],
  STORE_FAILED: ["RECAPTURE_SOURCE"],
  SPILL_FAILED: ["RECAPTURE_SOURCE"],
};

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

export interface ExtractionShape {
  mode: "lines" | "bytes" | "search";
  source_id: string;
  snapshot_id: string;
  deterministic: true;
  segments: Array<{ kind: string; start: number; end: number; text: string }>;
  result_bytes: number;
  complete: boolean;
  next_cursor: string | null;
  lines_scanned: number;
  scan_budget_exhausted: boolean;
  matches_found?: number;
  disclosed_bytes_source: number;
  disclosed_bytes_session: number;
  disclosure_limit_reached: boolean;
}

export interface StatsShape {
  scope: "session";
  totals: StatsTotalsShape;
  records: OperationRecordShape[];
  page: number;
  page_size: number;
  total_records: number;
  next_page: number | null;
}

export interface RecoveryShape {
  handles_valid: boolean;
  actions: string[];
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
  result_kind?: ResultKind;
  provenance?: ProvenanceShape;
  accounting_id?: string;
  extraction?: ExtractionShape;
  stats?: StatsShape;
  recovery?: RecoveryShape;
}

export class Coverage {
  complete = false;
  processedChunks = 0;
  plannedChunks = 0;
  upstreamTruncated: boolean | null = null;
  readonly omitted: Omission[] = [];

  omit(sourceId: string, selector: Record<string, unknown>, reason: string): void {
    if (!OMISSION_REASONS.has(reason)) throw new Error(`unknown omission reason: ${reason}`);
    if (this.omitted.length >= MAX_OMISSIONS) {
      // The contract caps the list at 32 entries, and an over-long list is rejected by the
      // output guard - which would convert "we told you what we left out" into a bare
      // LIMIT_EXCEEDED. The list stops growing and the omissions already in it still make
      // `complete` false, so the envelope stays truthful about *that* material having been
      // dropped; only the enumeration is bounded.
      return;
    }
    this.omitted.push({ source_id: sourceId, selector, reason });
  }

  /**
   * {@link omit}, collapsed onto an identical entry that is already recorded.
   *
   * A cap can drop many things that belong to the same source, selector and reason -
   * twelve claims over the per-answer ceiling are one fact about one chunk, not twelve -
   * and the coverage list holds 32 entries in total. Deduplicating keeps the list inside
   * its contract bound while still saying, once, that this source lost material for this
   * reason.
   */
  omitOnce(sourceId: string, selector: Record<string, unknown>, reason: string): void {
    const already = this.omitted.some(
      (entry) =>
        entry.source_id === sourceId
        && entry.reason === reason
        && JSON.stringify(entry.selector) === JSON.stringify(selector),
    );
    if (already) return;
    this.omit(sourceId, selector, reason);
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

/**
 * Used only when a caller builds an envelope without an operation record, which happens
 * for the fixed fallback envelope the guard emits when nothing else can be trusted.
 */
export const PLACEHOLDER_ACCOUNTING_ID = "acc_" + "0".repeat(16);

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
  resultKind?: ResultKind;
  provenance?: Provenance;
  accountingId?: string;
  extraction?: ExtractionShape;
  stats?: StatsShape;
  recovery?: RecoveryShape;
  schemaVersion?: string;
}

function defaultResultKind(code: string): ResultKind {
  if (code === "EXTRACTED") return "deterministic_extraction";
  if (code === "STATS") return "stats";
  if (code === "SPILLED" || code === "IMPORTED") return "pointer";
  if (code === "LARGE_READ" || code === "UNCLASSIFIABLE_READ") return "gate_decision";
  if (code === "ANSWERED" || code === "NO_MATCH") return "model_derived";
  return "failure";
}

const DEFAULT_LABELS: Partial<Record<ResultKind, ProvenanceLabel>> = {
  deterministic_extraction: "deterministic_extraction",
  stats: "session_metrics",
  pointer: "pointer_only",
  gate_decision: "gate_decision",
  failure: "no_model_output",
};

function defaultProvenance(kind: ResultKind): Provenance {
  const label = DEFAULT_LABELS[kind];
  if (label === undefined) {
    // A model-derived envelope must be given real provenance by the reader; there is no
    // honest default, so refuse rather than invent one.
    throw new Error("a model-derived envelope requires explicit provenance");
  }
  return deterministicProvenance(label);
}

function validatedRecovery(recovery: RecoveryShape): RecoveryShape {
  for (const action of recovery.actions) {
    if (!RECOVERY_ACTIONS.has(action)) throw new Error("unknown recovery action");
  }
  return { handles_valid: Boolean(recovery.handles_valid), actions: recovery.actions.slice(0, 6) };
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
  if (opts.code === "EXTRACTED") {
    if (answer.length > 0 || citations.length > 0) {
      throw new Error("EXTRACTED must carry no answer and no citations");
    }
    if (!opts.extraction) throw new Error("EXTRACTED requires an extraction block");
  }
  if (opts.code === "STATS") {
    if (answer.length > 0 || citations.length > 0) {
      throw new Error("STATS must carry no answer and no citations");
    }
    if (!opts.stats) throw new Error("STATS requires a stats block");
  }

  const schemaVersion = opts.schemaVersion ?? EMITTED_SCHEMA_VERSION;
  const envelope: Envelope = {
    schema_version: schemaVersion,
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

  if (schemaVersion !== "1.1") {
    // A 1.0 envelope carries no 1.1 block, ever. Attaching one while still declaring 1.0
    // would make the version string a lie rather than a compatible extension.
    return envelope;
  }

  const kind = opts.resultKind ?? defaultResultKind(opts.code);
  const provenance = opts.provenance ?? defaultProvenance(kind);
  if (provenance.derived !== (kind === "model_derived")) {
    throw new Error("provenance.derived must agree with result_kind");
  }
  envelope.result_kind = kind;
  envelope.provenance = provenanceToShape(provenance);
  envelope.accounting_id = opts.accountingId ?? PLACEHOLDER_ACCOUNTING_ID;
  if (opts.extraction) envelope.extraction = opts.extraction;
  if (opts.stats) envelope.stats = opts.stats;
  if (opts.recovery) envelope.recovery = validatedRecovery(opts.recovery);
  return envelope;
}

/**
 * Deterministic next steps for a failure code.
 *
 * `handlesValid` is stated explicitly because it is the fact a caller most needs: a reader,
 * provider, citation or provenance failure leaves every handle intact, so the right move is
 * a retry or a refined question over the *same* snapshot. Only a failure of the handle
 * itself calls for a recapture.
 */
export function recoveryFor(code: string, handlesValid?: boolean): RecoveryShape {
  return {
    handles_valid: handlesValid ?? HANDLES_SURVIVE.has(code),
    actions: RECOVERY_BY_CODE[code] ?? ["NONE"],
  };
}

const BLOCKED_CODES = new Set([
  "LARGE_READ", "UNCLASSIFIABLE_READ", "UNSAFE_SOURCE", "BINARY_UNSUPPORTED", "HOST_UNSAFE",
]);

export interface ErrorEnvelopeOptions {
  guidance?: string;
  accountingId?: string;
  provenance?: Provenance;
  sources?: SourceHandle[];
  handlesValid?: boolean;
  schemaVersion?: string;
}

/** Map a bounded failure to an envelope. The exception message never rides along. */
export function errorEnvelope(
  requestId: string,
  err: ShuntError,
  options: ErrorEnvelopeOptions | string = {},
): Envelope {
  // The 1.0 signature took a bare guidance string; keep it working.
  const opts: ErrorEnvelopeOptions = typeof options === "string" ? { guidance: options } : options;
  const build: BuildOptions = {
    requestId,
    status: BLOCKED_CODES.has(err.code) ? "blocked" : "error",
    code: err.code,
    retryable: err.retryable,
    recovery: recoveryFor(err.code, opts.handlesValid),
  };
  if (opts.guidance) build.guidance = opts.guidance;
  if (opts.sources) build.sources = opts.sources;
  if (opts.accountingId) build.accountingId = opts.accountingId;
  if (opts.provenance) build.provenance = opts.provenance;
  if (opts.schemaVersion) build.schemaVersion = opts.schemaVersion;
  return buildEnvelope(build);
}

export function serialized(envelope: unknown): string {
  return JSON.stringify(envelope);
}

export function serializedBytes(envelope: unknown): number {
  return new TextEncoder().encode(JSON.stringify(envelope)).length;
}
