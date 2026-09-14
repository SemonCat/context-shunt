/**
 * integration accounting (TypeScript core): an abandoned spilled pointer, and a requery
 * Shunt cannot see.
 *
 * TypeScript-side counterpart to the Python `test_gate_session4_unread_pointer_requery.py`
 * gate. Session 4 of the 2026-09-14 audit (`cron_2cf04e39ace6_20260914_061013`) spilled two
 * GBrain search results to pointers (message `960329`, ~34.9KB; message `960357`, ~17.6KB).
 * The reader was never invoked against either pointer. Instead the caller reissued two
 * smaller, more targeted GBrain searches of its own (message `960331`, ~10.8KB wire;
 * message `960359`, ~9.1KB) that landed under the tool-result-capture threshold and passed
 * straight through. The scope's ledger reported `net_tokens_saved=12516` - a real,
 * correctly-computed sum of what Shunt observed - but that says nothing about whether the
 * original two pointers were ever useful: a passthrough below the capture threshold
 * produces no envelope and therefore no operation record at all.
 *
 * This is not an arithmetic bug. What this locks down as regressions is that (1) the scope
 * total is unaffected by a same-shaped recovery workload Shunt never touches, because that
 * workload never reaches its hook in a recordable form, and (2) `BaselineKind`'s vocabulary
 * permanently has no member that could claim an unread pointer's saving was ever confirmed
 * or realized. The unclosed half needs Hermes' own tool-call event stream and is written up
 * in `docs/host-proposal-recovery-correlation.md` as a reviewable proposal, not an installed
 * patch.
 */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import type { BaselineKind } from "../src/accounting.js";
import { EMITTED_SCHEMA_VERSION } from "../src/limits.js";
import { UnavailableProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { makeCapability, makeConfig } from "./support.js";

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-ts-"));
}

/** Synthetic content shaped after a GBrain search result: many short JSON-ish rows. */
function gbrainResult(rows: number, tag: string): string {
  const lines: string[] = [];
  for (let i = 0; i < rows; i += 1) {
    lines.push(`{"doc": "${tag}_${String(i).padStart(5, "0")}", "score": 0.${String(i % 100).padStart(2, "0")}}`);
  }
  return lines.join("\n") + "\n";
}

function statsEnvelope(s: ShuntSession) {
  return s.stats({
    schema_version: EMITTED_SCHEMA_VERSION,
    request_id: "req_stats",
    operation: "stats",
  });
}

/**
 * Exhaustive switch: fails to compile if `BaselineKind` ever gains a member this does not
 * name. That is the TypeScript-side equivalent of the Python enum-value-set lock - a future
 * member meaning "this withheld payload was confirmed used" cannot land silently.
 */
function assertKnownBaselineKind(kind: BaselineKind): void {
  switch (kind) {
    case "full_payload_counterfactual":
    case "host_truncated_observed":
    case "none":
      return;
    default: {
      const exhaustive: never = kind;
      throw new Error(`unhandled BaselineKind: ${exhaustive}`);
    }
  }
}

describe("an abandoned spilled pointer and an invisible recovery requery (session 4 shape)", () => {
  it("credits two never-read spills as counterfactuals, never confirmations", () => {
    const config = makeConfig(tmp(), {
      tool_result_capture: { enabled: true, host_ordering_verified_locally: true },
    });
    const session = new ShuntSession("sess", config, makeCapability(true), {
      provider: new UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    });

    const first = gbrainResult(700, "alpha"); // comfortably over the 16384-byte spill threshold
    const second = gbrainResult(460, "beta");
    expect(new TextEncoder().encode(first).length).toBeGreaterThan(16384);
    expect(new TextEncoder().encode(second).length).toBeGreaterThan(16384);

    const outcomeA = session.postToolResult("req_search_960329", first)!;
    const outcomeB = session.postToolResult("req_search_960357", second)!;
    expect(outcomeA.action).toBe("spill");
    expect(outcomeA.envelope!.code).toBe("SPILLED");
    expect(outcomeB.action).toBe("spill");
    expect(outcomeB.envelope!.code).toBe("SPILLED");

    const records = statsEnvelope(session).stats!.records;
    const spillRecords = records.filter((r) => r.kind === "spill");
    expect(spillRecords).toHaveLength(2);
    for (const r of spillRecords) {
      assertKnownBaselineKind(r.baseline_kind as BaselineKind);
      expect(r.baseline_kind).toBe("full_payload_counterfactual");
      expect(r.baseline_credit_tokens).toBeGreaterThan(0);
      expect(r.net_tokens_saved).toBeGreaterThan(0);
    }
    expect(records.some((r) => r.kind === "read" || r.kind === "refined_read" || r.kind === "inspect")).toBe(
      false,
    );
  });

  it("never lets a below-threshold recovery search reach the ledger", () => {
    const config = makeConfig(tmp(), {
      tool_result_capture: { enabled: true, host_ordering_verified_locally: true },
    });
    const session = new ShuntSession("sess", config, makeCapability(true), {
      provider: new UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    });

    session.postToolResult("req_search_960329", gbrainResult(700, "alpha"));
    session.postToolResult("req_search_960357", gbrainResult(460, "beta"));

    // The reissued, narrower recovery searches: smaller by construction, so they land under
    // the capture threshold and pass straight through, exactly like the production requery.
    const recoveryOne = gbrainResult(210, "recover1");
    const recoveryTwo = gbrainResult(180, "recover2");
    expect(new TextEncoder().encode(recoveryOne).length).toBeLessThanOrEqual(16384);
    expect(new TextEncoder().encode(recoveryTwo).length).toBeLessThanOrEqual(16384);

    const outcomeC = session.postToolResult("req_search_960331", recoveryOne)!;
    const outcomeD = session.postToolResult("req_search_960359", recoveryTwo)!;
    // Directly observable without touching stats at all: a passthrough carries no envelope.
    expect(outcomeC.action).toBe("passthrough");
    expect(outcomeC.envelope).toBeUndefined();
    expect(outcomeD.action).toBe("passthrough");
    expect(outcomeD.envelope).toBeUndefined();

    // Querying stats is itself an accounted operation ("stats"), so a single query at the
    // end - after every post - is what proves the point: the only non-stats records in the
    // whole scope are the two original spills.
    const records = statsEnvelope(session).stats!.records;
    const nonStats = records.filter((r) => r.kind !== "stats");
    expect(nonStats).toHaveLength(2);
    expect(nonStats.every((r) => r.kind === "spill")).toBe(true);
    expect(nonStats.every((r) => r.baseline_kind === "full_payload_counterfactual")).toBe(true);
    expect(nonStats.every((r) => (r.net_tokens_saved as number) > 0)).toBe(true);
  });
});
