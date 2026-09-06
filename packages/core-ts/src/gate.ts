/**
 * The pre-read gate.
 *
 * Runs before the underlying tool executes, so a blocked decision means the host tool was
 * never invoked and no oversized payload ever existed. The decision is tri-state:
 *
 * - `passthrough` - not read-like (or the file does not exist); host policy owns it
 * - `allow`       - read-like and provably small or provably bounded
 * - `blocked`     - `LARGE_READ` / `UNCLASSIFIABLE_READ` / `UNSAFE_SOURCE`
 *
 * Sizing comes from a bounded probe that stops at 351 lines or the byte cap and carries
 * its own 1s deadline; an inexact probe is "unknown scale", which blocks.
 */
import { Clock, Deadline, monotonicClock } from "./clock.js";
import { isShuntError } from "./errors.js";
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

export type Prober = (path: string) => ProbeResult;

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
    const deadline = Deadline.start(this.clock, this.limits.gateProbeDeadlineMs);
    try {
      if (tool === "read") return this.evaluateRead(args, deadline);
      if (tool === "search") return this.evaluateSearch(args);
      if (tool === "shell") return this.evaluateShell(String(args["command"] ?? ""), deadline);
      return PASSTHROUGH;
    } catch (err) {
      if (isShuntError(err) && (err.code === "TIMEOUT" || err.code === "CANCELLED")) {
        // An unfinished probe means unknown scale, which blocks rather than executes.
        return blocked("LARGE_READ", "full_read", "PROBE_TIMEOUT");
      }
      throw err;
    }
  }

  private evaluateRead(args: ToolArgs, deadline: Deadline): GateDecision {
    const path = args["file_path"] ?? args["path"];
    if (typeof path !== "string" || path.length === 0) return PASSTHROUGH;

    const limit = args["limit"];
    const offset = args["offset"];
    for (const [name, value] of [["LIMIT", limit], ["OFFSET", offset]] as const) {
      if (value !== undefined && value !== null && (typeof value !== "number" || !Number.isInteger(value))) {
        return blocked("UNCLASSIFIABLE_READ", "unclassifiable", `BAD_${name}`);
      }
    }
    if (typeof limit === "number" && limit < 1) {
      return blocked("UNCLASSIFIABLE_READ", "unclassifiable", "BAD_LIMIT");
    }
    if (typeof offset === "number" && offset < 0) {
      return blocked("UNCLASSIFIABLE_READ", "unclassifiable", "BAD_OFFSET");
    }

    const bounded = typeof limit === "number" && limit <= this.limits.targetedReadMaxLines;
    const probed = this.probeSafe(path, deadline);
    if (probed === null) return PASSTHROUGH;
    if ("decision" in probed) return probed;
    if (bounded) return allow("bounded_lines");
    return this.sizeFullRead([probed]);
  }

  private evaluateSearch(args: ToolArgs): GateDecision {
    const maxMatches = args["max_matches"];
    if (
      typeof maxMatches !== "number" ||
      !Number.isInteger(maxMatches) ||
      maxMatches < 1 ||
      maxMatches > this.limits.targetedSearchMaxMatches
    ) {
      return blocked("UNCLASSIFIABLE_READ", "unclassifiable", "UNBOUNDED_SEARCH");
    }
    const pattern = args["pattern"];
    if (typeof pattern !== "string" || pattern.length === 0) {
      return blocked("UNCLASSIFIABLE_READ", "unclassifiable", "BAD_PATTERN");
    }
    return allow("bounded_search");
  }

  private evaluateShell(command: string, deadline: Deadline): GateDecision {
    const c: Classification = classifyCommand(command);
    if (c.form === "not_read_like") return PASSTHROUGH;
    if (c.form === "unclassifiable") {
      return blocked("UNCLASSIFIABLE_READ", "unclassifiable", c.reason || "UNPROVABLE");
    }

    const probes: SizedProbe[] = [];
    for (const path of c.files) {
      const probed = this.probeSafe(path, deadline);
      if (probed === null) return PASSTHROUGH;
      if ("decision" in probed) return probed;
      probes.push(probed);
    }

    if (c.form === "bounded_metadata") return allow("bounded_metadata");
    if (c.form === "bounded_search") {
      if ((c.boundMatches ?? 0) > this.limits.targetedSearchMaxMatches) {
        return blocked("UNCLASSIFIABLE_READ", "unclassifiable", "UNBOUNDED_SEARCH");
      }
      return allow("bounded_search");
    }
    if (c.form === "bounded_lines") {
      const bound = c.boundLines ?? 0;
      if (bound > this.limits.targetedReadMaxLines) {
        return blocked("LARGE_READ", "bounded_lines", "BOUND_OVER_CAP", { observedLines: bound });
      }
      return allow("bounded_lines");
    }
    return this.sizeFullRead(probes);
  }

  /** `null` means "let the host handle it" (missing file). */
  private probeSafe(path: string, deadline: Deadline): SizedProbe | GateDecision | null {
    deadline.check("PROBE");
    const probe = this.probe(path);
    // Checked again after the scan: a probe that overran the budget leaves the overall
    // scale unknown, and unknown scale blocks.
    deadline.check("PROBE");
    if (!probe.exists) return null;
    if ((probe.kind ?? "file") !== "file") {
      return blocked("UNSAFE_SOURCE", "unsafe", "NOT_REGULAR_FILE");
    }
    return { lines: probe.lines ?? 0, bytes: probe.bytes ?? 0, exact: probe.exact ?? true };
  }

  private sizeFullRead(probes: SizedProbe[]): GateDecision {
    const totalLines = probes.reduce((sum, p) => sum + p.lines, 0);
    const totalBytes = probes.reduce((sum, p) => sum + p.bytes, 0);
    const inexact = probes.some((p) => !p.exact);
    if (inexact || totalLines > this.limits.fullReadMaxLines) {
      return blocked(
        "LARGE_READ",
        "full_read",
        inexact ? "UNKNOWN_SCALE" : "OVER_LINE_THRESHOLD",
        inexact ? {} : { observedLines: totalLines, observedBytes: totalBytes },
      );
    }
    if (totalBytes > this.limits.maxTargetedReadBytes) {
      return blocked("LARGE_READ", "full_read", "OVER_BYTE_THRESHOLD", {
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
