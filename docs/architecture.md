# Architecture — normative specification (contract revision 1.1)

本文「必須」為 release gate；所有預設上限可調低，提高須重新通過安全及 benchmark gates。全域 read-only。來源基線見 [README](../README.md)，驗收見 [acceptance](acceptance.md)，保存與清除見 [security](security.md)，已知界限見 [limitations](limitations.md)。

## 契約版本 1.1（向後相容）

1.1 是相容性修訂，不是破壞性改版。兩個方向都嚴格：

* 1.0 request 仍然被接受，1.0 envelope 仍然通過驗證；
* 宣告 1.1 的 envelope **必須**帶 `result_kind`、`provenance`、`accounting_id`；
* 宣告 1.0 卻攜帶 1.1 欄位、或使用 1.1 operation 的 request，一律拒絕（`INVALID_REQUEST`）。未知的必要欄位絕不可被靜默忽略。

兩個常數分開：`EMITTED_SCHEMA_VERSION`（本核心產生的 envelope 宣告的版本）與 `SUPPORTED_REQUEST_VERSIONS`（本核心接受的輸入版本）。任何一處硬編碼單一版本字串都會在下一次改版時默默謊報。

1.1 新增：

| 新增 | 內容 |
| --- | --- |
| operation `inspect` | 零模型呼叫的決定性精確擷取，受單頁與累計揭露上限雙重約束 |
| operation `stats` | 唯讀的本 session 計量；不能重設、不能改保留、不能跨 session、不能揭露內容 |
| `result_kind` | `model_derived` / `deterministic_extraction` / `gate_decision` / `pointer` / `stats` / `failure` |
| `provenance` | requested / resolved / reported 三組供應商與模型分離，加上 attribution 狀態、政策與 usage 完整性 |
| `accounting_id` | 指向 store 內計量紀錄的不透明 ID；envelope 本身不含計量數字，量測因此不會自我指涉 |
| `recovery` | 失敗時的決定性後續動作，並明確標示 handle 是否仍有效 |
| `contracts/store/v1.sql` | 規範性 SQLite DDL，兩個核心逐字執行 |

Reader model 由 1.0 的單一固定模型改為**可設定**（預設仍為 `gpt-5.6-luna`）。這是刻意的行為改變而非默默放寬：取代原本硬性拒絕的，是 envelope 內誠實的 provenance —— 任何一次回答都說明請求了什麼模型、host 回報了什麼、以及該歸屬能被相信到什麼程度。

## 組件與信任邊界

```text
Hermes adapter                     OpenClaw adapter
 pre_tool_call                      before_tool_call
 transform_tool_result              result persistence / middleware
 ctx.llm                            runtime llm
           \                         /
            shared JSON contract + policy core
             | gate / snapshot / spill / chunk
             | question-driven read-only reader
             | citation verifier / output guard
             +--> bounded envelope --> main agent
```

兩個 adapter 只正規化 host 事件、能力與模型呼叫，不另寫 gate／citation／spill 語意。可使用各 host 所需語言；共用 JSON Schema、fixtures 與 conformance suite 是必須，共用可執行核心優先。核心含 gate、來源授權、immutable snapshot、spill registry、chunk planner、reader orchestration、citation verifier、output guard 與 metrics。

來源檔案／MCP payload 都是不可信資料，不能變成 system instructions。Reader 不取得 shell、網路、寫入工具或 host 完整 conversation；只收到 question、授權 chunks、必要定位 metadata 及固定指令。主代理永遠不自動收到被攔截來源的全文；只得到受限 envelope、答案與短引用。使用者明確要求的 targeted read 仍可回傳受限原文，屬不同路徑，也受安全政策約束。

## Large local read gate

