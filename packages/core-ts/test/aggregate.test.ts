import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { aggregateSnapshot } from "../src/aggregate.js";
import { DEFAULT_LIMITS } from "../src/limits.js";
import { UnavailableProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { type Snapshot } from "../src/snapshot.js";
import { makeCapability, makeConfig } from "./support.js";

function setup() {
  const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-"));
  mkdirSync(join(dir, "ws"), { recursive: true });
  const logs = [
    { level: "error", trace_id: "tr-a", service: "billing" },
    { level: "info", trace_id: "tr-b", service: "billing" },
    { level: "error", trace_id: "tr-a", service: "checkout" },
    { level: "error", trace_id: "tr-c", service: "billing" },
    { level: "error", trace_id: null },
    { level: "error", trace_id: "tr-d", service: null },
  ];
  const body = JSON.stringify({ data: { result: [
    { stream: { job: "synthetic" }, values: logs.map((row, i) => [String(i), JSON.stringify(row)]) },
  ] } });
  const path = join(dir, "ws", "loki.json");
  writeFileSync(path, body);
  const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
    provider: new UnavailableProvider("MUST_NOT_RUN"),
  });
  return { session, entry: session.registerPath(path) };
}

function request(
  entry: ReturnType<ShuntSession["registerPath"]>,
  selector: object,
  maxScan = 20_000,
  maxResultBytes = 16_384,
) {
  return {
    schema_version: "1.3", request_id: "req_aggregate", operation: "inspect",
    source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId, selector,
    budgets: { max_result_bytes: maxResultBytes, max_scan_lines: maxScan },
  };
}

