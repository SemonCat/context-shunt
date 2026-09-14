/**
 * unit reader: the provider payload is intent + bounded evidence, never the wider session.
 *
 * TypeScript-side counterpart to the Python `test_gate_reader_payload_scope.py` gate. The
 * product requirement this audits: Sol forms a bounded query naming a question and one or
 * more source handles; Luna receives only that question, the locator of the chunk under
 * evidence and the chunk's bounded text - never a sibling source registered in the same
 * session that the request did not select.
 *
 * The sentinel is planted where the reader genuinely could have picked it up if it read
 * from the session's registry instead of the request's own `sources` list: a second source
 * registered under the identical session id, holding content the request never references.
 * `FakeLuna.calls` records the exact `system`/`user` strings sent to the provider, so
 * asserting against it is asserting against the real payload boundary.
 */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { Reader } from "../src/reader.js";
import { snapshotBytes } from "../src/snapshot.js";
import { FakeLuna, answerJson, makeRegistry } from "./support.js";

const enc = (s: string) => new TextEncoder().encode(s);

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-ts-"));
}

const QUESTION = "What is the configured retry limit?";

const SELECTED_SOURCE = "service: checkout-api\nretry_limit: 7\ntimeout_ms: 4000\n";

const SENTINEL = "PRIVATE-CONTEXT-SENTINEL-b6f1";
const UNSELECTED_SOURCE = `internal_note: ${SENTINEL} do not disclose customer_id 9182\n`;

describe("reader provider payload scope", () => {
  it("excludes an unselected sibling source from the provider payload", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const selected = registry.register("sess", snapshotBytes(enc(SELECTED_SOURCE)));
    // Registered in the identical session, never referenced by the request below.
    registry.register("sess", snapshotBytes(enc(UNSELECTED_SOURCE)));

    const reply = answerJson("The retry limit is 7 [c1].", [
      { id: "c1", line_start: 2, line_end: 2, quote: "retry_limit: 7" },
    ]);
    const luna = new FakeLuna([reply]);
    const reader = new Reader(registry, luna);

    const request = {
      schema_version: "1.3",
      request_id: "req_payload_scope",
      operation: "read",
      question: QUESTION,
      sources: [
        {
          source_id: selected.sourceId,
          snapshot_id: selected.snapshot.snapshotId,
          selector: { kind: "all" },
        },
      ],
      budgets: { max_chunks: 8, max_answer_bytes: 8192, deadline_ms: 60000 },
    };

    const result = await reader.answerDetailed("sess", request);

    expect(result.envelope.code).toBe("ANSWERED");
    expect(luna.calls.length).toBeGreaterThan(0);

    for (const call of luna.calls) {
      expect(call.system.includes(SENTINEL)).toBe(false);
      expect(call.user.includes(SENTINEL)).toBe(false);
      expect(call.user.includes("9182")).toBe(false);
    }

    // A payload that excluded everything would pass the assertions above vacuously - prove
    // the selected evidence actually arrived.
    expect(luna.calls.some((call) => call.user.includes("retry_limit: 7"))).toBe(true);
    expect(luna.calls.every((call) => call.user.includes(QUESTION))).toBe(true);
  });

  it("excludes an unselected sibling source under a narrow search selector too", async () => {
    const registry = makeRegistry(tmp(), { sessionId: "sess" });
    const selected = registry.register("sess", snapshotBytes(enc(SELECTED_SOURCE)));
    registry.register("sess", snapshotBytes(enc(UNSELECTED_SOURCE)));

    const reply = answerJson("The retry limit is 7 [c1].", [
      { id: "c1", line_start: 2, line_end: 2, quote: "retry_limit: 7" },
    ]);
    const luna = new FakeLuna([reply]);
    const reader = new Reader(registry, luna);

    const request = {
      schema_version: "1.3",
      request_id: "req_payload_scope_search",
      operation: "read",
      question: QUESTION,
      sources: [
        {
          source_id: selected.sourceId,
          snapshot_id: selected.snapshot.snapshotId,
          selector: { kind: "search", pattern: "retry_limit", max_matches: 10 },
        },
      ],
      budgets: { max_chunks: 8, max_answer_bytes: 8192, deadline_ms: 60000 },
    };

    const result = await reader.answerDetailed("sess", request);

    expect(result.envelope.code).toBe("ANSWERED");
    expect(luna.calls.length).toBeGreaterThan(0);
    for (const call of luna.calls) {
      expect(call.system.includes(SENTINEL)).toBe(false);
      expect(call.user.includes(SENTINEL)).toBe(false);
    }
    expect(luna.calls.some((call) => call.user.includes("retry_limit: 7"))).toBe(true);
  });
});
