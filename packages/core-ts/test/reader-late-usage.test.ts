/**
 * unit reader: the envelope's `provenance.attempts_usage_complete` field (schema 1.3+).
 *
 * TypeScript-side counterpart to the Python `test_gate_reader_late_usage.py` gate. The
 * historical failure this replaces: two real production reads each made several reader
 * attempts before answering (`acc_d179a538c9a51b7a`: 7 started, 5 with provider-reported
 * usage; `acc_968992724b9a99d6`: 4 started, 2 with usage), and the missing usage on the
 * unmeasured attempts totalled in the tens of thousands of tokens. `ReaderCost.attemptsUsageComplete`
 * already carried that exact count internally, but the envelope an ordinary `read` returns
 * only ever carried the derived boolean `provenance.usage_complete` - which cannot
 * distinguish "one late attempt out of seven" from "almost nothing is measured". The real
 * count never reached the one place a caller looks at cost for a single answer.
 */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { DEFAULT_LIMITS as L, READER_MODEL } from "../src/limits.js";
import type { ProvenanceShape } from "../src/provenance.js";
import { FallbackChainProvider, HostBridgeProvider } from "../src/provider.js";
import { Reader } from "../src/reader.js";
import { snapshotBytes } from "../src/snapshot.js";
import { FakeLuna, answerJson, makeRegistry } from "./support.js";

const enc = (s: string) => new TextEncoder().encode(s);
const QUESTION = "What is the mode?";
const SOURCE = "mode = fast\n";
const ANSWER_TEXT = answerJson("mode = fast [c1]", [
  { id: "c1", line_start: 1, line_end: 1, quote: "mode = fast" },
]);

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-ts-"));
}

function request(entry: { sourceId: string; snapshot: { snapshotId: string } }) {
  return {
    schema_version: "1.3",
    request_id: "req_late_usage",
    operation: "read",
    question: QUESTION,
    sources: [
      {
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        selector: { kind: "all" },
      },
    ],
    budgets: { max_chunks: 8, max_answer_bytes: 8192, deadline_ms: 60000 },
  };
}

function fixture(provider: { target?: unknown; complete: (...a: never[]) => unknown }) {
  const registry = makeRegistry(tmp(), { sessionId: "sess" });
  const entry = registry.register("sess", snapshotBytes(enc(SOURCE)));
  // biome-ignore lint: test double stands in for the full ReaderProvider interface
  return { registry, entry, reader: new Reader(registry, provider as never) };
}

describe("provenance.attempts_usage_complete", () => {
  it("reports the real attempt count for a partially measured answer (shaped after acc_d179a538c9a51b7a)", async () => {
    const unmeasuredFailure = async () => {
      throw new Error("upstream unavailable");
    };
    const exactWinner = async () => ({
      text: ANSWER_TEXT,
      input_tokens: 9,
      output_tokens: 11,
      usage_exact: true,
    });

    const chain = new FallbackChainProvider(
      new HostBridgeProvider(unmeasuredFailure, L, READER_MODEL, "openai"),
      [new HostBridgeProvider(exactWinner, L, "fallback", "openai")],
    );
    const { entry, reader } = fixture(chain);
    const result = await reader.answerDetailed("sess", request(entry));

    expect(result.envelope.code).toBe("ANSWERED");
    const provenance = result.envelope.provenance as ProvenanceShape;

    expect(provenance.attempts_started).toBe(2);
    // The fact the old boolean could not carry: one of the two attempts is unmeasured,
    // not all of them.
    expect(provenance.attempts_usage_complete).toBe(1);
    expect(provenance.usage_complete).toBe(false);

    // The envelope may not invent its own count - it must match the underlying ledger.
    expect(provenance.attempts_started).toBe(result.cost.attemptsStarted);
    expect(provenance.attempts_usage_complete).toBe(result.cost.attemptsUsageComplete);
  });

  it("reports the complete count explicitly when every attempt is measured", async () => {
    const reply = answerJson("The mode is fast [c1].", [
      { id: "c1", line_start: 1, line_end: 1, quote: "mode = fast" },
    ]);
    const { entry, reader } = fixture(new FakeLuna([reply]));
    const result = await reader.answerDetailed("sess", request(entry));

    const provenance = result.envelope.provenance as ProvenanceShape;
    expect(provenance.attempts_started).toBe(1);
    expect(provenance.attempts_usage_complete).toBe(1);
    expect(provenance.usage_complete).toBe(true);
  });

  it("reports zero measured of several started when every attempt fails (shaped after acc_968992724b9a99d6)", async () => {
    const unmeasuredFailure = async () => {
      throw new Error("upstream unavailable");
    };
    const chain = new FallbackChainProvider(
      new HostBridgeProvider(unmeasuredFailure, L, READER_MODEL, "openai"),
      [new HostBridgeProvider(unmeasuredFailure, L, "fallback", "openai")],
    );
    const { entry, reader } = fixture(chain);
    const result = await reader.answerDetailed("sess", request(entry));

    const provenance = result.envelope.provenance as ProvenanceShape;
    expect(provenance.attempts_started as number).toBeGreaterThanOrEqual(2);
    expect(provenance.attempts_usage_complete).toBe(0);
    expect(provenance.usage_complete).toBe(false);
  });
});
