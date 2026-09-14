# Five-workflow intent-reader execution benchmark

This benchmark executes identical synthetic production-derived content through the owned legacy compactor, PRE code isolated from commit `1686db6`, and the NEW working-tree ShuntSession. The provider is a deterministic grounded fixture; payload bytes, attempts, returned usage, and elapsed time are observed during execution.

| Lane | Correct | Main bytes (tokens est.) | Reader payload in/out bytes | Provider tokens in/out/cache* | Core accounted in/out/cache | Attempts (reported/unknown) | Answer-cache hits | Requery | Full read | Harness ms | Mock delay configured/observed ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy_compactor | 3/5 | 64667 (16167) | unknown/unknown | unknown/unknown/unknown | unknown/unknown/unknown | 0 (0/0) | 0 | 19912 | 17601 | 80.684 | 0.0/0.000 |
| pre | 3/5 | 60271 (15068) | 145991/1098 | 19210/262/0 | 36499/276/unknown | 7 (5/2) | 0 | 19912 | 17601 | 223.574 | 14.0/18.982 |
| new | 5/5 | 43170 (10793) | 5132/332 | 1284/84/0 | 1284/84/0 | 2 (2/0) | 1 | 19912 | 0 | 224.958 | 4.0/6.069 |

\* Provider tokens are only values returned by the instrumented fixture. A deliberately missing usage report remains an unknown attempt, so each token total is a reported lower bound—not a completion-ratio estimate. The fixture uses bytes/4 as its explicit token tariff; these fields are copied from its actual `ModelResponse.usage`, not inferred afterward. Main-context tokens alone are estimated from observed bytes at bytes/4.

Correctness is computed from emitted answers/extractions against independently declared expectations in `corpus.json`. `Harness ms` is measured elapsed execution time; configured and observed mock delay are reported separately, with no arithmetic controlled-time substitute.

## Red checks

- `cache_bypass_changes_execution`: **PASS** — `{"bypassed_attempts": 2, "bypassed_cache_hits": 0, "name": "cache_bypass_changes_execution", "normal_attempts": 1, "normal_cache_hits": 1, "passed": true}`
- `aggregation_bypass_changes_execution_and_correctness`: **PASS** — `{"bypassed_attempts": 6, "bypassed_correct": {"session-2-minified-loki-counts": false, "session-3-distinct-and-exact-repeat": false}, "name": "aggregation_bypass_changes_execution_and_correctness", "normal_attempts": 1, "normal_correct": {"session-2-minified-loki-counts": true, "session-3-distinct-and-exact-repeat": true}, "passed": true}`

Workflow 4 retains 19,912 requery bytes in all lanes. Workflow 5 executes a 17,601-byte full read in legacy/PRE and bounded search in NEW. Those losses are measured operations, not assigned profile fields.

Corpus SHA-256: `0e5ba0c6ccd20c9f777f72c07b815903496527d7995cb9ebe78a5501755f5039`.

Machine-readable evidence: [`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json).
