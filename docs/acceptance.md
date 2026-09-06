# Acceptance gates

本文件是 normative 驗收規格，不是目前 checkout 的通過紀錄。數值與語意以 [architecture.md](architecture.md) 為準。可執行入口是 `./scripts/verify <suite> <gate> [options]`；成功退出 0，assertion failure 或缺失案例退出 1，缺少 live host/model prerequisite 則明確 `NOT_RUN` 並退出 2。每次輸出測試數、結果、版本與耗時到 gitignored `reports/`，不輸出來源內容或秘密。

`unit all` 執行全部 deterministic unit gates。現有 `integration all` 會列舉兩 host 的 local、post-tool、unsupported：local 使用真實 host，unsupported 是 deterministic fail-closed gate，post-tool 因 host 能力不足而 `NOT_RUN`；共用故障、取消與輸出上限案例屬 unit gates，不能冒充 host runtime 測量。Optional mode 若無安全能力證據，只能記為 disabled，不能記為功能已通過。缺少必備 host 環境不得以 mock／skip 取得 release pass。

## Unit gates

| 命令 | 必須可執行的斷言 |
| --- | --- |
| `./scripts/verify unit contract` | Request/envelope schemas 拒絕未知版本／欄位／operation、空白 question、越界 budget、非法 status/code 配對及偽造 internal 標記；合法 fixtures 全通過。 |
| `./scripts/verify unit pre-read` | 用 349/350/351 行 fixtures（各 ≤16 KiB）：前兩者 full Read 放行，351 行阻擋；未受限 cat 同樣行為。LF/CRLF、尾端換行、空檔一致。351 行的有效 offset+limit、受限 search 放行；offset-only、無界 search、不可分類 read-like shell 拒絕。加入長行 >16 KiB、多檔/glob/pipeline/substitution 合計超限案例；所有 blocked 案例工具 invocation=0。 |
| `./scripts/verify unit reader` | 缺 question 時模型 invocation=0；每個 chunk/retry/fallback 呼叫都帶原 question 與設定的模型。不傳 host conversation、不給工具；模型不可用回安全錯誤且不換模型。無命中與部分處理的 coverage/status 正確。歸屬無法證明時標為 `unverified` 而非 `actual`；`require_match` 政策改為拒絕並回 `PROVENANCE_UNAVAILABLE`；回報值與請求矛盾一律 `MODEL_ERROR`。Reader 失敗保留有效 handle 並給出決定性 recovery。 |
| `./scripts/verify unit citations` | 驗證 text 行與 JSON record/pointer 的 exact quote、完整 snapshot hash、有效範圍。拒絕假引用、越界、錯 hash、過期／跨 session handle；移除無有效引用的 assertion，無剩餘答案回 CITATION_INVALID。測試超長行切 chunk、CRLF、array/object/scalar、來源變動。模型自稱 verified 不算通過。 |
| `./scripts/verify unit no-raw-leak` | 在 raw payload 非引用區埋入 unique sentinels；對 spill、serialization、模型、verifier、output guard、retry 注入故障。主代理 envelope、logs、trace、error/fallback 中 sentinel 出現次數=0；僅私有 snapshot 與授權 reader input 可含原文。Provider 原始錯誤也不得回流。 |
| `./scripts/verify unit bounded-output` | 每一結果含全部 blocks/metadata ≤16 KiB；answer ≤8 KiB、quote ≤512 bytes、citations ≤16。8 MiB source、32 KiB/8k-token chunk、8 chunks、64k total input、2 concurrent calls、2,048 output tokens/call 均測邊界及超界。string/dict/list oversized MCP 全部 spill，不呼叫模型；quota、深度、node、binary blocks 超限回 bounded error。 |
| `./scripts/verify unit cancellation` | 使用 fake clock 與可控 provider：gate 1 秒、spill 5 秒、call 20 秒、request 60 秒 deadlines 正確；最多一次 transient retry，計入共同預算。排隊中、I/O 中、模型中、publish 前取消都停止新工作，取消 outstanding calls、清理未發布 artifacts、拒絕 late result。取消後不再傳資料給 provider。 |
| `./scripts/verify unit permissions` | traversal、root 外 symlink、替換 race、非 regular file、跨 session、expired ID、secret path/content/question/output、binary/invalid encoding 全拒絕。檢查 spill directory 0700/files 0600、atomic publish、64 MiB quota、TTL/session cleanup。錯誤不暴露敏感 path/value。 |
| `./scripts/verify unit no-writes` | 不註冊 writer、不接受 propose_patch、writer=true 設定拒絕。契約只接受 read/inspect/stats 三個唯讀 operation。Reader 無 shell/network/write 工具。比較來源樹 before/after hashes、權限與檔名，完全不變；只允許受控 cache/metrics/test artifacts 寫入，來源至少以唯讀權限執行一次完整流程。 |
| `./scripts/verify unit store` | DDL 來自 `contracts/store/v1.sql`（核心不內嵌 CREATE TABLE），欄位集合封閉且不含任何內容欄位，session 名稱不出現在 db/WAL。handle 不跨 host/profile/principal/session/generation 重放，且外部 scope 只得到 `UNKNOWN_HANDLE`。過期是 SQL 述詞而非刪檔；時鐘倒轉不能復活。multi-source batch 全有或全無；publish transaction 失敗後無可用 handle，孤兒 blob 由 recovery 回收；staged temp 由 recovery 清除。內容定址 dedupe 與 refcount 正確，刪除競爭下內容仍可讀，內容不符 fail closed 且不刪檔，blob 位置為 symlink/FIFO 時拒絕。目錄 0700／檔案 0600。entry/byte quota 拒絕而非驅逐。揭露上限在回傳前 check-and-increment，per-session 上限跨 handle 生效。baseline 只計一次。真實子行程並發共用同一 store 不遺失 handle。 |
| `./scripts/verify unit inspect` | 擷取為 snapshot 的精確子字串並標 `deterministic_extraction` / `derived=false`；三種 selector 全程零模型呼叫。snapshot 不符與外部 handle 皆拒絕且不洩漏內容。單頁 ≤16 KiB 且 byte selector 可精確命中 16384。掃描預算會停止無果搜尋並標 `SCAN_BUDGET_EXHAUSTED`。分頁完整覆蓋且不重複；cursor 被竄改、換 selector 或換 store 皆拒絕。累計揭露上限確實阻止分頁重組整份 payload。byte range 不切斷字元；search 僅字面比對。 |
| `./scripts/verify unit accounting` | 兩條公式與規格逐字相符且為有號數；inspect／精煉提問得到負值。未回報的 usage 存為 null 並以 `bytes_div_4` 標示，精確 usage 優先。`host_truncated_observed` 與 `full_payload_counterfactual` 分開。baseline 只計一次，重複復原只增加成本。Egress 以最終序列化 envelope 量測。stats 無法重設、無法跨 session、拒絕未知欄位、分頁有界且不含內容或路徑；紀錄只含封閉列舉。Metric label 拒絕 model/provider/path/request_id 等高基數值。 |

