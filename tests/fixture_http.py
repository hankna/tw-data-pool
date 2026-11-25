"""測試用傳輸：以 tests/fixtures/<日期>/ 的真實回應取代網路。找不到的端點丟 SourceError（用來測備援）。"""
import csv, gzip, json, io
from pathlib import Path
from pool.util import SourceError

FM = {
    ("TaiwanStockTradingDate", None): "fm_calendar.json",
    ("TaiwanStockPrice", "TAIEX"): "fm_taiex.json", ("TaiwanStockPrice", "TPEx"): "fm_tpex.json",
    ("TaiwanStockTotalInstitutionalInvestors", None): "fm_inst_spot.json",
    ("TaiwanStockTotalMarginPurchaseShortSale", None): "fm_margin.json",
    ("TaiwanFuturesInstitutionalInvestors", "TX"): "fm_fut_inst.json",
    ("TaiwanOptionInstitutionalInvestors", "TXO"): "fm_opt_inst.json",
    ("TaiwanFuturesDaily", "TX"): "fm_tx_daily.json", ("TaiwanOptionDaily", "TXO"): "fm_txo_daily.json.gz",
    ("TaiwanExchangeRate", "USD"): "fm_usd.json",
    ("GovernmentBondsYield", "United States 10-Year"): "fm_us10y.json",
    ("GovernmentBondsYield", "United States 2-Year"): "fm_us2y.json",
    **{("USStockPrice", s): f"fm_us_idx_{s[1:]}.json" for s in ("^GSPC", "^IXIC", "^DJI", "^SOX", "^VIX")},
    **{("USStockPrice", s): f"fm_us_{s}.json" for s in ("NVDA", "TSM", "AMD", "AVGO", "MU")},
}
OFFICIAL = {
    "BFI82U": "twse_BFI82U.json", "FMTQIK": "twse_FMTQIK.json", "MI_INDEX": "twse_MI_INDEX_IND.json",
    "MI_MARGN": "twse_MI_MARGN.json", "tpex_index": "tpex_openapi_index.json",
    "PutCallRatio": "taifex_openapi_PutCallRatio.json",
    "OpenInterestOfLargeTradersFutures": "taifex_openapi_LargeTradersFut_TXonly.json",
}


class FixtureHttp:
    token_state = "none"
    last_headers = {}

    def __init__(self, day="2026-11-20", off=()):
        self.dir = Path(__file__).parent / "fixtures" / day
        self.off = set(off)    # 要模擬失效的來源關鍵字，例如 {"FinMind"}、{"BFI82U"}
        self.calls = []

    def _load(self, fn):
        p = self.dir / fn
        if not p.exists():
            raise SourceError(f"無樣本 {fn}")
        raw = gzip.open(p).read() if fn.endswith(".gz") else p.read_bytes()
        return raw

    def finmind(self, dataset, data_id, start, end):
        self.calls.append(("fm", dataset, data_id))
        if "FinMind" in self.off:
            raise SourceError("FinMind 模擬失效")
        fn = FM.get((dataset, data_id))
        if not fn:
            raise SourceError(f"無樣本 {dataset} {data_id}")
        return json.loads(self._load(fn))["data"]

    def json(self, url, params=None):
        self.calls.append(("json", url))
        for k in self.off:
            if k in url:
                raise SourceError(f"{k} 模擬失效")
        if "MajorInstitutionalTradersDetailsOfFutures" in url:
            return json.loads(self._load("taifex_openapi_FutInstByDate.json"))
        if "MajorInstitutionalTradersDetailsOfCallsAndPuts" in url:
            return json.loads(self._load("taifex_openapi_OptInstCallsPutsByDate.json"))
        for k, fn in OFFICIAL.items():
            if url.rstrip("/").endswith(k):
                return json.loads(self._load(fn))
        raise SourceError(f"無樣本 {url}")

    def bytes(self, url, params=None, post=None):
        self.calls.append(("bytes", url))
        for k in self.off:
            if k in url:
                raise SourceError(f"{k} 模擬失效")
        if "futDataDown" in url:
            return self._load("taifex_futDataDown_TX_" + post["queryStartDate"].replace("/", "") + ".csv")
        if "rate.bot.com.tw/xrt/flcsv/0/" in url:   # 臺灣銀行牌告匯率（樣本只含 USD 一列，遠期欄以 0 表示無報價）
            day = url.rsplit("/", 1)[1]
            self.last_headers = {"content-disposition": f"attachment; filename=ExchangeRate@{day.replace('-', '')}1600.csv"}
            return self._load("bot_flcsv_" + day + ".csv")
        if "vix/log2data" in url:
            if url.endswith("202611new.txt"):
                return self._load("taifex_vix_202611new.txt")
        raise SourceError(f"無樣本 {url}")
