/** unit citations (TypeScript core) - the same fixture corpus as the Python core. */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  CitationVerifier, normalizeClaims, renderClaims, stripUnsupportedAssertions,
  unpublishedMarkerIds,
} from "../src/citations.js";
import { DEFAULT_LIMITS } from "../src/limits.js";
import { Reader } from "../src/reader.js";
import { SourceRegistry } from "../src/registry.js";
import { JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE, snapshotBytes } from "../src/snapshot.js";
import { conformance } from "./fixtures.js";
import { ScopeIdentity, SnapshotStore } from "../src/store.js";
import { FakeLuna, answerJson, makeIdentity, makeRegistry } from "./support.js";

const cases = conformance("citation-cases.json");
const enc = (s: string) => new TextEncoder().encode(s);

function registryWithSources() {
  const registry = makeRegistry(mkdtempSync(join(tmpdir(), "shunt-cit-")), { sessionId: "sess_a" });
  const entries: Record<string, ReturnType<SourceRegistry["register"]>> = {};
  for (const [name, spec] of Object.entries<any>(cases.sources)) {
    const media = spec.media_type === "application/json" ? JSON_MEDIA_TYPE : TEXT_MEDIA_TYPE;
    entries[name] = registry.register("sess_a", snapshotBytes(enc(spec.content), media));
  }
  return { registry, entries };
}

describe("citation verification conformance", () => {
  it("has a non-empty corpus covering text, JSON and failures", () => {
    expect(cases.cases.length).toBeGreaterThanOrEqual(25);
    const reasons = new Set(cases.cases.map((c: any) => c.expect.reason));
    for (const reason of ["OK", "QUOTE_NOT_FOUND", "LINE_OUT_OF_RANGE", "SNAPSHOT_MISMATCH"]) {
      expect(reasons.has(reason)).toBe(true);
    }
  });

  for (const c of cases.cases) {
    it(`verifies ${c.id}`, () => {
      const { registry, entries } = registryWithSources();
      const entry = entries[c.source]!;
      let session = c.foreign_session ? "sess_b" : "sess_a";
      let verifier = new CitationVerifier(registry);
      let sourceId = c.source_id ?? entry.sourceId;
      let snapshotId = c.snapshot_id ?? entry.snapshot.snapshotId;
      if (c.expired) {
        const clock = { now: 1_700_000_000_000 };
        const expiringStore = new SnapshotStore(
          join(mkdtempSync(join(tmpdir(), "shunt-exp-")), "cache"),
          DEFAULT_LIMITS,
          () => clock.now,
        );
        const expiringIdentity = makeIdentity("sess_a");
        expiringStore.openScope(expiringIdentity);
        const expiring = new SourceRegistry(expiringStore, expiringIdentity);
        const handle = expiring.register("sess_a", entry.snapshot);
        clock.now += (DEFAULT_LIMITS.storeHandleTtlSeconds + 1) * 1000;
        verifier = new CitationVerifier(expiring);
        sourceId = handle.sourceId;
        session = "sess_a";
      }
      const result = verifier.verify(session, {
        source_id: sourceId,
        snapshot_id: snapshotId,
        locator: c.locator,
        quote: c.quote,
      });
      expect({ verified: result.verified, reason: result.reason }).toEqual({
        verified: c.expect.verified,
        reason: c.expect.reason,
      });
    });
  }
});

const claimsCases = conformance("claims-cases.json");

describe("structured claims conformance", () => {
  it("has a corpus covering the fail-closed reasons", () => {
    expect(claimsCases.cases.length).toBeGreaterThanOrEqual(15);
    const ids = new Set(claimsCases.cases.map((c: any) => c.id));
    for (const id of [
      "unknown_citation_id_drops_the_claim",
      "duplicate_citation_id_within_a_claim_drops_it",
      "empty_citation_ids_drops_the_claim",
      "multi_citation_claim",
      "multi_claim_answer",
      "one_bad_claim_does_not_sink_a_good_one",
    ]) {
      expect(ids.has(id)).toBe(true);
    }
  });

  // Both cores must agree on every case here - this is the structural half of the fix for
  // the historical marker-omission class: a claim survives only when its citation_ids are
  // well-formed, unique, and every one of them names an id the same response actually
  // declared. Rendering then places every marker mechanically.
  for (const c of claimsCases.cases) {
    it(`normalizes and renders ${c.id}`, () => {
      const validIds = new Set<string>(c.citations_seen);
      const survivors = normalizeClaims(c.claims, validIds);
      const rendered = renderClaims(survivors);
      expect(survivors).toEqual(c.expect.surviving_claims);
      expect(rendered).toBe(c.expect.rendered);
    });
  }

  // A claim that writes its own marker is dropped, whatever the marker names. The
  // published `answer` is rendered from `text` verbatim, so a model-authored `[c999]` used
  // to reach the envelope as though the program had placed it - naming a citation that was
  // never published, inside an envelope still reporting citationsMechanicallyVerified.
  // Escaping would keep model bytes in a field whose whole meaning is that the program
  // wrote them, so the claim goes instead.
  it("refuses marker syntax in claim text rather than escaping it", () => {
    const seen = new Set(["c1"]);
    expect(
      normalizeClaims([{ text: "Retries stop after three attempts [c999]." , citation_ids: ["c1"] }], seen),
    ).toEqual([]);
    // The id being real changes nothing.
    expect(normalizeClaims([{ text: "Three [c1].", citation_ids: ["c1"] }], seen)).toEqual([]);
    // Brackets that are not marker syntax are ordinary prose.
    expect(
      normalizeClaims([{ text: "Read from config[cache] on startup.", citation_ids: ["c1"] }], seen),
    ).toEqual([{ text: "Read from config[cache] on startup.", citation_ids: ["c1"] }]);
  });
});

