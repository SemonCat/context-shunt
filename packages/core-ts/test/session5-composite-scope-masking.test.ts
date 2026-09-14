/**
 * integration accounting (TypeScript core): a mixed scope hides a real loss behind an
 * unrealized credit.
 *
 * TypeScript-side counterpart to the Python
 * `test_gate_session5_composite_scope_masking.py` gate. Session 5 of the 2026-09-14 audit
 * (`20260914_010010_6cbb09e5`) is not one source, it is two: a Slack-history dump spilled
 * and then fully re-read through inspect (a real, negative cost), and a bkt-rules dump
 * spilled and never read back at all (an uncontested, honestly-labelled counterfactual
 * credit, exactly like a session-4 abandoned pointer). The production scope net was a
 * small positive `3312` - which reads, taken alone, as "this scope saved tokens", but is
 * actually one genuine loss netted against one merely-unrealized credit that happens to be
 * larger. Neither of the two single-source tests this composes (`session5-full-coverage-
 * inspect` and `session4-unread-pointer-requery`) proves what happens when both shapes
 * share one scope, which is the actual production shape.
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

function slackHistoryDump(lines = 420): string {
  const rows: string[] = [];
  for (let i = 0; i < lines; i += 1) {
    rows.push(`user_${String(i % 7).padStart(2, "0")}: synthetic message body number ${String(i).padStart(4, "0")}`);
  }
  const body = rows.join("\n") + "\n";
  expect(new TextEncoder().encode(body).length).toBeGreaterThan(16384);
  return body;
}

function bktRulesDump(rows = 430): string {
  const lines: string[] = [];
  for (let i = 0; i < rows; i += 1) {
    lines.push(`rule_${String(i).padStart(4, "0")}: synthetic bkt condition ${String(i).padStart(4, "0")}`);
  }
  const body = lines.join("\n") + "\n";
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

describe("a mixed scope: one realized loss, one unrealized credit (session 5 composite shape)", () => {
  it("reports a positive scope total that is not proof the scope saved tokens", () => {
    const config = makeConfig(tmp(), {
      tool_result_capture: { enabled: true, host_ordering_verified_locally: true },
    });
    const session = new ShuntSession("sess", config, makeCapability(true), {
      provider: new UnavailableProvider("SHOULD_NOT_BE_CALLED"),
    });

    // Source A: Slack history, spilled then fully re-read through inspect.
    const slackBody = slackHistoryDump();
    const slackBytes = new TextEncoder().encode(slackBody).length;
    const slackOutcome = session.postToolResult("req_slack_history", slackBody)!;
    expect(slackOutcome.action).toBe("spill");
    const slackPointer = slackOutcome.envelope!.pointer as { source_id: string; snapshot_id: string };
    const split = 10000;
    const first = session.inspect(inspectRequest(slackPointer, 0, split));
    expect(first.code).toBe("EXTRACTED");
    const second = session.inspect(inspectRequest(slackPointer, split, slackBytes));
    expect(second.code).toBe("EXTRACTED");

    // Source B: bkt rules, spilled and never read back at all.
    const bktOutcome = session.postToolResult("req_bkt_rules", bktRulesDump())!;
    expect(bktOutcome.action).toBe("spill");
    // Deliberately: no read, no inspect against bktOutcome's pointer.

    const stats = statsEnvelope(session).stats!;
    const records = stats.records;
    const spillRecords = records.filter((r) => r.kind === "spill");
    expect(spillRecords).toHaveLength(2);
    // No record carries a source_id, so the two spills are told apart by insertion order -
    // the ledger is a chronological log and the Slack source spilled strictly first.
    const [slackSpill, bktSpill] = spillRecords as [
      (typeof spillRecords)[number],
      (typeof spillRecords)[number],
    ];
    const inspectRecords = records.filter((r) => r.kind === "inspect");
    expect(inspectRecords).toHaveLength(2);

    const knownKinds: BaselineKind[] = ["full_payload_counterfactual", "host_truncated_observed", "none"];
    expect(knownKinds).toContain(slackSpill.baseline_kind);
    expect(slackSpill.baseline_kind).toBe("full_payload_counterfactual");
    expect(bktSpill.baseline_kind).toBe("full_payload_counterfactual");
    for (const r of inspectRecords) {
      expect(r.baseline_credit_tokens).toBe(0);
      expect(r.net_tokens_saved as number).toBeLessThan(0);
    }

    const inspectSum = inspectRecords.reduce((acc, r) => acc + (r.net_tokens_saved as number), 0);
    const slackContribution = (slackSpill.net_tokens_saved as number) + inspectSum;
    expect(slackContribution).toBeLessThan(0);
    expect(bktSpill.net_tokens_saved as number).toBeGreaterThan(0);

    const totalNet = stats.totals!.net_tokens_saved as number;
    expect(totalNet).toBeGreaterThan(0);
    // The scope total must be smaller than the uncontested credit alone: it is already
    // netted against a real loss, not a clean saving stacked on top of it.
    expect(totalNet).toBeLessThan(bktSpill.net_tokens_saved as number);
    expect(totalNet).toBeCloseTo(slackContribution + (bktSpill.net_tokens_saved as number), 0);
  });
});
