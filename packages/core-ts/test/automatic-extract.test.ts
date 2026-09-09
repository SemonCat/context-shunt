import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { ShuntError } from "../src/errors.js";
import { serializedBytes } from "../src/envelope.js";
import { enforce } from "../src/guard.js";
import { Inspector } from "../src/inspect.js";
import { FallbackChainProvider, type ReaderProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { FakeLuna, makeCapability, makeConfig } from "./support.js";

const outage = () => new ShuntError("MODEL_ERROR", "PRIVATE_BODY", true);
function setup(provider: ReaderProvider = new FakeLuna([], outage()), config: Record<string, unknown> = {}) {
  const dir = mkdtempSync(join(tmpdir(), "shunt-auto-"));
  const session = new ShuntSession("sess", makeConfig(dir, config), makeCapability(), { provider });
  const body = 'prefix 日本 "quoted" \\ tab\t\n'.repeat(1000) + "TAIL_CANARY";
  const path = join(dir, "ws", "source.txt");
  writeFileSync(path, body);
  const entry = session.registerPath(path);
  const request = { schema_version: "1.1", request_id: "req_fallback", operation: "read",
    question: "What is here?", sources: [{ source_id: entry.sourceId,
      snapshot_id: entry.snapshot.snapshotId, selector: { kind: "all" } }],
    budgets: { max_chunks: 1, max_answer_bytes: 8192, deadline_ms: 60000 } };
  return { session, entry, request, body, dir };
}
function stats(session: ShuntSession) {
  return session.stats({ schema_version: "1.1", request_id: "req_stats", operation: "stats" }).stats!.records;
}

describe("automatic deterministic escape hatch", () => {
  it("exhausts all providers, labels exact bytes, and accounts once", async () => {
    const failure = outage();
    failure.billedUsage = { inputTokens: 10, outputTokens: 5, method: "exact" };
    const a = new FakeLuna([], failure);
    const b = new FakeLuna([], failure);
    const { session, entry, request, body } = setup(new FallbackChainProvider(a, [b]));
    const env = await session.read(request);
    expect(env.code).toBe("EXTRACTED");
    enforce(env);
    expect(a.callCount).toBe(2); expect(b.callCount).toBe(2);
    expect(env.status).toBe("partial"); expect(env.coverage.complete).toBe(false);
    expect(env.result_kind).toBe("deterministic_extraction");
    expect(env.provenance!.derived).toBe(false);
    expect(env.provenance!.attribution_status).toBe("not_applicable");
    expect(env.provenance!.attempts_started).toBe(4);
    expect(env.answer).toBe(""); expect(env.citations).toEqual([]);
    expect(env.guidance).toContain("Escape hatch: exact deterministic fallback extraction");
    expect(env.guidance).toContain("not an LLM summary"); expect(env.guidance).toContain("MODEL_ERROR");
    const segment = env.extraction!.segments[0]!;
    expect(segment.kind).toBe("bytes"); expect(segment.start).toBe(0);
    expect(Buffer.from(segment.text)).toEqual(Buffer.from(body).subarray(0, segment.end));
    expect(env.extraction!.result_bytes).toBeGreaterThan(0);
    expect(env.extraction!.result_bytes).toBeLessThanOrEqual(2048);
    expect(env.sources[0]!.snapshot_id).toBe(entry.snapshot.snapshotId);
    expect(env.recovery!.handles_valid).toBe(true);
    expect(JSON.stringify(env)).not.toContain("PRIVATE_BODY");
    expect(JSON.stringify(env)).not.toContain("TAIL_CANARY");
    const rows = stats(session).filter((r) => r.operation_id === env.accounting_id);
    expect(rows).toHaveLength(1);
    expect(rows[0]!.attempts_started).toBe(4);
    expect(rows[0]!.reader_input_tokens).toBe(40); expect(rows[0]!.reader_output_tokens).toBe(20);
    expect(rows[0]!.reader_token_method).toBe("exact");
    expect(rows[0]!.main_model_envelope_bytes).toBe(serializedBytes(env));
    expect(rows[0]!.delivery_boundary).toBe("extraction");
  });
  it.each([{ reader: { automatic_extract: false } }, { inspect: { enabled: false } }])(
    "retains original failure when disabled %j", async (config) => {
      const { session, request } = setup(undefined, config);
      const env = await session.read(request);
      expect(env.code).toBe("MODEL_ERROR"); expect(env.extraction).toBeUndefined();
      expect(env.recovery!.actions).toContain("INSPECT_HANDLE");
    });
  it("respects cumulative disclosure", async () => {
    const { session, request } = setup(undefined, { limits: { disclosure_max_per_source_bytes: 32 } });
    expect((await session.read(request)).extraction!.result_bytes).toBeLessThanOrEqual(32);
    const second = await session.read(request);
    expect(second.code).toBe("MODEL_ERROR"); expect(second.extraction).toBeUndefined();
  });
  it("retains all sources and omissions but extracts only the first", async () => {
    const { session, request, dir } = setup();
    const path = join(dir, "ws", "second.txt");
    writeFileSync(path, "SECOND_SOURCE_CANARY\n".repeat(100));
    const entry = session.registerPath(path);
    request.sources.push({ source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId, selector: { kind: "all" } });
    const env = await session.read(request);
    expect(env.code).toBe("EXTRACTED"); expect(env.sources).toHaveLength(2);
    expect(env.coverage.omitted.map((o) => o.source_id)).toEqual(request.sources.map((s) => s.source_id));
    expect(JSON.stringify(env)).not.toContain("SECOND_SOURCE_CANARY");
  });
  it.each(['not JSON', '{"claims": [], "citations": []}',
    '{"answer":"weak answer", "citations": []}',
    '{"answer":"Wrong [c1].", "citations":[{"id":"c1","line_start":1,"line_end":1,"quote":"missing"}]}',
    new ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", false), new ShuntError("CANCELLED")])(
    "never extracts for nonavailability %s", async (reply) => {
      const { session, request } = setup(new FakeLuna([], reply));
      expect((await session.read(request)).extraction).toBeUndefined();
    });
  it("does not extract after a format failure followed by outage", async () => {
    const { session, request } = setup(new FakeLuna(["bad"], outage()));
    expect((await session.read(request)).extraction).toBeUndefined();
  });
  it("extracts after an actual call timeout", async () => {
    const provider: ReaderProvider = { target: { model: "slow", provider: "test" },
      async complete() { await new Promise((r) => setTimeout(r, 150)); throw outage(); } };
    const { session, request } = setup(provider, { limits: { model_call_deadline_ms: 10 } });
    const env = await session.read(request);
    expect(env.code).toBe("EXTRACTED"); expect(env.guidance).toContain("TIMEOUT");
    expect(env.provenance!.attempts_started).toBe(1);
  });
  it.each(["STORE_FAILED", "SOURCE_EXPIRED", "SOURCE_CHANGED"])(
    "retains the original failure when fallback handle resolution fails: %s", async (code) => {
      const { session, request } = setup();
      const original = session.registry.resolve.bind(session.registry);
      let resolves = 0;
      vi.spyOn(session.registry, "resolve").mockImplementation((...args) => {
        if (++resolves > 1) throw new ShuntError(code, "PRIVATE_STORE_BODY");
        return original(...args);
      });
      const env = await session.read(request);
      expect(env.code).toBe("MODEL_ERROR"); expect(env.extraction).toBeUndefined();
      expect(JSON.stringify(env)).not.toContain("PRIVATE_STORE_BODY");
      expect(env.recovery!.handles_valid).toBe(false);
    });
  it.each([0, 4097, true, 2.5, "2048", null])("rejects invalid byte caps %s", (value) => {
    expect(() => setup(undefined, { reader: { fallback_max_bytes: value } })).toThrow();
  });
  it("narrows to config and request caps", async () => {
    const { session, request } = setup(undefined, { reader: { fallback_max_bytes: 100 } });
    request.budgets.max_answer_bytes = 50;
    expect((await session.read(request)).extraction!.result_bytes).toBeLessThanOrEqual(50);
  });
});

describe("shared automatic extraction contract", () => {
  const cases = JSON.parse(readFileSync(new URL("../../../contracts/v1/conformance/automatic-extract-cases.json", import.meta.url), "utf8")) as Array<{
    name: string; text: string; cap: number; expected: string | null; end: number | null;
  }>;
  it.each(cases)("$name", async (fixture) => {
    const { session, request, dir } = setup(undefined, { reader: { fallback_max_bytes: fixture.cap } });
    const path = join(dir, "ws", "fixture.txt");
    writeFileSync(path, fixture.text);
    const entry = session.registerPath(path);
    request.sources = [{ source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId, selector: { kind: "all" } }];
    const env = await session.read(request);
    if (fixture.expected === null) {
      expect(env.code).toBe("MODEL_ERROR"); expect(env.extraction).toBeUndefined();
    } else {
      enforce(env);
      expect(env.status).toBe("partial"); expect(env.code).toBe("EXTRACTED");
      expect(env.extraction!.segments).toEqual([{ kind: "bytes", start: 0, end: fixture.end, text: fixture.expected }]);
      expect(env.extraction!.complete).toBe(false); expect(env.provenance!.derived).toBe(false);
    }
  });
  it("never returns a whole line-oversized source", async () => {
    const { session, request, dir } = setup();
    const path = join(dir, "ws", "tiny-lines.txt");
    const body = "x\n".repeat(351);
    writeFileSync(path, body);
    const entry = session.registerPath(path);
    request.sources = [{ source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId, selector: { kind: "all" } }];
    const env = await session.read(request);
    expect(env.extraction!.result_bytes).toBeGreaterThan(0);
    expect(env.extraction!.result_bytes).toBeLessThan(Buffer.byteLength(body));
    expect(env.extraction!.complete).toBe(false);
  });
});

it("guard refusal consumes no disclosure", async () => {
  const { session, entry, request } = setup(undefined, { limits: { max_extended_envelope_bytes: 2048 } });
  const before = session.store.disclosureAllowance(session.identity, entry.sourceId);
  const env = await session.read(request);
  expect(env.code).toBe("MODEL_ERROR"); expect(env.extraction).toBeUndefined();
  expect(session.store.disclosureAllowance(session.identity, entry.sourceId)).toEqual(before);
});
it("request timeout without a response extracts", async () => {
  const provider: ReaderProvider = { target: { model: "slow", provider: "test" },
    async complete() { await new Promise((r) => setTimeout(r, 150)); throw outage(); } };
  const { session, request } = setup(provider);
  request.budgets.deadline_ms = 20;
  const env = await session.read(request);
  expect(env.code).toBe("EXTRACTED"); expect(env.guidance).toContain("TIMEOUT");
});
it("respects session disclosure exhaustion", async () => {
  const { session, entry, request } = setup(undefined, { limits: { disclosure_max_per_session_bytes: 32 } });
  expect(session.store.chargeDisclosure(session.identity, entry.sourceId, "bytes", 32).granted).toBe(true);
  const env = await session.read(request);
  expect(env.code).toBe("MODEL_ERROR"); expect(env.extraction).toBeUndefined();
});
it.each([null, 0, 1, "false", {}])("requires a boolean automatic flag: %s", (value) => {
  expect(() => setup(undefined, { reader: { automatic_extract: value } })).toThrow();
});
it("timeout retains already started fallback attempts", async () => {
  const first = new FakeLuna([], outage());
  let secondCalls = 0;
  const second: ReaderProvider = { target: { model: "slow", provider: "test" },
    async complete() { secondCalls++; await new Promise((r) => setTimeout(r, 150)); throw outage(); } };
  const { session, request } = setup(new FallbackChainProvider(first, [second]),
    { limits: { model_call_deadline_ms: 20 } });
  const env = await session.read(request);
  expect(env.code).toBe("EXTRACTED"); expect(first.callCount).toBe(1); expect(secondCalls).toBe(1);
  expect(env.provenance!.attempts_started).toBe(2);
  expect(stats(session).find((r) => r.operation_id === env.accounting_id)!.attempts_started).toBe(2);
});
it("secret guard refusal neither leaks nor charges", async () => {
  const { session, entry, request } = setup();
  const original = Inspector.prototype.extract;
  const secretMarker = "-----BEGIN PRIVATE KEY-----";
  const before = session.store.disclosureAllowance(session.identity, entry.sourceId);
  const spy = vi.spyOn(Inspector.prototype, "extract").mockImplementation(function (this: Inspector, ...args) {
    const extraction = original.apply(this, args);
    return { ...extraction, resultBytes: secretMarker.length,
      segments: [{ kind: "bytes", start: 0, end: secretMarker.length, text: secretMarker }] };
  });
  try {
    const env = await session.read(request);
    expect(env.code).toBe("MODEL_ERROR"); expect(env.extraction).toBeUndefined();
    expect(JSON.stringify(env)).not.toContain(secretMarker);
    expect(session.store.disclosureAllowance(session.identity, entry.sourceId)).toEqual(before);
  } finally { spy.mockRestore(); }
});
