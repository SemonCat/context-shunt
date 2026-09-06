/**
 * OpenClaw adapter contract tests.
 *
 * These exercise the adapter against a faithful stand-in for the host plugin API - the
 * hook names, the deny shape, the model bridge - so the wiring, the normalization and the
 * capability decisions are all asserted deterministically. They are not evidence about
 * the live host: that is what the opt-in `integration openclaw` gate is for, and it
 * reports not-run when the host is absent.
 */
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { READER_MODEL, ShuntSession, modeEnabled } from "@context-shunt/core";

import {
  ContextShuntPlugin,
  READER_TOOL_NAME,
  READER_TOOL_PARAMETERS,
  default as plugin,
} from "../index.js";
import { buildCapabilityReport, SUMA_EVIDENCE } from "../src/capability.js";
import { normalizeToolCall, requestIdFrom } from "../src/normalize.js";

interface Registered {
  hooks: string[];
  tools: string[];
}

function fakeApi(overrides: Record<string, unknown> = {}) {
  const registered: Registered = { hooks: [], tools: [] };
  const logs: string[] = [];
  const modelCalls: Array<{ model: string; system: string; user: string }> = [];
  const handlers = new Map<string, (event: any, ctx?: any) => unknown>();
  const api: any = {
    pluginConfig: {},
    hostVersion: "2026.9.2",
    on(hook: string, handler: (event: any, ctx?: any) => unknown) {
      registered.hooks.push(hook);
      handlers.set(hook, handler);
    },
    registerTool(
      factory: (ctx: Record<string, unknown>) => Record<string, unknown>,
      opts?: Record<string, unknown>,
    ) {
      // Mirrors OpenClaw's registerTool(factory, { name }): the factory receives the
      // session-scoped tool context and returns one AnyAgentTool-shaped object.
      const tool = factory({ sessionKey: "s1", sessionId: "sid1" });
      if (typeof tool["execute"] !== "function") {
        throw new Error("registerTool needs a tool object with execute()");
      }
      if (opts?.["name"] !== tool["name"]) throw new Error("declared tool name mismatch");
      registered.tools.push(String(tool["name"]));
      handlers.set(`tool:${String(tool["name"])}`, tool["execute"] as never);
    },
    logger: { info: (msg: string) => logs.push(msg), warn: (msg: string) => logs.push(msg) },
    runtime: {
      version: "2026.9.2",
      llm: {
        async complete(opts: any) {
          modelCalls.push({
            model: opts.model,
            system: opts.systemPrompt,
            user: opts.messages[0].content,
          });
          return {
            text: JSON.stringify({
              answer: "The retry ceiling is three [c1].",
              citations: [{ id: "c1", line_start: 1, line_end: 1, quote: "max_retries = 3" }],
            }),
            provider: "openai",
            model: "gpt-5.6-luna",
            usage: { inputTokens: 12, outputTokens: 8 },
          };
        },
      },
    },
    ...overrides,
  };
  return { api, registered, logs, modelCalls, handlers };
}

function workspace() {
  const dir = mkdtempSync(join(tmpdir(), "shunt-oc-"));
  mkdirSync(join(dir, "ws"), { recursive: true });
  return dir;
}

function configured(dir: string, extra: Record<string, unknown> = {}) {
  const f = fakeApi();
  f.api.pluginConfig = { workspace_roots: [join(dir, "ws")], spill_dir: join(dir, "cache"), ...extra };
  return f;
}

describe("normalization", () => {
  it("maps the covered read, search and shell tools", () => {
    expect(normalizeToolCall("read", { file_path: "/x", limit: 5 })).toEqual({
      tool: "read",
      args: { file_path: "/x", offset: undefined, limit: 5 },
    });
    expect(normalizeToolCall("grep", { pattern: "a", max_matches: 5 }).tool).toBe("other");
    expect(normalizeToolCall("exec", { command: "cat /x" })).toEqual({
      tool: "shell",
      args: { command: "cat /x" },
    });
  });

  it("leaves an uncovered tool alone", () => {
    expect(normalizeToolCall("spawn_agent", { prompt: "hi" }).tool).toBe("other");
  });

  it("sanitizes a host correlation id into a bounded request id", () => {
    expect(requestIdFrom("call/../../etc/passwd")).toBe("req_call....etcpasswd");
    expect(requestIdFrom(undefined)).toBe("req_gate");
    expect(requestIdFrom("x".repeat(200)).length).toBeLessThanOrEqual(60);
  });
});

