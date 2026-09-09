/** unit contract (TypeScript core) - the schemas are the cross-language boundary. */
import { describe, expect, it } from "vitest";

import { loadConfig } from "../src/config.js";
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

  it.each([
    ["reader", { enabld: true }],
    ["inspect", { enabld: true }],
    ["stats", { enabld: true }],
    ["tool_result_capture", { enabld: true }],
    ["suma_post_tool", { enabld: true }], // deprecated alias, still validated
    ["writer", { enabld: true }],
  ])("rejects unknown keys in the %s config section", (section, value) => {
    const raw = { workspace_roots: ["/tmp/context-shunt-config"], [section]: value };
    expect(() => loadConfig(raw as never, "/tmp/context-shunt-cache"))
      .toThrowError(ShuntError);
    try {
      loadConfig(raw as never, "/tmp/context-shunt-cache");
    } catch (err) {
      expect((err as ShuntError).detail).toBe("BAD_CONFIGURATION");
    }
  });

  it.each([
    { limits: { store_busy_timeout_mss: 1000 } },
    { store: { busy_timeout_ms: 1000 } },
    { accounting: { max_stats_pages: 4 } },
  ])("rejects an unknown limit or top-level section", (extra) => {
    expect(() => loadConfig(
      { workspace_roots: ["/tmp/context-shunt-config"], ...extra } as never,
      "/tmp/context-shunt-cache",
    )).toThrowError(ShuntError);
  });
});

describe("tool_result_capture / suma_post_tool config migration", () => {
  const roots = { workspace_roots: ["/tmp/context-shunt-config"] };

  it("accepts the deprecated suma_post_tool alias", () => {
    const config = loadConfig(
      { ...roots, suma_post_tool: { enabled: true } } as never,
      "/tmp/context-shunt-cache",
    );
    expect(config.toolResultCaptureEnabled).toBe(true);
    expect(config.sumaPostToolEnabled).toBe(true);
  });

  it("accepts the canonical tool_result_capture key when the alias is absent", () => {
    const config = loadConfig(
      { ...roots, tool_result_capture: { enabled: true } } as never,
      "/tmp/context-shunt-cache",
    );
    expect(config.toolResultCaptureEnabled).toBe(true);
  });

  it("accepts an agreeing alias and canonical key", () => {
    const config = loadConfig(
      {
        ...roots,
        tool_result_capture: { enabled: true },
        suma_post_tool: { enabled: true },
      } as never,
      "/tmp/context-shunt-cache",
    );
    expect(config.toolResultCaptureEnabled).toBe(true);
  });

  it("refuses a conflicting alias and canonical key", () => {
    expect(() => loadConfig(
      {
        ...roots,
        tool_result_capture: { enabled: true },
        suma_post_tool: { enabled: false },
      } as never,
      "/tmp/context-shunt-cache",
    )).toThrowError(ShuntError);
    try {
      loadConfig(
        {
          ...roots,
          tool_result_capture: { enabled: true },
          suma_post_tool: { enabled: false },
        } as never,
        "/tmp/context-shunt-cache",
      );
    } catch (err) {
      expect((err as ShuntError).detail).toBe("TOOL_RESULT_CAPTURE_CONFIG_CONFLICT");
    }
  });

  it("defaults the host-ordering attestation to false", () => {
    const config = loadConfig(roots as never, "/tmp/context-shunt-cache");
    expect(config.toolResultCaptureHostOrderingVerifiedLocally).toBe(false);
  });

  it("carries an explicit host-ordering attestation through", () => {
    const config = loadConfig(
      {
        ...roots,
        tool_result_capture: { enabled: true, host_ordering_verified_locally: true },
      } as never,
      "/tmp/context-shunt-cache",
    );
    expect(config.toolResultCaptureHostOrderingVerifiedLocally).toBe(true);
  });
});
