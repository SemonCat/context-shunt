/**
 * In-host reader bridge server: role-preserving model access for the Luna gates.
 *
 * Production-equivalent model dispatch for the OpenClaw adapter. It constructs the same
 * host-owned `runtime.llm.complete` facade the plugin API supplies and invokes the same
 * `execution.mode: "isolated-agent-runtime"` request shape as `adapters/openclaw/index.ts`.
 *
 * Why this exists
 * ---------------
 * The CLI bridge (`openclaw_cli.py`) drives `openclaw infer model run`, whose only input
 * is `--prompt`. It therefore concatenates the reader's system prompt and user message
 * into one user turn. The production adapter does not: `adapters/openclaw/index.ts` calls
 * `api.runtime.llm.complete({ messages: [{role:"user",...}], systemPrompt, ... })`, and the
 * host's own isolated-runtime policy *requires* that shape - `runtime-llm-isolated.ts`
 * rejects anything else with "pass system instructions through systemPrompt".
 *
 * A role-less prompt is a different prompt, and a different prompt can move a result in
 * either direction - so the CLI bridge cannot be used to claim a release-representative
 * score, and equally cannot be read as a pessimistic one. This server restores the role
 * separation and the shipped runtime path.
 *
 * What it matches
 * ---------------
 * Matched to the production adapter, deliberately and verifiably:
 *   - separate system role (`context.systemPrompt`) and a single user message;
 *   - the reader's own output cap (`maxTokens`), not the model's catalogue maximum;
 *   - `temperature: 0`;
 *   - no reasoning/thinking level, because the adapter passes none. The CLI bridge pinned
 *     `--thinking low`, which is a second deviation from production, not a neutral choice.
 *
 * `createRuntimeLlm` is the owner of `api.runtime.llm.complete`; its isolated branch calls
 * `runIsolatedAgentRuntimeCompletion`, which in turn calls `runIsolatedCompletion`. The
 * bridge supplies a dedicated plugin caller id, a closed model allowlist, a bound agent,
 * and the same purpose, temperature, role split, timeout and output cap as the adapter. A
 * dedicated caller keeps this isolated evaluation independent of any disabled/stale plugin
 * entry in the local host config. It does not enable or load the plugin, open a
 * conversation, or register tools.
 *
 * Protocol: newline-delimited JSON on stdin/stdout. Startup (config + catalogue load) costs
 * ~20s, so the caller spawns this once and keeps it, rather than paying it 120 times.
 *
 * It reads no secret value: OpenClaw's command-scoped resolver materializes only registered
 * model-provider references into the in-memory config supplied back to OpenClaw's runtime.
 * This server never inspects, logs, echoes or writes those values.
 */
import { createInterface } from "node:readline";

const HOST = process.env["CONTEXT_SHUNT_OPENCLAW_ROOT"];
if (!HOST) {
  process.stderr.write("CONTEXT_SHUNT_OPENCLAW_ROOT is required\n");
  process.exit(2);
}

const { loadConfig } = await import(`${HOST}/src/config/io.runtime.js`);
const { resolveCommandConfigWithSecrets } =
  await import(`${HOST}/src/cli/command-config-resolution.js`);
const { getModelsCommandSecretTargetIds } =
  await import(`${HOST}/src/cli/command-secret-targets.js`);
const { createRuntimeLlm } =
  await import(`${HOST}/src/plugins/runtime/runtime-llm.runtime.js`);

const authoredCfg = await loadConfig();
const { resolvedConfig: cfg } = await resolveCommandConfigWithSecrets({
  config: authoredCfg,
  commandName: "context-shunt isolated reader evaluation",
  targetIds: getModelsCommandSecretTargetIds(),
  autoEnable: false,
});
const AGENT = process.env["CONTEXT_SHUNT_OPENCLAW_AGENT"] || "main";
const ROUTE = process.env["CONTEXT_SHUNT_OPENCLAW_ROUTE"] || "sub2api-openai/gpt-5.6-luna";
const routeSlash = ROUTE.indexOf("/");
if (routeSlash <= 0 || routeSlash === ROUTE.length - 1) {
  process.stderr.write("CONTEXT_SHUNT_OPENCLAW_ROUTE must be <provider>/<model>\n");
  process.exit(2);
}

const llm = createRuntimeLlm({
  getConfig: () => cfg,
  authority: {
    caller: { kind: "plugin", id: "context-shunt-eval" },
    agentId: AGENT,
    requiresBoundAgent: true,
    allowComplete: true,
    allowModelOverride: true,
    allowedModels: [ROUTE],
    allowedCompletionModels: [ROUTE],
  },
});

/** Requested identity at startup; resolved identity is reported by every real call. */
const identity = {
  transport: "runtime.llm.complete/isolated-agent-runtime",
  agent: AGENT,
  requested_route: ROUTE,
  host_version: (cfg as { version?: unknown })?.version ?? null,
};
process.stdout.write(JSON.stringify({ ready: true, identity }) + "\n");

const rl = createInterface({ input: process.stdin });

function errorChain(error: unknown): string[] {
  const chain: string[] = [];
  let current: unknown = error;
  for (let depth = 0; depth < 6 && current && typeof current === "object"; depth += 1) {
    const item = current as { code?: unknown; name?: unknown; cause?: unknown };
    const kind = typeof item.code === "string" ? item.code : item.name;
    if (typeof kind === "string" && kind) chain.push(kind.slice(0, 64));
    current = item.cause;
  }
  return chain;
}

async function handle(line: string): Promise<void> {
  if (!line.trim()) return;
  let req: {
    id: number;
    system: string;
    user: string;
    max_output_tokens: number;
    timeout_ms: number;
    deadline_unix_ms: number;
  };
  try {
    req = JSON.parse(line);
  } catch {
    return;
  }
  const started = Date.now();
  try {
    const remainingMs = Math.min(req.timeout_ms, req.deadline_unix_ms - Date.now());
    if (!Number.isFinite(remainingMs) || remainingMs <= 0) {
      throw Object.assign(new Error("expired before provider dispatch"), {
        code: "DEADLINE_EXPIRED",
      });
    }
    const result = await llm.complete({
      messages: [{ role: "user", content: req.user }],
      systemPrompt: req.system,
      model: ROUTE,
      maxTokens: req.max_output_tokens,
      temperature: 0,
      purpose: "context-shunt-reader",
      execution: { mode: "isolated-agent-runtime", timeoutMs: Math.max(1, remainingMs) },
    });
    process.stdout.write(
      JSON.stringify({
        id: req.id,
        ok: true,
        text: result.text,
        elapsed_ms: Date.now() - started,
        ...identity,
        resolved_provider: result.provider,
        resolved_model: result.model,
        execution: result.execution,
        usage: result.usage ?? null,
      }) + "\n",
    );
  } catch (error) {
    // The host's message can quote the prompt back, so only a bounded type crosses.
    const code = (error as { code?: unknown })?.code;
    process.stdout.write(
      JSON.stringify({
        id: req.id,
        ok: false,
        error_kind: String(code ?? "UNKNOWN").slice(0, 64),
        error_chain: errorChain(error),
        elapsed_ms: Date.now() - started,
      }) + "\n",
    );
  }
}

for await (const line of rl) {
  // The reader deliberately plans more than one bounded chunk at a time. Keep those
  // physical calls concurrent, as the production adapter/runtime is, and let the request
  // id correlate out-of-order responses on the Python side.
  void handle(line);
}
