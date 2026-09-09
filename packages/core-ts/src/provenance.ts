/**
 * Reader provenance: what produced this envelope, stated truthfully.
 *
 * The rule this module exists to enforce is narrow and absolute: **`actual_model` is never
 * synthesized from `requested_model`**. Three separate facts are kept apart, because on
 * real hosts they are genuinely different things:
 *
 * - `requested*` - what the adapter asked the host for. Always known.
 * - `resolved*`  - what the host says it selected after its own policy and routing. Known
 *   only when the host exposes its selection; `null` otherwise, never back-filled from the
 *   request. OpenClaw's isolated completion path does expose this.
 * - `reported*`  - what the *provider* says generated the tokens. Known only when the
 *   provider reports it and the host passes it through.
 *
 * `Attribution` then names the strongest thing that can be proven, from `actual` (the
 * provider confirmed it and it agrees with the request) down through `resolved` (a routing
 * fact, not a provider confirmation), `unverified` (a value came back but the host surface
 * cannot separate a provider report from an echo of the request), `mismatch` (a concrete
 * value contradicts the request), `unknown` (nothing came back) and `not_applicable` (no
 * model call was made).
 *
 * `AttributionPolicy` decides what to do with an unprovable attribution.
 * `allow_unverified` (the default) publishes the truthful label; `require_match` refuses
 * instead. Either way the envelope records which policy was in force, so nobody has to
 * guess. A contradiction is refused under both: a mismatch is a wrong answer, not a weak
 * one.
 *
 * `Usage` distinguishes exact provider-reported counts from a named deterministic estimate
 * from unknown. `undefined` means "not reported" and must never be rendered as zero.
 */
import { ShuntError } from "./errors.js";

export type Attribution =
  | "actual"
  | "resolved"
  | "unverified"
  | "mismatch"
  | "unknown"
  | "not_applicable";

export type Confidence = "high" | "medium" | "low" | "none";

export type AttributionPolicy = "require_match" | "allow_unverified" | "not_applicable";

export type ResultKind =
  | "model_derived"
  | "deterministic_extraction"
  | "gate_decision"
  | "pointer"
  | "stats"
  | "failure";

export type ProvenanceLabel =
  | "model_generated_answer"
  | "deterministic_extraction"
  | "gate_decision"
  | "pointer_only"
  | "session_metrics"
  | "no_model_output";

export type TokenMethod = "exact" | "bytes_div_4" | "unknown" | "not_applicable";

export interface ModelIdentity {
  readonly provider?: string | undefined;
  readonly model?: string | undefined;
}

export const UNKNOWN_IDENTITY: ModelIdentity = Object.freeze({});

export function identityKnown(identity: ModelIdentity): boolean {
  return Boolean(identity.provider) || Boolean(identity.model);
}

export interface Usage {
  readonly inputTokens?: number | undefined;
  readonly outputTokens?: number | undefined;
  readonly cacheTokens?: number | undefined;
  readonly method: TokenMethod;
}

export const NO_USAGE: Usage = Object.freeze({ method: "unknown" as TokenMethod });

/** True only when the provider reported both directions exactly. */
export function usageComplete(usage: Usage): boolean {
  return (
    usage.method === "exact" && usage.inputTokens !== undefined && usage.outputTokens !== undefined
  );
}

function addOptional(left?: number, right?: number): number | undefined {
  if (left === undefined) return right;
  if (right === undefined) return left;
  return left + right;
}

function mergeMethod(left: TokenMethod, right: TokenMethod): TokenMethod {
  if (left === "not_applicable") return right;
  if (right === "not_applicable") return left;
  if (left === right) return left;
  // Mixing exact and estimated counts yields an estimate, not an exact total.
  if (left === "unknown" || right === "unknown") return "unknown";
  return "bytes_div_4";
}

/** Sum two attempts. Unknown plus anything stays unknown for that direction. */
export function mergeUsage(left: Usage, right: Usage): Usage {
  const merged: { -readonly [K in keyof Usage]: Usage[K] } = {
    method: mergeMethod(left.method, right.method),
  };
  const input = addOptional(left.inputTokens, right.inputTokens);
  const output = addOptional(left.outputTokens, right.outputTokens);
  const cache = addOptional(left.cacheTokens, right.cacheTokens);
  if (input !== undefined) merged.inputTokens = input;
  if (output !== undefined) merged.outputTokens = output;
  if (cache !== undefined) merged.cacheTokens = cache;
  return merged as Usage;
}

