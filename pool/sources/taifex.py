"""期交所解析：OpenAPI（只給最新一日，須核對 Date）、futDataDown／optDataDown（CSV，Big5）、VIX 月檔。"""
from __future__ import annotations
import csv
import datetime as dt
import io
from .. import txo as txo_mod
from ..util import NotYet, SourceError, num, rnd, ymd, iso, parse_date

OPEN = "https://openapi.taifex.com.tw/v1/"
_FUT = "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
_OPT = "MarketDataOfMajorInstitutionalTradersDetailsOfCallsAndPutsBytheDate"


def _open(ctx, name):
    return ctx.cached(("tfx", name), lambda: ctx.http.json(OPEN + name))


def _check_date(rows, D, what):
    if not rows or str(rows[0].get("Date")) != ymd(D):
        got = rows[0].get("Date") if rows else None
        raise NotYet(f"期交所 {what} 最新為 {got}，尚未公布 {iso(D)}")


def _i(s):
    return int(num(s))


_ITEM = {"外資及陸資": "foreign", "投信": "trust", "自營商": "dealer"}


def fut_inst(ctx):
    D = ctx.d("tw_last")
    rows = _open(ctx, _FUT)
    _check_date(rows, D, "期貨法人")
    cur = {_ITEM[r["Item"]]: _i(r["OpenInterest(Net)"]) for r in rows if r["ContractCode"] == "臺股期貨" and r["Item"] in _ITEM}
    if len(cur) < 3:
        raise SourceError("期貨法人缺列")
    prev = _prev_fut(ctx)
    return {k: {"net_oi": v, "chg": (v - prev[k]) if k in prev else None} for k, v in cur.items()}, D


def _prev_fut(ctx):
    """前一交易日各法人淨未平倉：先看 series/fut_oi.csv，沒有再向 FinMind 取。"""
    prev_day = ctx.cal["tw_prev"]
    if ctx.series is not None:
        for r in ctx.series.rows("fut_oi"):
            if r["date"] == prev_day:
                return {"foreign": int(r["foreign_net_oi"]), "trust": int(r["trust_net_oi"]), "dealer": int(r["dealer_net_oi"])}
    try:
        from . import finmind
        return finmind.fut_net_by_date(ctx).get(prev_day, {})
    except Exception:  # noqa: BLE001
        return {}


def opt_inst(ctx):
    D = ctx.d("tw_last")
    rows = _open(ctx, _OPT)
    _check_date(rows, D, "選擇權法人")
    hit = {r["CallPut"]: r for r in rows if r["ContractCode"] == "臺指選擇權" and r["Item"] == "外資及陸資"}
    if set(hit) != {"CALL", "PUT"}:
        raise SourceError("選擇權法人缺列")
    c, p = hit["CALL"], hit["PUT"]
    k = "ContractValueofOpenInterest(Net)(Thousands)"
    return {"foreign": {"call_net_oi": _i(c["OpenInterest(Net)"]), "call_net_oi_amt_k": _i(c[k]),
                        "put_net_oi": _i(p["OpenInterest(Net)"]), "put_net_oi_amt_k": _i(p[k])}}, D


def pc_ratio(ctx):
    D = ctx.d("tw_last")
    rows = _open(ctx, "PutCallRatio")
    hit = [r for r in rows if str(r.get("Date")) == ymd(D)]
    if not hit:
        raise NotYet("期交所 P/C 尚未公布目標日")
    r = hit[0]
    return {"oi_pct": num(r["PutCallOIRatio%"]), "vol_pct": num(r["PutCallVolumeRatio%"]),
            "put_oi": _i(r["PutOI"]), "call_oi": _i(r["CallOI"])}, D


def large_traders(ctx):
    D = ctx.d("tw_last")
    rows = ctx.cached(("tfx", "ltf"), lambda: ctx.http.json(OPEN + "OpenInterestOfLargeTradersFutures"))
    _check_date(rows, D, "大額交易人")
    pick = {r["TypeOfTraders"]: r for r in rows if r["Contract"] == "TX" and r["SettlementMonth"] == "999912"}
    if set(pick) != {"0", "1"}:
        raise SourceError("大額交易人缺 TX 全部月份")
    f = lambda r: {"top5_buy": _i(r["Top5Buy"]), "top5_sell": _i(r["Top5Sell"]), "top10_buy": _i(r["Top10Buy"]), "top10_sell": _i(r["Top10Sell"])}
    return {"scope": "TX+MTX/4 全部月份", "all": f(pick["0"]), "specific": f(pick["1"]),
            "market_oi": _i(pick["0"]["OIOfMarket"])}, D


