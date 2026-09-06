/** unit pre-read (TypeScript core) - the same fixture corpus as the Python core. */
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { PreReadGate, type ProbeResult, type ProbeSelection } from "../src/gate.js";
import { contractsDir, DEFAULT_LIMITS } from "../src/limits.js";
import { fileProber } from "../src/probe.js";
import { countLines, countLinesBounded, LineIndex } from "../src/textindex.js";

const conformance = (name: string) =>
  JSON.parse(readFileSync(join(contractsDir(), "conformance", name), "utf8"));

const gateCases = conformance("gate-cases.json");
const lineCases = conformance("line-count-cases.json");

function tableProber(table: Record<string, Record<string, unknown>>) {
  return (path: string, selection: ProbeSelection = { mode: "full" }): ProbeResult => {
    const entry = table[path];
    if (!entry || entry["missing"]) return { exists: false };
    const kind = (entry["kind"] as ProbeResult["kind"]) ?? "file";
    if (kind !== "file") return { exists: true, kind };
    const lines = (entry["lines"] as number) ?? 0;
    const bytes = (entry["bytes"] as number) ?? 0;
    const maxLine = (entry["max_line_bytes"] as number) ?? Math.ceil(bytes / Math.max(1, lines));
    if (selection.mode === "metadata") {
      return { exists: true, kind, lines: 0, bytes: 0, exact: true };
    }
    if (selection.mode === "lines" || selection.mode === "tail") {
      return {
        exists: true,
        kind,
        lines: Math.min(selection.limit, lines),
        bytes: Math.min(bytes, selection.limit * maxLine),
        exact: true,
      };
    }
    if (selection.mode === "search") {
      const matches = Math.min(selection.maxMatches, lines);
      return {
        exists: true,
        kind,
        lines: matches,
        bytes: matches * (maxLine + new TextEncoder().encode(path).length + 64),
        exact: true,
      };
    }
    return {
      exists: true,
      kind,
      lines,
      bytes,
      exact: true,
    };
  };
}

describe("line counting contract", () => {
  it("has a non-empty corpus", () => {
    expect(lineCases.cases.length).toBeGreaterThan(10);
  });

  for (const c of lineCases.cases) {
    it(`counts ${c.id}`, () => {
      const bytes = new TextEncoder().encode(c.content);
      expect(bytes.length).toBe(c.expected_bytes);
      expect(countLines(bytes)).toBe(c.expected_lines);
    });
  }

  it("clamps an inexact bounded count at the scan bound", () => {
    const data = new TextEncoder().encode("x\n".repeat(4000));
    const counted = countLinesBounded([data], { maxLines: 351, maxBytes: 1_000_000 });
    expect(counted.exact).toBe(false);
    expect(counted.lines).toBe(351);
  });

  it("keeps CR inside a CRLF line rather than normalizing it", () => {
    const index = new LineIndex(new TextEncoder().encode("a\r\nb\n"));
    expect(index.lineCount).toBe(2);
    expect(index.lineText(1)).toBe("a\r");
  });
});

describe("pre-read gate conformance", () => {
  const gate = new PreReadGate(tableProber(gateCases.probe_table));

  it("has a non-empty corpus covering all three outcomes", () => {
    expect(gateCases.cases.length).toBeGreaterThanOrEqual(60);
    const outcomes = new Set(gateCases.cases.map((c: any) => c.expect.decision));
    expect([...outcomes].sort()).toEqual(["allow", "blocked", "passthrough"]);
  });

  for (const c of gateCases.cases) {
    it(`decides ${c.id}`, () => {
      const decision = gate.evaluate(c.input.tool, c.input.args);
      const got: Record<string, unknown> = { decision: decision.decision, form: decision.form };
      if (decision.code) got["code"] = decision.code;
      const want: Record<string, unknown> = { decision: c.expect.decision, form: c.expect.form };
      if (c.expect.code) want["code"] = c.expect.code;
      expect(got).toEqual(want);
    });
  }
});

describe("real selected-output byte proofs", () => {
  it("blocks one oversized selected line for read, head, tail, and grep", () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-gate-"));
    const path = join(dir, "long.txt");
    writeFileSync(path, "A".repeat(20_000) + "\n");
    const gate = new PreReadGate(fileProber());
    for (const [tool, args] of [
      ["read", { file_path: path, offset: 1, limit: 1 }],
      ["shell", { command: `head -n 1 ${path}` }],
      ["shell", { command: `tail -n 1 ${path}` }],
      ["shell", { command: `grep -m 1 A ${path}` }],
    ] as const) {
      const decision = gate.evaluate(tool, args);
      expect(decision.decision).toBe("blocked");
      expect(decision.code).toBe("LARGE_READ");
    }
  });

  it("stops a full-file probe at the output threshold", () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-gate-"));
    const path = join(dir, "no-newlines.txt");
    writeFileSync(path, "A".repeat(1_000_000));
    const probe = fileProber()(path);
    expect(probe.exact).toBe(false);
    expect(probe.bytes).toBe(DEFAULT_LIMITS.maxTargetedReadBytes + 1);
  });

  it("blocks bounded-metadata output amplification before probing", () => {
    const files = Array.from({ length: 300 }, () => "/ws/a.txt");
    const gate = new PreReadGate(tableProber(gateCases.probe_table));
    const decision = gate.evaluate("shell", { command: `wc ${files.join(" ")}` });
    expect(decision).toMatchObject({
      decision: "blocked",
      code: "LARGE_READ",
      form: "bounded_metadata",
    });
  });
});
