# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

讓大型來源留在代理的主要 context 之外，但不以盲目的摘要取代原文。context-shunt
會在 host 工具執行前阻擋過大或無法證明有界的讀取，只把被攔下的 bytes 擷取到私有
snapshot store，讓代理改以明確問題查詢。回覆有固定上限、會揭露未涵蓋範圍，而且只保留
能由決定性程式逐 byte 對照 immutable snapshot 驗證的引用。

本專案全域唯讀：不修改檔案、不註冊 writer、不提供任意 cache 全文取回、不在偵測到秘密
後自動遮罩並繼續，也不宣稱「引用文字確實存在」就能證明模型推論正確。`inspect` 會刻意
回傳精確原文，但同時受單次結果、單一來源及單一 session 的 byte 預算約束。這些是 context
與揭露控制，不是「小到能放進預算的來源也絕不會完整回傳」的機密邊界。

## 為什麼使用 query-aware reading

啟發式摘要會在代理提出問題前先決定什麼重要，因此容易漏掉罕見細節、隱藏否定證據，且
難以稽核。context-shunt 改為把原始問題和有界的來源 chunks 一起交給 reader，保留明確的
`coverage` 紀錄，並在模型回答後驗證引用證據。答案不佳時，可以對同一 snapshot 提出更精準
的問題，也可以用決定性 `inspect` 查看原文；不會只因內容簡短就把它當成可信摘要。

```text
host 讀取要求
      |
      v
pre-read gate ---- 小型或可證明有界 ----> 原本的 host 工具
      |
      | 執行前阻擋
      v
授權 + snapshot ----> SQLite metadata + content-addressed 私有 blob
      |                                      |
      +---- 不透明 source_id + snapshot_id --+
                         |
          +--------------+----------------+
          |                               |
          v                               v
 query-aware reader                 決定性 inspect
 每個已處理 chunk 至少一次呼叫     精確 lines/bytes/search
 retry/fallback 有界                零模型呼叫
          |                               |
          v                               v
 引用驗證器 + output guard -------> 有界 envelope ------> 主要 context
```

## 保護範圍與非目標

- 完整文字讀取超過目前預設的 350 個實體行，或超過 16 KiB，會在執行前被阻擋。小型檔案、
  指定範圍的讀取及有界搜尋仍可直接通過。
- 只有分類器能證明輸出有界時，read-like shell 指令才會通過；無界或無法分類的讀取以
  `UNCLASSIFIABLE_READ` fail closed。
- 只有設定於 `workspace_roots` 的來源可被擷取。秘密路徑／內容、binary、無效文字、危險
  link、race 及非 regular file 都會被拒絕。
- Reader 只收到固定指令、問題與一段已授權 excerpt，不會取得 host conversation、shell、
  network 或 write tools。
- 來源 payload 與 provider error body 不會進入錯誤、log、metric label、retry、fallback，
  或出現在已驗證 quote 以外的原始輸出。`inspect` 的精確文字是刻意的例外，並會計入揭露量。
- Gate 只涵蓋 capability report 列出的 host tool id。若部署需要完整涵蓋，必須停用 host
  中未受控制的讀取工具。

## 快速開始

### 本機驗證

需要 Python 3.11 以上及 Node 22.22.3 以上（store 使用 `node:sqlite`）。

```bash
python3 -m venv .venv
./.venv/bin/pip install -e 'packages/core-py[dev]'
npm install
./scripts/verify unit all
./scripts/verify packaging all
./scripts/verify benchmark core
```

`scripts/verify` 的 exit code：通過為 0、失敗為 1、缺少 live host/model 前置條件的
`NOT_RUN` 為 2。`NOT_RUN` 絕不算通過。報告寫入 gitignored 的 `reports/`，不含來源 payload。

### Hermes

```bash
/path/to/hermes/python -m pip install ./packages/core-py
cp -R adapters/hermes/context-shunt ~/.hermes/plugins/context-shunt
```

把 [`examples/config/hermes.config.yaml`](examples/config/hermes.config.yaml) 合併到
`~/.hermes/config.yaml`、替換 `workspace_roots`，再重新啟動 Hermes。Plugin 的 `llm` policy
必須允許所設定的 model/provider override。Reader 也會成為 Hermes auxiliary task
`context_shunt_reader`；`auxiliary.context_shunt_reader` 的值優先於 plugin reader 預設，
Hermes 的 `auto` 則代表繼承。