1. 在實際工具執行前分類 Read 與 shell read。文字檔大於 **350 行**的 full Read 或無界 `cat` 類讀取必須阻擋；350 行內仍受 byte、安全與輸出 caps 限制。行定義為 LF 分隔的實體行，尾端 LF 不另增空行，空檔 0 行，CRLF 算一行。
2. Gate 回傳 `blocked/LARGE_READ`、不敏感的 source handle、行數或下界、允許的 targeted read 方式及 reader request 範例；不能自動捏造 question 或自動啟動 reader。小來源可正常執行。
3. Offset/limit 須為有效範圍且有效輸出有界；只給 offset 不算有界。Search 須有命中數、每筆長度及總 byte 限制。`head`、`tail`、`sed`、`awk`、`cat`、shell wrapper 等以 AST 和已支援的安全形態分類，不能用字串 regex 當完整防線。
4. 多檔、glob、pipeline、substitution、redirect、alias、動態路徑須合計預算；無法證明有界或安全的 read-like shell 指令回 `blocked/UNCLASSIFIABLE_READ`，引導受控 Read/search/reader。不得先執行再看大小。非讀取命令交由 host 原有政策；本工具不是任意 shell sandbox，不宣稱能辨識所有自訂腳本的讀取。
5. Host 必須枚舉並驗證所有宣告支援的 read 工具路徑；未知 raw read 工具不得宣称已受保護。需要全面保證的部署必須停用未受控的讀取能力。檔案大小探測最多掃描到 351 行或 byte cap；未知規模以 blocked 處理，探測本身也有 timeout。

## Hybrid snapshot store（1.1）

被攔截的內容必須被保存，否則 gate 只是拒絕而不是替代方案。保存採混合式：**SQLite 只擁有授權**，**不可變 payload 存在內容定址的私有檔案**。

SQLite 內容僅限：不透明 handle identity、session scope 與 generation、TTL、quota、內容 refcount、揭露累計、清理狀態與有界的操作計量。**絕不寫入**：來源路徑、question、answer、quote、payload 預覽、provider 錯誤內容，或任何檔案系統路徑。blob 位置由 SHA-256 內部推導，既不儲存也不外露。

Schema 是規範性的，放在 `contracts/store/v1.sql`，兩個語言核心逐字執行該檔，不得各自內嵌等價的 `CREATE TABLE`；cross-language interoperability 測試以同一個 store 檔案由兩邊互相讀寫驗證。

發佈順序（capture batch）必須依序為：

1. 呼叫端先完成整個 request 的驗證與授權，並有界地取得 bytes；
2. 每個 payload 以 `O_CREAT|O_EXCL|O_NOFOLLOW` 寫入暫存檔、`fsync`、`chmod` 0600，再原子 rename 到內容定址位置；
3. **單一** SQLite transaction 一次發佈所有 handle 並取得所有 refcount。

payload 與 metadata 都持久化之前不存在可用 handle；multi-source capture 要嘛全部發佈、要嘛一個都不發佈。(2) 與 (3) 之間崩潰只會留下沒有 row 的孤兒 blob 檔，由 sweep 回收，絕不會留下可用 handle。

可讀性是 SQL 述詞而非檔案是否存在：

```sql
revoked = 0 AND expires_at_ms > :now
AND scope.closed_at_ms IS NULL AND scope.generation = :generation
```

因此過期、撤銷、scope 已關閉或 generation 過期的 handle，在述詞不再成立的當下即不可讀，與實體清理是否執行無關。`expires_at_ms` 為 UTC 毫秒；每次讀取取 `max(wall_clock, clock_high_water_ms)`，時鐘倒轉不能復活已過期的 handle。

Dedupe 依內容雜湊，refcount 只在發佈或移除 handle 的同一個 transaction 內變動。刪除採 mark-then-sweep：先在 transaction 內標記 `pending_delete`，在所有鎖之外 unlink，再由第二個 transaction 重新確認 `refcount = 0 AND pending_delete = 1` 才刪除 row。雜湊碰撞或內容不符一律 fail closed（`STORE_FAILED`），**不刪除該檔案** —— 該 blob 可能仍被其他有效 handle 參照，刪除會把一個損壞擴散成多個。

Handle 綁定於受信任的 (host, profile, principal, session, generation)，五個成分都來自 host，都先摘要後儲存，因此不保留 session 名稱、帳號或 profile 標籤。**per-turn 事件不得關閉 scope**：Hermes 在每次 `run_conversation` 結束都會觸發 `on_session_end`，OpenClaw 會以 `reason: "compaction"` 在對話進行中觸發 `session_end`；在這兩處撤銷 handle，等於刪掉下一輪最需要的復原狀態。真正的界線才撤銷（Hermes 的 `on_session_finalize`/`on_session_reset`，OpenClaw 的 `new`/`reset`/`deleted`），其餘一律交給 TTL。

