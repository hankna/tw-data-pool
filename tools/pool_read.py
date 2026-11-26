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
CLOCK_LATE_MIN = 30   # 與 pool/run.py 相同：時鐘觸發晚到這麼多分鐘算警示
CLOCK_WINDOW_H = 6    # 報告只看最近幾小時內的時鐘延遲事件，避免同一事件在後面每份報告都再發一次簡訊
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


def _run_log(data_dir: str | None, now: dt.datetime):
    """讀 logs/run_log.csv，回傳 (排程與時鐘執行時間清單, 近 CLOCK_WINDOW_H 小時內的時鐘延遲事件)。
    只認 trigger 為 clock 或 schedule、且 run_at 不晚於 now 的列：手動補抓（可能只補單一項目，或用 --date
    以模擬時間重算）不代表各項目都有按時抓過。讀不到日誌時回傳 (None, [])，由呼叫端略過排程檢查，
    不拿 digest 的 generated_at 充當執行證據（它可能來自手動補抓）。"""
    runs, late = [], []
    try:
        import csv, io
        rows = list(csv.DictReader(io.StringIO(read_data_file("logs/run_log.csv", data_dir).decode("utf-8"))))
    except Exception:  # noqa: BLE001
        return None, late
    for r in rows:
        try:
            if r.get("trigger") not in ("clock", "schedule") or not r.get("run_at"):
                continue
            t = dt.datetime.fromisoformat(r["run_at"])
            if not t.tzinfo or t > now:
                continue
            runs.append(t)
            if r["trigger"] == "clock" and r.get("scheduled_for") and r.get("delay_min"):
                sf = dt.datetime.fromisoformat(r["scheduled_for"])
                if (sf.tzinfo and now - dt.timedelta(hours=CLOCK_WINDOW_H) < sf <= now
                        and float(r["delay_min"]) > CLOCK_LATE_MIN):
                    late.append(f"時鐘觸發延遲 {r['delay_min']} 分（應於 {r['scheduled_for'][5:16].replace('T', ' ')} 執行）")
        except ValueError:
            continue
    return runs, late