使用真實 checkout 驗證：

```bash
CONTEXT_SHUNT_HERMES_ROOT=/path/to/hermes-agent \
CONTEXT_SHUNT_HERMES_PYTHON=/path/to/hermes/python \
  ./scripts/verify integration hermes --mode local
./scripts/verify integration hermes --mode unsupported
```

### OpenClaw

```bash
npm run build --workspace @context-shunt/core
openclaw plugins install --link ./adapters/openclaw --force
openclaw plugins enable context-shunt
```

把 [`examples/config/openclaw.json`](examples/config/openclaw.json) 合併到
`openclaw.json`、替換 `workspace_roots`、在相鄰的 `llm` policy 授權 reader target，重啟
Gateway 後檢查載入結果：

```bash
openclaw plugins inspect context-shunt --runtime --json
CONTEXT_SHUNT_OPENCLAW_ROOT=/path/to/openclaw \
  ./scripts/verify integration openclaw --mode local
./scripts/verify integration openclaw --mode unsupported
```

完整安裝、清理、遷移與移除方式見 [`docs/install.md`](docs/install.md)。

## 三個工具

下列 host-facing 呼叫只使用兩個 adapter 都有註冊的欄位；adapter 會先補上內部 `tool`
discriminator，再依
[`contracts/v1/tool-args.schema.json`](contracts/v1/tool-args.schema.json) 驗證。內部 contract
也支援 read `selector` 及 inspect 的 `max_result_bytes`／`max_scan_lines`，但 OpenClaw 目前
註冊的 schema 未公開這些 optional fields。可攜式呼叫應使用下列共同子集。不透明 id 僅供
示意。

### `context_shunt_read`

來源必須二選一：初次擷取使用 `paths`，對先前回傳的 snapshot 再提問則使用 `handles`。
Core 內部 selector 有 `all`、`lines`、`records` 與有界 literal `search`；目前可攜式的
registered call 使用 `all`。

```json
{
  "question": "哪一個 retry limit 適用於 transient provider failure？",
  "paths": ["/workspace/project/config/runtime.yaml"]
}
```

成功 envelope 的形狀如下：

```json
{
  "schema_version": "1.1",
  "request_id": "reader-01",
  "status": "ok",
  "code": "ANSWERED",
  "answer": "Transient retry limit 是 1 [c1]。",
  "citations": [{
    "id": "c1",
    "source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
    "locator": {"kind": "lines", "start": 42, "end": 42},
    "quote": "max_transient_retries: 1",
    "verified": true
  }],
  "coverage": {"complete": true, "processed_chunks": 1, "planned_chunks": 1,
    "omitted": [], "upstream_truncated": false},
  "sources": [{"source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
    "media_type": "text/plain", "bytes": 4096,
    "expires_at": "2026-09-07T12:00:00Z"}],
  "retryable": false,
  "result_kind": "model_derived",
  "provenance": {"derived": true, "label": "model_generated_answer",
    "requested_provider": "openai", "requested_model": "gpt-5.6-luna",
    "resolved_provider": "openai", "resolved_model": "gpt-5.6-luna",
    "reported_provider": null, "reported_model": null,
    "attribution_status": "resolved", "attribution_confidence": "medium",
    "attribution_policy": "allow_unverified", "attempts_started": 1,
    "usage_complete": true, "citations_mechanically_verified": true,
    "fallback_used": false},
  "accounting_id": "acc_0123456789abcdef"
}
```

不重新擷取，直接精煉問題：

```json
{
  "question": "請顯示判定該 retry 為 transient 的精確條件。",
  "handles": [{
    "source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea"
  }]
}
```

### `context_shunt_inspect`

以零模型呼叫回傳精確 snapshot 文字。Lines 是 1-based inclusive，bytes 是 0-based
half-open；search 只接受 literal `needle`，不接受 regular expression。若結果未完成，以相同
handle、snapshot、selector 連同上一頁的 opaque `next_cursor` 繼續。

```json
{
  "source_id": "src_01ab",
  "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
  "selector": {"kind": "lines", "start": 40, "end": 42}
}
```