export interface Provenance {
  derived: boolean;
  label: ProvenanceLabel;
  attributionStatus: Attribution;
  attributionConfidence: Confidence;
  attributionPolicy: AttributionPolicy;
  attemptsStarted: number;
  usageComplete: boolean;
  citationsMechanicallyVerified: boolean;
  requested?: ModelIdentity;
  resolved?: ModelIdentity;
  reported?: ModelIdentity;
  fallbackUsed?: boolean;
  /**
   * One identity record per physical provider call behind this request, in call order.
   * Deliberately **not** carried into `ProvenanceShape`: the envelope contract is a
   * single statement about the answer, and per-call detail belongs to whoever is auditing
   * the calls rather than to every consumer of an answer. It exists because the fields
   * above describe the call that *answered* - multiplying them by `attemptsStarted` is
   * the one arithmetic that certifies calls no evidence describes. Absent when nothing
   * made a call.
   */
  callIdentities?: readonly unknown[];
}

export interface ProvenanceShape {
  derived: boolean;
  label: string;
  attribution_status: Attribution;
  attribution_confidence: Confidence;
  attribution_policy: AttributionPolicy;
  attempts_started: number;
  usage_complete: boolean;
  citations_mechanically_verified: boolean;
  requested_provider?: string;
  requested_model?: string;
  resolved_provider?: string | null;
  resolved_model?: string | null;
  reported_provider?: string | null;
  reported_model?: string | null;
  fallback_used?: boolean;
}

export function provenanceToShape(provenance: Provenance): ProvenanceShape {
  const shape: ProvenanceShape = {
    derived: provenance.derived,
    label: provenance.label,
    attribution_status: provenance.attributionStatus,
    attribution_confidence: provenance.attributionConfidence,
    attribution_policy: provenance.attributionPolicy,
    attempts_started: provenance.attemptsStarted,
    usage_complete: provenance.usageComplete,
    citations_mechanically_verified: provenance.citationsMechanicallyVerified,
  };
  if (provenance.requested?.provider !== undefined) {
    shape.requested_provider = provenance.requested.provider;
  }
  if (provenance.requested?.model !== undefined) {
    shape.requested_model = provenance.requested.model;
  }
  if (provenance.attributionStatus !== "not_applicable") {
    // Present-but-null is meaningful: it says "the host does not expose this", which is
    // different from the field being absent because it never applied.
    shape.resolved_provider = provenance.resolved?.provider ?? null;
    shape.resolved_model = provenance.resolved?.model ?? null;
    shape.reported_provider = provenance.reported?.provider ?? null;
    shape.reported_model = provenance.reported?.model ?? null;
  }
  if (provenance.fallbackUsed !== undefined) shape.fallback_used = provenance.fallbackUsed;
  return shape;
}

/** Provenance for a result no model touched. */
export function deterministicProvenance(
  label: ProvenanceLabel = "deterministic_extraction",
): Provenance {
  return {
    derived: false,
    label,
    attributionStatus: "not_applicable",
    attributionConfidence: "none",
    attributionPolicy: "not_applicable",
    attemptsStarted: 0,
    usageComplete: true,
    citationsMechanicallyVerified: true,
  };
}

function bareModel(ref: string): string {
  const parts = ref.trim().toLowerCase().split("/");
  return parts[parts.length - 1] ?? "";
}

/**
 * Providers often return a dated or namespaced variant of the requested id.
 * `gpt-5.6-luna`, `openai/gpt-5.6-luna` and `gpt-5.6-luna-2026-05-01` all agree; a
 * different family does not. Agreement is deliberately generous about decoration and
 * strict about identity, because the alternative is a false `mismatch` on every provider
 * that stamps a build date.
 */
/**
 * A trailing segment that decorates an id rather than renaming it: an ISO build date or a
 * numeric revision. Deliberately not an arbitrary word - `gpt-4` and `gpt-4-turbo` are
 * different models, and so are `gpt-5.6-luna` and `gpt-5.6-luna-evil`.
 */
const DECORATION = /^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|v?[0-9]+(?:[.\-][0-9]+)*)$/;

/**
 * Agreement is generous about *decoration* and strict about identity.
 *
 * Decoration used to be "the requested id plus a hyphen plus anything", which let a
 * substituted model keep the prefix and pass as the requested one - `gpt-5.6-luna-evil`
 * was classified as `gpt-5.6-luna` and could be published under its name. The suffix must
 * now look like a version, so a rename is a mismatch again.
 */
