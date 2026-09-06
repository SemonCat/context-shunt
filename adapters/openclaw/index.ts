/**
 * OpenClaw plugin entry point for context-shunt.
 *
 * Wiring (verified against the OpenClaw plugin SDK, host 2026.9.2):
 *
 * - `api.on("before_tool_call", handler)` runs before the tool executes and a
 *   `{ block: true }` result short-circuits it. That is what makes the large-read gate a
 *   pre-execution gate: a blocked read never runs. The host fails this hook closed on
 *   timeout, which matches what the gate needs.
 * - `api.runtime.llm.complete` runs a literal-zero-tool isolated completion pinned to
 *   `openai/gpt-5.6-luna`. If the host cannot serve it, the reader reports `MODEL_ERROR`.
 * - `api.registerTool` exposes the read-only reader. No writer tool is registered.
 *
 * The optional Suma post-tool mode is deliberately not wired: see `src/capability.ts` for
 * the host-source evidence, and `docs/capability-matrix.md` for the operator-facing table.
 */
import { homedir } from "node:os";
import { join } from "node:path";

import {
  type CapabilityReport,
  type Config,
  type Envelope,
  type GateDecision,
  HostBridgeProvider,
  type LunaProvider,
  READER_MODEL,
  SCHEMA_VERSION,
  ShuntError,
  ShuntSession,
  UnavailableProvider,
  errorEnvelope,
  enforceOrFixed,
  isShuntError,
  loadConfig,
  modeEnabled,
  reportToJson,
} from "@context-shunt/core";

import { buildCapabilityReport } from "./src/capability.js";
import { normalizeToolCall, requestIdFrom } from "./src/normalize.js";

export const PLUGIN_ID = "context-shunt";

/** The subset of the host plugin API this adapter uses. */
export interface OpenClawPluginApi {
  on(
    hook: string,
    handler: (event: Record<string, unknown>, ctx?: Record<string, unknown>) => unknown,
    opts?: Record<string, unknown>,
  ): void;
  registerTool?(
    tool:
      | Record<string, unknown>
      | ((ctx: Record<string, unknown>) => Record<string, unknown> | null | undefined),
    opts?: { name?: string; names?: string[]; optional?: boolean },
  ): void;
  pluginConfig?: Record<string, unknown>;
  logger?: { info(msg: string, ...rest: unknown[]): void; warn?(msg: string, ...rest: unknown[]): void };
  runtime?: {
    version?: string;
    llm?: {
      complete(opts: {
        messages: [{ role: "user"; content: string }];
        systemPrompt: string;
        model: string;
        maxTokens?: number;
        temperature?: number;
        signal?: AbortSignal | undefined;
        purpose?: string;
        execution: { mode: "isolated-agent-runtime"; timeoutMs: number };
      }): Promise<{
        text?: string;
        provider?: string;
        model?: string;
        usage?: { inputTokens?: number; outputTokens?: number };
      }>;
    };
  };
}

const DEFAULT_HOOKS = [
  "before_tool_call",
  "after_tool_call",
  "tool_result_persist",
  "before_message_write",
  "session_end",
] as const;

/**
 * Tool name and JSON-Schema parameters. `openclaw.plugin.json` declares the same name in
 * `contracts.tools`; OpenClaw rejects a runtime registration that is not declared there.
 */
export const READER_TOOL_NAME = "context_shunt_read";

export const READER_TOOL_DESCRIPTION =
  "Answer a question about one or more large files without pulling them into this " +
  "conversation. Returns a bounded, citation-verified answer. Read-only.";

export const READER_TOOL_PARAMETERS = {
  type: "object",
  additionalProperties: false,
  properties: {
    question: { type: "string", description: "The question to answer. Required." },
    paths: {
      type: "array",
      items: { type: "string" },
      description: "Absolute paths inside a configured workspace root.",
    },
  },
  required: ["question", "paths"],
} as const;

export class ContextShuntPlugin {
  private readonly sessions = new Map<string, ShuntSession>();
  readonly config: Config;
  readonly capability: CapabilityReport;

  constructor(
    private readonly api: OpenClawPluginApi,
    opts: { defaultCacheDir?: string } = {},
  ) {
    const raw = { ...(api.pluginConfig ?? {}) } as Record<string, unknown>;
    const cacheDir =
      opts.defaultCacheDir ?? process.env["CONTEXT_SHUNT_CACHE"] ?? join(homedir(), ".cache", "context-shunt");
    this.config = loadConfig(raw as never, cacheDir);
    this.capability = buildCapabilityReport({
      hooks: DEFAULT_HOOKS,
      hasModelBridge: typeof api.runtime?.llm?.complete === "function",
      hostVersion: api.runtime?.version ?? "",
    });
  }

  /** Registers only what the capability probe proved. */
  register(): void {
    if (modeEnabled(this.capability, "local_gate")) {
      this.api.on("before_tool_call", (event, ctx) => this.onBeforeToolCall(event, ctx));
    }
    this.api.on("session_end", (event) => this.onSessionEnd(event));
    if (modeEnabled(this.capability, "reader") && typeof this.api.registerTool === "function") {
      this.api.registerTool((ctx) => this.readerTool(ctx), { name: READER_TOOL_NAME });
    }
    this.api.logger?.info(
      `context-shunt capability report: ${JSON.stringify(reportToJson(this.capability))}`,
    );
  }

