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
import { Deadline, FakeClock } from "../src/clock.js";
import { ShuntError } from "../src/errors.js";
import {
  BASELINE_ESTIMATE_METHOD,
  DEFAULT_LIMITS as L,
  EMITTED_SCHEMA_VERSION,
} from "../src/limits.js";
import { ALLOWED_LABEL_KEYS, InMemoryMetrics, MetricsError } from "../src/metrics.js";
import {
  FallbackChainProvider, type HostBridgeCall, HostBridgeProvider, type ModelResponse,
  transientProviderError,
} from "../src/provider.js";
import { READER_MODEL } from "../src/limits.js";
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
    // `usageExact` is a constructor option, not a mutable field: assigning to it after
    // construction reached through `private readonly` and only compiled because the
    // declared typecheck was not being run.
    const silent = new FakeLuna(
      [],
      answerJson("mode = fast [c1]", [[1, 1, "mode = fast"]]),
      READER_MODEL,
      { usageExact: false },
    );
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

describe("fallback usage completeness", () => {
  /**
   * `exact` is a claim about the whole request, not about whichever attempt won.
   *
   * A chain whose first candidate failed without reporting usage and whose second
   * succeeded with exact counts merged that winner's usage into an empty accumulator, so
   * `readerCostOf` saw a complete `Usage` and returned `exact` - while the very same
   * record said `attemptsUsageComplete: 1` of `attemptsStarted: 2`. Python already
   * classified this schedule `bytes_div_4`; TypeScript did not.
   */
  function chainOf(...bridges: Array<() => Promise<Record<string, unknown>>>) {
    const [primary, ...rest] = bridges;
    return new FallbackChainProvider(
      new HostBridgeProvider(async () => (primary as () => Promise<Record<string, unknown>>)(), L, READER_MODEL, "openai"),
      rest.map((b, i) => new HostBridgeProvider(async () => b(), L, `fallback-${i}`, "openai")),
    );
  }

  const answered = () =>
    Promise.resolve({
      text: answerJson("mode = fast [c1]", [[1, 1, "mode = fast"]]),
      input_tokens: 7,
      output_tokens: 4,
      usage_exact: true,
    });

  it("is not exact when a successful fallback follows an unreported failure", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    let calls = 0;
    const chain = chainOf(
      () => {
        calls += 1;
        return Promise.reject(new Error("upstream unavailable"));
      },
      () => {
        calls += 1;
        return answered();
      },
    );
    const result = await new Reader(registry, chain).answerDetailed("sess", readRequest(entry));

    expect(calls).toBe(2);
    expect(result.cost.attemptsStarted).toBe(2);
    expect(result.cost.attemptsUsageComplete).toBe(1);
    expect(result.cost.method).toBe("bytes_div_4");
  });

  it("is exact only when every started attempt reported", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const chain = chainOf(answered);
    const result = await new Reader(registry, chain).answerDetailed("sess", readRequest(entry));

    expect(result.cost.attemptsStarted).toBe(result.cost.attemptsUsageComplete);
    expect(result.cost.method).toBe("exact");
    expect(result.cost.inputTokens).toBe(7);
  });

  /**
   * A retryable availability failure that still reports what the call was billed.
   *
   * This is what makes the chain *advance*: the previous version of this test used an
   * over-cap reply, which is `INVALID_MODEL_OUTPUT` - not an availability failure - so the
   * chain stopped at its first candidate and the case never exercised the fallback path it
   * claimed to cover. `HostBridgeProvider` rethrows a `ShuntError` unchanged, so a bridge
   * can report "unavailable, and here is what you were charged", which a metered gateway
   * genuinely can.
   */
  function billedUnavailable(inputTokens: number, outputTokens: number) {
    return async (): Promise<Record<string, unknown>> => {
      const err = new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
      (err as { billedUsage?: unknown }).billedUsage = {
        inputTokens,
        outputTokens,
        method: "exact",
      };
      throw err;
    };
  }

  const plainUnavailable = async (): Promise<Record<string, unknown>> => {
    throw new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
  };

  /** Drives the public Reader and reports what the providers were actually asked to do. */
  async function chainRun(
    first: () => Promise<Record<string, unknown>>,
    second: () => Promise<Record<string, unknown>>,
  ) {
    let calls = 0;
    const count = (bridge: () => Promise<Record<string, unknown>>) => async () => {
      calls += 1;
      return bridge();
    };
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const chain = new FallbackChainProvider(
      new HostBridgeProvider(count(first), L, READER_MODEL, "openai"),
      [new HostBridgeProvider(count(second), L, "gpt-5.6-sol", "openai")],
    );
    const result = await new Reader(registry, chain).answerDetailed("sess", readRequest(entry));
    return { calls, cost: result.cost };
  }

  it("keeps the exact total when every failed candidate reported its usage", async () => {
    const { calls, cost } = await chainRun(billedUnavailable(5, 3), billedUnavailable(7, 2));

    // Two candidates, and the reader's one retry: four real provider calls.
    expect(calls).toBe(4);
    expect(cost.attemptsStarted).toBe(calls);
    expect(cost.attemptsUsageComplete).toBe(calls);
    // Every attempt reported, so the sum of what was billed is exactly known - even though
    // the request answered nothing.
    expect(cost.method).toBe("exact");
    expect(cost.inputTokens).toBe(2 * (5 + 7));
    expect(cost.outputTokens).toBe(2 * (3 + 2));
  });

  it("counts the attempts that did report when only some of them did", async () => {
    const { calls, cost } = await chainRun(billedUnavailable(5, 3), plainUnavailable);

    expect(calls).toBe(4);
    expect(cost.attemptsStarted).toBe(calls);
    // One of the two candidates reports, on each of the two outer invocations.
    expect(cost.attemptsUsageComplete).toBe(2);
    // A partial tally is never presented as provider truth.
    expect(cost.method).toBe("bytes_div_4");
  });

  it("reports a named estimate when no candidate reported anything", async () => {
    const { calls, cost } = await chainRun(plainUnavailable, plainUnavailable);

    expect(calls).toBe(4);
    expect(cost.attemptsStarted).toBe(calls);
    expect(cost.attemptsUsageComplete).toBe(0);
    expect(cost.method).toBe("bytes_div_4");
  });
});

