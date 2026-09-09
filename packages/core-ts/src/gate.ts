/**
 * The pre-read gate.
 *
 * Runs before the underlying tool executes, so a blocked decision means the host tool was
 * never invoked and no oversized payload ever existed. The decision is tri-state:
 *
 * - `passthrough` - the host owns the call; the gate could not prove a safe intervention
 * - `allow`       - read-like and provably within a configured bounded form
 * - `blocked`     - an unbounded read of a regular file is provably over the full-read cap
 *
 * Sizing comes from a bounded probe that stops at 351 lines or the byte cap and carries
 * its own 1s deadline. A probe can only block a full read when its observed lower bound
 * proves that the source is over a configured cap; uncertainty passes through to the host.
 */
import { Clock, Deadline, monotonicClock } from "./clock.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { Classification, classifyCommand } from "./shell.js";

export type Decision = "passthrough" | "allow" | "blocked";
export type GateForm =
  | "not_read_like"
  | "full_read"
  | "bounded_lines"
  | "bounded_search"
  | "bounded_metadata"
  | "unclassifiable"
  | "unsafe";

export interface ProbeResult {
  readonly exists: boolean;
  readonly kind?: "file" | "directory" | "fifo" | "socket" | "device" | "other";
  readonly lines?: number;
  readonly bytes?: number;
  readonly exact?: boolean;
}

export type ProbeSelection =
  | { readonly mode: "full" }
  | { readonly mode: "metadata" }
  | { readonly mode: "lines"; readonly startLine: number; readonly limit: number }
  | { readonly mode: "tail"; readonly limit: number }
  | { readonly mode: "search"; readonly maxMatches: number };

export type Prober = (path: string, selection?: ProbeSelection) => ProbeResult;

export interface GateDecision {
  readonly decision: Decision;
  readonly form: GateForm;
  readonly code?: string;
  readonly reason: string;
  readonly observedLines?: number;
  readonly observedBytes?: number;
}

export interface ToolArgs {
  [key: string]: unknown;
}

const PASSTHROUGH: GateDecision = { decision: "passthrough", form: "not_read_like", reason: "" };

function blocked(code: string, form: GateForm, reason: string, extra: Partial<GateDecision> = {}): GateDecision {
  return { decision: "blocked", form, code, reason, ...extra };
}

function passthrough(
  form: GateForm = "not_read_like",
  reason = "",
  extra: Partial<GateDecision> = {},
): GateDecision {
  return { decision: "passthrough", form, reason, ...extra };
}

function allow(form: GateForm): GateDecision {
  return { decision: "allow", form, reason: "" };
}

interface SizedProbe {
  lines: number;
  bytes: number;
  exact: boolean;
}

export class PreReadGate {
  constructor(
    private readonly probe: Prober,
    private readonly limits: Limits = DEFAULT_LIMITS,
    private readonly clock: Clock = monotonicClock,
  ) {}

  evaluate(tool: string, args: ToolArgs): GateDecision {
    try {
      const deadline = Deadline.start(this.clock, this.limits.gateProbeDeadlineMs);
      if (tool === "read") return this.evaluateRead(args, deadline);
      if (tool === "search") return this.evaluateSearch(args, deadline);
      if (tool === "shell") return this.evaluateShell(String(args["command"] ?? ""), deadline);
      return PASSTHROUGH;
    } catch (err) {
      // An unfinished probe does not establish a large source. Fail open and let the
      // host report its own bounded tool result or error.
      if (err instanceof Error && (err as { code?: string }).code === "TIMEOUT") {
        return passthrough("unclassifiable", "PROBE_TIMEOUT");
      }
      if (err instanceof Error && (err as { code?: string }).code === "CANCELLED") {
        return passthrough("unclassifiable", "PROBE_CANCELLED");
      }
      // The gate is an advisory context-cost control. A probe or malformed adapter
      // input must never turn an otherwise valid host call into a synthetic error.
      return passthrough("unclassifiable", "PROBE_FAILED");
    }
  }

