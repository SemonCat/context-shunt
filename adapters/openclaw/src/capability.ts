/**
 * OpenClaw capability probe.
 *
 * The local gate and the reader are supported: `before_tool_call` runs before the tool
 * executes and can return a deny decision, and the runtime model bridge can be pinned to
 * `gpt-5.6-luna`.
 *
 * The optional Suma post-tool mode is **not** supported on this host, and the reasons are
 * structural rather than a matter of effort:
 *
 * 1. `CAPTURE_AFTER_TRUNCATION` - in OpenClaw the persistence guard caps the tool result
 *    *before* the plugin hook runs. In `src/agents/session-tool-result-guard.ts` the
 *    sequence is `capToolResultForPersistence(...)` and only then `persistToolResult(...)`,
 *    which is what invokes `tool_result_persist`. A plugin therefore receives content that
 *    has already been truncated, so "complete capture before truncation" is unprovable.
 * 2. `OBSERVE_ONLY_HOOK` - `after_tool_call` can see the result but cannot replace it
 *    (documented as "Observe" in `docs/plugins/hooks.md`), so the one hook positioned
 *    early enough cannot perform the safe replacement.
 * 3. `HOST_FAIL_OPEN` - the synchronous result hooks are documented as fail-open: a
 *    handler that throws is logged and its result ignored, leaving the original in place.
 *
 * Because the required order cannot be shown, the mode is reported `unsupported` and stays
 * off. `HOST_UNSAFE` is what a caller gets if it tries to force it on.
 */
import {
  type CapabilityReport,
  type DisabledReason,
  EMITTED_SCHEMA_VERSION,
  READER_MODEL,
  supported,
  unsupported,
} from "@context-shunt/core";

export const ADAPTER = "openclaw";
export const ADAPTER_VERSION = "1.1.0";

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

export const SUMA_EVIDENCE: readonly string[] = [
  "openclaw src/agents/session-tool-result-guard.ts: capToolResultForPersistence() runs before persistToolResult(), so tool_result_persist sees post-truncation content",
  "openclaw docs/plugins/hooks.md: after_tool_call is documented as observe-only and cannot replace a result",
  "openclaw docs/plugins/hooks.md: tool_result_persist / before_message_write are synchronous and fail-open - a failed result is ignored",
];

export interface ProbeInput {
  /** Hook names the host runtime actually exposes. */
  readonly hooks: readonly string[];
  /** Whether a runtime model bridge is present. */
  readonly hasModelBridge: boolean;
  readonly hostVersion: string;
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

  modes.push(
    unsupported(
      "suma_post_tool",
      ["CAPTURE_AFTER_TRUNCATION", "OBSERVE_ONLY_HOOK", "HOST_FAIL_OPEN"],
      SUMA_EVIDENCE,
    ),
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
