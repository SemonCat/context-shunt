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
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { ShuntError } from "../src/errors.js";
import { Inspector, decodeCursor, encodeCursor, escapedJsonCost } from "../src/inspect.js";
import { DEFAULT_LIMITS as L, EMITTED_SCHEMA_VERSION } from "../src/limits.js";
import { UnavailableProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { LineIndex } from "../src/textindex.js";
import { FakeLuna, makeCapability, makeConfig } from "./support.js";

/** Direct extractor calls do not go through a session, so they state the headroom. */
const WIRE = L.maxExtendedEnvelopeBytes;

// The store loads node:sqlite through createRequire for bundler independence; the test
// reads the same database the same way.
const { DatabaseSync } = createRequire(import.meta.url)("node:sqlite") as {
  DatabaseSync: new (path: string) => {
    prepare(sql: string): { get(): unknown };
    close(): void;
  };
};

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
    expect(env.failure_detail).toBe("SNAPSHOT_MISMATCH");
    expect(env.recovery).toEqual({ handles_valid: true, actions: ["REUSE_POINTER_PAIR"] });
    expect(env.guidance).toContain("does not match its immutable snapshot");
    expect(env.guidance).toContain("Reuse the exact source_id/snapshot_id pair");
    expect(env.guidance).toContain("Recapture only if that exact original pair");
    expect(JSON.stringify(env)).not.toContain(CANARY_HEAD);

    const shortened = request(entry, { kind: "lines", start: 1, end: 2 });
    shortened["snapshot_id"] = entry.snapshot.snapshotId.slice(0, -1);
    const malformed = s.inspect(shortened);
    expect(malformed.code).toBe("INVALID_REQUEST");
    expect(malformed.failure_detail).toBe("INVALID_SNAPSHOT_ID");
    expect(malformed.recovery).toEqual({
      handles_valid: false,
      actions: ["REUSE_POINTER_PAIR"],
    });
    expect(malformed.guidance).toContain("malformed");
    expect(malformed.guidance).toContain("do not shorten");

    const original = s.inspect(request(entry, { kind: "lines", start: 1, end: 2 }));
    expect(original.code).toBe("EXTRACTED");
    expect(original.extraction?.segments[0]?.text).toContain(CANARY_HEAD);
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
    expect(seen.join("") + "\n").toBe(text);
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

  it("records nothing for a page that discloses nothing", () => {
    // Otherwise a caller paging a fruitless search grows an uncapped table for free.
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s);
    for (let i = 0; i < 30; i += 1) {
      const env = s.inspect(
        request(
          entry,
          { kind: "search", needle: "no-such-token", max_matches: 5 },
          { maxScanLines: 1 },
        ),
      );
      expect(env.extraction!.result_bytes).toBe(0);
    }
    expect(s.store.disclosedBytes(s.identity)).toBe(0);
    const db = new DatabaseSync(join(s.store.root, "store.sqlite3"));
    const row = db.prepare("SELECT COUNT(*) AS n FROM disclosure_events").get() as { n: number };
    db.close();
    expect(Number(row.n)).toBe(0);
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
        { maxResultBytes: 7, maxScanLines: 1, maxWireBytes: WIRE, state: { offset } },
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
      { maxResultBytes: 4096, maxScanLines: 100, maxWireBytes: WIRE },
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

// -- the wire budget: escaped bytes, not raw bytes ---------------------------

describe("the wire budget", () => {
  it("matches the real serializer on ordinary text", () => {
    // The hand-rolled table exists for parity, so it still has to be arithmetically right.
    for (const sample of [
      "plain ascii",
      'a "quoted" phrase',
      "back\\slash",
      "tab\there\nnewline",
      "\u0000\u0001\u001f",
      "héllo wörld",
      "✓ ✗ ∑",
      "\u{1d11e} emoji \u{1f3af}",
      "",
    ]) {
      const expected = Buffer.byteLength(JSON.stringify(sample), "utf8") - 2;
      expect(escapedJsonCost(sample)).toBe(expected);
    }
  });

  it("keeps a quote-dense page inside the envelope cap and keeps paging", () => {
    // 16384 quote characters are 16 KiB of content but 32 KiB on the wire, so budgeting
    // only on raw bytes produced an envelope the guard then refused - turning ordinary
    // source into an unexplained LIMIT_EXCEEDED. Any code or JSON file has this density.
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s, '"'.repeat(40000));
    const req = request(entry, { kind: "bytes", start: 0, end: 40000 }, { maxScanLines: 1 });
    let recovered = 0;
    for (let page = 0; page < 8; page += 1) {
      const env = s.inspect({ ...req });
      expect(env.code).toBe("EXTRACTED");
      expect(Buffer.byteLength(JSON.stringify(env), "utf8")).toBeLessThanOrEqual(
        L.maxExtendedEnvelopeBytes,
      );
      recovered += env.extraction!.result_bytes;
      if (env.extraction!.complete || !env.extraction!.next_cursor) break;
      req["cursor"] = env.extraction!.next_cursor;
    }
    // Paging must still make progress, just in smaller pages.
    expect(recovered).toBeGreaterThan(16384);
  });

  it("charges only what a quote-dense page delivered", () => {
    // The charge used to be committed before the guard, so a refused page still cost budget.
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s, '"'.repeat(40000));
    const env = s.inspect(
      request(entry, { kind: "bytes", start: 0, end: 40000 }, { maxScanLines: 1 }),
    );
    const delivered = env.extraction!.result_bytes;
    expect(delivered).toBeGreaterThan(0);
    const allowance = s.store.disclosureAllowance(s.identity, entry.sourceId);
    expect(L.disclosureMaxPerSourceBytes - allowance.perSourceRemaining).toBe(delivered);
  });

  it("leaves a quote-free page exactly as it was", () => {
    // Escaping only binds when it expands, so plain text must page exactly as before.
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s, "q".repeat(40000));
    const env = s.inspect(
      request(entry, { kind: "bytes", start: 0, end: 40000 }, { maxScanLines: 1 }),
    );
    expect(env.extraction!.result_bytes).toBe(16384);
  });

  it("pages a one-line tool result as bytes instead of failing default inspect", () => {
    // The Hermes retirement evidence contained many large one-line results. Previously a
    // normal `lines` inspection of one of those records stalled and became a bare
    // `LIMIT_EXCEEDED` even though a bounded byte selector could make progress.
    const dir = tmp();
    const s = session(dir);
    const body = JSON.stringify({
      records: Array.from({ length: 320 }, (_, i) => ({ id: i, value: "x".repeat(96) })),
    });
    const entry = captured(dir, s, body + "\n");
    const req = request(entry, { kind: "lines", start: 1, end: 1 });
    const recovered: string[] = [];
    let env = s.inspect({ ...req });
    for (let page = 0; page < 32; page += 1) {
      expect(env.code).toBe("EXTRACTED");
      expect(env.extraction!.result_bytes).toBeGreaterThan(0);
      expect(env.extraction!.mode).toBe("bytes");
      expect(env.extraction!.segments[0]!.kind).toBe("bytes");
      recovered.push(env.extraction!.segments[0]!.text);
      if (env.extraction!.complete) break;
      req["cursor"] = env.extraction!.next_cursor;
      env = s.inspect({ ...req });
    }
    expect(recovered.join("")).toBe(body);
    expect(env.status).toBe("ok");
  });

  it("names a real refusal when inspect has no wire headroom", () => {
    const dir = tmp();
    const s = session(dir);
    const entry = captured(dir, s, '"'.repeat(30000) + "\n");
    const result = new Inspector().extract(
      entry.snapshot.data,
      new LineIndex(entry.snapshot.data),
      { kind: "lines", start: 1, end: 1 },
      { maxResultBytes: L.inspectMaxResultBytes, maxScanLines: L.inspectMaxScanLines, maxWireBytes: 1 },
    );
    expect(result.stalled).toBe(true);
    expect(result.stallReason).toBe("wire");
  });

  it("returns a bounded hit when searching a large one-line tool result", () => {
    const dir = tmp();
    const s = session(dir);
    const body = JSON.stringify({
      records: Array.from({ length: 320 }, (_, i) => ({ id: i, value: "x".repeat(96) })),
      needle: "target",
    });
    const entry = captured(dir, s, body + "\n");
    const env = s.inspect(request(entry, { kind: "search", needle: "target", max_matches: 1 }));
    expect(env.code).toBe("EXTRACTED");
    expect(env.extraction!.mode).toBe("search");
    expect(env.extraction!.segments[0]!.kind).toBe("bytes");
    expect(env.extraction!.segments[0]!.text).toContain("target");
    expect(env.extraction!.complete).toBe(false);
    expect(env.status).toBe("partial");
    expect(env.guidance).toContain("bytes selector");
  });

  it("reports disclosure exhaustion when one byte remains for a multibyte line", () => {
    const dir = tmp();
    const s = session(dir, {
      overrides: {
        limits: {
          disclosure_max_per_source_bytes: 1,
          disclosure_max_per_session_bytes: 1,
        },
      },
    });
    const entry = captured(dir, s, "é");
    const env = s.inspect(request(entry, { kind: "lines", start: 1, end: 1 }));
    expect(env.code).toBe("DISCLOSURE_EXHAUSTED");
    expect(env.extraction!.result_bytes).toBe(0);
  });
});