describe("capability probe", () => {
  it("supports the local gate and the reader when the host provides them", () => {
    const report = buildCapabilityReport({
      hooks: ["before_tool_call"],
      hasModelBridge: true,
      hostVersion: "2026.9.2",
    });
    expect(modeEnabled(report, "local_gate")).toBe(true);
    expect(modeEnabled(report, "reader")).toBe(true);
    expect(report.readerModel).toBe(READER_MODEL);
  });

  it("reports the Suma post-tool mode unsupported with host-source evidence", () => {
    const report = buildCapabilityReport({
      hooks: ["before_tool_call", "tool_result_persist", "after_tool_call"],
      hasModelBridge: true,
      hostVersion: "2026.9.2",
    });
    const mode = report.modes.find((m) => m.mode === "suma_post_tool")!;
    expect(mode.support).toBe("unsupported");
    expect([...mode.reasons]).toEqual([
      "CAPTURE_AFTER_TRUNCATION",
      "OBSERVE_ONLY_HOOK",
      "HOST_FAIL_OPEN",
    ]);
    expect(mode.evidence).toEqual(SUMA_EVIDENCE);
    expect(mode.evidence.length).toBeGreaterThan(0);
  });

  it("disables the gate when the hook is missing", () => {
    const report = buildCapabilityReport({ hooks: [], hasModelBridge: true, hostVersion: "x" });
    expect(modeEnabled(report, "local_gate")).toBe(false);
    expect(report.modes[0]!.reasons).toContain("HOOK_MISSING");
  });

  it("disables the reader when the model bridge is missing or tracing is unsafe", () => {
    const noModel = buildCapabilityReport({ hooks: ["before_tool_call"], hasModelBridge: false, hostVersion: "x" });
    expect(modeEnabled(noModel, "reader")).toBe(false);
    const tracing = buildCapabilityReport({
      hooks: ["before_tool_call"],
      hasModelBridge: true,
      hostVersion: "x",
      unsafeTracing: true,
    });
    expect(modeEnabled(tracing, "local_gate")).toBe(false);
    expect(modeEnabled(tracing, "reader")).toBe(false);
  });
});

describe("registration", () => {
  it("registers the gate hook and the read-only reader, and no writer", () => {
    const dir = workspace();
    const { api, registered, logs } = configured(dir);
    new ContextShuntPlugin(api).register();
    expect(registered.hooks).toContain("before_tool_call");
    expect(registered.tools).toEqual(["context_shunt_read"]);
    expect(registered.tools.some((t) => /write|patch|edit/i.test(t))).toBe(false);
    expect(logs.join("\n")).toContain("capability report");
  });

  it("does not register the reader when the isolated model bridge is absent", () => {
    const dir = workspace();
    const { api, registered } = configured(dir);
    delete api.runtime.llm;
    new ContextShuntPlugin(api).register();
    expect(registered.hooks).toContain("before_tool_call");
    expect(registered.tools).toEqual([]);
  });

  it("refuses to load with writer.enabled and reports the refusal", () => {
    const dir = workspace();
    const { api, logs } = configured(dir, { writer: { enabled: true } });
    expect(() => plugin.register(api)).toThrow();
    expect(logs.join("\n")).toContain("refused to load");
  });

  it("refuses a non-Luna reader model", () => {
    const dir = workspace();
    const { api } = configured(dir, { reader: { model: "gpt-5.6-sol" } });
    expect(() => new ContextShuntPlugin(api)).toThrow();
  });

  it("refuses a widened cap", () => {
    const dir = workspace();
    const { api } = configured(dir, { limits: { fullReadMaxLines: 5000 } });
    expect(() => new ContextShuntPlugin(api)).toThrow();
  });

  it("accepts a narrowed cap", () => {
    const dir = workspace();
    const { api } = configured(dir, { limits: { fullReadMaxLines: 100 } });
    expect(new ContextShuntPlugin(api).config.limits.fullReadMaxLines).toBe(100);
  });

  it("keeps the Suma post-tool mode off even when configuration asks for it", () => {
    const dir = workspace();
    const { api } = configured(dir, { suma_post_tool: { enabled: true } });
    const p = new ContextShuntPlugin(api);
    expect(p.config.sumaPostToolEnabled).toBe(true);
    expect(modeEnabled(p.capability, "suma_post_tool")).toBe(false);
    // Configuration alone cannot enable it: the session refuses to run the mode.
    expect(p.capabilityJson()["modes"]).toBeDefined();
  });
});

