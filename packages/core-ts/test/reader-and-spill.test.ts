/**
 * unit reader / bounded-output / cancellation / no-raw-leak / no-writes (TypeScript core).
 *
 * The spill conformance corpus is shared with the Python core; the rest asserts the same
 * invariants the Python gates assert, on this implementation.
 */
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, statSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { Coverage, buildEnvelope, serializedBytes } from "../src/envelope.js";
import { ShuntError } from "../src/errors.js";
import { OutputGuardError, enforce, enforceOrFixed } from "../src/guard.js";
import { DEFAULT_LIMITS as L, READER_MODEL } from "../src/limits.js";
import { InMemoryMetrics } from "../src/metrics.js";
import { Deadline, FakeClock } from "../src/clock.js";
import { HostBridgeProvider, UnavailableProvider } from "../src/provider.js";
import { Reader } from "../src/reader.js";
import { SourceRegistry } from "../src/registry.js";
import { JSON_MEDIA_TYPE, snapshotBytes } from "../src/snapshot.js";
import { SpillStore, SumaSpillEngine } from "../src/spill.js";
import { ShuntSession } from "../src/session.js";
import { conformance } from "./fixtures.js";
import { FakeLuna, answerJson, makeCapability, makeConfig } from "./support.js";

const enc = (s: string) => new TextEncoder().encode(s);
const SOURCE = 'import os\nmax_retries = 3\nbackoff = "exponential"\ntimeout_seconds = 30\n';
const QUESTION = "Where is the retry ceiling defined and what is it?";

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-ts-"));
}

function request(
  entry: { sourceId: string; snapshot: { snapshotId: string } },
  overrides: Record<string, unknown> = {},
) {
  return {
    schema_version: "1.0",
    request_id: "req_r1",
    operation: "read",
    question: QUESTION,
    sources: [
      {
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        selector: { kind: "all" },
      },
    ],
    budgets: { max_chunks: 8, max_answer_bytes: 8192, deadline_ms: 60000 },
    ...overrides,
  };
}

function fixture(reply?: string, content = SOURCE) {
  const registry = new SourceRegistry();
  const entry = registry.register("sess", snapshotBytes(enc(content)));
  const luna = new FakeLuna(reply === undefined ? [] : [reply]);
  return { registry, entry, luna, reader: new Reader(registry, luna) };
}