// -- UTF-8 boundary handling on an exact byte page --------------------------

describe("utf-8 boundaries on a byte page", () => {
  /**
   * A complete trailing character must survive an exact byte page.
   *
   * `backToBoundary` walked back over the trailing character's continuation bytes and
   * then dropped its lead byte too, so a range covering a whole string silently lost its
   * last character - `日本` over bytes 0..6 returned only `日`. Exact extraction that
   * quietly drops source bytes is the one thing this mode may never do.
   */
  for (const text of ["aé", "日本", "aβc", "🎯", "aa🎯", "ascii-only"]) {
    it(`keeps every character that fits: ${text}`, () => {
      const dir = tmp();
      const s = session(dir);
      const entry = captured(dir, s, text);
      const raw = enc(text);
      const env = s.inspect(request(entry, { kind: "bytes", start: 0, end: raw.length })) as {
        code: string;
        extraction: { segments: { text: string }[] };
      };
      expect(env.code).toBe("EXTRACTED");
      expect(env.extraction.segments.map((seg) => seg.text).join("")).toBe(text);
    });
  }

  it("explicitly rejects a byte selector cut inside a codepoint", () => {
    const dir = tmp(); const s = session(dir); const entry = captured(dir, s, "日本");
    for (const end of [3, 4, 5, 6]) {
      const env = s.inspect(request(entry, { kind: "bytes", start: 0, end }));
      if (end === 4 || end === 5) {
        expect(env.code).toBe("INVALID_REQUEST");
        expect(env.extraction).toBeUndefined();
      } else {
        expect(env.extraction!.segments.map((segment) => segment.text).join(""))
          .toBe("日本".slice(0, end / 3));
      }
    }
  });
});


