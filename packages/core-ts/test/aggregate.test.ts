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

function request(entry: ReturnType<ShuntSession["registerPath"]>, selector: object, maxScan = 20_000) {
  return {
    schema_version: "1.3", request_id: "req_aggregate", operation: "inspect",
    source_id: entry.sourceId, snapshot_id: entry.snapshot.snapshotId, selector,
    budgets: { max_result_bytes: 16_384, max_scan_lines: maxScan },
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
