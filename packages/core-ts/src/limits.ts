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

export const SCHEMA_VERSION = "1.0" as const;

const here = dirname(fileURLToPath(import.meta.url));

/** Works from `src/` (checkout) and from `dist/` (installed package). */
export function contractsDir(): string {
  return join(here, "..", "contracts", "v1");
}

function loadJson<T>(name: string): T {
  return JSON.parse(readFileSync(join(contractsDir(), name), "utf8")) as T;
}

export interface RawLimits {
  reader_model: string;
  gate: Record<string, number>;
  bytes: Record<string, number>;
  counts: Record<string, number>;
  tokens: Record<string, number>;
  deadlines_ms: Record<string, number>;
  spill: { ttl_seconds: number; dir_mode: string; file_mode: string };
  json: Record<string, number>;
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
  readonly maxSourcesPerRequest: number;
  readonly maxChunksPerRequest: number;
  readonly maxCitations: number;
  readonly maxConcurrentModelCalls: number;
  readonly maxChunkOverlapLines: number;
  readonly maxTransientRetries: number;
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
    maxSourcesPerRequest: pick(raw.counts, "max_sources_per_request"),
    maxChunksPerRequest: pick(raw.counts, "max_chunks_per_request"),
    maxCitations: pick(raw.counts, "max_citations"),
    maxConcurrentModelCalls: pick(raw.counts, "max_concurrent_model_calls"),
    maxChunkOverlapLines: pick(raw.counts, "max_chunk_overlap_lines"),
    maxTransientRetries: pick(raw.counts, "max_transient_retries"),
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
  });
}

export const DEFAULT_LIMITS: Limits = defaultLimits();
export const READER_MODEL = DEFAULT_LIMITS.readerModel;

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
    if (value > current) throw new Error(`limit ${key} may only be narrowed (max ${current})`);
    if (value < 0) throw new Error(`limit ${key} may not be negative`);
    next[key] = value;
  }
  return Object.freeze(next) as unknown as Limits;
}

export function legalPair(status: string, code: string): boolean {
  const pairs = statusCodePairs().pairs;
  const allowed = pairs[status];
  return Array.isArray(allowed) && allowed.includes(code);
}
