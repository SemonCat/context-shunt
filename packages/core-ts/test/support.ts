import { CapabilityReport, supported, unsupported } from "../src/capability.js";
import { Config, loadConfig } from "../src/config.js";
import { READER_MODEL } from "../src/limits.js";
import { LunaProvider, ModelResponse } from "../src/provider.js";
import { ShuntError } from "../src/errors.js";

export interface RecordedCall {
  system: string;
  user: string;
  model: string;
  maxOutputTokens: number;
  timeoutMs: number;
}

/** Records every call so gates can assert model, question propagation and counts. */
export class FakeLuna implements LunaProvider {
  readonly calls: RecordedCall[] = [];

  constructor(
    private readonly replies: Array<string | ShuntError | (() => string | ShuntError)> = [],
    private readonly defaultReply: string | ShuntError | (() => string | ShuntError) =
      JSON.stringify({ answer: "", citations: [] }),
    readonly model: string = READER_MODEL,
  ) {}

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
    if (typeof reply === "function") reply = (reply as () => string | ShuntError)() as never;
    if (reply instanceof ShuntError) throw reply;
    return {
      text: String(reply),
      model: this.model,
      usage: { inputTokens: 10, outputTokens: 5, estimated: false },
    };
  }
}

export function answerJson(answer: string, citations: unknown[]): string {
  return JSON.stringify({ answer, citations });
}

export function makeConfig(tmpDir: string, overrides: Record<string, unknown> = {}): Config {
  return loadConfig(
    { workspace_roots: [`${tmpDir}/ws`], spill_dir: `${tmpDir}/cache`, ...overrides },
    `${tmpDir}/cache`,
  );
}

export function makeCapability(suma = false): CapabilityReport {
  return {
    adapter: "test",
    adapterVersion: "1.0.0",
    hostName: "test-host",
    hostVersion: "0.0.0",
    contractVersion: "1.0",
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
