import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import Ajv from "ajv";
import { expect, it } from "vitest";
import { ContextShuntPlugin } from "../index.js";
import { captureToolsFrom } from "../src/capture.js";

it("loads the documented config through the actual adapter and host manifest schema", () => {
  const example = JSON.parse(readFileSync(new URL("../../../examples/config/openclaw.json", import.meta.url), "utf8"));
  const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
  const raw = example.plugins.entries["context-shunt"].config;
  const validate = new Ajv({ strict: false }).compile(manifest.configSchema);
  expect(validate(raw)).toBe(true);
  const dir = mkdtempSync(join(tmpdir(), "shunt-config-"));
  mkdirSync(join(dir, "ws"));
  const p = new ContextShuntPlugin({ on() {}, pluginConfig: {
    ...raw, workspace_roots: [join(dir, "ws")], cache_dir: join(dir, "cache"),
  } });
  expect(p.config.toolResultCaptureEnabled).toBe(false);
  expect(p.config.readerModel).toBe("gpt-5.6-luna");
  expect(validate({ ...raw, tool_result_capture: { enabled: true, read_only_tools: ["mcp__logs__query"] } })).toBe(true);
  expect(validate({ ...raw, tool_result_capture: { enabled: true, priority: 100 } })).toBe(false);
});

it("canonicalizes a differently-cased workspace alias before capturing the real source", async () => {
  const dir = mkdtempSync(join(tmpdir(), "shunt-config-case-"));
  const realRoot = join(dir, "WorkspaceRoot");
  const aliasParent = join(dir, "aliases");
  const aliasRoot = join(aliasParent, "workspaceroot");
  mkdirSync(realRoot);
  mkdirSync(aliasParent);
  symlinkSync(realRoot, aliasRoot, "dir");
  const source = join(realRoot, "settings.txt");
  writeFileSync(source, "max_retries = 3\n");

  const p = new ContextShuntPlugin({
    on() {},
    pluginConfig: {
      workspace_roots: [aliasRoot],
      cache_dir: join(dir, "cache"),
      reader: { model: "gpt-5.6-luna" },
    },
    runtime: {
      version: "2026.9.3",
      llm: {
        async complete() {
          return {
            text: JSON.stringify({
              answer: "The retry ceiling is three [c1].",
              citations: [{ id: "c1", line_start: 1, line_end: 1, quote: "max_retries = 3" }],
            }),
            provider: "openai",
            model: "gpt-5.6-luna",
          };
        },
      },
    },
  });
  expect(p.config.workspaceRoots[0]).toBe(realpathSync(realRoot));

  // A case-insensitive macOS volume may expose a differently-cased path. Exercise that form
  // only when realpath agrees on the canonical spelling; some volumes report the alias as a
  // distinct spelling even though lookup succeeds, which the core must continue to reject.
  const caseVariant = join(dir, "WORKSPACEROOT", "settings.txt");
  const explicitPath = source;
  const out = JSON.parse(await p.onReaderTool(
    { question: "What is the retry ceiling?", paths: [explicitPath] },
    { sessionKey: "case" },
  ));
  expect(out.code).toBe("ANSWERED");
  expect(out.citations[0].verified).toBe(true);
  if (existsSync(caseVariant) && realpathSync(caseVariant) === realpathSync(source)) {
    const variant = JSON.parse(await p.onReaderTool(
      { question: "What is the retry ceiling?", paths: [caseVariant] },
      { sessionKey: "case-variant" },
    ));
    expect(variant.code).toBe("ANSWERED");
  }
});

it("preserves exact configured MCP tool IDs while stripping only the adapter migration field", () => {
  const raw: Record<string, unknown> = {
    tool_result_capture: {
      enabled: true,
      read_only_tools: ["mcp__logs__query", "MCP__Logs__Query"],
    },
  };
  const tools = captureToolsFrom(raw);
  expect(tools).toContain("mcp__logs__query");
  expect(tools).toContain("MCP__Logs__Query");
  expect(raw.tool_result_capture).toEqual({ enabled: true });
});
