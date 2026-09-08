/**
 * The final output boundary.
 *
 * Nothing reaches the host without passing through here. The guard measures the
 * *serialized* envelope - every field, every citation, every metadata value - against the
 * cap that applies to its `result_kind`, re-checks the per-field caps, refuses unknown
 * fields, and refuses any citation not marked verified. A guard failure yields a fixed
 * small error envelope, never the input it was handed.
 *
 * Two caps, both normative. Most envelopes are capped at 16 KiB. A deterministic
 * extraction or a stats page carries a bounded payload of its own - up to 16 KiB of exact
 * snapshot bytes, or one page of records - so those are measured against
 * `maxExtendedEnvelopeBytes` (20 KiB), leaving 4 KiB for the envelope around a full-size
 * extraction. The extraction payload itself is measured separately against the 16 KiB
 * per-result cap, so the escape hatch cannot widen by hiding bytes in envelope overhead.
 *
 * Adding a field to the envelope means adding it here too: `ALLOWED_KEYS` is a closed set.
 */
import {
  DEFAULT_LIMITS,
  EMITTED_SCHEMA_VERSION,
  Limits,
  SUPPORTED_REQUEST_VERSIONS,
  envelopeByteCap,
  legalPair,
} from "./limits.js";
import { buildEnvelope, Envelope, serializedBytes } from "./envelope.js";
import { containsSecretMarker } from "./snapshot.js";
import { validateEnvelope } from "./schema.js";
import { utf8Length } from "./textindex.js";

const ALLOWED_KEYS = new Set([
  "schema_version", "request_id", "status", "code", "answer", "citations", "coverage",
  "sources", "retryable", "guidance", "pointer",
  // -- 1.1 --
  "result_kind", "provenance", "accounting_id", "extraction", "stats", "recovery",
  "import_receipt",
]);
const ALLOWED_SOURCE_KEYS = new Set([
  "source_id", "snapshot_id", "media_type", "bytes", "expires_at",
]);
const REQUIRED_V11_KEYS = ["result_kind", "provenance", "accounting_id"] as const;
const OPTIONAL_V11_KEYS = ["extraction", "stats", "recovery", "import_receipt"] as const;
const SAFE_ACCOUNTING_ID = /^acc_[0-9a-f]{16}$/;

export class OutputGuardError extends Error {}

const SAFE_REQUEST_ID = /^[A-Za-z0-9_.:-]{1,64}$/;

/** The smallest legal envelope. Used when nothing else can be trusted. */
export function fixedError(requestId: string, code = "LIMIT_EXCEEDED"): Envelope {
  const safe = typeof requestId === "string" && SAFE_REQUEST_ID.test(requestId)
    ? requestId
    : "req_unknown";
  const safeCode = legalPair("error", code) ? code : "LIMIT_EXCEEDED";
  return buildEnvelope({
    requestId: safe,
    status: "error",
    code: safeCode,
    retryable: false,
  });
}

/**
 * Version and content must agree in both directions. A 1.1 envelope missing a mandatory
 * 1.1 field is refused rather than published with the field quietly absent; a 1.0 envelope
 * carrying a 1.1 field is refused rather than published under a version string that
 * understates what it contains.
 */
function checkVersionFields(env: Record<string, unknown>, version: string): void {
  const presentV11 = [...REQUIRED_V11_KEYS, ...OPTIONAL_V11_KEYS].filter((key) => key in env);
  if (version === "1.0") {
    if (presentV11.length > 0) throw new OutputGuardError("1.0 envelope carries a 1.1 field");
    return;
  }
  for (const key of REQUIRED_V11_KEYS) {
    if (!(key in env)) throw new OutputGuardError("1.1 envelope missing a mandatory field");
  }
  if (!SAFE_ACCOUNTING_ID.test(String(env["accounting_id"]))) {
    throw new OutputGuardError("bad accounting id");
  }
  const provenance = env["provenance"];
  if (typeof provenance !== "object" || provenance === null) {
    throw new OutputGuardError("provenance must be an object");
  }
  const derived = (provenance as Record<string, unknown>)["derived"];
  if (typeof derived !== "boolean") {
    throw new OutputGuardError("provenance.derived must be a boolean");
  }
  if (derived !== (env["result_kind"] === "model_derived")) {
    throw new OutputGuardError("provenance.derived disagrees with result_kind");
  }
}

function checkExtraction(env: Record<string, unknown>, limits: Limits): void {
  const extraction = env["extraction"];
  if (extraction === undefined) return;
  if (typeof extraction !== "object" || extraction === null) {
    throw new OutputGuardError("extraction must be an object");
  }
  const block = extraction as Record<string, unknown>;
  if (block["deterministic"] !== true) {
    throw new OutputGuardError("extraction must declare itself deterministic");
  }
  const segments = block["segments"];
  if (!Array.isArray(segments) || segments.length > limits.inspectMaxSegments) {
    throw new OutputGuardError("extraction segments over cap");
  }
  let total = 0;
  for (const segment of segments) {
    if (typeof segment !== "object" || segment === null) {
      throw new OutputGuardError("extraction segment malformed");
    }
    const text = (segment as Record<string, unknown>)["text"];
    if (typeof text !== "string") throw new OutputGuardError("extraction segment malformed");
    if (containsSecretMarker(text)) throw new OutputGuardError("secret marker in extraction");
    total += utf8Length(text);
  }
  if (total > limits.maxExtractionBytes) {
    throw new OutputGuardError("extraction over per-result cap");
  }
  if (block["result_bytes"] !== total) {
    throw new OutputGuardError("extraction result_bytes disagrees with its segments");
  }
}

export function enforce(envelope: unknown, limits: Limits = DEFAULT_LIMITS): Envelope {
  if (typeof envelope !== "object" || envelope === null) throw new OutputGuardError("not an object");
  const env = envelope as Record<string, unknown>;
  for (const key of Object.keys(env)) {
    if (!ALLOWED_KEYS.has(key)) throw new OutputGuardError("unknown envelope field");
  }
  const version = env["schema_version"];
  if (typeof version !== "string" || !SUPPORTED_REQUEST_VERSIONS.has(version)) {
    throw new OutputGuardError("bad schema version");
  }
  const status = env["status"];
  const code = env["code"];
  if (typeof status !== "string" || typeof code !== "string" || !legalPair(status, code)) {
    throw new OutputGuardError("illegal status/code pairing");
  }

  checkVersionFields(env, version);

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
    if (!Number.isSafeInteger(bytes) || Number(bytes) < 0 || Number(bytes) > limits.maxSourceBytes) {
      throw new OutputGuardError("source bytes over cap");
    }
  }

  if (
    (code === "SPILLED" || code === "EXTRACTED" || code === "STATS" || code === "IMPORTED")
    && (answer.length > 0 || citations.length > 0)
  ) {
    throw new OutputGuardError(`${code} must not carry an answer`);
  }

  checkExtraction(env, limits);

  const cap = envelopeByteCap(
    typeof env["result_kind"] === "string" ? (env["result_kind"] as string) : undefined,
    limits,
  );
  if (serializedBytes(env) > cap) throw new OutputGuardError("envelope over byte cap");
  if (!validateEnvelope(env)) throw new OutputGuardError("envelope schema violation");
  return env as unknown as Envelope;
}

/** Never throws. A guard failure yields the fixed error envelope, never the input. */
export { EMITTED_SCHEMA_VERSION };

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
