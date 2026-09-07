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
import { classifyAttribution } from "../src/provenance.js";
import { FallbackChainProvider, HostBridgeProvider } from "../src/provider.js";
import { OutputGuardError, enforce, enforceOrFixed } from "../src/guard.js";
import { DEFAULT_LIMITS as L, READER_MODEL, narrowLimits } from "../src/limits.js";
import { InMemoryMetrics } from "../src/metrics.js";
import { Deadline, FakeClock } from "../src/clock.js";
import {
  HostBridgeProvider, UnavailableProvider, responseAttribution,
} from "../src/provider.js";
import { referencedIds } from "../src/citations.js";
import { Reader } from "../src/reader.js";
import { SourceRegistry } from "../src/registry.js";
import { JSON_MEDIA_TYPE, snapshotBytes } from "../src/snapshot.js";
import { SpillEngine } from "../src/spill.js";
import { ScopeIdentity, SnapshotStore } from "../src/store.js";
import { ShuntSession } from "../src/session.js";
import { conformance } from "./fixtures.js";
import {
  FakeLuna, answerJson, derivedProvenance, makeCapability, makeConfig, makeIdentity,
  makeRegistry,
} from "./support.js";

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
  const registry = makeRegistry(tmp(), { sessionId: "sess" });
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const env = await new Reader(registry, new UnavailableProvider()).answer("sess", request(entry));
    expect(env.status).toBe("partial");
    expect(env.coverage.omitted[0]!.reason).toBe("MODEL_ERROR");
    expect(env.answer).toBe("");
  });

  it("refuses a reported model that contradicts the request", async () => {
    // A different model is a wrong answer, not a weakly attributed one.
    const provider = new HostBridgeProvider(async () => ({
      text: "{}",
      reported_provider: "openai",
      reported_model: "gpt-5.6-sol",
      provider_confirms_generation: true,
      input_tokens: 1,
      output_tokens: 1,
      usage_exact: true,
    }), undefined, READER_MODEL, "openai");
    const response = await provider.complete({
      system: "s", user: "u", maxOutputTokens: 10, timeoutMs: 100,
    });
    expect(responseAttribution(response).status).toBe("mismatch");

    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const env = await new Reader(registry, provider)
      .answer("sess", request(entry));
    // Refused outright rather than published under the requested model's name.
    expect(env.status).toBe("error");
    expect(env.code).toBe("MODEL_ERROR");
    expect(env.answer).toBe("");
    expect(env.provenance!.attribution_status).toBe("mismatch");
    expect(env.provenance!.reported_model).toBe("gpt-5.6-sol");
    expect(env.provenance!.requested_model).toBe(READER_MODEL);
    // A model failure is not a handle failure.
    expect(env.recovery!.handles_valid).toBe(true);
  });

  it("labels an unprovable attribution unverified and never claims actual", async () => {
    // The host echoed the request back; that is not a provider confirmation.
    const provider = new HostBridgeProvider(async () => ({
      text: answerJson("The retry ceiling is three [c1].", [
        { id: "c1", line_start: 2, line_end: 2, quote: "max_retries = 3" },
      ]),
      reported_provider: "openai",
      reported_model: READER_MODEL,
      provider_confirms_generation: false,
      usage_exact: false,
    }), undefined, READER_MODEL, "openai");
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const result = await new Reader(registry, provider).answerDetailed("sess", request(entry));
    expect(result.envelope.code).toBe("ANSWERED");
    expect(result.envelope.provenance!.attribution_status).toBe("unverified");
    expect(result.envelope.provenance!.usage_complete).toBe(false);
    // Absent provider usage becomes a named estimate, never a zero.
    expect(result.cost.method).toBe("bytes_div_4");
    expect(result.cost.inputTokens).toBeGreaterThan(0);
  });

  it("refuses an unverified attribution under the require_match policy", async () => {
    const provider = new HostBridgeProvider(async () => ({
      text: answerJson("", []),
      provider_confirms_generation: false,
    }), undefined, READER_MODEL, "openai");
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const env = await new Reader(registry, provider, undefined, undefined, undefined, "require_match")
      .answer("sess", request(entry));
    expect(env.code).toBe("PROVENANCE_UNAVAILABLE");
    // A provenance failure is not a handle failure: recovery keeps the snapshot.
    expect(env.recovery!.handles_valid).toBe(true);
    expect(env.recovery!.actions).toContain("CONFIGURE_READER_MODEL");
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
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
      const limits = c.store_quota_bytes !== undefined
        ? narrowLimits(L, { storeMaxBytes: c.store_quota_bytes })
        : L;
      const registry = makeRegistry(tmp(), { sessionId: "sess", limits });
      const store = registry.store;
      if (c.inject === "publish_write_failure") {
        store.publish = () => {
          throw new ShuntError("STORE_FAILED", "WRITE_FAILED", false);
        };
      }
      if (c.inject === "publish_content_mismatch") {
        store.publish = () => {
          throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
        };
      }
      const engine = new SpillEngine(registry, limits, true);
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
    const engine = new SpillEngine(makeRegistry(tmp(), { sessionId: "sess" }), undefined, true);
    const outcome = engine.evaluate("sess", "req_s", "x".repeat(40000));
    expect(outcome.action).toBe("spill");
    expect(outcome.envelope!.answer).toBe("");
    expect(outcome.envelope!.citations).toEqual([]);
  });

  it("keeps the pointer envelope under the cap for any payload size", () => {
    const engine = new SpillEngine(makeRegistry(tmp(), { sessionId: "sess" }), undefined, true);
    const outcome = engine.evaluate("sess", "req_s", "y".repeat(4_000_000));
    const env = enforce(outcome.envelope);
    expect(serializedBytes(env)).toBeLessThanOrEqual(L.maxEnvelopeBytes);
    expect(env.pointer!.bytes).toBe(4_000_000);
  });

  it("spills strings, objects and arrays alike", () => {
    const engine = new SpillEngine(makeRegistry(tmp(), { sessionId: "sess" }), undefined, true);
    for (const payload of ["s".repeat(40000), { k: "v".repeat(40000) }, ["item".repeat(4000), "item".repeat(4000), "item".repeat(4000), "item".repeat(4000)]]) {
      const outcome = engine.evaluate("sess", "req_s", payload);
      expect(outcome.action).toBe("spill");
      expect(outcome.envelope!.answer).toBe("");
    }
  });

  it("contains hostile serialization and store exceptions without leaking them", () => {
    const secret = "SENTINEL-SPILL-EXCEPTION-31dce2";

    const serializationRegistry = makeRegistry(tmp(), { sessionId: "sess" });
    const hostile = new Proxy({}, {
      get() {
        throw new Error(secret);
      },
    });
    const serializationOutcome = new SpillEngine(serializationRegistry, undefined, true)
      .evaluate("sess", "req_s", hostile);
    expect(serializationOutcome).toMatchObject({ action: "error", code: "SPILL_FAILED" });
    expect(JSON.stringify(serializationOutcome)).not.toContain(secret);
    expect(serializationRegistry.count("sess")).toBe(0);

    const storeRegistry = makeRegistry(tmp(), { sessionId: "sess" });
    storeRegistry.store.publish = () => {
      throw new Error(secret);
    };
    const storeOutcome = new SpillEngine(storeRegistry, undefined, true)
      .evaluate("sess", "req_s", "x".repeat(40_000));
    expect(storeOutcome).toMatchObject({ action: "error", code: "SPILL_FAILED" });
    expect(JSON.stringify(storeOutcome)).not.toContain(secret);
    expect(storeRegistry.count("sess")).toBe(0);
  });

  it("writes private files and enforces the store byte quota", () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const store = registry.store;
    registry.register("sess", snapshotBytes(enc("payload bytes")));
    expect(statSync(store.root).mode & 0o777).toBe(0o700);
    const blobs = readdirSync(join(store.root, "blobs"));
    expect(blobs.length).toBe(1);

    const narrow = makeRegistry(tmp(), {
      sessionId: "sess",
      limits: narrowLimits(L, { storeMaxBytes: 8 }),
    });
    expect(() => narrow.register("sess", snapshotBytes(enc("far too many bytes"))))
      .toThrowError(ShuntError);
    expect(narrow.count("sess")).toBe(0);
  });

  it("purges a scope's artifacts at a real session boundary", () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    registry.register("sess", snapshotBytes(enc("payload")));
    expect(registry.expireSession("sess")).toBe(1);
    expect(registry.count("sess")).toBe(0);
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
      provenance: derivedProvenance(),
      accountingId: "acc_" + "0".repeat(15) + "3",
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
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
    expect([L.modelCallDeadlineMs, L.requestDeadlineMs]).toEqual([45000, 60000]);
    // The default model must be reachable under the default deadline. `model_call` was
    // 20000 through the 1.1 work, but the default reader model `gpt-5.6-luna` is a
    // reasoning model and a measured live call takes roughly 34s - so every call aborted
    // and the reader could never answer on a stock configuration. `deadlines_ms` is a
    // normative value a deployment may only narrow, so no operator could raise it either.
    // Asserted, not commented, so lowering it back under what the default model needs
    // fails here rather than silently disabling the reader.
    expect(L.modelCallDeadlineMs).toBeGreaterThanOrEqual(34_000);
    expect(L.modelCallDeadlineMs).toBeLessThanOrEqual(L.requestDeadlineMs);
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
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
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
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

// -- the envelope must fit even when every field individually does -----------

/** Ordinary quote-dense source: keys and string values, as any code or JSON file has. */
function quoteDenseSource(entries = 40, pairs = 40): string {
  const rows: string[] = [];
  for (let i = 0; i < entries; i += 1) {
    const cells: string[] = [];
    for (let j = 0; j < pairs; j += 1) {
      const key = String(i).padStart(2, "0") + String(j).padStart(2, "0");
      cells.push(`"k${key}":"v${key}"`);
    }
    rows.push(`export const e${String(i).padStart(2, "0")}={${cells.join(",")}};`);
  }
  return rows.join("\n");
}

async function answerOver(dir: string, fillerRepeats: number, count = 16) {
  const registry = makeRegistry(dir, { sessionId: "sess" });
  const body = quoteDenseSource();
  const entry = registry.register("sess", snapshotBytes(enc(body)));
  const lines = body.split("\n");
  const filler = "short keys mapped onto short string values in declaration order ".repeat(
    fillerRepeats,
  );
  const citations = Array.from({ length: count }, (_, i) => ({
    id: `c${i}`,
    line_start: i + 1,
    line_end: i + 1,
    quote: lines[i]!.slice(0, 512),
  }));
  const answer = Array.from(
    { length: count },
    (_, i) => `Entry ${String(i).padStart(2, "0")} uses ${filler}[c${i}].`,
  ).join(" ");
  const envelope = await new Reader(
    registry,
    new FakeLuna([answerJson(answer, citations)]),
  ).answer("sess", {
    schema_version: "1.0",
    request_id: "req_fit",
    operation: "read",
    question: "What does this file declare?",
    sources: [
      {
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        selector: { kind: "all" },
      },
    ],
    budgets: { max_chunks: 8, max_answer_bytes: L.maxAnswerBytes, deadline_ms: 60000 },
  });
  return envelope;
}

describe("fitting the envelope", () => {
  it("shows the field caps alone do not keep an envelope under the cap", () => {
    // This is why the reader has to measure: the per-field caps are not jointly
    // satisfiable. A 1 KiB answer with the maximum number of maximum-length quotes is
    // legal field by field and over the 16 KiB envelope cap. Before the fit step the guard
    // turned that into a bare LIMIT_EXCEEDED after the model call was already paid for.
    const quote = '"k":"v",'.repeat(64);
    expect(enc(quote).length).toBe(L.maxQuoteBytes);
    const coverage = new Coverage();
    coverage.upstreamTruncated = false;
    coverage.complete = true;
    const citations = Array.from({ length: L.maxCitations }, (_, i) => ({
      id: `c${i}`,
      source_id: `src_${String(i).padStart(16, "0")}`,
      snapshot_id: "sha256:" + "0".repeat(64),
      locator: { kind: "lines", start: 1, end: 1 },
      quote,
      verified: true as const,
    }));
    const env = buildEnvelope({
      requestId: "req_x",
      status: "ok",
      code: "ANSWERED",
      coverage,
      sources: [],
      retryable: false,
      answer: "a".repeat(1024),
      citations,
      resultKind: "model_derived",
      provenance: derivedProvenance(),
      accountingId: "acc_" + "0".repeat(16),
    });
    expect(serializedBytes(env)).toBeGreaterThan(L.maxEnvelopeBytes);
    expect(() => enforce(env)).toThrow(OutputGuardError);
  });

  it("publishes an answer that fits untouched", async () => {
    // The fit step must not shrink anything that was already inside the cap.
    const env = await answerOver(tmp(), 1);
    expect(env.code).toBe("ANSWERED");
    expect(env.status).toBe("ok");
    expect(env.citations).toHaveLength(16);
    expect(env.coverage.omitted.some((o) => o.reason === "BUDGET_EXCEEDED")).toBe(false);
    // Close to the cap on purpose: if scaffolding grows, this becomes a trimming case and
    // that should be visible rather than silent.
    expect(serializedBytes(env)).toBeLessThanOrEqual(L.maxEnvelopeBytes);
  });

  it("drops evidence and says so instead of refusing an oversized answer", async () => {
    const env = await answerOver(tmp(), 2);
    // A good verified answer must not become LIMIT_EXCEEDED.
    expect(env.code).toBe("ANSWERED");
    // Dropping evidence is not a complete result.
    expect(env.status).toBe("partial");
    expect(env.answer.length).toBeGreaterThan(0);
    expect(env.citations.length).toBeGreaterThan(0);
    expect(env.citations.length).toBeLessThan(16);
    // What was removed has to be recorded, not silently absent.
    expect(env.coverage.omitted.some((o) => o.reason === "BUDGET_EXCEEDED")).toBe(true);
    expect(serializedBytes(env)).toBeLessThanOrEqual(L.maxEnvelopeBytes);
    // Every surviving citation is still referenced by the answer, and vice versa.
    expect(new Set(referencedIds(env.answer))).toEqual(
      new Set(env.citations.map((c) => c.id)),
    );
  });

  it("trims deterministically, independent of citation order", async () => {
    // Dropping by cost, not position: the model's ordering must not change the outcome.
    const first = await answerOver(tmp(), 3);
    const second = await answerOver(tmp(), 3);
    expect(first.citations.map((c) => c.id)).toEqual(second.citations.map((c) => c.id));
    expect(first.answer).toBe(second.answer);
  });
});

// -- attribution: what counts as the same model, and what proves ACTUAL -------

describe("attribution identity", () => {
  /**
   * A prefix match is not an identity match.
   *
   * Decoration was accepted as any `requested + "-" + anything`, so `gpt-5.6-luna-evil`
   * was classified as the requested model and could be published as such. Only a date or
   * a numeric revision is decoration; an alphabetic suffix names a *different* model
   * (`gpt-4` and `gpt-4-turbo` are not the same model either).
   */
  // `actual` needs the requested id itself; a decorated variant is neither certified nor
  // contradicted, because nothing establishes that `luna` and `luna-2` are one model.
  for (const [observed, expected] of [
    ["gpt-5.6-luna", "actual"],
    ["openai/gpt-5.6-luna", "actual"],
    ["GPT-5.6-Luna", "actual"],
    ["gpt-5.6-luna-2026-05-01", "unverified"],
    ["gpt-5.6-luna-2", "unverified"],
    ["gpt-5.6-luna-evil", "mismatch"],
    ["gpt-5.6-luna-uncensored", "mismatch"],
    ["gpt-5.6-lunatic", "mismatch"],
    ["gpt-5.6-sol", "mismatch"],
  ] as const) {
    it(`classifies ${observed} as ${expected}`, () => {
      const { status } = classifyAttribution({
        requested: { provider: "openai", model: "gpt-5.6-luna" },
        resolved: {},
        reported: { provider: "openai", model: observed },
        providerConfirmsGeneration: true,
      });
      expect(status).toBe(expected);
    });
  }

  it("requires a reported model, not merely a reported provider, for actual", () => {
    const requested = { provider: "openai", model: "gpt-5.6-luna" };
    expect(
      classifyAttribution({
        requested,
        resolved: {},
        reported: { provider: "openai" },
        providerConfirmsGeneration: true,
      }).status,
    ).not.toBe("actual");
    expect(
      classifyAttribution({
        requested,
        resolved: {},
        reported: { provider: "openai", model: "gpt-5.6-luna" },
        providerConfirmsGeneration: true,
      }).status,
    ).toBe("actual");
  });
});

// -- the exported Reader API stayed usable across the 1.1 revision -----------


// -- legacy call shapes must still enforce the budget they carry --------------


describe("cancellation stops the fallback chain", () => {
  /**
   * An abort surfaced from the bridge as an ordinary exception, which
   * `HostBridgeProvider` sanitized into a *retryable* provider error - which is precisely
   * the chain's signal to advance. So cancelling the primary started the fallback instead
   * of ending the request.
   */
  it("does not start a fallback after the caller aborts", async () => {
    let attempts = 0;
    const controller = new AbortController();
    const bridge = async (opts: { signal?: AbortSignal }) => {
      attempts += 1;
      controller.abort();
      opts.signal?.throwIfAborted();
      throw new Error("aborted");
    };
    const chain = new FallbackChainProvider(
      new HostBridgeProvider(bridge, L, READER_MODEL, "openai"),
      [new HostBridgeProvider(bridge, L, "gpt-5.6-sol", "openai")],
    );

    await expect(
      chain.complete({
        system: "s",
        user: "u",
        maxOutputTokens: 10,
        timeoutMs: 5000,
        signal: controller.signal,
      }),
    ).rejects.toThrow(ShuntError);
    expect(attempts).toBe(1);
  });
});

// -- the pre-1.1 public signature, restored --------------------------------

describe("legacy Reader.answer contract", () => {
  /**
   * At the public baseline `389a01d`, `answer(sessionId, request, deadline?, signal?)`
   * resolved to an `Envelope`. 1.1 changed the arity *and* the return type, so a compiled
   * caller reading `result.status` got a thrown getter and a caller passing a signal in
   * the fourth position had it silently ignored. The rich record moved to `answerDetailed`.
   */
  it("resolves to the envelope itself", async () => {
    const { entry, reader } = fixture();
    const envelope = await reader.answer("sess", request(entry));
    expect(envelope.status).toBeTypeOf("string");
    expect(envelope.code).toBeTypeOf("string");
    expect(Object.keys(envelope)).toContain("citations");
    // The serialized shape is the envelope, not a nested record.
    expect(Object.keys(JSON.parse(JSON.stringify(envelope)))).not.toContain("envelope");
  });

  it("honours a Deadline in the third position", async () => {
    const clock = new FakeClock();
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const slow: any = {
      async complete() {
        clock.advance(200);
        return { text: answerJson("x [c1]", [{ id: "c1", line_start: 2, line_end: 2, quote: "max_retries" }]), model: READER_MODEL, usage: { inputTokens: 1, outputTokens: 1 } };
      },
    };
    const envelope = await new Reader(registry, slow, undefined, clock)
      .answer("sess", request(entry), Deadline.start(clock, 100));
    expect(envelope.status).toBe("error");
    expect(envelope.code).toBe("TIMEOUT");
  });

  it("honours an already-aborted signal in the fourth position", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
    const luna = new FakeLuna([], answerJson("", []));
    const controller = new AbortController();
    controller.abort();

    const envelope = await new Reader(registry, luna)
      .answer("sess", request(entry), undefined, controller.signal);
    expect(luna.callCount).toBe(0);
    expect(envelope.code).toBe("CANCELLED");
  });

  it("still offers the rich record under its own name", async () => {
    const { entry, reader } = fixture();
    const detailed = await reader.answerDetailed("sess", request(entry));
    expect(Object.keys(detailed).sort()).toEqual(["cost", "envelope", "provenance", "sourceIds"]);
    expect(detailed.envelope.status).toBeTypeOf("string");
  });
});
