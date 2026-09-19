import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { type Chunk, planChunks, renderExcerpt } from "../src/chunking.js";
import { Reader } from "../src/reader.js";
import { snapshotBytes } from "../src/snapshot.js";
import { claimsJson, makeRegistry, FakeLuna } from "./support.js";

const enc = (text: string) => new TextEncoder().encode(text);

function lineChunk(text: string, start: number, end: number): Chunk {
  return {
    sourceId: "src",
    snapshotId: "snap",
    locator: { kind: "lines", start, end },
    text,
    bytesLen: enc(text).length,
    estTokens: 1,
  };
}

describe("line excerpt gutters", () => {
  it("matches the line contract and preserves records unchanged", () => {
    expect(renderExcerpt(lineChunk("alpha\nbeta\ngamma", 10, 12))).toBe(
      "10: alpha\n11: beta\n12: gamma",
    );
    expect(renderExcerpt(lineChunk("before\rafter\u2028next\u0085last", 5, 5))).toBe(
      "5: before\rafter\u2028next\u0085last",
    );
    const records: Chunk = {
      ...lineChunk('{"a":1}\n{"a":2}', 1, 2),
      locator: { kind: "records", pointer: "", start: 1, end: 2 },
    };
    expect(renderExcerpt(records)).toBe(records.text);
  });

  it("represents a final selected blank line from the real LineIndex range", () => {
    const snapshot = snapshotBytes(enc("alpha\n\n"));
    const plan = planChunks(
      [{ sourceId: "src", snapshot, selector: { kind: "lines", start: 1, end: 2 } }],
      { maxChunks: 1, question: "What is present?" },
    );
    expect(plan.chunks[0]!.text).toBe("alpha\n");
    expect(renderExcerpt(plan.chunks[0]!)).toBe("1: alpha\n2: ");
  });
});

function request(entry: { sourceId: string; snapshot: { snapshotId: string } }) {
  return {
    schema_version: "1.0",
    request_id: "req_gutters",
    operation: "read",
    question: "What is the threshold?",
    sources: [{
      source_id: entry.sourceId,
      snapshot_id: entry.snapshot.snapshotId,
      selector: { kind: "lines", start: 1201, end: 1202 },
    }],
    budgets: { max_chunks: 1, max_answer_bytes: 8192, deadline_ms: 60000 },
  };
}

function fixture() {
  const content = [...Array(1200).fill("filler"), "", " threshold_0 = 100"].join("\n");
  const registry = makeRegistry(mkdtempSync(join(tmpdir(), "shunt-gutters-")), { sessionId: "sess" });
  const entry = registry.register("sess", snapshotBytes(enc(content)));
  return { content, entry, registry };
}

describe("reader gutter integration and strict verification", () => {
  it("sends authoritative gutters through the actual Reader path", async () => {
    const { content, entry, registry } = fixture();
    const luna = new FakeLuna([], (user) => {
      expect(user).toContain("1201: \n1202:  threshold_0 = 100");
      return claimsJson(
        [{ text: "threshold_0 is 100.", citation_ids: ["c1"] }],
        [{ id: "c1", line_start: 1202, line_end: 1202, quote: " threshold_0 = 100" }],
      );
    });
    const env = await new Reader(registry, luna).answer("sess", request(entry));
    expect(env.code).toBe("ANSWERED");
    expect(env.citations[0]!.locator).toEqual({ kind: "lines", start: 1202, end: 1202 });
  });

  it("keeps the strict verifier closed for a wrong line despite a present gutter", async () => {
    const { content, entry, registry } = fixture();
    const reply = claimsJson(
      [{ text: "threshold_0 is 100.", citation_ids: ["c1"] }],
      [{ id: "c1", line_start: 1201, line_end: 1201, quote: " threshold_0 = 100" }],
    );
    const env = await new Reader(registry, new FakeLuna([], reply)).answer("sess", request(entry));
    expect(env.code).toBe("CITATION_INVALID");
    expect(env.answer).toBe("");
  });

  it("rejects fabricated gutter text as a quote", async () => {
    const { content, entry, registry } = fixture();
    const reply = claimsJson(
      [{ text: "threshold_0 is 100.", citation_ids: ["c1"] }],
      [{ id: "c1", line_start: 1202, line_end: 1202, quote: "1202:  threshold_0 = 100" }],
    );
    const env = await new Reader(registry, new FakeLuna([], reply)).answer("sess", request(entry));
    expect(env.code).toBe("CITATION_INVALID");
    expect(env.answer).toBe("");
  });
});
