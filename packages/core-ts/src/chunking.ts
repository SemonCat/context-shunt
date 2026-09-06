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
import { Snapshot, canonicalJson, recordAt, recordCount, resolvePointer } from "./snapshot.js";
import { capBytes, utf8Length } from "./textindex.js";

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

/** Conservative estimate. Metrics derived from it are labelled `estimated`. */
export function estimateTokens(text: string, limits: Limits = DEFAULT_LIMITS): number {
  return Math.max(1, Math.ceil(utf8Length(text) / limits.bytesPerTokenEstimate));
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

function lineChunks(
  snapshot: Snapshot,
  sourceId: string,
  start: number,
  end: number,
  limits: Limits,
): Chunk[] {
  const budget = chunkByteBudget(limits);
  const chunks: Chunk[] = [];
  let cursor = start;
  while (cursor <= end) {
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
    // One physical line longer than a chunk is cut on a UTF-8 boundary, but the citation
    // locator still points at the original line.
    const text = capBytes(snapshot.lineIndex.rangeText(lo, hi), budget);
    chunks.push(makeChunk(sourceId, snapshot, { kind: "lines", start: lo, end: hi }, text, limits));
    cursor = hi + 1;
  }
  return chunks;
}

function recordChunks(
  snapshot: Snapshot,
  sourceId: string,
  pointer: string,
  start: number,
  end: number,
  limits: Limits,
): Chunk[] {
  const budget = chunkByteBudget(limits);
  const node = resolvePointer(snapshot.jsonValue, pointer);
  const total = recordCount(node);
  if (start < 1 || end < start || end > total) {
    throw new ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE");
  }
  const chunks: Chunk[] = [];
  let lo = start;
  while (lo <= end) {
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
    chunks.push(
      makeChunk(
        sourceId,
        snapshot,
        { kind: "records", pointer, start: lo, end: hi },
        parts.join("\n"),
        limits,
      ),
    );
    lo = hi + 1;
  }
  return chunks;
}

export function planChunks(
  selections: Array<{ sourceId: string; snapshot: Snapshot; selector: Record<string, unknown> }>,
  opts: { maxChunks: number; limits?: Limits },
): Plan {
  const limits = opts.limits ?? DEFAULT_LIMITS;
  const budgetChunks = Math.min(opts.maxChunks, limits.maxChunksPerRequest);
  const produced: Chunk[] = [];
  const omitted: Plan["omitted"] = [];
  let truncated = false;

  for (const { sourceId, snapshot, selector } of selections) {
    const kind = selector["kind"];
    let candidates: Chunk[];
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
      if (produced.length >= budgetChunks) {
        truncated = true;
        omitted.push({ source_id: sourceId, selector: chunk.locator, reason: "BUDGET_EXCEEDED" });
        continue;
      }
      produced.push(chunk);
    }
  }

  const totalEstTokens = produced.reduce((sum, c) => sum + c.estTokens, 0);
  if (totalEstTokens > limits.maxRequestInputTokens) {
    throw new ShuntError("LIMIT_EXCEEDED", "REQUEST_OVER_TOKEN_CAP");
  }
  return { chunks: produced, totalEstTokens, truncated, omitted };
}
