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
LATE_MIN = 60   # 關鍵項目過預計公布時間這麼久還要讀穿，判定資料池排程失靈並設 health.alert
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
    try:   # 公開 repo：免權限直接讀（排程報告的環境沒有 add_repo，也沒有本 repo 的 GitHub 權限）
        import urllib.request
        req = urllib.request.Request(f"https://raw.githubusercontent.com/{REPO}/data/{rel}",
                                     headers={"User-Agent": "tw-data-pool-reader", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        pass
    try:
        return _sh(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/{rel}?ref=data"])
    except Exception:  # noqa: BLE001
        _sh(["git", "fetch", "--depth", "1", "origin", "data"], cwd=ROOT)
        return _sh(["git", "show", f"FETCH_HEAD:{rel}"], cwd=ROOT)


def _last_run_at(data_dir: str | None, digest: dict | None):
    """資料池最近一次執行的時間：取 logs/run_log.csv 最後的 run_at，與 digest 的 generated_at 取較晚者。
    digest 只在內容有變時才重寫，單看 generated_at 會把「有跑、但沒有新資料」誤當成沒跑。兩者都讀不到回傳 None。"""
    times = []
    try:
        import csv, io
        for r in csv.DictReader(io.StringIO(read_data_file("logs/run_log.csv", data_dir).decode("utf-8"))):
            t = dt.datetime.fromisoformat(r["run_at"]) if r.get("run_at") else None
            if t and t.tzinfo:
                times.append(t)
    except Exception:  # noqa: BLE001
        pass
    if digest and digest.get("generated_at"):
        try:
            times.append(dt.datetime.fromisoformat(digest["generated_at"]))
        except ValueError:
            pass
    return max(times) if times else None


_ZH = [
    ("FinMind:", lambda s: f"FinMind（{s.split(':', 1)[1]}）"),
    ("TWSE:fund/BFI82U", lambda s: "證交所 三大法人買賣金額（BFI82U）"),
    ("TWSE:afterTrading/MI_INDEX", lambda s: "證交所 每日收盤行情（MI_INDEX）"),
    ("TWSE:marginTrading/MI_MARGN", lambda s: "證交所 信用交易統計（MI_MARGN）"),
    ("TWSE:", lambda s: "證交所"),
    ("TPEx:", lambda s: "櫃買中心 OpenAPI 指數"),
    ("TAIFEX:openapi/MarketDataOfMajorInstitutionalTradersDetailsOfFutures", lambda s: "期交所 OpenAPI 三大法人期貨"),
    ("TAIFEX:openapi/MarketDataOfMajorInstitutionalTradersDetailsOfCalls", lambda s: "期交所 OpenAPI 三大法人選擇權"),
    ("TAIFEX:openapi/PutCallRatio", lambda s: "期交所 OpenAPI 賣權買權比"),
    ("TAIFEX:openapi/OpenInterestOfLargeTraders", lambda s: "期交所 OpenAPI 大額交易人未沖銷部位"),
    ("TAIFEX:futDataDown", lambda s: "期交所 期貨每日交易行情下載"),
    ("TAIFEX:optDataDown", lambda s: "期交所 選擇權每日交易行情下載"),
    ("TAIFEX:file", lambda s: "期交所 臺指選擇權波動率指數每日檔"),
    ("derived:", lambda s: "資料池自算（逐履約價加總，已排除當日到期）"),
]


def src_zh(src: str | None) -> str:
    for k, f in _ZH:
        if src and src.startswith(k):
            return f(src)
    return src or ""


def brief(name: str, e: dict) -> str:
    v = e.get("v")
    if not isinstance(v, dict):
        return ""
    pick = {
        "taiex": ("close", "chg", "chg_pct", "turnover_bn"), "tpex": ("close", "chg_pct"),
        "inst_spot": ("foreign_bn", "trust_bn", "dealer_bn", "total_bn"),
        "fut_inst": None, "opt_inst": None, "tx_day": ("close", "chg", "oi", "basis"),
        "tx_night": ("close", "chg", "chg_pct", "session"), "txo_oi": ("monthly", "weekly"),
        "pc_ratio": ("oi_pct", "vol_pct", "put_oi", "call_oi"), "large_traders": ("market_oi",),
        "vix": ("close", "chg"), "margin": ("balance_bn", "chg_bn"), "usd_twd": ("mid",),
        "us_vix": ("close", "chg"), "us_yield": ("y10", "y2"),
    }.get(name, ())
    if name == "fut_inst":
        return " ".join(f"{k}={v[k]['net_oi']}({v[k]['chg']:+d})" if v[k].get("chg") is not None else f"{k}={v[k]['net_oi']}" for k in v)
    if name == "txo_oi":
        parts = []
        for c, tp in v.get("top", {}).items():
            cl, pu = tp.get("call") or [[None, None]], tp.get("put") or [[None, None]]
            parts.append(f"{c} 買權最大 {cl[0][0]}({cl[0][1]}) 賣權最大 {pu[0][0]}({pu[0][1]})")
        exp = "、".join(v.get("expiring_today") or []) or "無"
        return "；".join(parts) + f"；當日到期已排除：{exp}"
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

    for e in out_items.values():
        if e.get("src"):
            e["src_zh"] = src_zh(e["src"])

    # 資料池沒按時更新：關鍵項目已過預計公布時間 LATE_MIN 分鐘還要靠讀穿或仍缺，且資料池在預計公布時間之後
    # 一次都沒跑過，才判定排程失靈。資料池有準時跑、只是交易所晚公布的情形不算（那由 pool.run 逾 4 小時轉 missing 警示）。
    last_run = _last_run_at(a.data_dir, digest)
    late = []
    for name in via_direct + missing:
        cfg = cat["items"][name]
        if not cfg.get("key"):
            continue
        try:
            due = cal_mod.due_at(cfg["group"], cfg["due"], cal)
        except Exception:  # noqa: BLE001
            continue
        if now > due + dt.timedelta(minutes=LATE_MIN) and (last_run is None or last_run < due):
            late.append(name)

    hist, series = [], []
    try:
        import csv, io
        series = [r for r in csv.DictReader(io.StringIO(read_data_file("series/taiex_daily.csv", a.data_dir).decode("utf-8")))
                  if r["date"] <= cal["tw_last"]]
        hist = [[r["date"], *(float(r[k]) if r[k] else None for k in ("open", "high", "low", "close"))] for r in series][-60:]
    except Exception:  # noqa: BLE001
        notes.append("讀不到加權指數歷史序列")
    tx = out_items.get("taiex") or {}
    if tx.get("status") in DONE and tx.get("asof") == cal["tw_last"] and (not hist or hist[-1][0] != cal["tw_last"]):
        # 讀穿取得的當日加權：補進近 60 日序列，報告才有今天這根
        v = tx["v"]
        hist = (hist + [[tx["asof"], v.get("open"), v.get("high"), v.get("low"), v.get("close")]])[-60:]
        series = series + [{"date": tx["asof"], "close": v.get("close")}]
    ex = 3 if (digest is None and not via_direct) else 2 if missing else 1 if via_direct else 0
    derived = (digest or {}).get("derived") if (digest and "taiex" not in via_direct and not stale) else None
    derived_via = "pool" if derived else None
    if derived is None and tx.get("status") in DONE and tx.get("asof") == cal["tw_last"] and series:
        # 加權是讀穿來的：用同一支 derive() 在本機算，定義與資料池完全相同
        from pool.derive import derive
        vx = out_items.get("vix") or {}
        vc = vx["v"]["close"] if vx.get("status") in DONE and vx.get("asof") == cal["tw_last"] else None
        try:
            derived = derive(series, tx["v"]["close"], vc, cal["tw_last"])
            derived_via = "direct"
        except Exception as e:  # noqa: BLE001
            notes.append(f"衍生值本機計算失敗：{str(e)[:60]}")
    health = dict((digest or {}).get("health") or {"alert": False, "notes": []})
    if late:
        health["alert"] = True
        health["notes"] = [f"資料池未按時更新（{'、'.join(late)} 已過預計公布時間 {LATE_MIN} 分鐘仍靠讀穿或缺，"
                           f"資料池在預計公布後沒有執行紀錄），最後執行於 "
                           f"{last_run.isoformat(timespec='minutes') if last_run else '無紀錄'}"] + list(health.get("notes") or [])
    result = {
        "snapshot_id": (digest or {}).get("snapshot_id"), "generated_at": (digest or {}).get("generated_at"),
        "profile": a.profile, "date": a.date, "read_at": now.isoformat(timespec="seconds"),
        "calendar": {k: v for k, v in cal.items() if not k.startswith("_")},
        "items": out_items,
        "derived": derived,
        "derived_via": derived_via,
        "taiex_hist": hist,
        "taiex_hist_cols": ["date", "open", "high", "low", "close"],
        "health": health,
        "read": {"exit": ex, "via_direct": via_direct, "missing": missing, "late": late, "notes": notes},
    }
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", "utf-8")

    print(f"資料池快照 {result['snapshot_id'] or '無'}｜產生 {result['generated_at'] or '無'}｜報告日 {a.date}｜結束碼 {ex}")
    print("項目｜狀態｜資料日｜取得｜來源｜數值")
    for n in prof["need"]:
        e = out_items[n]
        print(f"{n}｜{e.get('status', '?')}｜{e.get('asof') or '-'}｜{e.get('via', '-')}｜{e.get('src_zh', '-')}｜{brief(n, e)}")
    d = result["derived"]
    if d:
        tag = "（讀穿資料本機計算）" if derived_via == "direct" else ""
        print(f"衍生{tag}：rv20={d['rv20_annual_pct']}% 1σ(rv)={d['sigma1_rv_pts']} 1σ(vix)={d['sigma1_vix_pts']}")
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