```json
{
  "schema_version": "1.1", "request_id": "inspect-01",
  "status": "ok", "code": "EXTRACTED", "answer": "", "citations": [],
  "coverage": {"complete": true, "processed_chunks": 0, "planned_chunks": 0,
    "omitted": [], "upstream_truncated": false},
  "sources": [{"source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
    "media_type": "text/plain", "bytes": 4096,
    "expires_at": "2026-09-07T12:00:00Z"}],
  "retryable": false,
  "result_kind": "deterministic_extraction",
  "provenance": {"derived": false, "label": "deterministic_extraction",
    "attribution_status": "not_applicable", "attribution_confidence": "none",
    "attribution_policy": "not_applicable", "attempts_started": 0,
    "usage_complete": true, "citations_mechanically_verified": true},
  "extraction": {"mode": "lines", "source_id": "src_01ab",
    "snapshot_id": "sha256:2a97516c354b68848cdbd8f54a226a0a55b21ed138e207ad6c5cbb9c00aa5aea",
    "deterministic": true,
    "segments": [{"kind": "lines", "start": 40, "end": 42,
      "text": "counts:\n  max_citations: 16\n  max_transient_retries: 1"}],
    "result_bytes": 54, "complete": true, "next_cursor": null,
    "lines_scanned": 3, "scan_budget_exhausted": false,
    "disclosed_bytes_source": 54, "disclosed_bytes_session": 54,
    "disclosure_limit_reached": false},
  "accounting_id": "acc_1111222233334444"
}
```

### `context_shunt_stats`

回傳本 session aggregate 與有界的 operation records 頁面，不能指定別的 session、重設
counter、修改 retention 或取得來源內容。

```json
{"page": 1, "page_size": 8}
```

重要 totals 欄位是 `operations`、`raw_input_bytes`、`baseline_credit_tokens`、
`main_model_envelope_tokens`、`reader_input_tokens`、`reader_output_tokens`、
`reader_cache_tokens`、`main_context_tokens_saved`、`net_tokens_saved`、
`attempts_started`、`attempts_usage_complete` 與 `disclosed_bytes`。公式及完整 record 欄位見
[`docs/metrics.md`](docs/metrics.md)。

## 設定

兩個 plugin 共用以下設定：

| Key | 目前預設 | 用途 |
| --- | --- | --- |
| `workspace_roots` | 必填 | 非空的來源 root allowlist。 |
| `cache_dir` | `$CONTEXT_SHUNT_CACHE` 或 `~/.cache/context-shunt` | 私有 store，必須在所有 workspace root 外。 |
| `spill_dir` | legacy alias | 只有未設定 `cache_dir` 時才使用的 1.1 前別名。 |
| `denylist` | `[]` | 額外 relative glob；內建 secret policy 仍會套用。 |
| `gate_enabled` | `true` | 啟用 pre-read gate。 |
| `reader.enabled` | `true` | 控制執行；`false` 會在不呼叫模型的情況下拒絕 read，但 adapter 仍可能註冊工具。 |
| `reader.model` | `gpt-5.6-luna` | Reader 要求的模型；1.1 起可設定。 |
| `reader.provider` | `""` | 可選 provider pin；空字串交由 host routing。 |
| `reader.attribution_policy` | `allow_unverified` | 誠實發布較弱歸屬；`require_match` 則拒絕。 |
| `reader.fallback_chain` | `[]` | 最多四個 availability target；不是品質 fallback。 |
| `inspect.enabled` | `true` | 決定性精確擷取。 |
| `stats.enabled` | `true` | Session accounting。 |
| `suma_post_tool.enabled` | `false` | Optional post-tool spill；兩個 host 都不支援，因此不會啟用。 |
| `limits` | contract 預設 | Integer override 只能縮小 `contracts/v1/limits.json` 的值。 |

`writer.enabled: true` 及含 `propose_patch` 的 `operations` 會被拒絕。兩個公開 core loader
都會拒絕未知的 top-level key、nested key 與 limit name，以及型別錯誤、放寬 limit、空 model
或超過四個 fallback entry。OpenClaw manifest 是額外的 host-side 驗證邊界，不是 core 唯一
的防線。

Threshold、cache/store、TTL、disclosure、token、concurrency、retry、deadline、JSON、paging
的目前預設，以及 host-specific `llm` 與 Hermes auxiliary key，完整列於
[`docs/configuration.md`](docs/configuration.md)。範例設定是 packaging gate 會載入的 fixture，
不是無法執行的示意設定。

## Escape hatch 與失敗行為

