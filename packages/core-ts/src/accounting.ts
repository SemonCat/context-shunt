/**
 * Token accounting: signed, one-time-credited, and never zero-for-unknown.
 *
 * Definitions, verbatim from the contract:
 *
 *     mainContextTokensSaved = baselineCreditTokens - mainModelEnvelopeTokens
 *     netTokensSaved         = mainContextTokensSaved - readerInputTokens - readerOutputTokens
 *
 * Both results are **signed**. A refined question, a failed retry and an inspect page all
 * produce a negative net, and that is the correct answer: they cost context without
 * withholding anything new.
 *
 * Three rules keep the numbers honest.
 *
 * **Zero is never unknown.** A token count the provider did not report is stored as SQL
 * NULL and rendered as `null`. Where a number is still required for the arithmetic, the
 * estimate is derived from bytes we measured ourselves and the record says `bytes_div_4`
 * rather than pretending the count was exact. `unknown` appears only when no attempt was
 * made at all.
 *
 * **The baseline is credited once.** `baselineCreditTokens` is non-zero only on the
 * operation that first withheld a given snapshot, so repeated recovery shows up as
 * accumulating cost rather than repeated savings.
 *
 * **A counterfactual is labelled as one.** `full_payload_counterfactual` is what the whole
 * payload *would* have cost; `host_truncated_observed` is the different, smaller baseline
 * that applies when the host had already truncated before we saw it. Crediting the full
 * payload there would be a fabrication.
 *
 * Egress is measured after the envelope is complete. The envelope carries only the opaque
 * `accounting_id`; the record it points at is written afterwards from the exact serialized
 * bytes, so the measurement can never include itself.
 */
import { randomBytes } from "node:crypto";

import { DEFAULT_LIMITS, Limits } from "./limits.js";
import type { TokenMethod } from "./provenance.js";
import type { OperationRecord } from "./store.js";

export type OperationKind =
  | "gate_block"
  | "capture"
  | "read"
  | "refined_read"
  | "inspect"
  | "stats"
  | "spill";

export type BaselineKind =
  /** We measured the entire payload; the saving is what it would have cost. */
  | "full_payload_counterfactual"
  /** The host truncated before we saw it; only the truncated size is observable. */
  | "host_truncated_observed"
  /** Nothing was withheld by this operation. */
  | "none";

export type DeliveryBoundary = "envelope" | "extraction" | "pointer" | "block_message" | "none";

export function newOperationId(): string {
  return "acc_" + randomBytes(8).toString("hex");
}

/** The single deterministic estimator, named `bytes_div_4` in every record. */
export function estimateTokens(byteCount: number, limits: Limits = DEFAULT_LIMITS): number {
  const divisor = Math.max(1, limits.bytesPerTokenEstimate);
  return Math.ceil(Math.max(0, byteCount) / divisor);
}

export interface Baseline {
  readonly kind: BaselineKind;
  readonly rawInputBytes: number;
  readonly tokens: number | undefined;
  readonly method: TokenMethod;
}

export function withheldPayloadBaseline(
  rawInputBytes: number,
  limits: Limits = DEFAULT_LIMITS,
): Baseline {
  return {
    kind: "full_payload_counterfactual",
    rawInputBytes,
    tokens: estimateTokens(rawInputBytes, limits),
    method: "bytes_div_4",
  };
}

/** A smaller, separately labelled baseline for an already-truncated upstream. */
export function hostTruncatedBaseline(
  observedBytes: number,
  limits: Limits = DEFAULT_LIMITS,
): Baseline {
  return {
    kind: "host_truncated_observed",
    rawInputBytes: observedBytes,
    tokens: estimateTokens(observedBytes, limits),
    method: "bytes_div_4",
  };
}

export function noBaseline(): Baseline {
  return { kind: "none", rawInputBytes: 0, tokens: undefined, method: "unknown" };
}

export interface ReaderCost {
  readonly inputTokens: number | undefined;
  readonly outputTokens: number | undefined;
  readonly cacheTokens: number | undefined;
  readonly method: TokenMethod;
  readonly attemptsStarted: number;
  readonly attemptsUsageComplete: number;
}