describe("each physical attempt is aggregated exactly once", () => {
  // A citation object, not a tuple: `answerJson` passes `citations` through verbatim, so a
  // tuple is a malformed citation, gets stripped, and the envelope becomes NO_MATCH -
  // which would quietly hollow out the availability assertion below.
  const answerText = answerJson("mode = fast [c1]", [
    { id: "c1", line_start: 1, line_end: 1, quote: "mode = fast" },
  ]);

  /** A provider that rethrows one *stable* error instance, which the contract allows. */
  function stableBilled(inputTokens: number, outputTokens: number) {
    const err = new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
    (err as { billedUsage?: unknown }).billedUsage = { inputTokens, outputTokens, method: "exact" };
    return async (): Promise<Record<string, unknown>> => {
      throw err;
    };
  }

  function chain(bridges: Array<() => Promise<Record<string, unknown>>>, counter: () => void) {
    const wrap = (b: () => Promise<Record<string, unknown>>) => async () => {
      counter();
      return b();
    };
    return new FallbackChainProvider(
      new HostBridgeProvider(wrap(bridges[0]!), L, READER_MODEL, "openai"),
      bridges.slice(1).map((b, i) => new HostBridgeProvider(wrap(b), L, `f${i}`, "openai")),
    );
  }

  /**
   * The aggregate used to be written onto the failing provider's own `ShuntError`, and that
   * same field was later read back as a fresh per-attempt report. A provider may
   * legitimately rethrow one stable instance - `HostBridgeProvider` passes a `ShuntError`
   * through unchanged - so across the reader's outer retry the first pass's aggregate came
   * back as input to the second: four calls totalling 24/10 were reported as 29/13.
   */
  it("does not re-ingest an aggregate when a provider reuses one error instance", async () => {
    let calls = 0;
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const provider = chain([stableBilled(5, 3), stableBilled(7, 2)], () => { calls += 1; });
    const result = await new Reader(registry, provider).answerDetailed("sess", readRequest(entry));

    expect(calls).toBe(4);
    expect(result.cost.attemptsStarted).toBe(calls);
    expect(result.cost.attemptsUsageComplete).toBe(calls);
    expect(result.cost.method).toBe("exact");
    // Two passes over both candidates, counted once each.
    expect(result.cost.inputTokens).toBe(2 * (5 + 7));
    expect(result.cost.outputTokens).toBe(2 * (3 + 2));
  });

  /**
   * The exhausted-budget exit re-attached the error it had already attached to, so one
   * billed call became two usage-complete attempts carrying doubled usage.
   */
  it("counts one attempt when the budget runs out after a single billed failure", async () => {
    let calls = 0;
    const slowBilled = async (): Promise<Record<string, unknown>> => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 60));
      const err = new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
      (err as { billedUsage?: unknown }).billedUsage = {
        inputTokens: 5,
        outputTokens: 3,
        method: "exact",
      };
      throw err;
    };
    const never = async (): Promise<Record<string, unknown>> => {
      calls += 1;
      throw new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
    };
    const provider = chain([slowBilled, never], () => {});

    let raised: ShuntError | undefined;
    try {
      // A budget the first call alone exhausts, so the second candidate never starts.
      await provider.complete({ system: "s", user: "u", maxOutputTokens: 100, timeoutMs: 50 });
    } catch (err) {
      raised = err as ShuntError;
    }

    expect(calls).toBe(1);
    expect(raised?.internalAttempts).toBe(1);
    expect(raised?.usageCompleteAttempts).toBe(1);
    expect(raised?.billedUsage).toEqual({ inputTokens: 5, outputTokens: 3, method: "exact" });
  });

  /**
   * A composite response is several physical calls, and arriving late does not merge them.
   * The late path counted it as one attempt, contradicting the aggregate usage it recorded
   * in the same breath.
   */
  it("keeps composite counts when the response arrives after the deadline", async () => {
    const clock = new FakeClock();
    let calls = 0;
    const failBilled = async (): Promise<Record<string, unknown>> => {
      calls += 1;
      const err = new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
      (err as { billedUsage?: unknown }).billedUsage = {
        inputTokens: 5,
        outputTokens: 3,
        method: "exact",
      };
      throw err;
    };
    // Succeeds, but spends the whole request budget on the way.
    const lateOk = async (): Promise<Record<string, unknown>> => {
      calls += 1;
      clock.advance(70_000);
      return { text: answerText, input_tokens: 7, output_tokens: 2, usage_exact: true };
    };
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const provider = chain([failBilled, lateOk], () => {});
    const result = await new Reader(registry, provider, undefined, clock)
      .answerDetailed("sess", readRequest(entry));

    expect(result.envelope.code).toBe("TIMEOUT");
    expect(calls).toBe(2);
    expect(result.cost.attemptsStarted).toBe(2);
    expect(result.cost.attemptsUsageComplete).toBe(2);
    expect(result.cost.method).toBe("exact");
    expect(result.cost.inputTokens).toBe(12);
    expect(result.cost.outputTokens).toBe(5);
  });

  /**
   * Ceilings are per call. Applying the single-call ceiling to a sum rejected work that was
   * legal call by call, so aggregate bookkeeping changed *availability*, not just metrics.
   */
  it("accepts an aggregate over the single-call ceiling when each attempt was legal", async () => {
    const each = L.maxOutputTokensPerCall - 548;
    expect(each * 2).toBeGreaterThan(L.maxOutputTokensPerCall);

    let calls = 0;
    const failBig = async (): Promise<Record<string, unknown>> => {
      calls += 1;
      const err = new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
      (err as { billedUsage?: unknown }).billedUsage = {
        inputTokens: 10,
        outputTokens: each,
        method: "exact",
      };
      throw err;
    };
    const okBig = async (): Promise<Record<string, unknown>> => {
      calls += 1;
      return { text: answerText, input_tokens: 10, output_tokens: each, usage_exact: true };
    };
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const provider = chain([failBig, okBig], () => {});
    const result = await new Reader(registry, provider).answerDetailed("sess", readRequest(entry));

    expect(calls).toBe(2);
    expect(result.envelope.code).toBe("ANSWERED");
    expect(result.envelope.answer.length).toBeGreaterThan(0);
    expect(result.cost.outputTokens).toBe(each * 2);
  });

  it("still refuses a single call that exceeds the per-call ceiling", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const overCap = new HostBridgeProvider(
      async () => ({
        text: answerText,
        input_tokens: 10,
        output_tokens: L.maxOutputTokensPerCall + 1,
        usage_exact: true,
      }),
      L,
      READER_MODEL,
      "openai",
    );
    const result = await new Reader(registry, overCap).answerDetailed("sess", readRequest(entry));
    expect(result.envelope.answer).toBe("");
  });
});