describe("reader gate", () => {
  it("makes zero model calls when the question is missing", async () => {
    const { entry, luna, reader } = fixture();
    const req = request(entry) as Record<string, unknown>;
    delete req["question"];
    const env = await reader.answer("sess", req);
    expect(luna.callCount).toBe(0);
    expect(env.status).toBe("error");
    expect(env.code).toBe("INVALID_REQUEST");
  });

  for (const question of ["", "   ", "\n\t "]) {
    it(`makes zero model calls for a blank question ${JSON.stringify(question)}`, async () => {
      const { entry, luna, reader } = fixture();
      const env = await reader.answer("sess", request(entry, { question }));
      expect(luna.callCount).toBe(0);
      expect(env.status).toBe("error");
    });
  }

  it("carries the original question and pins Luna on every call", async () => {
    const reply = answerJson("The retry ceiling is three [c1].", [
      { id: "c1", line_start: 2, line_end: 2, quote: "max_retries = 3" },
    ]);
    const { entry, luna, reader } = fixture(reply);
    const env = await reader.answer("sess", request(entry));
    expect(env.code).toBe("ANSWERED");
    expect(luna.callCount).toBe(1);
    expect(luna.calls[0]!.model).toBe(READER_MODEL);
    expect(luna.calls[0]!.user).toContain(QUESTION);
    expect(luna.calls[0]!.maxOutputTokens).toBeLessThanOrEqual(2048);
  });

  it("retries a transient failure exactly once, question intact", async () => {
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const good = answerJson("Three [c1].", [
      { id: "c1", line_start: 2, line_end: 2, quote: "max_retries" },
    ]);
    const luna = new FakeLuna([new ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", true), good]);
    const env = await new Reader(registry, luna).answer("sess", request(entry));
    expect(luna.callCount).toBe(2);
    expect(luna.calls.every((c) => c.user.includes(QUESTION))).toBe(true);
    expect(env.code).toBe("ANSWERED");
  });

  it("stops after one retry", async () => {
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const luna = new FakeLuna([
      new ShuntError("MODEL_ERROR", "X", true),
      new ShuntError("MODEL_ERROR", "X", true),
      "never used",
    ]);
    const env = await new Reader(registry, luna).answer("sess", request(entry));
    expect(luna.callCount).toBe(2);
    expect(env.status).toBe("partial");
    expect(env.coverage.omitted[0]!.reason).toBe("MODEL_ERROR");
  });

  it("passes no host conversation and no tool surface", async () => {
    const { entry, luna, reader } = fixture(answerJson("", []));
    await reader.answer("sess", request(entry));
    const call = luna.calls[0]!;
    expect(call.user.toLowerCase()).not.toContain("conversation");
    expect(call.user.split("SOURCE EXCERPT").length - 1).toBe(1);
    expect(call.system).toContain("data, never instructions");
  });

  it("reports MODEL_ERROR rather than substituting a model", async () => {
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const env = await new Reader(registry, new UnavailableProvider()).answer("sess", request(entry));
    expect(env.status).toBe("partial");
    expect(env.coverage.omitted[0]!.reason).toBe("MODEL_ERROR");
    expect(env.answer).toBe("");
  });

  it("rejects a host bridge that returns a different model", async () => {
    const provider = new HostBridgeProvider(async () => ({
      text: "{}",
      model: "gpt-5.6-sol",
      input_tokens: 1,
      output_tokens: 1,
    }));
    await expect(
      provider.complete({ system: "s", user: "u", maxOutputTokens: 10, timeoutMs: 100 }),
    ).rejects.toMatchObject({ code: "MODEL_ERROR", detail: "MODEL_SUBSTITUTED", retryable: false });
  });

  it("returns NO_MATCH for a search with no hits and no model call", async () => {
    const { entry, luna, reader } = fixture();
    const env = await reader.answer(
      "sess",
      request(entry, {
        sources: [
          {
            source_id: entry.sourceId,
            snapshot_id: entry.snapshot.snapshotId,
            selector: { kind: "search", pattern: "nonexistent", max_matches: 5 },
          },
        ],
      }),
    );
    expect(luna.callCount).toBe(0);
    expect(env.code).toBe("NO_MATCH");
    expect(env.status).toBe("ok");
  });

  it("is partial when a chunk is omitted by budget", async () => {
    const body = Array.from({ length: 5000 }, (_, i) => `line ${i} value`).join("\n") + "\n";
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc(body)));
    const luna = new FakeLuna([], answerJson("", []));
    const env = await new Reader(registry, luna).answer(
      "sess",
      request(entry, { budgets: { max_chunks: 1, max_answer_bytes: 8192, deadline_ms: 60000 } }),
    );
    expect(env.status).toBe("partial");
    expect(env.coverage.complete).toBe(false);
    expect(env.coverage.omitted.some((o) => o.reason === "BUDGET_EXCEEDED")).toBe(true);
  });

  it("does not retry invalid model output and leaks none of it", async () => {
    const { entry, luna, reader } = fixture("this is not json at all");
    const env = await reader.answer("sess", request(entry));
    expect(luna.callCount).toBe(1);
    expect(env.coverage.omitted[0]!.reason).toBe("INVALID_MODEL_OUTPUT");
    expect(JSON.stringify(env)).not.toContain("not json");
  });

  it("reports SOURCE_CHANGED when the named snapshot no longer matches", async () => {
    const { entry, luna, reader } = fixture();
    const req = request(entry) as any;
    req.sources[0].snapshot_id = "sha256:" + "0".repeat(64);
    const env = await reader.answer("sess", req);
    expect(luna.callCount).toBe(0);
    expect(env.code).toBe("SOURCE_CHANGED");
  });

  it("answers a JSON source with record citations", async () => {
    const doc = JSON.stringify({ items: [{ name: "alpha", retries: 1 }, { name: "beta", retries: 3 }] });
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc(doc), JSON_MEDIA_TYPE));
    const reply = answerJson("beta retries three times [c1].", [
      { id: "c1", record_start: 2, record_end: 2, quote: '"name":"beta"' },
    ]);
    const env = await new Reader(registry, new FakeLuna([reply])).answer(
      "sess",
      request(entry, {
        sources: [
          {
            source_id: entry.sourceId,
            snapshot_id: entry.snapshot.snapshotId,
            selector: { kind: "records", pointer: "/items", start: 2, end: 2 },
          },
        ],
      }),
    );
    expect(env.code).toBe("ANSWERED");
    expect(env.citations[0]!.locator["kind"]).toBe("records");
  });
});

