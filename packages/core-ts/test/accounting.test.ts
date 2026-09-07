/**
 * unit accounting (TypeScript core) - signed savings, one-time credit, never zero for
 * unknown.
 *
 * The arithmetic is small; the discipline is the point. This gate pins the two formulas,
 * proves the baseline is credited once per snapshot, proves an unreported token count stays
 * null rather than becoming a zero, and proves the stats surface cannot be turned into a
 * reset switch, a cross-session read or a content channel.
 */
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  composeRecord,
  envelopeEgress,
  estimateTokens,
  estimatedReaderCost,
  hostTruncatedBaseline,
  noBaseline,
  noReaderCost,
  withheldPayloadBaseline,
} from "../src/accounting.js";
import { Envelope } from "../src/envelope.js";
import {
  BASELINE_ESTIMATE_METHOD,
  DEFAULT_LIMITS as L,
  EMITTED_SCHEMA_VERSION,
} from "../src/limits.js";
import { ALLOWED_LABEL_KEYS, InMemoryMetrics, MetricsError } from "../src/metrics.js";
import { HostBridgeProvider, transientProviderError } from "../src/provider.js";
import { Reader } from "../src/reader.js";
import { makeRegistry } from "./support.js";
import { snapshotBytes } from "../src/snapshot.js";
import { ShuntSession } from "../src/session.js";
import { FakeLuna, answerJson, makeCapability, makeConfig } from "./support.js";

const CANARY = "ACCOUNTING-CANARY-51ee7a";

const enc = (t: string) => new TextEncoder().encode(t);

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-acct-"));
}

function session(dir: string, provider?: FakeLuna, overrides: Record<string, unknown> = {}) {
  return new ShuntSession("sess", makeConfig(dir, overrides), makeCapability(), {
    provider: provider ?? new FakeLuna(),
  });
}

function bigSource(dir: string, s: ShuntSession, marker = CANARY) {
  const ws = join(dir, "ws");
  mkdirSync(ws, { recursive: true });
  const path = join(ws, "big.txt");
  const rows = [`line 0001 ${marker}`];
  for (let i = 2; i <= 2000; i += 1) rows.push(`line ${String(i).padStart(4, "0")} value-${i}`);
  writeFileSync(path, rows.join("\n") + "\n");
  return s.registerPath(path);
}

function readRequest(
  entry: { sourceId: string; snapshot: { snapshotId: string } },
  question = "What values are configured?",
  refined = false,
): Record<string, unknown> {
  return {
    schema_version: EMITTED_SCHEMA_VERSION,
    request_id: "req_a1",
    operation: "read",
    question,
    sources: [
      {
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        selector: { kind: "all" },
      },
    ],
    budgets: { max_chunks: 2, max_answer_bytes: 4096, deadline_ms: 60000 },
    ...(refined ? { refined: true } : {}),
  };
}

function stats(s: ShuntSession, extra: Record<string, unknown> = {}): Envelope {
  return s.stats({
    schema_version: EMITTED_SCHEMA_VERSION,
    request_id: "req_stats",
    operation: "stats",
    ...extra,
  });
}

// -- the formulas -----------------------------------------------------------

