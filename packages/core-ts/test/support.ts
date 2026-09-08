import { mkdirSync } from "node:fs";

import { CapabilityReport, supported, unsupported } from "../src/capability.js";
import { Config, loadConfig } from "../src/config.js";
import { DEFAULT_LIMITS, EMITTED_SCHEMA_VERSION, Limits, READER_MODEL } from "../src/limits.js";
import {
  type Attribution,
  type AttributionPolicy,
  type Confidence,
  type Provenance,
} from "../src/provenance.js";
import {
  type ModelResponse,
  type ProviderTarget,
  type ReaderProvider,
} from "../src/provider.js";
import { SourceRegistry } from "../src/registry.js";
import { ScopeIdentity, SnapshotStore } from "../src/store.js";
import { ShuntError } from "../src/errors.js";

export interface RecordedCall {
  system: string;
  user: string;
  model: string;
  maxOutputTokens: number;
  timeoutMs: number;
}

export interface FakeLunaOptions {
  /** Report the model back, so attribution has something to classify. */
  reportModel?: boolean;
  /** Claim the reported value came from the provider, not from an echo. */
  confirmsGeneration?: boolean;
  /** Report exact token usage. When false, usage is unknown and stays unknown. */
  usageExact?: boolean;
  provider?: string;
}

/**
 * Records every call so gates can assert model, question propagation and counts.
 *
 * By default it behaves like a host that *can* prove attribution: it reports the model it
 * was asked for and confirms the report came from the provider. Tests that need the
 * weaker, more common case pass `confirmsGeneration: false` or `reportModel: false`.
 */
export class FakeLuna implements ReaderProvider {
  readonly calls: RecordedCall[] = [];
  private readonly reportModel: boolean;
  private readonly confirmsGeneration: boolean;
  private readonly usageExact: boolean;
  private readonly provider: string;

  constructor(
    private readonly replies: Array<
      string | ShuntError | ((user: string) => string | ShuntError)
    > = [],
    private readonly defaultReply: string | ShuntError | ((user: string) => string | ShuntError) =
      JSON.stringify({ answer: "", citations: [] }),
    readonly model: string = READER_MODEL,
    options: FakeLunaOptions = {},
  ) {
    this.reportModel = options.reportModel ?? true;
    this.confirmsGeneration = options.confirmsGeneration ?? true;
    this.usageExact = options.usageExact ?? true;
    this.provider = options.provider ?? "openai";
  }

  get target(): ProviderTarget {
    return { model: this.model, provider: this.provider };
  }

  get callCount(): number {
    return this.calls.length;
  }

  async complete(opts: {
    system: string;
    user: string;
    maxOutputTokens: number;
    timeoutMs: number;
  }): Promise<ModelResponse> {
    this.calls.push({ ...opts, model: this.model });
    let reply = this.replies.length > 0 ? (this.replies.shift() as never) : this.defaultReply;
    if (typeof reply === "function") {
      reply = (reply as (user: string) => string | ShuntError)(opts.user) as never;
    }
    if (reply instanceof ShuntError) throw reply;
    const identity = { provider: this.provider, model: this.model };
    return {
      text: String(reply),
      requested: identity,
      resolved: identity,
      reported: this.reportModel ? identity : {},
      providerConfirmsGeneration: this.confirmsGeneration && this.reportModel,
      usage: this.usageExact
        ? { inputTokens: 10, outputTokens: 5, method: "exact" as const }
        : { method: "unknown" as const },
      fallbackUsed: false,
    };
  }
}

/** The legacy reply shape: prose the model marks up itself with `[cN]`. */
export function answerJson(answer: string, citations: unknown[]): string {
  return JSON.stringify({ answer, citations });
}

/**
 * The current reply shape: structured claims plus the citations they reference. `claims`
 * items are `{text: string, citation_ids: string[]}`; markers are never written by the
 * caller here either - the reader places them, mechanically, from `citation_ids`.
 */
export function claimsJson(claims: unknown[], citations: unknown[]): string {
  return JSON.stringify({ claims, citations });
}

export function makeConfig(tmpDir: string, overrides: Record<string, unknown> = {}): Config {
  mkdirSync(`${tmpDir}/ws`, { recursive: true });
  return loadConfig(
    { workspace_roots: [`${tmpDir}/ws`], cache_dir: `${tmpDir}/cache`, ...overrides },
    `${tmpDir}/cache`,
  );
}

export function makeIdentity(sessionId = "sess-1", generation = 1): ScopeIdentity {
  return new ScopeIdentity({
    host: "test-host",
    profile: "test",
    principal: "local",
    session: sessionId,
    generation,
  });
}

export function makeStore(tmpDir: string, limits: Limits = DEFAULT_LIMITS): SnapshotStore {
  return new SnapshotStore(`${tmpDir}/cache`, limits);
}

export function makeRegistry(
  tmpDir: string,
  opts: { sessionId?: string; generation?: number; limits?: Limits } = {},
): SourceRegistry {
  const limits = opts.limits ?? DEFAULT_LIMITS;
  const store = makeStore(tmpDir, limits);
  const identity = makeIdentity(opts.sessionId ?? "sess-1", opts.generation ?? 1);
  store.openScope(identity);
  return new SourceRegistry(store, identity, limits);
}

/**
 * Provenance for a hand-built model-derived envelope in a test. Defaults to the strongest
 * honest case (a provider-confirmed match), so a test that cares about a weaker attribution
 * has to say so explicitly.
 */
export function derivedProvenance(overrides: Partial<Provenance> = {}): Provenance {
  const identity = { provider: "openai", model: READER_MODEL };
  return {
    derived: true,
    label: "model_generated_answer",
    attributionStatus: "actual" as Attribution,
    attributionConfidence: "high" as Confidence,
    attributionPolicy: "allow_unverified" as AttributionPolicy,
    attemptsStarted: 1,
    usageComplete: true,
    citationsMechanicallyVerified: true,
    requested: identity,
    resolved: identity,
    reported: identity,
    ...overrides,
  };
}

export function makeCapability(suma = false): CapabilityReport {
  return {
    adapter: "test",
    adapterVersion: "1.1.0",
    hostName: "test-host",
    hostVersion: "0.0.0",
    contractVersion: EMITTED_SCHEMA_VERSION,
    readerModel: READER_MODEL,
    toolsCovered: ["read", "search", "shell"],
    modes: [
      supported("local_gate"),
      supported("reader"),
      suma ? supported("suma_post_tool") : unsupported("suma_post_tool", ["ORDERING_UNPROVEN"]),
    ],
    testedFixtureId: "gate-cases.json",
  };
}
