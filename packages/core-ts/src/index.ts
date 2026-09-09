/**
 * context-shunt core: read-only, question-driven, citation-verified.
 *
 * It keeps large sources out of the main agent's context. It blocks oversized full reads
 * before they run, captures only what it withholds, answers questions about that immutable
 * snapshot through a configurable reader model, verifies every citation, and returns a
 * bounded envelope. It never writes to a source.
 *
 * Revision 1.1 adds three things and takes nothing away: a hybrid local store (SQLite owns
 * authorization, TTL, quotas, refcounts and accounting; immutable payloads live in
 * content-addressed private files), two more read-only escape hatches (`inspect` for
 * zero-model exact extraction under a cumulative disclosure ceiling, and `stats` for this
 * session's own bounded metrics), and truthful provenance plus signed token accounting on
 * every envelope.
 *
 * The design is inspired by the Compress-Cache-Retrieve pattern popularized by Headroom.
 * No Headroom code is used, and one difference is deliberate: **no tool here offers
 * retrieval of a cached original back into the main model context.** Everything the agent
 * can reach is either a cited model-derived answer or a capped, cumulatively-limited exact
 * extract.
 *
 * The bound is a byte budget, not a promise that a source can never come back whole. A
 * source small enough to fit the per-page, per-source and per-session ceilings can be
 * returned in full by `inspect` - the 350-line pre-read gate is a context-cost control, not
 * a confidentiality boundary. What the ceilings do guarantee is that a *large* payload
 * cannot be reassembled, and that every disclosed byte is counted.
 */
export const VERSION = "1.1.0";

export * from "./accounting.js";
export * from "./capability.js";
export * from "./citations.js";
export * from "./clock.js";
export * from "./config.js";
export * from "./envelope.js";
export * from "./errors.js";
export * from "./gate.js";
export * from "./inspect.js";
export * from "./guard.js";
export * from "./limits.js";
export * from "./legacy-compact.js";
export * from "./metrics.js";
export * from "./paths.js";
export * from "./probe.js";
export * from "./provenance.js";
export * from "./provider.js";
export * from "./reader.js";
export * from "./registry.js";
export * from "./schema.js";
export * from "./session.js";
export * from "./shell.js";
export * from "./snapshot.js";
export * from "./spill.js";
export * from "./store.js";
export * from "./textindex.js";
