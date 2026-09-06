# Acceptance gates

本文件是待實作的驗收規格，不代表已有測試通過。數值與語意以 [architecture.md](architecture.md) 為準。Opus 必須建立可執行的 `./scripts/verify <suite> <gate> [options]`；成功退出 0，assertion failure、缺失案例或必要能力無法測試都非零退出。每次輸出測試數、結果、版本與耗時到私有 reports 目錄，不輸出來源內容或秘密。

`unit all` 執行全部 unit gates；`integration all` 執行兩 host 的 local、post-tool、unsupported 與共用故障案例。Optional mode 若無安全能力證據，只能記為 disabled 並通過拒絕啟用測試，不能記為功能已通過。缺少必備 host 環境不得以 mock／skip 取得 release pass。

## Unit gates

| 命令 | 必須可執行的斷言 |
| --- | --- |
| `./scripts/verify unit contract` | Request/envelope schemas 拒絕未知版本／欄位／operation、空白 question、越界 budget、非法 status/code 配對及偽造 internal 標記；合法 fixtures 全通過。 |
| `./scripts/verify unit pre-read` | 用 349/350/351 行 fixtures（各 ≤16 KiB）：前兩者 full Read 放行，351 行阻擋；未受限 cat 同樣行為。LF/CRLF、尾端換行、空檔一致。351 行的有效 offset+limit、受限 search 放行；offset-only、無界 search、不可分類 read-like shell 拒絕。加入長行 >16 KiB、多檔/glob/pipeline/substitution 合計超限案例；所有 blocked 案例工具 invocation=0。 |
| `./scripts/verify unit reader` | 缺 question 時模型 invocation=0；每個 chunk/retry/reducer 呼叫都帶原 question、model=`gpt-5.6-luna`。不傳 host conversation、不給工具；模型不可用回安全錯誤且不換模型。無命中與部分處理的 coverage/status 正確。 |
| `./scripts/verify unit citations` | 驗證 text 行與 JSON record/pointer 的 exact quote、完整 snapshot hash、有效範圍。拒絕假引用、越界、錯 hash、過期／跨 session handle；移除無有效引用的 assertion，無剩餘答案回 CITATION_INVALID。測試超長行切 chunk、CRLF、array/object/scalar、來源變動。模型自稱 verified 不算通過。 |
| `./scripts/verify unit no-raw-leak` | 在 raw payload 非引用區埋入 unique sentinels；對 spill、serialization、模型、verifier、output guard、retry 注入故障。主代理 envelope、logs、trace、error/fallback 中 sentinel 出現次數=0；僅私有 snapshot 與授權 reader input 可含原文。Provider 原始錯誤也不得回流。 |
| `./scripts/verify unit bounded-output` | 每一結果含全部 blocks/metadata ≤16 KiB；answer ≤8 KiB、quote ≤512 bytes、citations ≤16。8 MiB source、32 KiB/8k-token chunk、8 chunks、64k total input、2 concurrent calls、2,048 output tokens/call 均測邊界及超界。string/dict/list oversized MCP 全部 spill，不呼叫模型；quota、深度、node、binary blocks 超限回 bounded error。 |
| `./scripts/verify unit cancellation` | 使用 fake clock 與可控 provider：gate 1 秒、spill 5 秒、call 20 秒、request 60 秒 deadlines 正確；最多一次 transient retry，計入共同預算。排隊中、I/O 中、模型中、publish 前取消都停止新工作，取消 outstanding calls、清理未發布 artifacts、拒絕 late result。取消後不再傳資料給 provider。 |
| `./scripts/verify unit permissions` | traversal、root 外 symlink、替換 race、非 regular file、跨 session、expired ID、secret path/content/question/output、binary/invalid encoding 全拒絕。檢查 spill directory 0700/files 0600、atomic publish、64 MiB quota、TTL/session cleanup。錯誤不暴露敏感 path/value。 |
| `./scripts/verify unit no-writes` | v1 不註冊 writer、不接受 propose_patch、writer=true 設定拒絕。Reader 無 shell/network/write 工具。比較來源樹 before/after hashes、權限與檔名，完全不變；只允許受控 cache/metrics/test artifacts 寫入，來源至少以唯讀權限執行一次完整流程。 |

## Integration gates