  private evaluateRead(args: ToolArgs, deadline: Deadline): GateDecision {
    const path = args["file_path"] ?? args["path"];
    if (typeof path !== "string" || path.length === 0) return passthrough("unclassifiable", "BAD_PATH");

    const limit = args["limit"];
    const offset = args["offset"];
    for (const [name, value] of [["LIMIT", limit], ["OFFSET", offset]] as const) {
      if (value !== undefined && value !== null && (typeof value !== "number" || !Number.isInteger(value))) {
        return passthrough("unclassifiable", `BAD_${name}`);
      }
    }
    if (typeof limit === "number" && limit < 1) {
      return passthrough("unclassifiable", "BAD_LIMIT");
    }
    if (typeof offset === "number" && offset < 0) {
      return passthrough("unclassifiable", "BAD_OFFSET");
    }

    // Any explicit limit is a bounded host call. A limit above our preferred page
    // size is still finite; the gate must not reinterpret it as an unbounded read.
    if (typeof limit === "number" && limit > this.limits.targetedReadMaxLines) {
      return passthrough("bounded_lines", "BOUND_OVER_CAP");
    }

    const bounded = typeof limit === "number";
    if (bounded) {
      const start = typeof offset === "number" ? Math.max(1, offset) : 1;
      // Cover both 0- and 1-based host offset conventions. The extra line is used only
      // for byte proof; the host is still capped at `limit` output lines.
      const scanLimit = limit + (typeof offset === "number" && offset > 0 ? 1 : 0);
      const selected = this.probeSafe(path, deadline, {
        mode: "lines",
        startLine: start,
        limit: scanLimit,
      });
      if (selected === null) return PASSTHROUGH;
      if ("decision" in selected) return selected;
      return this.sizeBounded([selected], "bounded_lines", limit);
    }
    const probed = this.probeSafe(path, deadline);
    if (probed === null) return PASSTHROUGH;
    if ("decision" in probed) return probed;
    return this.sizeFullRead([probed]);
  }

  private evaluateSearch(args: ToolArgs, deadline: Deadline): GateDecision {
    if (
      (args["target"] ?? "content") !== "content"
      || (args["output_mode"] ?? "content") !== "content"
      || (args["context"] ?? 0) !== 0
    ) {
      return passthrough("unclassifiable", "OUTPUT_AMPLIFICATION");
    }
    const maxMatches = args["max_matches"];
    if (
      typeof maxMatches !== "number" ||
      !Number.isInteger(maxMatches) ||
      maxMatches < 1 ||
      maxMatches > this.limits.targetedSearchMaxMatches
    ) {
      return passthrough("unclassifiable", "UNBOUNDED_SEARCH");
    }
    const pattern = args["pattern"];
    if (typeof pattern !== "string" || pattern.length === 0) {
      return passthrough("unclassifiable", "BAD_PATTERN");
    }
    const path = args["path"] ?? args["file_path"];
    if (typeof path !== "string" || path.length === 0) {
      return passthrough("unclassifiable", "UNRESOLVED_PATH");
    }
    const probed = this.probeSafe(path, deadline, { mode: "search", maxMatches });
    if (probed === null) return PASSTHROUGH;
    if ("decision" in probed) return probed;
    return this.sizeBounded([probed], "bounded_search", maxMatches);
  }

  private evaluateShell(command: string, deadline: Deadline): GateDecision {
    const c: Classification = classifyCommand(command);
    if (c.form === "not_read_like") return PASSTHROUGH;
    if (c.form === "unclassifiable") {
      return passthrough("unclassifiable", c.reason || "UNPROVABLE");
    }

    const probes: SizedProbe[] = [];
    let selection: ProbeSelection = { mode: "full" };
    let boundedLines = 0;
    if (c.form === "bounded_metadata") selection = { mode: "metadata" };
    if (c.form === "bounded_search") {
      if ((c.boundMatches ?? 0) > this.limits.targetedSearchMaxMatches) {
        return passthrough("bounded_search", "BOUND_OVER_CAP");
      }
      selection = { mode: "search", maxMatches: c.boundMatches ?? 0 };
    }
    if (c.form === "bounded_lines") {
      const bound = c.boundLines ?? 0;
      boundedLines = bound * c.files.length + (c.files.length > 1 ? c.files.length * 2 : 0);
      if (bound < 1 || boundedLines > this.limits.targetedReadMaxLines) {
        return passthrough("bounded_lines", "BOUND_OVER_CAP", {
          observedLines: boundedLines,
        });
      }
      selection = c.fromEnd
        ? { mode: "tail", limit: bound }
        : { mode: "lines", startLine: c.lineStart ?? 1, limit: bound };
    }
    if (c.form === "bounded_metadata") {
      const fields = c.metadataFields ?? 3;
      let projectedBytes = c.files.length > 1 ? 64 : 0;
      for (const path of c.files) {
        projectedBytes += new TextEncoder().encode(path).length + fields * 24 + 16;
        if (
          c.files.length > this.limits.targetedReadMaxLines
          || projectedBytes > this.limits.maxTargetedReadBytes
        ) {
          return passthrough("bounded_metadata", "OUTPUT_OVER_CAP", {
            observedLines: c.files.length,
            observedBytes: projectedBytes,
          });
        }
      }
    }
    for (const path of c.files) {
      const probed = this.probeSafe(path, deadline, selection);
      if (probed === null) continue;
      if ("decision" in probed) return probed;
      probes.push(probed);
    }
    if (probes.length === 0) return PASSTHROUGH;

    if (c.form === "bounded_metadata") return allow("bounded_metadata");
    if (c.form === "bounded_search") {
      return this.sizeBounded(probes, "bounded_search", c.boundMatches ?? 0);
    }
    if (c.form === "bounded_lines") {
      const headerBytes = c.files.length > 1
        ? c.files.reduce((total, path) => total + new TextEncoder().encode(path).length + 16, 0)
        : 0;
      return this.sizeBounded(probes, "bounded_lines", boundedLines, headerBytes);
    }
    return this.sizeFullRead(probes);
  }

