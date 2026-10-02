/**
 * The registered OpenClaw `context_shunt_inspect` entrance defaults bytes `align` to char.
 *
 * The core request contract keeps `strict` as its default; only the registered tool fills in
 * `char` when a `bytes` selector omits `align`. Every case drives the `execute` the plugin
 * actually registered. Fixture and expectations are identical to
 * `packages/core-py/tests/test_gate_hermes_inspect_default_char.py`, so the two adapters
 * cannot drift.
 */
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  ContextShuntPlugin,
  INSPECT_TOOL_DESCRIPTION,
  INSPECT_TOOL_NAME,
  INSPECT_TOOL_PARAMETERS,
  READER_TOOL_NAME,
} from "../index.js";

// a(0) é(1,2) 日(3..5) 🎯(6..9) ñ(10,11) x(12) LF(13).
const TEXT = "aé日🎯ñx\n";
const RAW = Buffer.from(TEXT, "utf8");
const FULL = Buffer.concat([RAW, RAW, RAW]);

const MISALIGNED: Array<[[number, number], [number, number, string]]> = [
  [[2, 5], [1, 3, "é"]],
  [[4, 8], [3, 6, "日"]],
  [[7, 11], [6, 10, "🎯"]],
  [[11, 13], [10, 13, "ñx"]],
  [[2, 9], [1, 6, "é日"]],
];

type Execute = (id: string, params: unknown) => unknown;

async function entrance() {
  const dir = mkdtempSync(join(tmpdir(), "shunt-oc-char-"));
  mkdirSync(join(dir, "ws"), { recursive: true });
  const path = join(dir, "ws", "mixed.txt");
  writeFileSync(path, FULL);
  const executes = new Map<string, Execute>();
  const api: any = {
    pluginConfig: { workspace_roots: [join(dir, "ws")], spill_dir: join(dir, "cache") },
    on() {},
    registerTool(factory: (ctx: Record<string, unknown>) => Record<string, unknown>) {
      const tool = factory({ sessionKey: "s1", sessionId: "sid1" });
      executes.set(String(tool["name"]), tool["execute"] as Execute);
    },
    logger: { info() {}, warn() {} },
    runtime: {
      version: "2026.9.2",
      llm: {
        async complete() {
          return {
            text: JSON.stringify({ answer: "Mixed text [c1].", citations: [] }),
            provider: "openai",
            model: "gpt-5.6-luna",
            usage: { inputTokens: 1, outputTokens: 1 },
          };
        },
      },
    },
  };
  new ContextShuntPlugin(api).register();
  const read = (await executes.get(READER_TOOL_NAME)!("r1", {
    question: "What is here?",
    paths: [path],
  })) as { content: Array<{ text: string }> };
  const handle = JSON.parse(read.content[0]!.text).sources[0];
  let n = 0;
  const inspect = (selector: unknown, extra: Record<string, unknown> = {}) => {
    const params = { source_id: handle.source_id, snapshot_id: handle.snapshot_id, selector, ...extra };
    const out = executes.get(INSPECT_TOOL_NAME)!(`i${(n += 1)}`, params) as {
      content: Array<{ text: string }>;
    };
    return JSON.parse(out.content[0]!.text);
  };
  return { inspect, handle, execute: executes.get(INSPECT_TOOL_NAME)! };
}

const bytes = (start: number, end: number, extra: Record<string, unknown> = {}) => ({
  kind: "bytes",
  start,
  end,
  ...extra,
});

const text = (out: any) => out.extraction.segments.map((s: any) => s.text).join("");

