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
  INSPECT_TOOL_NAME,
  READER_TOOL_NAME,
  READER_TOOL_PARAMETERS,
  STATS_TOOL_NAME,
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
    // Three read-only escape hatches, no writer.
    expect(registered.tools).toEqual([
      "context_shunt_read",
      "context_shunt_inspect",
      "context_shunt_stats",
    ]);
    expect(registered.tools.some((t) => /write|patch|edit/i.test(t))).toBe(false);
    expect(logs.join("\n")).toContain("capability report");
  });

  it("does not register the reader when the isolated model bridge is absent", () => {
    const dir = workspace();
    const { api, registered } = configured(dir);
    delete api.runtime.llm;
    new ContextShuntPlugin(api).register();
    expect(registered.hooks).toContain("before_tool_call");
    // The reader needs a model; inspect and stats do not, so they stay usable.
    expect(registered.tools).toEqual(["context_shunt_inspect", "context_shunt_stats"]);
  });

  it("refuses to load with writer.enabled and reports the refusal", () => {
    const dir = workspace();
    const { api, logs } = configured(dir, { writer: { enabled: true } });
    expect(() => plugin.register(api)).toThrow();
    expect(logs.join("\n")).toContain("refused to load");
  });

  it("loads a configured reader model and reports it as requested", async () => {
    // 1.1 makes the reader model configurable; the envelope keeps it honest.
    const dir = workspace();
    const { api } = configured(dir, { reader: { model: "gpt-5.6-sol" } });
    const shunt = new ContextShuntPlugin(api);
    expect(shunt.config.readerModel).toBe("gpt-5.6-sol");
    expect(shunt.capabilityJson()["reader_model"]).toBe("gpt-5.6-sol");
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\n");
    const out = JSON.parse(
      await shunt.onReaderTool({ question: "What is it?", paths: [path] }, {}),
    );
    expect(out.provenance.requested_model).toBe("gpt-5.6-sol");
  });

  it("refuses an unknown attribution policy", () => {
    const dir = workspace();
    const { api } = configured(dir, { reader: { attribution_policy: "trust_me" } });
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
    // Refused outright rather than published under the requested model's name.
    expect(out.status).toBe("error");
    expect(out.code).toBe("MODEL_ERROR");
    expect(out.answer).toBe("");
    expect(out.provenance.attribution_status).toBe("mismatch");
    expect(out.provenance.resolved_model).toBe("gpt-5.6-sol");
    // A model failure is not a handle failure.
    expect(out.recovery.handles_valid).toBe(true);
  });

  it("reports the host's own selection as resolved, never as a provider confirmation", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\n");
    const { api } = configured(dir);
    const out = JSON.parse(
      await new ContextShuntPlugin(api).onReaderTool(
        { question: "What is the retry ceiling?", paths: [path] },
        {},
      ),
    );
    expect(out.provenance.derived).toBe(true);
    // The isolated runtime exposes its post-policy selection: a routing fact, not proof
    // that those tokens were generated by that model.
    expect(out.provenance.attribution_status).toBe("resolved");
    expect(out.provenance.reported_model).toBeNull();
  });

  it("does not project absent usage as zero", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\n");
    const { api } = configured(dir);
    const previous = api.runtime.llm.complete;
    api.runtime.llm.complete = async (opts: never) => {
      const result = await previous(opts);
      // "CLI runtimes may not report token usage" - the host's own comment.
      delete result.usage;
      return result;
    };
    const out = JSON.parse(
      await new ContextShuntPlugin(api).onReaderTool(
        { question: "What is the retry ceiling?", paths: [path] },
        {},
      ),
    );
    expect(out.provenance.usage_complete).toBe(false);
  });

  it("declares exactly one source form and no write surface", () => {
    expect(Object.keys(READER_TOOL_PARAMETERS.properties).sort())
      .toEqual(["handles", "paths", "question"]);
    const blob = JSON.stringify(READER_TOOL_PARAMETERS).toLowerCase();
    for (const forbidden of ["write", "patch", "apply", "content", "raw"]) {
      expect(blob).not.toContain(forbidden);
    }
  });

  it("registers a tool object whose execute returns a text content block", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\n");
    const { api, registered, handlers } = configured(dir);
    new ContextShuntPlugin(api).register();
    expect(registered.tools).toContain(READER_TOOL_NAME);
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
    // OpenClaw rejects a runtime registration that the manifest does not declare, so the
    // two lists have to agree exactly.
    expect(manifest.contracts.tools)
      .toEqual([READER_TOOL_NAME, INSPECT_TOOL_NAME, STATS_TOOL_NAME]);
    const { api, registered } = configured(workspace());
    new ContextShuntPlugin(api).register();
    expect(registered.tools).toEqual(manifest.contracts.tools);
  });

  it("declares the 1.1 configuration surface in the manifest schema", () => {
    const manifest = JSON.parse(
      readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"),
    );
    const reader = manifest.configSchema.properties.reader.properties;
    // The model is configurable from 1.1; provenance, not a const, keeps it honest.
    expect(reader.model.const).toBeUndefined();
    expect(reader.model.default).toBe("gpt-5.6-luna");
    expect(reader.attribution_policy.enum).toEqual(["allow_unverified", "require_match"]);
    expect(manifest.configSchema.properties.inspect).toBeDefined();
    expect(manifest.configSchema.properties.stats).toBeDefined();
    // A 1.0 configuration keeps working.
    expect(manifest.configSchema.properties.spill_dir).toBeDefined();
  });
});

