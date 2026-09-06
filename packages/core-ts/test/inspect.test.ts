/**
 * unit inspect (TypeScript core) - deterministic extraction and the limits that make it
 * safe.
 *
 * The escape hatch that returns real source bytes is the one most worth attacking, so this
 * gate covers: zero model calls, the exact per-result cap, cursor authenticity, the scan
 * budget, and the cumulative disclosure ceiling that stops paging from reassembling a whole
 * payload in the main context.
 */
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { ShuntError } from "../src/errors.js";
import { Inspector, decodeCursor, encodeCursor } from "../src/inspect.js";
import { DEFAULT_LIMITS as L, EMITTED_SCHEMA_VERSION } from "../src/limits.js";
import { UnavailableProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { LineIndex } from "../src/textindex.js";
import { FakeLuna, makeCapability, makeConfig } from "./support.js";

const enc = (s: string) => new TextEncoder().encode(s);
const CANARY_HEAD = "CANARY-HEAD-1a2b3c";
const CANARY_MID = "CANARY-MID-4d5e6f";
const CANARY_TAIL = "CANARY-TAIL-7a8b9c";

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-inspect-"));
}

function body(lines = 600): string {
  const rows = Array.from({ length: lines }, (_, i) => `row ${String(i + 1).padStart(4, "0")} value-${i + 1}`);
  rows[0] = `${rows[0]} ${CANARY_HEAD}`;
  rows[Math.floor(lines / 2)] = `${rows[Math.floor(lines / 2)]} ${CANARY_MID}`;
  rows[lines - 1] = `${rows[lines - 1]} ${CANARY_TAIL}`;
  return rows.join("\n") + "\n";
}

/** A session whose provider fails on every call, so a model call would be visible. */
function session(
  dir: string,
  opts: { provider?: FakeLuna; overrides?: Record<string, unknown> } = {},
): ShuntSession {
  const config = makeConfig(dir, opts.overrides ?? {});
  return new ShuntSession("sess", config, makeCapability(), {
    provider: opts.provider ?? new UnavailableProvider("SHOULD_NOT_BE_CALLED"),
  });
}

function captured(dir: string, s: ShuntSession, text?: string) {
  const ws = join(dir, "ws");
  mkdirSync(ws, { recursive: true });
  const path = join(ws, "big.txt");
  writeFileSync(path, text ?? body());
  return s.registerPath(path);
}

function request(
  entry: { sourceId: string; snapshot: { snapshotId: string } },
  selector: Record<string, unknown>,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  const { maxResultBytes, maxScanLines, ...rest } = extra as {
    maxResultBytes?: number;
    maxScanLines?: number;
  };
  return {
    schema_version: EMITTED_SCHEMA_VERSION,
    request_id: "req_i1",
    operation: "inspect",
    source_id: entry.sourceId,
    snapshot_id: entry.snapshot.snapshotId,
    selector,
    budgets: {
      max_result_bytes: maxResultBytes ?? L.inspectMaxResultBytes,
      max_scan_lines: maxScanLines ?? L.inspectMaxScanLines,
    },
    ...rest,
  };
}

// -- deterministic, and labelled as such ------------------------------------

describe("deterministic extraction", () => {
  it("returns exact bytes and labels itself not derived", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s);
    const env = s.inspect(request(entry, { kind: "lines", start: 1, end: 3 }));
    expect(env.status).toBe("ok");
    expect(env.code).toBe("EXTRACTED");
    expect(env.result_kind).toBe("deterministic_extraction");
    expect(env.provenance!.derived).toBe(false);
    expect(env.provenance!.attribution_status).toBe("not_applicable");
    expect(env.extraction!.deterministic).toBe(true);
    // The bytes are a literal substring of the snapshot, not a summary of it.
    const text = env.extraction!.segments[0]!.text;
    expect(new TextDecoder().decode(entry.snapshot.data)).toContain(text);
    expect(text).toContain(CANARY_HEAD);
    expect(env.answer).toBe("");
    expect(env.citations).toEqual([]);
  });

  it("makes zero model calls", () => {
    const dir = tmp();
    const luna = new FakeLuna();
    const s = session(dir, { provider: luna });
    const entry = captured(dir, s);
    for (const selector of [
      { kind: "lines", start: 5, end: 9 },
      { kind: "bytes", start: 0, end: 64 },
      { kind: "search", needle: "value-42", max_matches: 3 },
    ]) {
      expect(s.inspect(request(entry, selector)).code).toBe("EXTRACTED");
    }
    expect(luna.callCount).toBe(0);
  });

  it("refuses a snapshot mismatch rather than answering from a newer snapshot", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s);
    const req = request(entry, { kind: "lines", start: 1, end: 2 });
    req["snapshot_id"] = "sha256:" + "0".repeat(64);
    const env = s.inspect(req);
    expect(env.code).toBe("SOURCE_CHANGED");
    expect(JSON.stringify(env)).not.toContain(CANARY_HEAD);
  });

  it("yields nothing for a foreign or expired handle", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s);
    const req = request(entry, { kind: "lines", start: 1, end: 2 });
    req["source_id"] = "src_" + "0".repeat(16);
    const env = s.inspect(req);
    expect(env.code).toBe("SOURCE_EXPIRED");
    expect(JSON.stringify(env)).not.toContain(CANARY_HEAD);
  });
});

