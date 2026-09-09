# context-shunt

[English](README.md) | [繁體中文](README.zh-TW.md)

把過大的檔案與工具結果留在主模型上下文之外。context-shunt 保存不可變更的快照，回傳不透明
handle。主模型再將 handle 與明確問題傳給成本較低的 reader。答案附有證據、涵蓋範圍與
經程式驗證的引用。

> **預發行、唯讀。** 依操作人員回報，Hermes 部署已完成原子切換，啟用 `tool_result_capture`
> 與內建 legacy fallback；獨立的 `oversize-tool-result-compactor` plugin 已停用。
> 新安裝預設仍關閉 capture，必須先確認 host 的執行順序。OpenClaw 支援本機讀取前防護，
> 不支援工具結果擷取。reader 實測與 provider 基準測試的驗收關卡仍為 `NOT_RUN`。

## 運作方式

### 本機檔案：先攔截，再讀取

讀取前閘門與帶著問題閱讀的流程，設計靈感來自 Spotify Portal/Shunt
（[設計來源](THIRD_PARTY_NOTICES.md)）。預設情況下，完整文字讀取必須同時符合兩項上限：
350 個實體行與 16 KiB。過大或無法證明範圍受限的讀取會在執行前遭攔截；安全的來源會保存下來，供 reader
依問題閱讀。小型或可證明範圍受限的讀取可繼續使用 host 原有工具。

Hermes 攔截 `read_file`、`search_files` 與 `terminal`；OpenClaw 涵蓋
`read` 與 `exec`，未註冊搜尋工具供閘門攔截。這不代表所有讀取工具都受保護；若需要完整防護，
必須停用未受控的工具。

### Hermes 工具結果：先擷取，再提問

`tool_result_capture` 在 `transform_tool_result` 攔截符合條件的過大 MCP／工具結果，
此時結果尚未進入主模型上下文。它將收到的完整內容保存為不可變更的
artifact，以有大小上限的不透明 handle／pointer 和 metadata 取代原結果。
擷取過程**不呼叫模型**，也不產生啟發式摘要。

這個 hook **收不到使用者的問題**。閱讀是另一個步驟：主模型必須呼叫
`context_shunt_read`，傳入明確問題與 artifact handle。部署範例的 reader
使用 `gpt-5.6-luna`；使用者可自行設定模型與 provider。

```text
符合條件的過大工具結果                    過大的本機讀取
              |                                |
     tool_result_capture                  pre-read gate
              |                                |
              +--------- 不可變更快照 ----------+
                              |
                    有界 handle → 主模型
                              |
              明確問題 + handle → context_shunt_read
                              |
                 reader → 證據／涵蓋範圍／定位資訊
                              |
                         程式驗證引用
                              |
                    主模型可檢視有界來源範圍
```

