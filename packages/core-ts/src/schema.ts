/**
 * Contract validation.
 *
 * The JSON Schemas are the cross-language boundary; this module is the TypeScript side of
 * it. Schema length checks count UTF-16 code units, so a byte guard is applied on top for
 * the fields the contract states in bytes.
 *
 * Version handling is deliberately strict in both directions:
 *
 * - a request declaring a revision this core does not support is `UNSUPPORTED_VERSION`;
 * - a request declaring 1.0 while carrying a 1.1 field, or using a 1.1 operation, is
 *   `INVALID_REQUEST` - an unknown mandatory field is refused, never ignored;
 * - `propose_patch` stays reserved and refused.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import Ajv2020, { type ValidateFunction } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

import { ShuntError } from "./errors.js";
import {
  DEFAULT_LIMITS,
  V11_ONLY_OPERATIONS,
  V11_ONLY_REQUEST_FIELDS,
  contractsDir,
  supportedRequestVersion,
} from "./limits.js";
import { utf8Length } from "./textindex.js";

const ajv = addFormats(new Ajv2020({ allErrors: false, strict: false }));
const cache = new Map<string, ValidateFunction>();

export const READ_OPERATIONS: ReadonlySet<string> = new Set(["read"]);
export const INSPECT_OPERATIONS: ReadonlySet<string> = new Set(["inspect"]);
export const STATS_OPERATIONS: ReadonlySet<string> = new Set(["stats"]);
export const ALL_OPERATIONS: ReadonlySet<string> = new Set(["read", "inspect", "stats"]);

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

export function toolArgsValidator(): ValidateFunction {
  return validator("tool-args.schema.json");
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
  refined?: boolean;
}

export interface InspectRequest {
  schema_version: string;
  request_id: string;
  operation: "inspect";
  source_id: string;
  snapshot_id: string;
  selector: Record<string, unknown>;
  budgets: { max_result_bytes: number; max_scan_lines: number };
  cursor?: string;
}

export interface StatsRequest {
  schema_version: string;
  request_id: string;
  operation: "stats";
  page?: number;
  page_size?: number;
}

export type CoreRequest = ReaderRequest | InspectRequest | StatsRequest;

/**
 * Validate one core request. `operations` is the set this call site accepts, so the reader
 * cannot be handed a stats request and the stats path cannot be handed a question.
 */
export function validateRequest(
  request: unknown,
  operations: ReadonlySet<string> = READ_OPERATIONS,
): CoreRequest {
  if (typeof request !== "object" || request === null) {
    throw new ShuntError("INVALID_REQUEST", "NOT_OBJECT", false);
  }
  const req = request as Record<string, unknown>;
  const version = req["schema_version"];
  if (!supportedRequestVersion(version)) {
    throw new ShuntError("UNSUPPORTED_VERSION", "BAD_SCHEMA_VERSION", false);
  }
  const operation = req["operation"];
  if (operation === "propose_patch") {
    // Reserved for a future writer contract. v1 refuses it as an unsupported operation
    // rather than treating it as an unknown enum value.
    throw new ShuntError("INVALID_REQUEST", "WRITER_OPERATION_UNSUPPORTED", false);
  }
  if (typeof operation !== "string" || !ALL_OPERATIONS.has(operation)) {
    throw new ShuntError("INVALID_REQUEST", "UNKNOWN_OPERATION", false);
  }
  if (!operations.has(operation)) {
    throw new ShuntError("INVALID_REQUEST", "OPERATION_NOT_ACCEPTED_HERE", false);
  }
  if (version === "1.0") {
    if (V11_ONLY_OPERATIONS.has(operation)) {
      throw new ShuntError("INVALID_REQUEST", "OPERATION_REQUIRES_1_1", false);
    }
    for (const key of Object.keys(req)) {
      if (V11_ONLY_REQUEST_FIELDS.has(key)) {
        throw new ShuntError("INVALID_REQUEST", "FIELD_REQUIRES_1_1", false);
      }
    }
  }
  if (!requestValidator()(request)) {
    throw new ShuntError("INVALID_REQUEST", "SCHEMA_VIOLATION", false);
  }
  if (operation === "read") assertQuestion(String(req["question"] ?? ""));
  return request as unknown as CoreRequest;
}

/** Validate the arguments an agent passed to one of the three escape-hatch tools. */
export function validateToolArgs(args: unknown): Record<string, unknown> {
  if (typeof args !== "object" || args === null) {
    throw new ShuntError("INVALID_REQUEST", "NOT_OBJECT", false);
  }
  if (!toolArgsValidator()(args)) {
    throw new ShuntError("INVALID_REQUEST", "TOOL_ARGS_VIOLATION", false);
  }
  const question = (args as Record<string, unknown>)["question"];
  if (typeof question === "string") assertQuestion(question);
  return args as Record<string, unknown>;
}

function assertQuestion(question: string): void {
  if (utf8Length(question) > DEFAULT_LIMITS.maxQuestionBytes) {
    throw new ShuntError("INVALID_REQUEST", "QUESTION_OVER_BYTE_CAP", false);
  }
  if (question.trim().length === 0) {
    throw new ShuntError("INVALID_REQUEST", "EMPTY_QUESTION", false);
  }
}

export function validateEnvelope(envelope: unknown): boolean {
  return envelopeValidator()(envelope) === true;
}

export function envelopeErrors(envelope: unknown): string[] {
  const validate = envelopeValidator();
  validate(envelope);
  return (validate.errors ?? []).map((error) => error.message ?? "invalid");
}
