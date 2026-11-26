# tw-data-pool 架構規格 v2.0

2026-11-22，Fable 5.2 設計。v1.0（同日 Opus 5.5）的假設經實測修正，差異見第 12 節。
本文件是建置依據；建置者照第 10 節工單做，不改架構。項目、來源、時間的唯一真相是 `pool/catalog.json`。

## 1. 目標

每個資料時點只抓一次，四份每日報告改讀資料池；同一天各報告的數字完全一致；舊資料自動淘汰；資料池故障時報告照常出刊。

## 2. 實測結論（2026-11-22，GitHub Actions 與雲端工作階段各測一次）

| 項目 | 結果 | 對設計的影響 |
|---|---|---|
| repo 內的 `FINMIND_TOKEN` | 失效，FinMind 回「Token is illegal」 | 資料池不能依賴金鑰；金鑰改為選用 |
| 交易所官方端點（證交所、期交所、櫃買，含 OpenAPI） | 從 GitHub Actions 全部連得上，0.1～3 秒 | 一手資料可當主來源，免金鑰、無額度 |
| 台指 VIX | 找到官方月檔固定網址 `…/vix/log2data/YYYYMMnew.txt`，652 bytes | 報告長期取不到 VIX 的問題解決 |
| FinMind 免金鑰 | 範圍內資料集都可用；只有台指 VIX、十大交易人需要 sponsor | 這兩項改走官方 |
| 夜盤歸屬 | 週五晚上的夜盤記在下週一（11/23），期交所已可取得，FinMind 尚未收錄 | 夜盤以期交所為主 |
| 賣權買權未平倉比 | 含當日到期契約算出 40.38%，排除後 30.05%，與期交所公布值完全相同 | 當日到期契約一律排除（現行抓取程式沒排除，見第 11 節） |
| 融資金額單位 | 證交所仟元、FinMind 元 | 寫進目錄，測試鎖住 |
| FinMind 與官方交叉比對 | 加權指數、三大法人、外資期貨淨未平倉、融資餘額四項完全一致 | 雙來源互為備援可行 |
| 手動觸發 workflow | 雲端工作階段可以呼叫 dispatch | 補抓不必進 GitHub 網頁 |

## 3. 架構

```
GitHub Actions（每天 9 次，見第 5 節）
  └─ python -m pool.run            對帳式抓取：只補「該有而還沒有」的項目
       ├─ 來源鏈：官方一手 → FinMind → 標記 missing
       ├─ 檢核、雙來源比對、衍生值
       └─ 寫入 data 分支
            latest/digest.json     給報告讀的精簡檔（實測 4.5 KB）
            daily/YYYY-MM-DD/      當日 digest 留底（最近 5 份）
            series/*.csv           長期序列（只追加）
            logs/                  執行紀錄、公布時間紀錄、健康狀態

報告排程（雲端 Claude）
  └─ python3 -I tools/pool_read.py --profile <報告>      一個指令
       ├─ 命中：直接輸出 digest 的所需項目
       └─ 缺項或過期：同一支程式當場直接抓（讀穿式快取），輸出格式相同
```

四個設計原則：

1. **對帳式抓取**。每次執行都問同一個問題：依目錄的預計公布時間，現在應該有哪些項目，哪些還不是 ok。只抓缺的。重跑不產生差異，漏跑一次由下一次補上，不需要為每個時段寫不同邏輯。
2. **讀穿式快取**。GitHub 排程會延遲，資料池難免有沒趕上的時候。讀取程式發現缺項時用同一套來源程式直接抓，報告端永遠只下一個指令，不再由 Claude 逐項手動查。
3. **定義只寫一次**。每個數字的算法（近月怎麼選、哪些契約排除、單位）寫在目錄與程式，報告不再各自加總。
4. **官方一手優先**。交易所端點免金鑰、無額度、公布最早。FinMind 作備援並用來交叉比對。兩者都不是媒體數字，2026-11-17「不用媒體數字」的原則不變；順序由 `catalog.json` 的 `source_policy` 一個設定值決定。

為什麼不放東京主機：主機 18:10 才跑每日流程，趕不上清晨與盤後報告；主機的抓取程式綁著 V3.4 驗收雜湊，不宜為報告需求改動。兩邊獨立，一邊故障不影響另一邊。主機整併列在第二期。

## 4. 資料契約

