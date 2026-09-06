/** unit pre-read (TypeScript core) - the same fixture corpus as the Python core. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { PreReadGate, type ProbeResult } from "../src/gate.js";
import { contractsDir } from "../src/limits.js";
import { countLines, countLinesBounded, LineIndex } from "../src/textindex.js";

const conformance = (name: string) =>
  JSON.parse(readFileSync(join(contractsDir(), "conformance", name), "utf8"));

const gateCases = conformance("gate-cases.json");
const lineCases = conformance("line-count-cases.json");

function tableProber(table: Record<string, Record<string, unknown>>) {
  return (path: string): ProbeResult => {
    const entry = table[path];
    if (!entry || entry["missing"]) return { exists: false };
    return {
      exists: true,
      kind: (entry["kind"] as ProbeResult["kind"]) ?? "file",
      lines: (entry["lines"] as number) ?? 0,
      bytes: (entry["bytes"] as number) ?? 0,
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
