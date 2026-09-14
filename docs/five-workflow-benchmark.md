# Five-workflow intent-reader benchmark

Synthetic production-derived shapes; no production content or provider call. Token values are bytes/4 estimates, not billed tokens. Controlled wall time uses a fixed 25ms mock-provider latency per attempt plus 50 MB/s processing. `null` reader/cache tokens mean not applicable or not reported—never zero substituted for unknown.

| Lane | Main tokens (est.) | Reader in/out reported | Reader in/out est. total | Provider cache | Attempts (usage complete) | Unknown/late | Requery bytes | Full-read bytes | Accuracy | Controlled wall ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy_compactor | 24553 | null/null | null/null | null | 0 (0) | 0 | 23112 | 17601 | 0.400 | 18.043 |
| pre | 18017 | 114567/2446 | 157826/3350 | null | 15 (11) | 4 | 23112 | 17601 | 0.800 | 405.123 |
| new | 7888 | 25686/600 | 25686/600 | null | 4 (4) | 0 | 23112 | 0 | 1.000 | 119.332 |

The legacy lane executes the golden-tested owned port of incumbent compactor v0.3.0. PRE parameters are frozen from branch `1686db6` and the sanitized pre-change trace; NEW parameters apply only the tested owned-reader capabilities: workflow 3's exact repeat is a scoped cache hit, workflows 2/3 use deterministic aggregation, and workflow 5 uses bounded selected retrieval. The replay is deterministic rather than a claim that either git tree or a provider was executed live. Workflow 4 remains unchanged because correlating Hermes requery calls is a host-owned gap; its 19,912 recovery bytes remain counted in every lane. The legacy/current full-read loss in workflow 5 is likewise included, rather than credited as a saving.

Deterministic outputs do not need model citations, so citation validity is `null`, not a vacuous 100%. Reader attempts with incomplete usage retain both reported lower-bound and estimated-total fields in the JSON artifact.

Corpus SHA-256: `ad322d962eafd159d484965e20b8c24bf15c90dafc1f88b17ca26e9c25e0ba07`.

Machine-readable evidence: [`evals/intent-reader-audit/latest.json`](../evals/intent-reader-audit/latest.json).