describe("inspect tool entrance: bytes align defaults to char", () => {
  it.each(MISALIGNED)("floors omitted-align %j at the registered execute", async (req, exp) => {
    const { inspect } = await entrance();
    const out = inspect(bytes(...req));
    expect(out.code, out.failure_detail).toBe("EXTRACTED");
    expect(out.extraction.byte_range).toEqual({ start: exp[0], end: exp[1] });
    expect(text(out)).toBe(exp[2]);
    expect(out.extraction.complete).toBe(true);
  });

  it.each(MISALIGNED)("treats omitted and explicit char %j as the same selector", async (req) => {
    const { inspect } = await entrance();
    const omitted = inspect(bytes(...req));
    const explicit = inspect(bytes(...req, { align: "char" }));
    for (const env of [omitted, explicit]) {
      delete env.request_id;
      delete env.accounting_id;
    }
    for (const key of ["disclosed_bytes_source", "disclosed_bytes_session"]) {
      expect(explicit.extraction[key] - omitted.extraction[key]).toBe(omitted.extraction.result_bytes);
      delete explicit.extraction[key];
      delete omitted.extraction[key];
    }
    expect(omitted).toEqual(explicit);
  });

  it("keeps explicit strict exact: refused without charge, aligned offsets unreported", async () => {
    const { inspect } = await entrance();
    const refused = inspect(bytes(2, 9, { align: "strict" }));
    expect(refused.code).toBe("INVALID_REQUEST");
    expect(refused.failure_detail).toBe("UTF8_RANGE_BOUNDARY");
    expect(refused.extraction).toBeUndefined();
    const exact = inspect(bytes(1, 6, { align: "strict" }));
    expect(exact.code).toBe("EXTRACTED");
    expect(text(exact)).toBe("é日");
    expect(exact.extraction.byte_range).toBeUndefined();
    expect(inspect(bytes(2, 9, { align: "strict" })).failure_detail).toBe("UTF8_RANGE_BOUNDARY");
    const again = inspect(bytes(1, 6, { align: "strict" }));
    expect(again.extraction.disclosed_bytes_source).toBe(exact.extraction.disclosed_bytes_source + 5);
  });

  it.each(["CHAR", "Strict", "", null, 1, true, "auto"])(
    "rejects invalid explicit align %j instead of repairing it",
    async (align) => {
      const { inspect } = await entrance();
      const out = inspect(bytes(2, 9, { align }));
      expect(out.code).toBe("INVALID_REQUEST");
      expect(out.failure_detail).toBe("TOOL_ARGS_VIOLATION");
      expect(out.extraction).toBeUndefined();
    },
  );

  it("leaves non-bytes selectors alone and still rejects align on them", async () => {
    const { inspect } = await entrance();
    const lines = inspect({ kind: "lines", start: 1, end: 1 });
    expect(lines.code).toBe("EXTRACTED");
    expect(lines.extraction.byte_range).toBeUndefined();
    const search = inspect({ kind: "search", needle: "日", max_matches: 1 });
    expect(search.code).toBe("EXTRACTED");
    expect(search.extraction.byte_range).toBeUndefined();
    for (const selector of [
      { kind: "lines", start: 1, end: 1, align: "char" },
      { kind: "search", needle: "日", max_matches: 1, align: "char" },
      { kind: "aggregate", records_pointer: "", align: "char" },
    ]) {
      const out = inspect(selector);
      expect(out.code).toBe("INVALID_REQUEST");
      expect(out.failure_detail).toBe("TOOL_ARGS_VIOLATION");
    }
  });

  it("pages omitted align and continues with omitted or explicit char", async () => {
    const { inspect } = await entrance();
    const start = 2;
    const end = FULL.length - 1;
    const collected: Buffer[] = [];
    let cursor: string | undefined;
    let chargedBefore: number | undefined;
    let total = 0;
    let done = false;
    for (let page = 0; page < 64 && !done; page += 1) {
      const selector = page % 2 === 0 ? bytes(start, end) : bytes(start, end, { align: "char" });
      const out = inspect(selector, { max_result_bytes: 4, ...(cursor ? { cursor } : {}) });
      expect(out.code, out.failure_detail).toBe("EXTRACTED");
      const ex = out.extraction;
      expect(ex.result_bytes).toBeLessThanOrEqual(4);
      expect(ex.byte_range).toEqual({ start: 1, end });
      chargedBefore ??= ex.disclosed_bytes_source - ex.result_bytes;
      for (const s of ex.segments) {
        const b = Buffer.from(s.text, "utf8");
        collected.push(b);
        total += b.length;
      }
      expect(ex.disclosed_bytes_source).toBe(chargedBefore! + total);
      done = ex.complete;
      cursor = ex.next_cursor;
    }
    expect(done).toBe(true);
    expect(Buffer.concat(collected).equals(FULL.subarray(1, end))).toBe(true);
  });

  it("refuses a cursor crossing between the default and explicit strict", async () => {
    const { inspect } = await entrance();
    const first = inspect(bytes(0, 28), { max_result_bytes: 4 });
    const cursor = first.extraction.next_cursor;
    expect(cursor).toBeTruthy();
    const crossed = inspect(bytes(0, 28, { align: "strict" }), { max_result_bytes: 4, cursor });
    expect(crossed.code).toBe("INVALID_REQUEST");
    expect(crossed.failure_detail).toBe("BAD_CURSOR");
    expect(crossed.extraction).toBeUndefined();
    const strictFirst = inspect(bytes(0, 28, { align: "strict" }), { max_result_bytes: 4 });
    const strictCursor = strictFirst.extraction.next_cursor;
    expect(strictCursor).toBeTruthy();
    const back = inspect(bytes(0, 28), { max_result_bytes: 4, cursor: strictCursor });
    expect(back.code).toBe("INVALID_REQUEST");
    expect(back.failure_detail).toBe("BAD_CURSOR");
  });

  it("never mutates caller-owned arguments", async () => {
    const { execute, handle } = await entrance();
    const selector = bytes(2, 9);
    const params = { source_id: handle.source_id, snapshot_id: handle.snapshot_id, selector };
    const before = structuredClone(params);
    Object.freeze(selector);
    Object.freeze(params);
    const out = execute("m1", params) as { content: Array<{ text: string }> };
    expect(JSON.parse(out.content[0]!.text).code).toBe("EXTRACTED");
    expect(params).toEqual(before);
    expect(Object.hasOwn(selector, "align")).toBe(false);
  });

  it("keeps the max_result_bytes cap for the default", async () => {
    const { inspect } = await entrance();
    const out = inspect(bytes(2, 9), { max_result_bytes: 2 });
    expect(out.code).toBe("EXTRACTED");
    expect(out.extraction.result_bytes).toBeLessThanOrEqual(2);
    expect(out.extraction.complete).toBe(false);
  });

  it("presents the tool default; the core request default stays strict", () => {
    // The core default itself is covered by packages/core-ts/test/inspect.test.ts
    // ("keeps the strict default refusal..."), which this change leaves untouched.
    const selector = INSPECT_TOOL_PARAMETERS.properties.selector.description;
    expect(selector).toContain("align omitted here means char");
    expect(selector).toContain("low-level core request default remains strict");
    expect(INSPECT_TOOL_DESCRIPTION).toContain('without "align" uses "char"');
  });
});
