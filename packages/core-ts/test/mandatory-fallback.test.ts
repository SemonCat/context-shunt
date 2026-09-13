import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { FakeClock } from "../src/clock.js";
import { ShuntError } from "../src/errors.js";
import { errorEnvelope } from "../src/envelope.js";
import { DEFAULT_LIMITS as L, narrowLimits } from "../src/limits.js";
import { compactToolResult } from "../src/legacy-compact.js";
import { FallbackChainProvider, HostBridgeProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { SpillEngine } from "../src/spill.js";
import { makeCapability, makeConfig, makeRegistry, FakeLuna } from "./support.js";
import { conformance } from "./fixtures.js";

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-ts-mandatory-fallback-"));
}

function oversizedBody(): string {
  return Array.from({ length: 3_000 }, (_, index) =>
    index % 7 === 0 ? `ERROR: row ${index}` : `row ${index} payload`,
  ).join("\n");
}

function directEngine(
  limits = narrowLimits(L, { maxToolResultBytes: 256, maxExtractionBytes: 8_000 }),
) {
  const registry = makeRegistry(tmp(), { sessionId: "sess", limits });
  return { registry, engine: new SpillEngine(registry, limits, true, 16_000), limits };
}

function readerRequest(entry: { sourceId: string; snapshot: { snapshotId: string } }) {
  return {
    schema_version: "1.1",
    request_id: "req_mandatory_fallback",
    operation: "read",
    question: "What does this source contain?",
    sources: [{
      source_id: entry.sourceId,
      snapshot_id: entry.snapshot.snapshotId,
      selector: { kind: "all" },
    }],
    budgets: { max_chunks: 1, max_answer_bytes: 8_192, deadline_ms: 60_000 },
  };
}

