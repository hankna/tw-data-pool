# tw-data-pool 架構規格 v1.0（2026-11-22，Opus 5.5 設計）

> 用途：每日共用資料池。資料只抓一次，所有報告先讀資料池，缺的項目才各自補抓。
> 本文件是建置依據，建置者照第 9 節工單逐項做，不自行改架構。

## 1. 問題與目標

| 現況 | 問題 |
|---|---|
| 美台 05:37、朝邸錄 06:15、盤中 11:12、盤後總覽 18:23 各自從 Drive 下載「戰略情報哨_tw_data.py」執行 | 同一份資料一天抓 4 次以上 |
| 該程式用不帶金鑰的 FinMind | 免金鑰額度低，常回 402，要等 65 秒重試，拖慢報告 |
| 台指 VIX 官方頁數值在下載檔裡、搜尋抓不到 | 報告常整項刪除 |
| 每份報告各抓各的 | 同一天不同報告的數字可能不一致 |

目標：
1. 每個資料時點只抓一次，報告改讀資料池。
2. 用 repo 內的 FinMind sponsor 金鑰（Secret `FINMIND_TOKEN`），解決 402 與 VIX。
3. 舊資料依「份數」自動刪除，git 歷史每週壓縮，不累積垃圾。
4. 資料池壞掉時，報告自動退回原本的抓法，不會開天窗。

## 2. 架構總覽

```
GitHub Actions（tw-data-pool，cron）
   └─ fetch/pool_fetch.py（FinMind 有金鑰為主，官方網站備援）
        └─ 寫入 data 分支 ─┬─ latest/         各資料集最新一份＋manifest.json
                           ├─ daily/YYYY-MM-DD/ 原始快照（保留最近 5 個交易日）
                           ├─ series/          長期時間序列 CSV（只追加、不刪）
                           └─ logs/run_log.csv  執行紀錄（保留 60 列）
報告排程（雲端 Claude）
   └─ add_repo tw-data-pool（read）→ tools/pool_read.py → pool.json
        └─ 缺項或過期 → 退回原方法（戰略情報哨_tw_data.py／WebSearch），並記「資料池缺 X」
東京主機（tw-index-forecast）
   └─ midday_latest.json、day_path_latest.json 照舊放 Drive，資料池不重抓，只在 manifest 記指標
```

設計取捨：
- 抓取放 GitHub Actions、不放東京主機：主機已有盤中與全日路徑，改主機要走 SSH 與協調頁流程，風險較高；Actions 與主機互相獨立，任一邊壞掉另一邊照常。主機與資料池仍有部分重複抓取（主機自己的 FinMind 日資料），列為第二期整併（第 10 節）。
- 資料放 git 的 `data` 孤兒分支、不放 main：main 只放程式與文件，資料歷史每週壓縮不影響程式歷史。
- 盤中資料不進資料池：盤中以主機快照為準，避免兩套盤中數字。

## 3. 更新時點（台灣時間，交易日）

GitHub Actions 的 cron 常延遲 5～20 分鐘，所以排程時間都比報告提早，報告一律檢查 `asof`。

| 編號 | 排程 | 抓什麼 | 供誰 |
|---|---|---|---|
| R1 | 05:08 | 台指期夜盤收盤、前一交易日台股收盤籌碼（補漏） | 美台 05:37 |
| R2 | 06:00 | 美股收盤（USStockPrice）、美債殖利率（GovernmentBondsYield）；FinMind 美股約 06:00 後更新，未更新的項目標 `pending` | 朝邸錄 06:15 |
| R3 | 16:40 | 加權／櫃買收盤、三大法人現貨、外資期貨與選擇權未平倉、台指期日盤、選擇權逐履約價未平倉、P/C 比、台指 VIX、新台幣 | 盤後總覽 18:23 |
| R4 | 21:35 | 融資融券、R3 當時尚未更新的項目重抓 | 紅隊推演 21:40、逐日研判 22:47 |

- 週六 R1、R2 也跑（對應週五夜盤與美股）。
- 非交易日判斷：先查 FinMind `TaiwanStockTradingDate`；不是交易日只寫 manifest `skip`，不抓資料（週六 R1、R2 例外，以「前一交易日」判斷）。
- 手動補抓：workflow_dispatch，可帶 `run_id`（R1～R4）與 `date`。
- 盤中 11:12：不抓。manifest 記主機 `midday_latest.json` 的 Drive 位置，報告照舊讀主機快照。

## 4. 目錄與檔案契約

### 4.1 data 分支目錄

```
latest/manifest.json
latest/tw_close.json      R3／R4 產出
latest/tw_night.json      R1 產出
latest/us_close.json      R2 產出
latest/vix.json           R3 產出
daily/2026-11-24/{同上檔名}.json
series/taiex_daily.csv    date,open,high,low,close,turnover_bn
series/inst_daily.csv     date,foreign_bn,trust_bn,dealer_bn
series/fut_oi.csv         date,foreign_tx_net_oi,foreign_tx_net_chg
series/opt_pc.csv         date,pc_oi_ratio,pc_vol_ratio
series/vix_daily.csv      date,close,close_1min_avg
series/margin_daily.csv   date,margin_bal_bn,margin_chg_bn,short_bal,short_chg
logs/run_log.csv          run_at,run_id,data_date,ok,partial,missing,seconds,note
```

