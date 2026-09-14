import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { serializedBytes } from "../src/envelope.js";
import { ShuntError } from "../src/errors.js";
import { DEFAULT_LIMITS, type Limits } from "../src/limits.js";
import { FallbackChainProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { FakeLuna, claimsJson, makeCapability, makeConfig } from "./support.js";

function fixture(lines = 2) {
  const dir = mkdtempSync(join(tmpdir(), "shunt-cache-"));
  mkdirSync(join(dir, "ws"), { recursive: true });
  const path = join(dir, "ws", "source.txt");
  writeFileSync(path, Array.from({ length: lines }, (_, i) =>
    i === 0 ? "retry_limit: 7" : `padding-${i}-${"x".repeat(200)}`).join("\n") + "\n");
  const reply = claimsJson(
    [{ text: "retry_limit is 7", citation_ids: ["c1"] }],
    [{ id: "c1", line_start: 1, line_end: 1, quote: "retry_limit: 7" }],
  );
  const provider = new FakeLuna([], reply);
  const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), { provider });
  return { session, provider, entry: session.registerPath(path) };
}

function request(entry: ReturnType<ShuntSession["registerPath"]>, id: string, overrides: object = {}) {
  return {
    schema_version: "1.3", request_id: id, operation: "read",
    question: "What is retry_limit?",
    sources: [{ source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId,
      selector: { kind: "lines", start: 1, end: entry.snapshot.lineCount } }],
    budgets: { max_chunks: 8, max_answer_bytes: 8192, deadline_ms: 60000 },
    ...overrides,
  };
}

describe("scoped exact reader answer reuse", () => {
  it("reuses a complete exact query and reports zero usage for the hit", async () => {
    const { session, provider, entry } = fixture();
    const first = await session.read(request(entry, "req_first"));
    const second = await session.read(request(entry, "req_second"));
    expect(first.code).toBe("ANSWERED");
    expect(second.answer).toBe(first.answer);
    expect(provider.callCount).toBe(1);
    expect(second.provenance).toMatchObject({
      cache_reused: true, attempts_started: 0, attempts_usage_complete: 0,
      usage_complete: true,
    });
    const stats = session.stats({
      schema_version: "1.2", request_id: "req_stats", operation: "stats", page_size: 8,
    });
    const row = stats.stats!.records.find((record) => record.operation_id === second.accounting_id)!;
    expect(row).toMatchObject({
      attempts_started: 0, attempts_usage_complete: 0, reader_token_method: "not_applicable",
      reader_input_tokens: null, reader_output_tokens: null, reader_cache_tokens: null,
    });
  });

  it("keeps query, selector, budget, model, snapshot, and authorization boundaries outside the hit", async () => {
    const { session, provider, entry } = fixture();
    await session.read(request(entry, "req_first"));
    await session.read(request(entry, "req_question", { question: "What retry limit applies?" }));
    await session.read(request(entry, "req_selector", {
      sources: [{ source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId,
        selector: { kind: "lines", start: 1, end: 1 } }],
    }));
    await session.read(request(entry, "req_budget", {
      budgets: { max_chunks: 8, max_answer_bytes: 4096, deadline_ms: 60000 },
    }));
    (provider as unknown as { model: string }).model = "gpt-5.6-luna-reconfigured";
    await session.read(request(entry, "req_model"));
    expect(provider.callCount).toBe(5);
    const changed = await session.read(request(entry, "req_snapshot", {
      sources: [{ source_id: entry.sourceId, snapshot_id: `sha256:${"0".repeat(64)}`,
        selector: { kind: "lines", start: 1, end: entry.snapshot.lineCount } }],
    }));
    expect(changed.code).toBe("SOURCE_CHANGED");
    expect(provider.callCount).toBe(5);
    session.close();
    const revoked = await session.read(request(entry, "req_revoked"));
    expect(revoked.code).toBe("SOURCE_EXPIRED");
    expect(revoked.provenance?.cache_reused).toBeUndefined();
  });

  it("never stores a partial answer", async () => {
    const { session, provider, entry } = fixture(400);
    const limited = request(entry, "req_partial", {
      budgets: { max_chunks: 1, max_answer_bytes: 8192, deadline_ms: 60000 },
    });
    expect((await session.read(limited)).status).toBe("partial");
    limited.request_id = "req_partial_again";
    expect((await session.read(limited)).status).toBe("partial");
    expect(provider.callCount).toBe(2);
  });

  it("never stores an answer produced by a fallback provider", async () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-cache-fallback-"));
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", "source.txt");
    writeFileSync(path, "retry_limit: 7\n");
    const reply = claimsJson(
      [{ text: "retry_limit is 7", citation_ids: ["c1"] }],
      [{ id: "c1", line_start: 1, line_end: 1, quote: "retry_limit: 7" }],
    );
    const primary = new FakeLuna([
      new ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", true),
    ], reply);
    const fallback = new FakeLuna([], reply, "fallback-model");
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider: new FallbackChainProvider(primary, [fallback]),
    });
    const entry = session.registerPath(path);

    const first = await session.read(request(entry, "req_fallback"));
    const second = await session.read(request(entry, "req_primary"));
    expect(first.provenance?.fallback_used).toBe(true);
    expect(second.provenance?.cache_reused).not.toBe(true);
    expect(primary.callCount).toBe(2);
    expect(fallback.callCount).toBe(1);
  });

  it("rechecks the envelope cap after replacing cached request metadata", async () => {
    const { session, provider, entry } = fixture();
    const first = await session.read(request(entry, "a"));
    const narrowed = {
      ...DEFAULT_LIMITS, maxEnvelopeBytes: serializedBytes(first) + 10,
    } satisfies Limits;
    (session as unknown as { reader: { limits: Limits } }).reader.limits = narrowed;

    const second = await session.read(request(entry, "x".repeat(64)));
    expect(provider.callCount).toBe(2);
    expect(second.provenance?.cache_reused).not.toBe(true);
  });

  it("evicts the least-recently-used answer after the fixed 32-entry bound", async () => {
    const { session, provider, entry } = fixture();
    for (let i = 0; i < 33; i += 1) {
      await session.read(request(entry, `req_${i}`, { question: `What is retry_limit? variant ${i}` }));
    }
    await session.read(request(entry, "req_again", { question: "What is retry_limit? variant 0" }));
    expect(provider.callCount).toBe(34);
  });
});
