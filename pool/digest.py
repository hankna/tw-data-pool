"""依目錄逐項解析、雙來源比對、組出 digest。"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from . import calendar as cal_mod, txo as txo_mod, validate
from .derive import derive
from .sources import Ctx, bot, finmind, taifex, tpex, twse
from .util import NotYet, SourceError, iso, parse_date

CATALOG = Path(__file__).with_name("catalog.json")


def load_catalog(path=None) -> dict:
    return json.loads(Path(path or CATALOG).read_text("utf-8"))


def _txo_fm(ctx):
    D = ctx.d("tw_last")
    return txo_mod.calc(finmind.txo_rows(ctx), D, ctx.days), D


def _txo_tf(ctx):
    D = ctx.d("tw_last")
    return txo_mod.calc(taifex.txo_rows(ctx), D, ctx.days), D


def _pc_derived(ctx):
    t = ctx.items.get("txo_oi")
    if not t or t.get("status") not in ("ok", "conflict"):
        raise NotYet("txo_oi 尚未就緒")
    s = t["v"]["oi_sum_excl_expiring"]
    return {"oi_pct": round(s["put"] / s["call"] * 100, 2) if s["call"] else None, "vol_pct": None,
            "put_oi": s["put"], "call_oi": s["call"]}, parse_date(t["asof"])


REG = {
    "taiex": {"FinMind": finmind.taiex, "TWSE": twse.taiex},
    "tpex": {"FinMind": finmind.tpex, "TPEx": tpex.tpex},
    "inst_spot": {"TWSE": twse.inst_spot, "FinMind": finmind.inst_spot},
    "fut_inst": {"TAIFEX": taifex.fut_inst, "FinMind": finmind.fut_inst},
    "opt_inst": {"TAIFEX": taifex.opt_inst, "FinMind": finmind.opt_inst},
    "tx_day": {"TAIFEX": taifex.tx_day, "FinMind": finmind.tx_day},
    "tx_night": {"TAIFEX": taifex.tx_night, "FinMind": finmind.tx_night},
    "txo_oi": {"FinMind": _txo_fm, "TAIFEX": _txo_tf},
    "pc_ratio": {"TAIFEX": taifex.pc_ratio, "derived": _pc_derived},
    "large_traders": {"TAIFEX": taifex.large_traders},
    "vix": {"TAIFEX": taifex.vix},
    "margin": {"TWSE": twse.margin, "FinMind": finmind.margin},
    "usd_twd": {"BOT": bot.usd_twd, "FinMind": finmind.usd_twd},
    "us_index": {"FinMind": finmind.us_index},
    "us_stock": {"FinMind": finmind.us_stock},
    "us_vix": {"FinMind": finmind.us_vix},
    "us_yield": {"FinMind": finmind.us_yield},
}
OFFICIAL = {"TWSE", "TAIFEX", "TPEx", "BOT", "derived"}


def chain(name: str, cfg: dict, policy: str):
    """依來源政策排出 [(vendor, label)]；目錄裡的順序已是 official_first 的結果。"""
    out = []
    for s in cfg["sources"]:
        vendor, _, rest = s.partition(":")
        if vendor in REG[name]:
            out.append((vendor, f"{vendor}:{rest.split('(')[0].split('?')[0]}"))
    if policy == "finmind_first":
        out.sort(key=lambda t: t[0] != "FinMind")
    return out


def _get(v, path):
    for p in path.split("."):
        v = v[p]
    return v


def _target(group: str, cal: dict):
    return {"tw_close": cal["tw_last"], "tw_night": cal["_night_trade"], "us_close": cal["us_last"]}[group]


def resolve(name: str, cfg: dict, ctx: Ctx, policy: str) -> dict:
    """回傳項目 entry。成功＝ok／conflict；否則 pending／missing。"""
    results, errors, notyet = [], [], 0
    for vendor, label in chain(name, cfg, policy):
        fn = REG[name][vendor]
        try:
            v, asof = fn(ctx)
            if isinstance(v, dict) and "_src" in v:   # 來源函式改走備援網址時，回報實際來源
                v = dict(v)
                label = v.pop("_src")
            validate.check(name, v, cfg)
            results.append({"vendor": vendor, "label": label, "v": v, "asof": iso(asof)})
            if len(results) == 1:
                # 只有需要雙來源比對的才多取一個來源，其餘第一個成功就停
                if not cfg.get("xcheck") or cfg["xcheck"].get("with") == "pc_ratio":
                    break
            else:
                break
        except NotYet:
            notyet += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{vendor}：{type(e).__name__}：{str(e)[:80]}")
    if not results:
        return {"status": "missing" if errors else "pending", "asof": None, **({"notes": errors} if errors else {})}
    if errors:
        # 前面的來源失敗、由後面的來源補上：原因只印到執行日誌（不進 digest，避免每次失敗都改變快照）。
        # 錯誤字串都由程式組成；帶金鑰的 FinMind 錯誤只含資料集與狀態碼。
        print(f"[來源備援] {name} 改用 {results[0]['vendor']}：" + "；".join(errors), file=sys.stderr)
    prim = results[0]
    entry = {"asof": prim["asof"], "status": "ok", "src": prim["label"], "v": prim["v"]}
    if cfg.get("label"):
        entry["label"] = cfg["label"]
    xc = cfg.get("xcheck")
    if xc and xc.get("with") == "pc_ratio":
        try:
            official, _ = taifex.pc_ratio(ctx)
            s = prim["v"]["oi_sum_excl_expiring"]
            same = s["put"] == official["put_oi"] and s["call"] == official["call_oi"]
            entry["xcheck"] = "match" if same else "conflict"
            if not same:
                entry["status"], entry["alt"] = "conflict", {"pc_ratio_put_call_oi": [official["put_oi"], official["call_oi"]]}
        except (NotYet, SourceError):
            entry["xcheck"] = "n/a"
    elif xc:
        if len(results) == 2:
            a, b = results
            try:
                va, vb = _get(a["v"], xc["field"]), _get(b["v"], xc["field"])
                same = va is None or vb is None or abs(va - vb) <= xc.get("tol", 0) + 1e-9
            except (KeyError, TypeError):
                same = True
            entry["xcheck"] = "match" if same else "conflict"
            if not same:
                off = a if a["vendor"] in OFFICIAL else b
                oth = b if off is a else a
                entry.update(status="conflict", v=off["v"], src=off["label"], alt={"src": oth["label"], "v": oth["v"]})
        else:
            entry["xcheck"] = "n/a"
    return entry


def snapshot_id(items: dict, derived: dict) -> str:
    keep = {k: {x: v for x, v in e.items() if x not in ("notes",)} for k, e in items.items()}
    raw = json.dumps({"items": keep, "derived": derived}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:8]