function modelAgrees(requested: string, observed: string): boolean {
  const left = bareModel(requested);
  const right = bareModel(observed);
  if (left === right) return true;
  const [longer, shorter] = right.length > left.length ? [right, left] : [left, right];
  if (!longer.startsWith(`${shorter}-`)) return false;
  return DECORATION.test(longer.slice(shorter.length + 1));
}

/**
 * Identity, not resemblance: the same id once a namespace and case are normalized.
 *
 * `modelAgrees` is deliberately broader, because its job is to decide what counts as a
 * *contradiction*. This one decides what counts as proof, which is a higher bar.
 */
function modelIsTheSame(requested?: string, observed?: string): boolean {
  // Nothing specific was asked for, so a reported model cannot disagree with it.
  if (!requested || !observed) return true;
  return bareModel(requested) === bareModel(observed);
}

function contradicts(requested: ModelIdentity, observed: ModelIdentity): boolean {
  if (requested.model && observed.model && !modelAgrees(requested.model, observed.model)) {
    return true;
  }
  return Boolean(
    requested.provider
      && observed.provider
      && requested.provider.trim().toLowerCase() !== observed.provider.trim().toLowerCase(),
  );
}

export interface ClassifyInput {
  requested: ModelIdentity;
  resolved: ModelIdentity;
  reported: ModelIdentity;
  /**
   * The adapter's assertion that `reported` came from the provider's own report of what
   * generated the tokens - not from the host echoing the request back. An adapter that
   * cannot tell the two apart passes `false`, and the result is `unverified` rather than
   * `actual`. This flag is the only place where "we know this is real" can be asserted,
   * and it is asserted by the adapter that read the host's source, never inferred here.
   */
  providerConfirmsGeneration: boolean;
}

/** Name the strongest attribution the host's answer actually supports. */
export function classifyAttribution(
  input: ClassifyInput,
): { status: Attribution; confidence: Confidence } {
  const resolvedKnown = identityKnown(input.resolved);
  const reportedKnown = identityKnown(input.reported);
  if (!resolvedKnown && !reportedKnown) return { status: "unknown", confidence: "none" };
  if (reportedKnown && contradicts(input.requested, input.reported)) {
    return { status: "mismatch", confidence: "high" };
  }
  if (resolvedKnown && contradicts(input.requested, input.resolved)) {
    return { status: "mismatch", confidence: "medium" };
  }
  // `actual` is a claim about which *model* generated the tokens, so a confirmation that
  // names only a provider cannot earn it - `identityKnown` is true for either half alone.
  // It also requires the reported id to *be* the requested one. A decorated variant
  // (`luna-2`, `luna-2026-05-01`) is not a contradiction - a build stamp really is the
  // same model - but nothing establishes that it is either: no provider-authoritative
  // alias contract says `name` and `name-2` name one model, and a provider may use numeric
  // names for genuinely different ones. Certifying that as `actual` would falsely prove
  // model-specific routing, so it falls to the weaker truthful label.
  if (
    input.providerConfirmsGeneration
    && Boolean(input.reported.model)
    && modelIsTheSame(input.requested.model, input.reported.model)
  ) {
    return { status: "actual", confidence: "high" };
  }
  if (resolvedKnown) return { status: "resolved", confidence: "medium" };
  return { status: "unverified", confidence: "low" };
}

/** A multi-chunk answer is only as well attributed as its weakest call. */
const ATTRIBUTION_ORDER: Record<Attribution, number> = {
  mismatch: 0,
  unknown: 1,
  unverified: 2,
  resolved: 3,
  actual: 4,
  not_applicable: 5,
};

export function weakestAttribution(
  current: { status: Attribution; confidence: Confidence },
  candidate: { status: Attribution; confidence: Confidence },
): { status: Attribution; confidence: Confidence } {
  return ATTRIBUTION_ORDER[candidate.status] < ATTRIBUTION_ORDER[current.status]
    ? candidate
    : current;
}

/**
 * Apply the configured attribution policy, or throw a bounded failure.
 *
 * `require_match` refuses anything weaker than `actual` or `resolved`: on a host that
 * cannot prove attribution this disables the reader, which is a legitimate choice but
 * never the silent default. `allow_unverified` refuses only an outright contradiction.
 */
export function enforceAttributionPolicy(
  provenance: Provenance,
  policy: AttributionPolicy,
): void {
  const status = provenance.attributionStatus;
  if (status === "not_applicable") return;
  if (status === "mismatch") throw new ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", false);
  if (policy === "require_match" && status !== "actual" && status !== "resolved") {
    throw new ShuntError("PROVENANCE_UNAVAILABLE", "ATTRIBUTION_UNPROVEN", false);
  }
}