// -- bounded output / spill --------------------------------------------------

function buildResult(spec: any): unknown {
  if (spec.kind === "string") {
    return spec.repeat ? spec.repeat.unit.repeat(spec.repeat.bytes) : spec.value;
  }
  if (spec.kind === "json") {
    if (spec.generate) return generate(spec.generate);
    const value = { ...spec.value };
    if (spec.pad_bytes) value["_pad"] = "p".repeat(spec.pad_bytes);
    return value;
  }
  if (spec.kind === "blocks") {
    return {
      content: spec.blocks.map((b: any) => {
        const entry: Record<string, unknown> = { type: b.type };
        if (b.media_type) entry["media_type"] = b.media_type;
        entry[b.type === "text" ? "text" : "data"] = b.repeat
          ? b.repeat.unit.repeat(b.repeat.bytes)
          : b.value;
        return entry;
      }),
    };
  }
  if (spec.kind === "internal_envelope") {
    return { marker: spec.repeat.unit.repeat(spec.repeat.bytes) };
  }
  throw new Error(`unknown result kind ${spec.kind}`);
}

function generate(spec: any): unknown {
  switch (spec.shape) {
    case "object":
      return Object.fromEntries(
        Array.from({ length: spec.entries }, (_, i) => [`k${i}`, `value-${i}`]),
      );
    case "array":
      return Array.from({ length: spec.entries }, (_, i) => `value-${i}`);
    case "nested":
      return { rows: Array.from({ length: spec.entries }, (_, i) => ({ id: i, tags: ["a", "b"] })) };
    case "deep": {
      let node: unknown = "leaf";
      for (let i = 0; i < spec.depth - 1; i += 1) node = { n: node };
      return node;
    }
    case "cycle": {
      const node: Record<string, unknown> = {};
      node["self"] = node;
      return node;
    }
    case "unserializable":
      return { when: () => 1 };
    default:
      throw new Error(spec.shape);
  }
}

