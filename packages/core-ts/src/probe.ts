/**
 * Bounded filesystem probe used by the gate.
 *
 * Scanning stops at 351 lines or the byte cap, whichever comes first, so probing a
 * multi-gigabyte file costs the same as probing a small one. A probe that stopped early is
 * `exact: false`, which the gate treats as unknown scale and blocks.
 */
import {
  type BigIntStats,
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
} from "node:fs";

import { ProbeResult, ProbeSelection } from "./gate.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { countLinesBounded } from "./textindex.js";

const CHUNK = 16 * 1024;

function* readBlocks(fd: number): Generator<Uint8Array> {
  for (;;) {
    const buf = Buffer.allocUnsafe(CHUNK);
    const read = readSync(fd, buf, 0, CHUNK, null);
    if (read === 0) return;
    yield buf.subarray(0, read);
  }
}

function kindOf(st: BigIntStats): NonNullable<ProbeResult["kind"]> {
  if (st.isDirectory()) return "directory";
  if (st.isFIFO()) return "fifo";
  if (st.isSocket()) return "socket";
  if (st.isBlockDevice() || st.isCharacterDevice()) return "device";
  return st.isFile() ? "file" : "other";
}

function sameIdentity(left: BigIntStats, right: BigIntStats): boolean {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}

export function fileProber(limits: Limits = DEFAULT_LIMITS) {
  return (path: string, selection: ProbeSelection = { mode: "full" }): ProbeResult => {
    let lst: BigIntStats;
    try {
      lst = lstatSync(path, { bigint: true });
    } catch {
      return { exists: false };
    }
    if (lst.isSymbolicLink()) {
      // A symlink is not a regular file for gate purposes; the path policy decides
      // whether the target may become a source at all.
      return { exists: true, kind: "other" };
    }
    const initialKind = kindOf(lst);
    if (initialKind !== "file") return { exists: true, kind: initialKind };

    // Bind classification and sizing to one nonblocking, no-follow descriptor. Without
    // this, an attacker could replace the lstat'd path with a symlink or FIFO before the
    // second open, escaping the path decision or hanging the synchronous host hook.
    let fd: number;
    try {
      fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    } catch {
      return { exists: true, kind: "other" };
    }
    try {
      const before = fstatSync(fd, { bigint: true });
      if (!before.isFile() || !sameIdentity(before, lst)) {
        return { exists: true, kind: "other" };
      }
      let result: ProbeResult;
      if (selection.mode === "metadata") {
        result = { exists: true, kind: "file", lines: 0, bytes: 0, exact: true };
      } else if (selection.mode === "lines") {
        result = selectedLines(fd, selection.startLine, selection.limit, limits);
      } else if (selection.mode === "tail") {
        result = selectedTail(fd, Number(before.size), selection.limit, limits);
      } else if (selection.mode === "search") {
        result = searchUpperBound(fd, path, selection.maxMatches, limits);
      } else {
        const counted = countLinesBounded(readBlocks(fd), {
          maxLines: limits.probeMaxLinesScanned,
          // A full read is blocked at the targeted-output byte cap, so reading beyond
          // the first byte over that cap cannot change the decision.
          maxBytes: limits.maxTargetedReadBytes + 1,
        });
        result = {
          exists: true,
          kind: "file",
          lines: counted.lines,
          bytes: counted.exact ? Number(before.size) : counted.bytesSeen,
          exact: counted.exact,
        };
      }
      const after = fstatSync(fd, { bigint: true });
      return sameIdentity(before, after) ? result : { exists: true, kind: "other" };
    } catch {
      return { exists: true, kind: "other" };
    } finally {
      closeSync(fd);
    }
  };
}

function selectedLines(fd: number, startLine: number, limit: number, limits: Limits): ProbeResult {
  const endLine = startLine + limit;
  let line = 1;
  let lines = 0;
  let bytes = 0;
  let scanned = 0;
  let currentSelected = false;
  for (;;) {
    const buffer = Buffer.allocUnsafe(CHUNK);
    const count = readSync(fd, buffer, 0, buffer.length, null);
    if (count === 0) break;
    for (let index = 0; index < count; index += 1) {
      scanned += 1;
      if (scanned > limits.maxSourceBytes) {
        return { exists: true, kind: "file", lines, bytes, exact: false };
      }
      currentSelected = line >= startLine && line < endLine;
      if (currentSelected) {
        bytes += 1;
        if (bytes > limits.maxTargetedReadBytes) {
          return { exists: true, kind: "file", lines, bytes, exact: true };
        }
      }
      if (buffer[index] === 0x0a) {
        if (currentSelected) lines += 1;
        line += 1;
        if (line >= endLine) {
          return { exists: true, kind: "file", lines, bytes, exact: true };
        }
      }
    }
  }
  if (currentSelected && bytes > 0) lines += 1;
  return { exists: true, kind: "file", lines, bytes, exact: true };
}