**舊版 spill 檔案永不被匯入為授權 handle。** 磁碟上一個未經認證的檔案不是 capability。1.1 提供計數與清除，但只在呼叫端明確要求時執行。

## 決定性擷取與累計揭露上限（1.1）

`inspect` 是「我需要看到真正的文字」的逃生門，刻意不是檢索工具。四個性質使它可以安全交給代理：

1. **精確且自我標示。** 回傳的每個 byte 都逐字複製自呼叫端指名的不可變 snapshot；envelope 標為 `deterministic_extraction` 且 `provenance.derived = false`，不可能被讀成摘要。該路徑完全沒有 provider 參照，「零 LLM 呼叫」因此是結構事實而非承諾。
2. **單頁有界。** 一頁上限 `inspect.max_result_bytes`（16 KiB），以 segment 的 UTF-8 bytes 計。
3. **累計有界。** 分頁是擊破單頁上限最明顯的方式，因此**每一頁在回傳任何 byte 之前**都先在同一個 transaction 內對 per-source 與 per-session 揭露上限做 check-and-increment。上限用盡後續頁回傳零內容並標 `DISCLOSURE_EXHAUSTED`。沒有任何設定可以讓重複小量讀取重組出**大型** payload。上限是位元組預算：小到能放進上限的來源，
`inspect` 可以完整回傳（350 行的 pre-read gate 是 context 成本控制，不是機密性邊界）。
4. **續頁是認證而非算術。** cursor 是以 store metadata 內的金鑰做 HMAC 標記的不透明 token，綁定 handle、snapshot hash 與正規化 selector；不能被編輯以跳過掃描預算，不能指向另一個 snapshot，也不能在另一個 store 重放。

Selector 為嚴格 union：`lines`（1-based inclusive）、`bytes`（0-based half-open）、`search`（僅字面 needle，永不接受 regex，因此掃描時間對 snapshot 大小線性）。byte range 兩端都會拉回 UTF-8 邊界，emit 的文字必然是 snapshot 的子字串。

## 共用 JSON contract

實作建立 `contracts/v1/*.schema.json`，JSON Schema 2020-12，所有 object `additionalProperties: false`，數字有上下界，字串以 UTF-8 byte guard 補充 schema 長度驗證。版本不相容必須拒絕；host metadata 放 adapter 私有區，不混入核心契約。

`contracts/v1/tool-args.schema.json` 另外規範代理實際呼叫的三個工具參數，兩個核心以同一份 schema 驗證，避免兩個 host 漂移。路徑只出現在該檔：capture 當下授權後即丟棄，永不進入 `request.schema.json`、envelope、store 或 metric label。`context_shunt_read` 必須恰好一種來源形式 —— `paths`（初次擷取）或 `handles`（對已持有 snapshot 的精煉提問）—— 同時給或都不給都會被拒絕。

Reader request 範例：

```json
{
  "schema_version": "1.0",
  "request_id": "req_01",
  "operation": "read",
  "question": "重試策略在哪裡定義？列出限制與證據。",
  "sources": [{"source_id": "src_01", "snapshot_id": "sha256:abc", "selector": {"kind": "lines", "start": 1, "end": 800}}],
  "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000}
}
```

`snapshot_id` 範例 hash 為示意；實際必須是完整 SHA-256。Question 必須非空且不超過 2 KiB。Sources 1–8 筆；source_id 是 session registry 的 opaque capability，不能由 reader request 任意傳入絕對路徑。Selector 為互斥 tagged union：`all`、`lines(start,end)`、`records(pointer,start,end)` 或 `search(pattern,max_matches)`；lines/record ordinal 都是 1-based inclusive，JSON Pointer 依 RFC 6901 定位。Search pattern 僅接受 bounded literal 或保證線性時間的 regex。來源註冊由 adapter 經授權完成，不能將 ID 視為繞過授權的憑證。Budgets 只能縮小部署 caps。

所有 gate、reader、spill 路徑回傳同一 envelope：