describe("suma spill conformance", () => {
  const spillCases = conformance("spill-cases.json");

  it("has a non-empty corpus", () => {
    expect(spillCases.cases.length).toBeGreaterThanOrEqual(20);
  });

  for (const c of spillCases.cases) {
    it(`handles ${c.id}`, () => {
      const dir = tmp();
      const registry = new SourceRegistry();
      const store = new SpillStore(join(dir, "cache"));
      if (c.session_spill_used_bytes !== undefined) {
        store.seedUsage("sess", c.session_spill_used_bytes);
      }
      if (c.inject === "write_failure") {
        store.write = () => {
          throw new ShuntError("SPILL_FAILED", "WRITE_FAILED", false);
        };
      }
      if (c.inject === "readback_mismatch") {
        store.write = () => {
          throw new ShuntError("SPILL_FAILED", "READBACK_MISMATCH", false);
        };
      }
      const engine = new SumaSpillEngine(store, registry, undefined, true);
      let internalId: string | undefined;
      if (c.result.kind === "internal_envelope") {
        internalId = registry.register("sess", snapshotBytes(enc("internal")), true).sourceId;
      }
      const outcome = engine.evaluate("sess", "req_s", buildResult(c.result), internalId);
      expect(outcome.action).toBe(c.expect.action);
      if (c.expect.code) expect(outcome.code).toBe(c.expect.code);
    });
  }

  it("never calls a model and never summarizes", () => {
    const engine = new SumaSpillEngine(new SpillStore(join(tmp(), "cache")), new SourceRegistry(), undefined, true);
    const outcome = engine.evaluate("sess", "req_s", "x".repeat(40000));
    expect(outcome.action).toBe("spill");
    expect(outcome.envelope!.answer).toBe("");
    expect(outcome.envelope!.citations).toEqual([]);
  });

  it("keeps the pointer envelope under the cap for any payload size", () => {
    const engine = new SumaSpillEngine(new SpillStore(join(tmp(), "cache")), new SourceRegistry(), undefined, true);
    const outcome = engine.evaluate("sess", "req_s", "y".repeat(4_000_000));
    const env = enforce(outcome.envelope);
    expect(serializedBytes(env)).toBeLessThanOrEqual(L.maxEnvelopeBytes);
    expect(env.pointer!.bytes).toBe(4_000_000);
  });

  it("spills strings, objects and arrays alike", () => {
    const engine = new SumaSpillEngine(new SpillStore(join(tmp(), "cache")), new SourceRegistry(), undefined, true);
    for (const payload of ["s".repeat(40000), { k: "v".repeat(40000) }, ["item".repeat(4000), "item".repeat(4000), "item".repeat(4000), "item".repeat(4000)]]) {
      const outcome = engine.evaluate("sess", "req_s", payload);
      expect(outcome.action).toBe("spill");
      expect(outcome.envelope!.answer).toBe("");
    }
  });

  it("contains hostile serialization and store exceptions without leaking them", () => {
    const secret = "SENTINEL-SPILL-EXCEPTION-31dce2";

    const serializationRegistry = new SourceRegistry();
    const hostile = new Proxy({}, {
      get() {
        throw new Error(secret);
      },
    });
    const serializationOutcome = new SumaSpillEngine(
      new SpillStore(join(tmp(), "cache")),
      serializationRegistry,
      undefined,
      true,
    ).evaluate("sess", "req_s", hostile);
    expect(serializationOutcome).toMatchObject({ action: "error", code: "SPILL_FAILED" });
    expect(JSON.stringify(serializationOutcome)).not.toContain(secret);
    expect(serializationRegistry.count("sess")).toBe(0);

    const storeRegistry = new SourceRegistry();
    const store = new SpillStore(join(tmp(), "cache"));
    store.write = () => {
      throw new Error(secret);
    };
    const storeOutcome = new SumaSpillEngine(store, storeRegistry, undefined, true)
      .evaluate("sess", "req_s", "x".repeat(40_000));
    expect(storeOutcome).toMatchObject({ action: "error", code: "SPILL_FAILED" });
    expect(JSON.stringify(storeOutcome)).not.toContain(secret);
    expect(storeRegistry.count("sess")).toBe(0);
  });

  it("writes private files and enforces the session quota", () => {
    const store = new SpillStore(join(tmp(), "cache"));
    expect(() => statSync(store.root)).toThrow();
    const written = store.write("sess", enc("payload bytes"));
    expect(statSync(store.root).mode & 0o777).toBe(0o700);
    expect(statSync(written).mode & 0o777).toBe(0o600);
    expect(readdirSync(store.root).length).toBe(1);
    store.seedUsage("sess", L.sessionSpillQuotaBytes);
    expect(() => store.write("sess", enc("one more"))).toThrowError(ShuntError);
  });

  it("purges a session's artifacts", () => {
    const store = new SpillStore(join(tmp(), "cache"));
    store.write("sess", enc("payload"));
    expect(store.purgeSession("sess")).toBe(1);
  });
});

