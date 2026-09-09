import { mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import Ajv from "ajv";
import { expect, it } from "vitest";
import { ContextShuntPlugin } from "../index.js";

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