// -- caps -------------------------------------------------------------------

describe("per-result caps", () => {
  it("never exceeds the sixteen KiB result cap", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s, Array.from({ length: 400 }, () => "x".repeat(127)).join("\n") + "\n");
    const env = s.inspect(request(entry, { kind: "lines", start: 1, end: 400 }));
    expect(env.extraction!.result_bytes).toBeLessThanOrEqual(16384);
    expect(env.extraction!.complete).toBe(false);
  });

  it("hits the cap exactly on a byte page", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s, "q".repeat(40000));
    const env = s.inspect(
      request(entry, { kind: "bytes", start: 0, end: 40000 }, { maxScanLines: 1 }),
    );
    expect(env.extraction!.result_bytes).toBe(16384);
    expect(enc(env.extraction!.segments[0]!.text).length).toBe(16384);
    expect(env.extraction!.complete).toBe(false);
  });

  it("stops a fruitless search at the scan budget", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s);
    const env = s.inspect(
      request(entry, { kind: "search", needle: "no-such-token", max_matches: 50 }, { maxScanLines: 25 }),
    );
    expect(env.extraction!.lines_scanned).toBe(25);
    expect(env.extraction!.scan_budget_exhausted).toBe(true);
    expect(env.extraction!.matches_found).toBe(0);
    expect(env.status).toBe("partial");
    expect(env.coverage.omitted.some((o) => o.reason === "SCAN_BUDGET_EXHAUSTED")).toBe(true);
  });
});

// -- pagination and cursors -------------------------------------------------

describe("pagination and cursors", () => {
  it("walks the range in order without gaps or repeats", () => {
    const dir = tmp();
    const s = session(dir);
    const text = body(300);
    const entry = captured(dir, s, text);
    const seen: string[] = [];
    const req = request(entry, { kind: "lines", start: 1, end: 300 }, { maxResultBytes: 400 });
    for (let i = 0; i < 50; i += 1) {
      const env = s.inspect({ ...req });
      const extraction = env.extraction!;
      if (extraction.segments.length > 0) seen.push(extraction.segments[0]!.text);
      if (extraction.complete || !extraction.next_cursor) break;
      req["cursor"] = extraction.next_cursor;
    }
    expect(seen.join("\n") + "\n").toBe(text);
  });

  it("refuses a tampered cursor before any scan", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s);
    const first = s.inspect(
      request(entry, { kind: "lines", start: 1, end: 300 }, { maxResultBytes: 200 }),
    );
    const cursor = first.extraction!.next_cursor!;
    expect(cursor).toBeTruthy();
    const flipped = cursor.slice(0, -1) + (cursor.endsWith("A") ? "B" : "A");
    const env = s.inspect(
      request(entry, { kind: "lines", start: 1, end: 300 }, { cursor: flipped }),
    );
    expect(env.code).toBe("INVALID_REQUEST");
  });

  it("does not let a cursor move to another selector", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s);
    const first = s.inspect(
      request(entry, { kind: "lines", start: 1, end: 300 }, { maxResultBytes: 200 }),
    );
    const cursor = first.extraction!.next_cursor!;
    // Same handle, same snapshot, different selector: the binding no longer matches.
    const env = s.inspect(request(entry, { kind: "lines", start: 1, end: 50 }, { cursor }));
    expect(env.code).toBe("INVALID_REQUEST");
  });

  it("does not authenticate a cursor from another store", () => {
    // The MAC key lives in the store's own metadata, so a cursor is not portable.
    const dirA = tmp();
    const dirB = tmp();
    const a = session(dirA);
    const b = session(dirB);
    const entry = captured(dirA, a);
    const selector = { kind: "lines", start: 1, end: 300 };
    const forged = encodeCursor(
      b.store.cursorKey(), entry.sourceId, entry.snapshot.snapshotId, selector, { line: 200 },
    );
    expect(a.inspect(request(entry, selector, { cursor: forged })).code).toBe("INVALID_REQUEST");
  });

  it("keeps cursor state opaque and content-free", () => {
    const key = new Uint8Array(32).fill(107);
    const selector = { kind: "lines", start: 1, end: 10 };
    const token = encodeCursor(key, "src_abcd1234", "sha256:" + "1".repeat(64), selector, { line: 5 });
    expect(token.startsWith("csr_")).toBe(true);
    expect(decodeCursor(key, token, "src_abcd1234", "sha256:" + "1".repeat(64), selector))
      .toEqual({ line: 5 });
    expect(() =>
      decodeCursor(key, token, "src_other1234", "sha256:" + "1".repeat(64), selector),
    ).toThrowError(ShuntError);
  });
});

