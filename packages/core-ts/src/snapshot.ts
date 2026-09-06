/**
 * Immutable snapshots and their line / record indexes.
 *
 * A snapshot binds a SHA-256 to the exact bytes a citation was taken from. JSON sources
 * are indexed by RFC 6901 pointer with a stable record ordinal: element *n* of an array
 * has ordinal *n+1*, object keys are ordered by a fixed sort, and a scalar is addressed by
 * a pointer to itself with ordinal 1. Pretty-printed line numbers are never used as
 * citations for a JSON source.
 */
import { createHash } from "node:crypto";

import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { LineIndex, decodeStrict } from "./textindex.js";

export const TEXT_MEDIA_TYPE = "text/plain";
export const JSON_MEDIA_TYPE = "application/json";

const BINARY_MAGICS: readonly number[][] = [
  [0x7f, 0x45, 0x4c, 0x46],
  [0x89, 0x50, 0x4e, 0x47],
  [0x47, 0x49, 0x46, 0x38],
  [0xff, 0xd8, 0xff],
  [0x25, 0x50, 0x44, 0x46],
  [0x50, 0x4b, 0x03, 0x04],
  [0x1f, 0x8b],
  [0x42, 0x5a, 0x68],
  [0xfd, 0x37, 0x7a, 0x58, 0x5a, 0x00],
  [0x4f, 0x67, 0x67, 0x53],
  [0x52, 0x49, 0x46, 0x46],
  [0xca, 0xfe, 0xba, 0xbe],
  [0x4d, 0x5a],
];
const TEXT_CONTROL_ALLOWLIST = new Set([0x09, 0x0a, 0x0d, 0x0c, 0x1b]);
const CONTROL_DENSITY_LIMIT = 0.3;

const SECRET_MARKERS = [
  "-----BEGIN RSA PRIVATE KEY-----",
  "-----BEGIN OPENSSH PRIVATE KEY-----",
  "-----BEGIN DSA PRIVATE KEY-----",
  "-----BEGIN EC PRIVATE KEY-----",
  "-----BEGIN PGP PRIVATE KEY BLOCK-----",
  "-----BEGIN PRIVATE KEY-----",
  "aws_secret_access_key",
  "AKIA",
  "ghp_",
  "github_pat_",
  "xoxb-",
  "xoxp-",
  "sk-ant-",
];

export function containsSecretMarker(text: string): boolean {
  const lowered = text.toLowerCase();
  return SECRET_MARKERS.some((marker) => lowered.includes(marker.toLowerCase()));
}

export function assertNoSecret(text: string, stage: string): void {
  if (containsSecretMarker(text)) throw new ShuntError("UNSAFE_SOURCE", `SECRET_IN_${stage}`);
}

/** Extension is not evidence: content is sniffed, then strictly decoded. */
export function looksBinary(sample: Uint8Array): boolean {
  if (sample.length === 0) return false;
  for (const magic of BINARY_MAGICS) {
    if (magic.every((byte, i) => sample[i] === byte)) return true;
  }
  let control = 0;
  for (const byte of sample) {
    if (byte === 0) return true;
    if (byte < 0x20 && !TEXT_CONTROL_ALLOWLIST.has(byte)) control += 1;
  }
  return control / sample.length > CONTROL_DENSITY_LIMIT;
}

export function assertText(data: Uint8Array): string {
  if (looksBinary(data.subarray(0, 8192))) {
    throw new ShuntError("BINARY_UNSUPPORTED", "BINARY_CONTENT");
  }
  try {
    return decodeStrict(data);
  } catch {
    throw new ShuntError("BINARY_UNSUPPORTED", "INVALID_ENCODING");
  }
}

export const SAFE_BLOCK_TYPES = new Set(["text"]);

/** A mixed result containing one unsupported block rejects the whole result. */
export function assertSupportedBlocks(blocks: readonly unknown[]): void {
  for (const block of blocks) {
    if (typeof block !== "object" || block === null) {
      throw new ShuntError("BINARY_UNSUPPORTED", "UNKNOWN_BLOCK");
    }
    const type = String((block as Record<string, unknown>)["type"] ?? "");
    if (!SAFE_BLOCK_TYPES.has(type)) {
      throw new ShuntError("BINARY_UNSUPPORTED", "UNSUPPORTED_BLOCK");
    }
    const record = block as Record<string, unknown>;
    if (typeof record["text"] !== "string") {
      throw new ShuntError("BINARY_UNSUPPORTED", "UNKNOWN_BLOCK");
    }
    if (["data", "blob", "image_url", "audio_url", "resource"].some((key) => key in record)) {
      throw new ShuntError("BINARY_UNSUPPORTED", "UNSUPPORTED_BLOCK");
    }
  }
}

export interface Snapshot {
  readonly snapshotId: string;
  readonly mediaType: string;
  readonly data: Uint8Array;
  readonly lineIndex: LineIndex;
  readonly jsonValue: unknown;
  readonly bytesLen: number;
  readonly lineCount: number;
}

export function digest(data: Uint8Array): string {
  return "sha256:" + createHash("sha256").update(data).digest("hex");
}