Hermes hook 目前擷取過大的**字串**結果；結構化／多模態區塊直接放行。它無法還原 producer
事先截掉的內容。Hook 順序僅在一台 Hermes 0.21.1 host 上確認過，其他安裝環境仍需驗證。
新部署必須自行確認順序，並同時設定 `tool_result_capture.enabled: true` 與
`tool_result_capture.host_ordering_verified_locally: true`。符合條件的過大結果若擷取失敗，
adapter 只回傳有界失敗訊息，不會以原始結果作為 fail-open 備援。
詳見[能力證據](docs/capability-matrix.md)與[切換程序](docs/acceptance.md#tool_result_capture-cutover-on-hermes)。

`suma_post_tool` 只是已棄用的設定遷移別名，並非產品名稱。
公開設定請使用 `tool_result_capture`。

### 既有 artifact：不經攔截也能匯入

Hermes 的 `context_shunt_import` 可接收其他 producer 已保存的文字／JSON artifact。
建立私有快照前，會驗證 manifest、允許的根目錄、一般檔案類型、大小、雜湊與機密政策。
它回傳 `IMPORTED`，而非 `SPILLED`；匯入不代表具備工具執行後攔截能力。此功能預設關閉，
需要明確設定 `artifact_import.roots` 與允許的 producer schemas。OpenClaw 尚未實作匯入。

## 提問與檢視

以擷取或匯入回傳的 handle 呼叫 `context_shunt_read`；請將下列示意識別值換成實際值：

```json
{
  "question": "重試次數的上限是多少？",
  "handles": [{
    "source_id": "src_example1234",
    "snapshot_id": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }]
}
```

初次擷取本機檔案時，改用 `"paths": ["/workspace/service/retry.py"]`，不要同時傳入 `handles`。
reader 處理每個 chunk 時，都會收到問題與已授權的摘錄，不會取得 host 對話或工具。回傳答案包含引文、
涵蓋範圍、遺漏項目與定位資訊。程式會對不可變更快照逐位元組驗證引文；這**不能證明**引文足以
支持 reader 的推論。採信答案前，請先確認涵蓋範圍是否完整，以及上游是否截斷內容。

| 工具 | 用途 | 模型呼叫 |
| --- | --- | --- |
| `context_shunt_read` | 針對已授權路徑或快照 handle 提問。 | 每個處理的 chunk 都會呼叫；重試與模型備援可能增加次數。 |
| `context_shunt_inspect` | 精確行範圍、UTF-8 安全的 byte 範圍，或字面搜尋結果。 | 零 |
| `context_shunt_stats` | 有界的 session token 與揭露量統計。 | 零 |
| `context_shunt_import` | 接收 producer 已保存的 artifact（僅 Hermes，需設定）。 | 零 |

`inspect` 獨立於 reader，沒有 provider 也能使用，但仍受設定、單次與累計揭露預算，以及
handle 有效性限制。快照不可變更且限定於 session；TTL 與 session 清理限制保存時間。
Workspace 與 import 根目錄使用不同白名單。不安全、含機密或二進位來源會遭拒；清理不保證
安全抹除。參見[工具 schema](contracts/v1/tool-args.schema.json)。

## Reader 失敗時

在 Python／Hermes 上，reader 重試與模型備援耗盡後，符合條件的 `MODEL_ERROR`、`TIMEOUT`
或 `CITATION_INVALID` 會觸發 **context-shunt 內部**移植的有界 legacy compactor
（`reader.legacy_compaction` 預設為 `true`）。模型身分不符與 provenance 政策拒絕不適用。
它會針對請求中的第一個來源，以訊號行、頭尾取樣、合併重複行與整理 JSON 產生決定性啟發式摘要，
不是 Luna 的答案，也不是精確來源範圍。

回應的 envelope 標示 `status: partial`、`code: LEGACY_COMPACTED`、
`result_kind: legacy_compaction` 與 `provenance.derived: false`。摘要放在
`legacy_compaction`，`answer` 與 `citations` 留空；涵蓋範圍仍標為部分，失敗的模型嘗試
仍列入統計。Hermes 切換後，由此內建備援取代獨立 legacy plugin。

若 compaction 停用或無法安全回傳，而 reader 完全無法使用，可在相關設定啟用時，
改用第二層受防護的精確前綴擷取。若兩層都無法安全回傳，回應只包含有界 pointer／失敗訊息
與復原指引，絕不放行過大的原始內容。可用有效 handle 縮小問題重問，或檢視有界範圍。
TypeScript／OpenClaw 支援精確擷取層，但尚未移植 legacy compaction。
詳見[備援語意與限制](docs/configuration.md#legacy-compaction-fallback-for-reader-outcomes-automatic-extraction-does-not-cover)。

## 快速開始

使用 Python 3.11+ 與 Node 22.22.3+。在現有 checkout 執行：

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

將 [Hermes 範例](examples/config/hermes.config.yaml) 合併至 `~/.hermes/config.yaml`，設定
`workspace_roots`，在 plugin 的 `llm` 政策授權 reader 模型／provider，然後重啟 Hermes。
`auxiliary.context_shunt_reader` 會覆寫 plugin reader 預設值；`auto` 表示繼承。
範例保持 capture 關閉，直到你確認本機 hook 順序。遷移時請依上述原子切換程序操作，
確保停用獨立 compactor 時 capture 已生效。

### OpenClaw

```bash
npm run build --workspace @context-shunt/core
openclaw plugins install --link ./adapters/openclaw --force
openclaw plugins enable context-shunt
```

將 [OpenClaw 範例](examples/config/openclaw.json) 合併至 `openclaw.json`，設定
`workspace_roots`，並在相鄰的 `llm` 政策授權 reader 目標，接著重啟 Gateway 並檢查載入結果：

```bash
openclaw plugins inspect context-shunt --runtime --json
```

OpenClaw 目前缺少對等的「工具執行後、進入上下文前」攔截介面：較早的 hook 只能觀察，
持久化 hook 則只能看到已被限縮的結果。即使設定要求啟用，`tool_result_capture` 仍然
**不受支援**，不會擷取任意過大的 MCP／工具輸出。參見[安裝與清理](docs/install.md)。

## Host 支援與如實統計

| 能力 | Hermes | OpenClaw 2026.9.2 |
| --- | --- | --- |
| 本機 pre-read gate、reader、精確 inspect、session stats／生命週期 | 支援（相容性基準為 0.18.2） | 支援 |
| 工具結果擷取 | 回報的 0.21.1 部署已啟用；預設關閉，需操作人員聲明已確認本機順序 | 不支援 |
| 外部 artifact 匯入 | 支援；設定前關閉 | 不支援（`IMPORT_UNIMPLEMENTED`） |
| 內建 legacy compaction | 支援，為預設 reader 失敗備援 | 尚未實作 |
| Reader 歸屬證據上限 | `unverified` | `resolved` |
| Writer／`propose_patch` | 尚未實作 | 尚未實作 |

兩個 adapter 都無法提供由 provider 確認的 `actual` 模型身分證據。請求指定的模型、解析後的路由
與 provider 確認的身分是不同資訊；回傳請求中的模型名稱，不能證明實際使用的模型身分。
模型／provider 設定與可用性備援都有明確紀錄，模型不符時會拒絕。

Token 統計分開記錄主上下文節省量與 reader 輸入／輸出，標示精確值或估算值，並包含重試與
備援嘗試。Token 減量不等於金額節省。Spotify 回報的節省量是靈感，**不是本專案實測保證**。
決定性 shadow corpus 只衡量有限的檢索路徑；與正式環境等效的 Luna 評估及 provider 基準測試
仍為 `NOT_RUN`。已部署的擷取路徑尚未通過這些驗收關卡。
詳見[指標](docs/metrics.md)、[能力矩陣](docs/capability-matrix.md)與[驗收關卡](docs/acceptance.md)。

## 文件與貢獻

- [架構](docs/architecture.md)、[安全](docs/security.md)與[限制](docs/limitations.md)
- [設定](docs/configuration.md)、[範例](examples/config/README.md)與[安裝](docs/install.md)
- [版本化契約](contracts/v1/)、[Python core](packages/core-py/) 與 [TypeScript core](packages/core-ts/)
- [Hermes adapter](adapters/hermes/)、[OpenClaw adapter](adapters/openclaw/) 與[評估 corpus](evals/)

提出變更前，請執行決定性檢查：

```bash
./scripts/verify unit all
./scripts/verify packaging all
./scripts/verify benchmark core
./scripts/verify shadow deterministic
npm run typecheck --workspaces --if-present
git diff --check
```

採用 [Apache-2.0](LICENSE) 授權。設計來源與相依套件授權見
[第三方聲明](THIRD_PARTY_NOTICES.md)。