describe("the publication invariant", () => {
  // Every `[cN]` in a published answer must name a citation the same envelope publishes.
  // `normalizeClaims` closes the forged-marker route in; this is the check on the way out,
  // so no later path can reintroduce one.
  it("has a corpus", () => {
    expect(claimsCases.marker_cases.cases.length).toBeGreaterThanOrEqual(6);
  });
  for (const c of claimsCases.marker_cases.cases) {
    it(`reports unpublished markers for ${c.id}`, () => {
      expect(unpublishedMarkerIds(c.text, new Set<string>(c.published))).toEqual(
        c.expect.unpublished,
      );
    });
  }
});

function request(entry: { sourceId: string; snapshot: { snapshotId: string } }, selector?: unknown) {
  return {
    schema_version: "1.0",
    request_id: "req_c",
    operation: "read",
    question: "What does the source say?",
    sources: [
      {
        source_id: entry.sourceId,
        snapshot_id: entry.snapshot.snapshotId,
        selector: selector ?? { kind: "all" },
      },
    ],
    budgets: { max_chunks: 8, max_answer_bytes: 8192, deadline_ms: 60000 },
  };
}

describe("the verifier is the only writer of `verified`", () => {
  it("ignores a model that claims verified", async () => {
    const registry = makeRegistry(mkdtempSync(join(tmpdir(), "shunt-cit-")), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("alpha\nbeta\n")));
    const reply = JSON.stringify({
      answer: "It says gamma [c1].",
      citations: [{ id: "c1", line_start: 1, line_end: 1, quote: "gamma", verified: true }],
    });
    const env = await new Reader(registry, new FakeLuna([reply])).answer("sess", request(entry));
    expect(env.code).toBe("CITATION_INVALID");
    expect(env.citations).toEqual([]);
    expect(env.answer).toBe("");
  });

  it("removes assertions without valid evidence and keeps the valid ones", async () => {
    const registry = makeRegistry(mkdtempSync(join(tmpdir(), "shunt-cit-")), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("alpha\nbeta\n")));
    const reply = answerJson("The first line is alpha [c1]. The third line is gamma [c2].", [
      { id: "c1", line_start: 1, line_end: 1, quote: "alpha" },
      { id: "c2", line_start: 3, line_end: 3, quote: "gamma" },
    ]);
    const env = await new Reader(registry, new FakeLuna([reply])).answer("sess", request(entry));
    expect(env.code).toBe("ANSWERED");
    expect(env.answer).toContain("alpha");
    expect(env.answer).not.toContain("gamma");
    expect(env.citations.map((c) => c.id)).toEqual(["c1"]);
  });

  it("drops uncited sentences", () => {
    expect(stripUnsupportedAssertions("Alpha is here [c1]. Also it is fast.", new Set(["c1"]))).toBe(
      "Alpha is here [c1].",
    );
  });

  it("rejects a quote over the byte cap even when it is present in the source", () => {
    const registry = makeRegistry(mkdtempSync(join(tmpdir(), "shunt-cit-")), { sessionId: "sess" });
    const entry = registry.register("sess", snapshotBytes(enc("Z".repeat(600) + "\n")));
    const result = new CitationVerifier(registry).verify("sess", {
      source_id: entry.sourceId,
      snapshot_id: entry.snapshot.snapshotId,
      locator: { kind: "lines", start: 1, end: 1 },
      quote: "Z".repeat(DEFAULT_LIMITS.maxQuoteBytes + 1),
    });
    expect(result).toEqual({ verified: false, reason: "QUOTE_OVER_CAP" });
  });

  it("rejects a citation that mixes two snapshots", () => {
    const registry = makeRegistry(mkdtempSync(join(tmpdir(), "shunt-cit-")), { sessionId: "sess" });
    const first = registry.register("sess", snapshotBytes(enc("alpha\n")));
    const second = registry.register("sess", snapshotBytes(enc("changed\n")));
    const result = new CitationVerifier(registry).verify("sess", {
      source_id: second.sourceId,
      snapshot_id: first.snapshot.snapshotId,
      locator: { kind: "lines", start: 1, end: 1 },
      quote: "changed",
    });
    expect(result.verified).toBe(false);
  });
});
