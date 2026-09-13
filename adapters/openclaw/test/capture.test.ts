import { mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { modeEnabled, ScopeIdentity, ShuntSession, validateEnvelope } from "@context-shunt/core";
import { ContextShuntPlugin } from "../index.js";
import { classifyToolResult, captureToolsFrom, captureToolResult, type AgentToolResult, type AgentToolResultMiddleware, type ToolResultEvent } from "../src/capture.js";

const SENTINEL = "RAW_CAPTURE_SENTINEL_";
const text = (value = SENTINEL.repeat(2500)): AgentToolResult => ({ content: [{ type: "text", text: value }] });
const ctx = { runtime: "openclaw" as const, sessionKey: "s1", sessionId: "generation-1" };
function setup(options: { enabled?: boolean; seam?: boolean; version?: string; tools?: string[] } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "shunt-capture-"));
  mkdirSync(join(dir, "ws"));
  const handlers: AgentToolResultMiddleware[] = [];
  const registrations: unknown[] = [];
  const complete = vi.fn<(...args: any[]) => Promise<any>>(async () => { throw new Error("provider unavailable"); });
  const p = new ContextShuntPlugin({
    on() {}, registerTool() {},
    ...(options.seam === false ? {} : { registerAgentToolResultMiddleware(handler: AgentToolResultMiddleware, opts: unknown) {
      handlers.push(handler); registrations.push(opts);
    } }),
    runtime: { version: options.version ?? "2026.9.3", llm: { complete } },
    pluginConfig: { workspace_roots: [join(dir, "ws")], cache_dir: join(dir, "cache"),
      tool_result_capture: { enabled: options.enabled ?? true, read_only_tools: options.tools ?? ["mcp__logs__query"] } },
  });
  p.register();
  // The OpenClaw adapter intentionally does not register this host seam after the retirement
  // canary. Keep the capture engine coverage isolated behind an explicitly synthetic
  // capability so these tests cannot accidentally prove live host delivery.
  const proven = {
    ...p.capability,
    modes: p.capability.modes.map((m) =>
      m.mode === "tool_result_capture"
        ? { ...m, support: "supported" as const, reasons: [] }
        : m,
    ),
  };
  const engineSession = new ShuntSession("s1", p.config, proven, {
    provider: (p as any).provider(),
    legacyCompaction: true,
    store: (p as any).store,
    identity: new ScopeIdentity({
      host: "openclaw",
      profile: "context-shunt",
      principal: "local",
      session: "s1",
      generation: 1,
    }),
  });
  const acquireSession = vi.fn((context: typeof ctx) => {
    if (!context.sessionKey) throw new Error("Capture requires session identity");
    return engineSession;
  });
  const engineHandler: AgentToolResultMiddleware = (event, context) => captureToolResult(
    event,
    context,
    captureToolsFrom({ tool_result_capture: { read_only_tools: options.tools ?? ["mcp__logs__query"] } }),
    () => acquireSession(context as typeof ctx),
    p.config.limits,
  );
  const call = (result = text(), name = "mcp__logs__query", extra: Partial<ToolResultEvent> = {}) => {
    const event = { toolCallId: "tc1", toolName: name, args: {}, result, ...extra };
    return engineHandler(event, { ...ctx })?.result ?? result;
  };
  return { p, handlers, registrations, complete, call, engineHandler, engineSession, acquireSession };
}
function envelope(result: AgentToolResult) {
  const value = JSON.parse(result.content[0]!.text!);
  validateEnvelope(value);
  return value;
}

