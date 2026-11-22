"""衍生值：只做資料池該算的（已實現波動率、1σ 點數、近 7 日收盤）。8 成區間等研判屬各報告。"""
from __future__ import annotations
import math
import statistics
from .util import rnd


def derive(series_rows: list, taiex_close, vix_close, d: str) -> dict:
    rows = [r for r in series_rows if r["date"] <= d]
    closes = [(r["date"], float(r["close"])) for r in rows]
    out = {"rv20_annual_pct": None, "sigma1_rv_pts": None, "sigma1_vix_pts": None, "closes_7": [[a, b] for a, b in closes[-7:]]}
    if taiex_close is None:
        return out
    if len(closes) >= 21:
        c = [x[1] for x in closes[-21:]]
        rets = [(c[i] / c[i - 1] - 1) * 100 for i in range(1, 21)]
        rv = statistics.stdev(rets) * math.sqrt(252)
        out["rv20_annual_pct"] = rnd(rv)
        out["sigma1_rv_pts"] = rnd(taiex_close * rv / 100 / math.sqrt(252), 1)
    if vix_close is not None:
        out["sigma1_vix_pts"] = rnd(taiex_close * vix_close / 100 / math.sqrt(252), 1)
    return out