export function noReaderCost(): ReaderCost {
  return {
    inputTokens: undefined,
    outputTokens: undefined,
    cacheTokens: undefined,
    method: "not_applicable",
    attemptsStarted: 0,
    attemptsUsageComplete: 0,
  };
}

/**
 * Deterministic fallback when usage was not reported. The byte counts come from the
 * messages this core built and the text it received, so the estimate is reproducible - but
 * the record still says `bytes_div_4` so nobody mistakes it for provider truth.
 */
export function estimatedReaderCost(opts: {
  promptBytes: number;
  completionBytes: number;
  attemptsStarted: number;
  limits?: Limits;
}): ReaderCost {
  const limits = opts.limits ?? DEFAULT_LIMITS;
  return {
    inputTokens: estimateTokens(opts.promptBytes, limits),
    outputTokens: estimateTokens(opts.completionBytes, limits),
    cacheTokens: undefined,
    method: "bytes_div_4",
    attemptsStarted: opts.attemptsStarted,
    attemptsUsageComplete: 0,
  };
}

export interface Egress {
  readonly boundary: DeliveryBoundary;
  readonly byteCount: number;
  readonly method: TokenMethod;
}

export function envelopeEgress(boundary: DeliveryBoundary, byteCount: number): Egress {
  return { boundary, byteCount, method: "bytes_div_4" };
}

/** The DDL admits exact/bytes_div_4/unknown for a baseline; not_applicable is not one. */
function baselineMethod(baseline: Baseline): TokenMethod {
  return baseline.method === "not_applicable" ? "unknown" : baseline.method;
}

export interface ComposeInput {
  operationId: string;
  kind: OperationKind;
  status: string;
  code: string;
  baseline: Baseline;
  /**
   * The store's one-time answer for this snapshot. When false the credit is zero even
   * though `raw_input_baseline_tokens` still reports what the payload measures, so the
   * record shows both "this much was withheld overall" and "this operation claims none of
   * that saving".
   */
  baselineCredited: boolean;
  /**
   * How many withheld bytes this operation may claim, when that is narrower than the
   * measured baseline. The measurement covers every selected source; the credit may cover
   * only some of them, because a read that reuses an already-credited source withholds
   * nothing new for it. Crediting the whole measured baseline whenever any part was new
   * inflated the saving on every mixed-source read. Omitted means "all of it", which is
   * the single-source case and the previous behaviour.
   */
  creditedBytes?: number | undefined;
  reader: ReaderCost;
  egress: Egress;
  limits?: Limits;
}

export function composeRecord(input: ComposeInput): OperationRecord {
  const limits = input.limits ?? DEFAULT_LIMITS;
  const envelopeTokens = estimateTokens(input.egress.byteCount, limits);
  const credit =
    !input.baselineCredited || input.baseline.tokens === undefined
      ? 0
      : input.creditedBytes === undefined
        ? input.baseline.tokens
        : estimateTokens(input.creditedBytes, input.limits);
  const mainSaved = credit - envelopeTokens;
  const netSaved = mainSaved - (input.reader.inputTokens ?? 0) - (input.reader.outputTokens ?? 0);
  return {
    operationId: input.operationId,
    kind: input.kind,
    status: input.status,
    code: input.code,
    rawInputBytes: input.baseline.rawInputBytes,
    rawInputBaselineTokens: input.baseline.tokens,
    baselineKind: input.baseline.kind,
    baselineMethod: baselineMethod(input.baseline),
    baselineCreditTokens: credit,
    mainModelEnvelopeBytes: input.egress.byteCount,
    mainModelEnvelopeTokens: envelopeTokens,
    envelopeTokenMethod: input.egress.method,
    readerInputTokens: input.reader.inputTokens,
    readerOutputTokens: input.reader.outputTokens,
    readerCacheTokens: input.reader.cacheTokens,
    readerTokenMethod: input.reader.method,
    attemptsStarted: input.reader.attemptsStarted,
    attemptsUsageComplete: input.reader.attemptsUsageComplete,
    deliveryBoundary: input.egress.boundary,
    mainContextTokensSaved: mainSaved,
    netTokensSaved: netSaved,
  };
}

