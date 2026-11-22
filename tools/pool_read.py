#!/usr/bin/env python3
"""報告端讀取程式（只用標準函式庫）。

  python3 -I tools/pool_read.py --profile chaodilu --date 2026-11-23 --out pool.json

做法：讀 data 分支 latest/digest.json → 依 profile 檢查每個項目的資料日 → 不符、pending、missing 或整份過期
就「讀穿」（匯入 pool/ 同一套程式直接抓，標 via: direct）。報告不寫入資料池。
結束碼：0 全部來自資料池；1 部分讀穿補齊；2 仍有缺項；3 完全失敗（報告退回現行做法）。
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = "hankna/tw-data-pool"
STALE_HOURS = 26
DONE = ("ok", "conflict")


def _sh(cmd, cwd=None, timeout=60):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "ignore")[:120])
    return r.stdout


def read_data_file(rel: str, data_dir: str | None):
    """讀 data 分支的檔案：本機目錄（測試）→ gh api raw → git fetch。回傳 bytes。"""
    if data_dir:
        return (Path(data_dir) / rel).read_bytes()
    try:
        return _sh(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/{rel}?ref=data"])
    except Exception:  # noqa: BLE001
        _sh(["git", "fetch", "--depth", "1", "origin", "data"], cwd=ROOT)
        return _sh(["git", "show", f"FETCH_HEAD:{rel}"], cwd=ROOT)


def brief(name: str, e: dict) -> str:
    v = e.get("v")
    if not isinstance(v, dict):
        return ""
    pick = {
        "taiex": ("close", "chg", "chg_pct", "turnover_bn"), "tpex": ("close", "chg_pct"),
        "inst_spot": ("foreign_bn", "trust_bn", "dealer_bn", "total_bn"),
        "fut_inst": None, "opt_inst": None, "tx_day": ("close", "chg", "oi", "basis"),
        "tx_night": ("close", "chg", "chg_pct", "session"), "txo_oi": ("monthly", "weekly"),
        "pc_ratio": ("oi_pct", "vol_pct"), "large_traders": ("market_oi",),
        "vix": ("close", "chg"), "margin": ("balance_bn", "chg_bn"), "usd_twd": ("mid",),
        "us_vix": ("close", "chg"), "us_yield": ("y10", "y2"),
    }.get(name, ())
    if name == "fut_inst":
        return " ".join(f"{k}={v[k]['net_oi']}({v[k]['chg']:+d})" if v[k].get("chg") is not None else f"{k}={v[k]['net_oi']}" for k in v)
    if name == "opt_inst":
        f = v["foreign"]
        return f"外資 call_net={f['call_net_oi']} put_net={f['put_net_oi']}"
    if name in ("us_index", "us_stock"):
        return " ".join(f"{k}={x['close']}({x['chg_pct']:+}%)" for k, x in v.items())
    return " ".join(f"{k}={v[k]}" for k in (pick or ()) if k in v)


def main(argv=None, http=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--out", default="pool.json")
    ap.add_argument("--data-dir", help="測試用：本機 data 目錄")
    ap.add_argument("--now", help="測試用：覆寫現在時間")
    ap.add_argument("--no-direct", action="store_true", help="不啟動讀穿")
    a = ap.parse_args(argv)

    from pool import calendar as cal_mod, digest as dg
    from pool.http import Http
    from pool.sources import Ctx
    from pool.util import parse_date

    cat = dg.load_catalog()
    prof = cat["profiles"].get(a.profile)
    if not prof:
        print(f"未知 profile：{a.profile}（可用：{', '.join(k for k in cat['profiles'] if not k.startswith('_'))}）")
        return 3
    http = http or Http()
    rdate = parse_date(a.date)
    if a.now:
        now = cal_mod.parse_now(a.now)
    elif rdate == cal_mod.now_tw().date():
        now = cal_mod.now_tw()
    else:
        hh, mm = prof["report"].split()[-1].split(":")
        now = dt.datetime.combine(rdate, dt.time(int(hh), int(mm)), cal_mod.TZ)

    digest, notes = None, []
    try:
        digest = json.loads(read_data_file("latest/digest.json", a.data_dir))
    except Exception as e:  # noqa: BLE001
        notes.append(f"讀不到資料池 digest：{str(e)[:60]}")

    # 交易日曆：先看資料池，再退回 FinMind
    days = []
    try:
        import csv, io
        days = sorted(parse_date(r["date"]) for r in csv.DictReader(io.StringIO(read_data_file("series/tw_calendar.csv", a.data_dir).decode("utf-8"))))
    except Exception:  # noqa: BLE001
        pass
    try:
        if not cal_mod.enough(days, now.date()):
            days = cal_mod.fetch_days(http, now.date())
        cal = cal_mod.compute(now, days)
    except Exception as e:  # noqa: BLE001
        print(f"完全失敗：無交易日曆（{str(e)[:60]}）")
        return 3

    stale = digest is None
    if digest:
        age = (now - dt.datetime.fromisoformat(digest["generated_at"])).total_seconds() / 3600
        if age > STALE_HOURS:
            stale = True
            notes.append(f"資料池已過期（{age:.0f} 小時前產生）")
    ctx = Ctx(http=http, cal=cal, now=now, days=days)
    out_items, via_direct, missing = {}, [], []
    for name in prof["need"]:
        cfg = cat["items"][name]
        tgt = dg._target(cfg["group"], cal)
        e = (digest or {}).get("items", {}).get(name)
        ok = (not stale) and e and e.get("status") in DONE and e.get("asof") == tgt
        if ok:
            out_items[name] = {**e, "via": "pool"}
            ctx.items[name] = e
            continue
        if (name in prof.get("allow_prev", []) and e and e.get("status") in DONE and not stale
                and (e.get("asof") or "") >= cal["tw_prev"]):
            out_items[name] = {**e, "via": "pool", "note": "資料日早於目標日（此項目允許前一日）"}
            continue
        if a.no_direct:
            out_items[name] = e or {"status": "missing", "asof": None}
            missing.append(name)
            continue
        r = dg.resolve(name, cfg, ctx, cat.get("source_policy", "official_first"))
        ctx.items[name] = r
        if r["status"] in DONE:
            out_items[name] = {**r, "via": "direct"}
            via_direct.append(name)
        else:
            old = e if (e and e.get("status") in DONE) else None
            out_items[name] = {**r, "via": "none", **({"stale": {"asof": old["asof"], "v": old.get("v")}} if old else {})}
            missing.append(name)

    ex = 3 if (digest is None and not via_direct) else 2 if missing else 1 if via_direct else 0
    result = {
        "snapshot_id": (digest or {}).get("snapshot_id"), "generated_at": (digest or {}).get("generated_at"),
        "profile": a.profile, "date": a.date, "read_at": now.isoformat(timespec="seconds"),
        "calendar": {k: v for k, v in cal.items() if not k.startswith("_")},
        "items": out_items,
        "derived": (digest or {}).get("derived") if (digest and "taiex" not in via_direct and not stale) else None,
        "health": (digest or {}).get("health"),
        "read": {"exit": ex, "via_direct": via_direct, "missing": missing, "notes": notes},
    }
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", "utf-8")

    print(f"資料池快照 {result['snapshot_id'] or '無'}｜產生 {result['generated_at'] or '無'}｜報告日 {a.date}｜結束碼 {ex}")
    print(f"{'項目':<14}{'狀態':<9}{'資料日':<12}{'來源':<10}數值")
    for n in prof["need"]:
        e = out_items[n]
        print(f"{n:<14}{e.get('status', '?'):<9}{(e.get('asof') or '-'):<12}{e.get('via', '-'):<10}{brief(n, e)}")
    d = result["derived"]
    if d:
        print(f"衍生：rv20={d['rv20_annual_pct']}% 1σ(rv)={d['sigma1_rv_pts']} 1σ(vix)={d['sigma1_vix_pts']}")
    elif a.profile != "midday":
        notes.append("衍生值未提供（加權資料非資料池當日版本）")
    for n in notes:
        print("備註：" + n)
    if via_direct:
        print("讀穿補齊：" + "、".join(via_direct))
    if missing:
        print("缺：" + "、".join(missing))
    if result["health"] and result["health"].get("alert"):
        print("警示：health.alert=true；" + "；".join(result["health"].get("notes", [])[:2]))
    return ex


if __name__ == "__main__":
    sys.exit(main())
