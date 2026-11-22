"""證交所（rwd 端點）解析。偶爾回空字串或「沒有符合條件的資料」，重打即成功。"""
from __future__ import annotations
import time
from ..util import NotYet, SourceError, num, rnd, ymd, iso

SLEEP = 2
BASE = "https://www.twse.com.tw/rwd/zh/"


def _get(ctx, path, params, tries=3):
    last = None
    for i in range(tries):
        try:
            j = ctx.http.json(BASE + path, params)
            if isinstance(j, dict) and j.get("stat") not in (None, "OK"):
                raise NotYet(f"證交所 {path}：{j.get('stat')}")
            return j
        except NotYet:
            raise
        except SourceError as e:
            last = e
            time.sleep(SLEEP)
    raise SourceError(f"證交所 {path} 失敗：{last}")


def _day(ctx, path, params, D):
    j = _get(ctx, path, {**params, "response": "json"})
    if str(j.get("date")) != ymd(D):
        raise NotYet(f"證交所 {path} 資料日 {j.get('date')} ≠ {iso(D)}")
    return j


def taiex(ctx):
    D = ctx.d("tw_last")
    j = _day(ctx, "afterTrading/MI_INDEX", {"date": ymd(D), "type": "IND"}, D)
    row = None
    for t in j.get("tables", []):
        for r in t.get("data", []):
            if r and r[0] == "發行量加權股價指數":
                row = r
    if not row:
        raise SourceError("MI_INDEX 找不到加權指數列")
    close, pts, pct = num(row[1]), num(row[3]), num(row[4])
    sign = -1 if "-" in str(row[2]) else 1
    chg, chg_pct = sign * pts, sign * abs(pct)
    # 成交金額：FMTQIK（元）
    f = _get(ctx, "afterTrading/FMTQIK", {"date": ymd(D), "response": "json"})
    roc = f"{D.year - 1911}/{D.month:02d}/{D.day:02d}"
    hit = [r for r in f.get("data", []) if r[0] == roc]
    if not hit:
        raise NotYet("FMTQIK 尚無目標日")
    turnover = rnd(num(hit[0][2]) / 1e8)
    o = h = l = None
    try:   # 開高低：既有程式已在用的 MI_5MINS_HIST（月檔）
        m = _get(ctx, "TAIEX/MI_5MINS_HIST", {"date": f"{D.year}{D.month:02d}01", "response": "json"})
        for r in m.get("data", []):
            if r[0] == roc:
                o, h, l = num(r[1]), num(r[2]), num(r[3])
    except (SourceError, NotYet):
        pass
    return {"open": o, "high": h, "low": l, "close": close, "chg": chg, "chg_pct": chg_pct, "turnover_bn": turnover}, D


def inst_spot(ctx):
    D = ctx.d("tw_last")
    j = _day(ctx, "fund/BFI82U", {"type": "day", "dayDate": ymd(D)}, D)
    d = {r[0]: num(r[3]) / 1e8 for r in j["data"]}
    try:
        foreign = d["外資及陸資(不含外資自營商)"] + d.get("外資自營商", 0.0)
        ds, dh = d["自營商(自行買賣)"], d["自營商(避險)"]
        return {"foreign_bn": rnd(foreign), "trust_bn": rnd(d["投信"]), "dealer_bn": rnd(ds + dh),
                "dealer_self_bn": rnd(ds), "dealer_hedge_bn": rnd(dh), "total_bn": rnd(d["合計"])}, D
    except KeyError as e:
        raise SourceError(f"BFI82U 缺列 {e}") from None


def margin(ctx):
    D = ctx.d("tw_last")
    j = _day(ctx, "marginTrading/MI_MARGN", {"date": ymd(D), "selectType": "MS"}, D)
    rows = {r[0]: r for r in j["tables"][0]["data"]}
    money, short = rows["融資金額(仟元)"], rows["融券(交易單位)"]
    today, prev = num(money[5]), num(money[4])   # 仟元 → ÷1e5 得億元
    return {"balance_bn": rnd(today / 1e5), "chg_bn": rnd((today - prev) / 1e5),
            "short_balance": int(num(short[5])), "short_chg": int(num(short[5]) - num(short[4]))}, D