export interface StatsTotalsShape {
  operations: number;
  raw_input_bytes: number;
  baseline_credit_tokens: number;
  main_model_envelope_tokens: number;
  reader_input_tokens: number | null;
  reader_output_tokens: number | null;
  reader_cache_tokens: number | null;
  main_context_tokens_saved: number;
  net_tokens_saved: number;
  attempts_started: number;
  attempts_usage_complete: number;
  disclosed_bytes: number;
}

export interface RawTotals {
  operations: number;
  rawInputBytes: number;
  baselineCreditTokens: number;
  mainModelEnvelopeTokens: number;
  readerInputTokens: number | undefined;
  readerOutputTokens: number | undefined;
  readerCacheTokens: number | undefined;
  mainContextTokensSaved: number;
  netTokensSaved: number;
  attemptsStarted: number;
  attemptsUsageComplete: number;
  disclosedBytes: number;
}

/** Shape the store's aggregate for the stats envelope, preserving nulls. */
export function totalsToShape(raw: RawTotals): StatsTotalsShape {
  return {
    operations: raw.operations,
    raw_input_bytes: raw.rawInputBytes,
    baseline_credit_tokens: raw.baselineCreditTokens,
    main_model_envelope_tokens: raw.mainModelEnvelopeTokens,
    // A null sum means no operation reported this direction. It stays null.
    reader_input_tokens: raw.readerInputTokens ?? null,
    reader_output_tokens: raw.readerOutputTokens ?? null,
    reader_cache_tokens: raw.readerCacheTokens ?? null,
    main_context_tokens_saved: raw.mainContextTokensSaved,
    net_tokens_saved: raw.netTokensSaved,
    attempts_started: raw.attemptsStarted,
    attempts_usage_complete: raw.attemptsUsageComplete,
    disclosed_bytes: raw.disclosedBytes,
  };
}

export interface OperationRecordShape {
  operation_id: string;
  kind: string;
  status: string;
  code: string;
  raw_input_bytes: number;
  raw_input_baseline_tokens: number | null;
  baseline_kind: string;
  baseline_method: string;
  baseline_credit_tokens: number;
  main_model_envelope_bytes: number;
  main_model_envelope_tokens: number;
  envelope_token_method: string;
  reader_input_tokens: number | null;
  reader_output_tokens: number | null;
  reader_cache_tokens: number | null;
  reader_token_method: string;
  attempts_started: number;
  attempts_usage_complete: number;
  delivery_boundary: string;
  main_context_tokens_saved: number;
  net_tokens_saved: number;
}

export function recordToShape(record: OperationRecord): OperationRecordShape {
  return {
    operation_id: record.operationId,
    kind: record.kind,
    status: record.status,
    code: record.code,
    raw_input_bytes: record.rawInputBytes,
    raw_input_baseline_tokens: record.rawInputBaselineTokens ?? null,
    baseline_kind: record.baselineKind,
    baseline_method: record.baselineMethod,
    baseline_credit_tokens: record.baselineCreditTokens,
    main_model_envelope_bytes: record.mainModelEnvelopeBytes,
    main_model_envelope_tokens: record.mainModelEnvelopeTokens,
    envelope_token_method: record.envelopeTokenMethod,
    reader_input_tokens: record.readerInputTokens ?? null,
    reader_output_tokens: record.readerOutputTokens ?? null,
    reader_cache_tokens: record.readerCacheTokens ?? null,
    reader_token_method: record.readerTokenMethod,
    attempts_started: record.attemptsStarted,
    attempts_usage_complete: record.attemptsUsageComplete,
    delivery_boundary: record.deliveryBoundary,
    main_context_tokens_saved: record.mainContextTokensSaved,
    net_tokens_saved: record.netTokensSaved,
  };
}
