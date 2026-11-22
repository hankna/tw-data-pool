"""FinMind 解析。單位陷阱：金額欄位是「元」。"""
from __future__ import annotations
import datetime as dt
from ..util import NotYet, SourceError, rnd, iso, parse_date

_B = 1e8


def _rows(ctx, dataset, data_id, start, end):
    return ctx.cached(("fm", dataset, data_id, iso(start), iso(end)),
                      lambda: ctx.http.finmind(dataset, data_id, iso(start), iso(end)))


def _sorted(rows):
    return sorted(rows, key=lambda r: r["date"])


def _need(rows, d, what):
    hit = [r for r in rows if r["date"] == iso(d)]
    if not hit:
        raise NotYet(f"FinMind {what} 尚無 {iso(d)}")
    return hit


def taiex(ctx):
    D = ctx.d("tw_last")
    rows = _sorted(_rows(ctx, "TaiwanStockPrice", "TAIEX", D - dt.timedelta(days=50), D + dt.timedelta(days=1)))
    r = _need(rows, D, "TAIEX")[0]
    ctx.side["taiex_hist"] = [(x["date"], x["open"], x["max"], x["min"], x["close"], rnd(x["Trading_money"] / _B, 2)) for x in rows]
    close, spread = r["close"], r["spread"]
    return {"open": r["open"], "high": r["max"], "low": r["min"], "close": close, "chg": spread,
            "chg_pct": rnd(spread / (close - spread) * 100), "turnover_bn": rnd(r["Trading_money"] / _B, 2)}, D


def tpex(ctx):
    D = ctx.d("tw_last")
    rows = _sorted(_rows(ctx, "TaiwanStockPrice", "TPEx", D - dt.timedelta(days=10), D + dt.timedelta(days=1)))
    r = _need(rows, D, "TPEx")[0]
    return {"close": r["close"], "chg_pct": rnd(r["spread"] / (r["close"] - r["spread"]) * 100)}, D


def inst_spot(ctx):
    D = ctx.d("tw_last")
    rows = _rows(ctx, "TaiwanStockTotalInstitutionalInvestors", None, D - dt.timedelta(days=5), D + dt.timedelta(days=1))
    day = {r["name"]: (r["buy"] - r["sell"]) / _B for r in _need(rows, D, "三大法人")}
    try:
        f = day["Foreign_Investor"] + day.get("Foreign_Dealer_Self", 0.0)
        ds, dh = day["Dealer_self"], day["Dealer_Hedging"]
        return {"foreign_bn": rnd(f), "trust_bn": rnd(day["Investment_Trust"]), "dealer_bn": rnd(ds + dh),
                "dealer_self_bn": rnd(ds), "dealer_hedge_bn": rnd(dh), "total_bn": rnd(day["total"])}, D
    except KeyError as e:
        raise SourceError(f"FinMind 三大法人缺欄位 {e}") from None


def margin(ctx):
    D = ctx.d("tw_last")
    rows = _rows(ctx, "TaiwanStockTotalMarginPurchaseShortSale", None, D - dt.timedelta(days=5), D + dt.timedelta(days=1))
    by = {r["name"]: r for r in _need(rows, D, "融資融券")}
    m, s = by["MarginPurchaseMoney"], by["ShortSale"]
    return {"balance_bn": rnd(m["TodayBalance"] / _B), "chg_bn": rnd((m["TodayBalance"] - m["YesBalance"]) / _B),
            "short_balance": s["TodayBalance"], "short_chg": s["TodayBalance"] - s["YesBalance"]}, D


_INST = {"外資": "foreign", "投信": "trust", "自營商": "dealer"}


def fut_net_by_date(ctx):
    """{日期: {foreign|trust|dealer: 淨未平倉口數}}（TX）。"""
    D = ctx.d("tw_last")
    rows = _rows(ctx, "TaiwanFuturesInstitutionalInvestors", "TX", D - dt.timedelta(days=10), D + dt.timedelta(days=1))
    out = {}
    for r in rows:
        k = _INST.get(r["institutional_investors"])
        if k:
            out.setdefault(r["date"], {})[k] = r["long_open_interest_balance_volume"] - r["short_open_interest_balance_volume"]
    return out


def fut_inst(ctx):
    D = ctx.d("tw_last")
    by = fut_net_by_date(ctx)
    cur = by.get(iso(D))
    if not cur or len(cur) < 3:
        raise NotYet("FinMind 期貨法人尚無目標日")
    prev = by.get(ctx.cal["tw_prev"], {})
    return {k: {"net_oi": cur[k], "chg": (cur[k] - prev[k]) if k in prev else None} for k in ("foreign", "trust", "dealer")}, D


def opt_inst(ctx):
    D = ctx.d("tw_last")
    rows = _rows(ctx, "TaiwanOptionInstitutionalInvestors", "TXO", D - dt.timedelta(days=3), D + dt.timedelta(days=1))
    hit = {r["call_put"]: r for r in _need(rows, D, "選擇權法人") if r["institutional_investors"] == "外資"}
    if set(hit) != {"買權", "賣權"}:
        raise NotYet("FinMind 選擇權外資資料不完整")
    c, p = hit["買權"], hit["賣權"]
    f = lambda r: (r["long_open_interest_balance_volume"] - r["short_open_interest_balance_volume"],
                   r["long_open_interest_balance_amount"] - r["short_open_interest_balance_amount"])
    return {"foreign": {"call_net_oi": f(c)[0], "call_net_oi_amt_k": f(c)[1],
                        "put_net_oi": f(p)[0], "put_net_oi_amt_k": f(p)[1]}}, D


