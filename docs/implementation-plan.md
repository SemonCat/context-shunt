# Implementation plan for Opus

依 [architecture.md](architecture.md) 實作；本文不重開架構決策。v1 read-only，reader 固定 `gpt-5.6-luna`，Suma post-tool mode 預設停用，writer 不實作。以下命令是實作時必須建立的驗收介面，目前尚不存在。

逐項完成後記錄變更、執行命令與結果；失敗先修復，不能標記完成。順序為 shared core → Hermes → OpenClaw → tests/evals → packaging。每項保留小幅可驗證變更，不建立遠端或 push。

## 1. Shared core

- [ ] **1.1 契約與測試入口**（無依賴）：建立 `contracts/v1/` request/envelope JSON Schemas、合法／非法 fixtures、`scripts/verify` 驗收入口。採 host 所需語言，固定依賴版本；測試缺失或零案例必須非零退出。完成條件：版本、enum、欄位與 budgets 驗證符合架構。驗證：`./scripts/verify unit contract`。
- [ ] **1.2 授權與 snapshot**（依賴 1.1）：建立 root/path policy、session registry、immutable snapshot 與 line/record index；涵蓋 secret、binary、symlink/race、TTL 與私有 spill 權限。完成條件：不安全來源無法進入 reader，引用定位綁定 snapshot。驗證：`./scripts/verify unit permissions`、`./scripts/verify unit citations`。
- [ ] **1.3 Pre-read gate**（依賴 1.2）：實作 full Read、支援的 shell AST 分類及 targeted read/search 預算，超過 350 行在執行前阻擋；不明 read-like 命令拒絕。完成條件：blocked 路徑的原始工具執行次數為零。驗證：`./scripts/verify unit pre-read`。
- [ ] **1.4 Luna reader**（依賴 1.2）：每次模型呼叫帶原 question，使用 `gpt-5.6-luna`；實作 chunk budgets、citation verifier、coverage、partial/error、deadline/cancellation。完成條件：只輸出驗證過且有界的答案，失敗不換模型或回 raw。驗證：`./scripts/verify unit reader`、`./scripts/verify unit citations`、`./scripts/verify unit cancellation`。
- [ ] **1.5 Spill 與輸出邊界**（依賴 1.2、1.4）：建立 optional Suma 純 spill/pointer，涵蓋 string/dict/list/所有 blocks；替換舊 heuristic 路徑。加入 final output guard、固定安全錯誤與無內容 metrics。完成條件：spill 成功才發布 pointer，故障無 raw fallback。驗證：`./scripts/verify unit bounded-output`、`./scripts/verify unit no-raw-leak`。

Checkpoint：`./scripts/verify unit all` 必須通過，再接 host；mock 測試不能作為 host interception 證據。

## 2. Hermes adapter

- [ ] **2.1 Gate 與 reader bridge**（依賴 1）：接入 `pre_tool_call`、`ctx.llm`，正規化 tool args/result，註冊 read-only reader，產生版本化 capability report。完成條件：host 真實工具路徑通過 gate，模型固定 Luna。驗證：`./scripts/verify integration hermes --mode local`。
- [ ] **2.2 安全 post-tool 邊界**（依賴 2.1）：先測 `transform_tool_result` fail-open，再以受控 producer/wrapper 在 raw 進入 fallback 前完成替換；注入 transform/wrapper/spill 例外。完成條件：有證據才允許 Suma mode；否則拒絕啟用並保留可用的 local gate。驗證：`./scripts/verify integration hermes --mode post-tool`、`./scripts/verify integration hermes --mode unsupported`。

Checkpoint：保存 host/SDK 版本、事件序與 sentinel 測試結果；不得以 try/catch 宣稱解決 host fail-open。

## 3. OpenClaw adapter

- [ ] **3.1 Gate 與 reader bridge**（依賴 1、2 checkpoint）：接入 `before_tool_call`、runtime llm，重用 schemas/core fixtures，產生 capability report。完成條件：與 Hermes 相同 contract 與錯誤語意。驗證：`./scripts/verify integration openclaw --mode local`。
- [ ] **3.2 驗證 capture/replacement 順序**（依賴 3.1）：以頭／中／尾 sentinel 記錄 capture、truncation、middleware、persistence、context insertion 順序。完成條件：完整 capture 在 truncation 前，安全替換在 persistence/context 前；無安全位置即拒絕 post-tool mode，不把截斷內容標完整。驗證：`./scripts/verify integration openclaw --mode post-tool`、`./scripts/verify integration openclaw --mode unsupported`。

Checkpoint：兩 adapter 共用 conformance fixtures。未證實安全的模式維持 disabled，capability report 明確列出原因。

## 4. Tests、evals 與 benchmarks

- [ ] **4.1 Regression 與 fault injection**（依賴 2、3）：完成 [acceptance.md](acceptance.md) 全部 deterministic gates，覆蓋 host error/fallback、跨 session、取消、超限與 v1 no-writes。完成條件：所有必須支援的模式通過；不支援模式必須通過 fail-closed 測試。驗證：`./scripts/verify unit all`、`./scripts/verify integration all`。
- [ ] **4.2 Luna evals**（依賴 4.1）：建立固定有答案、無答案、跨 chunk、record citation、prompt injection、partial coverage corpus；使用真實 Luna，保存不含敏感內容的分數與 usage。完成條件：符合 acceptance 門檻；provider 不可用不得算 pass。驗證：`./scripts/verify eval luna`。
- [ ] **4.3 成本與效能**（依賴 4.2）：對固定 corpus 比較 direct-read 與 shunt，分開計主 context、reader usage、latency、memory、spill；不捏造價格。完成條件：符合 acceptance benchmark 門檻。驗證：`./scripts/verify benchmark all`。

## 5. Packaging

- [ ] **5.1 打包與設定**（依賴 4）：提供兩 adapter 安裝包與範例設定，Suma=false、writer unavailable；預設 caps 符合架構。補 README 實際安裝／驗證命令、支援 host 版本與模式矩陣。完成條件：乾淨環境可安裝、載入、卸載並清理私有 artifacts。驗證：`./scripts/verify packaging all`。
- [ ] **5.2 Release review**（依賴 5.1）：保留 LICENSE；如匯入第三方程式碼，記錄實際 revision、檔案映射、修改與原始 notices。排除 spill、secret、fixtures 原始敏感資料與本地報告。完成條件：交付 artifacts、驗收結果、已停用模式及限制；不宣稱 upstream 51 tests 是本 repo 測試。驗證：`./scripts/verify release all`。

Release 不需支援無法安全攔截的 optional post-tool mode，但必須明確停用且通過 unsupported gate；兩 adapter 的 local gate 與 reader 都是 v1 必備。
