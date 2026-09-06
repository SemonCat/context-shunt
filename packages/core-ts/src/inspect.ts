/**
 * Deterministic extraction: exact snapshot bytes, zero model calls.
 *
 * `context_shunt_inspect` is the escape hatch for "I need to see the actual text", and it
 * is deliberately not a retrieval tool. Four properties make it safe to hand to an agent:
 *
 * **It is exact, and it says so.** Every byte returned is copied from the immutable
 * snapshot the caller named. The envelope is labelled `deterministic_extraction` with
 * `provenance.derived = false`, so it can never be read as a summary. No provider is
 * consulted; the class below has no provider reference at all, which is what makes "zero
 * LLM calls" a structural fact rather than a promise.
 *
 * **It is bounded per result.** One page is capped at `inspect.max_result_bytes` (16 KiB),
 * measured on the UTF-8 bytes of the emitted segments.
 *
 * **It is bounded cumulatively.** Paging is the obvious way to defeat a per-result cap, so
 * every page is charged against a per-source and a per-session disclosure ceiling before a
 * byte is returned. There is no configuration in which repeated small reads can reassemble
 * a whole payload into the main context.
 *
 * **Continuation is authenticated, not arithmetic.** A cursor is an opaque HMAC-tagged
 * token bound to the handle, the snapshot hash and the canonical selector. It cannot be
 * edited to jump the scan budget, cannot be pointed at a different snapshot, and cannot be
 * replayed into another store, because the key lives in that store's metadata.
 *
 * Scanning is linear: only a literal needle is accepted, never a regular expression, so no
 * caller-supplied pattern can be made to backtrack.
 */
import { createHash, createHmac, timingSafeEqual } from "node:crypto";

import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { LineIndex } from "./textindex.js";

export const CURSOR_PREFIX = "csr_";
const CURSOR_VERSION = 1;
const MAC_BYTES = 16;

export interface Segment {
  kind: "lines" | "bytes";
  start: number;
  end: number;
  text: string;
}

export interface CursorState {
  line?: number;
  offset?: number;
  matches?: number;
}

export interface Extraction {
  mode: "lines" | "bytes" | "search";
  segments: Segment[];
  resultBytes: number;
  complete: boolean;
  nextCursorState: CursorState | undefined;
  linesScanned: number;
  scanBudgetExhausted: boolean;
  matchesFound: number | undefined;
  /**
   * True when the page emitted nothing *and* the scan position did not move, so a caller
   * following `next_cursor` would loop forever. The extractor knows the start position, so
   * it is the only place that can tell this apart from an honest empty page (a scan-budget
   * stop emits nothing but does advance).
   */
  stalled: boolean;
}

function emptyExtraction(mode: Extraction["mode"]): Extraction {
  return {
    mode,
    segments: [],
    resultBytes: 0,
    complete: true,
    nextCursorState: undefined,
    linesScanned: 0,
    scanBudgetExhausted: false,
    matchesFound: undefined,
    stalled: false,
  };
}

/** Stable text a cursor is bound to. Any selector change invalidates the cursor. */
export function canonicalSelector(selector: Record<string, unknown>): string {
  const keys = Object.keys(selector).sort();
  return JSON.stringify(Object.fromEntries(keys.map((key) => [key, selector[key]])));
}

function binding(
  handleId: string,
  snapshotId: string,
  selector: Record<string, unknown>,
): string {
  const material = [handleId, snapshotId, canonicalSelector(selector)].join("");
  return createHash("sha256").update(material, "utf8").digest("hex").slice(0, 32);
}

