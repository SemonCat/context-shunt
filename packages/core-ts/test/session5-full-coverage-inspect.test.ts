/**
 * integration accounting (TypeScript core): a spilled source fully re-read through inspect.
 *
 * TypeScript-side counterpart to the Python `test_gate_session5_full_coverage_inspect.py`
 * gate. Session 5 of the 2026-09-14 audit spilled one tool result (a Slack-history dump,
 * ~17.6KB across several hundred short lines) and then paged it back with two `inspect`
 * calls covering the whole byte range. The production finding was that this source, taken
 * alone, made the estimate *worse* than not shunting it at all - the spill's one-time
 * baseline credit was smaller than what the two follow-up extractions cost.
 *
 * This drives the real `ShuntSession`/spill/accounting pipeline end to end and asks the one
 * question that matters: does the scope's summed `net_tokens_saved` actually go negative
 * once the full source has been re-read through inspect, rather than a misleading positive
 * "saving" hiding behind the spill's one-time credit alone.
 */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { EMITTED_SCHEMA_VERSION } from "../src/limits.js";
import { UnavailableProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { makeCapability, makeConfig } from "./support.js";

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-ts-"));
}

/** Synthetic content shaped after the production source: many short, uniform lines. */
function slackHistoryDump(lines = 420): string {
  const rows: string[] = [];
  for (let i = 0; i < lines; i += 1) {
    rows.push(`user_${String(i % 7).padStart(2, "0")}: synthetic message body number ${String(i).padStart(4, "0")}`);
  }
  const body = rows.join("\n") + "\n";
  expect(new TextEncoder().encode(body).length).toBeGreaterThan(16384);
  return body;
}

function inspectRequest(pointer: { source_id: string; snapshot_id: string }, start: number, end: number) {
  return {
    schema_version: EMITTED_SCHEMA_VERSION,
    request_id: `req_inspect_${start}_${end}`,
    operation: "inspect",
    source_id: pointer.source_id,
    snapshot_id: pointer.snapshot_id,
    selector: { kind: "bytes", start, end },
    budgets: { max_result_bytes: 16384, max_scan_lines: 20000 },
  };
}

function statsEnvelope(s: ShuntSession) {
  return s.stats({
    schema_version: EMITTED_SCHEMA_VERSION,
    request_id: "req_stats",
    operation: "stats",
  });
}

describe("a fully re-read spilled source (session 5 shape)", () => {
  it("reports a negative net, not a false saving", () => {
    const config = makeConfig(tmp(), {
      tool_result_capture: { enabled: true, host_ordering_verified_locally: true },
    });
    const session = new ShuntSession("sess", config, makeCapability(true), {
      provider: new UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    });

    const body = slackHistoryDump();
    const bodyBytes = new TextEncoder().encode(body).length;
    const outcome = session.postToolResult("req_slack_history", body)!;
    expect(outcome.action).toBe("spill");
    expect(outcome.envelope!.code).toBe("SPILLED");
    const pointer = outcome.envelope!.pointer as { source_id: string; snapshot_id: string };

    const split = 10000;
    const first = session.inspect(inspectRequest(pointer, 0, split));
    expect(first.code).toBe("EXTRACTED");
    const second = session.inspect(inspectRequest(pointer, split, bodyBytes));
    expect(second.code).toBe("EXTRACTED");

    const stats = statsEnvelope(session).stats!;
    const spillRecord = stats.records.find((r) => r.kind === "spill")!;
    const inspectRecords = stats.records.filter((r) => r.kind === "inspect");
    expect(inspectRecords).toHaveLength(2);

    expect(spillRecord.baseline_credit_tokens).toBeGreaterThan(0);
    for (const r of inspectRecords) {
      expect(r.baseline_credit_tokens).toBe(0);
      expect(r.net_tokens_saved).toBeLessThan(0);
    }

    // The empirical claim: once genuinely read back in full, the scope's net is honestly
    // negative - the spill's one-time credit does not mask the inspect overhead that made
    // this source, in total, cost more than it saved. This is the production finding.
    expect(stats.totals!.net_tokens_saved).toBeLessThan(0);
    expect(stats.totals!.net_tokens_saved).toBeLessThan(spillRecord.net_tokens_saved as number);
  });
});
