"""臺灣銀行牌告匯率（rate.bot.com.tw）。

https://rate.bot.com.tw/xrt/flcsv/0/<YYYY-MM-DD> 是該日的牌告匯率 CSV（UTF-8 含 BOM，無日期欄）。
每列一個幣別、22 欄：幣別、「本行買入」、現金、即期、遠期 10～180 天，接著「本行賣出」與同樣欄位，結尾多一個逗號。
0.00000 代表該欄沒有報價。2026-11-20 的 USD 即期買入 32.89、賣出 32.99、現金 32.54／33.21，與 FinMind
TaiwanExchangeRate 同日數值逐欄相同（FinMind 的匯率資料即來自臺灣銀行）。
"""
from __future__ import annotations
import csv
import io
from ..util import NotYet, SourceError, rnd, iso

BASE = "https://rate.bot.com.tw/xrt/flcsv/0/"


def usd_twd(ctx):
    D = ctx.d("tw_last")
    raw = ctx.cached(("bot", iso(D)), lambda: ctx.http.bytes(BASE + iso(D)))
    rows = [r for r in csv.reader(io.StringIO(raw.decode("utf-8-sig", "replace"))) if r and r[0].strip() == "USD"]
    if not rows:
        raise NotYet(f"臺灣銀行牌告匯率尚無 {iso(D)}")
    r = [c.strip() for c in rows[0]]
    if len(r) < 14 or r[1] != "本行買入" or r[11] != "本行賣出":
        raise SourceError("臺灣銀行牌告匯率 CSV 欄位與預期不符")
    buy, sell = float(r[3]), float(r[13])
    if buy <= 0 or sell <= 0:
        raise NotYet(f"臺灣銀行 {iso(D)} 無美元即期報價")
    return {"spot_buy": buy, "spot_sell": sell, "mid": rnd((buy + sell) / 2)}, D