// -- the cumulative ceiling -------------------------------------------------

describe("the cumulative disclosure ceiling", () => {
  it("stops repeated pages from refilling the main context", () => {
    // The whole point: a per-result cap alone would just be defeated by paging.
    const dir = tmp();
    const s = session(dir, { overrides: { limits: { disclosure_max_per_source_bytes: 4096 } } });
    const entry = captured(dir, s);
    let disclosed = 0;
    let exhausted = false;
    const req = request(entry, { kind: "lines", start: 1, end: 600 }, { maxResultBytes: 1024 });
    for (let i = 0; i < 50; i += 1) {
      const env = s.inspect({ ...req });
      const extraction = env.extraction!;
      disclosed += extraction.result_bytes;
      if (env.code === "DISCLOSURE_EXHAUSTED") {
        exhausted = true;
        expect(extraction.result_bytes).toBe(0);
        expect(extraction.disclosure_limit_reached).toBe(true);
        expect(env.recovery!.handles_valid).toBe(true);
        break;
      }
      if (!extraction.next_cursor) break;
      req["cursor"] = extraction.next_cursor;
    }
    expect(exhausted).toBe(true);
    expect(disclosed).toBeLessThanOrEqual(4096);
    // Well short of the whole file: the payload cannot be reassembled this way.
    expect(disclosed).toBeLessThan(entry.snapshot.bytesLen);
  });

  it("reports the ceiling on every page", () => {
    const dir = tmp();
    const s = session(dir, { overrides: { limits: { disclosure_max_per_source_bytes: 8192 } } });
    const entry = captured(dir, s);
    const env = s.inspect(
      request(entry, { kind: "lines", start: 1, end: 20 }, { maxResultBytes: 512 }),
    );
    const extraction = env.extraction!;
    expect(extraction.disclosed_bytes_source).toBe(extraction.result_bytes);
    expect(extraction.disclosed_bytes_session).toBe(extraction.result_bytes);
    expect(extraction.disclosure_limit_reached).toBe(false);
  });
});

// -- extractor unit behaviour ----------------------------------------------

describe("extractor behaviour", () => {
  it("never splits a character on a byte range", () => {
    const data = enc("héllo wörld ✓ ".repeat(200));
    const inspector = new Inspector();
    let offset = 0;
    const parts: string[] = [];
    for (let i = 0; i < 4000; i += 1) {
      const result = inspector.extract(
        data,
        new LineIndex(data),
        { kind: "bytes", start: 0, end: data.length },
        { maxResultBytes: 7, maxScanLines: 1, state: { offset } },
      );
      if (result.segments.length === 0) break;
      parts.push(result.segments[0]!.text);
      if (result.nextCursorState === undefined) break;
      offset = result.nextCursorState.offset as number;
    }
    expect(parts.join("")).toBe(new TextDecoder().decode(data));
  });

  it("accepts only a literal needle", () => {
    // A regex-looking needle is matched literally, so no pattern can be made to backtrack.
    const data = enc("plain (a+)+b line\nliteral (a+)+b here\n");
    const result = new Inspector().extract(
      data,
      new LineIndex(data),
      { kind: "search", needle: "(a+)+b", max_matches: 5 },
      { maxResultBytes: 4096, maxScanLines: 100 },
    );
    expect(result.matchesFound).toBe(2);
  });

  it("treats a range past the end as an empty exact answer", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s, "one\ntwo\n");
    const env = s.inspect(request(entry, { kind: "lines", start: 500, end: 600 }));
    expect(env.code).toBe("EXTRACTED");
    expect(env.extraction!.segments).toEqual([]);
    expect(env.extraction!.result_bytes).toBe(0);
  });
});
