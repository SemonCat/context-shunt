/**
 * Contract validation.
 *
 * The JSON Schemas are the cross-language boundary; this module is the TypeScript side of
 * it. Schema length checks count UTF-16 code units, so a byte guard is applied on top for
 * the fields the contract states in bytes.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import Ajv2020, { type ValidateFunction } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, SCHEMA_VERSION, contractsDir } from "./limits.js";
import { utf8Length } from "./textindex.js";

const ajv = addFormats(new Ajv2020({ allErrors: false, strict: false }));
const cache = new Map<string, ValidateFunction>();

function validator(name: string): ValidateFunction {
  let fn = cache.get(name);
  if (!fn) {
    const schema = JSON.parse(readFileSync(join(contractsDir(), name), "utf8")) as object;
    fn = ajv.compile(schema);
    cache.set(name, fn);
  }
  return fn;
}

export function requestValidator(): ValidateFunction {
  return validator("request.schema.json");
}

export function envelopeValidator(): ValidateFunction {
  return validator("envelope.schema.json");
}

export interface ReaderRequest {
  schema_version: string;
  request_id: string;
  operation: "read";
  question: string;
  sources: Array<{
    source_id: string;
    snapshot_id: string;
    selector: Record<string, unknown>;
  }>;
  budgets: { max_chunks: number; max_answer_bytes: number; deadline_ms: number };
}

export function validateRequest(request: unknown): ReaderRequest {
  if (typeof request !== "object" || request === null) {
    throw new ShuntError("INVALID_REQUEST", "NOT_OBJECT", false);
  }
  const req = request as Record<string, unknown>;
  if (req["schema_version"] !== SCHEMA_VERSION) {
    throw new ShuntError("UNSUPPORTED_VERSION", "BAD_SCHEMA_VERSION", false);
  }
  if (req["operation"] === "propose_patch") {
    // Reserved for a future writer contract. v1 refuses it as an unsupported operation
    // rather than treating it as an unknown enum value.
    throw new ShuntError("INVALID_REQUEST", "WRITER_OPERATION_UNSUPPORTED", false);
  }
  if (!requestValidator()(request)) {
    throw new ShuntError("INVALID_REQUEST", "SCHEMA_VIOLATION", false);
  }
  const question = String(req["question"] ?? "");
  if (utf8Length(question) > DEFAULT_LIMITS.maxQuestionBytes) {
    throw new ShuntError("INVALID_REQUEST", "QUESTION_OVER_BYTE_CAP", false);
  }
  if (question.trim().length === 0) {
    throw new ShuntError("INVALID_REQUEST", "EMPTY_QUESTION", false);
  }
  return request as unknown as ReaderRequest;
}

export function validateEnvelope(envelope: unknown): boolean {
  return envelopeValidator()(envelope) === true;
}