describe("the accounting formulas", () => {
  it("are exactly as specified", () => {
    const record = composeRecord({
      operationId: "acc_" + "1".repeat(16),
      kind: "read",
      status: "ok",
      code: "ANSWERED",
      baseline: withheldPayloadBaseline(400_000),
      baselineCredited: true,
      reader: {
        inputTokens: 6100,
        outputTokens: 420,
        cacheTokens: undefined,
        method: "exact",
        attemptsStarted: 1,
        attemptsUsageComplete: 1,
      },
      egress: envelopeEgress("envelope", 2048),
    });
    const baselineTokens = estimateTokens(400_000);
    const envelopeTokens = estimateTokens(2048);
    expect(record.baselineCreditTokens).toBe(baselineTokens);
    expect(record.mainModelEnvelopeTokens).toBe(envelopeTokens);
    expect(record.mainContextTokensSaved).toBe(baselineTokens - envelopeTokens);
    expect(record.netTokensSaved).toBe(record.mainContextTokensSaved - 6100 - 420);
  });

  it("are signed and go negative when they should", () => {
    // An inspect page or a refined question costs context and withholds nothing new.
    const record = composeRecord({
      operationId: "acc_" + "2".repeat(16),
      kind: "inspect",
      status: "ok",
      code: "EXTRACTED",
      baseline: noBaseline(),
      baselineCredited: false,
      reader: noReaderCost(),
      egress: envelopeEgress("extraction", 16384),
    });
    expect(record.mainContextTokensSaved).toBe(-estimateTokens(16384));
    expect(record.netTokensSaved).toBe(record.mainContextTokensSaved);
    expect(record.baselineKind).toBe("none");
    expect(record.rawInputBaselineTokens).toBeUndefined();
  });

  it("name an estimate rather than fabricating a zero", () => {
    const reader = estimatedReaderCost({
      promptBytes: 8000, completionBytes: 400, attemptsStarted: 1,
    });
    expect(reader.method).toBe("bytes_div_4");
    expect(reader.method).toBe(BASELINE_ESTIMATE_METHOD);
    expect(reader.inputTokens).toBe(estimateTokens(8000));
    expect(reader.attemptsUsageComplete).toBe(0);
    // noReaderCost means "no attempt", a different fact from "unreported".
    expect(noReaderCost().inputTokens).toBeUndefined();
    expect(noReaderCost().method).toBe("not_applicable");
  });

  it("label a host-truncated baseline separately", () => {
    // Crediting the full payload when the host already truncated would be a fabrication.
    const counterfactual = withheldPayloadBaseline(1_000_000);
    const observed = hostTruncatedBaseline(16_384);
    expect(counterfactual.kind).toBe("full_payload_counterfactual");
    expect(observed.kind).toBe("host_truncated_observed");
    expect(observed.tokens!).toBeLessThan(counterfactual.tokens!);
  });
});

// -- one-time credit --------------------------------------------------------

describe("the one-time baseline credit", () => {
  it("credits once and makes recovery pure cost", async () => {
    const dir = tmp();
    const luna = new FakeLuna(
      [],
      answerJson(`The first line carries ${CANARY} [c1].`, [
        { id: "c1", line_start: 1, line_end: 1, quote: CANARY },
      ]),
    );
    const s = session(dir, luna);
    const entry = bigSource(dir, s);

    expect((await s.read(readRequest(entry))).code).toBe("ANSWERED");
    const refined = await s.read(readRequest(entry, "And the second line?", true));
    expect(["ok", "partial"]).toContain(refined.status);

    const records = stats(s).stats!.records;
    const reads = records.filter((r) => r.kind === "read" || r.kind === "refined_read");
    expect(reads.length).toBe(2);
    expect(reads.filter((r) => r.baseline_credit_tokens > 0).length).toBe(1);
    const refinement = reads.find((r) => r.kind === "refined_read")!;
    expect(refinement.baseline_credit_tokens).toBe(0);
    // The refinement still reports what the payload measures, so the zero credit is
    // visibly a policy decision rather than a missing measurement.
    expect(refinement.raw_input_baseline_tokens!).toBeGreaterThan(0);
    expect(refinement.main_context_tokens_saved).toBeLessThan(0);
  });

  it("adds overhead for a failed retry without claiming a saving", async () => {
    const dir = tmp();
    const luna = new FakeLuna([], transientProviderError());
    const s = session(dir, luna);
    const entry = bigSource(dir, s);
    await s.read(readRequest(entry));
    const failed = await s.read(readRequest(entry, "try again", true));
    const refinement = stats(s).stats!.records.find((r) => r.kind === "refined_read")!;
    expect(refinement.baseline_credit_tokens).toBe(0);
    expect(refinement.attempts_started).toBeGreaterThanOrEqual(1);
    expect(failed.coverage.complete).toBe(false);
  });

  it("accumulates inspect cost against one credited baseline", async () => {
    const dir = tmp();
    const s = session(dir);
    const entry = bigSource(dir, s);
    await s.read(readRequest(entry));
    for (let i = 0; i < 3; i += 1) {
      s.inspect({
        schema_version: EMITTED_SCHEMA_VERSION,
        request_id: "req_i",
        operation: "inspect",
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        selector: { kind: "lines", start: 1, end: 40 },
        budgets: { max_result_bytes: 1024, max_scan_lines: 100 },
      });
    }
    const inspects = stats(s, { page_size: 8 }).stats!.records.filter((r) => r.kind === "inspect");
    expect(inspects.length).toBe(3);
    expect(inspects.every((r) => r.baseline_credit_tokens === 0)).toBe(true);
    expect(inspects.every((r) => r.main_context_tokens_saved < 0)).toBe(true);
    expect(inspects.every((r) => r.delivery_boundary === "extraction")).toBe(true);
  });
});

