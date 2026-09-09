/**
 * Configuration.
 *
 * Rules that matter more than the rest:
 *
 * - `writer.enabled = true` is refused at load time. There is no writer, so accepting the
 *   flag and quietly ignoring it would turn a missing feature into a hidden one.
 * - `suma_post_tool.enabled` defaults to `false` and, even when set, only takes effect if
 *   the adapter's capability probe proves a safe capture/replacement order.
 * - A cap may be narrowed, never widened.
 *
 * Changed in 1.1: the reader model and provider are configurable. Revision 1.0 refused any
 * `reader.model` other than `gpt-5.6-luna`; that was a single-model contract and it is
 * deliberately relaxed here. `gpt-5.6-luna` remains the **default**, and what replaces the
 * old hard refusal is truthful provenance: every envelope states the requested model,
 * whatever the host reported, and how strongly the attribution can be believed, so a
 * different model can never be passed off as the default one.
 *
 * `reader.attribution_policy` decides what happens when attribution cannot be proven.
 * `allow_unverified` (the default) publishes the answer with `attribution_status =
 * unverified`; `require_match` refuses instead. A contradiction is refused either way.
 *
 * `reader.fallback_chain` is availability-only. Every entry keeps its own reported
 * provenance and usage, and reaching one is recorded as `fallback_used`.
 */
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { resolve, sep } from "node:path";

import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits, POSITIVE_LIMITS, narrowLimits } from "./limits.js";
import type { AttributionPolicy } from "./provenance.js";

const NARROWABLE = new Set<keyof Limits>([
  "fullReadMaxLines", "targetedReadMaxLines", "targetedSearchMaxMatches", "maxToolResultBytes",
  "probeMaxLinesScanned", "maxEnvelopeBytes", "maxTargetedReadBytes", "maxSourceBytes",
  "maxChunkBytes", "maxAnswerBytes", "maxQuoteBytes", "maxQuestionBytes",
  "sessionSpillQuotaBytes", "maxSourcesPerRequest", "maxChunksPerRequest", "maxCitations",
  "maxConcurrentModelCalls", "maxChunkOverlapLines", "maxTransientRetries", "maxChunkTokens",
  "maxRequestInputTokens", "maxOutputTokensPerCall", "bytesPerTokenEstimate",
  "gateProbeDeadlineMs", "spillIoDeadlineMs", "requestDeadlineMs", "modelCallDeadlineMs",
  "spillTtlSeconds", "jsonMaxDepth", "jsonMaxNodes",
  // -- 1.1 --
  "maxExtendedEnvelopeBytes", "maxExtractionBytes", "inspectMaxResultBytes",
  "inspectMaxSegments", "inspectMaxLinesPerPage", "inspectMaxBytesPerPage",
  "inspectMaxScanLines", "inspectMaxScanBytes", "inspectMaxSearchMatches",
  "inspectMaxNeedleBytes", "disclosureMaxPerSourceBytes", "disclosureMaxPerSessionBytes",
  "storeDdlVersion", "storeBusyTimeoutMs", "storeMaxEntries", "storeMaxBytes",
  "storeHandleTtlSeconds", "statsMaxRecordsPerPage", "statsMaxPages",
]);

const SNAKE_LIMITS: Record<string, keyof Limits> = {
  full_read_max_lines: "fullReadMaxLines",
  targeted_read_max_lines: "targetedReadMaxLines",
  targeted_search_max_matches: "targetedSearchMaxMatches",
  probe_max_lines_scanned: "probeMaxLinesScanned",
  max_tool_result_bytes: "maxToolResultBytes",
  max_envelope_bytes: "maxEnvelopeBytes",
  max_targeted_read_bytes: "maxTargetedReadBytes",
  max_source_bytes: "maxSourceBytes",
  max_chunk_bytes: "maxChunkBytes",
  max_answer_bytes: "maxAnswerBytes",
  max_quote_bytes: "maxQuoteBytes",
  max_question_bytes: "maxQuestionBytes",
  session_spill_quota_bytes: "sessionSpillQuotaBytes",
  max_sources_per_request: "maxSourcesPerRequest",
  max_chunks_per_request: "maxChunksPerRequest",
  max_citations: "maxCitations",
  max_concurrent_model_calls: "maxConcurrentModelCalls",
  max_chunk_overlap_lines: "maxChunkOverlapLines",
  max_transient_retries: "maxTransientRetries",
  max_chunk_tokens: "maxChunkTokens",
  max_request_input_tokens: "maxRequestInputTokens",
  max_output_tokens_per_call: "maxOutputTokensPerCall",
  bytes_per_token_estimate: "bytesPerTokenEstimate",
  gate_probe_deadline_ms: "gateProbeDeadlineMs",
  spill_io_deadline_ms: "spillIoDeadlineMs",
  model_call_deadline_ms: "modelCallDeadlineMs",
  request_deadline_ms: "requestDeadlineMs",
  spill_ttl_seconds: "spillTtlSeconds",
  json_max_depth: "jsonMaxDepth",
  json_max_nodes: "jsonMaxNodes",
  max_extended_envelope_bytes: "maxExtendedEnvelopeBytes",
  max_extraction_bytes: "maxExtractionBytes",
  inspect_max_result_bytes: "inspectMaxResultBytes",
  inspect_max_segments: "inspectMaxSegments",
  inspect_max_lines_per_page: "inspectMaxLinesPerPage",
  inspect_max_bytes_per_page: "inspectMaxBytesPerPage",
  inspect_max_scan_lines: "inspectMaxScanLines",
  inspect_max_scan_bytes: "inspectMaxScanBytes",
  inspect_max_search_matches: "inspectMaxSearchMatches",
  inspect_max_needle_bytes: "inspectMaxNeedleBytes",
  disclosure_max_per_source_bytes: "disclosureMaxPerSourceBytes",
  disclosure_max_per_session_bytes: "disclosureMaxPerSessionBytes",
  store_ddl_version: "storeDdlVersion",
  store_busy_timeout_ms: "storeBusyTimeoutMs",
  store_max_entries: "storeMaxEntries",
  store_max_bytes: "storeMaxBytes",
  store_handle_ttl_seconds: "storeHandleTtlSeconds",
  stats_max_records_per_page: "statsMaxRecordsPerPage",
  stats_max_pages: "statsMaxPages",
};
const MAX_MODEL_REF_BYTES = 128;
const MAX_FALLBACK_ENTRIES = 4;
const READER_KEYS = new Set([
  "enabled", "model", "provider", "attribution_policy", "fallback_chain", "automatic_extract", "fallback_max_bytes",
]);
const ENABLED_SECTION_KEYS = new Set(["enabled"]);
const CONFIG_KEYS = new Set([
  "workspace_roots", "spill_dir", "cache_dir", "denylist", "gate_enabled", "reader",
  "inspect", "stats", "suma_post_tool", "writer", "operations", "limits",
]);