  /** `null` means "let the host handle it" (missing file). */
  private probeSafe(
    path: string,
    deadline: Deadline,
    selection: ProbeSelection = { mode: "full" },
  ): SizedProbe | GateDecision | null {
    deadline.check("PROBE");
    const probe = this.probe(path, selection);
    // Checked again after the scan: an over-budget probe cannot justify a veto.
    deadline.check("PROBE");
    if (!probe.exists) return null;
    if ((probe.kind ?? "file") !== "file") {
      return passthrough("unsafe", "NOT_REGULAR_FILE");
    }
    const lines = probe.lines ?? 0;
    const bytes = probe.bytes ?? 0;
    const exact = probe.exact ?? true;
    if (
      !Number.isSafeInteger(lines) || lines < 0
      || !Number.isSafeInteger(bytes) || bytes < 0
      || typeof exact !== "boolean"
    ) {
      return passthrough("unclassifiable", "INVALID_PROBE");
    }
    return { lines, bytes, exact };
  }

  private sizeBounded(
    probes: SizedProbe[],
    form: "bounded_lines" | "bounded_search",
    outputLines: number,
    extraBytes = 0,
  ): GateDecision {
    const totalBytes = probes.reduce((sum, p) => sum + p.bytes, extraBytes);
    if (probes.some((p) => !p.exact)) {
      return passthrough(form, "UNKNOWN_SCALE", { observedLines: outputLines });
    }
    if (totalBytes > this.limits.maxTargetedReadBytes) {
      return passthrough(form, "OVER_BYTE_THRESHOLD", {
        observedLines: outputLines,
        observedBytes: totalBytes,
      });
    }
    return allow(form);
  }

  private sizeFullRead(probes: SizedProbe[]): GateDecision {
    const totalLines = probes.reduce((sum, p) => sum + p.lines, 0);
    const totalBytes = probes.reduce((sum, p) => sum + p.bytes, 0);
    const inexact = probes.some((p) => !p.exact);
    if (totalBytes > this.limits.maxTargetedReadBytes) {
      return blocked("LARGE_READ", "full_read", "OVER_BYTE_THRESHOLD", {
        observedLines: totalLines,
        observedBytes: totalBytes,
      });
    }
    if (inexact || totalLines > this.limits.fullReadMaxLines) {
      if (totalLines > this.limits.fullReadMaxLines) {
        return blocked("LARGE_READ", "full_read", "OVER_LINE_THRESHOLD", {
          observedLines: totalLines,
          observedBytes: totalBytes,
        });
      }
      // An inexact result with no lower-bound evidence is uncertainty, not a veto.
      return passthrough("full_read", "UNKNOWN_SCALE", {
        observedLines: totalLines,
        observedBytes: totalBytes,
      });
    }
    return allow("full_read");
  }
}

export const GUIDANCE_LARGE_READ =
  "This source is over the full-read threshold, so the read was stopped before it ran. " +
  "Re-read a specific range with offset+limit (<=350 lines), run a bounded search, or ask " +
  "the context-shunt reader a question about it - the reader answers from the source " +
  "without putting it in this conversation.";

export const GUIDANCE_UNCLASSIFIABLE_READ =
  "This command reads a file but could not be proven bounded and safe, so it was stopped " +
  "before it ran. Use a plain read with offset+limit, a bounded search, or ask the " +
  "context-shunt reader a question about the file.";

export const GUIDANCE_UNSAFE_SOURCE =
  "The target is not a regular file, so it was not read. Name a regular file inside a " +
  "configured workspace root.";

export function guidanceFor(decision: GateDecision): string {
  if (decision.code === "LARGE_READ") return GUIDANCE_LARGE_READ;
  if (decision.code === "UNCLASSIFIABLE_READ") return GUIDANCE_UNCLASSIFIABLE_READ;
  return GUIDANCE_UNSAFE_SOURCE;
}
