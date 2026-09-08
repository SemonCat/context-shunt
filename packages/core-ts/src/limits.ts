/**
 * Normative caps, loaded from the shared contract rather than hardcoded.
 *
 * Both language cores read `contracts/v1/limits.json`. A deployment may lower a cap
 * (`narrowLimits`); raising one is rejected here so a config file can never widen the
 * security envelope that the acceptance gates measure.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

/** Works from `src/` (checkout) and from `dist/` (installed package). */
export function contractsDir(): string {
  return join(here, "..", "contracts", "v1");
}

/** The normative store DDL, read from the vendored contract rather than embedded. */
export function storeDdlPath(): string {
  return join(here, "..", "contracts", "store", "v1.sql");
}

export function storeDdl(): string {
  return readFileSync(storeDdlPath(), "utf8");
}

function loadJson<T>(name: string): T {
  return JSON.parse(readFileSync(join(contractsDir(), name), "utf8")) as T;
}

export interface RawLimits {
  reader_model: string;
  contract: { emitted_version: string; supported_request_versions: string[] };
  gate: Record<string, number>;
  bytes: Record<string, number>;
  counts: Record<string, number>;
  tokens: Record<string, number>;
  deadlines_ms: Record<string, number>;
  spill: { ttl_seconds: number; dir_mode: string; file_mode: string };
  json: Record<string, number>;
  inspect: Record<string, number>;
  disclosure: Record<string, number>;
  store: Record<string, number | string>;
  accounting: Record<string, number | string>;
}

export interface StatusCodePairs {
  statuses: string[];
  codes: string[];
  pairs: Record<string, string[]>;
}

let rawCache: RawLimits | undefined;
let pairsCache: StatusCodePairs | undefined;

export function rawLimits(): RawLimits {
  rawCache ??= loadJson<RawLimits>("limits.json");
  return rawCache;
}

export function statusCodePairs(): StatusCodePairs {
  pairsCache ??= loadJson<StatusCodePairs>("status-code-pairs.json");
  return pairsCache;
}

export interface Limits {
  readonly readerModel: string;
  readonly fullReadMaxLines: number;
  readonly targetedReadMaxLines: number;
  readonly targetedSearchMaxMatches: number;
  readonly probeMaxLinesScanned: number;
  readonly maxToolResultBytes: number;
  readonly maxEnvelopeBytes: number;
  readonly maxTargetedReadBytes: number;
  readonly maxSourceBytes: number;
  readonly maxChunkBytes: number;
  readonly maxAnswerBytes: number;
  readonly maxQuoteBytes: number;
  readonly maxQuestionBytes: number;
  readonly sessionSpillQuotaBytes: number;
  readonly maxClaimTextBytes: number;
  readonly maxSourcesPerRequest: number;
  readonly maxChunksPerRequest: number;
  readonly maxCitations: number;
  readonly maxConcurrentModelCalls: number;
  readonly maxChunkOverlapLines: number;
  readonly maxTransientRetries: number;
  readonly maxClaimsPerAnswer: number;
  readonly maxCitationIdsPerClaim: number;
  readonly maxFormatRetries: number;
  readonly maxChunkTokens: number;
  readonly maxRequestInputTokens: number;
  readonly maxOutputTokensPerCall: number;
  readonly bytesPerTokenEstimate: number;
  readonly gateProbeDeadlineMs: number;
  readonly spillIoDeadlineMs: number;
  readonly modelCallDeadlineMs: number;
  readonly requestDeadlineMs: number;
  readonly spillTtlSeconds: number;
  readonly jsonMaxDepth: number;
  readonly jsonMaxNodes: number;
  // -- 1.1 additions --
  readonly maxExtendedEnvelopeBytes: number;
  readonly maxExtractionBytes: number;
  readonly inspectMaxResultBytes: number;
  readonly inspectMaxSegments: number;
  readonly inspectMaxLinesPerPage: number;
  readonly inspectMaxBytesPerPage: number;
  readonly inspectMaxScanLines: number;
  readonly inspectMaxScanBytes: number;
  readonly inspectMaxSearchMatches: number;
  readonly inspectMaxNeedleBytes: number;
  readonly disclosureMaxPerSourceBytes: number;
  readonly disclosureMaxPerSessionBytes: number;
  readonly storeDdlVersion: number;
  readonly storeBusyTimeoutMs: number;
  readonly storeMaxEntries: number;
  readonly storeMaxBytes: number;
  readonly storeHandleTtlSeconds: number;
  readonly statsMaxRecordsPerPage: number;
  readonly statsMaxPages: number;
}

function pick(group: Record<string, number>, key: string): number {
  const value = group[key];
  if (typeof value !== "number") {
    throw new Error(`limits.json is missing ${key}`);
  }
  return value;
}