// -- egress measurement -----------------------------------------------------

describe("egress measurement", () => {
  it("measures the final encoded envelope", async () => {
    // The envelope carries only the opaque id, so the measurement is not self-referential.
    const dir = tmp();
    const s = session(dir);
    const entry = bigSource(dir, s);
    const envelope = await s.read(readRequest(entry));
    const exactBytes = new TextEncoder().encode(JSON.stringify(envelope)).length;
    const record = stats(s).stats!.records.find(
      (r) => r.operation_id === envelope.accounting_id,
    )!;
    expect(record.main_model_envelope_bytes).toBe(exactBytes);
    expect(record.main_model_envelope_tokens).toBe(estimateTokens(exactBytes));
    expect(record.envelope_token_method).toBe(BASELINE_ESTIMATE_METHOD);
  });

  it("carries no monetary value anywhere", async () => {
    const dir = tmp();
    const s = session(dir);
    const entry = bigSource(dir, s);
    await s.read(readRequest(entry));
    const blob = JSON.stringify(stats(s)).toLowerCase();
    for (const money of ["usd", "cost", "price", "dollar", "cents"]) {
      expect(blob).not.toContain(money);
    }
  });
});

// -- the stats surface ------------------------------------------------------

describe("the stats surface", () => {
  it("is read-only and scoped to this session", async () => {
    const dir = tmp();
    const s = session(dir);
    const entry = bigSource(dir, s);
    await s.read(readRequest(entry));
    const before = stats(s).stats!.total_records;

    // There is no parameter that could reset a counter or name another session; an
    // unknown field is rejected rather than ignored.
    const rejected = stats(s, { reset: true });
    expect(rejected.status).toBe("error");
    expect(rejected.code).toBe("INVALID_REQUEST");
    expect(stats(s, { session_id: "someone-else" }).status).toBe("error");
    // Reading stats records its own operation and destroys nothing.
    expect(stats(s).stats!.total_records).toBeGreaterThan(before);
  });

  it("pages boundedly and reveals no content", async () => {
    const dir = tmp();
    const s = session(dir);
    const entry = bigSource(dir, s);
    for (let i = 0; i < 12; i += 1) await s.read(readRequest(entry));
    const page = stats(s, { page: 1, page_size: 8 }).stats!;
    expect(page.records.length).toBeLessThanOrEqual(L.statsMaxRecordsPerPage);
    expect(page.next_page).toBe(2);
    const pageTwo = stats(s, { page: 2, page_size: 8 }).stats!;
    const firstIds = new Set(page.records.map((r) => r.operation_id));
    expect(pageTwo.records.every((r) => !firstIds.has(r.operation_id))).toBe(true);

    const blob = JSON.stringify(stats(s, { page: 1, page_size: 8 }));
    expect(blob).not.toContain(CANARY);
    expect(blob).not.toContain(dir);
    expect(blob).not.toContain("What values are configured?");
  });

  it("carries only closed enums and counters in a record", async () => {
    const dir = tmp();
    const s = session(dir);
    const entry = bigSource(dir, s);
    await s.read(readRequest(entry));
    for (const record of stats(s).stats!.records) {
      expect(["gate_block", "capture", "read", "refined_read", "inspect", "stats", "spill"])
        .toContain(record.kind);
      expect(["ok", "partial", "blocked", "error"]).toContain(record.status);
      expect(["exact", "bytes_div_4", "unknown"]).toContain(record.baseline_method);
      expect(["envelope", "extraction", "pointer", "block_message", "none"])
        .toContain(record.delivery_boundary);
      // No model or provider name anywhere in a record.
      expect(JSON.stringify(record).toLowerCase()).not.toContain("luna");
    }
  });

  it("preserves null for an unreported direction", async () => {
    // A session where no provider reported usage shows null, not zero.
    const dir = tmp();
    const luna = new FakeLuna([], answerJson("", []), undefined, { usageExact: false });
    const s = session(dir, luna);
    const entry = bigSource(dir, s);
    await s.read(readRequest(entry));
    expect(stats(s).stats!.totals.reader_cache_tokens).toBeNull();
    const record = stats(s).stats!.records.find((r) => r.kind === "read")!;
    expect(record.reader_token_method).toBe("bytes_div_4");
    expect(record.reader_cache_tokens).toBeNull();
    expect(record.attempts_usage_complete).toBe(0);
  });
});

// -- metric labels ----------------------------------------------------------