```json
{
  "schema_version": "1.0",
  "request_id": "req_01",
  "status": "ok",
  "code": "ANSWERED",
  "answer": "重試上限為三次 [c1]。",
  "citations": [{"id": "c1", "source_id": "src_01", "snapshot_id": "sha256:abc", "locator": {"kind": "lines", "start": 42, "end": 42}, "quote": "max_retries = 3", "verified": true}],
  "coverage": {"complete": true, "processed_chunks": 1, "planned_chunks": 1, "omitted": [], "upstream_truncated": false},
  "sources": [{"source_id": "src_01", "snapshot_id": "sha256:abc", "media_type": "text/plain", "bytes": 18000, "expires_at": "2026-09-07T00:00:00Z"}],
  "retryable": false
}
```

必填欄位為上例全部欄位；無內容用空字串／陣列，不能附加 raw/debug 欄位。`status` 僅 `ok|partial|blocked|error`。`code` 固定 enum：`ANSWERED, NO_MATCH, LARGE_READ, UNCLASSIFIABLE_READ, SPILLED, INVALID_REQUEST, UNSUPPORTED_VERSION, UNSAFE_SOURCE, SOURCE_CHANGED, SOURCE_EXPIRED, BINARY_UNSUPPORTED, LIMIT_EXCEEDED, TIMEOUT, MODEL_ERROR, INVALID_MODEL_OUTPUT, CITATION_INVALID, SPILL_FAILED, HOST_UNSAFE, UPSTREAM_TRUNCATED, CANCELLED`。Schema 要約束合法 status/code 配對；`SPILLED` 為 ok 且 answer 空，不代表已回答問題。未成功執行的 coverage.complete 必須 false。

Coverage.omitted 每筆使用 `{source_id, selector, reason}`；未知剩餘範圍以 selector `all` 加 `reason` 說明，不能用空陣列假裝完整。Sources 僅含 handle、snapshot hash、media_type、bytes、expiry，不含絕對 spill 路徑、內容 preview 或秘密。Upstream_truncated 為 `true|false|null`，null 代表未能證明，不能設 complete=true。Citations 的 verified 欄位只能由 verifier 寫入，不能相信模型輸出。

## Snapshot、chunk 與 verified citations

- 核心對授權來源建立 immutable snapshot，SHA-256 綁定原始 bytes；讀取前後用檔案 identity／metadata 檢查 race，無法建立一致快照回 SOURCE_CHANGED，不混用版本。引用以 snapshot 為準，後續 targeted read 若來源變動須重新註冊。
- UTF-8 text 以實體行索引；不正規化換行導致 offset 漂移。超長行可分 byte-safe UTF-8 chunks，但引用仍標原始行，quote 必須是该行中的精確片段。JSON 以 deterministic serialization 建立 snapshot，保存 JSON Pointer 與穩定 record ordinal；array 的第 n 筆 ordinal=n+1，object keys 依固定排序建立索引；scalar 使用指向原值的 pointer，ordinal=1。不把 JSON pretty-print 行數當原始行引用。
- Record citation locator 使用 `{kind:"records", pointer:"/items", start:2, end:2}`，quote 為對應 record canonical JSON 的精確片段。Verifier 重讀 snapshot／index，驗證 handle 權限、hash、range 與 exact quote；拒絕不存在的行／record、混用 snapshot 與偽造 verified。
- Chunk manifest 記錄 source、snapshot、行／record／byte 範圍與順序；相鄰重疊最多一行且占預算。每個模型呼叫都帶原 question。Reducer 如有使用，也限同一模型、deadline 與 input budget，且只能組合已驗證 evidence。
- 回答中的來源事實必須對應 citation ID；純機械驗證只能保證定位與 quote 正確，不能證明推論必然成立，語意支持度由 eval gate 檢驗。無證據不得填補答案；invalid citation 對應 assertion 要整段移除，無剩餘有效答案則 error/CITATION_INVALID。

## Payload、chunk 與 timeout caps

