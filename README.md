# tw-data-pool

每日共用資料池：台股、台指期、台指選擇權、美股收盤資料每個時點只抓一次，所有報告先讀這裡，缺的項目才各自補抓。

- 架構與建置工單：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 程式與文件在 `main`；資料在 `data` 分支（每週壓縮歷史，不要依賴 data 分支的 git 歷史）。
- FinMind 金鑰只放 GitHub Secret `FINMIND_TOKEN`，任何檔案與紀錄都不得出現金鑰。

## 建置狀態（2026-11-22）

- W1～W5 完成並上線：抓取核心、對帳式執行、排程（pool／maintain／test）、報告端讀取程式 `tools/pool_read.py`。
- 驗收：`tests/fixtures/2026-11-20/` 對 `tests/golden/digest_2026-11-20.json` 全數吻合；GitHub 上實際執行的 17 個項目與標準答案也完全相同。
- 來源順序：官方優先（`pool/catalog.json` 的 `source_policy`）。FinMind 金鑰失效，不更新；程式偵測到失效就改走免金鑰，`health.token` 標 `invalid`。
- Opus 覆核（11/22）修正：重抓失敗沿用原值、美國休市日、結算日近月、逾時轉缺、排程延遲紀錄、允許前一日的範圍。
- W7 完成（11/22，Hank 核准提前上線）：美台股市現況策略、朝邸錄、盤中策略簡報、盤後總覽四份排程提示詞改為先讀資料池，段落內容見 `docs/REPORT_SNIPPET.md`；資料池標 missing 才退回原外搜方法，結束碼 3 退回 Drive 舊抓取程式。
- W8（舊抓取程式退場）待上線兩週穩定後再議，刪除前先問 Hank。