### 4.1 digest.json（schema `tw-data-pool/2`）

完整範例是 `tests/golden/digest_2026-11-20.json`（真實數字）。結構：

```json
{
  "schema": "tw-data-pool/2",
  "generated_at": "2026-11-21T06:35:00+08:00",
  "snapshot_id": "8 碼，items 與 derived 正規化後的 sha256 前 8 碼",
  "calendar": {"tw_last": "2026-11-20", "tw_prev": "2026-11-19", "tw_next": "2026-11-23", "us_last": "2026-11-20"},
  "items": {
    "taiex": {"asof": "2026-11-20", "status": "ok", "src": "FinMind:TaiwanStockPrice", "xcheck": "match", "v": {"close": 36033.29, "…": "…"}}
  },
  "derived": {"rv20_annual_pct": 48.97, "sigma1_rv_pts": 1111.7, "sigma1_vix_pts": 1183.3, "closes_7": []},
  "health": {"alert": false, "token": "none|valid|invalid", "notes": []}
}
```

- `status`：`ok`、`pending`（還沒到預計公布時間，或來源尚未公布）、`missing`（已過預計時間、所有來源都失敗）、`conflict`（兩個來源不一致；`v` 取官方值，`alt` 記另一來源的值）。
- `asof` 是資料日，不是抓取時間。各項目的 `asof` 可以不同（例如盤後 18:23 時融資還是前一日），報告依 `asof` 標示日期。
- `snapshot_id`：報告在資料來源狀態寫「資料池快照 xxxxxxxx」，日後對帳可以還原當時讀到的版本（`daily/` 留底）。
- 數值單位固定：金額億元（欄名結尾 `_bn`）、口數整數、指數與價格保留原始小數。

### 4.2 項目與來源

17 個項目的來源順序、欄位、單位、檢核範圍、預計公布時間全部在 `pool/catalog.json`。重點定義：

| 項目 | 主來源 | 備援 | 容易錯的地方 |
|---|---|---|---|
| taiex、tpex | FinMind | 證交所、櫃買 | 收盤與證交所 MI_INDEX 比對 |
| inst_spot 三大法人 | 證交所 BFI82U | FinMind | 外資含外資自營；自營含避險 |
| fut_inst、opt_inst | 期交所 OpenAPI | FinMind | OpenAPI 只給最新一日，要核對 Date |
| tx_day、tx_night | 期交所 futDataDown | FinMind | 夜盤歸屬下一個交易日；近月取 6 碼最小者 |
| txo_oi | FinMind（逐履約價） | 期交所 optDataDown | 排除當日到期契約；週選有 W（週三）與 F（週五）兩種 |
| pc_ratio | 期交所 OpenAPI | 由 txo_oi 自算 | 兩者必須相等，當作 txo_oi 的檢核 |
| large_traders | 期交所 OpenAPI | 期交所 largeTraderFutDown CSV（OpenAPI 晚一天時） | 取 TX、全部月份（OpenAPI 999912、CSV 999999）；2026-11-24 兩來源逐欄相同 |
| vix | 期交所月檔 | FinMind（需金鑰） | Big5、Tab 分隔 |
| opt_struct 選擇權籌碼與波動結構 | 期交所 optDataDown（當日與前一交易日）＋callsAndPutsDateDown＋VIX 月檔 | 無 | 2026-11-26 新增，定義見 `docs/OPT_STRUCT.md`；非關鍵項目，只給盤後事實 |
| margin | 證交所 MI_MARGN | FinMind | 單位仟元與元 |
| usd_twd | 臺灣銀行牌告匯率 CSV（rate.bot.com.tw/xrt/flcsv/0/日期） | FinMind | 這是臺銀牌告，不是台北外匯經紀收盤價；臺銀一天掛牌多次，當日牌價 16:30 後才收錄（讀穿、強制重抓同樣適用）；回應檔名（ExchangeRate@YYYYMMDDHHMM.csv）缺少或資料日與請求日不符就拒收改走 FinMind；11/20、11/24、11/25 兩來源相同 |
| us_index、us_stock、us_vix、us_yield | FinMind | 無（報告自行外搜） | 台灣 06:00 後才更新 |

不在範圍：盤中資料（照舊讀主機快照 `midday_latest.json`、`day_path_latest.json`）、新聞與事件、原油黃金比特幣、Apollo、SemiAnalysis、個股。

