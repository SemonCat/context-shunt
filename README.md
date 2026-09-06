# context-shunt

將大型工具資料留在主代理 context 之外，以問題導向的 reader 回傳可驗證答案與引用。此 repo 目前只有規格，尚未實作或通過測試。

## v1 範圍

- **Read-only**：Hermes 與 OpenClaw 各有一個 adapter，共用 JSON contract 與安全核心。
- **Large local read gate**：超過 350 行的 full Read 與未受限的 `cat` 類讀取，在工具執行前阻擋並提供 reader 指引；受限 offset/limit/search 可放行，但仍須符合輸出與安全上限。
- **Question-driven reader**：每次呼叫必須帶 question，固定使用 `gpt-5.6-luna`，只回傳受限答案、已驗證的行／record 引用與 coverage。
- **Optional Suma mode**：oversized MCP 結果使用純 spill/pointer；替換舊 heuristic compactor，包含 dict/list，預設停用。
- **Code-writer**：僅保留 future optional contract 的設計邊界；v1 不註冊工具、不授予寫入權限，預設禁用。

Read-only 指不修改使用者來源或執行寫入型業務工具；受控 spill、暫存與不含內容的 metrics 可由核心寫入。

## 文件

1. [架構與資料契約](docs/architecture.md)
2. [給 Opus 的逐步實作工作單](docs/implementation-plan.md)
3. [Unit / integration / eval / benchmark 驗收](docs/acceptance.md)
4. [第三方來源與授權說明](THIRD_PARTY_NOTICES.md)

## 已採用的查證基線

以下是使用者提供、另一位 Astra 已完成的查證，本輪未重新研究 upstream：官方 `spotify/portal-ai-plugins` 的 `main` 分支 `plugins/shunt` 為 Apache-2.0，當時 51 tests 全過；它使用 pre-read gate、放行 targeted reads，並以每次帶 question 的 bulk-reader 呼叫廉價模型。

Hermes 提供 `pre_tool_call`、`transform_tool_result`、`ctx.llm`，但 transform 例外會 fail-open。OpenClaw 提供 `before_tool_call`、tool-result persistence/middleware、runtime llm；**middleware 前是否已有 truncation 尚待實作階段驗證**。這些是整合前提，不代表本專案已經完成 host 相容性或安全驗證。

## 實作原則

來源內容不可信。Gate、spill 與 citation verification 由確定性程式負責，reader 不能決定權限或改寫檔案。失敗不得將被攔截的大型原始資料送回主代理；無法證明 host 能保障這點的模式不得啟用。

實作語言依 host SDK 接入需求決定；JSON Schema 是跨 adapter 的相容性邊界。所有下列數值是 v1 設計預設，需由 acceptance gates 驗證，不是已量測結果。安裝與執行命令須在實作後補上，不提供未存在的 CLI。

## 授權

本專案採 [Apache License 2.0](LICENSE)。第三方程式碼與修改紀錄須依 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 保存。本輪不建立遠端、不 push。
