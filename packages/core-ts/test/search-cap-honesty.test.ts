/**
 * integration inspect (TypeScript core): a search truncated by `max_matches` must not claim
 * completeness.
 *
 * TypeScript-side counterpart to the Python `test_gate_search_cap_honesty.py` gate. Session 3
 * of the 2026-09-14 audit (`20260913_220020_6c5f7a47`) asked an "exact error count/distinct
 * trace IDs" question that only the LLM reader answered, and the reader's answer timed out at
 * 2/4 chunks - a partial answer the reader path already, correctly, never lets read as a
 * confirmed whole-source count. But the product objective is broader than "the LLM must not
 * lie about an incomplete scan": "exact counts/grouping/filtering should use deterministic
 * processing where feasible - don't make an LLM scan everything for a simple count."
 * `inspect`'s `search` selector is exactly that deterministic alternative for a literal-
 * substring count, and it was never audited for the same honesty property the reader path
 * already has.
 *
 * It should have been: before this fix, a search selector that stopped because it hit its own
 * requested `max_matches` cap - not because the source ran out - still reported
 * `complete: true` and returned no continuation cursor, indiscriminately of whether further
 * matches existed just past the cutoff. The fix scopes `complete` to what it can actually
 * mean: the entire remaining source was looked at for this needle, not merely that the
 * caller's own cap was satisfied. Hitting the match cap now behaves exactly like hitting the
 * scan or byte budget already did.
 */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import type { ExtractionShape } from "../src/envelope.js";
import { EMITTED_SCHEMA_VERSION } from "../src/limits.js";
import { UnavailableProvider } from "../src/provider.js";
import { ShuntSession } from "../src/session.js";
import { makeCapability, makeConfig } from "./support.js";

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-ts-"));
}

/** Many short lines, a literal needle scattered through them - shaped like the Loki payloads
 * in session 3, not copied from any real log. */
function lokiShapedSource(totalLines = 3000, errorEvery = 60): { body: string; trueCount: number } {
  const lines: string[] = [];
  let trueCount = 0;
  for (let i = 0; i < totalLines; i += 1) {
    if (i % errorEvery === 0 && i > 0) {
      lines.push(`line ${String(i).padStart(5, "0")} ERROR synthetic failure code ${i % 7}`);
      trueCount += 1;
    } else {
      lines.push(`line ${String(i).padStart(5, "0")} ok synthetic value ${i % 7}`);
    }
  }
  return { body: lines.join("\n") + "\n", trueCount };
}

function searchRequest(
  pointer: { source_id: string; snapshot_id: string },
  opts: { maxMatches: number; maxScanLines?: number; cursor?: string },
) {
  const request: Record<string, unknown> = {
    schema_version: EMITTED_SCHEMA_VERSION,
    request_id: "req_search",
    operation: "inspect",
    source_id: pointer.source_id,
    snapshot_id: pointer.snapshot_id,
    selector: { kind: "search", needle: "ERROR", max_matches: opts.maxMatches },
    budgets: { max_result_bytes: 16384, max_scan_lines: opts.maxScanLines ?? 20000 },
  };
  if (opts.cursor !== undefined) request["cursor"] = opts.cursor;
  return request;
}

function newSession() {
  const config = makeConfig(tmp(), {
    tool_result_capture: { enabled: true, host_ordering_verified_locally: true },
  });
  return new ShuntSession("sess", config, makeCapability(true), {
    provider: new UnavailableProvider("SHOULD_NOT_BE_CALLED"),
  });
}

function spilledPointer(session: ShuntSession, body: string) {
  const outcome = session.postToolResult("req_source", body)!;
  expect(outcome.action).toBe("spill");
  return outcome.envelope!.pointer as { source_id: string; snapshot_id: string };
}

describe("a search capped by max_matches no longer claims the whole source was seen", () => {
  it("reports incomplete coverage with a continuation cursor when the cap, not the source, stops the scan", () => {
    const session = newSession();
    const { body, trueCount } = lokiShapedSource();
    expect(trueCount).toBe(49);
    const pointer = spilledPointer(session, body);

    const env = session.inspect(searchRequest(pointer, { maxMatches: 20 }));
    const extraction: ExtractionShape = env.extraction!;
    expect(extraction.matches_found).toBe(20);

    // The bug this locks shut: a page that stopped only because it hit its own requested cap
    // - with most of the source never scanned - must not be indistinguishable from a page
    // that genuinely reached the end of the source.
    expect(extraction.complete).toBe(false);
    expect(extraction.next_cursor).not.toBeNull();
    expect(extraction.lines_scanned).toBeLessThan(3000);
  });

  it("lets a caller page a scan-budget cutoff to the true total with zero reader calls", () => {
    // A cursor is bound to its selector (max_matches included), so a resumed call cannot
    // change max_matches mid-page. What a caller *can* vary between pages is the scan
    // budget, so this drives the same recovery through a small max_scan_lines instead: the
    // first page stops on the scan budget with plenty of cap headroom left (max_matches=200,
    // well above the true count), and resuming with the identical selector but another scan
    // budget keeps going until the source is genuinely exhausted.
    const session = newSession();
    const { body, trueCount } = lokiShapedSource();
    const pointer = spilledPointer(session, body);

    let seen = 0;
    let request = searchRequest(pointer, { maxMatches: 200, maxScanLines: 1000 });
    let pages = 0;
    for (;;) {
      pages += 1;
      expect(pages).toBeLessThanOrEqual(10);
      const env = session.inspect(request);
      const extraction: ExtractionShape = env.extraction!;
      seen += extraction.matches_found as number;
      if (extraction.complete) break;
      expect(extraction.next_cursor).not.toBeNull();
      request = searchRequest(pointer, {
        maxMatches: 200,
        maxScanLines: 1000,
        cursor: extraction.next_cursor as string,
      });
    }

    expect(pages).toBeGreaterThan(1);
    expect(seen).toBe(trueCount);
  });

  it("treats max_matches as a resumable per-page cap", () => {
    const session = newSession();
    const { body, trueCount } = lokiShapedSource();
    expect(trueCount).toBe(49);
    const pointer = spilledPointer(session, body);

    const capped = session.inspect(searchRequest(pointer, { maxMatches: 20 }));
    const extraction: ExtractionShape = capped.extraction!;
    expect(extraction.matches_found).toBe(20);
    expect(extraction.complete).toBe(false);
    const cursor = extraction.next_cursor as string;
    expect(cursor).not.toBeNull();

    const resumed = session.inspect(searchRequest(pointer, { maxMatches: 20, cursor }));
    expect(resumed.extraction?.matches_found).toBe(20);
    expect(resumed.extraction?.complete).toBe(false);
    const final = session.inspect(searchRequest(pointer, {
      maxMatches: 20, cursor: resumed.extraction?.next_cursor as string,
    }));
    expect(final.extraction?.matches_found).toBe(9);
    expect(final.extraction?.complete).toBe(true);
    expect(20 + 20 + 9).toBe(trueCount);
  });

  it("reports an honest exact count in one page when the cap is never actually reached", () => {
    const session = newSession();
    const { body, trueCount } = lokiShapedSource();
    const pointer = spilledPointer(session, body);

    const env = session.inspect(searchRequest(pointer, { maxMatches: 200 }));
    const extraction: ExtractionShape = env.extraction!;
    expect(extraction.matches_found).toBe(trueCount);
    expect(extraction.complete).toBe(true);
    expect(extraction.next_cursor).toBeNull();
    expect(extraction.lines_scanned).toBe(3000);
  });
});
