# tw-data-pool

每日共用資料池：台股、台指期、台指選擇權、美股收盤資料每個時點只抓一次，所有報告先讀這裡，缺的項目才各自補抓。

- 架構與建置工單：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 程式與文件在 `main`；資料在 `data` 分支（每週壓縮歷史，不要依賴 data 分支的 git 歷史）。
- FinMind 金鑰只放 GitHub Secret `FINMIND_TOKEN`，任何檔案與紀錄都不得出現金鑰。
