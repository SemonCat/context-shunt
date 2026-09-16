/** unit contract (TypeScript core) - the schemas are the cross-language boundary. */
import { describe, expect, it } from "vitest";

import { loadConfig } from "../src/config.js";
import { ShuntError } from "../src/errors.js";
import {
  DEFAULT_LIMITS, EMITTED_SCHEMA_VERSION, statusCodePairs, legalPair,
} from "../src/limits.js";
import {
  envelopeValidator, requestValidator, validateRequest, validateToolArgs,
} from "../src/schema.js";
import { fixtureDocs } from "./fixtures.js";

describe("request fixtures", () => {
  for (const { name, document } of fixtureDocs("request", "valid")) {
    it(`accepts ${name}`, () => expect(requestValidator()(document)).toBe(true));
  }
  for (const { name, document } of fixtureDocs("request", "invalid")) {
    it(`rejects ${name}`, () => expect(requestValidator()(document)).toBe(false));
  }
});

describe("public tool arguments", () => {
  it("accepts the bounded aggregate selector and rejects an over-wide grouping", () => {
    const args = {
      tool: "context_shunt_inspect",
      source_id: "src_abcd",
      snapshot_id: `sha256:${"a".repeat(64)}`,
      selector: {
        kind: "aggregate",
        records_pointer: "/data/result",
        expand_pointer: "/values",
        record_pointer: "/1",
        parse_json: true,
        filter: { pointer: "/level", equals: "error" },
        distinct: ["/trace_id"],
        group_by: ["/service"],
      },
    };
    expect(validateToolArgs(args)).toBe(args);

    const invalid = structuredClone(args);
    invalid.selector.group_by = ["/a", "/b", "/c", "/d", "/e"];
    expect(() => validateToolArgs(invalid)).toThrowError(ShuntError);
    try {
      validateToolArgs(invalid);
    } catch (err) {
      expect((err as ShuntError).detail).toBe("TOOL_ARGS_VIOLATION");
    }
  });
});