describe("output guard", () => {
  const citation = () => ({
    id: "c1",
    source_id: "src_abcd1234",
    snapshot_id: "sha256:" + "a".repeat(64),
    locator: { kind: "lines", start: 1, end: 1 },
    quote: "alpha",
    verified: true as const,
  });

  const base = () => {
    const coverage = new Coverage();
    coverage.complete = true;
    coverage.processedChunks = 1;
    coverage.plannedChunks = 1;
    coverage.upstreamTruncated = false;
    return buildEnvelope({
      requestId: "req_1",
      status: "ok",
      code: "ANSWERED",
      answer: "a [c1]",
      citations: [citation()],
      coverage,
    });
  };

  it("enforces answer, quote and citation caps", () => {
    expect(() => enforce(base())).not.toThrow();
    expect(() => enforce({ ...base(), answer: "x".repeat(L.maxAnswerBytes + 1) })).toThrowError(OutputGuardError);
    expect(() =>
      enforce({ ...base(), citations: [{ ...citation(), quote: "q".repeat(L.maxQuoteBytes + 1) }] }),
    ).toThrowError(OutputGuardError);
    expect(() =>
      enforce({ ...base(), citations: Array.from({ length: L.maxCitations + 1 }, citation) }),
    ).toThrowError(OutputGuardError);
  });

  it("never lets an unverified citation out", () => {
    const env = { ...base(), citations: [{ ...citation(), verified: false }] };
    expect(enforceOrFixed(env).code).toBe("LIMIT_EXCEEDED");
  });

  it("emits a fixed envelope instead of a poisoned one", () => {
    const poisoned = { ...base(), raw: "SENTINEL-RAW-PAYLOAD" } as Record<string, unknown>;
    const guarded = enforceOrFixed(poisoned);
    expect(JSON.stringify(guarded)).not.toContain("SENTINEL-RAW-PAYLOAD");
    expect(guarded.status).toBe("error");
  });

  it("caps the reader answer to the requested budget", async () => {
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc("alpha value here\n")));
    const reply = answerJson("Alpha is present [c1]. ".repeat(2000), [
      { id: "c1", line_start: 1, line_end: 1, quote: "alpha" },
    ]);
    const env = await new Reader(registry, new FakeLuna([], reply)).answer(
      "sess",
      request(entry, { budgets: { max_chunks: 8, max_answer_bytes: 512, deadline_ms: 60000 } }),
    );
    expect(new TextEncoder().encode(env.answer).length).toBeLessThanOrEqual(512);
    expect(() => enforce(env)).not.toThrow();
  });

  it("pins the contract caps", () => {
    expect(L.maxConcurrentModelCalls).toBe(2);
    expect(L.maxChunksPerRequest).toBe(8);
    expect(L.maxRequestInputTokens).toBe(64000);
    expect(L.maxOutputTokensPerCall).toBe(2048);
    expect(L.maxChunkTokens).toBe(8000);
  });
});

// -- cancellation ------------------------------------------------------------

describe("cancellation and deadlines", () => {
  it("pins the contract deadlines", () => {
    expect([L.gateProbeDeadlineMs, L.spillIoDeadlineMs]).toEqual([1000, 5000]);
    expect([L.modelCallDeadlineMs, L.requestDeadlineMs]).toEqual([20000, 60000]);
  });

  it("expires and cancels", () => {
    const clock = new FakeClock();
    const deadline = Deadline.start(clock, 60000);
    deadline.check("STAGE");
    clock.advance(60001);
    expect(() => deadline.check("STAGE")).toThrowError(ShuntError);
    const other = Deadline.start(new FakeClock(), 60000);
    other.cancel();
    // Cancellation beats an unexpired deadline.
    expect(() => other.check("STAGE")).toThrowError(ShuntError);
    try {
      other.check("STAGE");
    } catch (err) {
      expect((err as ShuntError).code).toBe("CANCELLED");
    }
  });

  it("never lets a stage outlive the request budget", () => {
    const clock = new FakeClock();
    const deadline = Deadline.start(clock, 60000);
    clock.advance(55000);
    expect(deadline.subBudget(L.modelCallDeadlineMs)).toBe(5000);
  });

  it("makes zero provider calls when cancelled while queued", async () => {
    const clock = new FakeClock();
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const luna = new FakeLuna([], answerJson("", []));
    const deadline = Deadline.start(clock, L.requestDeadlineMs);
    deadline.cancel();
    const env = await new Reader(registry, luna, undefined, clock).answer("sess", request(entry), deadline);
    expect(luna.callCount).toBe(0);
    expect(env.code).toBe("CANCELLED");
  });

  it("publishes nothing once the budget is spent", async () => {
    const clock = new FakeClock();
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const slow: any = {
      async complete() {
        clock.advance(61000);
        return {
          text: answerJson("max_retries is three [c1]", [
            { id: "c1", line_start: 2, line_end: 2, quote: "max_retries" },
          ]),
          model: READER_MODEL,
          usage: { inputTokens: 0, outputTokens: 0, estimated: true },
        };
      },
    };
    const env = await new Reader(registry, slow, undefined, clock).answer("sess", request(entry));
    expect(env.answer).toBe("");
    expect(env.coverage.complete).toBe(false);
  });
});