describe("session lifecycle follows the host's own reason enum", () => {
  function readerPlugin(dir: string) {
    const { api } = configured(dir);
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\nbackoff = fixed\n");
    return { shunt: new ContextShuntPlugin(api), path };
  }

  it("keeps handles across a compaction, which rotates the id mid-conversation", async () => {
    const dir = workspace();
    const { shunt, path } = readerPlugin(dir);
    const ctx = { sessionKey: "agent:main", sessionId: "s1" };
    const first = JSON.parse(
      await shunt.onReaderTool({ question: "What is it?", paths: [path] }, ctx),
    );
    const handle = first.sources[0];

    // Compaction ends "the session" while the conversation carries on.
    shunt.onSessionEnd({ sessionKey: "agent:main", sessionId: "s1", reason: "compaction" });

    const second = JSON.parse(
      await shunt.onReaderTool(
        {
          question: "And the backoff?",
          handles: [{ source_id: handle.source_id, snapshot_id: handle.snapshot_id }],
        },
        { sessionKey: "agent:main", sessionId: "s2" },
      ),
    );
    expect(["ANSWERED", "NO_MATCH"]).toContain(second.code);
    expect(second.sources[0].snapshot_id).toBe(handle.snapshot_id);
  });

  it("revokes handles when the user actually clears the conversation", async () => {
    const dir = workspace();
    const { shunt, path } = readerPlugin(dir);
    const ctx = { sessionKey: "agent:main", sessionId: "s1" };
    const first = JSON.parse(
      await shunt.onReaderTool({ question: "What is it?", paths: [path] }, ctx),
    );
    const handle = first.sources[0];

    shunt.onSessionEnd({ sessionKey: "agent:main", sessionId: "s1", reason: "reset" });

    const replayed = JSON.parse(
      await shunt.onReaderTool(
        {
          question: "What is it?",
          handles: [{ source_id: handle.source_id, snapshot_id: handle.snapshot_id }],
        },
        ctx,
      ),
    );
    expect(replayed.code).toBe("SOURCE_EXPIRED");
  });
});

