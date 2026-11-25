"""臺灣銀行牌告匯率（rate.bot.com.tw）。

https://rate.bot.com.tw/xrt/flcsv/0/<YYYY-MM-DD> 是該日最後一次掛牌的牌告匯率 CSV（UTF-8 含 BOM，無日期欄）。
每列一個幣別、22 欄：幣別、「本行買入」、現金、即期、遠期 10～180 天，接著「本行賣出」與同樣欄位，結尾多一個逗號。
0.00000 代表該欄沒有報價。回應檔名 ExchangeRate@YYYYMMDDHHMM.csv 帶資料日與掛牌時間；週末、未來日期回 HTTP 200
純文字「查無資料」（檔名 XRT.csv）。
2026-11-20 USD 即期 32.89／32.99、現金 32.54／33.21，11/24、11/25 亦經實測，皆與 FinMind TaiwanExchangeRate 同日相同。
臺銀一天掛牌多次（11/25 為 09:01～16:02），所以當日牌價 16:30 後才收錄。
"""
from __future__ import annotations
import csv
import io
import re
from ..util import NotYet, SourceError, rnd, iso

BASE = "https://rate.bot.com.tw/xrt/flcsv/0/"
READY = "16:30"
_FNAME = re.compile(r"ExchangeRate(?:@|%40)(\d{8})(\d{4})\.csv", re.I)   # 檔名可能以 URL 編碼送出


def _fetch(ctx, D):
    def get():
        body = ctx.http.bytes(BASE + iso(D))
        return body, dict(getattr(ctx.http, "last_headers", {}) or {})
    return ctx.cached(("bot", iso(D)), get)


def usd_twd(ctx):
    D = ctx.d("tw_last")
    ctx.not_before(D, READY, "臺灣銀行牌告匯率")
    raw, headers = _fetch(ctx, D)
    text = raw.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    usd = [r for r in rows if r and r[0].strip() == "USD"]
    if not usd:
        if "查詢找不到" in text or "XRT.csv" in headers.get("content-disposition", ""):
            raise NotYet(f"臺灣銀行牌告匯率尚無 {iso(D)}")     # 查無資料（週末、未來日期）回純文字 XRT.csv
        # 既不是牌告 CSV、也不是查無資料（例如被擋下的網頁）：當成錯誤改走 FinMind，只記內容類型與長度，不記原文
        raise SourceError(f"臺灣銀行回應不是牌告 CSV（content-type {headers.get('content-type', '?')[:40]}，"
                          f"{len(raw)} bytes）")
    # CSV 沒有日期欄：有報價就必須從檔名確認資料日，缺檔名或格式改變一律拒收、改走 FinMind
    m = _FNAME.search(headers.get("content-disposition", ""))
    if not m:
        raise SourceError("臺灣銀行回應缺少可辨識的檔名，無法確認資料日")
    if m.group(1) != iso(D).replace("-", ""):
        raise SourceError(f"臺灣銀行回應的資料日 {m.group(1)} 與請求日 {iso(D)} 不符")
    head = [c.strip() for c in rows[0]]
    r = [c.strip() for c in usd[0]]
    if (len(head) < 14 or head[3] != "即期" or head[13] != "即期" or len(r) < 14
            or r[1] != "本行買入" or r[11] != "本行賣出"):
        raise SourceError("臺灣銀行牌告匯率 CSV 欄位與預期不符")
    try:
        buy, sell = float(r[3]), float(r[13])
    except ValueError:
        raise SourceError("臺灣銀行牌告匯率即期欄不是數字") from None
    if buy <= 0 or sell <= 0:
        raise NotYet(f"臺灣銀行 {iso(D)} 無美元即期報價")
    return {"spot_buy": buy, "spot_sell": sell, "mid": rnd((buy + sell) / 2)}, D