/** One availability target: a model, optionally pinned to a provider. */
export interface ProviderRef {
  readonly model: string;
  readonly provider: string;
}

export interface Config {
  readonly workspaceRoots: readonly string[];
  /** Private cache root: SQLite metadata plus content-addressed payload files. */
  readonly spillDir: string;
  readonly denylist: readonly string[];
  readonly gateEnabled: boolean;
  readonly readerEnabled: boolean;
  readonly readerModel: string;
  readonly readerProvider: string;
  readonly readerAttributionPolicy: AttributionPolicy;
  readonly readerAutomaticExtract?: boolean;
  readonly readerFallbackMaxBytes?: number;
  readonly readerFallbackChain: readonly ProviderRef[];
  readonly inspectEnabled: boolean;
  readonly statsEnabled: boolean;
  readonly sumaPostToolEnabled: boolean;
  readonly limits: Limits;
}

export interface RawConfig {
  workspace_roots?: string[];
  spill_dir?: string;
  cache_dir?: string;
  denylist?: string[];
  gate_enabled?: boolean;
  reader?: {
    enabled?: boolean;
    model?: string;
    provider?: string;
    attribution_policy?: string;
    automatic_extract?: boolean;
    fallback_max_bytes?: number;
    fallback_chain?: Array<{ model?: string; provider?: string }>;
  };
  inspect?: { enabled?: boolean };
  stats?: { enabled?: boolean };
  suma_post_tool?: { enabled?: boolean };
  writer?: { enabled?: boolean };
  operations?: string[];
  limits?: Record<string, unknown>;
}