### 4.3 data 分支目錄

```
latest/digest.json
daily/2026-11-20/digest.json            每次有變動就覆蓋當日這一份
series/taiex_daily.csv                  date,open,high,low,close,turnover_bn
series/inst_daily.csv                   date,foreign_bn,trust_bn,dealer_bn,total_bn
series/fut_oi.csv                       date,foreign_net_oi,trust_net_oi,dealer_net_oi
series/opt_pc.csv                       date,pc_oi_pct,pc_vol_pct,put_oi,call_oi
series/vix_daily.csv                    date,close,close_1min_avg
series/margin_daily.csv                 date,balance_bn,short_balance
series/opt_struct.csv                   選擇權籌碼與波動結構每日一列（影子期帳本，欄位見 pool/optstruct.py 的 SERIES_COLS）
series/tw_calendar.csv                  date（交易日；每週更新）
series/availability.csv                 date,item,first_ok_at,src（最近 60 天）
logs/run_log.csv                        run_at,trigger,scheduled_for,delay_min,filled,pending,missing,conflict,seconds,note
logs/health.json                        每週產生：各項目公布時間分布、排程延遲分布、建議調整
```

## 5. 執行時點（台灣時間）

| 時點 | 星期 | 這時通常補得到 | 趕上哪份報告 |
|---|---|---|---|
| 05:12 | 二～六 | 夜盤收盤；順便重抓前一日的關鍵項目，偵測修訂 | 美台 05:37 |
| 06:02、06:32 | 二～六 | 美股收盤、美債殖利率 | 朝邸錄 06:15 |
| 14:12 | 一～五 | 加權、櫃買、台指 VIX | — |
| 15:22 | 一～五 | 三大法人現貨、期貨與選擇權法人、台指期日盤 | — |
| 16:17 | 一～五 | 逐履約價未平倉、賣權買權比、大額交易人、匯率 | — |
| 17:32 | 一～五 | 前面漏掉的全部補齊 | 盤後總覽 18:23 |
| 21:22、21:52 | 一～五 | 融資融券 | 紅隊、逐日研判 |

- 各項目的預計公布時間是初估。每次首次取得的時間寫進 `series/availability.csv`，每週日的維護工作算出各項目第 90 百分位的公布時間與排程延遲，寫進 `logs/health.json` 並列出建議的時點調整。調整由人（或 Claude）改 `catalog.json`，程式不自動改排程。
- 05:12 與 06:02 離報告很近，排程延遲時趕不上屬於預期內；報告端的讀穿機制補這個缺口。
- 交易日判斷用 `series/tw_calendar.csv`（來源 FinMind TaiwanStockTradingDate，每週更新）。非交易日執行時只寫一列紀錄。
- 手動補抓：dispatch `pool.yml`，可帶 `date`、`items`、`force`。

### 5.1 時鐘（2026-11-25 起）

上線後實測 GitHub 的 schedule 在本 repo 延遲 6～12 小時（`logs/run_log.csv` 11/23～11/25 共 18 次，delay_min 387～727），盤後四個時段都拖到隔天凌晨才跑，11/25 盤後總覽 11 個項目全靠讀穿。上表時點因此改由 `clock.yml` 觸發：