// -- no raw leak / no writes -------------------------------------------------

describe("no raw leak and no writes", () => {
  const HEAD = "SENTINEL-HEAD-9f2a1c";
  const MID = "SENTINEL-MID-4b7e33";

  const payload = () =>
    HEAD + "\n" + Array.from({ length: 3000 }, (_, i) => `filler line ${i}`).join("\n") + "\n" + MID + "\n";

  it("returns no sentinel on any reader failure", async () => {
    const registry = new SourceRegistry();
    const entry = registry.register("sess", snapshotBytes(enc(payload())));
    const metrics = new InMemoryMetrics();
    const bridge = new HostBridgeProvider(async () => {
      throw new Error(`upstream rejected prompt containing ${MID}`);
    });
    const env = await new Reader(registry, bridge, undefined, undefined, metrics).answer(
      "sess",
      request(entry),
    );
    for (const blob of [JSON.stringify(env), metrics.rendered()]) {
      expect(blob).not.toContain(HEAD);
      expect(blob).not.toContain(MID);
    }
  });

  it("drops the provider error body at the boundary", async () => {
    const provider = new HostBridgeProvider(async () => {
      throw new Error(`HTTP 500 body: ${HEAD}`);
    });
    await expect(
      provider.complete({ system: "s", user: "u", maxOutputTokens: 10, timeoutMs: 10 }),
    ).rejects.toSatisfy((err: unknown) => {
      const shunt = err as ShuntError;
      return shunt.code === "MODEL_ERROR" && !shunt.message.includes(HEAD);
    });
  });

  it("refuses free text in an error detail", () => {
    expect(() => new ShuntError("MODEL_ERROR", `failed on ${HEAD}`)).toThrow();
  });

  it("rejects content in metric labels", () => {
    const metrics = new InMemoryMetrics();
    expect(() => metrics.count("gate_decision", { path: HEAD })).toThrow();
    expect(() => metrics.count("gate_decision", { reason: `${HEAD} ${MID}` })).toThrow();
    expect(() => metrics.count("gate_decision", { request_id: "req_x" })).toThrow();
  });

  it("blocks an oversized read and leaves the source tree byte identical", async () => {
    const dir = tmp();
    mkdirSync(join(dir, "ws"), { recursive: true });
    const big = join(dir, "ws", "big.txt");
    writeFileSync(big, payload());
    const small = join(dir, "ws", "small.txt");
    writeFileSync(small, "alpha\nbeta\n");
    const before = readdirSync(join(dir, "ws")).map((n) => [n, statSync(join(dir, "ws", n)).size]);

    const session = new ShuntSession(join("sess"), makeConfig(dir), makeCapability(), {
      provider: new FakeLuna([], answerJson("The first line is alpha [c1].", [
        { id: "c1", line_start: 1, line_end: 1, quote: "alpha" },
      ])),
    });
    const decision = session.evaluateToolCall("read", { file_path: big });
    expect(decision.decision).toBe("blocked");
    const blockEnv = session.blockEnvelope("req_leak", decision);
    expect(JSON.stringify(blockEnv)).not.toContain(HEAD);
    expect(JSON.stringify(blockEnv)).not.toContain(big);

    const entry = session.registerPath(small);
    const env = await session.read(request(entry));
    expect(env.code).toBe("ANSWERED");
    session.close();
    expect(readdirSync(join(dir, "ws")).map((n) => [n, statSync(join(dir, "ws", n)).size])).toEqual(before);
  });

  it("succeeds against a read-only source", async () => {
    const dir = tmp();
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", "readonly.txt");
    writeFileSync(path, "max_retries = 3\n");
    chmodSync(path, 0o444);
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider: new FakeLuna([], answerJson("The ceiling is three [c1].", [
        { id: "c1", line_start: 1, line_end: 1, quote: "max_retries = 3" },
      ])),
    });
    const env = await session.read(request(session.registerPath(path)));
    expect(env.code).toBe("ANSWERED");
    expect(statSync(path).mode & 0o777).toBe(0o444);
    chmodSync(path, 0o644);
  });
});