describe("the deterministic inspector", () => {
  it("returns exact bytes, makes no model call and reports its disclosure", async () => {
    const dir = workspace();
    const { api } = configured(dir);
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\nbeta\ngamma\n");
    const shunt = new ContextShuntPlugin(api);
    let calls = 0;
    const previous = api.runtime.llm.complete;
    api.runtime.llm.complete = async (opts: never) => {
      calls += 1;
      return previous(opts);
    };
    const captured = JSON.parse(
      await shunt.onReaderTool({ question: "What is here?", paths: [path] }, {}),
    );
    const before = calls;
    const handle = captured.sources[0];
    const out = JSON.parse(
      shunt.onInspectTool(
        {
          source_id: handle.source_id,
          snapshot_id: handle.snapshot_id,
          selector: { kind: "lines", start: 1, end: 2 },
        },
        {},
      ),
    );
    expect(out.code).toBe("EXTRACTED");
    expect(out.result_kind).toBe("deterministic_extraction");
    expect(out.provenance.derived).toBe(false);
    expect(out.extraction.segments[0].text).toBe("max_retries = 3\nbeta");
    expect(calls).toBe(before);
  });

  it("reports this session's accounting without revealing content", async () => {
    const dir = workspace();
    const { api } = configured(dir);
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\nSECRET-MARKER-9911\n");
    const shunt = new ContextShuntPlugin(api);
    await shunt.onReaderTool({ question: "What is here?", paths: [path] }, {});
    const out = JSON.parse(shunt.onStatsTool({}, {}));
    expect(out.code).toBe("STATS");
    expect(out.stats.scope).toBe("session");
    expect(out.stats.records.length).toBeGreaterThan(0);
    const blob = JSON.stringify(out);
    expect(blob).not.toContain("SECRET-MARKER-9911");
    expect(blob).not.toContain(dir);
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

// -- the documented fallback chain is actually wired ------------------------

describe("availability fallback chain", () => {
  /**
   * `reader.fallback_chain` parsed, validated and documented - and did nothing. Both
   * adapters built a bare `HostBridgeProvider`, so a deployment that configured an
   * availability fallback silently had none: the first unavailable provider ended the
   * request.
   */
  it("advances to the configured fallback when the primary is unavailable", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\nbackoff = fixed\n");

    const attempted: string[] = [];
    const f = fakeApi();
    f.api.pluginConfig = {
      workspace_roots: [join(dir, "ws")],
      spill_dir: join(dir, "cache"),
      reader: { model: READER_MODEL, fallback_chain: [{ provider: "openai", model: "gpt-5.6-sol" }] },
    };
    f.api.runtime.llm.complete = async (opts: any) => {
      attempted.push(opts.model);
      if (opts.model.endsWith(READER_MODEL)) {
        // A retryable availability failure, which is the only fallback trigger.
        throw new Error("upstream unavailable");
      }
      return {
        text: JSON.stringify({
          answer: "The retry ceiling is three [c1].",
          citations: [{ id: "c1", line_start: 1, line_end: 1, quote: "max_retries = 3" }],
        }),
        provider: "openai",
        model: "gpt-5.6-sol",
        usage: { inputTokens: 12, outputTokens: 8 },
      };
    };

    const out = JSON.parse(
      await new ContextShuntPlugin(f.api).onReaderTool(
        { question: "What is the retry ceiling?", paths: [path] },
        { sessionKey: "s1" },
      ),
    );

    expect(attempted).toHaveLength(2);
    expect(attempted[0]).toContain(READER_MODEL);
    expect(attempted[1]).toContain("gpt-5.6-sol");
    expect(out.code).toBe("ANSWERED");
    expect(out.provenance.fallback_used).toBe(true);
    // Both attempts reached a provider and were billed, so both are accounted for.
    expect(out.provenance.attempts_started).toBe(2);
  });

  it("uses no chain when none is configured", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\nbackoff = fixed\n");
    const { api, modelCalls } = configured(dir);
    const out = JSON.parse(
      await new ContextShuntPlugin(api).onReaderTool(
        { question: "What is the retry ceiling?", paths: [path] },
        { sessionKey: "s1" },
      ),
    );
    expect(out.code).toBe("ANSWERED");
    expect(modelCalls).toHaveLength(1);
    expect(out.provenance.fallback_used).toBe(false);
  });
});

describe("fallback routes to the candidate's own provider", () => {
  /**
   * The bridge received each candidate's provider and ignored it, building every route
   * from `config.readerProvider`. A fallback onto a *different* provider was therefore
   * sent to the primary's provider under the fallback's model name. The existing test
   * used one provider for both entries, so it could not see this.
   */
  it("uses the fallback entry's provider, not the primary's", async () => {
    const dir = workspace();
    const path = join(dir, "ws", "conf.txt");
    writeFileSync(path, "max_retries = 3\nbackoff = fixed\n");

    const attempted: string[] = [];
    const f = fakeApi();
    f.api.pluginConfig = {
      workspace_roots: [join(dir, "ws")],
      spill_dir: join(dir, "cache"),
      reader: {
        model: READER_MODEL,
        provider: "openai",
        fallback_chain: [{ provider: "anthropic", model: "claude-reader" }],
      },
    };
    f.api.runtime.llm.complete = async (opts: any) => {
      attempted.push(opts.model);
      if (opts.model.startsWith("openai/")) throw new Error("upstream unavailable");
      return {
        text: JSON.stringify({
          answer: "The retry ceiling is three [c1].",
          citations: [{ id: "c1", line_start: 1, line_end: 1, quote: "max_retries = 3" }],
        }),
        provider: "anthropic",
        model: "claude-reader",
        usage: { inputTokens: 12, outputTokens: 8 },
      };
    };

    const out = JSON.parse(
      await new ContextShuntPlugin(f.api).onReaderTool(
        { question: "What is the retry ceiling?", paths: [path] },
        { sessionKey: "s1" },
      ),
    );

    expect(attempted).toEqual([`openai/${READER_MODEL}`, "anthropic/claude-reader"]);
    expect(out.provenance.fallback_used).toBe(true);
  });
});
