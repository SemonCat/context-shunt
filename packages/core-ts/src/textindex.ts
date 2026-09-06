/**
 * Physical-line counting and indexing.
 *
 * The contract is: physical lines are LF-separated; a trailing LF does not add an empty
 * line; an empty file is 0 lines; CRLF is one line. `wc -l` does not implement this - it
 * undercounts a file with no trailing newline - so the counter is written to the contract
 * and pinned by `contracts/v1/conformance/line-count-cases.json`.
 */
const LF = 0x0a;

export function countLines(data: Uint8Array): number {
  if (data.length === 0) return 0;
  let lines = 0;
  for (const byte of data) if (byte === LF) lines += 1;
  return data[data.length - 1] === LF ? lines : lines + 1;
}

export interface BoundedCount {
  /** A lower bound when `exact` is false. */
  readonly lines: number;
  readonly bytesSeen: number;
  readonly exact: boolean;
}

/** Count while streaming, stopping at `maxLines` or `maxBytes`. Inexact means "unknown scale". */
export function countLinesBounded(
  blocks: Iterable<Uint8Array>,
  opts: { maxLines: number; maxBytes: number },
): BoundedCount {
  let lines = 0;
  let total = 0;
  let trailingLf = true;
  for (const block of blocks) {
    if (block.length === 0) continue;
    let take = block;
    if (total + take.length > opts.maxBytes) take = take.subarray(0, opts.maxBytes - total);
    total += take.length;
    for (const byte of take) if (byte === LF) lines += 1;
    trailingLf = take[take.length - 1] === LF;
    if (lines >= opts.maxLines || total >= opts.maxBytes) {
      return { lines: Math.min(lines, opts.maxLines), bytesSeen: total, exact: false };
    }
  }
  if (total === 0) return { lines: 0, bytesSeen: 0, exact: true };
  return { lines: lines + (trailingLf ? 0 : 1), bytesSeen: total, exact: true };
}

/** 1-based inclusive physical-line index. Newlines are never normalized. */
export class LineIndex {
  private static readonly STRIDE = 64;
  private readonly checkpoints: number[] = [];
  private readonly count: number;

  constructor(private readonly data: Uint8Array) {
    if (data.length === 0) {
      this.count = 0;
      return;
    }
    this.checkpoints.push(0);
    let lines = 0;
    for (let offset = 0; offset < data.length; offset += 1) {
      if (data[offset] !== LF) continue;
      lines += 1;
      const next = offset + 1;
      if (next < data.length && lines % LineIndex.STRIDE === 0) {
        this.checkpoints.push(next);
      }
    }
    this.count = lines + (data[data.length - 1] === LF ? 0 : 1);
  }

  get lineCount(): number {
    return this.count;
  }

  /** Line content without its terminating LF. */
  lineBytes(ordinal: number): Uint8Array {
    if (ordinal < 1 || ordinal > this.lineCount) throw new RangeError("line out of range");
    const start = this.lineStart(ordinal);
    const newline = this.data.indexOf(LF, start);
    return this.data.subarray(start, newline === -1 ? this.data.length : newline);
  }

  lineText(ordinal: number): string {
    return decodeStrict(this.lineBytes(ordinal));
  }

  rangeBytes(start: number, end: number): Uint8Array {
    if (start < 1 || end < start || end > this.lineCount) {
      throw new RangeError("line range out of range");
    }
    const rangeStart = this.lineStart(start);
    let rangeEnd = rangeStart;
    for (let ordinal = start; ordinal <= end; ordinal += 1) {
      const newline = this.data.indexOf(LF, rangeEnd);
      if (newline === -1) {
        rangeEnd = this.data.length;
        break;
      }
      rangeEnd = ordinal === end ? newline : newline + 1;
    }
    return this.data.subarray(rangeStart, rangeEnd);
  }

  rangeText(start: number, end: number): string {
    return decodeStrict(this.rangeBytes(start, end));
  }

  private lineStart(ordinal: number): number {
    const block = Math.floor((ordinal - 1) / LineIndex.STRIDE);
    const baseOrdinal = block * LineIndex.STRIDE + 1;
    let offset = this.checkpoints[block] as number;
    for (let current = baseOrdinal; current < ordinal; current += 1) {
      const newline = this.data.indexOf(LF, offset);
      if (newline === -1) throw new RangeError("line out of range");
      offset = newline + 1;
    }
    return offset;
  }
}

const strictDecoder = new TextDecoder("utf-8", { fatal: true });

export function decodeStrict(data: Uint8Array): string {
  return strictDecoder.decode(data);
}

export function utf8Bytes(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

export function utf8Length(text: string): number {
  return utf8Bytes(text).length;
}

/** Truncate to a byte budget on a UTF-8 boundary. */
export function capBytes(text: string, maxBytes: number): string {
  const raw = utf8Bytes(text);
  if (raw.length <= maxBytes) return text;
  let end = maxBytes;
  while (end > 0) {
    try {
      return strictDecoder.decode(raw.subarray(0, end));
    } catch {
      end -= 1;
    }
  }
  return "";
}
