/** Map an OpenClaw tool call onto the core's `(tool, args)` shape. */
import { READ_TOOLS, SEARCH_TOOLS, SHELL_TOOLS } from "./capability.js";

export type CoreTool = "read" | "search" | "shell" | "other";

export function normalizeToolCall(
  toolName: string,
  params: Record<string, unknown> | undefined,
): { tool: CoreTool; args: Record<string, unknown> } {
  const name = (toolName ?? "").trim().toLowerCase();
  const args = params ?? {};
  if (name in READ_TOOLS) {
    return {
      tool: "read",
      args: {
        file_path: args["file_path"] ?? args["path"] ?? args["filename"],
        offset: args["offset"] ?? args["start_line"],
        limit: args["limit"] ?? args["lines"] ?? args["num_lines"],
      },
    };
  }
  if (name in SEARCH_TOOLS) {
    return {
      tool: "search",
      args: {
        path: args["path"] ?? args["directory"],
        pattern: args["pattern"] ?? args["query"],
        max_matches: args["max_matches"] ?? args["max_results"] ?? args["limit"],
      },
    };
  }
  if (name in SHELL_TOOLS) {
    return { tool: "shell", args: { command: args["command"] ?? args["cmd"] ?? "" } };
  }
  return { tool: "other", args: {} };
}

/** A bounded, host-safe request id derived from host correlation fields. */
export function requestIdFrom(raw: unknown): string {
  const text = typeof raw === "string" ? raw : "";
  const safe = text.replace(/[^A-Za-z0-9_.:-]/g, "").slice(0, 56);
  return `req_${safe || "gate"}`;
}