describe("exact tool-result identity classification", () => {
  it.each(["read", "web_fetch", "web_search", "read_mcp_resource"])("defaults %s to eligible", (name) => {
    expect(classifyToolResult(` ${name.toUpperCase()} `)).toBe("eligible");
  });
  it.each([
    "skills_list_extra", "context_shunt_read_fake", "mcp__x__read_resource_extra",
    "mcp__docs__read_resource", "unknown_tool", "rewrite", "mcp__x__get_prompt_extra",
    "mcp____list_resources", "mcp__bad-server__list_prompts",
  ])("gives %s neither protection nor default eligibility", (name) => {
    expect(classifyToolResult(name)).toBe("passthrough");
    expect(classifyToolResult(name, new Set([name.toUpperCase()]))).toBe("eligible");
    const f = setup({ tools: [] });
    const input = text("SKILL.md read_resource /skills/review\n".repeat(1000));
    expect(f.call(input, name)).toBe(input);
    expect(f.acquireSession).not.toHaveBeenCalled();
  });
  it.each(["skill_view", "skills_list", "ask_user", "clarify", "todo", "message",
    "sessions_send", "sessions_spawn", "sessions_history", "write_file", "delete_file",
    "spawn_agent", "mcp__db__update", "context_shunt_read", "context_shunt_inspect",
    "context_shunt_stats", "list_mcp_resources", "list_mcp_resource_templates",
    "mcp__a___b__list_resources", "mcp__a____b__list_prompts", "mcp__a___b__get_prompt",
  ])("protected precedence defeats configuration for %s", (name) => {
    expect(classifyToolResult(` ${name.toUpperCase()} `, new Set([name]))).toBe("protected");
  });
  it("treats prompt-delivery syntax as literal payload data", () => {
    const fixture = "single 'quote', double \"quote\", backtick `text`, literal $(touch /tmp/openclaw/openclaw-trust-classifier/SHOULD_NOT_EXIST), and semicolon ; end.";
    const f = setup({ tools: [] });
    const input = text(fixture.repeat(150));
    expect(f.call(input, "unknown_tool")).toBe(input);
    expect(f.acquireSession).not.toHaveBeenCalled();
    expect(envelope(f.call(input, "read_mcp_resource")).code).toBe("SPILLED");
  });
});

