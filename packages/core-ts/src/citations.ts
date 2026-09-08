/**
 * Citation verification.
 *
 * `verified` is written here and nowhere else. The verifier re-reads the snapshot the
 * citation names, checks the handle belongs to this session, that the snapshot hash
 * matches in full, that the locator is in range, and that the quote is an exact substring
 * of the addressed line range (text) or of the canonical JSON of the addressed record.
 *
 * A model claiming `verified: true` proves nothing. Mechanical verification proves
 * location and exactness only; semantic support is what the opt-in Luna eval measures.
 */
import { isShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { SourceRegistry } from "./registry.js";
import { Snapshot, canonicalJson, recordAt, recordCount, resolvePointer } from "./snapshot.js";
import { utf8Length } from "./textindex.js";

export type Reason =
  | "OK"
  | "LINE_OUT_OF_RANGE"
  | "QUOTE_NOT_FOUND"
  | "POINTER_NOT_FOUND"
  | "RECORD_OUT_OF_RANGE"
  | "SNAPSHOT_MISMATCH"
  | "HANDLE_UNKNOWN"
  | "HANDLE_EXPIRED"
  | "QUOTE_OVER_CAP"
  | "LOCATOR_UNSUPPORTED";

export interface VerificationResult {
  readonly verified: boolean;
  readonly reason: Reason;
}

export interface CitationInput {
  readonly id?: string;
  readonly source_id?: string;
  readonly snapshot_id?: string;
  readonly locator?: Record<string, unknown>;
  readonly quote?: unknown;
}

const CITATION_REF = /\[(c\d{1,3})\]/g;

function fail(reason: Reason): VerificationResult {
  return { verified: false, reason };
}

export class CitationVerifier {
  constructor(
    private readonly registry: SourceRegistry,
    private readonly limits: Limits = DEFAULT_LIMITS,
  ) {}

  verify(sessionId: string, citation: CitationInput): VerificationResult {
    const quote = citation.quote;
    if (typeof quote !== "string" || quote.length === 0) return fail("QUOTE_NOT_FOUND");
    if (utf8Length(quote) > this.limits.maxQuoteBytes) return fail("QUOTE_OVER_CAP");

    let entry;
    try {
      entry = this.registry.resolve(sessionId, String(citation.source_id ?? ""));
    } catch (err) {
      const expired = isShuntError(err) && err.detail === "TTL_ELAPSED";
      return fail(expired ? "HANDLE_EXPIRED" : "HANDLE_UNKNOWN");
    }

    const snapshot = entry.snapshot;
    if (citation.snapshot_id !== snapshot.snapshotId) return fail("SNAPSHOT_MISMATCH");

    const locator = citation.locator ?? {};
    const kind = locator["kind"];
    if (kind === "lines") return this.verifyLines(snapshot, locator, quote);
    if (kind === "records") return this.verifyRecords(snapshot, locator, quote);
    // "all" and "search" address a scope, not a location, so they cannot support a quote.
    return fail("LOCATOR_UNSUPPORTED");
  }

  private verifyLines(
    snapshot: Snapshot,
    locator: Record<string, unknown>,
    quote: string,
  ): VerificationResult {
    // A JSON snapshot is addressed by record, never by pretty-printed line number.
    if (snapshot.jsonValue !== undefined) return fail("LOCATOR_UNSUPPORTED");
    const start = locator["start"];
    const end = locator["end"];
    if (!Number.isInteger(start) || !Number.isInteger(end)) return fail("LINE_OUT_OF_RANGE");
    const s = start as number;
    const e = end as number;
    if (s < 1 || e < s || e > snapshot.lineCount) return fail("LINE_OUT_OF_RANGE");
    let window: string;
    try {
      window = snapshot.lineIndex.rangeText(s, e);
    } catch {
      return fail("LINE_OUT_OF_RANGE");
    }
    return window.includes(quote) ? { verified: true, reason: "OK" } : fail("QUOTE_NOT_FOUND");
  }

  private verifyRecords(
    snapshot: Snapshot,
    locator: Record<string, unknown>,
    quote: string,
  ): VerificationResult {
    if (snapshot.jsonValue === undefined) return fail("LOCATOR_UNSUPPORTED");
    const pointer = locator["pointer"];
    const start = locator["start"];
    const end = locator["end"];
    if (typeof pointer !== "string" || !Number.isInteger(start) || !Number.isInteger(end)) {
      return fail("RECORD_OUT_OF_RANGE");
    }
    const s = start as number;
    const e = end as number;
    if (s < 1 || e < s) return fail("RECORD_OUT_OF_RANGE");
    let node: unknown;
    try {
      node = resolvePointer(snapshot.jsonValue, pointer);
    } catch {
      return fail("POINTER_NOT_FOUND");
    }
    if (e > recordCount(node)) return fail("RECORD_OUT_OF_RANGE");
    let window = "";
    try {
      for (let i = s; i <= e; i += 1) window += canonicalJson(recordAt(node, i));
    } catch {
      return fail("RECORD_OUT_OF_RANGE");
    }
    return window.includes(quote) ? { verified: true, reason: "OK" } : fail("QUOTE_NOT_FOUND");
  }
}

export function referencedIds(text: string): string[] {
  return [...text.matchAll(CITATION_REF)].map((m) => m[1] as string);
}

/**
 * Drop every sentence whose citation did not verify. Sentences with no citation are also
 * dropped: an assertion about the source with no evidence must not survive.
 *
 * This is the legacy contract: a model that places its own `[cN]` markers in prose. It is
 * kept, unmodified, for a response that already used that shape - see
 * {@link normalizeClaims} and {@link renderClaims} for the current one, where the program
 * places every marker instead of trusting the model to.
 */
export function stripUnsupportedAssertions(answer: string, validIds: Set<string>): string {
  if (answer.trim().length === 0) return "";
  const parts = answer.trim().split(/(?<=[.!?。！？\n])\s+/);
  const kept: string[] = [];
  for (const part of parts) {
    const refs = new Set(referencedIds(part));
    if (refs.size > 0 && [...refs].every((id) => validIds.has(id))) kept.push(part.trim());
  }
  return kept.join(" ").trim();
}

export interface Claim {
  readonly text: string;
  readonly citation_ids: string[];
}

const CLAIM_CITATION_ID = /^c\d{1,3}$/;
const TRAILING_PUNCT = /^(.*?)([.!?。！？]*)$/s;

/**
 * Structurally validate a model's `claims` array against its own `citations`.
 *
 * A claim survives only if `text` is a non-empty string within
 * `limits.maxClaimTextBytes` and `citation_ids` is a non-empty, duplicate-free list of
 * well-formed ids that all appear in `validLocalIds` - the ids the same response actually
 * declared in its `citations` array (before namespacing). Unknown, duplicate or missing
 * ids drop *that claim*, never the whole answer, and never guessed at: a dropped claim is
 * exactly as much evidence-free as a legacy sentence with no marker, so it is held to the
 * same fail-closed rule.
 *
 * This is structural validation only. Whether a surviving id also verifies against the
 * snapshot bytes is decided later, once, by {@link CitationVerifier} - this function never
 * marks anything `verified`.
 */
export function normalizeClaims(
  raw: unknown,
  validLocalIds: Set<string>,
  limits: Limits = DEFAULT_LIMITS,
): Claim[] {
  if (!Array.isArray(raw)) return [];
  const out: Claim[] = [];
  for (const item of raw.slice(0, limits.maxClaimsPerAnswer)) {
    if (typeof item !== "object" || item === null) continue;
    const rec = item as Record<string, unknown>;
    const text = rec["text"];
    const ids = rec["citation_ids"];
    if (typeof text !== "string" || text.trim().length === 0) continue;
    if (utf8Length(text) > limits.maxClaimTextBytes) continue;
    if (!Array.isArray(ids) || ids.length === 0 || ids.length > limits.maxCitationIdsPerClaim) {
      continue;
    }
    const seen = new Set<string>();
    let malformed = false;
    for (const cid of ids) {
      if (
        typeof cid !== "string"
        || !CLAIM_CITATION_ID.test(cid)
        || seen.has(cid)
        || !validLocalIds.has(cid)
      ) {
        malformed = true;
        break;
      }
      seen.add(cid);
    }
    if (!malformed) out.push({ text: text.trim(), citation_ids: [...(ids as string[])] });
  }
  return out;
}

/**
 * Deterministically render surviving claims into prose with `[cN]` markers.
 *
 * The model never places a marker itself; every one in a published answer is put there by
 * this function, from a citation id the model supplied *and* the verifier confirmed.
 * Marker placement is therefore no longer a formatting task the model can get right or
 * wrong - the historical failure this replaces was exactly that: a correct answer with a
 * valid `citations` entry, discarded because the marker was missing from the prose.
 *
 * A claim with no `citation_ids` is not rendered: an assertion with nothing left to
 * support it is exactly what must not survive, matching the legacy rule in
 * {@link stripUnsupportedAssertions}.
 */
export function renderClaims(claims: Claim[]): string {
  const parts: string[] = [];
  for (const claim of claims) {
    const ids = claim.citation_ids ?? [];
    const text = (claim.text ?? "").trim();
    if (ids.length === 0 || text.length === 0) continue;
    const markers = ids.map((id) => `[${id}]`).join("");
    const match = TRAILING_PUNCT.exec(text);
    const [body, punct] = match ? [match[1] ?? text, match[2] ?? ""] : [text, ""];
    const rendered = punct ? `${body} ${markers}${punct}` : `${text} ${markers}`;
    parts.push(rendered.trim());
  }
  return parts.join(" ").trim();
}