- `tools/clock.py` 讀 `pool.yml` 的 cron（時刻表仍只有這一份；非雙引號寫法或欄位不合法會直接報錯），睡到各時段時送出 `workflow_dispatch`（`slot`＝該 cron 字串、`slot_at`＝該時段的精確時間，延遲以它計算）。dispatch 不經排程佇列，一兩分鐘內就開跑。
- 每個時鐘 job 約 5 小時 45 分。一啟動就先 dispatch 下一棒，讓它在 `concurrency: clock` 排隊，本棒不論正常結束、逾時或被取消都有人接；結束時若佇列裡沒有下一棒，再補送一次。同一組同時最多一個在跑、一個排隊，不會分叉。
- 新一棒啟動時，前 20 分鐘內的時段用原時段補送（涵蓋交棒空窗，含 05:12 的修訂重抓）；沒有的話送一次 `slot=catch-up`。重複送出無害（對帳式抓取）。
- `pool.yml` 與 `maintain.yml` 共用 `concurrency: data-branch`，設 `queue: max`（2026-05 起 GitHub 支援）：排隊的執行依序處理，延遲到達的備援 cron 不會取消時鐘送出、正在排隊的時段。
- `clock.yml` 每 3 小時的 cron、`pool.yml` 原有的 cron 都只當斷鏈備援；就算延遲，一啟動也會補跑再接回時刻表。`pool.yml` 的排程觸發先做「備援檢查」（`tools/fallback_gate.py`）：依 cron（含星期）算出原時段，data 分支 `run_log.csv` 已有 trigger＝clock、scheduled_for＝該時段的紀錄就直接結束（05:12 時段另要求該紀錄 note 以 `recheck=ok;` 或 `recheck=skipped;` 開頭，重抓失敗或舊紀錄沒有標記都照常執行）；即使該時段已處理，data 分支 `latest/digest.json` 若還有已過預計公布時間、卻沒有目標日有效值的項目，也照常執行補抓；只看同一時段，別的時段成功不算，查不到或讀不到照常執行。照常執行時以原時段作為 `--scheduled-for`，延遲與修訂重抓目標日不再靠執行時間回推（備援延遲超過 24 小時仍無法分辨原時段，實測延遲最多約 12 小時）。
- 公開 repo 的託管 runner 不計費，時鐘整天占一個 runner 沒有費用。
- 監控兩層：`pool.run` 在時鐘觸發卻晚到 30 分鐘以上時設 `health.alert`；報告端 `pool_read.py` 發現關鍵項目過預計公布時間 60 分鐘仍要讀穿或仍缺，且在「預計公布～預計公布後 60 分鐘」之間沒有任何排程或時鐘執行紀錄（看 `logs/run_log.csv`；手動補抓與期限後才到的備援都不算），也設 `health.alert`；資料雖已由資料池提供、但預計公布在最近 6 小時內且同樣沒有準時執行的關鍵項目，以「排程失靈但已補齊」警示。資料池有準時跑、只是交易所晚公布的情形不算排程失靈；讀不到 run_log 時略過此檢查並在 notes 註明（不拿 digest 的產生時間充當執行證據）。時鐘延遲改由報告端掃 `run_log.csv` 最近 6 小時的事件判斷（digest 另存 `health.bad`、`health.clock_late`，報告只沿用資料健康狀態，不會沿用幾小時前的延遲警示）。05:12 的修訂重抓以「原時段當下的目標日」為準：執行時目標日沒變就照常重抓（不論晚多久），已換（過了下一個收盤）才略過並在 notes 註明，避免把當天資料當成修訂。FinMind 金鑰被拒時，notes 只記 HTTP 碼或 FinMind 狀態碼與資料集（不記訊息文字，避免回顯金鑰進入公開資料分支），方便判斷是失效還是誤判。帶金鑰的 FinMind 請求出錯時，錯誤訊息同樣只留資料集與狀態碼。報告依規定發簡訊，排程失靈當天就會知道。
- 讀穿取得當日加權時，`pool_read.py` 會用讀穿值覆蓋序列裡同日的 K 棒（避免修訂前舊值與新收盤混用）、補進 `taiex_hist`，並用同一支 `derive()` 在本機算衍生值（`derived_via: direct`）。資料池的衍生值只在其 `closes_7` 最後一日與加權資料日相同時才沿用。加權來自資料池、VIX 是讀穿時，`sigma1_vix_pts` 用當日 VIX 重算。

## 6. 報告怎麼讀

每份報告的提示詞把現有的「資料來源規則」「台股資料抓取程式」「FinMind 請求上限」等段落刪掉，換成 `docs/REPORT_SNIPPET.md` 的短段落。核心只有三步：

1. `add_repo` 掛 `hankna/tw-data-pool`（read），淺層複製。
2. `python3 -I tw-data-pool/tools/pool_read.py --profile <代號> --date <報告日> --out pool.json`
3. 讀程式印出的摘要表與 `pool.json`。`ok` 的項目直接用；`missing` 的項目才用 WebSearch 或 web-fetch 子代理，並在資料來源狀態寫一行。

讀取程式的行為：

- 用 `gh api`（raw）讀 data 分支的 `latest/digest.json`；失敗改用 `git fetch --depth 1 origin data`。
- 依 profile 判斷每個項目的 `asof` 是否符合報告需要的日期。不符合、`pending`、`missing`，或整份 digest 的 `generated_at` 超過 26 小時，就對這些項目啟動讀穿：匯入 `pool/` 的來源程式直接抓，結果標 `via: direct`。
- 結束碼：0＝全部來自資料池；1＝部分讀穿補齊；2＝仍有缺項；3＝完全失敗（報告退回現行做法）。
- 報告不寫入資料池，也不重抓 `ok` 的項目。