describe("capture engine (synthetic capability only)", () => {
  it.each([
    "skills_list", "skill_view", "ask_user", "clarify", "todo",
    "context_shunt_read", "context_shunt_inspect", "context_shunt_stats",
    "list_mcp_resources", "list_mcp_resource_templates",
    "mcp__docs__list_resources", "mcp__docs__list_prompts", "mcp__docs__get_prompt",
    "MCP__Docs___Team__List_Resources", "mcp__docs____team__get_prompt",
    "message", "sessions_spawn", "sessions_history", "write", "edit", "mcp__db__update",
  ])("protects %s before touching payload or session, even if configured", (name) => {
    const f = setup({ tools: [name] });
    const input = text(JSON.stringify({
      instructions: "Follow the host instructions. ".repeat(700),
      skills: [{ name: "review", description: "Review the current change" }],
      answer: "Keep capture retired", todos: [{ task: "Verify", status: "pending" }],
    }));
    expect(Buffer.byteLength(JSON.stringify(input))).toBeGreaterThan(16 * 1024);
    const session = vi.fn(() => { throw new Error("must not create session"); });
    const event = { toolCallId: "protected", toolName: ` ${name} `, args: {}, result: input };
    const register = vi.spyOn(f.engineSession.registry, "register");
    const account = vi.spyOn((f.p as any).store, "recordOperation");
    const before = JSON.stringify(input);
    expect(captureToolResult(event, ctx, captureToolsFrom({
      tool_result_capture: { read_only_tools: [name] },
    }), session, f.p.config.limits)).toBeUndefined();
    expect(f.call(input, name)).toBe(input);
    expect(JSON.stringify(input)).toBe(before);
    Object.defineProperty(event, "result", { get() { throw new Error("payload touched"); } });
    expect(captureToolResult(event, ctx, new Set([name]), session, f.p.config.limits)).toBeUndefined();
    expect(session).not.toHaveBeenCalled();
    expect(f.acquireSession).not.toHaveBeenCalled();
    expect(register).not.toHaveBeenCalled();
    expect(account).not.toHaveBeenCalled();
    expect(f.complete).not.toHaveBeenCalled();
  });
  it.each(["read_mcp_resource", "mcp__docs__read_resource"])("captures exact resource identity %s", (name) => {
    const f = setup({ tools: name.startsWith("mcp__") ? [name] : [] });
    expect(envelope(f.call(text(), name)).code).toBe("SPILLED");
  });
  it("does not register the unverified official seam even when enabled", () => {
    const f = setup(); f.p.register();
    expect(f.handlers).toHaveLength(0);
    expect(f.registrations).toHaveLength(0);
    expect(modeEnabled(f.p.capability, "tool_result_capture")).toBe(false);
    expect(f.p.capability.modes.find((m) => m.mode === "tool_result_capture")?.reasons)
      .toContain("ORDERING_UNPROVEN");
    const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
    expect(manifest.contracts.agentToolResultMiddleware).toEqual(["openclaw", "codex"]);
    expect(JSON.stringify(f.p.capability)).toContain("effective model-visible history");
  });
  it.each([{ enabled: false }, { seam: false }, { version: "2026.9.2" }, { version: "2026.9.4" }])("does not register without proven capability: %j", (options) => {
    const f = setup(options);
    expect(f.handlers).toHaveLength(0);
    expect(modeEnabled(f.p.capability, "tool_result_capture")).toBe(false);
  });
  it.each(["openclaw", "codex"] as const)("spills eligible text for %s with no model call", (runtime) => {
    const f = setup(); const original = text();
    const result = f.engineHandler!({ toolCallId: "tc", toolName: "mcp__logs__query", args: {}, result: original }, { ...ctx, runtime })!.result;
    const env = envelope(result);
    expect(env.code).toBe("SPILLED");
    expect(env.pointer.snapshot_id).toMatch(/^sha256:/);
    expect(env.coverage.upstream_truncated).toBe(true);
    expect(env.coverage.complete).toBe(false);
    expect(JSON.stringify(result)).not.toContain(SENTINEL);
    expect(Buffer.byteLength(JSON.stringify(result))).toBeLessThan(4096);
    expect(f.complete).not.toHaveBeenCalled();
    expect(original.content[0]!.text).toContain(SENTINEL);
  });
  it("serializes JSON details deterministically and resolves the immutable handle in the reader scope", () => {
    const f = setup();
    const a = envelope(f.call({ content: [{ type: "text", text: "query response" }], details: { rows: Array(5000).fill("row"), count: 5000 } }));
    const b = envelope(f.call({ details: { count: 5000, rows: Array(5000).fill("row") }, content: [{ text: "query response", type: "text" }] }));
    expect(a.pointer.snapshot_id).toBe(b.pointer.snapshot_id);
    const inspected = JSON.parse(f.p.onInspectTool({ ...a.pointer, selector: { kind: "bytes", start: 0, end: 100 } }, { sessionKey: "s1", sessionId: "rotated" }));
    expect(inspected.code).toBe("EXTRACTED");
    const other = JSON.parse(f.p.onInspectTool({ ...a.pointer, selector: { kind: "bytes", start: 0, end: 100 } }, { sessionKey: "other" }));
    expect(other.code).not.toBe("EXTRACTED");
  });
  it.each(["read_mcp_resource", "mcp__docs__read_resource"])("recovers %s through inspect/read with truthful accounting", async (name) => {
    const f = setup({ tools: name.startsWith("mcp__") ? [name] : [] });
    const captured = envelope(f.call(text("max_retries = 3\n" + "detail\n".repeat(3000)), name));
    const inspected = JSON.parse(f.p.onInspectTool({ ...captured.pointer,
      selector: { kind: "bytes", start: 0, end: 100 } }, ctx));
    expect(inspected.code).toBe("EXTRACTED");
    expect(JSON.stringify(inspected)).toContain("max_retries = 3");
    expect(f.complete).not.toHaveBeenCalled();
    f.complete.mockResolvedValue({ text: JSON.stringify({
      answer: "The retry ceiling is three [c1].",
      citations: [{ id: "c1", line_start: 1, line_end: 1, quote: "max_retries = 3" }],
    }), provider: "openai", model: "gpt-5.6-luna", usage: { inputTokens: 12, outputTokens: 8 } });
    const response = JSON.parse(await f.p.onReaderTool({
      question: "What is the retry ceiling?", handles: [{ source_id: captured.pointer.source_id, snapshot_id: captured.pointer.snapshot_id }],
    }, { sessionKey: "s1", toolCallId: "followup" }));
    expect(f.complete).toHaveBeenCalled();
    const options = f.complete.mock.calls[0]![0];
    expect(options.messages[0].content).toContain("What is the retry ceiling?");
    expect(options.messages[0].content).toContain("max_retries = 3");
    expect(options.model).toBe("gpt-5.6-luna");
    expect(options.execution.mode).toBe("isolated-agent-runtime");
    expect(response.code).toBe("ANSWERED");
    expect(response.provenance.derived).toBe(true);
    const records = JSON.parse(f.p.onStatsTool({}, ctx)).stats.records;
    const capturedRecord = records.find((row: any) => row.kind === "spill");
    expect(capturedRecord.delivery_boundary).toBe("pointer");
    expect(capturedRecord.baseline_credit_tokens).toBeGreaterThan(0);
    for (const output of [inspected, response]) {
      const row = records.find((row: any) => row.operation_id === output.accounting_id);
      expect(row.baseline_credit_tokens).toBe(0);
    }
    const readRecord = records.find((row: any) => row.operation_id === response.accounting_id);
    expect(readRecord.attempts_started).toBe(1);
    expect(readRecord.reader_input_tokens).toBe(12);
    expect(readRecord.reader_output_tokens).toBe(8);
  });
  it("uses labelled legacy compaction when reader availability is exhausted", async () => {
    const f = setup();
    const captured = envelope(f.call(text("error: query timed out\n" + "repeated log line\n".repeat(1500))));
    expect(f.complete).not.toHaveBeenCalled();
    const response = JSON.parse(await f.p.onReaderTool({ question: "Why did the query fail?",
      handles: [{ source_id: captured.pointer.source_id, snapshot_id: captured.pointer.snapshot_id }],
    }, { sessionKey: "s1", toolCallId: "read-unavailable" }));
    validateEnvelope(response);
    expect(response.code).toBe("LEGACY_COMPACTED");
    expect(response.result_kind).toBe("legacy_compaction");
    expect(response.provenance.derived).toBe(false);
    expect(response.provenance.attempts_started).toBeGreaterThan(0);
    expect(response.coverage.complete).toBe(false);
    expect(response.answer).toBe("");
    expect(response.citations).toEqual([]);
    expect(response.extraction).toBeUndefined();
    expect(Buffer.byteLength(JSON.stringify(response))).toBeLessThanOrEqual(16384);
  });
  it("leaves short text and JSON untouched", () => {
    const f = setup();
    for (const result of [text("short"), { ...text("ok"), details: { rows: [1, 2] } }]) expect(f.call(result)).toBe(result);
  });
  it.each([99_999, 100_000, 100_001])("refuses ambiguous text boundary %i without a snapshot or raw sentinel", (length) => {
    const result = setup().call(text(SENTINEL + "x".repeat(length - SENTINEL.length)));
    expect(envelope(result).code).toBe("HOST_UNSAFE");
    expect(envelope(result).sources).toEqual([]);
    expect(JSON.stringify(result)).not.toContain(SENTINEL);
  });
  it.each([
    { content: Array.from({ length: 200 }, () => ({ type: "text", text: SENTINEL })) },
    { ...text(), details: { truncated: true, originalSizeBytes: 200000 } },
    { ...text(), details: { data: "x".repeat(100000) } },
  ])("refuses host block/details ceilings %j", (input) => {
    const result = setup().call(input);
    expect(envelope(result).code).toBe("HOST_UNSAFE");
    expect(JSON.stringify(result)).not.toContain(SENTINEL);
    expect(envelope(result).pointer).toBeUndefined();
  });
  it.each(["image", "audio", "unknown"])("withholds unsupported %s blocks without persisting them", (type) => {
    const result = setup().call({ content: [...text().content, { type, data: SENTINEL }] });
    expect(envelope(result).code).toBe("BINARY_UNSUPPORTED");
    expect(JSON.stringify(result)).not.toContain(SENTINEL);
  });
  it.each(["message", "sessions_spawn", "mcp__db__update", "unknown_tool", "context_shunt_read"])("excludes control/mutating/unknown tool %s", (name) => {
    const f = setup({ tools: ["message", "sessions_spawn", "mcp__db__update"] });
    const result = { ...text(), details: { status: "accepted", messageId: "receipt", runId: "job" } };
    expect(f.call(result, name)).toBe(result);
  });
  it("preserves delivery receipts, accepted work, termination and side-effect accounting even on opted-in tools", () => {
    const f = setup();
    for (const details of [{ messageDelivery: { status: "settled" } }, { status: "accepted" }, { termination: true }, { sideEffects: { count: 1 } }]) {
      const input = { ...text(), details }; expect(f.call(input)).toBe(input);
    }
    const input = { ...text(), terminate: true }; expect(f.call(input)).toBe(input);
  });
  it("preserves tool failure facts when replacing oversized error text", () => {
    const f = setup();
    const output = f.call({ ...text(), details: { status: "error", exitCode: 7 }, isError: true }, "read", { isError: true });
    expect(envelope(output).code).toBe("SPILLED");
    expect(output.details).toMatchObject({ status: "error", exitCode: 7 });
    expect(output["isError"]).toBe(true);
    expect(JSON.stringify(output)).not.toContain(SENTINEL);
  });
  it.each([
    { status: "completed", success: false },
    { status: "completed", timedOut: true },
    { ok: true, error: SENTINEL.repeat(2000) },
    { status: "error", success: true },
  ])("preserves host terminal classifier inputs", (details) => {
    const output = setup().call({ ...text(), details });
    expect(output.details).toMatchObject({ ...details, ...(details.error ? { error: true } : {}) });
    expect(JSON.stringify(output)).not.toContain(SENTINEL);
  });
  it.each(["store", "middleware", "invalid-outcome", "serialization"])("never raw fail-opens after %s failure", (kind) => {
    const f = setup();
    const session = f.engineSession;
    let input = text();
    if (kind === "store") vi.spyOn(session.registry, "register").mockImplementation(() => { throw new Error(SENTINEL); });
    if (kind === "middleware") vi.spyOn(session, "postToolResult").mockImplementation(() => { throw new Error(SENTINEL); });
    if (kind === "invalid-outcome") vi.spyOn(session, "postToolResult").mockReturnValue({ action: "spill", bytesMeasured: 0 });
    if (kind === "serialization") { const cyclic: any = { raw: SENTINEL }; cyclic.self = cyclic; input = { ...input, details: cyclic }; }
    const output = f.call(input);
    expect(envelope(output).pointer).toBeUndefined();
    if (kind === "store") {
      const env = envelope(output);
      expect(env.code).toBe("LEGACY_COMPACTED");
      expect(env.status).toBe("partial");
      expect(env.coverage.complete).toBe(false);
      expect(env.legacy_compaction.summary).toContain(SENTINEL);
      expect(env.legacy_compaction.summary_bytes).toBeLessThanOrEqual(16384);
      expect(env.legacy_compaction.summary).not.toContain(SENTINEL.repeat(2000));
      expect(Buffer.byteLength(JSON.stringify(output))).toBeLessThan(32768);
    } else {
      expect(JSON.stringify(output)).not.toContain(SENTINEL);
      expect(Buffer.byteLength(JSON.stringify(output))).toBeLessThan(4096);
    }
  });
  it.each([null, {}, { content: null }, { content: [{ type: "text", text: 1 }] }])("fails closed on malformed payload", (input) => {
    const f = setup();
    try {
      const output = f.call(input as unknown as AgentToolResult);
      expect(envelope(output).sources).toEqual([]);
      expect(JSON.stringify(output)).not.toContain(SENTINEL);
    } catch (error) {
      // Malformed top-level values may deliberately throw to the verified host runner.
      expect(input).toBeNull();
    }
  });
  it("refuses missing session identity instead of publishing cross-session handles", () => {
    const f = setup();
    const output = f.engineHandler!({ toolCallId: "tc", toolName: "read", args: {}, result: text() }, { runtime: "codex" })!.result;
    expect(envelope(output).code).toBe("SPILL_FAILED");
    expect(JSON.stringify(output)).not.toContain(SENTINEL);
  });
});