describe("estimates cover every physical attempt", () => {
  const answerText = answerJson("mode = fast [c1]", [
    { id: "c1", line_start: 1, line_end: 1, quote: "mode = fast" },
  ]);
  const plainUnavailable = async (): Promise<Record<string, unknown>> => {
    throw new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
  };
  function billedUnavailable(inputTokens: number, outputTokens: number) {
    return async (): Promise<Record<string, unknown>> => {
      const err = new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
      (err as { billedUsage?: unknown }).billedUsage = { inputTokens, outputTokens, method: "exact" };
      throw err;
    };
  }

  /** Runs a two-candidate chain and reports the prompt bytes actually transmitted. */
  async function measured(
    first: () => Promise<Record<string, unknown>>,
    second: () => Promise<Record<string, unknown>>,
  ) {
    let calls = 0;
    let promptBytes = 0;
    const count = (b: () => Promise<Record<string, unknown>>): HostBridgeCall => async (opts) => {
      calls += 1;
      promptBytes += enc(opts.system).length + enc(opts.user).length;
      return b();
    };
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    const chain = new FallbackChainProvider(
      new HostBridgeProvider(count(first), L, READER_MODEL, "openai"),
      [new HostBridgeProvider(count(second), L, "f0", "openai")],
    );
    const result = await new Reader(registry, chain).answerDetailed("sess", readRequest(entry));
    return { calls, promptBytes, cost: result.cost, envelope: result.envelope };
  }

  /**
   * The prompt is re-sent by every candidate a fallback tries, but was charged once per
   * outer invocation - so a two-candidate chain estimated four calls' input from one
   * call's bytes and halved the reported cost.
   */
  it("estimates input from every attempt's prompt on an unreported failure", async () => {
    const { calls, promptBytes, cost } = await measured(plainUnavailable, plainUnavailable);

    expect(calls).toBe(4);
    expect(cost.attemptsStarted).toBe(calls);
    expect(cost.method).toBe("bytes_div_4");
    // The value, not just the method: what was transmitted is what is estimated from.
    expect(cost.inputTokens).toBe(Math.ceil(promptBytes / 4));
  });

  it("estimates input from every attempt's prompt on an eventual success", async () => {
    const winner = async (): Promise<Record<string, unknown>> => ({ text: answerText });
    const { calls, promptBytes, cost, envelope } = await measured(plainUnavailable, winner);

    expect(calls).toBe(2);
    expect(envelope.code).toBe("ANSWERED");
    expect(cost.attemptsStarted).toBe(calls);
    expect(cost.inputTokens).toBe(Math.ceil(promptBytes / 4));
  });

  /**
   * Output the reader never saw is still output that was billed. Reporting zero for an
   * all-failure chain whose attempts reported what they produced understated real spend.
   */
  it("includes billed output from attempts whose text was never seen", async () => {
    const { calls, cost } = await measured(billedUnavailable(5, 3), plainUnavailable);

    expect(calls).toBe(4);
    expect(cost.method).toBe("bytes_div_4");
    // Two of the four attempts reported three output tokens each.
    expect(cost.outputTokens).toBe(6);
  });

  /**
   * Bounding only the sum cannot prove every constituent respected the per-call cap: a
   * failure reporting one token over it, plus a winner reporting one, stayed under the
   * two-attempt ceiling and was published as `exact`.
   */
  it("refuses a billed-failure claim no single call could have produced", async () => {
    const overCap = billedUnavailable(1, L.maxOutputTokensPerCall + 1);
    const tinyWinner = async (): Promise<Record<string, unknown>> => ({
      text: answerText,
      input_tokens: 1,
      output_tokens: 1,
      usage_exact: true,
    });
    const { calls, cost, envelope } = await measured(overCap, tinyWinner);

    expect(calls).toBe(2);
    // The call still failed the way it failed, so the chain still advanced and answered.
    expect(envelope.code).toBe("ANSWERED");
    // But the impossible claim is not evidence, so the total is not exact and never
    // carries the over-cap number.
    expect(cost.method).not.toBe("exact");
    expect(cost.outputTokens).toBeLessThan(L.maxOutputTokensPerCall);
  });

  /**
   * The other branch: when every attempt reports usage the total is `exact`, and the
   * loser's billed output has to survive there too. It does because the chain folds the
   * loser's claim into the winner's reported usage, so `exact` reads it from there while
   * the estimate branch reads it from `billedFromFailedAttempts` - one path each, never
   * both.
   */
  it("counts a billed loser and an exact winner exactly once on the exact branch", async () => {
    const billedLoser = billedUnavailable(1, 7);
    const exactWinner = async (): Promise<Record<string, unknown>> => ({
      text: answerText,
      input_tokens: 9,
      output_tokens: 11,
      usage_exact: true,
    });
    const { calls, cost, envelope } = await measured(billedLoser, exactWinner);

    expect(calls).toBe(2);
    expect(envelope.code).toBe("ANSWERED");
    expect(cost.attemptsStarted).toBe(2);
    expect(cost.attemptsUsageComplete).toBe(2);
    expect(cost.method).toBe("exact");
    expect(cost.inputTokens).toBe(10);
    expect(cost.outputTokens).toBe(18);
  });

  it("still accepts a billed-failure claim that respects the per-call cap", async () => {
    const atCap = billedUnavailable(1, L.maxOutputTokensPerCall);
    const { cost } = await measured(atCap, plainUnavailable);
    // Exactly at the ceiling is legal, and two attempts reported it.
    expect(cost.outputTokens).toBe(L.maxOutputTokensPerCall * 2);
  });
});