`health.alert` 為 true（關鍵項目在最後一個時點後仍缺、連續兩個交易日同一項目缺、或 digest 過期）時，只有朝邸錄與盤後總覽兩份報告在寄信後各傳一則簡訊，其他報告不傳。

## 7. 品質機制

| 機制 | 做法 |
|---|---|
| 寫入前檢核 | 日期等於目標日；數值在目錄的 `valid` 範圍；三大法人分項加總等於合計；未平倉非負。不過就不寫，該項標 `missing` |
| 雙來源比對 | 標 `key` 的 7 個項目，兩個來源都取得時比對；差異超過容許值標 `conflict`，取官方值並留下另一值 |
| 內部一致 | `txo_oi` 排除當日到期後的加總必須等於 `pc_ratio` 的口數 |
| 修訂偵測 | 隔天 05:12 重抓前一日關鍵項目；有變動就更新、`rev` 加一、寫進 run_log |
| 來源可追 | 每個值帶 `src` 與 `asof`；報告帶 `snapshot_id` |
| 測試 | `tests/fixtures/2026-11-20/` 是當天各來源的真實回應；`tests/golden/digest_2026-11-20.json` 是獨立計算的標準答案。解析、衍生、到期契約排除、單位換算都以它為準 |
| 金鑰 | 選用。以 `Authorization: Bearer` 標頭傳送（FinMind v4 不收網址參數），有效就帶上（額度較高）；失效就當次改免金鑰並把 `health.token` 標 `invalid`。任何輸出不得含金鑰 |
| 連線 | 單一傳輸模組：只允許清單內的網域、逾時 40 秒、重試 2 次、回應上限 8 MB、不跟隨跨網域轉址 |

## 8. 保留與清理

| 對象 | 規則 |
|---|---|
| `daily/` | 留最近 5 份（依份數，長假不會被清空） |
| `series/` | 只追加；每年 1 月把前一年搬到 `series/archive/YYYY/` |
| `availability.csv` | 留 60 天 |
| `run_log.csv` | 留 300 列（約 6 週） |
| data 分支歷史 | 每週日 03:17 壓成單一 commit 後強制推送；平日保留歷史當作一週內的復原點 |
| 分支 | 只留 `main` 與 `data`；功能分支合併後刪除 |
| 一次性檔案 | 實測用的 `probe.yml` 與結果已刪，結論留在本文件第 2 節 |

所有會寫 data 分支的 workflow 共用 `concurrency: data-branch`，不取消進行中的工作。

## 9. 用量

| 項目 | 估算 | 依據與不確定性 |
|---|---|---|
| GitHub Actions | 每月約 200 次、約 200 分鐘 | 每次不滿 1 分鐘以 1 分鐘計。私有 repo 免費額度每月 2,000 分鐘（免費方案），與帳號其他私有 repo 共用；帳號目前的實際用量我查不到 |
| 對外請求 | 每天約 60～90 次，官方為主 | 官方端點無額度；FinMind 免金鑰有每小時請求上限（現行報告常遇到 402），資料池只把它當備援與美股來源 |
| repo 大小 | data 分支壓縮後小於 1 MB | digest 4.5 KB、序列每年約 60 KB |
| 報告端 | 每份報告省去下載抓取程式、逐項查 FinMind、等 402 重試、解析的步驟 | 省多少要量：基準是 11/20 三份報告的執行時間（美台 15.5 分、盤中 13.4 分、盤後 14.5 分），切換後比對 |
| 新增費用 | 無 | 不需要付費資料源或升級方案 |

## 10. 建置工單（建置用 Sonnet 5.5；W7 改提示詞前切 Opus 5.5 審一次）

只用 Python 標準函式庫（測試可用 pytest），不引入 pandas、requests。