設定模型可能能力不足、不可用、quota 用盡、逾時，或回傳格式錯誤／無效引用。
context-shunt 不會把這些狀況改造成無引用摘要，也不會 fallback 到原始 payload。

- Transient provider error 最多 retry 一次，且仍共用 call/input/request deadline 預算。
  設定的 fallback chain 只處理 availability。
- 逾時為 `TIMEOUT`、provider failure 為 `MODEL_ERROR`、格式錯誤或超限為
  `INVALID_MODEL_OUTPUT`、沒有引用存活為 `CITATION_INVALID`、歸屬政策拒絕為
  `PROVENANCE_UNAVAILABLE`。
- `recovery.handles_valid` 表示 snapshot 是否仍可重用；actions 可能包含
  `RETRY_SAME_QUESTION`、`REFINE_QUESTION_SAME_SNAPSHOT`、`INSPECT_HANDLE`、
  `NARROW_SELECTOR` 或 `WAIT_AND_RETRY`。
- 用 `handles` 對同一 snapshot 提出更精確問題，不必重新擷取；需要真正原文時，用窄範圍
  selector 呼叫 `inspect`。
- `coverage.complete: false`、`coverage.omitted`、processed/planned chunks 與
  `upstream_truncated` 會防止 partial answer 冒充完整。若為符合 envelope 上限而捨棄證據，
  相應 assertion 也會被移除。
- Inspect 會在單頁、掃描、per-source 或 per-session 預算停止。Cursor 不會擴張授權；
  disclosure 用盡後不再回傳內容。
- Capture/store/output guard 失敗時，不會發布可用 handle 或 raw fallback；原本的 oversized
  operation 維持 blocked。

## Provenance 與 accounting

`result_kind: model_derived` 代表 answer 是模型產生；`deterministic_extraction` 代表精確
snapshot bytes。Gate decision、pointer、stats 及 failure 也都標示為 non-derived。機械引用
驗證只證明 quote 位於指定 snapshot 位置，不證明它在語意上支持模型 claim。

模型身分分成三層：`requested_*` 是 adapter 的要求，`resolved_*` 是 host 套用政策後的選擇，
`reported_*` 是 host 有提供時的 provider report。`attribution_status` 可為 `actual`、
`resolved`、`unverified`、`mismatch`、`unknown` 或 `not_applicable`。目前沒有 supported adapter
能證明 `actual`：Hermes 上限為 `unverified`，OpenClaw 可證明 `resolved`。缺少的值維持 null，
絕不以 requested value 補成更強的身分聲明。

Accounting 使用有號數：

```text
main_context_tokens_saved = baseline_credit_tokens - main_model_envelope_tokens
net_tokens_saved = main_context_tokens_saved
                   - reader_input_tokens - reader_output_tokens
```

完整 payload baseline 是明確標示的 counterfactual（`full_payload_counterfactual`），每個
snapshot 只 credit 一次；已被 host 截短的輸入用 `host_truncated_observed`。Envelope 與
baseline 估算標為 `bytes_div_4`，只有 provider 回報時 reader usage 才算 exact。缺少的
provider input/output count 會以實際量到的 prompt/completion bytes 估算並標為 `bytes_div_4`；
無法取得的 cache usage 或未呼叫模型的欄位維持 `null`，不會捏造成零。每次實際
retry/fallback 都計入 `attempts_started`，usage completeness 則記錄有多少 attempt 提供 exact
可用 count。Stats 不提供金額或 wall-clock latency；provider benchmark 只有實際執行時才報
latency/token，也不會自行捏造價格。

## Artifact lifecycle 與限制

SQLite 只存 authorization metadata、摘要後的 scope、expiry、generation、quota、refcount、
disclosure total 及有界 operation records；不存 path、question、answer、quote、preview、
provider error body、model/provider name 或 blob path。Immutable payload 位於由內容 hash
內部推導的 blob 位置。

Directory 會重新確認為 `0700`、payload file 為 `0600`；安全 open 拒絕 symbolic link、
hard link、FIFO、device 與 replacement。Handle 綁定 host/profile/principal/session/generation。
目前預設 TTL 為一小時；每次啟動和日常流程會 sweep，真正 reset/finalize/delete 時會 revoke，
一般 turn 或 compaction 不會。刪除只做 unlink，不代表 secure erase。