describe("mandatory legacy fallback", () => {
  const golden = conformance<{
    cases: Array<{
      id: string;
      prefix: string;
      unit: string;
      repeat: number;
      separator: string;
      suffix: string;
      hard_chars: number;
      expected: string;
    }>;
  }>("legacy-golden.json");

  for (const testCase of golden.cases) {
    it(`matches the incumbent compactor golden ${testCase.id}`, () => {
      const body = testCase.prefix
        + Array.from({ length: testCase.repeat }, () => testCase.unit).join(testCase.separator)
        + testCase.suffix;
      expect(compactToolResult(body, { hardChars: testCase.hard_chars })).toBe(testCase.expected);
    });
  }

  it("keeps a deeply nested structured string bounded if the compactor walk exhausts the JS stack", () => {
    const depth = 2_000;
    const deep = `${"{\"nested\":".repeat(depth)}null${"}".repeat(depth)}`;
    const summary = compactToolResult(deep, { hardChars: 500 });
    expect(Array.from(summary).length).toBeLessThanOrEqual(500);
    expect(summary).not.toContain("RangeError");
  });

  it.each([
    ["ShuntError", new ShuntError("STORE_FAILED", "WRITE_FAILED", false)],
    ["unexpected store error", new Error("raw-store-body-should-not-escape")],
    ["store capacity", new ShuntError("LIMIT_EXCEEDED", "STORE_BYTE_QUOTA", false)],
  ])("compacts an owned capture/store failure (%s)", (_name, failure) => {
    const { registry, engine } = directEngine();
    registry.store.publish = () => { throw failure; };
    const body = oversizedBody();
    const outcome = engine.evaluate("sess", "req_spill_failure", body, undefined, "acc_1111111111111111");
    expect(outcome.action).toBe("error");
    expect(outcome.code).toBe("LEGACY_COMPACTED");
    const envelope = outcome.envelope!;
    expect(envelope.code).toBe("LEGACY_COMPACTED");
    expect(envelope.legacy_compaction?.original_failure).toBe(
      failure instanceof ShuntError && failure.code === "LIMIT_EXCEEDED"
        ? "LIMIT_EXCEEDED"
        : "STORE_FAILED",
    );
    expect(envelope.legacy_compaction?.summary).toBe(compactToolResult(body, { hardChars: 16_000 }));
    expect(envelope.failure_detail).toBe(
      failure instanceof ShuntError ? failure.detail : "INTERNAL_ERROR",
    );
    expect(envelope.sources).toEqual([]);
    expect(envelope.recovery?.handles_valid).toBe(false);
    expect(envelope.provenance?.derived).toBe(false);
    expect(envelope.provenance?.label).toBe("legacy_compaction");
    expect(envelope.coverage.complete).toBe(false);
    expect(JSON.stringify(outcome)).not.toContain("raw-store-body-should-not-escape");
  });

  it("does not use a raw fallback for content mismatch", () => {
    const { registry, engine } = directEngine();
    registry.store.publish = () => {
      throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
    };
    const outcome = engine.evaluate("sess", "req_mismatch", oversizedBody());
    expect(outcome.code).toBe("STORE_FAILED");
    expect(outcome.envelope?.legacy_compaction).toBeUndefined();
    expect(outcome.envelope?.recovery?.handles_valid).toBe(false);
  });

  it("keeps source cap, binary and secret policy refusals explicit", () => {
    const limits = narrowLimits(L, { maxToolResultBytes: 256, maxSourceBytes: 512 });
    const { engine } = directEngine(limits);
    const overCap = engine.evaluate("sess", "req_cap", "x".repeat(513));
    expect(overCap.code).toBe("LIMIT_EXCEEDED");
    expect(overCap.envelope?.legacy_compaction).toBeUndefined();

    const { engine: binaryEngine } = directEngine();
    const binary = binaryEngine.evaluate("sess", "req_binary", new Uint8Array([0, ...Array(400).fill(1)]));
    expect(binary.action).toBe("blocked");
    expect(binary.code).toBe("BINARY_UNSUPPORTED");
    expect(binary.envelope?.legacy_compaction).toBeUndefined();

    const { engine: secretEngine } = directEngine();
    const secret = secretEngine.evaluate(
      "sess",
      "req_secret",
      ("-----BEGIN PRIVATE KEY-----\n" + "x".repeat(500)),
    );
    expect(secret.action).toBe("error");
    expect(secret.code).toBe("UNSAFE_SOURCE");
    expect(secret.envelope?.legacy_compaction).toBeUndefined();
  });

  it("returns a bounded legacy result when the session boundary throws unexpectedly", () => {
    const dir = tmp();
    const session = new ShuntSession(
      "sess",
      makeConfig(dir, { tool_result_capture: { enabled: true } }),
      makeCapability(true),
      { legacyCompaction: false },
    );
    const body = oversizedBody();
    vi.spyOn(session.spill, "evaluate").mockImplementation(() => {
      throw new Error("adapter-internal-secret");
    });
    const outcome = session.postToolResult("req_unexpected", body)!;
    expect(outcome.code).toBe("LEGACY_COMPACTED");
    expect(outcome.envelope?.legacy_compaction?.original_failure).toBe("SPILL_FAILED");
    expect(outcome.envelope?.failure_detail).toBe("INTERNAL_ERROR");
    expect(JSON.stringify(outcome)).not.toContain("adapter-internal-secret");
    expect(JSON.stringify(outcome)).not.toContain(body.slice(-40));
  });

  it("never lets legacy_compaction=false disable reader fallback", async () => {
    const dir = tmp();
    const session = new ShuntSession(
      "sess",
      makeConfig(dir),
      makeCapability(),
      {
        provider: new FakeLuna([], new ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", true)),
        legacyCompaction: false,
      },
    );
    const body = oversizedBody();
    const path = join(dir, "ws", "source.txt");
    writeFileSync(path, body);
    const entry = session.registerPath(path);
    const envelope = await session.read(readerRequest(entry));
    expect(envelope.code).toBe("LEGACY_COMPACTED");
    expect(envelope.legacy_compaction?.original_failure).toBe("MODEL_ERROR");
    expect(envelope.provenance?.derived).toBe(false);
    expect(envelope.provenance?.label).toBe("legacy_compaction");
    expect(envelope.provenance?.citations_mechanically_verified).toBe(false);
    expect(envelope.coverage.complete).toBe(false);
    expect(envelope.answer).toBe("");
    expect(envelope.citations).toEqual([]);
    expect(envelope.guidance).toContain("never as the question's answer");
    expect(envelope.guidance).toContain("exact count");
    expect(envelope.guidance).toContain("citation evidence");
    expect(envelope.sources[0]?.source_id).toBe(entry.sourceId);
  });

  it("uses the migrated config character ceiling without a session toggle", async () => {
    const dir = tmp();
    const session = new ShuntSession(
      "sess",
      makeConfig(dir, { reader: { legacy_compaction_max_chars: 1_000 } }),
      makeCapability(),
      { provider: new FakeLuna([], new ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", true)) },
    );
    const path = join(dir, "ws", "source.txt");
    writeFileSync(path, oversizedBody());
    const envelope = await session.read(readerRequest(session.registerPath(path)));

    expect(envelope.code).toBe("LEGACY_COMPACTED");
    expect(envelope.legacy_compaction?.hard_cap_chars).toBe(1_000);
    expect(Array.from(envelope.legacy_compaction?.summary ?? "").length).toBeLessThanOrEqual(1_000);
  });

  it("fails open after deadline with a locator that recovers exact retained source", async () => {
    const dir = tmp();
    const clock = new FakeClock();
    const calls = { primary: 0, fallback: 0 };
    const provider = new FallbackChainProvider(
      new HostBridgeProvider(async () => {
        calls.primary += 1;
        clock.advance(60_000);
        throw new ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", true);
      }),
      [new HostBridgeProvider(async () => {
        calls.fallback += 1;
        return { text: "{}" };
      }, L, "fallback")],
    );
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider,
      clock,
    });
    const marker = "LOCATOR-ONLY-CANARY-7d3e9a";
    const body = "ERROR: first provider unavailable\n"
      + Array.from({ length: 2_000 }, (_, index) => `ordinary row ${index}\n`).join("")
      + `exact retained evidence ${marker}\n`;
    const path = join(dir, "ws", "deadline-source.txt");
    writeFileSync(path, body);
    const entry = session.registerPath(path);

    const env = await session.read(readerRequest(entry));
    expect(env.code).toBe("LEGACY_COMPACTED");
    expect(env.legacy_compaction?.original_failure).toBe("TIMEOUT");
    expect(calls).toEqual({ primary: 1, fallback: 0 });
    expect(env.sources[0]?.source_id).toBe(entry.sourceId);
    expect(env.sources[0]?.snapshot_id).toBe(entry.snapshot.snapshotId);
    expect(env.coverage.omitted.some((item) => item.reason === "UNKNOWN_REMAINDER")).toBe(true);
    expect(JSON.stringify(env)).not.toContain(body);

    const inspected = session.inspect({
      schema_version: "1.1",
      request_id: "req_deadline_locator",
      operation: "inspect",
      source_id: env.sources[0]?.source_id,
      snapshot_id: env.sources[0]?.snapshot_id,
      selector: { kind: "search", needle: marker, max_matches: 1 },
      budgets: { max_result_bytes: 4096, max_scan_lines: 20_000 },
    });
    expect(inspected.code).toBe("EXTRACTED");
    expect(inspected.extraction?.segments[0]?.text).toContain(marker);
    expect(calls).toEqual({ primary: 1, fallback: 0 });
  });

  it("refuses fallback when the disclosure ceiling is already exhausted", async () => {
    const dir = tmp();
    const session = new ShuntSession(
      "sess",
      makeConfig(dir, {
        limits: { disclosure_max_per_source_bytes: 64, disclosure_max_per_session_bytes: 64 },
      }),
      makeCapability(),
      {
        provider: new FakeLuna([], new ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", true)),
      },
    );
    const path = join(dir, "ws", "source.txt");
    writeFileSync(path, oversizedBody());
    const entry = session.registerPath(path);
    const charge = session.store.chargeDisclosure(
      session.identity,
      entry.sourceId,
      "bytes",
      64,
    );
    expect(charge.granted).toBe(true);
    const envelope = await session.read(readerRequest(entry));
    expect(envelope.code).toBe("DISCLOSURE_EXHAUSTED");
    expect(envelope.legacy_compaction).toBeUndefined();
    expect(envelope.recovery?.handles_valid).toBe(true);
  });

  it("preserves paid reader identity and usage on deterministic fallback", async () => {
    const dir = tmp();
    const session = new ShuntSession(
      "sess",
      makeConfig(dir),
      makeCapability(),
      { provider: new FakeLuna([], new ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", true)) },
    );
    const path = join(dir, "ws", "source.txt");
    writeFileSync(path, oversizedBody());
    const entry = session.registerPath(path);
    const envelope = await session.read(readerRequest(entry));
    expect(envelope.code).toBe("LEGACY_COMPACTED");
    expect(envelope.provenance?.attempts_started).toBeGreaterThan(0);
    expect(envelope.provenance?.requested_model).toBeDefined();
    expect(envelope.provenance?.attribution_status).not.toBe("not_applicable");
  });
});

describe("invalid request recovery", () => {
  it.each([
    ["TOOL_ARGS_VIOLATION", { handles_valid: true, actions: ["NONE"] }],
    ["INVALID_SNAPSHOT_ID", { handles_valid: false, actions: ["REUSE_POINTER_PAIR"] }],
  ] as const)("distinguishes %s from other schema failures", (detail, expected) => {
    const env = errorEnvelope("req_invalid", new ShuntError("INVALID_REQUEST", detail, false));
    expect(env.failure_detail).toBe(detail);
    expect(env.recovery).toEqual(expected);
    if (detail === "INVALID_SNAPSHOT_ID") {
      expect(env.guidance).toContain("exact source_id/snapshot_id pair");
    } else {
      expect(env.guidance).not.toContain("exact source_id/snapshot_id pair");
      expect(env.guidance).toContain("tool argument schema");
    }
  });
});


describe("session ownership and policy boundaries", () => {
  function setup() {
    const dir = tmp();
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {provider: new FakeLuna(), legacyCompaction: false});
    const body = oversizedBody();
    const path = join(dir, "ws", "source.txt"); writeFileSync(path, body);
    const entry = session.registerPath(path);
    return {session, body, entry};
  }
  it.each(["MODEL_ERROR", "TIMEOUT", "INVALID_MODEL_OUTPUT", "CITATION_INVALID", "unexpected"])("recovers reader %s with the incumbent compactor", async (code) => {
    const {session, body, entry} = setup();
    vi.spyOn((session as any).reader, "answerDetailed").mockRejectedValue(code === "unexpected" ? new Error("PRIVATE_EXCEPTION") : new ShuntError(code, "NO_VALID_EVIDENCE"));
    const env = await session.read(readerRequest(entry));
    expect(env.code).toBe("LEGACY_COMPACTED");
    expect(env.legacy_compaction!.summary).toBe(compactToolResult(body, {hardChars: 16000}));
    expect(JSON.stringify(env)).not.toContain("PRIVATE_EXCEPTION");
    expect(env.provenance!.derived).toBe(false);
  });
  it.each([new Error("PRIVATE_EXCEPTION"), new ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_PAGE_BUDGET")])("recovers eligible inspect internals", (failure) => {
    const {session, entry} = setup();
    vi.spyOn((session as any).inspector, "extract").mockImplementation(() => {throw failure;});
    const env = session.inspect({schema_version: "1.1", operation: "inspect", request_id: "req_inspect", source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId, selector: {kind: "lines", start: 1, end: 2}, budgets: {max_result_bytes: 1000, max_scan_lines: 100}});
    expect(env.code).toBe("LEGACY_COMPACTED");
    expect(env.legacy_compaction!.summary_bytes).toBeLessThanOrEqual(1000);
    expect(session.store.disclosureAllowance(session.identity, entry.sourceId).perSourceRemaining).toBe(session.config.limits.disclosureMaxPerSourceBytes - env.legacy_compaction!.summary_bytes);
  });
  it.each(["INVALID_REQUEST", "UNSAFE_SOURCE", "BINARY_UNSUPPORTED", "SOURCE_EXPIRED", "SOURCE_CHANGED", "PROVENANCE_UNAVAILABLE", "DISCLOSURE_EXHAUSTED", "UNSUPPORTED_VERSION"])("never falls back for %s", async (code) => {
    const {session, entry} = setup();
    vi.spyOn((session as any).reader, "answerDetailed").mockRejectedValue(new ShuntError(code));
    expect((await session.read(readerRequest(entry))).legacy_compaction).toBeUndefined();
  });
  it("validates a malformed snapshot before recovering an unexpected reader failure", async () => {
    const {session, entry} = setup();
    vi.spyOn((session as any).reader, "answerDetailed").mockRejectedValue(new Error("PRIVATE_EXCEPTION"));
    const request = readerRequest(entry); request.sources[0]!.snapshot_id = "sha256:" + "a".repeat(71);
    const env = await session.read(request);
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("INVALID_SNAPSHOT_ID");
    expect(env.recovery!.handles_valid).toBe(false);
    expect(env.recovery!.actions).toContain("REUSE_POINTER_PAIR");
  });
});
