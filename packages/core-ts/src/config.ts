/**
 * Configuration.
 *
 * Two rules matter more than the rest:
 *
 * - `writer.enabled = true` is refused at load time. v1 has no writer, so accepting the
 *   flag and quietly ignoring it would turn a missing feature into a hidden one.
 * - `suma_post_tool.enabled` defaults to `false` and, even when set, only takes effect if
 *   the adapter's capability probe proves a safe capture/replacement order.
 */
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { resolve, sep } from "node:path";

import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits, narrowLimits } from "./limits.js";

const NARROWABLE = new Set<keyof Limits>([
  "fullReadMaxLines", "targetedReadMaxLines", "targetedSearchMaxMatches", "maxToolResultBytes",
  "probeMaxLinesScanned", "maxEnvelopeBytes", "maxTargetedReadBytes", "maxSourceBytes",
  "maxChunkBytes", "maxAnswerBytes", "maxQuoteBytes", "maxQuestionBytes",
  "sessionSpillQuotaBytes", "maxSourcesPerRequest", "maxChunksPerRequest", "maxCitations",
  "maxConcurrentModelCalls", "maxChunkOverlapLines", "maxTransientRetries", "maxChunkTokens",
  "maxRequestInputTokens", "maxOutputTokensPerCall", "bytesPerTokenEstimate",
  "gateProbeDeadlineMs", "spillIoDeadlineMs", "requestDeadlineMs", "modelCallDeadlineMs",
  "spillTtlSeconds", "jsonMaxDepth", "jsonMaxNodes",
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
};
const POSITIVE_LIMITS = new Set<keyof Limits>([
  "bytesPerTokenEstimate", "maxChunkBytes", "maxChunkTokens", "maxConcurrentModelCalls",
]);

export interface Config {
  readonly workspaceRoots: readonly string[];
  readonly spillDir: string;
  readonly denylist: readonly string[];
  readonly gateEnabled: boolean;
  readonly readerEnabled: boolean;
  readonly readerModel: string;
  readonly sumaPostToolEnabled: boolean;
  readonly limits: Limits;
}

export interface RawConfig {
  workspace_roots?: string[];
  spill_dir?: string;
  denylist?: string[];
  gate_enabled?: boolean;
  reader?: { enabled?: boolean; model?: string };
  suma_post_tool?: { enabled?: boolean };
  writer?: { enabled?: boolean };
  operations?: string[];
  limits?: Record<string, unknown>;
}

export function loadConfig(raw: RawConfig | undefined, defaultSpillDir: string): Config {
  const cfg = raw ?? {};

  for (const nested of [cfg.reader, cfg.suma_post_tool, cfg.writer]) {
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
  for (const value of [
    cfg.gate_enabled,
    cfg.reader?.enabled,
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

  const model = cfg.reader?.model ?? DEFAULT_LIMITS.readerModel;
  if (model !== DEFAULT_LIMITS.readerModel) {
    // v1 is a single-model contract; a different model is a configuration error, not a
    // silent substitution.
    throw new ShuntError("MODEL_ERROR", "MODEL_NOT_ALLOWED", false);
  }

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

  const spillDir = canonicalConfigPath(cfg.spill_dir ?? defaultSpillDir);
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
    sumaPostToolEnabled: cfg.suma_post_tool?.enabled ?? false,
    limits,
  };
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
