# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

過大 payload 的證據中介層。把大型工具結果或檔案擋在主模型上下文之外，只回傳不透明 handle，
並在提問時以不可變更快照逐位元組驗證引用。

> **目前狀態：預發行、唯讀。** Hermes 0.18.2 支援 artifact import；Hermes 0.18.2 與
> OpenClaw 2026.9.2 支援讀取前攔截。兩者都無法攔截過大的工具執行結果。專案沒有 writer，
> 也不支援 `propose_patch`。Shadow A/B 中依賴 provider 的 gates 皆為 `NOT_RUN`，因此目前
> 不宣稱已達正式環境等效。

## 為什麼需要 context-shunt

截斷或啟發式摘要雖然省空間，卻必須事先猜測什麼重要。少見的條件、否定結果，甚至問題的答案
都可能因此消失——而啟發式做法最先切掉的，往往正是 log 頁面的中段。

context-shunt 會在內容進入上下文前先完整保存，只給主模型 metadata 與 handle，並提供兩條取用
路徑：不呼叫模型的決定性搜尋，以及針對單一明確問題、附帶已驗證引用的低成本 reader。系統
不會在沒有提問的情況下自行產生摘要。

## 主要路徑：過大的工具結果

真正耗掉 session 上下文的通常是**工具結果**——log 查詢頁、雲端 journal 頁、issue tracker
匯出、wiki 頁面——而不是原始碼檔案。要把這些擋在上下文之外，必須在 host 截斷並寫入之前拿到
完整結果。OpenClaw 不提供這個順序保證，所以 `tool_result_capture`（原內部代號
`suma_post_tool`，現已更名，理由與退場方式見 `docs/capability-matrix.md`）在該 host 上維持
關閉。在 Hermes 上，這個順序已在一台實際運作中的 0.21.1 host 上直接驗證過，但那只是單一
運作實例的證據，不代表每一次安裝都成立——因此預設仍是關閉，僅在部署方明確設定
`host_ordering_verified_locally: true`（操作者自行確認）後才會回報為支援。

host 能提供的，是別人已經寫下來的 artifact。若 compactor 或 spooler 已把過大結果寫成檔案並
附上 manifest，擷取這一步其實已經完成。`context_shunt_import` 就是接收這份 artifact：

```text
producer 寫出 artifact + manifest
                |
       context_shunt_import
                |
   逐項重新驗證：allowlist 內的 root、正規化後的一般檔案、
   非 symlink／hardlink、實際讀到的位元組比對大小與 digest、
   限定 text 或 JSON、機密內容政策
                |
        私有不可變更快照
                |
   不透明 handle + metadata ------> 主模型上下文
                |
                +-- context_shunt_inspect：精確原文，0 次模型呼叫
                `-- context_shunt_read：單一問題，附驗證引用
```

Manifest 的每個欄位與其中每條路徑都視為不可信輸入。Manifest 只是一組「聲明」，在對檔案完成
重新驗證前都不採信；被拒絕時不會留下 handle，也不會回傳 payload。Import contract 與特定
producer 無關——外部 manifest 透過 translation profile 轉換成核心格式，而未列入部署 allowlist
的 schema 即使已有對應 profile 也一律拒絕。

這**不是** post-tool 攔截，envelope 也把兩者分開：import 回報 `IMPORTED`，永不使用 `SPILLED`。
此功能預設關閉，啟用時必須明確設定 import root 與允許的 producer manifest schema。

## 次要路徑：過大的檔案讀取

原本的 pre-read gate，行為不變。代理直接讀取檔案時往往還不知道答案在哪裡，原文卻已占用主
模型上下文；依目前預設，完整文字讀取可通過 350 個實體行與 16 KiB，超過則在**執行前**阻擋，
並保存被擋下的內容。

```text
host 讀取要求
        |
        +-- 小型或可證明有界 ----------> 原本的 host 工具
        |
        `-- 過大或無法證明有界 ------> 阻擋、保存，取得與上方相同的 handle
```

這是唯一能在操作發生前介入的路徑。它只涵蓋 capability report 明列的 host 工具；若部署環境
要求所有讀取都受控，必須停用未受 context-shunt 管理的原始讀取工具。

## 核心保證

- **沒有提問就不會有摘要。** Reader 只在收到明確問題時執行。系統中沒有任何自動的通用摘要，
  Reader 失敗時也不會退回啟發式摘要。
- **依問題讀取。** 每個實際處理的 chunk 都會收到原始問題。Reader 只能看到已授權的片段，
  不會取得 host 對話或其他工具。
- **Reader 輸出是證據，不是結論。** 回傳內容包含引用、涵蓋範圍、省略項目與可機械核對的
  locator。驗證只能證明引用文字確實存在於所指位置，不能證明它支持該主張；envelope 會如實
  說明，而不暗示已得出結論。
- **引用可核對，涵蓋範圍不隱瞞。** 決定性程式會把每段公開引用與快照比對，並列出未處理或
  省略的 chunk。這只能證明文字確實位於引用位置，不能證明它在語意上支持模型推論。