function base64UrlNoPad(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

export function encodeCursor(
  key: Uint8Array,
  handleId: string,
  snapshotId: string,
  selector: Record<string, unknown>,
  state: CursorState,
): string {
  const orderedState = Object.fromEntries(
    Object.keys(state).sort().map((name) => [name, (state as Record<string, unknown>)[name]]),
  );
  const payload = Buffer.from(
    JSON.stringify({ b: binding(handleId, snapshotId, selector), s: orderedState, v: CURSOR_VERSION }),
    "utf8",
  );
  const mac = createHmac("sha256", key).update(payload).digest().subarray(0, MAC_BYTES);
  return CURSOR_PREFIX + base64UrlNoPad(Buffer.concat([payload, mac]));
}

/**
 * Authenticate a cursor and confirm it belongs to exactly this request. Every rejection is
 * the same bounded error, so a caller probing with edited cursors learns nothing about
 * which check failed.
 */
export function decodeCursor(
  key: Uint8Array,
  token: string,
  handleId: string,
  snapshotId: string,
  selector: Record<string, unknown>,
): CursorState {
  const bad = (): never => {
    throw new ShuntError("INVALID_REQUEST", "BAD_CURSOR", false);
  };
  if (typeof token !== "string" || !token.startsWith(CURSOR_PREFIX)) bad();
  let raw: Buffer;
  try {
    raw = Buffer.from(token.slice(CURSOR_PREFIX.length), "base64url");
  } catch {
    return bad();
  }
  if (raw.length <= MAC_BYTES) bad();
  const payload = raw.subarray(0, raw.length - MAC_BYTES);
  const mac = raw.subarray(raw.length - MAC_BYTES);
  const expected = createHmac("sha256", key).update(payload).digest().subarray(0, MAC_BYTES);
  if (mac.length !== expected.length || !timingSafeEqual(mac, expected)) bad();
  let decoded: unknown;
  try {
    decoded = JSON.parse(payload.toString("utf8"));
  } catch {
    return bad();
  }
  if (
    typeof decoded !== "object"
    || decoded === null
    || (decoded as { v?: unknown }).v !== CURSOR_VERSION
    || (decoded as { b?: unknown }).b !== binding(handleId, snapshotId, selector)
    || typeof (decoded as { s?: unknown }).s !== "object"
    || (decoded as { s?: unknown }).s === null
  ) {
    bad();
  }
  return { ...(decoded as { s: CursorState }).s };
}

const encoder = new TextEncoder();

function utf8Length(text: string): number {
  return encoder.encode(text).length;
}

/** Stateless extractor over one immutable snapshot. Holds no provider reference. */
export class Inspector {
  constructor(private readonly limits: Limits = DEFAULT_LIMITS) {}

  extract(
    data: Uint8Array,
    index: LineIndex,
    selector: Record<string, unknown>,
    opts: { maxResultBytes: number; maxScanLines: number; state?: CursorState },
  ): Extraction {
    const budget = Math.min(
      opts.maxResultBytes,
      this.limits.inspectMaxResultBytes,
      this.limits.maxExtractionBytes,
    );
    if (budget <= 0) throw new ShuntError("INVALID_REQUEST", "ZERO_RESULT_BUDGET", false);
    const scanBudget = Math.min(opts.maxScanLines, this.limits.inspectMaxScanLines);
    const state = opts.state ?? {};
    switch (selector["kind"]) {
      case "lines":
        return this.lines(index, selector, budget, scanBudget, state);
      case "bytes":
        return this.bytes(data, selector, budget, state);
      case "search":
        return this.search(index, selector, budget, scanBudget, state);
      default:
        throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
    }
  }

  // -- lines ---------------------------------------------------------------

  private lines(
    index: LineIndex,
    selector: Record<string, unknown>,
    budget: number,
    scanBudget: number,
    state: CursorState,
  ): Extraction {
    const requestedStart = Number(selector["start"]);
    const requestedEnd = Number(selector["end"]);
    if (requestedEnd < requestedStart) {
      throw new ShuntError("INVALID_REQUEST", "BAD_LINE_RANGE", false);
    }
    const start = Math.max(requestedStart, state.line ?? requestedStart);
    const end = Math.min(requestedEnd, index.lineCount);
    const out = emptyExtraction("lines");
    // The range is entirely past the end of the snapshot: an empty exact answer.
    if (start > end) return out;

    const emitted: string[] = [];
    let used = 0;
    let ordinal = start;
    const pageLines = Math.min(this.limits.inspectMaxLinesPerPage, scanBudget);
    while (ordinal <= end && out.linesScanned < pageLines) {
      let line: string;
      try {
        line = index.lineText(ordinal);
      } catch {
        break;
      }
      const chunk = emitted.length === 0 ? line : `\n${line}`;
      const size = utf8Length(chunk);
      if (used + size > budget) break;
      emitted.push(line);
      used += size;
      out.linesScanned += 1;
      ordinal += 1;
    }

    if (emitted.length > 0) {
      out.segments.push({ kind: "lines", start, end: ordinal - 1, text: emitted.join("\n") });
    }
    out.resultBytes = used;
    out.scanBudgetExhausted = out.linesScanned >= pageLines && ordinal <= end;
    if (ordinal <= end) {
      out.complete = false;
      out.nextCursorState = { line: ordinal };
      out.stalled = ordinal === start && emitted.length === 0;
    }
    return out;
  }

  // -- bytes ---------------------------------------------------------------

  private bytes(
    data: Uint8Array,
    selector: Record<string, unknown>,
    budget: number,
    state: CursorState,
  ): Extraction {
    const requestedStart = Number(selector["start"]);
    const requestedEnd = Number(selector["end"]);
    if (requestedEnd < requestedStart) {
      throw new ShuntError("INVALID_REQUEST", "BAD_BYTE_RANGE", false);
    }
    const start = Math.max(requestedStart, state.offset ?? requestedStart);
    const end = Math.min(requestedEnd, data.length);
    const out = emptyExtraction("bytes");
    if (start >= end) return out;

    const take = Math.min(end - start, budget, this.limits.inspectMaxBytesPerPage);
    // A byte range can land inside a multi-byte character. Both edges are pulled to a
    // UTF-8 boundary so the emitted text is exactly a substring of the snapshot and never
    // a mojibake fragment; the cursor resumes from the boundary actually used.
    const begin = forwardToBoundary(data, start);
    const finish = backToBoundary(data, begin, begin + take);
    if (finish <= begin) {
      out.complete = end <= begin;
      if (out.complete) return out;
      // Nothing fits without splitting a character; advancing is the only honest move.
      const advanced = Math.min(end, begin + 1);
      out.nextCursorState = { offset: advanced };
      out.stalled = advanced <= start;
      return out;
    }
    const text = new TextDecoder("utf-8", { fatal: true }).decode(data.subarray(begin, finish));
    out.segments.push({ kind: "bytes", start: begin, end: finish, text });
    out.resultBytes = finish - begin;
    if (finish < end) {
      out.complete = false;
      out.nextCursorState = { offset: finish };
    }
    return out;
  }

  // -- search --------------------------------------------------------------

  private search(
    index: LineIndex,
    selector: Record<string, unknown>,
    budget: number,
    scanBudget: number,
    state: CursorState,
  ): Extraction {
    const needle = String(selector["needle"]);
    if (utf8Length(needle) > this.limits.inspectMaxNeedleBytes) {
      throw new ShuntError("INVALID_REQUEST", "NEEDLE_OVER_CAP", false);
    }
    const maxMatches = Math.min(
      Number(selector["max_matches"]),
      this.limits.inspectMaxSearchMatches,
    );
    const context = Number(selector["context_lines"] ?? 0);
    const out = emptyExtraction("search");
    out.matchesFound = 0;

    let ordinal = Math.max(1, state.line ?? 1);
    const already = Math.max(0, state.matches ?? 0);
    let used = 0;
    const remainingMatches = maxMatches - already;
    if (remainingMatches <= 0) return out;

    while (ordinal <= index.lineCount && out.linesScanned < scanBudget) {
      let line: string;
      try {
        line = index.lineText(ordinal);
      } catch {
        ordinal += 1;
        out.linesScanned += 1;
        continue;
      }
      out.linesScanned += 1;
      if (line.includes(needle)) {
        const low = Math.max(1, ordinal - context);
        const high = Math.min(index.lineCount, ordinal + context);
        let text: string;
        try {
          text = index.rangeText(low, high);
        } catch {
          ordinal += 1;
          continue;
        }
        const size = utf8Length(text);
        if (used + size > budget || out.segments.length >= this.limits.inspectMaxSegments) {
          out.complete = false;
          out.nextCursorState = { line: ordinal, matches: already + (out.matchesFound ?? 0) };
          out.resultBytes = used;
          return out;
        }
        out.segments.push({ kind: "lines", start: low, end: high, text });
        used += size;
        out.matchesFound = (out.matchesFound ?? 0) + 1;
        if ((out.matchesFound ?? 0) >= remainingMatches) {
          ordinal += 1;
          break;
        }
      }
      ordinal += 1;
    }

    out.resultBytes = used;
    const moreLines = ordinal <= index.lineCount;
    const matchedAll = (out.matchesFound ?? 0) >= remainingMatches;
    if (moreLines && out.linesScanned >= scanBudget && !matchedAll) {
      out.scanBudgetExhausted = true;
    }
    if (moreLines && !matchedAll) {
      out.complete = false;
      out.nextCursorState = { line: ordinal, matches: already + (out.matchesFound ?? 0) };
    }
    return out;
  }
}

/** Advance to the next UTF-8 character start at or after `offset`. */
function forwardToBoundary(data: Uint8Array, offset: number): number {
  let position = offset;
  while (position < data.length && ((data[position] as number) & 0xc0) === 0x80) position += 1;
  return position;
}

/** Pull back to the last UTF-8 character end at or before `offset`. */
function backToBoundary(data: Uint8Array, begin: number, offset: number): number {
  let position = Math.min(offset, data.length);
  while (position > begin && ((data[position - 1] as number) & 0xc0) === 0x80) position -= 1;
  if (position > begin) {
    const lead = data[position - 1] as number;
    // `position` sits just after a lead byte whose continuation bytes were cut.
    if (sequenceWidth(lead) > 1) position -= 1;
  }
  return position;
}

function sequenceWidth(lead: number): number {
  if (lead < 0x80) return 1;
  if (lead >= 0xf0) return 4;
  if (lead >= 0xe0) return 3;
  if (lead >= 0xc0) return 2;
  return 1;
}