### 4.2 每個資料檔的共同格式

```json
{
  "schema": "tw-data-pool/1",
  "dataset": "tw_close",
  "data_date": "2026-11-24",
  "fetched_at": "2026-11-24T16:42:10+08:00",
  "run_id": "R3",
  "status": "ok | partial | missing",
  "items": {
    "taiex": {"value": {...}, "source": "FinMind:TaiwanStockPrice", "status": "ok"},
    "margin": {"value": null, "source": null, "status": "pending", "note": "21:35 再抓"}
  }
}
```

- `items.*.status` 只能是 `ok`、`pending`（還沒公布，之後會補）、`missing`（試過所有來源都失敗）。
- `source` 寫實際來源：`FinMind:資料集名稱` 或 `TWSE:端點`、`TAIFEX:端點`。
- 數值單位固定：金額億元（`_bn`）、口數整數、指數保留 2 位小數。

### 4.3 manifest.json

```json
{
  "schema": "tw-data-pool/1",
  "updated_at": "...",
  "trading_day": "2026-11-24",
  "prev_trading_day": "2026-11-21",
  "datasets": {
    "tw_close": {"data_date": "2026-11-24", "status": "partial", "run_id": "R3", "pending": ["margin"]},
    "us_close": {"data_date": "2026-11-21", "status": "ok", "run_id": "R2"}
  },
  "host_files": {
    "midday_latest": {"drive_folder": "1S4MGrO90HWCM_9sUZwaH8mghBotXkwrT", "title": "midday_latest.json"},
    "day_path_latest": {"drive_folder": "1S4MGrO90HWCM_9sUZwaH8mghBotXkwrT", "title": "day_path_latest.json"}
  }
}
```

## 5. 項目對照（資料池負責的範圍）

| 項目 | 主來源（有金鑰） | 備援 | 檔案 |
|---|---|---|---|
| 加權指數 OHLC、成交值 | FinMind TaiwanStockPrice TAIEX | TWSE MI_INDEX、FMTQIK | tw_close |
| 櫃買指數 | FinMind（資料集名稱建置時查官方文件確認） | TPEx openapi | tw_close |
| 三大法人現貨 | TaiwanStockTotalInstitutionalInvestors | TWSE BFI82U | tw_close |
| 外資台指期淨未平倉 | TaiwanFuturesInstitutionalInvestors TX | TAIFEX futContractsDateDown | tw_close |
| 外資選擇權買賣權淨部位 | TaiwanOptionInstitutionalInvestors TXO | TAIFEX callsAndPutsDateDown | tw_close |
| 台指期日盤、夜盤 | TaiwanFuturesDaily TX（trading_session） | TAIFEX futDataDown | tw_close／tw_night |
| 選擇權最大未平倉前三、P/C 比 | TaiwanOptionDaily TXO（position） | TAIFEX optDataDown、pcRatio | tw_close |
| 十大交易人 | TaiwanFuturesOpenInterestLargeTraders（有金鑰可試） | 無 | tw_close |
| 台指 VIX | TaiwanOptionVix | TAIFEX vixDaily3MNew 月檔下載 | vix |
| 融資融券 | TaiwanStockTotalMarginPurchaseShortSale | TWSE MI_MARGN | tw_close（R4） |
| 新台幣 | TaiwanExchangeRate USD | 無 | tw_close |
| 美股 | USStockPrice ^GSPC ^IXIC ^DJI ^SOX NVDA TSM AMD AVGO MU | 無（報告自行外搜） | us_close |
| 美債殖利率 | GovernmentBondsYield 2Y、10Y | 無 | us_close |

所有 FinMind 資料集名稱，B1 建置時先對照官方文件（https://finmind.github.io/）實測，名稱不符就改成正確名稱並更新本表，不要猜。

不在範圍（報告照舊外搜）：美國 VIX、比特幣、原油黃金即時價、新聞事件、Apollo、SemiAnalysis。

## 6. 報告讀取規則（寫進每份報告的提示詞）

1. 開工先 `add_repo` 掛 `hankna/tw-data-pool`（access read），淺層複製 main，執行
   `python3 -I tools/pool_read.py --date 資料日 --need tw_close,vix > pool.json`。
   `pool_read.py` 用 `gh api repos/hankna/tw-data-pool/contents/...?ref=data` 讀檔，不需複製 data 分支。
