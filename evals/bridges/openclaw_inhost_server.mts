/**
 * In-host reader bridge server: role-preserving model access for the Luna gates.
 *
 * NOT production-equivalent, and not wired into any gate. It reaches the model through the
 * simple-completion transport, while the production adapter uses the isolated agent
 * runtime (see "Not matched" below). No score from this file may be reported as a
 * production-equivalent result.
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
 * separation; it does not by itself make a score representative.
 *
 * What it matches, and what it does not
 * -------------------------------------
 * Matched to the production adapter, deliberately and verifiably:
 *   - separate system role (`context.systemPrompt`) and a single user message;
 *   - the reader's own output cap (`maxTokens`), not the model's catalogue maximum;
 *   - `temperature: 0`;
 *   - no reasoning/thinking level, because the adapter passes none. The CLI bridge pinned
 *     `--thinking low`, which is a second deviation from production, not a neutral choice.
 *
 * Not matched, and recorded rather than glossed: production reaches the model through the
 * agent-harness runtime (`runIsolatedCompletion`). In this environment that path is not
 * reachable for the reader model - `openai/gpt-5.6-luna` resolves to the `codex` agent
 * runtime, which reports `owner-plugin-degraded`, and a bare `sub2api-openai` call fails
 * auth lookup because the harness resolves the auth profile upstream. This server uses the
 * simple-completion transport, which resolves the same credentials through the host's own
 * profile store, and reports `transport: "simple-completion"` in every result so no report
 * can imply the harness path was exercised.
 *
 * Protocol: newline-delimited JSON on stdin/stdout. Startup (config + catalogue load) costs
 * ~20s, so the caller spawns this once and keeps it, rather than paying it 120 times.
 *
 * It reads no secret: `prepareSimpleCompletionModelForAgent` resolves credentials inside
 * the host, and only `auth.apiKey` is handed straight back to the host's own completion
 * call. Nothing is logged, echoed, or written.
 */
import { createInterface } from "node:readline";

const HOST = process.env["CONTEXT_SHUNT_OPENCLAW_ROOT"];
if (!HOST) {
  process.stderr.write("CONTEXT_SHUNT_OPENCLAW_ROOT is required\n");
  process.exit(2);
}

const { loadConfig } = await import(`${HOST}/src/config/io.runtime.js`);
const { prepareSimpleCompletionModelForAgent, completeWithPreparedSimpleCompletionModel } =
  await import(`${HOST}/src/agents/simple-completion-runtime.js`);

const cfg = await loadConfig();
const AGENT = process.env["CONTEXT_SHUNT_OPENCLAW_AGENT"] || "main";
const ROUTE = process.env["CONTEXT_SHUNT_OPENCLAW_ROUTE"] || "sub2api-openai/gpt-5.6-luna";

const prepared = await prepareSimpleCompletionModelForAgent({
  cfg,
  agentId: AGENT,
  modelRef: ROUTE,
  skipAgentDiscovery: true,
  allowBundledStaticCatalogFallback: true,
});
if ("error" in prepared) {
  process.stderr.write(`prepare failed: ${prepared.error}\n`);
  process.exit(2);
}

/** Identity of what actually answered, recorded once and echoed on every result. */
const identity = {
  transport: "simple-completion",
  agent: AGENT,
  requested_route: ROUTE,
  resolved_provider: prepared.selection.provider,
  resolved_model: prepared.selection.modelId,
  model_api: prepared.model.api,
  host_version: cfg?.version ?? null,
};
process.stdout.write(JSON.stringify({ ready: true, identity }) + "\n");

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((p): p is { type: string; text: string } =>
      Boolean(p && typeof p === "object" && (p as { type?: string }).type === "text"),
    )
    .map((p) => p.text)
    .join("");
}

const rl = createInterface({ input: process.stdin });
for await (const line of rl) {
  if (!line.trim()) continue;
  let req: { id: number; system: string; user: string; max_output_tokens: number };
  try {
    req = JSON.parse(line);
  } catch {
    continue;
  }
  const started = Date.now();
  try {
    const result = await completeWithPreparedSimpleCompletionModel({
      model: prepared.model,
      auth: prepared.auth,
      cfg,
      // The production shape: system instructions in their own role, one user turn.
      context: {
        systemPrompt: req.system,
        messages: [{ role: "user", content: req.user, timestamp: Date.now() }],
      },
      // The reader's cap, and the adapter's temperature. No reasoning: production sends none.
      options: { maxTokens: req.max_output_tokens, temperature: 0 },
    });
    const usage = (result as { usage?: Record<string, number> }).usage;
    process.stdout.write(
      JSON.stringify({
        id: req.id,
        ok: true,
        text: textOf((result as { content?: unknown }).content),
        elapsed_ms: Date.now() - started,
        ...identity,
        usage: usage ?? null,
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
        elapsed_ms: Date.now() - started,
      }) + "\n",
    );
  }
}