it("never claims full matching-line coverage for an oversized search window", () => {
  const dir = tmp();
  const s = session(dir);
  const entry = captured(dir, s, "prefix target " + "tail ".repeat(10000));
  const env = s.inspect(request(entry, { kind: "search", needle: "target", max_matches: 1 }));
  expect(env.code).toBe("EXTRACTED");
  expect(env.status).toBe("partial");
  expect(env.coverage.complete).toBe(false);
  expect(env.coverage.omitted.length).toBeGreaterThan(0);
  expect(env.extraction!.complete).toBe(false);
  expect(env.guidance).toContain("bytes selector");
});

it.each([["éA", 1, 2], ["éA", 1, 3], ["é", 0, 1]] as const)(
  "rejects partial UTF-8 selector %s [%i,%i) without disclosure", (text, start, end) => {
    const dir = tmp(); const s = session(dir); const entry = captured(dir, s, text);
    const env = s.inspect(request(entry, { kind: "bytes", start, end }));
    expect(env.code).toBe("INVALID_REQUEST");
    expect(env.extraction).toBeUndefined();
    expect(s.store.disclosureAllowance(s.identity, entry.sourceId).perSourceRemaining)
      .toBe(L.disclosureMaxPerSourceBytes);
  },
);

it("cannot skip an undisclosed UTF-8 codepoint when the byte budget is too small", () => {
  const dir = tmp(); const s = session(dir); const entry = captured(dir, s, "é");
  const req = request(entry, { kind: "bytes", start: 0, end: 2 }, { maxResultBytes: 1 });
  const env = s.inspect(req);
  expect(env.code).toBe("LEGACY_COMPACTED");
  expect(env.coverage.complete).toBe(false);
  expect(env.legacy_compaction!.summary_bytes).toBeLessThanOrEqual(1);
  expect(env.extraction).toBeUndefined();
  expect(s.store.disclosureAllowance(s.identity, entry.sourceId).perSourceRemaining)
    .toBe(L.disclosureMaxPerSourceBytes - env.legacy_compaction!.summary_bytes);
  const recovered = s.inspect(request(entry, { kind: "bytes", start: 0, end: 2 }, { maxResultBytes: 2 }));
  expect(recovered.extraction!.segments[0]!.text).toBe("é");
  expect(recovered.extraction!.complete).toBe(true);
});