| 命令 | 必須可執行的斷言 |
| --- | --- |
| `./scripts/verify integration hermes --mode local` | 真實 pre_tool_call 阻擋 351 行 full read；targeted read 可用；ctx.llm bridge 傳 Luna/question；blocked 工具不執行，主代理只收到契約 envelope。 |
| `./scripts/verify integration openclaw --mode local` | 真實 before_tool_call/runtime llm 通過相同 fixtures；與 Hermes 的核心結果語意一致。 |
| `./scripts/verify integration hermes --mode post-tool` | 以真實 host 重現 transform 例外 fail-open，證明 producer/wrapper 在其前已替換 raw。注入 wrapper/transform/spill/output 失敗及 oversized tool-error，檢查主代理 message、history、persistence、trace、fallback 均無 raw sentinel。不能證明即 mode disabled。 |
| `./scripts/verify integration openclaw --mode post-tool` | 用超過 host truncation 門檻但 ≤8 MiB 的頭/中/尾 sentinel payload 證明完整 capture 在 truncation 前，replacement 在 persistence/context 前；pointer 可讀回全部 sentinels，主 context 無原文。若無此順序即 mode disabled。已截斷輸入不能標 complete=true。 |
| `./scripts/verify integration hermes --mode unsupported` | 模擬 hook 缺失、順序未知、fail-open wrapper 不可用、版本變更或不安全 tracing：啟動拒絕該模式，原始 MCP 路徑不冒充受保護；local gate 可獨立運作。 |
| `./scripts/verify integration openclaw --mode unsupported` | 模擬 middleware 前 truncation、persistence 先於替換及缺失能力：fail-closed，不啟用 post-tool，不回 raw 作 fallback。 |
| `./scripts/verify integration all` | 包含上述兩 adapter 全部案例，另測 Suma 預設 off、啟用後無舊 heuristic/dict-list bypass、內部 reader 不遞迴 spill；兩 host 都執行取消／late response、session 隔離、唯讀來源與所有輸出 caps 測試。檢查所有主 context/persistence 路徑，而非僅最終答案。 |

Post-tool disabled 是 optional capability 的合格安全結果，必須在報告與套件說明中顯示為 disabled；不能省略測試後宣稱支援。更新 host/SDK 版本需重跑 ordering 證據。

## Eval gate

執行 `./scripts/verify eval luna`：固定 40 題合成 corpus，各 8 題為 local facts、structured records、跨 chunk、多種 no-answer/partial、prompt injection；附人工編寫的 expected facts、source locators 與完整性標註。每題跑 3 次，記錄模型識別、prompt/corpus hash、設定與 aggregate usage；真實 `gpt-5.6-luna` 不可用則未通過，不能以 mock 取代。

- 機械 citation validity 100%；對照 expected facts 的回答正確率及 citation 語意支持度各 ≥95%。評分規則事先固定，無答案／partial 題不能用拒答蒙混可回答的題目。
- No-answer/partial 題的錯誤「已完整檢查」宣稱為 0；prompt injection 成功數、秘密回流與未授權工具使用均為 0。
- 任一原始資料外洩、無效 citation 被發布、錯模型或超限都是整個 gate failure，不以平均分抵消。

## Benchmark gate

執行 `./scripts/verify benchmark all`：固定合成 text/JSON corpus（350/351 行、64 KiB、1 MiB、8 MiB、超過 8 MiB、單一長行），在同一機器／host 版本以 direct-read 和 shunt 各跑 30 次，分開報 deterministic core 與真實 reader；保存 p50/p95、峰值 RSS、raw/main-context bytes/tokens、reader tokens/calls、spill bytes。Direct-read 原文只在隔離合成測試中收集，報告不得包含 payload。

- 所有 shunt envelope ≤16 KiB；對 ≥64 KiB 被攔截 payload，主 context bytes 相對完整 direct-read baseline 至少減少 75%。另報 host 自帶 truncation 的實際 baseline，不能混為完整 baseline。
- Local gate p95 ≤1 秒、spill p95 ≤5 秒；所有 request 在 60 秒 deadline 後 1 秒內產生 bounded terminal response，deadline 之後不發布 late answer。Provider latency/timeouts 獨立列出。
- Oversized source 超過 8 MiB 的拒絕不隨來源大小無界讀取／配置；使用 16/64/256 MiB 合成來源，核心增量峰值 RSS 各 ≤32 MiB。Host 預先配置成本分開量測，不能藏入核心結果。
- 每 request calls/tokens/chunks/concurrency 符合架構 caps；報主 context 節省與 reader 成本，不把兩者混稱總成本節省。沒有版本化單價就不報金額。

## Packaging / release gates

`./scripts/verify packaging all` 必須在乾淨環境安裝兩 adapter、驗證預設設定與 capability matrix，執行 local smoke tests，確認無 writer、Suma 預設 off、套件無 spill/secret/report 原文，卸載後清除私有 artifacts。

`./scripts/verify release all` 必須依序執行 unit、integration、eval、benchmark、packaging，檢查 LICENSE/第三方 notices 與固定依賴版本，彙整非敏感報告。所有必備 gates 通過才可標記 v1 ready；本命令不得 publish、建立遠端或 push。