2. 判讀：`data_date` 等於預期資料日、`status=ok` 的項目直接使用，報告來源寫「資料池（原始來源：FinMind:…）」。
3. `pending`、`missing`、日期不符、整個讀取失敗：只對這些項目退回原方法（戰略情報哨_tw_data.py、WebSearch），並在資料來源狀態寫一行「資料池缺：項目（原因）」。
4. 禁止：報告自行重抓資料池已有且 ok 的項目；報告寫入資料池。

## 7. 保留與清理

| 區域 | 規則 | 理由 |
|---|---|---|
| daily/ | 只保留日期最新 5 個資料夾，每次執行後清理 | 依份數不依日曆天，春節長假不會被清空 |
| latest/ | 永遠覆蓋 | 只有最新一份 |
| series/ | 只追加、不刪；每年 1/1 把前一年移到 `series/archive/YYYY/` | 指標計算需要長期序列，一年約 50KB，可忽略 |
| logs/run_log.csv | 保留最近 60 列 | 約 3 週紀錄，夠查問題 |
| git 歷史 | 每週日 03:17 把 data 分支壓成單一 commit 後強制推送 | 真正釋放 .git 空間；讀取端不依賴歷史 |

並行保護：所有 workflow 用同一個 `concurrency: group: data-branch`，避免壓縮與抓取同時寫入。

## 8. 檢核與監控

寫入前檢查（不過就該項標 `missing`，不寫入錯值）：
- 日期：每項資料日等於預期交易日。
- 範圍：加權指數單日漲跌超過 ±10%、成交值為 0、未平倉為負，視為異常。
- 一致：三大法人合計與各項加總差距不超過 1 億元。
- 重複：同一 data_date 已是 ok 的項目，R4 不覆蓋（只補 pending／missing）。

監控：
- 每次執行結果寫 `logs/run_log.csv`。
- 單次 `missing` 不通知（報告會自動備援）；同一項目連續 2 個交易日 `missing`，或 workflow 失敗，用 GitHub Actions 失敗通知（寄 Hank 信箱），並由每週稽核排程列入。
- 金鑰只在 Secret；log 與 JSON 不得出現金鑰（沿用舊 workflow 的 scrub 做法）。

## 9. 建置工單（照順序，建置階段用 Sonnet 5.5）

| 工單 | 內容 | 驗收 |
|---|---|---|
| B1 | `fetch/pool_fetch.py`：以「戰略情報哨_tw_data.py」為基礎改寫，FinMind 改帶 `Authorization: Bearer $FINMIND_TOKEN`，輸出改成第 4 節格式，加 VIX、美股、殖利率、交易日判斷、第 8 節檢核 | 本機用 11/20、11/21 兩天跑出完整 JSON，數字與官方網頁抽查一致 |
| B2 | `fetch/prune.py`：daily 保留 5 份、logs 60 列、series 年度歸檔 | 單元測試：造 8 個資料夾只剩 5 個；含長假情境 |
| B3 | `.github/workflows/pool.yml`：R1～R4 cron、workflow_dispatch、concurrency、寫 data 分支 | 手動觸發 R3 成功、data 分支出現檔案 |
| B4 | `.github/workflows/squash.yml`：週日壓縮 data 分支 | 手動觸發後 data 分支只剩 1 個 commit，內容不變 |
| B5 | `tools/pool_read.py`：讀 manifest 與指定資料集、檢查日期、輸出合併 JSON 與缺項清單（exit code 0＝全有、2＝部分缺、3＝讀取失敗） | 測試三種 exit code |
| B6 | 影子期 2 個交易日：資料池照跑、報告不改；比對資料池與報告實際使用的數字 | 差異表，差異需有解釋 |
| B7 | 報告逐一切換讀取規則（第 6 節）：美台 → 朝邸錄 → 盤後總覽 → 盤中（只用前一日籌碼）→ 紅隊、逐日研判 | 每份切換後第一份報告的資料來源狀態正確 |
| B8 | 舊 `finmind-test.yml` 與 `results/` 移除（需 Hank 同意刪除） | — |

## 10. 第二期（暫不做，列入追蹤）

- 東京主機改讀資料池的日資料，主機只保留盤中抓取，徹底消除重複。
- 資料池加「事件與新聞摘要」層（美台股市現況策略與朝邸錄共用的美股收盤新聞），需評估 Parallel Search／Exa 在 Actions 內呼叫的可行性與費用。

## 11. 用量估算

| 項目 | 估算 | 依據 |
|---|---|---|
| GitHub Actions 分鐘 | 每月約 100 次執行 × 1～2 分鐘＝100～200 分鐘 | 私有 repo 免費方案每月 2,000 分鐘；與其他 repo 共用額度，建置後觀察實際分鐘數 |
| FinMind 請求 | 每次執行約 15～25 次，每天約 80 次 | sponsor 額度遠高於此 |
| repo 大小 | data 分支壓縮後 < 2MB | daily 5 份 × 約 200KB＋series |
| Claude 用量 | 每份報告省下 10～30 次抓取與解析工具呼叫 | 依現行提示詞的抓取步驟估 |