目前預設包含：每個 captured source 8 MiB、512 個 live handles、store 內 256 MiB distinct
content、每個 inspect result 16 KiB、每個 source 累計揭露 256 KiB、每個 session 1 MiB。
部署只能縮小，不能放寬 contract caps。精確設定名和值見
[`docs/configuration.md`](docs/configuration.md)，安全與 retention 詳情見
[`docs/security.md`](docs/security.md)。

## Capability 與 release 狀態

| 能力 | Hermes 0.18.2 | OpenClaw 2026.9.2 |
| --- | --- | --- |
| Pre-read gate | supported | supported |
| Query-aware reader | supported；歸屬 `unverified` | supported；歸屬 `resolved` |
| 決定性 inspect | supported | supported |
| Session stats/lifecycle | supported | supported |
| Oversized post-tool spill/pointer | unsupported | unsupported |
| Writer / `propose_patch` | 未實作 | 未實作 |

這張表是 code/source capability evidence，不表示此 checkout 的所有 live release gate 都已
通過。Deterministic unit、unsupported-mode、core benchmark 與 packaging gates 已實作。
已記錄的真實 host integration evidence 共執行 122 個 cases、0 failed；重新執行仍須由使用者
提供 Hermes 與 OpenClaw checkout。兩個 post-tool gate 因 host seam 不受支援，維持
`NOT_RUN`。40 題 production-equivalent Luna eval 與 provider benchmark 也仍為 `NOT_RUN`；
不能把缺少的 live model 證據描述成通過。因此 `release all` 尚不是通過的 production release
signal。詳見 [`docs/capability-matrix.md`](docs/capability-matrix.md) 與
[`docs/acceptance.md`](docs/acceptance.md)。

## 開發、相容性與支援

兩個獨立核心共用 JSON schemas、conformance fixtures、caps、status-code table 與規範性
SQLite DDL：

| 路徑 | 用途 |
| --- | --- |
| [`contracts/v1/`](contracts/v1/) | Request/envelope/tool schemas、limits、status pairs、fixtures |
| [`contracts/store/v1.sql`](contracts/store/v1.sql) | 規範性 local store schema |
| [`packages/core-py/`](packages/core-py/) | Hermes 使用的 Python core |
| [`packages/core-ts/`](packages/core-ts/) | OpenClaw 使用的 TypeScript core |
| [`adapters/hermes/`](adapters/hermes/) | Hermes adapter |
| [`adapters/openclaw/`](adapters/openclaw/) | OpenClaw adapter |
| [`evals/`](evals/) | 固定 reader eval corpus 與 bridges |
| [`scripts/verify`](scripts/verify) | 驗證入口 |

Review contract parity 後執行 `./scripts/sync-contracts --check`；只有刻意修改 root contracts
時才執行 `./scripts/sync-contracts`。Contract 1.1 接受 1.0 request/envelope，但拒絕偽裝成
1.0 的 1.1 欄位。DDL revision 1 可 additive migration；未知 store revision fail closed，應清除
cache。Host 升級後，必須重跑 local integration gate 才能恢復相容性證據。

貢獻變更前請執行 `./scripts/verify unit all`、`./scripts/verify packaging all` 與相關 integration
gate。不可為了缺少 host/provider 而削弱 deterministic tests。疑難排解及安全清理方式見
[`docs/install.md`](docs/install.md) 與 [`docs/limitations.md`](docs/limitations.md)。

移除時，請 disable/uninstall OpenClaw plugin，或刪除 Hermes plugin directory；再移除對應 core
package 與 host config entry。若不再需要 snapshot，停止 host 後刪除所設定的 `cache_dir`。
對應用程式而言，刪除 cache 無法復原且不等於 secure erase；若使用自訂位置，刪除前務必確認
精確路徑。完整指令與 DDL revision migration 說明見 [`docs/install.md`](docs/install.md)。

其他文件包括規範性 [`architecture`](docs/architecture.md)、
[`security model`](docs/security.md)、[`acceptance gates`](docs/acceptance.md)、
[`capability matrix`](docs/capability-matrix.md)、
[`configuration reference`](docs/configuration.md)、
[`accounting reference`](docs/metrics.md)、[`limitations`](docs/limitations.md) 及目前的
[`development status`](docs/implementation-plan.md)。

## 授權與 notices

[Apache License 2.0](LICENSE)。Spotify Shunt 與 Headroom 僅作為設計基線列名致謝；本專案未
複製或改寫其程式碼。Runtime dependencies 與第三方關係詳見
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