| 項目 | v1 預設硬上限 |
| --- | --- |
| 可直接回主代理的單次工具結果 | 16 KiB，合計所有 content blocks／metadata |
| 單次 targeted read | 350 行或 200 matches，且 ≤16 KiB |
| 單個 snapshot／MCP 原始 payload | 8 MiB；超過不做無界 buffer，回 LIMIT_EXCEEDED |
| 每 session spill quota／TTL | 64 MiB／1 小時，session 結束清除 |
| 每 chunk | 32 KiB 且估計 ≤8,000 tokens，較小者；按 UTF-8 邊界切割 |
| 每 request | ≤8 chunks，總模型 input ≤64,000 tokens（含所有 prompt／reducer），≤2 concurrent calls |
| Reader answer／單 quote／citations | 8 KiB／512 bytes／16 筆，仍須符合整體 envelope ≤16 KiB |
| Model output | 每 call ≤2,048 tokens，最終仍受 byte caps |
| Gate 探測／spill I/O | 1 秒／5 秒 |
| 每模型 call／整體 request | 20 秒／60 秒，含排隊、重試、驗證 |
| Retry | 最多 1 次，僅 transient provider error，計入相同 token／deadline 預算 |

各 layer 在 materialize 前採 streaming 計數；host 若已先配置大型物件，另在 host 能力報告揭露該成本，核心不能宣稱消除它。Provider usage 若不可取得，使用保守估計並標 metrics 為 estimated。Token budget 無法可靠界定時拒絕請求。各 chunk、模型和 final envelope 都重新計數；不因摘要變短就放寬原始讀取上限。

## Path、secret、binary safety

Local sources 僅限明確設定的 workspace roots；canonicalize 並檢查 symlink、traversal、hardlink／檔案 identity race；只允許 regular files，拒絕 devices、FIFOs、sockets、遠端 URL 與任意 archive 展開。利用安全 open／descriptor 驗證保證讀到授權物件；host 不支援必要語意時拒絕該來源。Registry 隔離 session／tenant，來源 handle 不跨 session 重用。

預設拒絕 `.env`、私鑰、credential stores 與管理者 denylist；對 question、text／structured values、模型 answer／quotes 都做 secret policy 檢查，命中則拒絕受影響操作，不把原始值寫進錯誤訊息。偵測器不能保證辨識所有秘密，因此 provider 必須是管理者允許的資料目的地，allowlist 與最小必要 chunks 仍是必要防線。v1 不透過自動遮罩變更原始 snapshot 後仍冒充原始引用。

以 content sniffing 加 encoding validation 判定 binary，不只看副檔名；v1 拒絕 binary、未知 encoding 與 MCP image/audio/resource blobs，回安全 error；不做 OCR、base64 展開或把 binary 丟給 LLM。混合 content 結果若含不支援 block，整體不回傳原始 block。

Spill 存在 workspace 之外的私有 cache，directory 0700／files 0600，atomic publish、quota 與 TTL 清除；不放進 git。清除檔案不宣稱安全抹除。JSON serialization 限深 64、最多 100,000 nodes，循環或不支援值回 error，不默默跳過 dict/list。Spill 不呼叫 LLM；讀取時才以 question 使用 reader。

## 兩個 adapters 與 optional Suma mode

| Adapter | Pre-tool | Reader model bridge | Post-tool release 條件 |
| --- | --- | --- | --- |
| Hermes | pre_tool_call 正規化並執行 gate | ctx.llm；並以 `register_auxiliary_task` 註冊 `context_shunt_reader`，使用者 `auxiliary.<key>` 設定優先於 plugin 預設。歸屬上限為 `unverified` | transform_tool_result 有 fail-open 風險；需外層受控 producer／wrapper 保證無 raw fallback |
| OpenClaw | before_tool_call 正規化並執行 gate | runtime llm 的 isolated-agent-runtime 路徑（零工具、不繼承對話）。host 回報自身選定結果，歸屬為 `resolved` | 必須用 sentinel 實測 middleware 相對於 truncation、persistence、context insertion 的順序 |

兩者須在啟動時產生 capability report：host/SDK version、tool coverage、hook ordering、raw replacement guarantee、model support、tested fixture ID。模型不可用就回 MODEL_ERROR，不默默換模型。Runtime upgrades 使能力證據失效，重跑 conformance 才能啟用相關模式。

**Suma oversized MCP post-tool mode** 是共用核心的 optional mode，不是第三個 host adapter，預設 `suma_post_tool.enabled=false`。啟用時移除旧 heuristic compactor 的執行路徑，不得串接兩者。对完整 serialized MCP result（含 string、dict、list、所有 content blocks）計 bytes：≤16 KiB 通過安全檢查後回傳；大於上限則純 spill，原子替換為 `ok/SPILLED` pointer envelope。Reader 工具與內部 envelope 明確標記並驗證，以免 recursive spill；不依 payload 自稱 internal 來跳過檢查。

