import { describe, expect, it } from "vitest";

import { Coverage, buildEnvelope } from "../src/envelope.js";
import { enforce, enforceOrFixed } from "../src/guard.js";
import { DEFAULT_LIMITS, envelopeByteCap } from "../src/limits.js";
import { compactToolResult } from "../src/legacy-compact.js";
import { deterministicProvenance } from "../src/provenance.js";

function legacyEnvelope(summary: string, summaryBytes = new TextEncoder().encode(summary).length) {
  return buildEnvelope({
    requestId: "req_legacy",
    status: "partial",
    code: "LEGACY_COMPACTED",
    coverage: new Coverage(),
    retryable: false,
    resultKind: "legacy_compaction",
    provenance: deterministicProvenance("legacy_compaction"),
    accountingId: "acc_0123456789abcdef",
    legacyCompaction: {
      deterministic: true,
      source_id: "src_legacy1234",
      snapshot_id: "sha256:" + "0".repeat(64),
      summary,
      summary_bytes: summaryBytes,
      original_bytes: 100,
      hard_cap_chars: 16_000,
      original_failure: "MODEL_ERROR",
    },
  });
}

describe("legacy compactor", () => {
  it("matches the incumbent log shaping for a small signal-bearing input", () => {
    expect(compactToolResult("one\nERROR: fail")).toBe(
      "Line count: 2\n"
      + "High-signal line count: 1\n\n"
      + "High-signal exact lines:\n"
      + "L2: ERROR: fail\n\n"
      + "First sample lines:\n"
      + "L1: one\n"
      + "L2: ERROR: fail",
    );
  });

  it("matches the incumbent JSON structure, field and snippet sections", () => {
    const payload = JSON.stringify({
      status: "error",
      query: "sum(rate(errors[5m]))",
      results: [{ value: 1 }, { value: 2 }],
    });
    expect(compactToolResult(payload)).toBe(
      "JSON structure:\n"
      + "Root: object(3 keys)\n"
      + "$.results: array(2 items)\n"
      + "$.results[0]: object(1 keys)\n"
      + "$.results[1]: object(1 keys)\n\n"
      + "Query/time/count/status fields:\n"
      + "$.status: \"error\"\n"
      + "$.query: \"sum(rate(errors[5m]))\"\n\n"
      + "High-signal exact JSON string values:\n"
      + "L1: $.status: error\n\n"
      + "Representative exact JSON string values:\n"
      + "L1: $.query: sum(rate(errors[5m]))\n\n"
      + "Representative JSON snippets:\n"
      + "Top-level keys: status, query, results\n"
      + "$.status: \"error\"\n"
      + "$.query: \"sum(rate(errors[5m]))\"\n"
      + "$.results: [{\"value\": 1}, {\"value\": 2}]",
    );
  });

  it("collapses repeated lines and samples both ends without reproducing the body", () => {
    const body = [
      ...Array.from({ length: 40 }, () => "same line"),
      "unique tail",
    ].join("\n");
    const summary = compactToolResult(body);
    expect(summary).toContain("Repeated exact lines:");
    expect(summary).toContain("40x: same line");
    expect(summary).not.toContain("same line\nsame line\nsame line");

    const large = Array.from({ length: 100 }, (_, i) => `row ${i}`).join("\n");
    const sampled = compactToolResult(large);
    expect(sampled).toContain("L1: row 0");
    expect(sampled).toContain("L100: row 99");
    expect(sampled).not.toContain("L50: row 49");
  });

  it("redacts bearer, OpenAI-shaped and Slack-shaped secret values", () => {
    const bearer = "a".repeat(40);
    const openAi = "b1".repeat(20);
    const slack = "c".repeat(24);
    const summary = compactToolResult(
      `Authorization: Bearer ${bearer}\nsk-${openAi}\nxoxb-${slack}\nordinary line`,
    );
    expect(summary).toContain("[redacted secret]");
    expect(summary).not.toContain(bearer);
    expect(summary).not.toContain(openAi);
    expect(summary).not.toContain(slack);
  });

  it("degrades malformed JSON to the log path and stays within a narrow cap", () => {
    const malformed = "{\"broken\": [1, 2, " + "x".repeat(5_000);
    const summary = compactToolResult(malformed, { hardChars: 100 });
    expect(summary).toContain("Line count:");
    expect(Array.from(summary).length).toBeLessThanOrEqual(100);
  });

  it("caps multibyte summaries by characters without splitting a surrogate pair", () => {
    const summary = compactToolResult("日本語 ERROR: 失敗しました\n".repeat(500), { hardChars: 100 });
    expect(Array.from(summary).length).toBeLessThanOrEqual(100);
    expect(summary).not.toContain("\ufffd");
  });
});

describe("legacy envelope guard", () => {
  it("keeps legacy envelopes under the ordinary wire cap", () => {
    expect(envelopeByteCap("legacy_compaction")).toBe(DEFAULT_LIMITS.maxEnvelopeBytes);
  });

  it("requires a deterministic, correctly measured, secret-free bounded summary", () => {
    const marker = "-----BEGIN PRIVATE KEY-----";
    expect(() => enforce(legacyEnvelope(marker))).toThrow("secret marker");
    expect(() => enforce(legacyEnvelope("safe", 99))).toThrow("summary_bytes");
    expect(() => enforce(legacyEnvelope("x".repeat(DEFAULT_LIMITS.maxExtractionBytes + 1))))
      .toThrow("over per-result cap");
  });

  it("converts an invalid legacy envelope to a fixed error without echoing its body", () => {
    const marker = "-----BEGIN PRIVATE KEY-----";
    const invalid = legacyEnvelope(marker);
    const safe = enforceOrFixed(invalid);
    expect(safe.code).toBe("LIMIT_EXCEEDED");
    expect(JSON.stringify(safe)).not.toContain(marker);
    expect(safe.legacy_compaction).toBeUndefined();
  });
});
