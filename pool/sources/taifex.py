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


def _large_open(ctx, D):
    rows = ctx.cached(("tfx", "ltf"), lambda: ctx.http.json(OPEN + "OpenInterestOfLargeTradersFutures"))
    _check_date(rows, D, "大額交易人")
    return {r["TypeOfTraders"]: [r["Top5Buy"], r["Top5Sell"], r["Top10Buy"], r["Top10Sell"], r["OIOfMarket"]]
            for r in rows if r["Contract"] == "TX" and r["SettlementMonth"] == "999912"}


def _large_csv(ctx, D):
    """largeTraderFutDown：當晚就有目標日（OpenAPI 常晚一天）。欄位：日期、商品、名稱、到期月份、類別、前五買、前五賣、前十買、前十賣、全市場。
    全部月份在 CSV 記為 999999（OpenAPI 為 999912），2026-11-24 兩者數值逐欄相同。"""
    d = iso(D).replace("-", "/")
    raw = ctx.cached(("csv", "ltf", iso(D)), lambda: ctx.http.bytes(
        "https://www.taifex.com.tw/cht/3/largeTraderFutDown", post={"queryStartDate": d, "queryEndDate": d}))
    rows = list(csv.reader(io.StringIO(raw.decode("big5", "ignore"))))
    return {r[4].strip(): [c.strip() for c in r[5:10]] for r in rows
            if len(r) >= 10 and r[0].strip() == d and r[1].strip() == "TX" and r[3].strip() == "999999"}


def large_traders(ctx):
    D = ctx.d("tw_last")
    src = None
    try:
        pick = _large_open(ctx, D)
    except NotYet:
        pick = _large_csv(ctx, D)
        if not pick:
            raise NotYet(f"期交所大額交易人尚未公布 {iso(D)}（OpenAPI 與 CSV 皆無）")
        src = "TAIFEX:largeTraderFutDown（CSV 備援，OpenAPI 尚未更新到目標日）"
    if set(pick) != {"0", "1"}:
        raise SourceError("大額交易人缺 TX 全部月份")
    f = lambda r: {"top5_buy": _i(r[0]), "top5_sell": _i(r[1]), "top10_buy": _i(r[2]), "top10_sell": _i(r[3])}  # noqa: E731
    v = {"scope": "TX+MTX/4 全部月份", "all": f(pick["0"]), "specific": f(pick["1"]), "market_oi": _i(pick["0"][4])}
    return ({**v, "_src": src} if src else v), D


# ---- CSV 下載 ----
def _csv(ctx, kind, commodity, d):
    raw = ctx.cached(("csv", kind, commodity, iso(d)), lambda: ctx.http.bytes(
        f"https://www.taifex.com.tw/cht/3/{kind}", post={"down_type": "1", "commodity_id": commodity,
                                                         "queryStartDate": iso(d).replace("-", "/"),
                                                         "queryEndDate": iso(d).replace("-", "/")}))
    return list(csv.reader(io.StringIO(raw.decode("big5", "ignore"))))


def _near_row(rows, d, session, days=None):
    """近月：6 碼、不含「/」、成交量>0，且不是當日結算的契約（第三個週三的日盤結算後即失效）。"""
    cand = [r for r in rows if len(r) > 17 and r[1] == "TX" and r[0] == iso(d).replace("-", "/")
            and "/" not in r[2] and r[17].strip() == session and (num(r[9]) or 0) > 0
            and txo_mod.expiry(r[2].strip(), days) != d]
    if not cand:
        raise NotYet(f"期交所 futDataDown 尚無 {iso(d)} {session}")
    return min(cand, key=lambda r: r[2].strip())


def tx_day(ctx):
    D = ctx.d("tw_last")
    r = _near_row(_csv(ctx, "futDataDown", "TX", D), D, "一般", ctx.days)
    tc = ctx.items.get("taiex", {}).get("v", {}).get("close")
    return {"contract": r[2].strip(), "open": num(r[3]), "high": num(r[4]), "low": num(r[5]), "close": num(r[6]),
            "settle": num(r[10]), "chg": num(r[7]), "volume": _i(r[9]), "oi": _i(r[11]),
            "basis": rnd(num(r[6]) - tc) if tc is not None else None}, D


def tx_night(ctx):
    N = parse_date(ctx.cal["_night_trade"])
    r = _near_row(_csv(ctx, "futDataDown", "TX", N), N, "盤後", ctx.days)
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


# ---- 選擇權籌碼與波動結構（opt_struct）用的原始列 ----
_WHO = {"自營商": "dealer", "投信": "trust", "外資及陸資": "foreign"}


def opt_chain(ctx, d):
    """optDataDown 一般時段逐履約價列：契約、買賣權、履約價、結算價、未平倉、到期日（CSV 第 21 欄，缺就用規則推算）。"""
    key = iso(d).replace("-", "/")
    out = []
    for r in _csv(ctx, "optDataDown", "TXO", d):
        if len(r) > 17 and r[1] == "TXO" and r[17].strip() == "一般" and r[0] == key:
            c, oi = r[2].strip(), num(r[11])
            raw_exp = r[20].strip() if len(r) > 20 else ""
            exp = parse_date(raw_exp) if len(raw_exp) == 8 and raw_exp.isdigit() else txo_mod.expiry(c, ctx.days)
            out.append({"contract": c, "cp": "call" if r[4].strip() == "買權" else "put", "strike": num(r[3]),
                        "settle": num(r[10]), "oi": int(oi) if oi is not None else None, "exp": exp})
    if not out or not any(x["settle"] is not None for x in out):
        raise NotYet(f"期交所 optDataDown 尚無 {iso(d)}（或結算價未公布）")
    return out


def opt_inst_range(ctx, d0, d1):
    """callsAndPutsDateDown：臺指選擇權三大法人買賣權分計，可查區間（一次取回當日與前一交易日）。"""
    a, b = iso(d0).replace("-", "/"), iso(d1).replace("-", "/")
    raw = ctx.cached(("csv", "callsAndPuts", a, b), lambda: ctx.http.bytes(
        "https://www.taifex.com.tw/cht/3/callsAndPutsDateDown",
        post={"queryStartDate": a, "queryEndDate": b, "commodityId": "TXO"}))
    out = []
    for r in csv.reader(io.StringIO(raw.decode("big5", "ignore"))):
        if len(r) > 12 and r[1].strip() == "臺指選擇權" and r[3].strip() in _WHO and r[2].strip().upper() in ("CALL", "PUT"):
            out.append({"date": r[0].strip().replace("/", "-"), "cp": r[2].strip().lower(), "who": _WHO[r[3].strip()],
                        "buy": _i(r[4]), "sell": _i(r[6]), "long_oi": _i(r[10]), "short_oi": _i(r[12])})
    if not any(x["date"] == iso(d1) for x in out):
        raise NotYet(f"期交所選擇權法人買賣權分計尚無 {iso(d1)}")
    return out


def vix_history(ctx, D, months=5):
    """台指 VIX 每日收盤 [(日期, 收盤)]：往回讀幾個月的官方月檔（每檔不到 1 KB）；讀不到的月份略過。"""
    out, y, m = {}, D.year, D.month
    for _ in range(months):
        try:
            for ymd_, close, _avg in _vix_file(ctx, f"{y}{m:02d}"):
                if close is not None:
                    out[f"{ymd_[:4]}-{ymd_[4:6]}-{ymd_[6:]}"] = close
        except SourceError:
            pass
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return sorted(out.items())


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
