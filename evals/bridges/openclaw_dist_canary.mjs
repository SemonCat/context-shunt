// Disposable exact-host OpenClaw runtime canary. It resolves the configured SecretRef in
// memory, starts the owned loopback budget relay, and points the host runtime at that relay.
// It emits only redacted identity/result metadata. The caller supplies an isolated state/home.
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { i as loadConfig } from "/Users/edisonpve/openclaw/dist/io.runtime-HpEGYkfh.mjs";
import { resolveCommandConfigWithSecrets } from "/Users/edisonpve/openclaw/dist/command-config-resolution.runtime-DodkAM9v.mjs";
import { getModelsCommandSecretTargetIds } from "/Users/edisonpve/openclaw/dist/command-secret-targets-BjpvqUh9.mjs";
import { resolveModelCostConfig, resolveModelCostConfigFingerprint } from "/Users/edisonpve/openclaw/dist/usage-format-B1TX1JUa.mjs";

const root = process.env.CONTEXT_SHUNT_REPO ?? "/Users/edisonpve/Documents/code/personal/context-shunt";
const route = "sub2api-openai/gpt-5.6-luna";
const authored = loadConfig();
const { resolvedConfig: cfg } = await resolveCommandConfigWithSecrets({
  config: authored, commandName: "context-shunt isolated native canary",
  targetIds: getModelsCommandSecretTargetIds(), autoEnable: false,
});
const provider = cfg.models?.providers?.["sub2api-openai"];
if (!provider || typeof provider.apiKey !== "string" || typeof provider.baseUrl !== "string") throw new Error("resolved provider is incomplete");
const cost = resolveModelCostConfig({ provider: "sub2api-openai", model: "gpt-5.6-luna", config: cfg });
if (!cost) throw new Error("verified Luna pricing is unavailable");
const pricing = { route, currency: "USD", unit: "per_million_tokens", source: "openclaw.resolveModelCostConfig", fingerprint: resolveModelCostConfigFingerprint(cfg), rates: { input: cost.input, output: cost.output, cacheRead: cost.cacheRead, cacheWrite: cost.cacheWrite }, tieredPricing: (cost.tieredPricing ?? []).map((tier) => ({ range: tier.range, cost: { input: tier.input, output: tier.output, cacheRead: tier.cacheRead, cacheWrite: tier.cacheWrite } })) };
const proxy = spawn("python3", ["-m", "bridges.luna_budget_proxy"], {
  cwd: root, stdio: ["ignore", "pipe", "inherit"], env: { ...process.env, PYTHONPATH: `${root}/evals`, CONTEXT_SHUNT_PROXY_PRICING_JSON: JSON.stringify({ pricing: pricing }), CONTEXT_SHUNT_PROXY_UPSTREAM: `${provider.baseUrl.replace(/\/$/, "")}/chat/completions`, CONTEXT_SHUNT_PROXY_API_KEY: provider.apiKey, CONTEXT_SHUNT_LUNA_BUDGET_DB: process.env.CONTEXT_SHUNT_LUNA_BUDGET_DB ?? "", CONTEXT_SHUNT_PROXY_PORT: "0" },
});
const line = createInterface({ input: proxy.stdout });
const ready = await new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error("proxy startup timeout")), 10_000); line.once("line", value => { clearTimeout(timer); resolve(JSON.parse(value)); }); proxy.once("error", reject); });
console.log(JSON.stringify({ ready: true, identity_only: true, proxy_port: ready.port, route, resolver_fingerprint: pricing.fingerprint, tier_count: pricing.tieredPricing.length }));
proxy.kill("SIGTERM");