export function defaultLimits(): Limits {
  const raw = rawLimits();
  return Object.freeze({
    readerModel: raw.reader_model,
    fullReadMaxLines: pick(raw.gate, "full_read_max_lines"),
    targetedReadMaxLines: pick(raw.gate, "targeted_read_max_lines"),
    targetedSearchMaxMatches: pick(raw.gate, "targeted_search_max_matches"),
    probeMaxLinesScanned: pick(raw.gate, "probe_max_lines_scanned"),
    maxToolResultBytes: pick(raw.bytes, "max_tool_result_bytes"),
    maxEnvelopeBytes: pick(raw.bytes, "max_envelope_bytes"),
    maxTargetedReadBytes: pick(raw.bytes, "max_targeted_read_bytes"),
    maxSourceBytes: pick(raw.bytes, "max_source_bytes"),
    maxChunkBytes: pick(raw.bytes, "max_chunk_bytes"),
    maxAnswerBytes: pick(raw.bytes, "max_answer_bytes"),
    maxQuoteBytes: pick(raw.bytes, "max_quote_bytes"),
    maxQuestionBytes: pick(raw.bytes, "max_question_bytes"),
    sessionSpillQuotaBytes: pick(raw.bytes, "session_spill_quota_bytes"),
    maxClaimTextBytes: pick(raw.bytes, "max_claim_text_bytes"),
    maxSourcesPerRequest: pick(raw.counts, "max_sources_per_request"),
    maxChunksPerRequest: pick(raw.counts, "max_chunks_per_request"),
    maxCitations: pick(raw.counts, "max_citations"),
    maxConcurrentModelCalls: pick(raw.counts, "max_concurrent_model_calls"),
    maxChunkOverlapLines: pick(raw.counts, "max_chunk_overlap_lines"),
    maxTransientRetries: pick(raw.counts, "max_transient_retries"),
    maxClaimsPerAnswer: pick(raw.counts, "max_claims_per_answer"),
    maxCitationIdsPerClaim: pick(raw.counts, "max_citation_ids_per_claim"),
    maxFormatRetries: pick(raw.counts, "max_format_retries"),
    maxChunkTokens: pick(raw.tokens, "max_chunk_tokens"),
    maxRequestInputTokens: pick(raw.tokens, "max_request_input_tokens"),
    maxOutputTokensPerCall: pick(raw.tokens, "max_output_tokens_per_call"),
    bytesPerTokenEstimate: pick(raw.tokens, "bytes_per_token_estimate"),
    gateProbeDeadlineMs: pick(raw.deadlines_ms, "gate_probe"),
    spillIoDeadlineMs: pick(raw.deadlines_ms, "spill_io"),
    modelCallDeadlineMs: pick(raw.deadlines_ms, "model_call"),
    requestDeadlineMs: pick(raw.deadlines_ms, "request"),
    spillTtlSeconds: raw.spill.ttl_seconds,
    jsonMaxDepth: pick(raw.json, "max_depth"),
    jsonMaxNodes: pick(raw.json, "max_nodes"),
    maxExtendedEnvelopeBytes: pick(raw.bytes, "max_extended_envelope_bytes"),
    maxExtractionBytes: pick(raw.bytes, "max_extraction_bytes"),
    inspectMaxResultBytes: pick(raw.inspect, "max_result_bytes"),
    inspectMaxSegments: pick(raw.inspect, "max_segments"),
    inspectMaxLinesPerPage: pick(raw.inspect, "max_lines_per_page"),
    inspectMaxBytesPerPage: pick(raw.inspect, "max_bytes_per_page"),
    inspectMaxScanLines: pick(raw.inspect, "max_scan_lines"),
    inspectMaxScanBytes: pick(raw.inspect, "max_scan_bytes"),
    inspectMaxSearchMatches: pick(raw.inspect, "max_search_matches"),
    inspectMaxNeedleBytes: pick(raw.inspect, "max_needle_bytes"),
    disclosureMaxPerSourceBytes: pick(raw.disclosure, "max_per_source_bytes"),
    disclosureMaxPerSessionBytes: pick(raw.disclosure, "max_per_session_bytes"),
    storeDdlVersion: pick(raw.store as Record<string, number>, "ddl_version"),
    storeBusyTimeoutMs: pick(raw.store as Record<string, number>, "busy_timeout_ms"),
    storeMaxEntries: pick(raw.store as Record<string, number>, "max_entries"),
    storeMaxBytes: pick(raw.store as Record<string, number>, "max_bytes"),
    storeHandleTtlSeconds: pick(raw.store as Record<string, number>, "handle_ttl_seconds"),
    statsMaxRecordsPerPage: pick(
      raw.accounting as Record<string, number>,
      "max_stats_records_per_page",
    ),
    statsMaxPages: pick(raw.accounting as Record<string, number>, "max_stats_pages"),
  });
}

