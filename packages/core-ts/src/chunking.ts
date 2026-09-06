/**
 * Chunk planning.
 *
 * A plan is produced before any model call so the token budget is known up front; if it
 * cannot be bounded the request is refused rather than started. Chunks are cut on UTF-8
 * boundaries, and every chunk records the source, snapshot and exact line/record range it
 * came from.
 */
import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits, chunkByteBudget } from "./limits.js";
import { READER_SYSTEM_PROMPT, buildUserMessage } from "./provider.js";
import { Snapshot, canonicalJson, recordAt, recordCount, resolvePointer } from "./snapshot.js";
import { utf8Length } from "./textindex.js";

export interface Chunk {
  readonly sourceId: string;
  readonly snapshotId: string;
  readonly locator: Record<string, unknown>;
  readonly text: string;
  readonly bytesLen: number;
  readonly estTokens: number;
}

export interface Plan {
  readonly chunks: Chunk[];
  readonly totalEstTokens: number;
  readonly truncated: boolean;
  readonly omitted: Array<{ source_id: string; selector: Record<string, unknown>; reason: string }>;
}

/**
 * Tokens each call spends on the fixed instruction and the excerpt wrapper.
 *
 * The request budget covers *all* prompt input, not just chunk text, so the planner has to
 * reserve this per chunk or an eight-chunk plan silently overruns the cap.
 */
export function perCallOverheadTokens(
  limits: Limits = DEFAULT_LIMITS,
  question = "",
  locator: Record<string, unknown> = {},
): number {
  return estimateTokens(READER_SYSTEM_PROMPT, limits)
    + estimateTokens(buildUserMessage(question, "", locator), limits);
}

/** Conservative estimate. Metrics derived from it are labelled `estimated`. */
export function estimateTokens(text: string, limits: Limits = DEFAULT_LIMITS): number {
  return Math.max(1, Math.ceil(utf8Length(text) / limits.bytesPerTokenEstimate));
}

function splitUtf8(text: string, budget: number): string[] {
  const raw = new TextEncoder().encode(text);
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const parts: string[] = [];
  let cursor = 0;
  while (cursor < raw.length) {
    let end = Math.min(raw.length, cursor + budget);
    for (;;) {
      try {
        parts.push(decoder.decode(raw.subarray(cursor, end)));
        break;
      } catch {
        end -= 1;
        if (end === cursor) throw new ShuntError("LIMIT_EXCEEDED", "CHUNK_BOUNDARY_INVALID");
      }
    }
    cursor = end;
  }
  return parts;
}

function makeChunk(
  sourceId: string,
  snapshot: Snapshot,
  locator: Record<string, unknown>,
  text: string,
  limits: Limits,
): Chunk {
  return {
    sourceId,
    snapshotId: snapshot.snapshotId,
    locator,
    text,
    bytesLen: utf8Length(text),
    estTokens: estimateTokens(text, limits),
  };
}

function* lineChunks(
  snapshot: Snapshot,
  sourceId: string,
  start: number,
  end: number,
  limits: Limits,
): Generator<Chunk> {
  const budget = chunkByteBudget(limits);
  let cursor = start;
  while (cursor <= end) {
    const line = snapshot.lineIndex.lineText(cursor);
    if (utf8Length(line) > budget) {
      for (const part of splitUtf8(line, budget)) {
        yield makeChunk(
          sourceId,
          snapshot,
          { kind: "lines", start: cursor, end: cursor },
          part,
          limits,
        );
      }
      cursor += 1;
      continue;
    }
    const lo = cursor;
    let size = 0;
    let hi = lo - 1;
    while (hi < end) {
      const candidate = hi + 1;
      const lineBytes = snapshot.lineIndex.lineBytes(candidate).length + 1;
      if (size > 0 && size + lineBytes > budget) break;
      size += lineBytes;
      hi = candidate;
      if (size > budget) break;
    }
    if (hi < lo) hi = lo;
    const text = snapshot.lineIndex.rangeText(lo, hi);
    yield makeChunk(sourceId, snapshot, { kind: "lines", start: lo, end: hi }, text, limits);
    cursor = hi + 1;
  }
}

