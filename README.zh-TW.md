# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

把大型讀取擋在主模型的上下文之外，交給成本較低或可自行設定的 reader 依問題查找，並以不可變更
快照逐字驗證引用。

> **目前狀態：預發行、唯讀。** Hermes 0.18.2 與 OpenClaw 2026.9.2 已支援讀取前攔截；
> 兩者都無法攔截過大的工具執行結果。專案沒有 writer，也不支援 `propose_patch`，目前不宣稱
> 已可用於正式環境。

## 為什麼需要 context-shunt

代理直接讀取檔案時，往往還不知道答案在哪裡，原文卻已占用主模型的上下文。依目前預設，
超過 350 個實體行或 16 KiB 的完整文字讀取會被攔截，以免排擠對話、指令與工作狀態。

截斷或啟發式摘要雖然省空間，卻必須事先猜測什麼重要。少見的條件、否定結果，甚至問題的答案
都可能因此消失。context-shunt 保留來源快照，讓代理先問清楚，再讀真正需要的部分。

## 運作流程

```text
host 讀取要求
        |
        +-- 小型或可證明有界 ----------> 原本的 host 工具
        |
        `-- 過大或無法證明有界
                    |
               執行前阻擋
                    |
        依 workspace_roots 授權
                    |
             私有不可變更快照
                    |
             reader 依問題查找
                    |
                 驗證引用
                    |
             有界答案 ------> 主模型上下文
                    |
                    `-------> 需要原文時用 inspect
```

Gate 只涵蓋 capability report 明列的 host 工具。若部署環境要求所有讀取都受控，必須停用未受
context-shunt 管理的原始讀取工具。

## 核心保證

- **依問題讀取。** 每個實際處理的 chunk 都會收到原始問題。Reader 只能看到已授權的片段，
  不會取得 host 對話或其他工具。
- **引用可核對，涵蓋範圍不隱瞞。** 決定性程式會把每段公開引用與快照比對，並列出未處理或
  省略的 chunk。這只能證明文字確實位於引用位置，不能證明它在語意上支持模型推論。
- **可精確查看原文。** `context_shunt_inspect` 不呼叫模型，可依行號、UTF-8 安全位元組範圍或
  字面搜尋回傳受限的精確內容。
- **快照不可變更且有使用範圍。** `workspace_roots` 是 allowlist；機密路徑／內容、二進位檔案
  與不安全來源一律拒絕。私有 blob 由 TTL 與 session 清理機制移除。Inspect 預算與刪除動作是揭露控制，
  不等於機密保證或安全抹除。
- **Token 帳務如實標示。** Session 記錄會分開計算主模型省下的 token 與 reader 輸入／輸出，
  區分精確值和估算值，也納入實際重試與可用性備援。專案不會把 token 自行換算
  成金額節省。

## 三個唯讀工具

| 工具 | 用途 | 模型呼叫 |
| --- | --- | --- |
| `context_shunt_read` | 對已授權路徑或既有快照 handle 提問。 | 每個已處理 chunk 至少一次；重試與可用性備援可能增加呼叫次數。 |
| `context_shunt_inspect` | 從快照取得精確行、UTF-8 安全位元組範圍或字面搜尋結果。 | 0 |
| `context_shunt_stats` | 查看目前 session 的有界 token 與揭露帳務。 | 0 |

即使 `reader.enabled` 為 false，adapter 仍可能註冊 reader 工具；真正執行時會在呼叫模型前拒絕。

## 一次完整讀取

先對註冊工具提出明確問題：

```json
{
  "question": "What is the retry ceiling?",
  "paths": ["/workspace/service/retry.py"]
}
```

回傳 envelope 的部分欄位可能如下：

```json
{
  "status": "ok",
  "code": "ANSWERED",
  "answer": "Retries stop after three attempts [c1].",
  "citations": [
    {
      "id": "c1",
      "locator": {"kind": "lines", "start": 41, "end": 41},
      "quote": "max_retries = 3",
      "verified": true
    }
  ],
  "coverage": {
    "complete": true,
    "processed_chunks": 1,
    "planned_chunks": 1,
    "omitted": [],
    "upstream_truncated": false
  }
}
```

為了便於閱讀，上例省略了不透明的 source／snapshot ID、provenance、recovery 與 accounting
欄位。完整格式請查閱 [tool argument schema](contracts/v1/tool-args.schema.json) 與
[版本化 contracts](contracts/v1/)。

## 快速開始

需要 Python 3.11 以上與 Node 22.22.3 以上。在既有 checkout 內執行：

```bash
python3 -m venv .venv
./.venv/bin/pip install -e 'packages/core-py[dev]'
npm install
./scripts/verify unit all
```

### Hermes

```bash
/path/to/hermes/python -m pip install ./packages/core-py
cp -R adapters/hermes/context-shunt ~/.hermes/plugins/context-shunt
```

