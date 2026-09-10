/** Synthetic OpenClaw capture engine; live middleware remains retired/unregistered. */
import {
  type Limits, type ShuntSession, ShuntError, enforceOrFixed, fixedError,
} from "@context-shunt/core";
import { normalizeToolIdentity, requestIdFrom } from "./normalize.js";

export interface AgentToolResult {
  content: Array<{ type: string; text?: string; data?: string; mimeType?: string }>;
  details?: unknown;
  [key: string]: unknown;
}
export interface ToolResultEvent {
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
  result: AgentToolResult;
  isError?: boolean;
  threadId?: string;
  turnId?: string;
  cwd?: string;
}
export interface MiddlewareContext extends Record<string, unknown> {
  runtime: "openclaw" | "codex";
  sessionKey?: string;
  sessionId?: string;
}
export type AgentToolResultMiddleware = (
  event: ToolResultEvent, ctx: MiddlewareContext,
) => { result: AgentToolResult } | void;

const DEFAULT_READ_ONLY_TOOLS = ["read", "web_fetch", "web_search", "read_mcp_resource"];
const PROTECTED_TOOLS = new Set([
  "skill_view", "skills_list", "ask_user", "clarify", "todo",
  "context_shunt_read", "context_shunt_inspect", "context_shunt_stats",
  "list_mcp_resources", "list_mcp_resource_templates", "message",
]);
const MCP_CATALOG_TOOLS = new Set(["list_resources", "list_prompts", "get_prompt"]);
// Preserve the adapter's session/message/mutation guards as delimited identity families,
// not substring guesses (e.g. "rewrite" does not mean "write").
const CONTROL_ACTIONS = new Set([
  "send", "spawn", "write", "edit", "delete", "remove", "update", "create", "terminate",
]);

export type ToolResultClassification = "protected" | "eligible" | "passthrough";

/** Identity only, before payload access or session/store/provider/accounting work.
 * A native MCP tool can shadow a generated read utility. Without immutable executed-
 * handler provenance, mcp__<server>__read_resource requires exact operator opt-in.
 */
export function classifyToolResult(
  toolName: string, eligible: ReadonlySet<string> = new Set(DEFAULT_READ_ONLY_TOOLS),
): ToolResultClassification {
  const name = normalizeToolIdentity(toolName);
  const parts = name.split("_");
  const mcp = /^mcp__([a-z0-9_]+)__(list_resources|list_prompts|get_prompt)$/.exec(name);
  if (PROTECTED_TOOLS.has(name)
    || (mcp && MCP_CATALOG_TOOLS.has(mcp[2]!))
    || (name.startsWith("sessions_") && name.length > "sessions_".length)
    || parts.some((part) => CONTROL_ACTIONS.has(part))) return "protected";
  if (name && (DEFAULT_READ_ONLY_TOOLS.includes(name)
    || [...eligible].some((entry) => normalizeToolIdentity(entry) === name))) return "eligible";
  return "passthrough";
}

const CONTROL_DETAILS = ["messageDelivery", "deliveryStatus", "messageId", "deliveryId",
  "childSessionKey", "runId", "termination", "terminate", "sideEffects"];

