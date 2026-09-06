# Third-party notices

## Spotify Shunt

- 來源識別：`spotify/portal-ai-plugins`，`main` 分支，`plugins/shunt`。
- 授權：Apache License 2.0；本 repo 的 [LICENSE](LICENSE) 包含完整授權文本。
- 關係：本規格採用其 pre-read gate 與 question-driven bulk-reader 設計基線。
- 查證來源：使用者提供之 Astra 查證結果；當時 upstream 51 tests 全過。本輪沒有重新查詢 upstream，也沒有匯入第三方程式碼。
- 精確 revision：本輪未提供 commit SHA；不得把可變的 `main` 當作可重現版本，也不得將該測試結果宣稱為本 repo 的結果。

實作若複用 upstream 程式碼，匯入者必須在首次匯入時填入實際 commit SHA、來源檔案與本地對應路徑，保留原始 copyright／attribution／license headers，在修改檔案標示修改，並複製該匯入版本適用的 NOTICE 內容。這是匯入工作的一部分，不要求 plan writer 再做 upstream 研究。

## Host 與服務邊界

Hermes、OpenClaw、Suma 與 `gpt-5.6-luna` 在本規格中作為整合介面或服務名稱使用。本輪未附帶其 SDK、程式碼或模型權重，亦未宣稱它們的授權。實作新增依賴時須記錄 package、固定版本、授權、來源及需要散布的 notices。

## 匯入紀錄格式

目前無程式碼匯入紀錄。首次匯入時新增以下欄位，禁止以推測補齊權利人或 NOTICE：

| 元件／版本或 commit | 原始路徑 → 本地路徑 | 授權及原始 notices | 本地修改與日期 |
| --- | --- | --- | --- |

本文件不取代第三方原始授權或其必須保存的 NOTICE。