Spill 必須先完成並可回讀驗證，再釋出 pointer；overflow、磁碟滿、permissions、serialization、replacement 失敗只回 bounded error，不回原始 payload。若 host 原始 error result oversized，也適用同樣規則；主代理收到的 error 只能保留安全 code。

Hermes 的 transform try/catch 本身不足以對抗 host fail-open。必須在原始結果可能進入 host fallback 之前，用受控 MCP producer wrapper 完成 capture/spill/safe-envelope，且測試 wrapper 例外也不把 raw 交回 host；無法證明则該 MCP 路徑不得啟用 mode。讀取 gate 可獨立啟用。

OpenClaw 必須針對頭、中、尾 sentinel 量測 raw capture、truncation、persistence、middleware 與 context insertion 的事件序。要求完整 capture 發生在 truncation 前，安全替換發生在 persistence/context 前；只在 persistence 後攔截不合格。若 middleware 前已有 truncation，尋找經測試的更早 capture 邊界；找不到則 `HOST_UNSAFE`、mode 不啟用。遇到來源已截斷的資料只能 partial/UPSTREAM_TRUNCATED、揭露 coverage，不能以 snapshot hash 冒充來源完整。

## Partial、failure 與 no raw context leak

- `ok/ANSWERED` 僅在所有選定範圍完成且引用有效時使用；搜尋無命中可 ok/NO_MATCH，但只能對實際完整搜尋範圍下結論。
- 達到 budget、timeout、部分 chunk failure：已有驗證答案則 partial 並列 omitted/reason；否則 error。任何 partial 都不得宣稱全來源不存在某事。單一來源的權限／secret／binary failure 使整個 request 拒絕，不回其他來源答案。
- 錯誤 envelope 不含 raw、model response、stack、shell arguments、provider error body 或敏感 path。Timeout／cancel 要取消 outstanding work、封鎖 late results 並清除未發布 artifacts。已失效 handle 不自動重抓原始資料。
- No raw leak 適用於被攔截的大型 payload 在主代理 message、tool history、persistence、trace、log、例外、fallback、debug 及 retry 路徑；允許的短 quote 仍計入 caps。原文僅可存在授權 snapshot／私有 spill 與 reader 的最小必要輸入，不能傳給其他模型或主代理。
- Output guard 是最後固定邊界；其失敗也必須由 adapter 生成固定小錯誤。若 host 不能保證此邊界，拒絕啟用該 interception mode，而不是依賴 fail-open。

## Provenance 與 reader 設定（1.1）

`actual_model` **永不**由 `requested_model` 合成。三件事分開記錄，任何一項都可能缺席：

| 欄位 | 意義 |
| --- | --- |
| `requested_*` | adapter 向 host 要求的供應商與模型。永遠已知。 |
| `resolved_*` | host 自述其政策與路由後選定的目標。只有 host 揭露時才已知，否則為 `null`，絕不由 request 回填。 |
| `reported_*` | **供應商**自述實際產生 tokens 的模型。只有供應商回報且 host 傳遞時才已知。 |

`attribution_status` 說明能被證明的最強結論：`actual`（供應商確認且與請求一致）、`resolved`（host 回報自身路由結果，是路由事實而非供應商確認）、`unverified`（有值回來，但該 host surface 無法區分「供應商回報」與「請求的回音」）、`mismatch`（具體值與請求矛盾）、`unknown`（什麼都沒回來）、`not_applicable`（根本沒有模型呼叫）。

`provider_confirms_generation` 只能由**讀過該 host 原始碼的 adapter**斷言，核心不做推論。Adapter 無法區分兩者時傳 `false`，結果即為 `unverified`。

`reader.attribution_policy` 決定無法證明歸屬時的行為：`allow_unverified`（預設，發佈誠實標示的答案）或 `require_match`（改為拒絕，回 `PROVENANCE_UNAVAILABLE`）。預設選 `allow_unverified` 的理由是：在無法證明歸屬的 host 上，`require_match` 等於完全停用 reader。矛盾（`mismatch`）在兩種政策下都拒絕 —— 那是錯的答案，不是弱的答案。Envelope 一定記錄當時生效的政策。

