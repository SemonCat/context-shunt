/* Current bundled OpenClaw JSONL bridge. Startup resolves SecretRefs in memory and builds
 * the same isolated-agent runtime facade as the production adapter; no completion occurs
 * until a JSONL request arrives. It is selected by CONTEXT_SHUNT_OPENCLAW_SERVER and run
 * with node via CONTEXT_SHUNT_OPENCLAW_TSX. */
import { createInterface } from "node:readline";
import { i as loadConfig } from "/Users/edisonpve/openclaw/dist/io.runtime-HpEGYkfh.mjs";
import { resolveCommandConfigWithSecrets } from "/Users/edisonpve/openclaw/dist/command-config-resolution.runtime-DodkAM9v.mjs";
import { getModelsCommandSecretTargetIds } from "/Users/edisonpve/openclaw/dist/command-secret-targets-BjpvqUh9.mjs";
import { createRuntimeLlm } from "/Users/edisonpve/openclaw/dist/runtime-llm.runtime-D2BA8WPA.mjs";
import { resolveModelCostConfig, resolveModelCostConfigFingerprint } from "/Users/edisonpve/openclaw/dist/usage-format-B1TX1JUa.mjs";

const route = process.env.CONTEXT_SHUNT_OPENCLAW_ROUTE || "sub2api-openai/gpt-5.6-luna";
const slash = route.indexOf("/");
if (slash <= 0 || slash === route.length - 1) throw new Error("invalid fixed provider/model route");
const providerName = route.slice(0, slash);
const modelName = route.slice(slash + 1);
const authored = loadConfig();
const { resolvedConfig: cfg } = await resolveCommandConfigWithSecrets({
  config: authored,
  commandName: "context-shunt fixed-corpus reader",
  targetIds: getModelsCommandSecretTargetIds(),
  autoEnable: false,
});
const provider = cfg.models?.providers?.[providerName];
if (!provider || typeof provider.apiKey !== "string") throw new Error("resolved provider is incomplete");
const cost = resolveModelCostConfig({ provider: providerName, model: modelName, config: cfg });
if (!cost) throw new Error("verified route pricing is unavailable");
const pricing = {
  route, currency: "USD", unit: "per_million_tokens", source: "openclaw.resolveModelCostConfig",
  fingerprint: resolveModelCostConfigFingerprint(cfg),
  rates: { input: cost.input, output: cost.output, cacheRead: cost.cacheRead, cacheWrite: cost.cacheWrite },
  tieredPricing: (cost.tieredPricing ?? []).map((tier) => ({ range: tier.range, cost: { input: tier.input, output: tier.output, cacheRead: tier.cacheRead, cacheWrite: tier.cacheWrite } })),
};
const AGENT = process.env.CONTEXT_SHUNT_OPENCLAW_AGENT || "main";
const llm = createRuntimeLlm({
  getConfig: () => cfg,
  authority: { caller: { kind: "plugin", id: "context-shunt-eval" }, agentId: AGENT, requiresBoundAgent: true, allowComplete: true, allowModelOverride: true, allowedModels: [route], allowedCompletionModels: [route] },
});
const identity = { transport: "runtime.llm.complete/isolated-agent-runtime", agent: AGENT, requested_route: route, host_version: cfg.version ?? null, pricing };
process.stdout.write(JSON.stringify({ ready: true, identity }) + "\n");

function errorChain(error) {
  const chain = [];
  let current = error;
  for (let depth = 0; depth < 6 && current && typeof current === "object"; depth += 1) {
    const kind = typeof current.code === "string" ? current.code : current.name;
    if (typeof kind === "string" && kind) chain.push(kind.slice(0, 64));
    current = current.cause;
  }
  return chain;
}

async function handle(line) {
  if (!line.trim()) return;
  let req;
  try { req = JSON.parse(line); } catch { return; }
  const started = Date.now();
  try {
    if (!req || typeof req !== "object" || typeof req.id !== "number" || typeof req.system !== "string" || typeof req.user !== "string" || !Number.isInteger(req.max_output_tokens) || req.max_output_tokens < 1 || req.max_output_tokens > 2048 || !Number.isInteger(req.timeout_ms) || req.timeout_ms < 1 || req.timeout_ms > 60000 || !Number.isFinite(req.deadline_unix_ms)) throw Object.assign(new Error("invalid bounded request"), { code: "INVALID_REQUEST" });
    const remainingMs = Math.min(req.timeout_ms, req.deadline_unix_ms - Date.now());
    if (remainingMs <= 0) throw Object.assign(new Error("expired before provider dispatch"), { code: "DEADLINE_EXPIRED" });
    const result = await llm.complete({ messages: [{ role: "user", content: req.user }], systemPrompt: req.system, model: route, maxTokens: req.max_output_tokens, temperature: 0, purpose: "context-shunt-reader", execution: { mode: "isolated-agent-runtime", timeoutMs: Math.max(1, remainingMs) } });
    process.stdout.write(JSON.stringify({ id: req.id, ok: true, text: result.text, elapsed_ms: Date.now() - started, ...identity, resolved_provider: result.provider, resolved_model: result.model, execution: result.execution, usage: result.usage ?? null }) + "\n");
  } catch (error) {
    process.stdout.write(JSON.stringify({ id: req?.id ?? null, ok: false, error_kind: String(error?.code ?? "UNKNOWN").slice(0, 64), error_chain: errorChain(error), elapsed_ms: Date.now() - started }) + "\n");
  }
}
const rl = createInterface({ input: process.stdin });
for await (const line of rl) void handle(line);