  /** Veto an oversized or unprovable read before the tool runs. */
  onBeforeToolCall(
    event: Record<string, unknown>,
    ctx?: Record<string, unknown>,
  ): { block: true; blockReason: string } | undefined {
    if (!this.config.gateEnabled) return undefined;
    const params = event?.params;
    const { tool, args } = normalizeToolCall(
      String(event?.toolName ?? ""),
      typeof params === "object" && params !== null
        ? params as Record<string, unknown>
        : undefined,
    );
    if (tool === "other") return undefined;
    const session = this.session(sessionIdOf(ctx, event));
    let decision: GateDecision;
    try {
      decision = session.evaluateToolCall(tool, args);
    } catch {
      // Fail closed for a read-like call we could not evaluate.
      return { block: true, blockReason: serialize(session.blockEnvelope(requestIdFrom(event?.toolCallId), {
        decision: "blocked", form: "unclassifiable", code: "HOST_UNSAFE", reason: "GATE_ERROR",
      })) };
    }
    if (decision.decision !== "blocked") return undefined;
    const envelope = session.blockEnvelope(requestIdFrom(event?.toolCallId), decision);
    return { block: true, blockReason: serialize(envelope) };
  }

  /**
   * The registered tool object. `execute(toolCallId, params)` is the host's shape, and the
   * bounded envelope is returned as a single text content block.
   */
  readerTool(toolContext: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      name: READER_TOOL_NAME,
      description: READER_TOOL_DESCRIPTION,
      parameters: READER_TOOL_PARAMETERS,
      execute: async (toolCallId: string, params: unknown, signal?: AbortSignal) => {
        const text = await this.onReaderTool(params, { ...toolContext, toolCallId, signal });
        return { content: [{ type: "text", text }] };
      },
    };
  }

  /** Answer a question about registered sources. Read-only. */
  async onReaderTool(input: any, ctx?: Record<string, unknown>): Promise<string> {
    const session = this.session(sessionIdOf(ctx));
    const requestId = requestIdFrom(ctx?.toolCallId ?? "reader");
    const paths: string[] = Array.isArray(input?.paths)
      ? input.paths.map(String)
      : typeof input?.paths === "string"
        ? [input.paths]
        : [];

    const sources: Array<Record<string, unknown>> = [];
    try {
      for (const path of paths.slice(0, this.config.limits.maxSourcesPerRequest)) {
        const entry = session.registerPath(path);
        sources.push({
          source_id: entry.sourceId,
          snapshot_id: entry.snapshot.snapshotId,
          selector: input?.selector ?? { kind: "all" },
        });
      }
    } catch (err) {
      if (!isShuntError(err)) throw err;
      return serialize(enforceOrFixed(errorEnvelope(requestId, err), this.config.limits));
    }

    const envelope = await session.read({
      schema_version: SCHEMA_VERSION,
      request_id: requestId,
      operation: "read",
      question: typeof input?.question === "string" ? input.question : "",
      sources,
      budgets: {
        max_chunks: this.config.limits.maxChunksPerRequest,
        max_answer_bytes: this.config.limits.maxAnswerBytes,
        deadline_ms: this.config.limits.requestDeadlineMs,
      },
    }, ctx?.["signal"] instanceof AbortSignal ? ctx["signal"] : undefined);
    return serialize(envelope);
  }

  onSessionEnd(event: Record<string, unknown>): void {
    const key = sessionIdOf(undefined, event);
    const session = this.sessions.get(key);
    if (session) {
      session.close();
      this.sessions.delete(key);
    }
  }

  capabilityJson(): Record<string, unknown> {
    return reportToJson(this.capability);
  }

  private session(key: string): ShuntSession {
    let session = this.sessions.get(key);
    if (!session) {
      session = new ShuntSession(key, this.config, this.capability, { provider: this.provider() });
      this.sessions.set(key, session);
    }
    return session;
  }

  private provider(): LunaProvider {
    const llm = this.api.runtime?.llm;
    if (!llm || typeof llm.complete !== "function") {
      return new UnavailableProvider("HOST_LLM_UNAVAILABLE");
    }
    return new HostBridgeProvider(async ({ system, user, model, maxOutputTokens, timeoutMs, signal }) => {
      // The host owns credentials and routing; provider exception text is dropped by
      // HostBridgeProvider so only MODEL_ERROR crosses back.
      const result = await llm.complete({
        messages: [{ role: "user", content: user }],
        systemPrompt: system,
        model: `openai/${model}`,
        maxTokens: maxOutputTokens,
        temperature: 0,
        signal,
        purpose: "context-shunt-reader",
        execution: { mode: "isolated-agent-runtime", timeoutMs },
      });
      if (result.provider !== "openai") {
        throw new ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", false);
      }
      return {
        text: result.text ?? "",
        model: result.model ?? "",
        input_tokens: result.usage?.inputTokens ?? 0,
        output_tokens: result.usage?.outputTokens ?? 0,
      };
    }, this.config.limits, READER_MODEL);
  }
}

function sessionIdOf(
  ctx?: Record<string, unknown>,
  event?: Record<string, unknown>,
): string {
  for (const candidate of [ctx?.["sessionId"], event?.["sessionId"], ctx?.["sessionKey"], event?.["sessionKey"]]) {
    if (typeof candidate === "string" && candidate.length > 0) return candidate;
  }
  return "unbound";
}

function serialize(envelope: Envelope | Record<string, unknown>): string {
  return JSON.stringify(envelope);
}

/** OpenClaw plugin entry. */
export default {
  id: PLUGIN_ID,
  name: "Context Shunt",
  description: "Read-only large-source gate and question-driven reader.",
  register(api: OpenClawPluginApi): void {
    try {
      new ContextShuntPlugin(api).register();
    } catch (err) {
      // A refused configuration (writer.enabled, a non-Luna model, a widened cap) must
      // stop the plugin loudly rather than load a half-configured gate.
      const code = isShuntError(err) ? (err as ShuntError).code : "INVALID_REQUEST";
      api.logger?.warn?.(`context-shunt refused to load: ${code}`);
      throw err;
    }
  },
};
