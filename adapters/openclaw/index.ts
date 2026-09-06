/**
 * OpenClaw plugin entry point for context-shunt.
 *
 * Wiring (verified against the OpenClaw plugin SDK, host 2026.9.x):
 *
 * - `api.on("before_tool_call", handler, { matcher })` runs before the tool executes and a
 *   deny decision short-circuits it. That is what makes the large-read gate a
 *   pre-execution gate: a blocked read never runs. The host fails this hook closed on
 *   timeout, which matches what the gate needs.
 * - The runtime model bridge is pinned to `gpt-5.6-luna`. If the host cannot serve that
 *   model the reader reports `MODEL_ERROR` rather than answering with a substitute.
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
  on(hook: string, handler: (event: any, ctx?: any) => unknown, opts?: Record<string, unknown>): void;
  registerTool?(definition: Record<string, unknown>, handler: (input: any, ctx?: any) => unknown): void;
  pluginConfig?: Record<string, unknown>;
  logger?: { info(msg: string, ...rest: unknown[]): void; warn?(msg: string, ...rest: unknown[]): void };
  hostVersion?: string;
  availableHooks?: readonly string[];
  llm?: {
    complete(opts: {
      messages: Array<{ role: string; content: string }>;
      model: string;
      maxTokens?: number;
      temperature?: number;
      timeoutMs?: number;
    }): Promise<{ text?: string; model?: string; usage?: { inputTokens?: number; outputTokens?: number } }>;
  };
}

const DEFAULT_HOOKS = [
  "before_tool_call",
  "after_tool_call",
  "tool_result_persist",
  "before_message_write",
  "session_end",
] as const;

export const READER_TOOL_DEFINITION = {
  name: "context_shunt_read",
  description:
    "Answer a question about one or more large files without pulling them into this " +
    "conversation. Returns a bounded, citation-verified answer. Read-only.",
  parameters: {
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
  },
} as const;

export class ContextShuntPlugin {
  private readonly sessions = new Map<string, ShuntSession>();
  readonly config: Config;
  readonly capability: CapabilityReport;

  constructor(
    private readonly api: OpenClawPluginApi,
    opts: { defaultRoot?: string; defaultCacheDir?: string } = {},
  ) {
    const raw = { ...(api.pluginConfig ?? {}) } as Record<string, unknown>;
    if (!Array.isArray(raw["workspace_roots"]) || (raw["workspace_roots"] as unknown[]).length === 0) {
      raw["workspace_roots"] = [opts.defaultRoot ?? process.cwd()];
    }
    const cacheDir =
      opts.defaultCacheDir ?? process.env["CONTEXT_SHUNT_CACHE"] ?? join(homedir(), ".cache", "context-shunt");
    this.config = loadConfig(raw as never, cacheDir);
    this.capability = buildCapabilityReport({
      hooks: api.availableHooks ?? DEFAULT_HOOKS,
      hasModelBridge: typeof api.llm?.complete === "function",
      hostVersion: api.hostVersion ?? "",
    });
  }

  /** Registers only what the capability probe proved. */
  register(): void {
    if (modeEnabled(this.capability, "local_gate")) {
      this.api.on("before_tool_call", (event, ctx) => this.onBeforeToolCall(event, ctx));
    }
    this.api.on("session_end", (event) => this.onSessionEnd(event));
    if (modeEnabled(this.capability, "reader") && typeof this.api.registerTool === "function") {
      this.api.registerTool(READER_TOOL_DEFINITION as unknown as Record<string, unknown>, (input, ctx) =>
        this.onReaderTool(input, ctx),
      );
    }
    this.api.logger?.info(
      `context-shunt capability report: ${JSON.stringify(reportToJson(this.capability))}`,
    );
  }

  /** Veto an oversized or unprovable read before the tool runs. */
  onBeforeToolCall(event: any, ctx?: any): { decision: "deny"; reason: string } | undefined {
    if (!this.config.gateEnabled) return undefined;
    const { tool, args } = normalizeToolCall(String(event?.toolName ?? ""), event?.params);
    if (tool === "other") return undefined;
    const session = this.session(String(ctx?.sessionKey ?? event?.sessionKey ?? "default"));
    let decision: GateDecision;
    try {
      decision = session.evaluateToolCall(tool, args);
    } catch (err) {
      if (!isShuntError(err)) throw err;
      // Fail closed for a read-like call we could not evaluate.
      return { decision: "deny", reason: serialize(session.blockEnvelope(requestIdFrom(event?.toolCallId), {
        decision: "blocked", form: "unclassifiable", code: "HOST_UNSAFE", reason: "GATE_ERROR",
      })) };
    }
    if (decision.decision !== "blocked") return undefined;
    const envelope = session.blockEnvelope(requestIdFrom(event?.toolCallId), decision);
    return { decision: "deny", reason: serialize(envelope) };
  }

  /** Answer a question about registered sources. Read-only. */
  async onReaderTool(input: any, ctx?: any): Promise<string> {
    const session = this.session(String(ctx?.sessionKey ?? "default"));
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
    });
    return serialize(envelope);
  }

  onSessionEnd(event: any): void {
    const key = String(event?.sessionKey ?? "default");
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
    const llm = this.api.llm;
    if (!llm || typeof llm.complete !== "function") {
      return new UnavailableProvider("HOST_LLM_UNAVAILABLE");
    }
    return new HostBridgeProvider(async ({ system, user, model, maxOutputTokens, timeoutMs }) => {
      // The host owns credentials and routing; provider exception text is dropped by
      // HostBridgeProvider so only MODEL_ERROR crosses back.
      const result = await llm.complete({
        messages: [
          { role: "system", content: system },
          { role: "user", content: user },
        ],
        model,
        maxTokens: maxOutputTokens,
        temperature: 0,
        timeoutMs,
      });
      return {
        text: result.text ?? "",
        model: result.model ?? "",
        input_tokens: result.usage?.inputTokens ?? 0,
        output_tokens: result.usage?.outputTokens ?? 0,
      };
    }, this.config.limits, READER_MODEL);
  }
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