export function snapshotBytes(
  data: Uint8Array,
  mediaTypeHint = TEXT_MEDIA_TYPE,
  limits: Limits = DEFAULT_LIMITS,
): Snapshot {
  if (data.length > limits.maxSourceBytes) {
    throw new ShuntError("LIMIT_EXCEEDED", "SOURCE_OVER_BYTE_CAP", false);
  }
  const text = assertText(data);
  assertNoSecret(text, "SOURCE");
  let jsonValue: unknown;
  if (mediaTypeHint === JSON_MEDIA_TYPE) {
    try {
      jsonValue = JSON.parse(text);
    } catch {
      throw new ShuntError("UNSAFE_SOURCE", "INVALID_JSON");
    }
    jsonDepthAndNodes(jsonValue, limits);
  }
  const lineIndex = new LineIndex(data);
  return {
    snapshotId: digest(data),
    mediaType: mediaTypeHint,
    data,
    lineIndex,
    jsonValue,
    bytesLen: data.length,
    lineCount: lineIndex.lineCount,
  };
}

// -- JSON record addressing --------------------------------------------------

export function unescapePointerToken(token: string): string {
  return token.replace(/~1/g, "/").replace(/~0/g, "~");
}

export function resolvePointer(value: unknown, pointer: string): unknown {
  if (pointer === "") return value;
  if (!pointer.startsWith("/")) throw new ShuntError("INVALID_REQUEST", "BAD_POINTER");
  let current = value;
  for (const raw of pointer.split("/").slice(1)) {
    const token = unescapePointerToken(raw);
    if (Array.isArray(current)) {
      if (!/^\d+$/.test(token)) throw new ShuntError("INVALID_REQUEST", "POINTER_NOT_FOUND");
      const idx = Number(token);
      if (idx >= current.length) throw new ShuntError("INVALID_REQUEST", "POINTER_NOT_FOUND");
      current = current[idx];
    } else if (typeof current === "object" && current !== null) {
      const obj = current as Record<string, unknown>;
      if (!Object.prototype.hasOwnProperty.call(obj, token)) {
        throw new ShuntError("INVALID_REQUEST", "POINTER_NOT_FOUND");
      }
      current = obj[token];
    } else {
      throw new ShuntError("INVALID_REQUEST", "POINTER_NOT_FOUND");
    }
  }
  return current;
}

/** Deterministic serialization: sorted keys, no incidental whitespace. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const obj = value as Record<string, unknown>;
  const parts = Object.keys(obj)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(obj[key])}`);
  return `{${parts.join(",")}}`;
}

export function recordCount(node: unknown): number {
  if (Array.isArray(node)) return node.length;
  if (typeof node === "object" && node !== null) return Object.keys(node).length;
  return 1;
}

/** 1-based: array element n has ordinal n+1; object keys sort stably. */
export function recordAt(node: unknown, ordinal: number): unknown {
  if (ordinal < 1) throw new ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE");
  if (Array.isArray(node)) {
    if (ordinal > node.length) throw new ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE");
    return node[ordinal - 1];
  }
  if (typeof node === "object" && node !== null) {
    const obj = node as Record<string, unknown>;
    const keys = Object.keys(obj).sort();
    if (ordinal > keys.length) throw new ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE");
    const key = keys[ordinal - 1] as string;
    return { [key]: obj[key] };
  }
  if (ordinal !== 1) throw new ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE");
  return node;
}

/** Bounded structural walk. Cycles and oversized structures raise LIMIT_EXCEEDED. */
export function jsonDepthAndNodes(
  value: unknown,
  limits: Limits = DEFAULT_LIMITS,
): { depth: number; nodes: number } {
  let maxDepth = 0;
  let nodes = 0;
  const seen = new Set<object>();
  const stack: Array<{ node: unknown; depth: number }> = [{ node: value, depth: 1 }];
  while (stack.length > 0) {
    const { node, depth } = stack.pop() as { node: unknown; depth: number };
    nodes += 1;
    maxDepth = Math.max(maxDepth, depth);
    if (depth > limits.jsonMaxDepth) throw new ShuntError("LIMIT_EXCEEDED", "JSON_TOO_DEEP");
    if (nodes > limits.jsonMaxNodes) throw new ShuntError("LIMIT_EXCEEDED", "JSON_TOO_MANY_NODES");
    if (typeof node === "object" && node !== null) {
      if (seen.has(node)) throw new ShuntError("LIMIT_EXCEEDED", "JSON_CYCLE");
      seen.add(node);
      let children: unknown[];
      if (Array.isArray(node)) {
        children = node;
      } else {
        const prototype = Object.getPrototypeOf(node);
        if (prototype !== Object.prototype && prototype !== null) {
          throw new ShuntError("LIMIT_EXCEEDED", "JSON_UNSUPPORTED_VALUE");
        }
        const descriptors = Object.getOwnPropertyDescriptors(node);
        if (Reflect.ownKeys(node).some((key) => typeof key !== "string")) {
          throw new ShuntError("LIMIT_EXCEEDED", "JSON_UNSUPPORTED_VALUE");
        }
        children = [];
        for (const descriptor of Object.values(descriptors)) {
          if (!("value" in descriptor) || descriptor.enumerable !== true) {
            throw new ShuntError("LIMIT_EXCEEDED", "JSON_UNSUPPORTED_VALUE");
          }
          children.push(descriptor.value);
        }
      }
      for (const child of children) stack.push({ node: child, depth: depth + 1 });
    } else if (typeof node === "number" && !Number.isFinite(node)) {
      throw new ShuntError("LIMIT_EXCEEDED", "JSON_UNSUPPORTED_VALUE");
    } else if (!["string", "number", "boolean", "undefined"].includes(typeof node) && node !== null) {
      throw new ShuntError("LIMIT_EXCEEDED", "JSON_UNSUPPORTED_VALUE");
    } else if (typeof node === "undefined") {
      throw new ShuntError("LIMIT_EXCEEDED", "JSON_UNSUPPORTED_VALUE");
    }
  }
  return { depth: maxDepth, nodes };
}