def _near(rows):
    """近月：到期月份 6 碼、不含「/」、成交量>0 之中最小者。"""
    c = sorted({r["contract_date"] for r in rows if len(r["contract_date"]) == 6 and "/" not in r["contract_date"] and r["volume"] > 0})
    if not c:
        raise NotYet("無成交的近月契約")
    return [r for r in rows if r["contract_date"] == c[0]][0]


def _tx(ctx, d, session):
    rows = _rows(ctx, "TaiwanFuturesDaily", "TX", d - dt.timedelta(days=3), d + dt.timedelta(days=1))
    return _near([r for r in _need(rows, d, "TX") if r["trading_session"] == session])


def tx_day(ctx):
    D = ctx.d("tw_last")
    r = _tx(ctx, D, "position")
    tc = ctx.items.get("taiex", {}).get("v", {}).get("close")
    return {"contract": r["contract_date"], "open": r["open"], "high": r["max"], "low": r["min"], "close": r["close"],
            "settle": r["settlement_price"], "chg": r["spread"], "volume": r["volume"], "oi": r["open_interest"],
            "basis": rnd(r["close"] - tc) if tc is not None else None}, D


def tx_night(ctx):
    N = parse_date(ctx.cal["_night_trade"])
    r = _tx(ctx, N, "after_market")
    return {"trading_date": iso(N), "session": ctx.cal["_night_session"], "contract": r["contract_date"],
            "open": r["open"], "high": r["max"], "low": r["min"], "close": r["close"], "chg": r["spread"],
            "chg_pct": r["spread_per"], "volume": r["volume"]}, N


def txo_rows(ctx):
    D = ctx.d("tw_last")
    rows = _rows(ctx, "TaiwanOptionDaily", "TXO", D, D)
    pos = [r for r in rows if r["trading_session"] == "position" and r["date"] == iso(D)]
    if not pos:
        raise NotYet("FinMind 選擇權逐履約價尚無目標日")
    return [(r["contract_date"], r["call_put"], r["strike_price"], r["open_interest"]) for r in pos]


def usd_twd(ctx):
    D = ctx.d("tw_last")
    rows = _rows(ctx, "TaiwanExchangeRate", "USD", D - dt.timedelta(days=5), D + dt.timedelta(days=1))
    r = _need(rows, D, "匯率")[0]
    return {"spot_buy": r["spot_buy"], "spot_sell": r["spot_sell"], "mid": rnd((r["spot_buy"] + r["spot_sell"]) / 2)}, D


# ---- 美股 ----
def _us_asof(ctx, latest: str) -> dt.date:
    """最新一筆日期 ≥ 預期日才算到；若已過預期日 05:00 起算 10 小時仍沒有，視為美國休市、採最新一筆。"""
    X, L = ctx.d("us_last"), parse_date(latest)
    if L >= X:
        return L
    from ..calendar import TZ
    deadline = dt.datetime.combine(X + dt.timedelta(days=1), dt.time(15, 0), TZ)
    if ctx.now >= deadline:
        return L
    raise NotYet(f"美股尚無 {iso(X)}")


def _us_series(ctx, sid):
    X = ctx.d("us_last")
    rows = _sorted(_rows(ctx, "USStockPrice", sid, X - dt.timedelta(days=14), X + dt.timedelta(days=2)))
    if len(rows) < 2:
        raise NotYet(f"美股 {sid} 資料不足")
    return rows


def _us_pack(ctx, ids):
    out, asof = {}, None
    for sid in ids:
        rows = _us_series(ctx, sid)
        L = _us_asof(ctx, rows[-1]["date"])
        asof = asof or L
        a, b = rows[-1], rows[-2]
        out[sid] = {"close": a["Close"], "chg_pct": rnd((a["Close"] / b["Close"] - 1) * 100)}
    return out, asof


def us_index(ctx):
    return _us_pack(ctx, ["^GSPC", "^IXIC", "^DJI", "^SOX"])


def us_stock(ctx):
    return _us_pack(ctx, ["NVDA", "TSM", "AMD", "AVGO", "MU"])


def us_vix(ctx):
    rows = _us_series(ctx, "^VIX")
    L = _us_asof(ctx, rows[-1]["date"])
    return {"close": rows[-1]["Close"], "chg": rnd(rows[-1]["Close"] - rows[-2]["Close"])}, L


def us_yield(ctx):
    X = ctx.d("us_last")
    got = {}
    for key, name in (("y10", "United States 10-Year"), ("y2", "United States 2-Year")):
        rows = _sorted(_rows(ctx, "GovernmentBondsYield", name, X - dt.timedelta(days=14), X + dt.timedelta(days=2)))
        if len(rows) < 2:
            raise NotYet(f"{name} 資料不足")
        got[key] = rows
    L = _us_asof(ctx, got["y10"][-1]["date"])
    out = {}
    for k, rows in got.items():
        if rows[-1]["date"] != got["y10"][-1]["date"]:
            raise NotYet("10 年與 2 年殖利率日期不一致")
        out[k] = rows[-1]["value"]
        out[f"{k}_chg_bp"] = rnd((rows[-1]["value"] - rows[-2]["value"]) * 100, 1)
    return out, L