把可執行的 [Hermes 設定範例](examples/config/hermes.config.yaml)合併到
`~/.hermes/config.yaml`，修改 `workspace_roots` 後重新啟動 Hermes。Hermes 外掛的 `llm`
policy 必須允許指定的 model／provider。`auxiliary.context_shunt_reader` 會覆蓋外掛的 reader
預設值；Hermes 的 `auto` 表示沿用原設定。

### OpenClaw

```bash
npm run build --workspace @context-shunt/core
openclaw plugins install --link ./adapters/openclaw --force
openclaw plugins enable context-shunt
```

把可執行的 [OpenClaw 設定範例](examples/config/openclaw.json)合併到 `openclaw.json`，修改
`workspace_roots`，並在相鄰的 `llm` policy 允許 reader 的目標模型。重新啟動 Gateway 後確認實際
載入內容：

```bash
openclaw plugins inspect context-shunt --runtime --json
```

兩個 host 的完整步驟請見[安裝、升級、清理與移除](docs/install.md)。

## 目前可用範圍

| 能力 | Hermes 0.18.2 | OpenClaw 2026.9.2 |
| --- | --- | --- |
| 過大讀取的 pre-read gate | 支援 | 支援 |
| 依問題讀取 | 支援；歸屬上限為 `unverified` | 支援；歸屬上限為 `resolved` |
| 精確 inspect 與 session stats | 支援 | 支援 |
| 過大 post-tool 結果攔截 | 不支援 | 不支援 |
| Writer / `propose_patch` | 未實作 | 未實作 |

兩個 adapter 都無法證明 provider-authoritative 的 `actual` 模型身分。OpenClaw 能回報 host
最終選定的 route；Hermes 無法區分 provider 回報與 request echo。

不需要 live provider 的決定性 gates 已實作。先前記錄的真實 host integration evidence 共
122 個案例、0 個失敗。40 題 production-equivalent Luna 評估與 provider benchmark
仍為 `NOT_RUN`；兩個 post-tool gate 也因缺少 host 端必要介面而維持 `NOT_RUN`。`NOT_RUN` 不計為
通過。詳情請見 [capability matrix](docs/capability-matrix.md) 與
[acceptance gates](docs/acceptance.md)。

## Reader 無法回答時

模型錯誤、配額不足、逾時、格式錯誤或無效引用都會安全拒絕；原本過大的內容不會因此回到
主模型上下文。

- 沿用回傳的 handle，把問題問得更精確；不可變更快照不必重新擷取。
- 需要原文時，用 `context_shunt_inspect` 指定範圍或字面搜尋。
- 採用答案前先看 `coverage`。上游截斷、期限或限制造成的缺漏都會明確標為 partial。

## 設定與帳務

Reader 預設模型為 `gpt-5.6-luna`；`reader.model` 與 `reader.provider` 都可設定，provider 留空時
交由 host routing。`fallback_chain` 只處理可用性，不會補救品質不佳的答案。所有數值 limits
只能縮小，不能放寬。

[設定參考](docs/configuration.md)整理 host policy、deadline、TTL、store、揭露、concurrency、
retry 與 envelope limits；[metrics 說明](docs/metrics.md)定義主模型上下文節省、reader usage、
估算方式、重試計算及 session scope。

## 文件索引

| 主題 | 文件 |
| --- | --- |
| 設計與信任邊界 | [Architecture](docs/architecture.md)、[安全性](docs/security.md)、[已知限制](docs/limitations.md) |
| Host 支援與 release evidence | [Capability matrix](docs/capability-matrix.md)、[acceptance gates](docs/acceptance.md)、[開發狀態](docs/implementation-plan.md) |
| 設定與維運 | [設定參考](docs/configuration.md)、[metrics](docs/metrics.md)、[安裝指南](docs/install.md) |
| 公開 contracts 與儲存格式 | [版本化 contracts](contracts/v1/)、[SQLite DDL](contracts/store/v1.sql) |
| 實作 | [Python core](packages/core-py/)、[TypeScript core](packages/core-ts/)、[Hermes adapter](adapters/hermes/)、[OpenClaw adapter](adapters/openclaw/) |
| 評估與驗證 | [Evaluation corpus](evals/)、[`scripts/verify`](scripts/verify) |

## 參與開發

送出變更前請執行不需 live provider 的檢查：

```bash
./scripts/verify unit all
./scripts/verify packaging all
./scripts/verify benchmark core
npm run typecheck --workspaces --if-present
git diff --check
```

這些命令不會把缺少的 live-model evidence 算成通過。Host integration 與 live evaluation 的要求
記錄在 [acceptance guide](docs/acceptance.md)。

## 授權與致謝

本專案採用 [Apache-2.0](LICENSE) 授權。第三方元件與授權資訊列於
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