for (const suffix of ["\nB\n", "\nB", "\n\nB\n", "\nB\nC\n", "\r\nB\r\n"]) {
  it.each([[20000, 16384], [16384, 16384], [10, 10]])(
    `preserves all selected LF bytes across pages with suffix ${JSON.stringify(suffix)}, width %i budget %i`,
    (width, budget) => {
      const dir = tmp(); const s = session(dir); const text = "A".repeat(width!) + suffix;
      const entry = captured(dir, s, text);
      const req = request(entry, { kind: "lines", start: 1, end: entry.snapshot.lineIndex.lineCount }, { maxResultBytes: budget! });
      const parts: string[] = [];
      let done = false;
      let disclosed = 0;
      for (let i = 0; i < 32; i++) {
        const env = s.inspect({ ...req });
        expect(env.code).toBe("EXTRACTED");
        parts.push(...env.extraction!.segments.map((segment) => segment.text));
        disclosed = env.extraction!.disclosed_bytes_source;
        if (env.extraction!.complete) { done = true; break; }
        req["cursor"] = env.extraction!.next_cursor;
      }
      expect(done).toBe(true);
      const expected = text.endsWith("\n") ? text.slice(0, -1) : text;
      expect(parts.join("")).toBe(expected);
      expect(disclosed).toBe(Buffer.byteLength(expected));
    },
  );
}

it("makes every small UTF-8 range exact or explicitly unable to progress", () => {
  const raw = enc("éA🎯\nB"); const index = new LineIndex(raw); const inspector = new Inspector();
  const boundaries = new Set([0, 2, 3, 7, 8, 9]);
  for (let start = 0; start <= raw.length; start++) {
    for (let end = start; end <= raw.length; end++) {
      for (let budget = 1; budget <= 5; budget++) {
        let state = {}; let returned = ""; let stopped = false;
        for (let n = 0; n <= raw.length; n++) {
          let page;
          try {
            page = inspector.extract(raw, index, { kind: "bytes", start, end }, {
              maxResultBytes: budget, maxScanLines: 1, maxWireBytes: WIRE, state,
            });
          } catch (error) {
            expect(error).toBeInstanceOf(ShuntError);
            expect((error as ShuntError).code).toBe("INVALID_REQUEST");
            expect(start !== end && (!boundaries.has(start) || !boundaries.has(end))).toBe(true);
            expect(returned).toBe(""); stopped = true; break;
          }
          for (const segment of page.segments) {
            expect(segment.start).toBeGreaterThanOrEqual(start);
            expect(segment.end).toBeLessThanOrEqual(end);
            expect(enc(segment.text)).toEqual(raw.subarray(segment.start, segment.end));
            returned += segment.text;
          }
          if (page.stalled) {
            expect(page.complete).toBe(false); expect(page.segments).toEqual([]);
            expect(page.nextCursorState).toEqual({ offset: start + Buffer.byteLength(returned) });
            stopped = true; break;
          }
          if (page.complete) {
            expect(enc(returned)).toEqual(raw.subarray(start, end)); stopped = true; break;
          }
          expect(page.nextCursorState).toEqual({ offset: start + Buffer.byteLength(returned) });
          state = page.nextCursorState!;
        }
        expect(stopped).toBe(true);
      }
    }
  }
});