/**
 * Every physical call is accounted for exactly once, whichever path notices it finished,
 * and every constituent claim is one a single call could legally have made.
 *
 * The ordinary success and failure paths were the first half of this rule. These are the
 * paths that bypassed them: a stable provider error re-read across the reader's outer
 * retry, a response that arrived after the deadline, a cancellation that raced the
 * provider, and a plain `ReaderProvider` whose usage no bridge ever bounded.
 */
describe("every physical attempt is accounted for once, on every path", () => {
  const answerText = answerJson("mode = fast [c1]", [
    { id: "c1", line_start: 1, line_end: 1, quote: "mode = fast" },
  ]);
  const identity = { provider: "plain", model: READER_MODEL };

  function billedError(inputTokens: number, outputTokens: number): ShuntError {
    const err = new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
    (err as { billedUsage?: unknown }).billedUsage = { inputTokens, outputTokens, method: "exact" };
    return err;
  }

  function fixture(provider: ConstructorParameters<typeof Reader>[1], clock?: FakeClock) {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("mode = fast\n")));
    return {
      reader: new Reader(registry, provider, L, clock),
      request: readRequest(entry, "What is the mode?"),
    };
  }

  /**
   * A provider may throw one stable `ShuntError` instance for every call it fails. The
   * chain used to write its aggregate onto that object and read the same field back on
   * the reader's next outer attempt, so its own earlier total arrived as fresh evidence:
   * four calls reporting 24/10 in total were published as 29/13.
   */
  it("does not re-ingest a stable provider error across the outer retry", async () => {
    let calls = 0;
    const raising = (err: ShuntError): HostBridgeProvider => {
      const call: HostBridgeCall = async () => {
        calls += 1;
        throw err;
      };
      return new HostBridgeProvider(call, L, READER_MODEL, "openai");
    };
    const chain = new FallbackChainProvider(raising(billedError(5, 3)), [
      raising(billedError(7, 2)),
    ]);
    const { reader, request } = fixture(chain);
    const { cost } = await reader.answerDetailed("sess", request);

    expect(calls).toBe(4);
    expect(cost.attemptsStarted).toBe(4);
    expect(cost.attemptsUsageComplete).toBe(4);
    expect(cost.method).toBe("exact");
    expect(cost.inputTokens).toBe(24);
    expect(cost.outputTokens).toBe(10);
  });

  /**
   * A response that arrives after the deadline is refused, but it was still paid for. The
   * late branch recorded only the winner's own usage, so the prompt every earlier
   * candidate re-sent and the output they reported disappeared.
   */
  it("charges a late composite response for every prompt and unseen token", async () => {
    const clock = new FakeClock();
    let calls = 0;
    let promptBytes = 0;
    const count = (body: (opts: { system: string; user: string }) => Promise<
      Record<string, unknown>
    >): HostBridgeCall => async (opts) => {
      calls += 1;
      promptBytes += enc(opts.system).length + enc(opts.user).length;
      return body(opts);
    };
    const failed = new HostBridgeProvider(count(async () => {
      throw billedError(5, 3);
    }), L, READER_MODEL, "openai");
    const late = new HostBridgeProvider(count(async () => {
      clock.advance(70_000);
      return { text: answerText };
    }), L, "fallback", "openai");
    const { reader, request } = fixture(new FallbackChainProvider(failed, [late]), clock);
    const { envelope, cost } = await reader.answerDetailed("sess", request);

    expect(envelope.code).toBe("TIMEOUT");
    expect(calls).toBe(2);
    expect(cost.attemptsStarted).toBe(2);
    expect(cost.inputTokens).toBe(Math.ceil(promptBytes / 4));
    expect(cost.outputTokens).toBe(Math.ceil(enc(answerText).length / 4) + 3);
  });

  /**
   * Cancellation can win the race against the provider that is already failing. The
   * winning arm carried none of the call's metadata, so a call billed 5/3 was reported as
   * zero usage-complete attempts and zero output.
   */
  it("keeps a billed attempt that cancellation raced", async () => {
    const clock = new FakeClock();
    const deadline = Deadline.start(clock, 60_000);
    let calls = 0;
    const first = new HostBridgeProvider(async () => {
      calls += 1;
      deadline.cancel();
      throw billedError(5, 3);
    }, L, READER_MODEL, "openai");
    const never = new HostBridgeProvider(async () => {
      calls += 1;
      throw billedError(7, 2);
    }, L, "fallback", "openai");
    const { reader, request } = fixture(new FallbackChainProvider(first, [never]), clock);
    const { envelope, cost } = await reader.answerDetailed("sess", request, deadline);

    expect(envelope.code).toBe("CANCELLED");
    // Cancelling the primary must still not start the fallback.
    expect(calls).toBe(1);
    expect(cost.attemptsStarted).toBe(1);
    expect(cost.attemptsUsageComplete).toBe(1);
    expect(cost.outputTokens).toBe(3);
  });

  /**
   * The chain accepts any `ReaderProvider`, so a constituent's claim may never have been
   * bounded anywhere. A plain provider's failed attempt claiming one token over the
   * per-call cap, plus a winner claiming one, stayed under the two-attempt aggregate
   * ceiling and was published as `exact`.
   */
  it("caps a plain provider's billed failure before the merge", async () => {
    const failing = {
      target: identity,
      async complete(): Promise<ModelResponse> {
        throw billedError(1, L.maxOutputTokensPerCall + 1);
      },
    };
    const winner = {
      target: identity,
      async complete(): Promise<ModelResponse> {
        return {
          text: answerText,
          requested: identity,
          resolved: identity,
          reported: identity,
          providerConfirmsGeneration: true,
          usage: { inputTokens: 1, outputTokens: 1, method: "exact" },
          fallbackUsed: false,
        };
      },
    };
    const { reader, request } = fixture(new FallbackChainProvider(failing, [winner]));
    const { envelope, cost } = await reader.answerDetailed("sess", request);

    // The call still failed the way it failed, so the chain still advanced and answered.
    expect(envelope.code).toBe("ANSWERED");
    expect(cost.method).not.toBe("exact");
    expect(cost.outputTokens).toBeLessThan(L.maxOutputTokensPerCall);
  });

  /** A winner is a constituent too, and an aggregate bound cannot vouch for it. */
  it("caps a plain provider that wins the fallback", async () => {
    const unavailable = {
      target: identity,
      async complete(): Promise<ModelResponse> {
        throw new ShuntError("MODEL_ERROR", "UNAVAILABLE", true);
      },
    };
    const overCap = {
      target: identity,
      async complete(): Promise<ModelResponse> {
        return {
          text: answerText,
          requested: identity,
          resolved: identity,
          reported: identity,
          providerConfirmsGeneration: true,
          usage: {
            inputTokens: 1,
            outputTokens: L.maxOutputTokensPerCall + 1,
            method: "exact",
          },
          fallbackUsed: false,
        };
      },
    };
    const { reader, request } = fixture(new FallbackChainProvider(unavailable, [overCap]));
    const { envelope } = await reader.answerDetailed("sess", request);

    expect(envelope.code).not.toBe("ANSWERED");
    expect(envelope.answer).toBe("");
  });

  /**
   * The other direction, and the reason the aggregate bound scales: two attempts of 1,500
   * output tokens are each legal and total 3,000 against a 2,048 per-call ceiling.
   * Re-applying the single-call ceiling to the sum refused a valid fallback outright.
   */
  it("accepts a legal sum that exceeds the single-call ceiling", async () => {
    const half = Math.floor(L.maxOutputTokensPerCall * 0.75);
    const failing: HostBridgeCall = async () => {
      throw billedError(1, half);
    };
    const winner: HostBridgeCall = async () => ({
      text: answerText,
      input_tokens: 1,
      output_tokens: half,
      usage_exact: true,
    });
    const chain = new FallbackChainProvider(
      new HostBridgeProvider(failing, L, READER_MODEL, "openai"),
      [new HostBridgeProvider(winner, L, "fallback", "openai")],
    );
    const { reader, request } = fixture(chain);
    const { envelope, cost } = await reader.answerDetailed("sess", request);

    expect(envelope.code).toBe("ANSWERED");
    expect(cost.method).toBe("exact");
    expect(cost.outputTokens).toBe(half * 2);
    expect(cost.outputTokens).toBeGreaterThan(L.maxOutputTokensPerCall);
  });
});
