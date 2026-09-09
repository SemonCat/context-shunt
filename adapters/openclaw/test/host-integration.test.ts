/** Deterministic integration against the installed 2026.9.3 host loader/runner.
 * No gateway, live configuration, provider calls, deployment or host source writes.
 * Requires CONTEXT_SHUNT_OPENCLAW_ROOT; absent prerequisites are NOT_RUN in verify.
 */
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { modeEnabled } from "@context-shunt/core";

import { buildCapabilityReport } from "../src/capability.js";

const ROOT = process.env["CONTEXT_SHUNT_OPENCLAW_ROOT"] ?? "";
const available = ROOT.length > 0 && existsSync(join(ROOT, "package.json"));

function hostFile(relative: string): string {
  const path = join(ROOT, relative);
  if (!existsSync(path)) throw new Error(`host file missing: ${relative}`);
  return readFileSync(path, "utf8");
}

describe.skipIf(!available)("openclaw host integration", () => {
  it("reads the installed host version", () => {
    const pkg = JSON.parse(hostFile("package.json"));
    expect(pkg.name).toBe("openclaw");
    expect(String(pkg.version)).toBe("2026.9.3");
  });

  it("exposes the typed hooks the adapter depends on", () => {
    const hookTypes = hostFile("src/plugins/hook-types.ts");
    for (const hook of ["before_tool_call", "after_tool_call", "tool_result_persist"]) {
      expect(hookTypes).toContain(`"${hook}"`);
    }
  });

  it("still documents after_tool_call as observe-only", () => {
    const docs = hostFile("docs/plugins/hooks.md");
    // The capability table row, not the matcher row that merely names the hook.
    const row = docs
      .split("\n")
      .find((line) => line.trimStart().startsWith("| `after_tool_call`"));
    expect(row).toBeDefined();
    expect(row).toMatch(/Observe/);
  });

  it("exposes the official fail-closed middleware with ingress ceilings", () => {
    const runner = hostFile("src/agents/harness/tool-result-middleware.ts");
    for (const evidence of ["MAX_MIDDLEWARE_CONTENT_BLOCKS = 200", "MAX_MIDDLEWARE_TEXT_CHARS = 100_000",
      "MAX_MIDDLEWARE_DETAILS_BYTES = 100_000", "MAX_MIDDLEWARE_IMAGE_DATA_CHARS = 5_000_000",
      "sanitizeToolResultForMiddleware(event.result)", "Tool output unavailable due to post-processing error.",
      "buildDeliveredMessagingFailureFallback", "for (const handler of handlersForRun)"]) expect(runner).toContain(evidence);
    const registrar = hostFile("src/plugins/registry-registrars-tools-hooks.ts");
    expect(registrar).toContain("plugin must declare contracts.agentToolResultMiddleware");
    expect(registrar).toContain("plugin must be explicitly enabled to register agent tool result middleware");
    expect(hostFile("src/plugins/agent-tool-result-middleware-types.ts")).not.toContain("priority");
  });

  it("reports capture supported only with enabled official registration", () => {
    const pkg = JSON.parse(hostFile("package.json"));
    const report = buildCapabilityReport({
      hooks: ["before_tool_call", "after_tool_call", "tool_result_persist", "session_end"],
      hasModelBridge: true,
      hostVersion: String(pkg.version),
      hasToolResultMiddleware: true, captureEnabled: true,
    });
    expect(report.hostVersion).toBe(String(pkg.version));
    expect(modeEnabled(report, "tool_result_capture")).toBe(true);
    expect(modeEnabled(report, "local_gate")).toBe(true);
    expect(modeEnabled(report, "reader")).toBe(true);
    // The two paths that need no provider at all.
    expect(modeEnabled(report, "deterministic_inspect")).toBe(true);
    expect(modeEnabled(report, "session_stats")).toBe(true);
    expect(modeEnabled(report, "session_lifecycle")).toBe(true);
  });

  it("still declares the session_end reason enum the lifecycle decision rests on", () => {
    // The adapter keeps handles through a compaction and revokes only on an explicit
    // clear. That decision is only sound while the host's own reason enum says so.
    const hookTypes = hostFile("src/plugins/hook-types.ts");
    const start = hookTypes.indexOf("export type PluginHookSessionEndReason");
    expect(start).toBeGreaterThan(-1);
    const block = hookTypes.slice(start, hookTypes.indexOf(";", start));
    for (const reason of ["new", "reset", "deleted", "compaction", "idle", "shutdown"]) {
      expect(block).toContain(`"${reason}"`);
    }
    // If a future host stops rotating on compaction, the lifecycle rule needs redoing.
    expect(hookTypes).toContain("nextSessionId?: string");
  });

  it("still says absent usage must not be projected as zero", () => {
    // The adapter passes absent token counts through as absent on the strength of this.
    const isolated = hostFile("src/agents/isolated-completion.ts");
    expect(isolated).toContain("absence must not be projected as zero");
    // And the isolated runtime is still the zero-tool, no-conversation path.
    const runtime = hostFile("src/plugins/runtime/types-core.ts");
    expect(runtime).toContain("Fresh, literal-zero-tool completion through the configured agent runtime.");
    expect(runtime).toContain("Isolated runtimes currently accept one fresh user prompt, not a replayed chat history.");
  });

  it("registers a tool name the host manifest contract declares", () => {
    // OpenClaw rejects a runtime registration that is not declared in contracts.tools.
    const manifest = JSON.parse(hostFile("package.json")) && JSON.parse(
      readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"),
    );
    expect(manifest.contracts.tools).toContain("context_shunt_read");
    expect(manifest.contracts.tools).toContain("context_shunt_inspect");
    expect(manifest.contracts.tools).toContain("context_shunt_stats");
  });

  it("loads through the real host and vetoes before the wrapped tool executes", () => {
    const temp = mkdtempSync(join(tmpdir(), "context-shunt-openclaw-host-"));
    const workspace = join(temp, "workspace");
    mkdirSync(workspace);
    const source = join(workspace, "large.txt");
    writeFileSync(source, Array.from({ length: 400 }, (_, i) => `line ${i}\n`).join(""));
    const pluginRoot = fileURLToPath(new URL("..", import.meta.url));
    const script = String.raw`
      import { join } from "node:path";
      import { pathToFileURL } from "node:url";
      const root = process.env.HOST_ROOT;
      const pluginRoot = process.env.PLUGIN_ROOT;
      const workspace = process.env.TEST_WORKSPACE;
      const source = process.env.TEST_SOURCE;
      const loader = await import(pathToFileURL(join(root, "dist/plugins/loader.js")).href);
      const harness = await import(pathToFileURL(join(root, "dist/plugin-sdk/agent-harness-runtime.js")).href);
      const config = { plugins: { allow: ["context-shunt"], load: { paths: [pluginRoot] }, entries: {
        "context-shunt": { enabled: true, llm: { allowModelOverride: true,
          allowedModels: ["openai/gpt-5.6-luna"],
          allowedCompletionModels: ["openai/gpt-5.6-luna"] },
          config: { workspace_roots: [workspace], cache_dir: join(workspace, "..", ".cache"),
            tool_result_capture: { enabled: true, read_only_tools: ["mcp__logs__query"] } } }
      } } };
      const registry = loader.loadOpenClawPlugins({ cache: false, activate: true, workspaceDir: workspace, config });
      const record = registry.plugins.find((entry) => entry.id === "context-shunt");
      const registration = registry.tools.find((entry) => entry.names.includes("context_shunt_read"));
      const reader = registration?.factory({ sessionKey: "agent:main:shunt", sessionId: "shunt" });
      const toolNames = registry.tools.flatMap((entry) => entry.names).sort();
      let executions = 0;
      const rawTool = { name: "read", label: "read", description: "sentinel",
        parameters: { type: "object", additionalProperties: false,
          properties: { path: { type: "string" } }, required: ["path"] },
        execute: async () => { executions += 1; return { content: [{ type: "text", text: "EXECUTED" }] }; } };
      const wrapped = harness.wrapToolWithBeforeToolCallHook(rawTool, {
        config, sessionKey: "agent:main:shunt", sessionId: "shunt", agentId: "main", runId: "run-shunt"
      }, { emitDiagnostics: false });
      const result = await wrapped.execute("tc-host", { path: source });
      const middlewares = registry.agentToolResultMiddlewares.filter((entry) => entry.pluginId === "context-shunt");
      const captureEvent = { toolCallId: "tc-capture", toolName: "mcp__logs__query", args: {},
        result: { content: [{ type: "text", text: "RAW_CAPTURE_SENTINEL_".repeat(2500) }] } };
      const runtimeResults = [];
      for (const runtime of ["openclaw", "codex"]) {
        const runner = harness.createAgentToolResultMiddlewareRunner({ runtime, sessionKey: "agent:main:shunt", sessionId: "shunt" }, middlewares.map((entry) => entry.handler));
        const captured = await runner.applyToolResultMiddleware(captureEvent);
        const capped = await runner.applyToolResultMiddleware({ ...captureEvent,
          result: { content: [{ type: "text", text: "RAW_CAPTURE_SENTINEL_".repeat(6000) }] } });
        const failures = [];
        for (const handler of [() => { throw Error("RAW_CAPTURE_SENTINEL_"); }, () => ({ result: { content: null } })]) {
          const failedRunner = harness.createAgentToolResultMiddlewareRunner({ runtime }, [handler]);
          failures.push(await failedRunner.applyToolResultMiddleware(captureEvent));
          failures.push(await failedRunner.applyToolResultMiddleware({ ...captureEvent, toolName: "message", args: { action: "send", target: "test" },
            result: { content: [{ type: "text", text: "RAW_CAPTURE_SENTINEL_" }], details: { messageDelivery: { status: "settled" }, ok: true, messageId: "receipt" } } }));
        }
        runtimeResults.push({ runtime, captured, capped, failures });
      }
      console.log(JSON.stringify({ middlewareCount: middlewares.length, runtimeResults, status: record?.status, errors: registry.diagnostics.filter((entry) => entry.level === "error"),
        hooks: registry.typedHooks.map((entry) => entry.hookName), reader: reader?.name,
        toolNames,
        executions, blocked: result?.details?.status, envelope: JSON.parse(result.content[0].text) }));
    `;
    const stdout = execFileSync(process.execPath, ["--input-type=module", "-e", script], {
      encoding: "utf8",
      timeout: 60_000,
      maxBuffer: 1024 * 1024,
      env: {
        ...process.env,
        HOST_ROOT: ROOT,
        PLUGIN_ROOT: pluginRoot,
        TEST_WORKSPACE: workspace,
        TEST_SOURCE: source,
      },
    });
    const result = JSON.parse(stdout.trim().split("\n").at(-1)!);
    expect(result.middlewareCount).toBe(1);
    for (const runtime of result.runtimeResults) {
      expect(JSON.parse(runtime.captured.content[0].text).code).toBe("SPILLED");
      expect(JSON.parse(runtime.capped.content[0].text).code).toBe("HOST_UNSAFE");
      expect(JSON.stringify(runtime)).not.toContain("RAW_CAPTURE_SENTINEL_");
      expect(runtime.failures[0].details).toEqual({ status: "error", middlewareError: true });
      expect(runtime.failures[1].details).toMatchObject({ ok: true, deliveryStatus: "sent" });
      expect(runtime.failures[2].details).toEqual({ status: "error", middlewareError: true });
      expect(runtime.failures[3].details).toMatchObject({ ok: true, deliveryStatus: "sent" });
    }
    expect(result.status).toBe("loaded");
    expect(result.errors).toEqual([]);
    expect(result.hooks).toContain("before_tool_call");
    expect(result.hooks).not.toContain("tool_result_persist");
    expect(result.reader).toBe("context_shunt_read");
    // All three read-only tools are accepted by the real host registry, and no writer is.
    expect(result.toolNames).toEqual([
      "context_shunt_inspect",
      "context_shunt_read",
      "context_shunt_stats",
    ]);
    expect(result.executions).toBe(0);
    expect(result.blocked).toBe("blocked");
    expect(result.envelope.code).toBe("LARGE_READ");
  }, 70_000);
});

describe.skipIf(available)("openclaw host integration prerequisites", () => {
  it("reports the missing prerequisite rather than passing", () => {
    expect(ROOT).toBe("");
  });
});