function* recordChunks(
  snapshot: Snapshot,
  sourceId: string,
  pointer: string,
  start: number,
  end: number,
  limits: Limits,
): Generator<Chunk> {
  const budget = chunkByteBudget(limits);
  const node = resolvePointer(snapshot.jsonValue, pointer);
  const total = recordCount(node);
  if (start < 1 || end < start || end > total) {
    throw new ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE");
  }
  let lo = start;
  while (lo <= end) {
    const first = canonicalJson(recordAt(node, lo));
    if (utf8Length(first) > budget) {
      for (const part of splitUtf8(first, budget)) {
        yield makeChunk(
          sourceId,
          snapshot,
          { kind: "records", pointer, start: lo, end: lo },
          part,
          limits,
        );
      }
      lo += 1;
      continue;
    }
    const parts: string[] = [];
    let size = 0;
    let hi = lo - 1;
    while (hi < end) {
      const rendered = canonicalJson(recordAt(node, hi + 1));
      if (parts.length > 0 && size + utf8Length(rendered) > budget) break;
      parts.push(rendered);
      size += utf8Length(rendered);
      hi += 1;
    }
    yield makeChunk(
      sourceId,
      snapshot,
      { kind: "records", pointer, start: lo, end: hi },
      parts.join("\n"),
      limits,
    );
    lo = hi + 1;
  }
}

export function planChunks(
  selections: Array<{ sourceId: string; snapshot: Snapshot; selector: Record<string, unknown> }>,
  opts: { maxChunks: number; limits?: Limits; question?: string },
): Plan {
  const limits = opts.limits ?? DEFAULT_LIMITS;
  const budgetChunks = Math.min(opts.maxChunks, limits.maxChunksPerRequest);
  const produced: Chunk[] = [];
  const omitted: Plan["omitted"] = [];
  let truncated = false;
  let spentTokens = 0;

  for (const { sourceId, snapshot, selector } of selections) {
    const kind = selector["kind"];
    let candidates: Iterable<Chunk>;
    if (kind === "all") {
      candidates =
        snapshot.jsonValue !== undefined
          ? recordChunks(snapshot, sourceId, "", 1, recordCount(snapshot.jsonValue), limits)
          : lineChunks(snapshot, sourceId, 1, snapshot.lineCount, limits);
    } else if (kind === "lines") {
      const start = Number(selector["start"]);
      let end = Number(selector["end"]);
      if (end > snapshot.lineCount) end = snapshot.lineCount;
      if (start < 1 || start > end) throw new ShuntError("INVALID_REQUEST", "LINE_OUT_OF_RANGE");
      candidates = lineChunks(snapshot, sourceId, start, end, limits);
    } else if (kind === "records") {
      candidates = recordChunks(
        snapshot,
        sourceId,
        String(selector["pointer"] ?? ""),
        Number(selector["start"]),
        Number(selector["end"]),
        limits,
      );
    } else {
      throw new ShuntError("INVALID_REQUEST", "UNSUPPORTED_SELECTOR");
    }

    for (const chunk of candidates) {
      if (chunk.estTokens > limits.maxChunkTokens) {
        throw new ShuntError("LIMIT_EXCEEDED", "CHUNK_OVER_TOKEN_CAP");
      }
      const callTokens = estimateTokens(READER_SYSTEM_PROMPT, limits)
        + estimateTokens(
          buildUserMessage(opts.question ?? "", chunk.text, chunk.locator),
          limits,
        );
      const projected = spentTokens + callTokens;
      if (produced.length >= budgetChunks || projected > limits.maxRequestInputTokens) {
        truncated = true;
        // One bounded omission represents the unprocessed remainder of this selection;
        // enumerating every possible chunk could itself exhaust memory and overflow the
        // envelope's 32-item omission cap.
        omitted.push({ source_id: sourceId, selector, reason: "BUDGET_EXCEEDED" });
        break;
      }
      spentTokens = projected;
      produced.push(chunk);
    }
  }

  const totalEstTokens = spentTokens;
  if (totalEstTokens > limits.maxRequestInputTokens) {
    throw new ShuntError("LIMIT_EXCEEDED", "REQUEST_OVER_TOKEN_CAP");
  }
  return { chunks: produced, totalEstTokens, truncated, omitted };
}