`reader.fallback_chain` **只為可用性**存在。只在可重試的可用性失敗時前進，絕不因答案品質不佳而前進；每次嘗試保留自己的 provenance 與 usage，且啟用過 fallback 會記為 `fallback_used`。答案品質不佳的誠實補救是對同一 snapshot 提出精煉問題，把可用性 fallback 當成語意品質救援是誤導。

Reader 失敗（provider、timeout、格式錯誤、citation 失效、provenance 失效）**必須保留有效 handle**，並在 `recovery` 中給出決定性後續動作。只有 handle 本身失效才需要重新擷取來源。

## Metrics 與 token accounting

記錄 gate allow/block reason、raw/output bytes、spill bytes/count、quota failures、chunks planned/processed/omitted、verified/rejected citations、timeout/cancel/retry、status/code、LLM calls/input/output tokens、各階段 latency histogram、host mode/capability failures。主代理 context bytes/tokens saved 與 reader 成本分開報告；只在有版本化單價配置時估計費用，否則報 token usage。

每次操作記錄有界的非內容 metadata：`operation_id`、`kind`、`status`、`code`、`raw_input_bytes`、`raw_input_baseline_tokens`、`baseline_kind`/`baseline_method`、`baseline_credit_tokens`、`main_model_envelope_bytes`/`_tokens`、reader input/output/cache tokens（若有回報）、`attempts_started`、`attempts_usage_complete`、`delivery_boundary`、`main_context_tokens_saved`、`net_tokens_saved`。

公式為契約：

```text
main_context_tokens_saved = baseline_credit_tokens - main_model_envelope_tokens
net_tokens_saved          = main_context_tokens_saved
                          - reader_input_tokens - reader_output_tokens
```

兩者皆為**有號數**。精煉提問、失敗重試與 inspect 分頁都會得到負值，這是正確答案：它們消耗 context 而沒有新增任何被withheld 的內容。

三條紀律：

* **零不代表未知。** 供應商未回報的 token 數存為 NULL、呈現為 `null`；仍需數字時以自行量測的 bytes 推導，並標記 `bytes_div_4` 而非冒充精確值。`unknown` 只出現在完全沒有嘗試的情況。精確的供應商 usage 永遠優先。
* **baseline 只計一次。** `baseline_credit_tokens` 只在首次 withhold 該 snapshot 的操作上為非零；其後同一 snapshot 的操作記為零 credit，但仍記錄自身 envelope 與 reader 開銷，因此重複復原呈現為累積成本而非重複節省。
* **反事實要標示為反事實。** `full_payload_counterfactual` 是「若整份 payload 進入對話會花掉多少」；`host_truncated_observed` 是 host 早已截斷、只能觀察到截斷後大小時適用的另一個較小 baseline。在後者情況下計入完整 payload 是捏造。

Egress 在 envelope 完成後量測。Envelope 只帶不透明的 `accounting_id`，其指向的紀錄在序列化之後才寫入 store，因此量測不可能包含自身。

Metrics labels 只允許 bounded enums（adapter/mode/reason/status/form/decision/result/stage），**不含 model 或 provider 名稱**、source path、question、answer、quote、payload、secret、完整 request/source IDs。模型名稱是無界的廠商文字，會隨設定改變，作為 metric 維度既是成本問題也是部署指紋；「請求了哪個模型」屬於 envelope 的 provenance，不屬於時間序列。診斷 request correlation 只存短期隨機 ID 於受限事件記錄，不作 metric label。預設停用 provider prompt logging，測試主代理和 reader tracing 的隔離；若無法關閉不安全 tracing 就阻擋功能啟用。

## Future optional code-writer contract

只預留獨立的未來 `operation: propose_patch` 概念：question、授權 source handles、base snapshot hashes → patch proposal、evidence、base hashes。未來必須另定 schema version、write scopes、conflict checks、review/apply contract 與驗收；本 v1 schema 不接受此 operation，不註冊 writer tool，不呼叫寫入模型、不 apply patch。即使設定 writer.enabled=true，v1 也以 unsupported configuration 拒絕，不能變成隱藏功能。