## Integration gates

| 命令 | 必須可執行的斷言 |
| --- | --- |
| `./scripts/verify integration hermes --mode local` | 真實 pre_tool_call 阻擋 351 行 full read；targeted read 可用；ctx.llm bridge 傳設定模型與原 question；blocked 工具不執行，主代理只收到契約 envelope。三個唯讀工具都被真實 tool registry 接受。Reader 以 `register_auxiliary_task` 註冊進 host 自身登錄，使用者 `auxiliary.<key>` 設定優先於 plugin 預設，host 的 `auto` sentinel 回退至 plugin 預設。**handle 必須存活 per-turn 的 `on_session_end`** 並能回答下一輪的精煉提問；`on_session_finalize` 才撤銷。inspect 經 host 回傳精確 bytes 且不增加模型呼叫；stats 不含內容。歸屬為 `unverified`。 |
| `./scripts/verify integration openclaw --mode local` | 真實 before_tool_call/runtime llm 通過相同 fixtures；與 Hermes 的核心結果語意一致。三個工具都被真實 plugin loader 接受。另驗證兩項 host 事實：`PluginHookSessionEndReason` 仍含 `compaction`（lifecycle 決策依據），以及 isolated-completion 仍聲明「absence must not be projected as zero」（accounting 決策依據）。 |
| `./scripts/verify integration hermes --mode post-tool` | 以真實 host 重現 transform 例外 fail-open，證明 producer/wrapper 在其前已替換 raw。注入 wrapper/transform/spill/output 失敗及 oversized tool-error，檢查主代理 message、history、persistence、trace、fallback 均無 raw sentinel。不能證明即 mode disabled。 |
| `./scripts/verify integration openclaw --mode post-tool` | 用超過 host truncation 門檻但 ≤8 MiB 的頭/中/尾 sentinel payload 證明完整 capture 在 truncation 前，replacement 在 persistence/context 前；pointer 可讀回全部 sentinels，主 context 無原文。若無此順序即 mode disabled。已截斷輸入不能標 complete=true。 |
| `./scripts/verify integration hermes --mode unsupported` | 模擬 hook 缺失、順序未知、fail-open wrapper 不可用、版本變更或不安全 tracing：啟動拒絕該模式，原始 MCP 路徑不冒充受保護；local gate 可獨立運作。 |
| `./scripts/verify integration openclaw --mode unsupported` | 模擬 middleware 前 truncation、persistence 先於替換及缺失能力：fail-closed，不啟用 post-tool，不回 raw 作 fallback。 |
| `./scripts/verify integration all` | Release 目標是包含上述兩 adapter 的 host-level assertions。現有命令只聚合已實作的 local、unsupported 與明確 `NOT_RUN` 的 post-tool gates；它不宣稱兩 host 都已 runtime 測量取消／late response、所有 persistence 路徑或全部輸出 caps。那些 deterministic assertions 由 unit gates 提供。 |

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

`./scripts/verify packaging all` 必須在乾淨環境安裝兩 adapter、驗證預設設定與 capability matrix，執行 local smoke tests，確認無 writer、post-tool 預設 off、套件無 spill/secret/report 原文，卸載後清除私有 artifacts。套件必須包含規範性 store DDL 與 tool-args schema；乾淨安裝的 wheel 必須能在無 repository 的空目錄中僅憑自身 vendored DDL 開啟 store 並讀回 payload。清理斷言同時區分 per-turn 邊界（保留 handle）與真實 session 邊界（撤銷 handle 並移除內容）。

`./scripts/verify release all` 必須依序執行 unit、integration、eval、benchmark、packaging，檢查 LICENSE/第三方 notices 與固定依賴版本，彙整非敏感報告。所有必備 gates 通過才可標記 v1 ready；本命令不得 publish、建立遠端或 push。
