/** unit contract (TypeScript core) - the schemas are the cross-language boundary. */
import { describe, expect, it } from "vitest";

import { ShuntError } from "../src/errors.js";
import { DEFAULT_LIMITS, statusCodePairs, legalPair } from "../src/limits.js";
import { envelopeValidator, requestValidator, validateRequest } from "../src/schema.js";
import { fixtureDocs } from "./fixtures.js";

describe("request fixtures", () => {
  for (const { name, document } of fixtureDocs("request", "valid")) {
    it(`accepts ${name}`, () => expect(requestValidator()(document)).toBe(true));
  }
  for (const { name, document } of fixtureDocs("request", "invalid")) {
    it(`rejects ${name}`, () => expect(requestValidator()(document)).toBe(false));
  }
});

describe("envelope fixtures", () => {
  for (const { name, document } of fixtureDocs("envelope", "valid")) {
    it(`accepts ${name}`, () => expect(envelopeValidator()(document)).toBe(true));
  }
  for (const { name, document } of fixtureDocs("envelope", "invalid")) {
    it(`rejects ${name}`, () => expect(envelopeValidator()(document)).toBe(false));
  }
});

describe("contract invariants", () => {
  const minimal = () =>
    structuredClone(
      fixtureDocs("request", "valid").find((f) => f.name === "minimal.json")?.document,
    ) as Record<string, unknown>;

  it("rejects the reserved writer operation with its own detail", () => {
    const doc = minimal();
    doc["operation"] = "propose_patch";
    expect(() => validateRequest(doc)).toThrowError(ShuntError);
    try {
      validateRequest(doc);
    } catch (err) {
      expect((err as ShuntError).detail).toBe("WRITER_OPERATION_UNSUPPORTED");
    }
  });

  it("rejects an unknown version with its own code", () => {
    const doc = minimal();
    doc["schema_version"] = "2.0";
    try {
      validateRequest(doc);
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).code).toBe("UNSUPPORTED_VERSION");
    }
  });

  it("rejects a whitespace-only question", () => {
    const doc = minimal();
    doc["question"] = "   ";
    expect(() => validateRequest(doc)).toThrowError(ShuntError);
  });

  it("pins the status/code table to the schema enums", () => {
    const pairs = statusCodePairs();
    const schema = envelopeValidator().schema as any;
    expect(new Set(pairs.codes)).toEqual(new Set(schema.properties.code.enum));
    expect(new Set(pairs.statuses)).toEqual(new Set(schema.properties.status.enum));
    for (const [status, codes] of Object.entries(pairs.pairs)) {
      for (const code of codes) expect(legalPair(status, code)).toBe(true);
    }
  });

  it("matches the shared limits contract", () => {
    expect(DEFAULT_LIMITS.fullReadMaxLines).toBe(350);
    expect(DEFAULT_LIMITS.readerModel).toBe("gpt-5.6-luna");
    expect(DEFAULT_LIMITS.maxEnvelopeBytes).toBe(16384);
  });
});
