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

    // The counter is not the whole honesty question: what does the ledger say this
    // answer actually cost? The winner alone reported an exact 9/11, but one of the two
    // started attempts is genuinely unmeasured - reporting the winner's 9/11 as `exact`
    // would silently zero the failed attempt's real, unknown cost. The reader guards
    // against exactly this by downgrading the whole total to a named byte estimate
    // whenever any attempt is unmeasured.
    expect(result.cost.method).toBe("bytes_div_4");
    expect(result.cost.method).not.toBe("exact");
    // A downgraded estimate must still be a real number derived from measured bytes -
    // not blanked out just because it is no longer exact.
    expect(result.cost.inputTokens).toBeGreaterThan(0);
    expect(result.cost.outputTokens).toBeGreaterThan(0);
    // And it must not simply equal the winner's exact figures - if it did, the unmeasured
    // failed attempt's prompt bytes would have been dropped from the total.
    expect(result.cost.inputTokens === 9 && result.cost.outputTokens === 11).toBe(false);
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

    // The control case: nothing is unmeasured, so the ledger is allowed to say `exact`
    // and to carry the provider's own numbers rather than a byte estimate.
    expect(result.cost.method).toBe("exact");
    expect(result.cost.inputTokens).toBe(10);
    expect(result.cost.outputTokens).toBe(5);
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

    // Nothing was ever measured, but calls were still made and billed - the honest
    // report is a named estimate derived from the bytes actually sent, never a bare
    // zero. A zero here would read as "this failure cost nothing", which is false.
    expect(result.cost.method).toBe("bytes_div_4");
    expect(result.cost.inputTokens).toBeGreaterThan(0);
  });
});
