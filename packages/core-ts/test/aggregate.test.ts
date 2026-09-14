import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { UnavailableProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
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
});