| 工單 | 內容 | 驗收 |
|---|---|---|
| W1 核心 | `pool/http.py`、`pool/sources/{finmind,twse,taifex,tpex}.py`（解析與抓取分開）、`pool/calendar.py`、`pool/validate.py`、`pool/derive.py`、`pool/digest.py`、目錄載入 | 以 fixtures 為輸入、假設時間 2026-11-21 06:35，產出與 golden 相同（整數完全相同、小數 ±0.01；`generated_at`、`snapshot_id`、`health` 不比） |
| W2 對帳 | `python -m pool.run [--now] [--date] [--items] [--force] [--data-dir]`：來源鏈、雙來源比對、狀態轉換、序列追加、公布時間紀錄、執行紀錄、清理 | 用假時鐘跑 14:12 只處理到期項目、融資到 21:22 才出現；連跑兩次第二次無差異；竄改 fixture 觸發 conflict；造 8 份 daily 只剩 5 份 |
| W3 排程 | `pool.yml`（第 5 節時點與 dispatch）、`maintain.yml`（週日：交易日曆、健康統計、壓縮歷史）、`test.yml`（main 的 push 與 PR 跑 pytest） | dispatch 一次後 data 分支出現 digest；再 dispatch 無新 commit；壓縮後只剩 1 個 commit 且內容雜湊不變 |
| W4 回補 | `python -m pool.backfill --from 2025-11-01`：用 FinMind 區間查詢與期交所 VIX 月檔補 series | 六個序列各約 250 列，日期連續對得上交易日曆 |
| W5 讀取 | `tools/pool_read.py`：profile、過期判斷、讀穿、摘要表、結束碼 | 四種結束碼各有測試；摘要表在 60 行以內 |
| W6 影子期 | 11/23、11/24 兩個交易日資料池照跑、報告不改。比對 digest 與當天報告實際用的數字，整理排程延遲與各項目首次取得時間 | 差異表；每個差異有解釋；據此校正 `due` 與時點 |
| W7 切換 | 依序改提示詞：美台 → 朝邸錄 → 盤後總覽 → 盤中。每份切換後看第一份報告的資料來源狀態與執行時間 | 四份報告都以資料池為來源；執行時間與基準比較；出問題把提示詞改回前一版 |
| W8 收尾 | Drive 的「戰略情報哨_tw_data.py」停用後移到垃圾桶（刪除前問 Hank）；Notion 建一頁資料池索引（位置、規格連結、決策） | 沒有兩份並存的抓取程式 |

## 11. 已知缺陷與處置

現行報告共用的「戰略情報哨_tw_data.py」計算全部契約的賣權買權未平倉比時，沒有排除當日到期的契約。現在每週三、週五各有一檔週選到期，這兩天算出的比值會偏離期交所公布值（11/20 為 40.38% 對 30.05%），最大未平倉也可能列到已到期的契約。已於 2026-11-22 修正並上傳 Drive（新檔 ID 1a46HGYfJ9QXW73gcodKkBMw5C8eXHlZi）：未平倉排除當日到期契約，並附上期交所公布值供核對；以 11/18（月選結算）、11/19（無到期）、11/20（F 週選到期）三天實測，自算值都與期交所公布值相同。資料池上線後由 `txo_oi` 的規則取代。

## 12. 與 v1.0 的差異

| v1.0 | v2.0 | 原因 |
|---|---|---|
| 以 repo 的 FinMind 金鑰為主來源 | 官方一手優先、金鑰選用 | 金鑰實測失效；官方端點實測可用且較早公布 |
| R1～R4 四個時段各抓固定內容 | 對帳式，每次只補缺項，共 9 個時點 | 排程會延遲或漏跑；公布時間需要實測校正 |
| 報告缺項時退回舊抓法 | 讀取程式讀穿，同一套程式直接抓 | 報告端維持一個指令，舊抓取程式可以退役 |
| 五個資料檔加 manifest | 單一 digest（4.5 KB）加序列 | 報告只需要讀一個小檔 |
| daily 保留原始快照 | daily 只留 digest | 逐履約價明細主機已有，不重複存 |
| 資料集名稱建置時再確認 | 全部實測過，附 fixtures 與標準答案 | 建置者不需要猜 |
| 沒有 P/C 與夜盤的定義 | 明訂排除當日到期契約、夜盤歸屬日 | 實測發現現行算法有誤 |

## 13. 第二期（不在本次範圍）

- 東京主機的日資料改讀資料池，主機只留盤中抓取。
- 紅隊推演（21:40）、逐日研判（22:47）、週報接上資料池；這幾份的提示詞本次沒有逐一檢視。
- 美股收盤新聞摘要層，供美台與朝邸錄共用。