/** Adapter-only producer declaration; the core configuration/storage schema stays shared. */
export function captureToolsFrom(raw: Record<string, unknown>): ReadonlySet<string> {
  const capture = raw["tool_result_capture"];
  if (!capture || typeof capture !== "object" || Array.isArray(capture)) {
    return new Set(DEFAULT_READ_ONLY_TOOLS);
  }
  const copy = { ...capture } as Record<string, unknown>;
  const names = copy["read_only_tools"];
  delete copy["read_only_tools"];
  raw["tool_result_capture"] = copy;
  if (names === undefined) return new Set(DEFAULT_READ_ONLY_TOOLS);
  if (!Array.isArray(names) || names.length > 100 || names.some((name) =>
    typeof name !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(name))) {
    throw new ShuntError("INVALID_REQUEST", "INVALID_CAPTURE_TOOLS", false);
  }
  return new Set([...DEFAULT_READ_ONLY_TOOLS, ...names as string[]]);
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

/** Preserve success/error facts without copying arbitrary payload bytes into a pointer. */
function semanticDetails(details: unknown): Record<string, unknown> {
  const safe: Record<string, unknown> = {};
  const source = record(details);
  for (const key of ["ok", "success", "isError", "timedOut"]) {
    if (typeof source?.[key] === "boolean") safe[key] = source[key];
  }
  const exitCode = source?.["exitCode"];
  if (typeof exitCode === "number" && Number.isFinite(exitCode)) safe["exitCode"] = exitCode;
  const status = typeof source?.["status"] === "string" ? source["status"].trim().toLowerCase() : "";
  if (["ok", "success", "completed", "error", "failed", "failure", "timeout", "timed_out",
    "blocked", "denied", "forbidden", "unavailable", "approval-unavailable", "disabled",
    "aborted", "cancelled", "canceled", "killed", "invalid"].includes(status)) safe["status"] = status;
  const signal = source?.["signal"];
  if (typeof signal === "string" && /^SIG[A-Z0-9]{1,12}$/.test(signal)) safe["signal"] = signal;
  // The host classifier treats any truthy error as failure, even with ok=true.
  // Keep the fact, never its potentially oversized error message/object.
  if (source?.["error"]) safe["error"] = true;
  return safe;
}

export function captureToolResult(
  event: ToolResultEvent, ctx: MiddlewareContext, eligible: ReadonlySet<string>,
  session: () => ShuntSession, limits: Limits,
): { result: AgentToolResult } | void {
  if (classifyToolResult(event.toolName, eligible) !== "eligible") return;
  const requestId = requestIdFrom(event.toolCallId);
  // Throws intentionally reach the host's bounded error and delivered-message fallback.
  // Do not catch and return event.result, including when any property getter throws.
  const original = event.result;
  const details = record(original.details);
  if (CONTROL_DETAILS.some((key) => details && key in details)
    || details?.["status"] === "accepted"
    || Object.keys(original).some((key) => !["content", "details", "isError"].includes(key))) return;
  const replacement = (code: string, guidance: string) => ({ result: {
    content: [{ type: "text", text: JSON.stringify(enforceOrFixed({
      ...fixedError(requestId, code), guidance,
    }, limits)) }],
    details: { ...semanticDetails(original.details), contextShunt: "withheld" },
    ...(typeof original["isError"] === "boolean" ? { isError: original["isError"] } : {}),
  } });
  try {
    if (ctx.runtime !== "openclaw" && ctx.runtime !== "codex") throw new Error("runtime");
    if (!Array.isArray(original.content) || original.content.length === 0) throw new Error("content");
    // UTF-16-safe truncation can leave 99999 chars when a surrogate straddles the cap.
    // The host may also drop/merge earlier blocks without a marker; even below these
    // boundaries only the middleware-visible representation is an immutable snapshot.
    if (original.content.length >= 200 || original.content.some((block) =>
      block.type === "text" && typeof block.text === "string" && block.text.length >= 99_999)
      || details?.["truncated"] === true
      || (original.details !== undefined && Buffer.byteLength(JSON.stringify(original.details), "utf8") >= 100_000)) {
      return replacement("HOST_UNSAFE", "HOST_INGRESS_AMBIGUOUS: output withheld; no complete snapshot or handle published. Retry a bounded producer query.");
    }
    if (original.content.some((block) => block.type !== "text" || typeof block.text !== "string"
      || Object.keys(block).some((key) => !["type", "text"].includes(key)))) {
      return replacement("BINARY_UNSUPPORTED", "Non-text or invalid middleware content withheld; no snapshot published.");
    }
    const outcome = session().postToolResult(requestId, original, { upstreamTruncated: true });
    if (!outcome) throw new Error("capture disabled");
    if (outcome.action === "passthrough") return;
    if (!outcome.envelope) throw new Error("missing envelope");
    const envelope = enforceOrFixed({ ...outcome.envelope,
      guidance: outcome.action === "spill"
        ? "Immutable middleware-visible text/JSON snapshot; original upstream completeness is unknown. Ask context_shunt_read a real question using this handle."
        : "Tool output withheld; capture failed. No raw fallback. Retry a bounded producer query.",
    }, limits);
    return { result: {
      content: [{ type: "text", text: JSON.stringify(envelope) }],
      details: { ...semanticDetails(original.details), contextShunt: "withheld" },
      ...(typeof original["isError"] === "boolean" ? { isError: original["isError"] } : {}),
    } };
  } catch {
    return replacement("SPILL_FAILED", "Tool output withheld; capture failed. No raw fallback.");
  }
}
