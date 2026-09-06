/**
 * The final output boundary.
 *
 * Nothing reaches the host without passing through here. The guard measures the
 * *serialized* envelope - every field, every citation, every metadata value - against the
 * 16 KiB cap, re-checks the per-field caps, refuses unknown fields, and refuses any
 * citation not marked verified. A guard failure yields a fixed small error envelope,
 * never the input it was handed.
 */
import { SCHEMA_VERSION, DEFAULT_LIMITS, Limits, legalPair } from "./limits.js";
import { Envelope, serializedBytes } from "./envelope.js";
import { containsSecretMarker } from "./snapshot.js";
import { utf8Length } from "./textindex.js";

const ALLOWED_KEYS = new Set([
  "schema_version", "request_id", "status", "code", "answer", "citations", "coverage",
  "sources", "retryable", "guidance", "pointer",
]);
const ALLOWED_SOURCE_KEYS = new Set([
  "source_id", "snapshot_id", "media_type", "bytes", "expires_at",
]);

export class OutputGuardError extends Error {}

/** The smallest legal envelope. Used when nothing else can be trusted. */
export function fixedError(requestId: string, code = "LIMIT_EXCEEDED"): Envelope {
  const safe = typeof requestId === "string" && requestId.trim().length > 0 ? requestId.slice(0, 64) : "req_unknown";
  return {
    schema_version: SCHEMA_VERSION,
    request_id: safe,
    status: "error",
    code,
    answer: "",
    citations: [],
    coverage: {
      complete: false,
      processed_chunks: 0,
      planned_chunks: 0,
      omitted: [],
      upstream_truncated: null,
    },
    sources: [],
    retryable: false,
  };
}

export function enforce(envelope: unknown, limits: Limits = DEFAULT_LIMITS): Envelope {
  if (typeof envelope !== "object" || envelope === null) throw new OutputGuardError("not an object");
  const env = envelope as Record<string, unknown>;
  for (const key of Object.keys(env)) {
    if (!ALLOWED_KEYS.has(key)) throw new OutputGuardError("unknown envelope field");
  }
  if (env["schema_version"] !== SCHEMA_VERSION) throw new OutputGuardError("bad schema version");
  const status = env["status"];
  const code = env["code"];
  if (typeof status !== "string" || typeof code !== "string" || !legalPair(status, code)) {
    throw new OutputGuardError("illegal status/code pairing");
  }

  const answer = env["answer"];
  if (typeof answer !== "string") throw new OutputGuardError("answer must be a string");
  if (utf8Length(answer) > limits.maxAnswerBytes) throw new OutputGuardError("answer over cap");
  if (containsSecretMarker(answer)) throw new OutputGuardError("secret marker in answer");

  const citations = env["citations"];
  if (!Array.isArray(citations) || citations.length > limits.maxCitations) {
    throw new OutputGuardError("citations over cap");
  }
  for (const citation of citations) {
    if (typeof citation !== "object" || citation === null) throw new OutputGuardError("bad citation");
    const c = citation as Record<string, unknown>;
    if (c["verified"] !== true) throw new OutputGuardError("unverified citation");
    const quote = c["quote"];
    if (typeof quote !== "string" || utf8Length(quote) > limits.maxQuoteBytes) {
      throw new OutputGuardError("quote over cap");
    }
    if (containsSecretMarker(quote)) throw new OutputGuardError("secret marker in quote");
  }

  const coverage = env["coverage"];
  if (typeof coverage !== "object" || coverage === null) throw new OutputGuardError("coverage missing");
  if (status !== "ok" && (coverage as Record<string, unknown>)["complete"] === true) {
    throw new OutputGuardError("non-ok result claims complete coverage");
  }

  const sources = env["sources"];
  if (!Array.isArray(sources)) throw new OutputGuardError("sources missing");
  for (const source of sources) {
    if (typeof source !== "object" || source === null) throw new OutputGuardError("bad source handle");
    for (const key of Object.keys(source as Record<string, unknown>)) {
      if (!ALLOWED_SOURCE_KEYS.has(key)) {
        throw new OutputGuardError("source handle carries an unexpected field");
      }
    }
    const bytes = (source as Record<string, unknown>)["bytes"];
    if (typeof bytes === "number" && bytes > limits.maxSourceBytes) {
      throw new OutputGuardError("source bytes over cap");
    }
  }

  if (code === "SPILLED" && (answer.length > 0 || citations.length > 0)) {
    throw new OutputGuardError("SPILLED must not carry an answer");
  }
  if (serializedBytes(env) > limits.maxEnvelopeBytes) {
    throw new OutputGuardError("envelope over byte cap");
  }
  return env as unknown as Envelope;
}

/** Never throws. A guard failure yields the fixed error envelope, never the input. */
export function enforceOrFixed(envelope: unknown, limits: Limits = DEFAULT_LIMITS): Envelope {
  let requestId = "req_unknown";
  try {
    if (typeof envelope === "object" && envelope !== null) {
      const candidate = (envelope as Record<string, unknown>)["request_id"];
      if (typeof candidate === "string") requestId = candidate;
    }
    return enforce(envelope, limits);
  } catch {
    return fixedError(requestId);
  }
}