- **可精確查看原文。** `context_shunt_inspect` 不呼叫模型，可依行號、UTF-8 安全位元組範圍或
  字面搜尋回傳受限的精確內容。
- **快照不可變更且有使用範圍。** `workspace_roots` 與 `artifact_import.roots` 是兩份獨立的
  allowlist，因此中介 producer 的 artifact 不會擴大一般讀取可擷取的範圍；機密路徑／內容、二進位檔案
  與不安全來源一律拒絕。私有 blob 由 TTL 與 session 清理機制移除。Inspect 預算與刪除動作是揭露控制，
  不等於機密保證或安全抹除。
- **Token 帳務如實標示。** Session 記錄會分開計算主模型省下的 token 與 reader 輸入／輸出，
  區分精確值和估算值，也納入實際重試與可用性備援。專案不會把 token 自行換算
  成金額節省。

## 四個唯讀工具

| 工具 | 用途 | 模型呼叫 |
| --- | --- | --- |
| `context_shunt_import` | 接收 producer 已保存的過大工具結果 artifact。回傳 handle 與 metadata，絕不回傳 artifact 內容。 | 0 |
| `context_shunt_read` | 對已授權路徑或既有快照 handle 提問。 | 每個已處理 chunk 至少一次；重試與可用性備援可能增加呼叫次數。 |
| `context_shunt_inspect` | 從快照取得精確行、UTF-8 安全位元組範圍或字面搜尋結果。 | 0 |
| `context_shunt_stats` | 查看目前 session 的有界 token 與揭露帳務。 | 0 |

決定性的逃生門是第一級功能：`inspect` 不需要 provider、不需要 reader 設定、也不呼叫模型，
即使 reader 被停用或缺少 bridge 仍可使用。即使 `reader.enabled` 為 false，adapter 仍可能註冊
reader 工具；真正執行時會在呼叫模型前拒絕。`context_shunt_import` 只在部署已設定
`artifact_import` 且 capability probe 支援時才註冊。

## 一次完整 import

交出 producer 已寫好的 artifact：

```json
{ "manifest_path": "/var/lib/your-compactor/artifacts/q-8412.manifest.json" }
```

回傳 envelope 的部分欄位：

```json
{
  "status": "ok",
  "code": "IMPORTED",
  "answer": "",
  "citations": [],
  "pointer": {
    "source_id": "src_9f2c41b7e0d3a86e",
    "snapshot_id": "sha256:2a97...5aea",
    "bytes": 1048576,
    "internal": true
  },
  "import_receipt": {
    "producer": "your-compactor",
    "manifest_schema": "context_shunt.artifact_import.v1",
    "origin_tool": "log_query",
    "artifact_sha256": "2a97...5aea",
    "bytes": 1048576,
    "upstream_truncated": false
  }
}
```

`artifact_sha256` 是實際讀到的位元組所算出的 digest，而不是 manifest 聲明的值——receipt 存在
時，兩者已被證明相同。

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
| 外部 artifact import | 支援；未設定前不啟用 | 不支援（`IMPORT_UNIMPLEMENTED`：僅有 Python core） |
| 過大讀取的 pre-read gate | 支援 | 支援 |
| 依問題讀取 | 支援；歸屬上限為 `unverified` | 支援；歸屬上限為 `resolved` |
| 精確 inspect 與 session stats | 支援 | 支援 |
| 過大 post-tool 結果攔截 | 不支援 | 不支援 |
| Writer / `propose_patch` | 未實作 | 未實作 |

兩個 adapter 都無法證明 provider-authoritative 的 `actual` 模型身分。OpenClaw 能回報 host
最終選定的 route；Hermes 無法區分 provider 回報與 request echo。

OpenClaw 不支援 `artifact_import`，原因是 TypeScript core 尚未實作 import boundary——這是
專案本身的缺口，而非 host 限制，因此補上時不需要變更 host。

不需要 live provider 的決定性 gates 已實作。先前記錄的真實 host integration evidence 共
122 個案例、0 個失敗。40 題 production-equivalent Luna 評估與 provider benchmark
仍為 `NOT_RUN`；兩個 post-tool gate 也因缺少 host 端必要介面而維持 `NOT_RUN`。`NOT_RUN` 不計為
通過。詳情請見 [capability matrix](docs/capability-matrix.md) 與
[acceptance gates](docs/acceptance.md)。

## Shadow A/B 證明了什麼、沒證明什麼

`./scripts/verify shadow all` 在固定的合成語料上比較四條 lane：raw baseline、啟發式
head/tail compactor 的參考實作、透過 import boundary 加 `inspect` 的決定性檢索，以及依問題
查找的 reader。

其中三個 gate 只靠本 repo 就能實測——主模型上下文 token 縮減（≥ 60%）、相對 raw baseline
沒有證據回歸，以及決定性檢索 lane 的延遲。在目前語料上，檢索 lane 保住了 compactor
從頁面中段丟掉的每一段預期引用。縮減比例只計入實際被中介的項目：其中一個因超過 source cap
而被拒絕的項目占了整體 baseline 的絕大部分，若把它的 counterfactual 也算進去，這個數字就會
變成「對沒有任何 lane 能作答的 payload 省下的量」。