export const DEFAULT_LIMITS: Limits = defaultLimits();
export const READER_MODEL = DEFAULT_LIMITS.readerModel;

/**
 * Contract revision. Two constants, deliberately separate: `EMITTED_SCHEMA_VERSION` is
 * what every envelope this core builds declares, `SUPPORTED_REQUEST_VERSIONS` is what it
 * will accept on input. Revision 1.1 is backward compatible - a 1.0 request is still
 * accepted and a 1.0 envelope still validates - but a request that declares 1.0 while
 * carrying a 1.1 field is refused rather than accepted with the field ignored.
 */
export const EMITTED_SCHEMA_VERSION: string = rawLimits().contract.emitted_version;
export const SUPPORTED_REQUEST_VERSIONS: ReadonlySet<string> = new Set(
  rawLimits().contract.supported_request_versions,
);
/** Backward-compatible alias. New code should say which of the two it means. */
export const SCHEMA_VERSION: string = EMITTED_SCHEMA_VERSION;

/** Fields and operations that only exist from 1.1 onward. */
export const V11_ONLY_REQUEST_FIELDS: ReadonlySet<string> = new Set(["refined"]);
export const V11_ONLY_OPERATIONS: ReadonlySet<string> = new Set(["inspect", "stats"]);
export const V11_ONLY_ENVELOPE_FIELDS: ReadonlySet<string> = new Set([
  "result_kind", "provenance", "accounting_id", "extraction", "stats", "recovery",
  "import_receipt",
]);

/** Deterministic estimator name recorded whenever provider usage is unavailable. */
export const BASELINE_ESTIMATE_METHOD = String(rawLimits().accounting["baseline_method"]);

export function supportedRequestVersion(version: unknown): boolean {
  return typeof version === "string" && SUPPORTED_REQUEST_VERSIONS.has(version);
}

/**
 * The serialized cap that applies to one envelope.
 *
 * Deterministic extraction and stats carry a bounded payload of their own - up to
 * `maxExtractionBytes` of exact snapshot bytes, or one page of operation records - so they
 * are measured against `maxExtendedEnvelopeBytes`. Every other envelope keeps the original
 * 16 KiB cap. Both values live in `contracts/v1/limits.json`.
 */
export function envelopeByteCap(
  resultKind: string | undefined,
  limits: Limits = DEFAULT_LIMITS,
): number {
  return resultKind === "deterministic_extraction" || resultKind === "stats"
    ? limits.maxExtendedEnvelopeBytes
    : limits.maxEnvelopeBytes;
}

/** Caps a deployment may not set to zero, because zero would disable rather than tighten. */
export const POSITIVE_LIMITS: ReadonlySet<string> = new Set([
  "bytesPerTokenEstimate", "maxChunkBytes", "maxChunkTokens", "maxConcurrentModelCalls",
  "inspectMaxResultBytes", "inspectMaxScanLines", "storeDdlVersion", "storeBusyTimeoutMs",
  "statsMaxRecordsPerPage", "statsMaxPages",
]);

/** Chunk byte budget: the smaller of the byte cap and the token cap in bytes. */
export function chunkByteBudget(limits: Limits = DEFAULT_LIMITS): number {
  return Math.min(limits.maxChunkBytes, limits.maxChunkTokens * limits.bytesPerTokenEstimate);
}

export function narrowLimits(limits: Limits, overrides: Partial<Record<keyof Limits, number>>): Limits {
  const next: Record<string, unknown> = { ...limits };
  for (const [key, value] of Object.entries(overrides)) {
    if (!(key in limits)) throw new Error(`unknown limit: ${key}`);
    const current = (limits as unknown as Record<string, unknown>)[key];
    if (typeof current !== "number" || typeof value !== "number") {
      throw new Error(`limit is not numeric: ${key}`);
    }
    if (!Number.isSafeInteger(value)) throw new Error(`limit is not a safe integer: ${key}`);
    if (value > current) throw new Error(`limit ${key} may only be narrowed (max ${current})`);
    if (value < 0) throw new Error(`limit ${key} may not be negative`);
    if (value === 0 && POSITIVE_LIMITS.has(key)) {
      throw new Error(`limit ${key} must be positive`);
    }
    next[key] = value;
  }
  return Object.freeze(next) as unknown as Limits;
}

export function legalPair(status: string, code: string): boolean {
  const pairs = statusCodePairs().pairs;
  const allowed = pairs[status];
  return Array.isArray(allowed) && allowed.includes(code);
}