describe("envelope fixtures", () => {
  for (const { name, document } of fixtureDocs("envelope", "valid")) {
    it(`accepts ${name}`, () => expect(envelopeValidator()(document)).toBe(true));
  }
  for (const { name, document } of fixtureDocs("envelope", "invalid")) {
    it(`rejects ${name}`, () => expect(envelopeValidator()(document)).toBe(false));
  }

  it("requires contract 1.2 for the raw artifact locator", () => {
    const doc = structuredClone(
      fixtureDocs("envelope", "valid")
        .find((f) => f.name === "v11_partial_legacy_compacted.json")?.document,
    ) as Record<string, any>;
    doc["legacy_compaction"]["raw_artifact_path"] =
      `/private/cache/artifacts/scp_${"0".repeat(32)}`
      + `/src_0123456789abcdef.${"1".repeat(32)}.txt`;
    expect(envelopeValidator()(doc)).toBe(false);
    doc["schema_version"] = "1.2";
    expect(envelopeValidator()(doc)).toBe(true);
    doc["schema_version"] = "1.3";
    doc["provenance"]["attempts_usage_complete"] = 0;
    expect(envelopeValidator()(doc)).toBe(true);
    for (const unsafe of [
      "../../sensitive.txt",
      "/private/../sensitive.txt",
      `/private/cache/artifacts/scp_${"0".repeat(32)}/source.txt\n`,
    ]) {
      doc["legacy_compaction"]["raw_artifact_path"] = unsafe;
      expect(envelopeValidator()(doc)).toBe(false);
    }
  });

  it("requires envelope 1.3 for the new provenance fields", () => {
    const doc = structuredClone(
      fixtureDocs("envelope", "valid")
        .find((f) => f.name === "v11_ok_answered_derived.json")?.document,
    ) as Record<string, any>;
    doc["provenance"]["attempts_usage_complete"] = 1;
    expect(envelopeValidator()(doc)).toBe(false);
    doc["schema_version"] = "1.3";
    expect(envelopeValidator()(doc)).toBe(true);
    expect(EMITTED_SCHEMA_VERSION).toBe("1.3");
  });

  it("requires zero attempts when a complete answer is reused from cache", () => {
    const doc = structuredClone(
      fixtureDocs("envelope", "valid")
        .find((f) => f.name === "v11_ok_answered_derived.json")?.document,
    ) as Record<string, any>;
    doc["schema_version"] = "1.3";
    doc["provenance"]["attempts_usage_complete"] = 1;
    doc["provenance"]["cache_reused"] = true;
    expect(envelopeValidator()(doc)).toBe(false);
    doc["provenance"]["attempts_started"] = 0;
    doc["provenance"]["attempts_usage_complete"] = 0;
    expect(envelopeValidator()(doc)).toBe(true);
  });

  it("requires envelope 1.3 for aggregate scan counters", () => {
    const doc = structuredClone(
      fixtureDocs("envelope", "valid")
        .find((f) => f.name === "v11_ok_extracted.json")?.document,
    ) as Record<string, any>;
    doc["extraction"]["records_scanned"] = 4;
    doc["extraction"]["records_matched"] = 2;
    expect(envelopeValidator()(doc)).toBe(false);
    doc["schema_version"] = "1.3";
    doc["provenance"]["attempts_usage_complete"] = 0;
    doc["extraction"]["mode"] = "aggregate";
    doc["extraction"]["segments"][0]["kind"] = "aggregate";
    expect(envelopeValidator()(doc)).toBe(true);
  });

  it("binds aggregate counters and segments to aggregate mode", () => {
    const doc = structuredClone(
      fixtureDocs("envelope", "valid")
        .find((f) => f.name === "v11_ok_extracted.json")?.document,
    ) as Record<string, any>;
    doc["schema_version"] = "1.3";
    doc["provenance"]["attempts_usage_complete"] = 0;
    doc["extraction"]["mode"] = "aggregate";
    doc["extraction"]["records_scanned"] = 4;
    doc["extraction"]["records_matched"] = 2;
    expect(envelopeValidator()(doc)).toBe(false);
    doc["extraction"]["segments"][0]["kind"] = "aggregate";
    expect(envelopeValidator()(doc)).toBe(true);
    doc["extraction"]["mode"] = "lines";
    expect(envelopeValidator()(doc)).toBe(false);
  });
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

  it("defaults both Hermes host attestations to false", () => {
    const config = loadConfig(roots as never, "/tmp/context-shunt-cache");
    expect(config.toolResultCaptureHostOrderingVerifiedLocally).toBe(false);
    expect(config.toolResultCaptureHostConsumerScopeVerifiedLocally).toBe(false);
  });

  it("carries explicit Hermes host attestations through", () => {
    const config = loadConfig(
      {
        ...roots,
        tool_result_capture: {
          enabled: true,
          host_ordering_verified_locally: true,
          host_consumer_scope_verified_locally: true,
        },
      } as never,
      "/tmp/context-shunt-cache",
    );
    expect(config.toolResultCaptureHostOrderingVerifiedLocally).toBe(true);
    expect(config.toolResultCaptureHostConsumerScopeVerifiedLocally).toBe(true);
  });
});

describe("mandatory legacy compaction config migration", () => {
  const roots = { workspace_roots: ["/tmp/context-shunt-config"] };

  it.each([false, true])("accepts and ignores the deprecated on/off key (%s)", (enabled) => {
    const config = loadConfig(
      { ...roots, reader: { legacy_compaction: enabled } } as never,
      "/tmp/context-shunt-cache",
    );
    expect(config.readerLegacyCompactionMaxChars).toBe(16_000);
  });

  it("carries the legacy compactor character ceiling into typed config", () => {
    const config = loadConfig(
      { ...roots, reader: { legacy_compaction_max_chars: 12_345 } } as never,
      "/tmp/context-shunt-cache",
    );
    expect(config.readerLegacyCompactionMaxChars).toBe(12_345);
  });

  it.each([999, 60_001, 12.5, "16000"])("rejects an invalid legacy character ceiling (%s)", (value) => {
    expect(() => loadConfig(
      { ...roots, reader: { legacy_compaction_max_chars: value } } as never,
      "/tmp/context-shunt-cache",
    )).toThrowError(ShuntError);
  });
});
