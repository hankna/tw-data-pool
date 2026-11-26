"""寫入前檢核：不過就不寫，該來源視為失敗。"""
from __future__ import annotations
import math
from .util import SourceError


def _walk(v, path=""):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from _walk(x, f"{path}.{k}" if path else k)
    elif isinstance(v, (list, tuple)):
        for i, x in enumerate(v):
            yield from _walk(x, f"{path}[{i}]")
    else:
        yield path, v


def check(name: str, v: dict, cfg: dict) -> None:
    for p, x in _walk(v):
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            raise SourceError(f"{name}.{p} 非有限數")
    rule = cfg.get("valid") or {}
    if name == "taiex":
        lo, hi = rule["close"]
        if not lo <= v["close"] <= hi:
            raise SourceError(f"加權收盤 {v['close']} 超出合理範圍")
        if abs(v["chg_pct"]) > rule["abs_chg_pct_max"]:
            raise SourceError(f"加權漲跌幅 {v['chg_pct']}% 超過 {rule['abs_chg_pct_max']}%")
        if v["turnover_bn"] < rule["turnover_bn_min"]:
            raise SourceError("加權成交金額過小")
    if name == "inst_spot":
        if abs(v["foreign_bn"] + v["trust_bn"] + v["dealer_bn"] - v["total_bn"]) > 0.02:
            raise SourceError("三大法人分項加總不等於合計")
    nonneg = {
        "tx_day": ["oi", "volume"], "tx_night": ["volume"],
        "pc_ratio": ["put_oi", "call_oi"],
        "large_traders": ["market_oi"],
    }.get(name, [])
    for k in nonneg:
        if v.get(k) is not None and v[k] < 0:
            raise SourceError(f"{name}.{k} 為負數")
    if name == "txo_oi":
        s = v["oi_sum_excl_expiring"]
        if s["call"] < 0 or s["put"] < 0 or not v.get("monthly"):
            raise SourceError("選擇權未平倉彙總異常")
    if name == "opt_struct":
        o, m = v["oi"], (v["vol"] or {}).get("month")
        if o["call_oi"] <= 0 or o["put_oi"] <= 0:
            raise SourceError("選擇權未平倉合計異常")
        if not m or not 3 <= m["iv_atm"] <= 400:
            raise SourceError("月選價平隱含波動率缺漏或超出合理範圍")
