# Five-workflow real Luna validation

This opt-in run executed the same five sanitized fixtures through the owned legacy compactor, PRE `ShuntSession`/`Reader` isolated from commit `1686db6`, and the NEW working-tree implementation. Semantic reads used OpenClaw's supported in-host `runtime.llm.complete` isolated-agent transport with distinct system and user roles. Deterministic NEW aggregation/search cases did not call a model.

Resolved route required and observed on every completed call: `sub2api-openai/gpt-5.6-luna`.

| Lane | Correct | Citations valid/applicable | Coverage complete/total | Calls (reported/unknown) | Provider tokens in/out/cache* | Role bytes | Transport bytes in/out | Lane wall ms | Cache hits |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy_compactor | 3/5 | 0/0 | 0/0 | 0 (0/0) | unknown/unknown/unknown | unknown | unknown/unknown | 88.507 | 0 |
| pre | 3/5 | 3/3 | 5/17 | 8 (8/0) | 21532/2846/3840 | 180834 | 183042/11147 | 96642.369 | 0 |
| new | 5/5 | 2/2 | 7/18 | 2 (2/0) | 1280/108/0 | 5132 | 5564/1926 | 39067.595 | 1 |

\* Token values are provider-reported lower bounds only. Missing input/output or cache usage remains `unknown`; no count is reconstructed from bytes or a completion ratio. Main-context byte/token estimates are separate in the JSON evidence.

Wall time is the observed duration of each independently launched lane, including its own initialization. It is reported as execution evidence, not as an end-to-end time-savings claim; the legacy lane makes no provider calls and is not latency-comparable to PRE/NEW.

The run started 10 real attempts: 10 completed, 0 timed out, 0 failed, and 0 remained late/in-flight. Timed-out, failed, or late usage remains unknown rather than being reconstructed from response bytes.

Each real call retains only redacted payload evidence: role names, byte lengths, SHA-256 digests, exact-template booleans, bounded caps, resolved route/execution identity, status, duration, and provider usage when reported. It stores no system prompt, question, source excerpt, completion, citation quote, credential, or host stderr.

The cache/aggregation red checks remain explicitly fixture-based sabotage checks in `latest.json`; they demonstrate that bypassing either feature changes measured calls/correctness but are not relabeled as real-provider evidence. No deterministic case was forced through Luna for this run.

Acceptance: **PASS**.

Exact command (first export `CONTEXT_SHUNT_OPENCLAW_ROOT` to the existing clean OpenClaw checkout; its local value is intentionally not retained):

```sh
CONTEXT_SHUNT_LUNA_EVAL=1 CONTEXT_SHUNT_OPENCLAW_ROOT="$CONTEXT_SHUNT_OPENCLAW_ROOT" CONTEXT_SHUNT_OPENCLAW_ROUTE=sub2api-openai/gpt-5.6-luna CONTEXT_SHUNT_LUNA_BUDGET_DB="$CONTEXT_SHUNT_LUNA_BUDGET_DB" CONTEXT_SHUNT_EVAL_PARENT_CONTEXT_CANARY=PRIVATE_PARENT_CONTEXT_CANARY_7f9070 .venv/bin/python evals/intent-reader-audit/real_run.py --json-output evals/intent-reader-audit/real-luna-latest.json --markdown-output docs/five-workflow-real-luna.md
```

Machine-readable redacted evidence: [`evals/intent-reader-audit/real-luna-latest.json`](../evals/intent-reader-audit/real-luna-latest.json).