describe("metric labels", () => {
  it("are a closed enum that rejects high-cardinality values", () => {
    const metrics = new InMemoryMetrics();
    expect(ALLOWED_LABEL_KEYS.has("model")).toBe(false);
    expect(ALLOWED_LABEL_KEYS.has("provider")).toBe(false);
    expect(ALLOWED_LABEL_KEYS.has("path")).toBe(false);
    for (const bad of [
      { model: "gpt-5.6-luna" },
      { provider: "openai" },
      { request_id: "req_1" },
    ]) {
      expect(() => metrics.count("reader_model_calls", bad)).toThrowError(MetricsError);
    }
    // An allowed key still refuses a value that is not a bounded token.
    expect(() => metrics.count("gate_decision", { reason: "path /tmp/secret with spaces" }))
      .toThrowError(MetricsError);
  });
});

// -- exact provider usage must survive, and a late call must still be billed --

describe("provider usage preservation", () => {
  /**
   * `ChunkOutcome.usage` started at `NO_USAGE`, whose method is `unknown` - correct for a
   * bridge that reported no counts, wrong for an empty accumulator. `unknown + exact` is
   * `unknown`, so the provider's exact counts were merged away and every request fell
   * back to the byte estimate.
   */
  it("keeps exact provider counts instead of falling back to the estimate", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const luna = new FakeLuna([], answerJson("mode = fast [c1]", [[1, 1, "mode = fast"]]));
    const result = await new Reader(registry, luna).answerDetailed("sess", readRequest(entry));

    expect(result.cost.method).toBe("exact");
    expect(result.cost.attemptsStarted).toBe(1);
    expect(result.cost.attemptsUsageComplete).toBe(1);
    expect(result.cost.inputTokens).toBe(10);
    expect(result.cost.outputTokens).toBe(5);
  });

  it("still reports a named estimate when the bridge reports nothing", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const silent = new FakeLuna([], answerJson("mode = fast [c1]", [[1, 1, "mode = fast"]]));
    silent.usageExact = false;
    const result = await new Reader(registry, silent).answerDetailed("sess", readRequest(entry));

    expect(result.cost.method).toBe("bytes_div_4");
    expect(result.cost.attemptsUsageComplete).toBe(0);
  });
});

describe("mixed-source baseline credit", () => {
  /**
   * `baselineFor` summed the bytes of every selected source but folded the per-source
   * credit results into a single OR, so a second read mixing an already-credited source
   * with a new one credited both again.
   */
  it("credits only the newly withheld source", async () => {
    const dir = tmp();
    const s = session(dir);
    const ws = join(dir, "ws");
    mkdirSync(ws, { recursive: true });
    writeFileSync(join(ws, "one.txt"), Array.from({ length: 2000 }, (_, i) => `one ${i} value`).join("\n"));
    writeFileSync(join(ws, "two.txt"), Array.from({ length: 3000 }, (_, i) => `two ${i} value`).join("\n"));
    const first = s.registerPath(join(ws, "one.txt"));
    const second = s.registerPath(join(ws, "two.txt"));

    const req = (...entries: Array<typeof first>) => {
      const base = readRequest(first) as Record<string, unknown>;
      base["sources"] = entries.map((e) => ({
        source_id: e.sourceId,
        snapshot_id: e.snapshot.snapshotId,
        selector: { kind: "all" },
      }));
      return base;
    };

    await s.read(req(first));
    const afterFirst = Number(stats(s).stats!.totals.baseline_credit_tokens);
    expect(afterFirst).toBeGreaterThan(0);

    await s.read(req(first, second));
    const afterSecond = Number(stats(s).stats!.totals.baseline_credit_tokens);

    const added = afterSecond - afterFirst;
    const onlySecond = Math.ceil(second.snapshot.bytesLen / 4);
    expect(added).toBe(onlySecond);
  });
});

describe("usage survives an over-cap bridge reply", () => {
  it("reports the exact counts the call was billed", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const oversized = new HostBridgeProvider(async () => ({
      text: "x".repeat(L.maxToolResultBytes + 10),
      input_tokens: 23,
      output_tokens: 11,
      usage_exact: true,
    }));
    const result = await new Reader(registry, oversized).answerDetailed("sess", readRequest(entry));

    expect(result.cost.attemptsStarted).toBeGreaterThanOrEqual(1);
    expect(result.cost.method).toBe("exact");
    expect(result.cost.inputTokens).toBe(23);
    expect(result.cost.outputTokens).toBe(11);
  });
});
