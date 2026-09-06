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
import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits, narrowLimits } from "./limits.js";

const NARROWABLE = new Set<keyof Limits>([
  "fullReadMaxLines", "targetedReadMaxLines", "targetedSearchMaxMatches", "maxToolResultBytes",
  "maxEnvelopeBytes", "maxSourceBytes", "maxChunkBytes", "maxAnswerBytes", "maxQuoteBytes",
  "sessionSpillQuotaBytes", "maxSourcesPerRequest", "maxChunksPerRequest", "maxCitations",
  "maxConcurrentModelCalls", "maxChunkTokens", "maxRequestInputTokens", "maxOutputTokensPerCall",
  "requestDeadlineMs", "modelCallDeadlineMs", "spillTtlSeconds",
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
  limits?: Record<string, number>;
}

export function loadConfig(raw: RawConfig | undefined, defaultSpillDir: string): Config {
  const cfg = raw ?? {};

  if (cfg.writer?.enabled) {
    // Not "not implemented yet" - refused, so it cannot become a hidden capability.
    throw new ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", false);
  }
  if (cfg.operations?.includes("propose_patch")) {
    throw new ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", false);
  }

  const roots = cfg.workspace_roots ?? [];
  if (roots.length === 0) throw new ShuntError("UNSAFE_SOURCE", "NO_WORKSPACE_ROOT", false);

  const model = cfg.reader?.model ?? DEFAULT_LIMITS.readerModel;
  if (model !== DEFAULT_LIMITS.readerModel) {
    // v1 is a single-model contract; a different model is a configuration error, not a
    // silent substitution.
    throw new ShuntError("MODEL_ERROR", "MODEL_NOT_ALLOWED", false);
  }

  let limits = DEFAULT_LIMITS;
  const overrides = cfg.limits ?? {};
  for (const key of Object.keys(overrides)) {
    if (!NARROWABLE.has(key as keyof Limits)) {
      throw new ShuntError("INVALID_REQUEST", "UNKNOWN_LIMIT_OVERRIDE", false);
    }
  }
  if (Object.keys(overrides).length > 0) {
    try {
      limits = narrowLimits(limits, overrides as Partial<Record<keyof Limits, number>>);
    } catch {
      throw new ShuntError("INVALID_REQUEST", "LIMIT_MAY_ONLY_NARROW", false);
    }
  }

  return {
    workspaceRoots: roots,
    spillDir: cfg.spill_dir ?? defaultSpillDir,
    denylist: cfg.denylist ?? [],
    gateEnabled: cfg.gate_enabled ?? true,
    readerEnabled: cfg.reader?.enabled ?? true,
    readerModel: model,
    sumaPostToolEnabled: cfg.suma_post_tool?.enabled ?? false,
    limits,
  };
}