# ---- CSV 下載 ----
def _csv(ctx, kind, commodity, d):
    raw = ctx.cached(("csv", kind, commodity, iso(d)), lambda: ctx.http.bytes(
        f"https://www.taifex.com.tw/cht/3/{kind}", post={"down_type": "1", "commodity_id": commodity,
                                                         "queryStartDate": iso(d).replace("-", "/"),
                                                         "queryEndDate": iso(d).replace("-", "/")}))
    return list(csv.reader(io.StringIO(raw.decode("big5", "ignore"))))


def _near_row(rows, d, session):
    cand = [r for r in rows if len(r) > 17 and r[1] == "TX" and r[0] == iso(d).replace("-", "/")
            and "/" not in r[2] and r[17].strip() == session and (num(r[9]) or 0) > 0]
    if not cand:
        raise NotYet(f"期交所 futDataDown 尚無 {iso(d)} {session}")
    return min(cand, key=lambda r: r[2].strip())


def tx_day(ctx):
    D = ctx.d("tw_last")
    r = _near_row(_csv(ctx, "futDataDown", "TX", D), D, "一般")
    tc = ctx.items.get("taiex", {}).get("v", {}).get("close")
    return {"contract": r[2].strip(), "open": num(r[3]), "high": num(r[4]), "low": num(r[5]), "close": num(r[6]),
            "settle": num(r[10]), "chg": num(r[7]), "volume": _i(r[9]), "oi": _i(r[11]),
            "basis": rnd(num(r[6]) - tc) if tc is not None else None}, D


def tx_night(ctx):
    N = parse_date(ctx.cal["_night_trade"])
    r = _near_row(_csv(ctx, "futDataDown", "TX", N), N, "盤後")
    return {"trading_date": iso(N), "session": ctx.cal["_night_session"], "contract": r[2].strip(),
            "open": num(r[3]), "high": num(r[4]), "low": num(r[5]), "close": num(r[6]), "chg": num(r[7]),
            "chg_pct": num(r[8]), "volume": _i(r[9])}, N


def txo_rows(ctx):
    D = ctx.d("tw_last")
    rows = _csv(ctx, "optDataDown", "TXO", D)
    out = [(r[2].strip(), "call" if r[4].strip() == "買權" else "put", num(r[3]), int(num(r[11])))
           for r in rows if len(r) > 17 and r[1] == "TXO" and r[17].strip() == "一般" and num(r[11]) is not None
           and r[0] == iso(D).replace("-", "/")]
    if not out:
        raise NotYet("期交所 optDataDown 尚無目標日")
    return out


# ---- VIX ----
def _vix_file(ctx, ym):
    url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{ym}new.txt"
    raw = ctx.cached(("vix", ym), lambda: ctx.http.bytes(url))
    out = []
    for line in raw.decode("big5", "ignore").splitlines()[2:]:
        p = line.split()
        if len(p) >= 4 and p[0].isdigit():
            out.append((p[0], num(p[2]), num(p[3])))
    return out


def vix(ctx):
    D = ctx.d("tw_last")
    rows = _vix_file(ctx, f"{D.year}{D.month:02d}")
    hit = [r for r in rows if r[0] == ymd(D)]
    if not hit:
        raise NotYet("VIX 月檔尚無目標日")
    prev = [r for r in rows if r[0] < ymd(D)]
    if not prev:   # 月初第一個交易日：向上月檔取前一日
        pm = (D.replace(day=1) - dt.timedelta(days=1))
        prev = _vix_file(ctx, f"{pm.year}{pm.month:02d}")
    chg = rnd(hit[0][1] - prev[-1][1]) if prev else None
    return {"close": hit[0][1], "close_1min_avg": hit[0][2], "chg": chg}, D
