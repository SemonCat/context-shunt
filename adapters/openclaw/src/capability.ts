import {
  type CapabilityReport,
  type DisabledReason,
  EMITTED_SCHEMA_VERSION,
  READER_MODEL,
  supported,
  unsupported,
} from "@context-shunt/core";

export const ADAPTER = "openclaw";
export const ADAPTER_VERSION = "1.2.1";

/**
 * OpenClaw tool ids this adapter claims to cover. A read tool outside this list is not
 * protected, and the capability report says so rather than implying blanket coverage.
 */
export const READ_TOOLS: Record<string, "read"> = {
  read: "read",
};
export const SEARCH_TOOLS: Record<string, "search"> = {};
export const SHELL_TOOLS: Record<string, "shell"> = {
  exec: "shell",
};

export const TOOL_RESULT_CAPTURE_EVIDENCE: readonly string[] = [
  "OpenClaw 2026.9.3 / 773b6d8: registerAgentToolResultMiddleware with manifest contracts.agentToolResultMiddleware=[openclaw,codex] and explicit plugin enablement",
  "Embedded OpenClaw and OpenClaw-owned Codex dynamic tools: replacement before model delivery when registered; Codex-native PostToolUse is observe-only, replacement unsupported",
  "src/agents/harness/tool-result-middleware.ts: host fails closed on throws/invalid output and preserves delivered-message fallback",
  "Ingress sanitizes before first handler: 200 blocks, 100000 UTF-16 chars per text aggregation, 100000 details bytes, 5000000 image chars; detectable boundaries refused, original completeness unknown even below caps",
  "src/plugins/agent-tool-result-middleware.ts and loader: registry order, no priority; disable Tokenjuice and other result reducers atomically when enabling capture",
  "Snapshots contain deterministic middleware-visible text/JSON only; upstream_truncated=null, never a complete-original claim; explicitly configured read-only tools only",
];

/** Deprecated alias. `suma_post_tool` was never a product name. */
export const SUMA_EVIDENCE = TOOL_RESULT_CAPTURE_EVIDENCE;

export interface ProbeInput {
  /** Hook names the host runtime actually exposes. */
  readonly hooks: readonly string[];
  /** Whether a runtime model bridge is present. */
  readonly hasModelBridge: boolean;
  readonly hostVersion: string;
  readonly hasToolResultMiddleware?: boolean;
  readonly captureEnabled?: boolean;
  /** Set when the host cannot disable provider prompt tracing. */
  readonly unsafeTracing?: boolean;
  /**
   * The reader model this deployment configured. The report states what was *requested*;
   * whether the host actually served it is a provenance question the envelope answers.
   */
  readonly readerModel?: string;
}

export function buildCapabilityReport(input: ProbeInput): CapabilityReport {
  const hooks = new Set(input.hooks);
  const modes = [];

  if (!hooks.has("before_tool_call")) {
    modes.push(unsupported("local_gate", ["HOOK_MISSING"]));
  } else if (input.unsafeTracing) {
    modes.push(unsupported("local_gate", ["UNSAFE_TRACING"]));
  } else {
    modes.push(
      supported("local_gate", [
        "openclaw before_tool_call runs before tool execution and can deny the call",
      ]),
    );
  }

  const readerReasons: DisabledReason[] = [];
  if (!input.hasModelBridge) readerReasons.push("MODEL_UNAVAILABLE");
  if (input.unsafeTracing) readerReasons.push("UNSAFE_TRACING");
  modes.push(
    readerReasons.length > 0
      ? unsupported("reader", readerReasons)
      : supported("reader", [
          `isolated runtime completion requested with model=${input.readerModel ?? READER_MODEL}`,
          "attribution ceiling: the host reports its own post-policy selection, which is a "
            + "routing fact - this adapter reports attribution_status=resolved, never actual",
        ]),
  );

  // Deterministic extraction and stats need no provider at all, so they survive an
  // unavailable model bridge.
  modes.push(
    supported("deterministic_inspect", [
      "no provider reference on the inspect path; zero model calls",
    ]),
  );
  modes.push(supported("session_stats", ["store-backed, session-scoped only"]));

  // Real lifecycle boundaries, so recovery handles survive a compaction rotation.
  modes.push(
    hooks.has("session_end")
      ? supported("session_lifecycle", [
          'openclaw session_end carries a reason enum; "compaction" rotates mid-conversation',
          "handles are revoked only for new/reset/deleted; every other reason relies on TTL",
        ])
      : unsupported("session_lifecycle", ["HOOK_MISSING"], [
          "no session_end hook; handle teardown falls back to TTL",
        ]),
  );

  const captureReasons: DisabledReason[] = [];
  if (!input.hasToolResultMiddleware) captureReasons.push("HOOK_MISSING");
  if (input.hostVersion !== "2026.9.3") captureReasons.push("HOST_VERSION_UNVERIFIED");
  if (input.captureEnabled !== true) captureReasons.push("CONFIG_DISABLED");
  if (input.unsafeTracing) captureReasons.push("UNSAFE_TRACING");
  modes.push(
    captureReasons.length === 0
      ? supported("tool_result_capture", TOOL_RESULT_CAPTURE_EVIDENCE)
      : unsupported("tool_result_capture", captureReasons, [
          ...TOOL_RESULT_CAPTURE_EVIDENCE,
          "Not registered: requires enabled capture, official API and the verified 2026.9.3 contract; revalidate host upgrades",
        ]),
  );

  modes.push(
    unsupported("artifact_import", ["IMPORT_UNIMPLEMENTED"], [
      "the import boundary exists only in the Python core (context_shunt.artifacts); this adapter has nothing to call",
      "not a host limitation: OpenClaw can register the tool, so this becomes supportable without any host change",
    ]),
  );

  return {
    adapter: ADAPTER,
    adapterVersion: ADAPTER_VERSION,
    hostName: "openclaw",
    hostVersion: input.hostVersion || "unknown",
    contractVersion: EMITTED_SCHEMA_VERSION,
    readerModel: input.readerModel ?? READER_MODEL,
    toolsCovered: [
      ...Object.keys(READ_TOOLS),
      ...Object.keys(SEARCH_TOOLS),
      ...Object.keys(SHELL_TOOLS),
    ].sort(),
    modes,
    testedFixtureId: "contracts/v1/conformance/gate-cases.json",
  };
}