_ZH = [
    ("FinMind:", lambda s: f"FinMind（{s.split(':', 1)[1]}）"),
    ("TWSE:fund/BFI82U", lambda s: "證交所 三大法人買賣金額（BFI82U）"),
    ("TWSE:afterTrading/MI_INDEX", lambda s: "證交所 每日收盤行情（MI_INDEX）"),
    ("TWSE:marginTrading/MI_MARGN", lambda s: "證交所 信用交易統計（MI_MARGN）"),
    ("TWSE:", lambda s: "證交所"),
    ("TPEx:", lambda s: "櫃買中心 OpenAPI 指數"),
    ("BOT:", lambda s: "臺灣銀行 牌告匯率"),
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
        if name in prof.get("allow_prev", []) and not stale:
            # latest 在新交易日開盤後就把今日改成 pending，前一日的值只留在 daily/{前一交易日}/digest.json
            prev = e if (e and e.get("status") in DONE and (e.get("asof") or "") >= cal["tw_prev"]) else None
            if prev is None:
                try:
                    pe = json.loads(read_data_file(f"daily/{cal['tw_prev']}/digest.json", a.data_dir)
                                    .decode("utf-8")).get("items", {}).get(name)
                except Exception:  # noqa: BLE001
                    pe = None
                if pe and pe.get("status") in DONE and (pe.get("asof") or "") >= cal["tw_prev"]:
                    prev = pe
            if prev:
                out_items[name] = {**prev, "via": "pool", "note": "資料日早於目標日（此項目允許前一日）"}
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

    # 資料池沒按時更新：關鍵項目已過「預計公布＋LATE_MIN」，而在 [預計公布, 預計公布＋LATE_MIN] 之間沒有任何排程或
    # 時鐘執行，就是排程失靈。期限後才到的備援不算準時；資料池有準時跑、只是交易所晚公布的情形不算（那由 pool.run
    # 逾 4 小時轉 missing 警示）。檢查對象：仍要讀穿或仍缺的關鍵項目（late），以及雖已由資料池提供、但預計公布在
    # 最近 CLOCK_WINDOW_H 小時內的關鍵項目（late_filled：逾時後才補齊，排程仍失靈；更早的不再重複發簡訊）。
    runs, clock_late = _run_log(a.data_dir, now)
    last_run = max(runs) if runs else None
    late, late_filled = [], []
    if runs is None:
        notes.append("讀不到資料池執行紀錄（logs/run_log.csv），未檢查排程是否準時")
    for name in ([] if runs is None else prof["need"]):
        cfg = cat["items"][name]
        if not cfg.get("key"):
            continue
        try:
            due = cal_mod.due_at(cfg["group"], cfg["due"], cal)
        except Exception:  # noqa: BLE001
            continue
        limit = due + dt.timedelta(minutes=LATE_MIN)
        if now <= limit or any(due <= t <= limit for t in runs):
            continue
        if name in via_direct or name in missing:
            late.append(name)
        elif now - due <= dt.timedelta(hours=CLOCK_WINDOW_H):
            late_filled.append(name)

    hist, series = [], []
    try:
        import csv, io
        series = [r for r in csv.DictReader(io.StringIO(read_data_file("series/taiex_daily.csv", a.data_dir).decode("utf-8")))
                  if r["date"] <= cal["tw_last"]]
        hist = [[r["date"], *(float(r[k]) if r[k] else None for k in ("open", "high", "low", "close"))] for r in series][-60:]
    except Exception:  # noqa: BLE001
        notes.append("讀不到加權指數歷史序列")
    tx = out_items.get("taiex") or {}
    if tx.get("status") in DONE and tx.get("asof") == cal["tw_last"] and (
            tx.get("via") == "direct" or not hist or hist[-1][0] != cal["tw_last"]):
        # 當日加權補進近 60 日序列；讀穿值一律覆蓋序列裡同日的舊 K 棒（可能是修訂前的值），RV 與點數才用同一個收盤
        v, d0 = tx["v"], tx["asof"]
        hist = ([h for h in hist if h[0] != d0] + [[d0, v.get("open"), v.get("high"), v.get("low"), v.get("close")]])[-60:]
        series = [r for r in series if r["date"] != d0] + [{"date": d0, "close": v.get("close")}]
    ex = 3 if (digest is None and not via_direct) else 2 if missing else 1 if via_direct else 0
    derived = (digest or {}).get("derived") if (digest and tx.get("via") == "pool" and not stale) else None
    if derived and ((derived.get("closes_7") or [[None]])[-1][0] != tx.get("asof")):
        derived = None   # 資料池的衍生值算自別的資料日（例如當日加權仍缺），不沿用
    derived_via = "pool" if derived else None
    vx = out_items.get("vix") or {}
    vc_today = vx["v"]["close"] if vx.get("status") in DONE and vx.get("asof") == cal["tw_last"] else None
    if derived and vx.get("via") == "direct" and vc_today is not None:
        # 加權來自資料池、VIX 是讀穿的：資料池的 VIX 1σ 算在 VIX 公布前（為空或舊值），用同一支 derive() 重算這一欄
        from pool.derive import derive
        derived = {**derived, "sigma1_vix_pts": derive([], tx["v"]["close"], vc_today, cal["tw_last"])["sigma1_vix_pts"]}
        notes.append("VIX 1σ 以讀穿的當日台指 VIX 本機重算")
    if derived is None and tx.get("status") in DONE and tx.get("asof") == cal["tw_last"] and series:
        # 加權是讀穿來的：用同一支 derive() 在本機算，定義與資料池完全相同
        from pool.derive import derive
        try:
            derived = derive(series, tx["v"]["close"], vc_today, cal["tw_last"])
            derived_via = "direct"
        except Exception as e:  # noqa: BLE001
            notes.append(f"衍生值本機計算失敗：{str(e)[:60]}")
    health = dict((digest or {}).get("health") or {"alert": False, "notes": []})
    if "bad" in health:
        # 資料健康（缺漏、衝突）照沿用；digest 裡的時鐘延遲可能是幾小時前的事件，改由 run_log 時間窗重新判斷
        health["alert"] = bool(health["bad"])
        health["notes"] = [n for n in (health.get("notes") or []) if not n.startswith("時鐘觸發延遲")]
        health["clock_late"] = bool(clock_late)
    if clock_late:
        health["alert"] = True
        health["notes"] = [n for n in clock_late if n not in (health.get("notes") or [])] + list(health.get("notes") or [])
    if late_filled:
        health["alert"] = True
        health["notes"] = [f"資料池排程失靈但已補齊（{'、'.join(late_filled)} 在預計公布後 {LATE_MIN} 分鐘內沒有排程執行紀錄，"
                           f"目前資料已齊）"] + list(health.get("notes") or [])
    if late:
        health["alert"] = True
        health["notes"] = [f"資料池未按時更新（{'、'.join(late)} 已過預計公布時間 {LATE_MIN} 分鐘仍靠讀穿或缺，"
                           f"資料池在預計公布後 {LATE_MIN} 分鐘內沒有執行紀錄），最後執行於 "
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
        "read": {"exit": ex, "via_direct": via_direct, "missing": missing, "late": late, "late_filled": late_filled,
                 "notes": notes},
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