describe("before_tool_call gate", () => {
  function planted(dir: string, lines: number) {
    const path = join(dir, "ws", `f${lines}.txt`);
    writeFileSync(path, Array.from({ length: lines }, (_, i) => `line ${i}`).join("\n") + "\n");
    return path;
  }

  it("denies a full read over the threshold with a contract envelope", () => {
    const dir = workspace();
    const { api } = configured(dir);
    const p = new ContextShuntPlugin(api);
    const path = planted(dir, 351);
    const result = p.onBeforeToolCall({ toolName: "read", params: { file_path: path }, toolCallId: "tc1" }, { sessionKey: "s1" });
    expect(result?.block).toBe(true);
    const envelope = JSON.parse(String(result?.blockReason));
    expect(envelope.status).toBe("blocked");
    expect(envelope.code).toBe("LARGE_READ");
    expect(envelope.coverage.complete).toBe(false);
    expect(envelope.request_id).toBe("req_tc1");
    expect(JSON.stringify(envelope)).not.toContain(path);
  });

  it("allows a full read at the threshold and a bounded read past it", () => {
    const dir = workspace();
    const { api } = configured(dir);
    const p = new ContextShuntPlugin(api);
    expect(p.onBeforeToolCall({ toolName: "read", params: { file_path: planted(dir, 350) } })).toBeUndefined();
    expect(
      p.onBeforeToolCall({ toolName: "read", params: { file_path: planted(dir, 351), offset: 1, limit: 50 } }),
    ).toBeUndefined();
  });

  it("denies an unprovable read-like shell command and passes other commands through", () => {
    const dir = workspace();
    const { api } = configured(dir);
    const p = new ContextShuntPlugin(api);
    const denied = p.onBeforeToolCall({ toolName: "exec", params: { command: `cat ${planted(dir, 400)} | grep x` } });
    expect(JSON.parse(String(denied?.blockReason)).code).toBe("UNCLASSIFIABLE_READ");
    expect(p.onBeforeToolCall({ toolName: "exec", params: { command: "npm test" } })).toBeUndefined();
  });

  it("passes uncovered tools straight through", () => {
    const dir = workspace();
    const { api } = configured(dir);
    const p = new ContextShuntPlugin(api);
    expect(p.onBeforeToolCall({ toolName: "spawn_agent", params: { prompt: "x" } })).toBeUndefined();
  });

  it("passes everything through when the gate is disabled by configuration", () => {
    const dir = workspace();
    const { api } = configured(dir, { gate_enabled: false });
    const p = new ContextShuntPlugin(api);
    expect(p.onBeforeToolCall({ toolName: "read", params: { file_path: planted(dir, 900) } })).toBeUndefined();
  });
});

