/**
 * context-shunt core: read-only, question-driven, citation-verified.
 *
 * v1 keeps large sources out of the main agent's context. It blocks oversized full reads
 * before they run, answers questions about a source through a fixed cheap reader model,
 * verifies every citation against an immutable snapshot, and returns a bounded envelope.
 * It never writes to a source.
 */
export const VERSION = "1.0.0";

export * from "./capability.js";
export * from "./citations.js";
export * from "./clock.js";
export * from "./config.js";
export * from "./envelope.js";
export * from "./errors.js";
export * from "./gate.js";
export * from "./guard.js";
export * from "./limits.js";
export * from "./metrics.js";
export * from "./paths.js";
export * from "./probe.js";
export * from "./provider.js";
export * from "./reader.js";
export * from "./registry.js";
export * from "./schema.js";
export * from "./session.js";
export * from "./shell.js";
export * from "./snapshot.js";
export * from "./spill.js";
export * from "./textindex.js";
