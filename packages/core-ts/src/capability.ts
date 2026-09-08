/**
 * Capability detection and the startup report.
 *
 * A mode is enabled only when the adapter can *prove* the host gives it what the mode
 * needs. Where the proof does not exist the mode is reported `unsupported` with a fixed
 * reason and stays off - never faked, never "probably fine".
 *
 * For the optional Suma post-tool mode the required proof is two-part: complete capture of
 * the result *before* any host truncation, and safe replacement *before* persistence and
 * context insertion. See `docs/capability-matrix.md` for the file-and-line evidence behind
 * each reason.
 */
export type Support = "supported" | "unsupported" | "disabled_by_config";

export type DisabledReason =
  | "HOOK_MISSING"
  | "CAPTURE_AFTER_TRUNCATION"
  | "REPLACEMENT_AFTER_PERSISTENCE"
  | "OBSERVE_ONLY_HOOK"
  | "HOST_FAIL_OPEN"
  | "ORDERING_UNPROVEN"
  | "MODEL_UNAVAILABLE"
  | "UNSAFE_TRACING"
  | "HOST_VERSION_UNVERIFIED"
  | "CONFIG_DISABLED"
  /** The adapter's language core has no implementation of the mode - not a host limit. */
  | "IMPORT_UNIMPLEMENTED";

export interface ModeCapability {
  readonly mode: string;
  readonly support: Support;
  readonly reasons: readonly DisabledReason[];
  readonly evidence: readonly string[];
}

export function supported(mode: string, evidence: readonly string[] = []): ModeCapability {
  return { mode, support: "supported", reasons: [], evidence };
}

export function unsupported(
  mode: string,
  reasons: readonly DisabledReason[],
  evidence: readonly string[] = [],
): ModeCapability {
  return { mode, support: "unsupported", reasons, evidence };
}

export function disabledByConfig(mode: string): ModeCapability {
  return { mode, support: "disabled_by_config", reasons: ["CONFIG_DISABLED"], evidence: [] };
}

export interface CapabilityReport {
  readonly adapter: string;
  readonly adapterVersion: string;
  readonly hostName: string;
  readonly hostVersion: string;
  readonly contractVersion: string;
  readonly readerModel: string;
  readonly toolsCovered: readonly string[];
  readonly modes: readonly ModeCapability[];
  readonly testedFixtureId: string;
}

export function modeEnabled(report: CapabilityReport, mode: string): boolean {
  return report.modes.some((m) => m.mode === mode && m.support === "supported");
}

export function reportToJson(report: CapabilityReport): Record<string, unknown> {
  return {
    adapter: report.adapter,
    adapter_version: report.adapterVersion,
    host: { name: report.hostName, version: report.hostVersion },
    contract_version: report.contractVersion,
    reader_model: report.readerModel,
    tools_covered: [...report.toolsCovered],
    modes: report.modes.map((m) => ({
      mode: m.mode,
      support: m.support,
      enabled: m.support === "supported",
      reasons: [...m.reasons],
      evidence: [...m.evidence],
    })),
    tested_fixture_id: report.testedFixtureId,
  };
}