function selectedTail(
  fd: number,
  fileBytes: number,
  limit: number,
  limits: Limits,
): ProbeResult {
  if (fileBytes === 0) return { exists: true, kind: "file", lines: 0, bytes: 0, exact: true };
  const last = Buffer.allocUnsafe(1);
  readExactAt(fd, last, fileBytes - 1);
  const neededNewlines = limit + (last[0] === 0x0a ? 1 : 0);
  let found = 0;
  let cursor = fileBytes;
  while (cursor > 0) {
    const start = Math.max(0, cursor - CHUNK);
    const length = cursor - start;
    const buffer = Buffer.allocUnsafe(length);
    readExactAt(fd, buffer, start);
    for (let index = length - 1; index >= 0; index -= 1) {
      if (buffer[index] === 0x0a) {
        found += 1;
        if (found === neededNewlines) {
          const bytes = fileBytes - (start + index + 1);
          return { exists: true, kind: "file", lines: limit, bytes, exact: true };
        }
      }
    }
    cursor = start;
    const selectedSoFar = fileBytes - cursor;
    if (selectedSoFar > limits.maxTargetedReadBytes) {
      return {
        exists: true,
        kind: "file",
        lines: limit,
        bytes: limits.maxTargetedReadBytes + 1,
        exact: true,
      };
    }
  }
  return { exists: true, kind: "file", lines: limit, bytes: fileBytes, exact: true };
}

function readExactAt(fd: number, buffer: Buffer, position: number): void {
  let offset = 0;
  while (offset < buffer.length) {
    const count = readSync(fd, buffer, offset, buffer.length - offset, position + offset);
    if (count === 0) throw new Error("short read");
    offset += count;
  }
}

function searchUpperBound(fd: number, path: string, maxMatches: number, limits: Limits): ProbeResult {
  // Conservative allowance for path, line number, separators and host result framing.
  const prefixBytes = new TextEncoder().encode(path).length + 64;
  let bytesSeen = 0;
  let lineBytes = 0;
  let lineCount = 0;
  let maxLineBytes = 0;
  let endedWithNewline = false;
  for (;;) {
    const buffer = Buffer.allocUnsafe(CHUNK);
    const count = readSync(fd, buffer, 0, buffer.length, null);
    if (count === 0) break;
    for (let index = 0; index < count; index += 1) {
      bytesSeen += 1;
      if (bytesSeen > limits.maxSourceBytes) {
        return { exists: true, kind: "file", lines: lineCount, bytes: bytesSeen, exact: false };
      }
      lineBytes += 1;
      const currentUpper = maxMatches * (lineBytes + prefixBytes);
      if (currentUpper > limits.maxTargetedReadBytes) {
        return {
          exists: true,
          kind: "file",
          lines: Math.min(maxMatches, lineCount + 1),
          bytes: currentUpper,
          exact: true,
        };
      }
      endedWithNewline = buffer[index] === 0x0a;
      if (endedWithNewline) {
        lineCount += 1;
        maxLineBytes = Math.max(maxLineBytes, lineBytes);
        lineBytes = 0;
        const upper = maxMatches * (maxLineBytes + prefixBytes);
        if (upper > limits.maxTargetedReadBytes) {
          return { exists: true, kind: "file", lines: maxMatches, bytes: upper, exact: true };
        }
      }
    }
  }
  if (lineBytes > 0 || (bytesSeen > 0 && !endedWithNewline)) {
    lineCount += 1;
    maxLineBytes = Math.max(maxLineBytes, lineBytes);
  }
  const matches = Math.min(maxMatches, lineCount);
  return {
    exists: true,
    kind: "file",
    lines: matches,
    bytes: matches * (maxLineBytes + prefixBytes),
    exact: true,
  };
}