describe("reader tool", () => {
  it("answers with Luna and verified citations", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\nbackoff = fixed\n");
    const { api, modelCalls } = configured(dir);
    const p = new ContextShuntPlugin(api);
    const out = JSON.parse(
      await p.onReaderTool({ question: "What is the retry ceiling?", paths: [path] }, { sessionKey: "s1" }),
    );
    expect(out.code).toBe("ANSWERED");
    expect(out.citations[0].verified).toBe(true);
    expect(modelCalls).toHaveLength(1);
    expect(modelCalls[0]!.model).toBe(`openai/${READER_MODEL}`);
    expect(modelCalls[0]!.user).toContain("What is the retry ceiling?");
  });

  it("returns a bounded error for a source outside the workspace roots", async () => {
    const dir = workspace();
    const outside = mkdtempSync(join(tmpdir(), "shunt-out-"));
    const path = join(outside, "secret.txt");
    writeFileSync(path, "classified\n");
    const { api } = configured(dir);
    const out = JSON.parse(
      await new ContextShuntPlugin(api).onReaderTool({ question: "What is in it?", paths: [path] }, {}),
    );
    expect(out.status).toBe("blocked");
    expect(out.code).toBe("UNSAFE_SOURCE");
    expect(JSON.stringify(out)).not.toContain(path);
  });

  it("makes zero model calls without a question", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\n");
    const { api, modelCalls } = configured(dir);
    const out = JSON.parse(await new ContextShuntPlugin(api).onReaderTool({ paths: [path] }, {}));
    expect(modelCalls).toHaveLength(0);
    expect(out.status).toBe("error");
  });

  it("reports MODEL_ERROR rather than substituting when the host serves another model", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\n");
    const { api } = configured(dir);
    api.runtime.llm.complete = async () => ({
      text: "{}",
      provider: "openai",
      model: "gpt-5.6-sol",
    });
    const out = JSON.parse(
      await new ContextShuntPlugin(api).onReaderTool({ question: "What is it?", paths: [path] }, {}),
    );
    expect(out.coverage.omitted[0].reason).toBe("MODEL_ERROR");
    expect(out.answer).toBe("");
  });

  it("declares only a question and paths - no write surface", () => {
    expect(Object.keys(READER_TOOL_PARAMETERS.properties).sort()).toEqual(["paths", "question"]);
  });

  it("registers a tool object whose execute returns a text content block", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\n");
    const { api, registered, handlers } = configured(dir);
    new ContextShuntPlugin(api).register();
    expect(registered.tools).toEqual([READER_TOOL_NAME]);
    const execute = handlers.get(`tool:${READER_TOOL_NAME}`)!;
    const result: any = await (execute as any)("tc_tool", {
      question: "What is the retry ceiling?",
      paths: [path],
    });
    expect(result.content[0].type).toBe("text");
    expect(JSON.parse(result.content[0].text).code).toBe("ANSWERED");
  });

  it("declares the registered tool name in the plugin manifest contracts", () => {
    const manifest = JSON.parse(
      readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"),
    );
    expect(manifest.contracts.tools).toEqual([READER_TOOL_NAME]);
  });
});

describe("Suma post-tool mode is fail-closed on this host", () => {
  it("returns null from postToolResult even for an oversized payload", () => {
    const dir = workspace();
    const { api } = configured(dir, { suma_post_tool: { enabled: true } });
    const p = new ContextShuntPlugin(api);
    // Reaching into the session the way the adapter would if the mode were wired.
    const session = (p as any).session("s1");
    expect(session.sumaEnabled).toBe(false);
    expect(session.postToolResult("req_x", "y".repeat(200000))).toBeNull();
  });

  it("spills correctly once a host is proven safe, so the engine itself is not the blocker", () => {
    const dir = workspace();
    const { api } = configured(dir, { suma_post_tool: { enabled: true } });
    const p = new ContextShuntPlugin(api);
    const proven = {
      ...p.capability,
      modes: p.capability.modes.map((m) =>
        m.mode === "suma_post_tool" ? { ...m, support: "supported" as const, reasons: [] } : m,
      ),
    };
    const session = new ShuntSession("s2", p.config, proven);
    const outcome = session.postToolResult("req_x", "y".repeat(200000));
    expect(outcome?.action).toBe("spill");
    expect(outcome?.envelope?.code).toBe("SPILLED");
    expect(outcome?.envelope?.answer).toBe("");
  });
});