export function loadConfig(raw: RawConfig | undefined, defaultSpillDir: string): Config {
  if (raw !== undefined && (
    typeof raw !== "object" || raw === null || Array.isArray(raw)
  )) throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  const cfg = raw ?? {};

  for (const key of Object.keys(cfg)) {
    if (!CONFIG_KEYS.has(key)) throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }

  for (const nested of [cfg.reader, cfg.inspect, cfg.stats, cfg.suma_post_tool, cfg.writer]) {
    if (nested !== undefined && (
      typeof nested !== "object" || nested === null || Array.isArray(nested)
    )) throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }
  if (cfg.operations !== undefined && (
    !Array.isArray(cfg.operations) || cfg.operations.some((item) => typeof item !== "string")
  )) throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);

  const rootsRaw = cfg.workspace_roots;
  if (
    !Array.isArray(rootsRaw)
    || rootsRaw.length === 0
    || rootsRaw.some((root) => typeof root !== "string" || root.length === 0)
  ) {
    throw new ShuntError("UNSAFE_SOURCE", "NO_WORKSPACE_ROOT", false);
  }
  if (cfg.denylist !== undefined && (
    !Array.isArray(cfg.denylist) || cfg.denylist.some((item) => typeof item !== "string")
  )) throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  for (const key of Object.keys(cfg.reader ?? {})) {
    if (!READER_KEYS.has(key)) throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }
  for (const section of [cfg.inspect, cfg.stats, cfg.suma_post_tool, cfg.writer]) {
    for (const key of Object.keys(section ?? {})) {
      if (!ENABLED_SECTION_KEYS.has(key)) {
        throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
      }
    }
  }
  for (const value of [
    cfg.gate_enabled,
    cfg.reader?.enabled,
    cfg.reader?.automatic_extract,
    cfg.inspect?.enabled,
    cfg.stats?.enabled,
    cfg.suma_post_tool?.enabled,
    cfg.writer?.enabled,
  ]) {
    if (value !== undefined && typeof value !== "boolean") {
      throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
    }
  }

  if (cfg.writer?.enabled) {
    // Not "not implemented yet" - refused, so it cannot become a hidden capability.
    throw new ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", false);
  }
  if (cfg.operations?.includes("propose_patch")) {
    throw new ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", false);
  }

  const roots = rootsRaw.map(canonicalConfigPath);

  const model = readModelRef(cfg.reader?.model ?? DEFAULT_LIMITS.readerModel, true);
  const provider = readModelRef(cfg.reader?.provider ?? "", false);
  const policyRaw = cfg.reader?.attribution_policy ?? "allow_unverified";
  if (policyRaw !== "allow_unverified" && policyRaw !== "require_match") {
    throw new ShuntError("INVALID_REQUEST", "BAD_ATTRIBUTION_POLICY", false);
  }
  const fallbackMaxBytes = cfg.reader?.fallback_max_bytes === undefined
    ? 2048 : cfg.reader.fallback_max_bytes;
  if (!Number.isSafeInteger(fallbackMaxBytes) || fallbackMaxBytes < 1 || fallbackMaxBytes > 4096) {
    throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }
  const chainRaw = cfg.reader?.fallback_chain ?? [];
  if (!Array.isArray(chainRaw) || chainRaw.length > MAX_FALLBACK_ENTRIES) {
    throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }
  const fallbackChain: ProviderRef[] = chainRaw.map((entry) => {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
    }
    for (const key of Object.keys(entry)) {
      if (key !== "model" && key !== "provider") {
        throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
      }
    }
    return {
      model: readModelRef(entry.model ?? "", true),
      provider: readModelRef(entry.provider ?? "", false),
    };
  });

  let limits = DEFAULT_LIMITS;
  const rawOverrides = cfg.limits ?? {};
  if (typeof rawOverrides !== "object" || rawOverrides === null || Array.isArray(rawOverrides)) {
    throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }
  const overrides: Partial<Record<keyof Limits, number>> = {};
  for (const [rawKey, value] of Object.entries(rawOverrides)) {
    const key = SNAKE_LIMITS[rawKey] ?? (NARROWABLE.has(rawKey as keyof Limits)
      ? rawKey as keyof Limits
      : undefined);
    if (!key) {
      throw new ShuntError("INVALID_REQUEST", "UNKNOWN_LIMIT_OVERRIDE", false);
    }
    if (
      !Number.isSafeInteger(value)
      || Number(value) < 0
      || (Number(value) === 0 && POSITIVE_LIMITS.has(key))
      || key in overrides
    ) {
      throw new ShuntError("INVALID_REQUEST", "BAD_LIMIT_OVERRIDE", false);
    }
    overrides[key] = Number(value);
  }
  if (Object.keys(overrides).length > 0) {
    try {
      limits = narrowLimits(limits, overrides);
    } catch {
      throw new ShuntError("INVALID_REQUEST", "LIMIT_MAY_ONLY_NARROW", false);
    }
  }

  const spillDir = canonicalConfigPath(cfg.cache_dir ?? cfg.spill_dir ?? defaultSpillDir);
  if (roots.some((root) => spillDir === root || spillDir.startsWith(root + sep))) {
    throw new ShuntError("UNSAFE_SOURCE", "SPILL_INSIDE_WORKSPACE", false);
  }

  return {
    workspaceRoots: roots,
    spillDir,
    denylist: cfg.denylist ?? [],
    gateEnabled: cfg.gate_enabled ?? true,
    readerEnabled: cfg.reader?.enabled ?? true,
    readerModel: model,
    readerProvider: provider,
    readerAttributionPolicy: policyRaw as AttributionPolicy,
    readerFallbackChain: fallbackChain,
    readerAutomaticExtract: cfg.reader?.automatic_extract ?? true,
    readerFallbackMaxBytes: fallbackMaxBytes,
    inspectEnabled: cfg.inspect?.enabled ?? true,
    statsEnabled: cfg.stats?.enabled ?? true,
    sumaPostToolEnabled: cfg.suma_post_tool?.enabled ?? false,
    limits,
  };
}

function readModelRef(value: unknown, required: boolean): string {
  if (typeof value !== "string" || new TextEncoder().encode(value).length > MAX_MODEL_REF_BYTES) {
    throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }
  const trimmed = value.trim();
  if (required && trimmed.length === 0) {
    throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }
  return trimmed;
}

function canonicalConfigPath(value: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", false);
  }
  const expanded = value === "~"
    ? homedir()
    : value.startsWith(`~${sep}`)
      ? resolve(homedir(), value.slice(2))
      : value;
  const absolute = resolve(expanded);
  try {
    return realpathSync(absolute);
  } catch {
    return absolute;
  }
}
