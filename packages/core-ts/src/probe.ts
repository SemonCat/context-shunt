/**
 * Bounded filesystem probe used by the gate.
 *
 * Scanning stops at 351 lines or the byte cap, whichever comes first, so probing a
 * multi-gigabyte file costs the same as probing a small one. A probe that stopped early is
 * `exact: false`, which the gate treats as unknown scale and blocks.
 */
import { closeSync, lstatSync, openSync, readSync } from "node:fs";

import { ProbeResult } from "./gate.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { countLinesBounded } from "./textindex.js";

const CHUNK = 64 * 1024;

function* readBlocks(path: string): Generator<Uint8Array> {
  const fd = openSync(path, "r");
  try {
    for (;;) {
      const buf = Buffer.allocUnsafe(CHUNK);
      const read = readSync(fd, buf, 0, CHUNK, null);
      if (read === 0) return;
      yield buf.subarray(0, read);
    }
  } finally {
    closeSync(fd);
  }
}

export function fileProber(limits: Limits = DEFAULT_LIMITS) {
  return (path: string): ProbeResult => {
    let st;
    try {
      st = lstatSync(path);
    } catch {
      return { exists: false };
    }
    if (st.isSymbolicLink()) {
      // A symlink is not a regular file for gate purposes; the path policy decides
      // whether the target may become a source at all.
      return { exists: true, kind: "other" };
    }
    if (st.isDirectory()) return { exists: true, kind: "directory" };
    if (st.isFIFO()) return { exists: true, kind: "fifo" };
    if (st.isSocket()) return { exists: true, kind: "socket" };
    if (st.isBlockDevice() || st.isCharacterDevice()) return { exists: true, kind: "device" };
    if (!st.isFile()) return { exists: true, kind: "other" };

    let counted;
    try {
      counted = countLinesBounded(readBlocks(path), {
        maxLines: limits.probeMaxLinesScanned,
        maxBytes: limits.maxSourceBytes,
      });
    } catch {
      return { exists: true, kind: "other" };
    }
    return {
      exists: true,
      kind: "file",
      lines: counted.lines,
      bytes: counted.exact ? st.size : counted.bytesSeen,
      exact: counted.exact,
    };
  };
}
