/**
 * Bounded, content-free failures.
 *
 * Every failure that reaches the host is a `ShuntError` with a contract `code` and a
 * short operator-safe message. Raw payloads, prompts, provider error bodies, secrets and
 * absolute paths never enter these objects.
 */
export const SAFE_MESSAGES: Record<string, string> = {
  INVALID_REQUEST: "request rejected by the v1 contract",
  UNSUPPORTED_VERSION: "unsupported contract version",
  UNSAFE_SOURCE: "source rejected by the path or content policy",
  SOURCE_CHANGED: "source changed while a consistent snapshot was being taken",
  SOURCE_EXPIRED: "source handle is expired or belongs to another session",
  BINARY_UNSUPPORTED: "binary or unsupported content is not read by v1",
  LIMIT_EXCEEDED: "a configured limit was exceeded",
  TIMEOUT: "deadline exceeded",
  MODEL_ERROR: "reader model call failed",
  INVALID_MODEL_OUTPUT: "reader model output did not match the required shape",
  CITATION_INVALID: "no assertion survived citation verification",
  SPILL_FAILED: "oversized result could not be spilled",
  STORE_FAILED: "snapshot store could not publish or authorize a handle",
  DISCLOSURE_EXHAUSTED: "cumulative disclosure ceiling reached for this source or session",
  PROVENANCE_UNAVAILABLE:
    "reader provenance could not be established under the configured policy",
  EXTRACTED: "deterministic extraction returned exact snapshot bytes",
  STATS: "session accounting returned",
  HOST_UNSAFE: "host cannot guarantee the required interception order",
  CANCELLED: "request cancelled",
  LARGE_READ: "full read exceeds the configured line or byte threshold",
  UNCLASSIFIABLE_READ: "read-like command could not be proven bounded and safe",
  UPSTREAM_TRUNCATED: "upstream result was already truncated",
};

export const RETRYABLE_CODES = new Set(["MODEL_ERROR", "TIMEOUT"]);

const SAFE_DETAIL = /^[A-Z0-9_]{1,48}$/;

export class ShuntError extends Error {
  readonly code: string;
  readonly detail: string | undefined;
  readonly retryable: boolean;

  constructor(code: string, detail?: string, retryable?: boolean) {
    if (!(code in SAFE_MESSAGES)) throw new Error(`unknown error code: ${code}`);
    if (detail !== undefined && !SAFE_DETAIL.test(detail)) {
      throw new Error("error detail must be a short bounded token, not free text");
    }
    const base = SAFE_MESSAGES[code] as string;
    super(detail ? `${base} (${detail})` : base);
    this.name = "ShuntError";
    this.code = code;
    this.detail = detail;
    this.retryable = retryable ?? RETRYABLE_CODES.has(code);
  }

  safeMessage(): string {
    return this.message;
  }
}

export function isShuntError(value: unknown): value is ShuntError {
  return value instanceof ShuntError;
}
