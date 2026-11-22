"""櫃買中心 openapi 解析。"""
from __future__ import annotations
from ..util import NotYet, num, rnd, ymd, iso


def tpex(ctx):
    D = ctx.d("tw_last")
    rows = ctx.http.json("https://www.tpex.org.tw/openapi/v1/tpex_index")
    hit = [r for r in rows if r.get("Date") == ymd(D)]
    if not hit:
        raise NotYet(f"櫃買指數尚無 {iso(D)}")
    close, chg = num(hit[0]["Close"]), num(hit[0]["Change"])
    return {"close": close, "chg_pct": rnd(chg / (close - chg) * 100)}, D
