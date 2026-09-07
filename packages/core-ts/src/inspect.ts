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
 * a *large* payload into the main context. A source small enough to fit the per-page,
 * per-source and per-session ceilings can be returned in full - the pre-read gate blocks a
 * read on context cost, not on confidentiality - so the guarantee here is the byte budget
 * and the accounting of it, not that a source can never come back whole.
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

/**
 * Characters JSON gives a two-byte short escape. Everything else below 0x20 costs six
 * (`\u00XX`); everything at or above it costs its UTF-8 length.
 */
const SHORT_ESCAPES = new Set([0x22, 0x5c, 0x08, 0x0c, 0x0a, 0x0d, 0x09]);

/**
 * Serialized bytes `text` occupies inside a JSON string, excluding the quote marks.
 *
 * Hand-rolled on purpose. `JSON.stringify` and Python's `json.dumps(ensure_ascii=False)`
 * agree on ordinary text but not on every input, and a page boundary that differs between
 * the two cores would be a parity failure against the shared fixtures. This table is a
 * closed set defined over code points, so both cores return the same number by
 * construction.
 */
export function escapedJsonCost(text: string): number {
  let total = 0;
  for (const char of text) {
    const point = char.codePointAt(0) as number;
    if (SHORT_ESCAPES.has(point)) total += 2;
    else if (point < 0x20) total += 6;
    else if (point < 0x80) total += 1;
    else if (point < 0x800) total += 2;
    else if (point < 0x10000) total += 3;
    else total += 4;
  }
  return total;
}

/**
 * Serialized cost of the segment object around its text, plus its list comma. Measured
 * rather than hardcoded so that adding a field to a segment cannot silently under-budget
 * the page. The structure is pure ASCII, so the platform serializers agree on it; only the
 * caller-derived `text` needs {@link escapedJsonCost}.
 */
export function segmentWireOverhead(kind: string, start: number, end: number): number {
  return Buffer.byteLength(JSON.stringify({ kind, start, end, text: "" }), "utf8") + 1;
}

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
  /**
   * Which budget ended the page. Only meaningful alongside `stalled`, where it is the
   * difference between "this unit is larger than any page" and "this unit is larger than
   * what is left of the disclosure allowance" - two refusals with different remedies.
   */
  stallReason: "content" | "wire";
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
    stallReason: "content",
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
    opts: {
      maxResultBytes: number;
      maxScanLines: number;
      maxWireBytes: number;
      state?: CursorState;
    },
  ): Extraction {
    const budget = Math.min(
      opts.maxResultBytes,
      this.limits.inspectMaxResultBytes,
      this.limits.maxExtractionBytes,
    );
    if (budget <= 0) throw new ShuntError("INVALID_REQUEST", "ZERO_RESULT_BUDGET", false);
    if (opts.maxWireBytes <= 0) {
      throw new ShuntError("LIMIT_EXCEEDED", "NO_ENVELOPE_HEADROOM", false);
    }
    const wire = opts.maxWireBytes;
    const scanBudget = Math.min(opts.maxScanLines, this.limits.inspectMaxScanLines);
    const state = opts.state ?? {};
    switch (selector["kind"]) {
      case "lines":
        return this.lines(index, selector, budget, wire, scanBudget, state);
      case "bytes":
        return this.bytes(data, selector, budget, wire, state);
      case "search":
        return this.search(index, selector, budget, wire, scanBudget, state);
      default:
        throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
    }
  }

  // -- lines ---------------------------------------------------------------

  private lines(
    index: LineIndex,
    selector: Record<string, unknown>,
    budget: number,
    wireBudget: number,
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
    // The whole page is one segment, so its structural cost is paid once. It is measured
    // against the widest end ordinal the page could reach, never a narrower one that would
    // let the last line overshoot.
    let wireUsed = segmentWireOverhead("lines", start, end);
    let stoppedOnWire = false;
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
      const wireSize = escapedJsonCost(chunk);
      if (wireUsed + wireSize > wireBudget) {
        stoppedOnWire = true;
        break;
      }
      emitted.push(line);
      used += size;
      wireUsed += wireSize;
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
      if (out.stalled && stoppedOnWire) out.stallReason = "wire";
    }
    return out;
  }

  // -- bytes ---------------------------------------------------------------

  private bytes(
    data: Uint8Array,
    selector: Record<string, unknown>,
    budget: number,
    wireBudget: number,
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
    let finish = backToBoundary(data, begin, begin + take);
    if (finish <= begin) {
      out.complete = end <= begin;
      if (out.complete) return out;
      // Nothing fits without splitting a character; advancing is the only honest move.
      const advanced = Math.min(end, begin + 1);
      out.nextCursorState = { offset: advanced };
      out.stalled = advanced <= start;
      return out;
    }
    let text = new TextDecoder("utf-8", { fatal: true }).decode(data.subarray(begin, finish));
    // A byte range may be cut at any character boundary, so the wire budget shortens the
    // page rather than refusing it: walk the decoded window and stop at the last character
    // whose escaped cost still fits.
    let wireUsed = segmentWireOverhead("bytes", begin, finish);
    let keptBytes = 0;
    let kept = "";
    for (const char of text) {
      const cost = escapedJsonCost(char);
      if (wireUsed + cost > wireBudget) break;
      wireUsed += cost;
      keptBytes += utf8Length(char);
      kept += char;
    }
    if (kept.length < text.length) {
      finish = begin + keptBytes;
      text = kept;
    }
    if (finish <= begin) {
      // Not even one character fits the envelope headroom. Advancing would emit a cursor
      // the caller could not make progress with, so this is refused instead.
      out.complete = false;
      out.nextCursorState = { offset: begin };
      out.stalled = true;
      out.stallReason = "wire";
      return out;
    }
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
    wireBudget: number,
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
    let wireUsed = 0;
    const startLine = ordinal;
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
        const wireSize = segmentWireOverhead("lines", low, high) + escapedJsonCost(text);
        const overWire = wireUsed + wireSize > wireBudget;
        if (
          used + size > budget ||
          overWire ||
          out.segments.length >= this.limits.inspectMaxSegments
        ) {
          out.complete = false;
          out.nextCursorState = { line: ordinal, matches: already + (out.matchesFound ?? 0) };
          out.resultBytes = used;
          // A first match too large for the page leaves the cursor where it was. Reporting
          // which budget bound it keeps "this match cannot ever fit" distinct from "the
          // allowance ran out".
          out.stalled = out.segments.length === 0 && ordinal === startLine;
          if (out.stalled && overWire) out.stallReason = "wire";
          return out;
        }
        out.segments.push({ kind: "lines", start: low, end: high, text });
        used += size;
        wireUsed += wireSize;
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

/**
 * Pull back to the last UTF-8 character end at or before `offset`.
 *
 * The snapshot is validated UTF-8 before it is ever stored (`assertText`), so every
 * character *ending* at or before `offset` is complete. That makes the test cheap: a cut
 * is already on a boundary unless the byte it lands on is a continuation byte, in which
 * case `offset` is inside a character and only that character is dropped.
 *
 * The previous version walked back over the continuation bytes and then dropped the lead
 * byte as well, which discarded a character that fitted entirely: `日本` cut at 6 came
 * back as `日`, and cut at 3 came back empty.
 */
function backToBoundary(data: Uint8Array, begin: number, offset: number): number {
  let position = Math.min(offset, data.length);
  // `position === data.length` is the end of the payload, which is always a boundary.
  while (
    position > begin &&
    position < data.length &&
    ((data[position] as number) & 0xc0) === 0x80
  ) {
    position -= 1;
  }
  return position;
}