describe("structured deterministic aggregation", () => {
  it("counts, groups, and finds distinct fields in minified Loki embedded JSON", () => {
    const { session, entry } = setup();
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/data/result", expand_pointer: "/values",
      record_pointer: "/1", parse_json: true,
      filter: { pointer: "/level", equals: "error" },
      distinct: ["/trace_id"], group_by: ["/service"],
    }));
    expect(env.code).toBe("EXTRACTED");
    expect(env.status).toBe("ok");
    expect(env.provenance?.attempts_started).toBe(0);
    const result = JSON.parse(env.extraction!.segments[0]!.text);
    expect(result.matched_count).toBe(5);
    expect(result.records_scanned).toBe(6);
    expect(result.distinct[0]).toEqual({
      path: "/trace_id", count: 4, values: ["tr-a", "tr-c", "tr-d", null], values_complete: true,
    });
    expect(result.groups).toEqual([
      { key: ["billing"], count: 2 }, { key: ["checkout"], count: 1 },
      { key: [null], count: 1 }, { key: [{ missing: true }], count: 1 },
    ]);
    expect(result.group_count).toBe(4);
  });

  it("refuses a scan beyond the caller's record budget without a partial count", () => {
    const { session, entry } = setup();
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/data/result", expand_pointer: "/values",
      record_pointer: "/1", parse_json: true,
    }, 3));
    expect(env.code).toBe("LIMIT_EXCEEDED");
    expect(env.extraction).toBeUndefined();
  });

  it("charges empty outer expansions against the scan budget", () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-empty-"));
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", "empty-expansions.json");
    writeFileSync(path, JSON.stringify({ outer: [{ records: [] }, { records: [] }] }));
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider: new UnavailableProvider("MUST_NOT_RUN"),
    });
    const entry = session.registerPath(path);
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/outer", expand_pointer: "/records",
    }, 1));
    expect(env.code).toBe("LIMIT_EXCEEDED");
    expect(env.extraction).toBeUndefined();
  });

  it.each(["distinct", "group_by"])(
    "rejects a non-scalar %s target rather than reporting it as missing",
    (field) => {
      const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-object-"));
      mkdirSync(join(dir, "ws"), { recursive: true });
      const path = join(dir, "ws", "object-value.json");
      writeFileSync(path, JSON.stringify({ records: [{ value: { nested: 1 } }, {}] }));
      const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
        provider: new UnavailableProvider("MUST_NOT_RUN"),
      });
      const entry = session.registerPath(path);
      const env = session.inspect(request(entry, {
        kind: "aggregate", records_pointer: "/records", [field]: ["/value"],
      }));
      expect(env.code).toBe("INVALID_REQUEST");
      expect(env.failure_detail).toBe("BAD_SELECTOR");
      expect(env.extraction).toBeUndefined();
    },
  );

  it("orders canonical aggregate keys by UTF-8 bytes across runtimes", () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-unicode-"));
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", "unicode-order.json");
    writeFileSync(path, JSON.stringify({ records: [{ value: "\ue000" }, { value: "😀" }] }));
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider: new UnavailableProvider("MUST_NOT_RUN"),
    });
    const entry = session.registerPath(path);
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/records",
      distinct: ["/value"], group_by: ["/value"],
    }));
    const result = JSON.parse(env.extraction!.segments[0]!.text);
    expect(result.distinct[0].values).toEqual(["\ue000", "😀"]);
    expect(result.groups.map((row: { key: string[] }) => row.key))
      .toEqual([["\ue000"], ["😀"]]);
  });

  it("rejects escaped lone surrogate aggregate keys", () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-surrogate-"));
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", "lone-surrogate.json");
    writeFileSync(path, '{"records":[{"value":"\\ud800"}]}');
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider: new UnavailableProvider("MUST_NOT_RUN"),
    });
    const entry = session.registerPath(path);
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/records",
      distinct: ["/value"], group_by: ["/value"],
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("BAD_SELECTOR");
    expect(env.extraction).toBeUndefined();
  });

  it("rejects a lone surrogate before measuring embedded JSON bytes", () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-embedded-surrogate-"));
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", "embedded-surrogate.json");
    writeFileSync(path, '{"records":[{"value":"\\ud800"}]}');
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider: new UnavailableProvider("MUST_NOT_RUN"),
    });
    const entry = session.registerPath(path);
    const env = session.inspect(request(entry, {
      kind: "aggregate",
      records_pointer: "/records",
      record_pointer: "/value",
      parse_json: true,
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("BAD_JSON");
    expect(env.extraction).toBeUndefined();
  });

  it.each(["NaN", "Infinity", "-Infinity"])(
    "rejects non-standard %s in text/plain JSON",
    (constant) => {
      const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-constant-"));
      const session = new ShuntSession(
        "sess",
        makeConfig(dir, { tool_result_capture: {
          enabled: true, host_ordering_verified_locally: true,
        } }),
        makeCapability(true),
        { provider: new UnavailableProvider("MUST_NOT_RUN") },
      );
      const body = `{"records":[${constant}],"padding":"${"x".repeat(17_000)}"}`;
      const outcome = session.postToolResult("spill-constant", body)!;
      expect(outcome.action).toBe("spill");
      const entry = session.registry.resolve("sess", outcome.sourceId!);
      const env = session.inspect(request(entry, {
        kind: "aggregate", records_pointer: "/records",
      }));
      expect(env.code).toBe("INVALID_REQUEST");
      expect(env.failure_detail).toBe("BAD_JSON");
    },
  );

  it.each(["NaN", "Infinity", "-Infinity"])(
    "rejects non-standard %s in embedded JSON",
    (constant) => {
      const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-embedded-constant-"));
      mkdirSync(join(dir, "ws"), { recursive: true });
      const path = join(dir, "ws", "embedded-constant.json");
      writeFileSync(path, JSON.stringify({ records: [{ value: constant }] }));
      const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
        provider: new UnavailableProvider("MUST_NOT_RUN"),
      });
      const entry = session.registerPath(path);
      const env = session.inspect(request(entry, {
        kind: "aggregate",
        records_pointer: "/records",
        record_pointer: "/value",
        parse_json: true,
      }));
      expect(env.code).toBe("INVALID_REQUEST");
      expect(env.failure_detail).toBe("BAD_JSON");
    },
  );

  it.each(["distinct", "group_by"] as const)(
    "rejects a lone surrogate in the emitted %s pointer name",
    (field) => {
      const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-pointer-surrogate-"));
      mkdirSync(join(dir, "ws"), { recursive: true });
      const path = join(dir, "ws", "ordinary.json");
      writeFileSync(path, '{"records":[{}]}');
      const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
        provider: new UnavailableProvider("MUST_NOT_RUN"),
      });
      const entry = session.registerPath(path);
      const env = session.inspect(request(entry, {
        kind: "aggregate", records_pointer: "/records", [field]: ["/\ud800"],
      }));
      expect(env.code).toBe("INVALID_REQUEST");
      expect(env.failure_detail).toBe("BAD_SELECTOR");
      expect(env.extraction).toBeUndefined();
    },
  );

  it("rejects malformed UTF-8 in a direct text snapshot without replacement", () => {
    const malformed = {
      snapshotId: "sha256:fixture",
      mediaType: "text/plain",
      data: Uint8Array.from([0x7b, 0x22, 0x78, 0x22, 0x3a, 0xc3, 0x28, 0x7d]),
      lineIndex: undefined,
      jsonValue: undefined,
      bytesLen: 8,
      lineCount: 1,
    } as unknown as Snapshot;
    expect(() => aggregateSnapshot(
      malformed,
      { records_pointer: "/records" },
      {
        maxResultBytes: 16_384,
        maxWireBytes: 20_000,
        maxRecords: 20_000,
        limits: DEFAULT_LIMITS,
      },
    )).toThrowError(expect.objectContaining({ code: "INVALID_REQUEST", detail: "BAD_JSON" }));
  });

  it.each([
    '[{"value":1}]',
    '[{"value":9007199254740992},{"value":9007199254740993}]',
    '[{"value":1.5}]',
    '[{"value":1e-400}]',
    '[{"value":9007199254740991.1}]',
  ])("rejects numeric keys whose original lexeme may be lost: %s", (rawRecords) => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-number-"));
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", "unsafe-number.json");
    writeFileSync(path, `{"records":${rawRecords}}`);
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider: new UnavailableProvider("MUST_NOT_RUN"),
    });
    const entry = session.registerPath(path);
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/records", distinct: ["/value"],
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("BAD_SELECTOR");
    expect(env.extraction).toBeUndefined();
  });

  it.each([
    [513, "SCHEMA_VIOLATION"],
    [511, "BAD_SELECTOR"],
  ] as const)("rejects an oversized equality filter before scanning records: %i", (length, detail) => {
    const { session, entry } = setup();
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/data/result",
      filter: { pointer: "/value", equals: "x".repeat(length) },
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe(detail);
    expect(env.extraction).toBeUndefined();
  });

  it("keeps exact cardinalities when bounded key samples are truncated", () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-cardinality-"));
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", "records.json");
    writeFileSync(path, JSON.stringify({ records: Array.from({ length: 205 }, (_, i) => ({
      key: `key-${String(i).padStart(3, "0")}`,
    })) }));
    const session = new ShuntSession("sess", makeConfig(dir), makeCapability(), {
      provider: new UnavailableProvider("MUST_NOT_RUN"),
    });
    const entry = session.registerPath(path);
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/records", distinct: ["/key"], group_by: ["/key"],
    }));
    const result = JSON.parse(env.extraction!.segments[0]!.text);
    expect(result).toMatchObject({
      matched_count: 205, group_count: 205, groups_complete: false,
    });
    expect(result.groups).toHaveLength(200);
    expect(result.distinct[0]).toMatchObject({ count: 205, values_complete: false });
    expect(result.distinct[0].values).toHaveLength(200);
  });

  function decodeSetup(name: string, body: unknown, overrides: Record<string, unknown> = {}) {
    const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-decode-"));
    mkdirSync(join(dir, "ws"), { recursive: true });
    const path = join(dir, "ws", name);
    writeFileSync(path, JSON.stringify(body));
    const session = new ShuntSession("sess", makeConfig(dir, overrides), makeCapability(), {
      provider: new UnavailableProvider("MUST_NOT_RUN"),
    });
    return { session, entry: session.registerPath(path) };
  }

  it("matches current output exactly when decode_pointer is omitted", () => {
    const { session, entry } = setup();
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/data/result",
    }));
    const result = JSON.parse(env.extraction!.segments[0]!.text);
    expect(result).not.toHaveProperty("decoded_from");
  });

  it("decodes one layer, then resolves records_pointer against the decoded root", () => {
    const records = [
      { status: "200", page: "1", per_page: "10" },
      { status: "200", page: "1", per_page: "10" },
      { status: "404", page: "2", per_page: "10" },
      { status: "200", page: "2", per_page: "20" },
      { status: "500", page: "1", per_page: "10" },
      { status: "200", page: "3", per_page: "20" },
      { status: "404", page: "1", per_page: "10" },
      { status: "200", page: "2", per_page: "10" },
      { status: "301", page: "1", per_page: "10" },
    ];
    const inner = JSON.stringify({ data: records.map((row) => ({ line: JSON.stringify(row) })) });
    const { session, entry } = decodeSetup("wrapped.json", { result: inner });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
      record_pointer: "/line", parse_json: true,
      distinct: ["/status"], group_by: ["/status"],
    }));
    expect(env.code).toBe("EXTRACTED");
    expect(env.status).toBe("ok");
    const result = JSON.parse(env.extraction!.segments[0]!.text);
    expect(result.decoded_from).toBe("/result");
    expect(result.records_scanned).toBe(9);
    expect(result.matched_count).toBe(9);
    expect(result.distinct[0]).toEqual({
      path: "/status", count: 4, values: ["200", "301", "404", "500"], values_complete: true,
    });
    expect(result.groups).toEqual([
      { key: ["200"], count: 5 }, { key: ["301"], count: 1 },
      { key: ["404"], count: 2 }, { key: ["500"], count: 1 },
    ]);
    expect(result.group_count).toBe(4);
  });

  it("does not recurse through a second JSON-string layer", () => {
    const doublyEncoded = JSON.stringify(JSON.stringify({ data: [] }));
    const { session, entry } = decodeSetup("double.json", { result: doublyEncoded });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "",
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("BAD_SELECTOR");
    expect(env.extraction).toBeUndefined();
  });

  it("rejects a malformed decode_pointer at request validation", () => {
    const { session, entry } = decodeSetup("plain.json", { result: "{}" });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "result", records_pointer: "/data",
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("SCHEMA_VIOLATION");
    expect(env.extraction).toBeUndefined();
  });

  it("rejects a decode_pointer that resolves to nothing", () => {
    const { session, entry } = decodeSetup("plain.json", { result: "{}" });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/missing", records_pointer: "/data",
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("POINTER_NOT_FOUND");
    expect(env.extraction).toBeUndefined();
  });

  it("rejects a non-string decode_pointer target", () => {
    const { session, entry } = decodeSetup("object.json", { result: { data: [] } });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("BAD_SELECTOR");
    expect(env.extraction).toBeUndefined();
  });

  it("rejects malformed JSON text at decode_pointer", () => {
    const { session, entry } = decodeSetup("bad.json", { result: "not json{" });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
    }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.failure_detail).toBe("BAD_JSON");
    expect(env.extraction).toBeUndefined();
  });

  it("enforces the node cap on the decoded structure", () => {
    const body = { result: JSON.stringify({ data: Array.from({ length: 200 }, (_, i) => ({ n: i })) }) };
    const { session, entry } = decodeSetup("deep.json", body, { limits: { json_max_nodes: 20 } });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
    }));
    expect(env.code).toBe("LIMIT_EXCEEDED");
    expect(env.failure_detail).toBe("JSON_TOO_MANY_NODES");
    expect(env.extraction).toBeUndefined();
  });

  it("enforces the depth cap on the decoded structure", () => {
    // The raw file is shallow - one top-level object holding one string - regardless of how
    // deeply the *decoded* value nests, so depth-cap isolation (unlike byte-cap isolation) is
    // reachable standalone: only the decode stage's own jsonDepthAndNodes walk can trip it.
    let nested: unknown = { data: [] };
    for (let i = 0; i < 10; i++) {
      nested = { data: nested };
    }
    const body = { result: JSON.stringify(nested) };
    const { session, entry } = decodeSetup("deep-decode.json", body, { limits: { json_max_depth: 5 } });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
    }));
    expect(env.code).toBe("LIMIT_EXCEEDED");
    expect(env.failure_detail).toBe("JSON_TOO_DEEP");
    expect(env.extraction).toBeUndefined();
  });

  it("shares one node budget between decode_pointer and per-record parse_json", () => {
    // decode_pointer alone walks {"data": [{"line": "..."} x 50]} = 102 nodes (1 root
    // object + 1 array + 50 * (1 object + 1 string)). Each subsequent per-record
    // parse_json of {"n": i} adds 2 more nodes, 100 total across all 50 records. A cap
    // strictly between 102 and 202 proves the two stages share one cumulative counter:
    // the decode alone must fit under it, and only accumulating per-record parses pushes
    // the total over.
    const records = Array.from({ length: 50 }, (_, i) => ({ n: i }));
    const inner = JSON.stringify({ data: records.map((row) => ({ line: JSON.stringify(row) })) });
    const { session, entry } = decodeSetup("node-budget.json", { result: inner }, {
      limits: { json_max_nodes: 150 },
    });
    const decodeOnlyEnv = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
    }));
    expect(decodeOnlyEnv.code).toBe("EXTRACTED");

    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
      record_pointer: "/line", parse_json: true,
    }));
    expect(env.code).toBe("LIMIT_EXCEEDED");
    expect(env.failure_detail).toBe("JSON_TOO_MANY_NODES");
    expect(env.extraction).toBeUndefined();
  });

  it("shares one byte budget between decode_pointer and per-record parse_json", () => {
    // A source file's raw bytes are always >= the decode_pointer target's decoded bytes
    // (embedding a JSON string only ever adds escaping overhead), so decode_pointer's own
    // byte cap can't be isolated from the file-level read cap using a single decode. What
    // *is* reachable - and is the doubling risk the shared budget guards against - is
    // decode_pointer's bytes plus every subsequent per-record parse_json's bytes adding up
    // past the cap even though the file itself comfortably fit under it.
    const records = Array.from({ length: 50 }, (_, i) => ({ n: i, pad: "z".repeat(20) }));
    const inner = JSON.stringify({ data: records.map((row) => JSON.stringify(row)) });
    const body = { result: inner };
    const outerBytes = Buffer.byteLength(JSON.stringify(body), "utf8");
    const { session, entry } = decodeSetup("shared-budget.json", body, {
      limits: { max_source_bytes: outerBytes + 50 },
    });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data", parse_json: true,
    }));
    expect(env.code).toBe("LIMIT_EXCEEDED");
    expect(env.failure_detail).toBe("RESULT_OVER_SOURCE_CAP");
    expect(env.extraction).toBeUndefined();
  });

  it("still shrinks decode_pointer output to fit the value-sample cap", () => {
    const records = Array.from({ length: 205 }, (_, i) => ({ key: `key-${String(i).padStart(3, "0")}` }));
    const inner = JSON.stringify({ data: records.map((row) => ({ line: JSON.stringify(row) })) });
    const { session, entry } = decodeSetup("shrink.json", { result: inner });
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
      record_pointer: "/line", parse_json: true,
      distinct: ["/key"], group_by: ["/key"],
    }));
    expect(env.code).toBe("EXTRACTED");
    const result = JSON.parse(env.extraction!.segments[0]!.text);
    expect(result.decoded_from).toBe("/result");
    expect(result.matched_count).toBe(205);
    expect(result.groups_complete).toBe(false);
    expect(result.distinct[0].values_complete).toBe(false);
  });

  it("shrinks decode_pointer output to fit a tightened byte budget, preserving decoded_from", () => {
    // Only 20 distinct groups here - well under the 200-row hard sample cap the previous
    // test exercises - so a tightened max_result_bytes below the naturally emitted size is
    // the only thing that can force the fits()-loop's group/distinct truncation. That
    // proves decoded_from survives the byte-driven shrink path specifically, not just the
    // value-sample cap path.
    const records = Array.from({ length: 20 }, (_, i) => ({ key: `key-${String(i).padStart(3, "0")}` }));
    const inner = JSON.stringify({ data: records.map((row) => ({ line: JSON.stringify(row) })) });
    const { session, entry } = decodeSetup("byte-shrink.json", { result: inner });
    const selector = {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
      record_pointer: "/line", parse_json: true,
      distinct: ["/key"], group_by: ["/key"],
    };
    const fullEnv = session.inspect(request(entry, selector));
    const fullResult = JSON.parse(fullEnv.extraction!.segments[0]!.text);
    expect(fullResult.group_count).toBe(20);
    expect(fullResult.groups).toHaveLength(20);

    const shrunkEnv = session.inspect(request(entry, selector, 20_000, 600));
    expect(shrunkEnv.code).toBe("EXTRACTED");
    const shrunkResult = JSON.parse(shrunkEnv.extraction!.segments[0]!.text);
    expect(shrunkResult.decoded_from).toBe("/result");
    expect(shrunkResult.matched_count).toBe(20);
    expect(shrunkResult.group_count).toBe(20);
    expect(shrunkResult.groups_complete).toBe(false);
    expect(shrunkResult.groups.length).toBeLessThan(20);
  });

  it("never mutates the original snapshot", () => {
    const body = { result: JSON.stringify({ data: [{ line: JSON.stringify({ status: "200" }) }] }) };
    const { session, entry } = decodeSetup("immutable.json", body);
    const originalValue = JSON.parse(JSON.stringify(entry.snapshot.jsonValue));
    const originalSnapshotId = entry.snapshot.snapshotId;
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "/result", records_pointer: "/data",
      record_pointer: "/line", parse_json: true,
    }));
    expect(env.code).toBe("EXTRACTED");
    expect(entry.snapshot.jsonValue).toEqual(originalValue);
    expect(entry.snapshot.snapshotId).toBe(originalSnapshotId);
  });

  it("decodes a root that is itself a JSON string via decode_pointer \"\"", () => {
    // decode_pointer's "" means "the whole root" (same empty-pointer convention every other
    // pointer field already uses), so this covers a snapshot whose root value *is* a bare
    // JSON string rather than an object wrapping one, e.g. a source that serializes its
    // entire body as `"{\"data\":[...]}"`.
    const inner = JSON.stringify({ data: [{ line: JSON.stringify({ status: "200" }) }] });
    const { session, entry } = decodeSetup("empty-decode-pointer.json", inner);
    const env = session.inspect(request(entry, {
      kind: "aggregate", decode_pointer: "", records_pointer: "/data",
      record_pointer: "/line", parse_json: true,
    }));
    expect(env.code).toBe("EXTRACTED");
    const result = JSON.parse(env.extraction!.segments[0]!.text);
    expect(result.decoded_from).toBe("");
    expect(result.records_scanned).toBe(1);
    expect(result.matched_count).toBe(1);
  });

  it("aggregates JSON captured through the real oversized tool-result route", () => {
    const dir = mkdtempSync(join(tmpdir(), "shunt-aggregate-spill-"));
    const value = { data: { result: [{ values: [["0", JSON.stringify({
      level: "error", trace_id: "trace-spill", service: "billing",
    })]] }] }, padding: "x".repeat(17_000) };
    const session = new ShuntSession(
      "sess",
      makeConfig(dir, { tool_result_capture: {
        enabled: true, host_ordering_verified_locally: true,
      } }),
      makeCapability(true),
      { provider: new UnavailableProvider("MUST_NOT_RUN") },
    );
    const outcome = session.postToolResult("spill-json", JSON.stringify(value));
    expect(outcome?.action).toBe("spill");
    const entry = session.registry.resolve("sess", outcome!.sourceId!);
    expect(entry.snapshot.mediaType).toBe("text/plain");
    const env = session.inspect(request(entry, {
      kind: "aggregate", records_pointer: "/data/result", expand_pointer: "/values",
      record_pointer: "/1", parse_json: true, group_by: ["/service"],
    }));
    expect(JSON.parse(env.extraction!.segments[0]!.text)).toMatchObject({
      matched_count: 1, group_count: 1,
    });
  });
});
