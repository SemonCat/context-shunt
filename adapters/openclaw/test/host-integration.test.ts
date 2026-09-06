/**
 * integration openclaw --mode local: the real host checkout, or nothing.
 *
 * Two things are verified against the installed host rather than against a stand-in:
 *
 * 1. The hooks the adapter depends on exist in the host's own typed-hook catalogue, and
 *    `after_tool_call` is still documented as observe-only.
 * 2. The ordering evidence behind the disabled Suma post-tool mode still holds in the
 *    host source: the persistence cap runs *before* the plugin's persist hook. If a host
 *    upgrade changes that, this gate fails and the capability decision has to be redone -
 *    which is exactly what "re-run the ordering evidence on upgrade" means.
 *
 * What this is *not*: a live runtime sentinel measurement of capture/truncation/persistence
 * order inside a running gateway. That needs a running host and is listed as future work
 * in docs/capability-matrix.md. The mode stays disabled either way, so nothing here can
 * turn an unsupported mode into a supported one.
 *
 *     CONTEXT_SHUNT_OPENCLAW_ROOT=/path/to/openclaw \
 *     scripts/verify integration openclaw --mode local
 *
 * Without that, `scripts/verify` reports NOT_RUN (exit 2), never a pass.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { modeEnabled } from "@context-shunt/core";

import { ContextShuntPlugin } from "../index.js";
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
    expect(String(pkg.version)).toMatch(/^\d{4}\.\d+/);
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

  it("still caps the tool result before invoking the plugin persist hook", () => {
    // This is the evidence behind CAPTURE_AFTER_TRUNCATION. If the order flips, the gate
    // fails and the capability decision must be re-derived rather than inherited.
    const guard = hostFile("src/agents/session-tool-result-guard.ts");
    const capIndex = guard.indexOf("const capped = capToolResultForPersistence(");
    const persistIndex = guard.indexOf("const transformed = persistToolResult(capped,");
    expect(capIndex).toBeGreaterThan(-1);
    expect(persistIndex).toBeGreaterThan(capIndex);
  });

  it("keeps the Suma post-tool mode disabled for this host version", () => {
    const pkg = JSON.parse(hostFile("package.json"));
    const report = buildCapabilityReport({
      hooks: ["before_tool_call", "after_tool_call", "tool_result_persist"],
      hasModelBridge: true,
      hostVersion: String(pkg.version),
    });
    expect(report.hostVersion).toBe(String(pkg.version));
    expect(modeEnabled(report, "suma_post_tool")).toBe(false);
    expect(modeEnabled(report, "local_gate")).toBe(true);
    expect(modeEnabled(report, "reader")).toBe(true);
  });

  it("loads against the host version and registers only the read-only surface", () => {
    const pkg = JSON.parse(hostFile("package.json"));
    const hooks: string[] = [];
    const tools: string[] = [];
    const api: any = {
      pluginConfig: { workspace_roots: [ROOT] },
      hostVersion: String(pkg.version),
      availableHooks: ["before_tool_call", "after_tool_call", "tool_result_persist", "session_end"],
      on: (hook: string) => hooks.push(hook),
      registerTool: (def: Record<string, unknown>) => tools.push(String(def["name"])),
      logger: { info: () => {} },
      llm: { complete: async () => ({ text: "{}", model: "gpt-5.6-luna" }) },
    };
    new ContextShuntPlugin(api).register();
    expect(hooks).toContain("before_tool_call");
    expect(tools).toEqual(["context_shunt_read"]);
    expect(hooks).not.toContain("tool_result_persist");
  });
});

describe.skipIf(available)("openclaw host integration prerequisites", () => {
  it("reports the missing prerequisite rather than passing", () => {
    expect(ROOT).toBe("");
  });
});