另外五個 gate 回報 `NOT_RUN`，且不會改用無法回答問題的 lane 來計分。Reader lane **無論是否
設定 bridge 都固定回報 `NOT_RUN`**：要對模型 lane 計分，必須有事先固定的語料與門檻，而那是
[`eval luna`](docs/acceptance.md) 所負責的 gate。這五個 gate 分別是：task correctness、
semantic evidence support、mechanical citation validity（檢索 lane 不產生引用，在該 lane
計分會得到毫無意義的 100%）、follow-up 比率，以及 net cost reduction——後者還需要本 repo
所沒有的版本化價目表。

以上結果都不構成替換線上 compactor 的依據。此中介層是附加功能；推進順序與各階段所需證據
記錄於 [acceptance gates](docs/acceptance.md#what-has-to-be-true-before-the-live-compactor-is-replaced)。

## Reader 無法回答時

模型錯誤、配額不足、逾時、格式錯誤或無效引用都會安全拒絕。Handle 與有界的 inspect 路徑
仍然保留；原本過大的內容不會因此回到主模型上下文，也沒有可退回的啟發式摘要。

- 沿用回傳的 handle，把問題問得更精確；不可變更快照不必重新擷取。
- 需要原文時，用 `context_shunt_inspect` 指定範圍或字面搜尋。
- 採用答案前先看 `coverage`。上游截斷、期限或限制造成的缺漏都會明確標為 partial。

## 設定與帳務

Reader 預設模型為 `gpt-5.6-luna`；`reader.model` 與 `reader.provider` 都可設定，provider 留空時
交由 host routing。`fallback_chain` 只處理可用性，不會補救品質不佳的答案。
Python/Hermes 可用性重試全部失敗後，先嘗試有界、確定性的 legacy compaction，
明確標示為部分結果且非模型摘要。停用此功能或無法安全產出時，才自動回傳第一個來源的精確 UTF-8 位元組前綴（2 KiB，
最多 4 KiB），明確標示為 escape hatch，並非 LLM 摘要。此擷取不依問題或 reader selector
選文，且沿用 inspect 的累計揭露與輸出限制；`reader.automatic_extract: false` 或
`inspect.enabled: false` 可停用，`reader.fallback_max_bytes` 可調整上限。
格式錯誤、引用無效、取消及有效但較弱的答案不會觸發。詳見
[設定與相容性](docs/configuration.md#automatic-exact-extraction-after-reader-unavailability)。所有數值 limits
只能縮小，不能放寬。

`artifact_import` 預設關閉，且沒有預設的 root 與 producer schema；只開啟旗標而未同時設定
兩者會被視為設定錯誤，而不是放行全部。

[設定參考](docs/configuration.md)整理 host policy、deadline、TTL、store、揭露、concurrency、
retry、import 與 envelope limits；[metrics 說明](docs/metrics.md)定義主模型上下文節省、reader usage、
估算方式、重試計算及 session scope。

## 文件索引

| 主題 | 文件 |
| --- | --- |
| 設計與信任邊界 | [Architecture](docs/architecture.md)、[安全性](docs/security.md)、[已知限制](docs/limitations.md) |
| Host 支援與 release evidence | [Capability matrix](docs/capability-matrix.md)、[acceptance gates](docs/acceptance.md)、[開發狀態](docs/implementation-plan.md) |
| 設定與維運 | [設定參考](docs/configuration.md)、[metrics](docs/metrics.md)、[安裝指南](docs/install.md) |
| Import contract | [`artifact-import.schema.json`](contracts/v1/artifact-import.schema.json) 與其[一致性語料](contracts/v1/conformance/artifact-import-cases.json) |
| 公開 contracts 與儲存格式 | [版本化 contracts](contracts/v1/)、[SQLite DDL](contracts/store/v1.sql) |
| 實作 | [Python core](packages/core-py/)、[TypeScript core](packages/core-ts/)、[Hermes adapter](adapters/hermes/)、[OpenClaw adapter](adapters/openclaw/) |
| 評估與驗證 | [Evaluation corpus](evals/)、[shadow A/B 語料](evals/shadow/corpus.json)、[`scripts/verify`](scripts/verify) |

## 參與開發

送出變更前請執行不需 live provider 的檢查：

```bash
./scripts/verify unit all
./scripts/verify packaging all
./scripts/verify benchmark core
./scripts/verify shadow deterministic
npm run typecheck --workspaces --if-present
git diff --check
```

這些命令不會把缺少的 live-model evidence 算成通過。Host integration 與 live evaluation 的要求
記錄在 [acceptance guide](docs/acceptance.md)。

## 授權與致謝

本專案採用 [Apache-2.0](LICENSE) 授權。第三方元件與授權資訊列於
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
