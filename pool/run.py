"""對帳式執行：只補「已到預計公布時間、但還不是 ok」的項目。

用法：python -m pool.run [--now ISO] [--date YYYY-MM-DD] [--items a,b] [--force] [--recheck]
                         [--data-dir DIR] [--trigger NAME] [--scheduled-for ISO]
"""
from __future__ import annotations
import argparse
import datetime as dt
import sys
import time
from pathlib import Path
from . import calendar as cal_mod, digest as dg
from .derive import derive
from .http import Http
from .sources import Ctx
from .store import Store
from .util import iso, parse_date

DONE = ("ok", "conflict")
TIMEOUT_H = 4   # 過預計公布時間 4 小時仍未公布，由 pending 改標 missing（會觸發警示）


def load_days(http, store: Store, today: dt.date, refresh: bool = False):
    rows = store.rows("tw_calendar")
    days = sorted(parse_date(r["date"]) for r in rows)
    if refresh or not cal_mod.enough(days, today):
        try:
            days = cal_mod.fetch_days(http, today)
            store.upsert("tw_calendar", [{"date": iso(d)} for d in days])
        except Exception:  # noqa: BLE001
            if not days:   # 完全沒有日曆：退而用「週一至週五」，國定假日會誤判，健康檢查會標註
                days = [today - dt.timedelta(days=60) + dt.timedelta(days=i) for i in range(160)]
                days = [d for d in days if d.weekday() < 5]
    return days


def update_series(store: Store, items: dict, side: dict, now_iso: str):
    if side.get("taiex_hist"):
        store.upsert("taiex_daily", [dict(zip(("date", "open", "high", "low", "close", "turnover_bn"), r)) for r in side["taiex_hist"]])
    g = lambda n: items.get(n, {}) if items.get(n, {}).get("status") in DONE else None
    if (e := g("taiex")):
        v = e["v"]
        store.upsert("taiex_daily", [{"date": e["asof"], "open": v["open"], "high": v["high"], "low": v["low"], "close": v["close"], "turnover_bn": v["turnover_bn"]}])
    if (e := g("inst_spot")):
        v = e["v"]
        store.upsert("inst_daily", [{"date": e["asof"], "foreign_bn": v["foreign_bn"], "trust_bn": v["trust_bn"], "dealer_bn": v["dealer_bn"], "total_bn": v["total_bn"]}])
    if (e := g("fut_inst")):
        v = e["v"]
        store.upsert("fut_oi", [{"date": e["asof"], "foreign_net_oi": v["foreign"]["net_oi"], "trust_net_oi": v["trust"]["net_oi"], "dealer_net_oi": v["dealer"]["net_oi"]}])
    if (e := g("pc_ratio")):
        v = e["v"]
        store.upsert("opt_pc", [{"date": e["asof"], "pc_oi_pct": v["oi_pct"], "pc_vol_pct": v["vol_pct"], "put_oi": v["put_oi"], "call_oi": v["call_oi"]}])
    if (e := g("vix")):
        v = e["v"]
        store.upsert("vix_daily", [{"date": e["asof"], "close": v["close"], "close_1min_avg": v["close_1min_avg"]}])
    if (e := g("margin")):
        v = e["v"]
        store.upsert("margin_daily", [{"date": e["asof"], "balance_bn": v["balance_bn"], "short_balance": v["short_balance"]}])
    avail = [{"date": e["asof"], "item": n, "first_ok_at": now_iso, "src": e.get("src", "")}
             for n, e in items.items() if e.get("status") in DONE and e.get("fresh")]
    if avail:
        store.upsert("availability", avail)


def run(http, store: Store, now: dt.datetime, *, only=None, force=False, recheck=False, trigger="manual",
        scheduled_for=None, catalog=None, date=None) -> dict:
    t0 = time.time()
    cat = catalog or dg.load_catalog()
    policy = cat.get("source_policy", "official_first")
    days = load_days(http, store, now.date())
    cal = cal_mod.compute(now, days)
    old = store.read_digest() or {}
    old_items = old.get("items", {})
    ctx = Ctx(http=http, cal=cal, now=now, days=days, series=store)
    items, notes = {}, []
    for name, cfg in cat["items"].items():
        tgt = dg._target(cfg["group"], cal)
        prev = old_items.get(name)
        asked = only is None or name in only
        due = cal_mod.due_at(cfg["group"], cfg["due"], cal)
        done = prev and prev.get("status") in DONE and prev.get("asof") == tgt
        again = force and asked or (recheck and cfg.get("key") and cfg["group"] == "tw_close" and asked)
        if done and not again:
            items[name] = prev
            ctx.items[name] = prev
            continue
        if now < due and not force:
            e = {"status": "pending", "asof": None}
        elif not asked:
            e = prev if prev else {"status": "pending", "asof": None}
        else:
            e = dg.resolve(name, cfg, ctx, policy)
            if e["status"] not in DONE and done:
                # 重抓（修訂偵測或強制）失敗時沿用原值，不讓暫時性的連線問題蓋掉已取得的資料
                notes.append(f"{name} 重抓失敗，沿用原值")
                e = prev
            elif e["status"] == "pending" and now > due + dt.timedelta(hours=TIMEOUT_H):
                e = {**e, "status": "missing", "notes": e.get("notes", []) + [f"逾預計時間 {TIMEOUT_H} 小時仍未公布"]}
            if e is prev:
                pass
            elif e["status"] in DONE:
                e["fresh"] = not done
                if done and prev.get("v") != e["v"]:
                    e["rev"] = prev.get("rev", 0) + 1
                    notes.append(f"{name} 修訂（rev {e['rev']}）")
                elif done:
                    e["rev"] = prev.get("rev", 0)
        if e["status"] not in DONE and prev and prev.get("status") in DONE and prev.get("asof") != tgt:
            e["prev"] = {k: prev[k] for k in ("asof", "v", "src") if k in prev}
        items[name] = e
        ctx.items[name] = e
    update_series(store, items, ctx.side, now.isoformat(timespec="seconds"))
    tc = items.get("taiex", {}).get("v", {}).get("close") if items.get("taiex", {}).get("status") in DONE else None
    vc = items.get("vix", {}).get("v", {}).get("close") if items.get("vix", {}).get("status") in DONE else None
    derived = derive(store.rows("taiex_daily"), tc, vc, cal["tw_last"])
    filled = sum(1 for e in items.values() if e.get("fresh"))
    for e in items.values():
        e.pop("fresh", None)
    bad = [n for n, e in items.items() if cat["items"][n].get("key") and e["status"] in ("missing", "conflict")]
    health = {"alert": bool(bad), "token": getattr(http, "token_state", "none"),
              "notes": notes + [f"{n}：{items[n]['status']}" for n in bad]}
    out = {"schema": cat["schema"], "generated_at": now.isoformat(timespec="seconds"),
           "snapshot_id": dg.snapshot_id(items, derived),
           "calendar": {k: v for k, v in cal.items() if not k.startswith("_")},
           "items": items, "derived": derived, "health": health}
    # 沒有任何變動就不覆寫，避免每次都產生無意義的提交
    if old and old.get("snapshot_id") == out["snapshot_id"] and old.get("health") == health:
        changed = False
    else:
        store.write_digest(out, cal["tw_last"])
        changed = True
    cnt = lambda s: sum(1 for e in items.values() if e["status"] == s)
    delay = ""
    if scheduled_for:
        delay = round((now - cal_mod.parse_now(scheduled_for)).total_seconds() / 60, 1)
    store.log_run({"run_at": now.isoformat(timespec="seconds"), "trigger": trigger, "scheduled_for": scheduled_for or "",
                   "delay_min": delay, "filled": filled,
                   "pending": cnt("pending"), "missing": cnt("missing"), "conflict": cnt("conflict"),
                   "seconds": round(time.time() - t0, 1), "note": ";".join(notes)[:120]})
    out["_changed"] = changed
    return out


def sched_from_cron(cron: str, now: dt.datetime) -> str:
    """cron（UTC，固定分與時）→ 最近一次應觸發的台灣時間。"""
    mi, hr = (int(x) for x in cron.split()[:2])
    u = now.astimezone(dt.timezone.utc)
    s = u.replace(hour=hr, minute=mi, second=0, microsecond=0)
    if s > u:
        s -= dt.timedelta(days=1)
    return s.astimezone(cal_mod.TZ).isoformat(timespec="seconds")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pool.run")
    ap.add_argument("--now")
    ap.add_argument("--date")
    ap.add_argument("--items")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--recheck", action="store_true")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--scheduled-for")
    ap.add_argument("--cron", help="排程觸發時帶入 cron 字串，用來計算排程延遲")
    a = ap.parse_args(argv)
    now = cal_mod.parse_now(a.now) if a.now else cal_mod.now_tw()
    if a.date:   # 指定日期：以該日 18:00 的視角重算
        now = dt.datetime.combine(parse_date(a.date), dt.time(18, 0), cal_mod.TZ)
    if a.cron and not a.scheduled_for:
        a.scheduled_for = sched_from_cron(a.cron, now)
    store = Store(a.data_dir)
    http = Http()
    out = run(http, store, now, only=set(a.items.split(",")) if a.items else None, force=a.force, recheck=a.recheck,
              trigger=a.trigger, scheduled_for=a.scheduled_for)
    s = {k: sum(1 for e in out["items"].values() if e["status"] == k) for k in ("ok", "conflict", "pending", "missing")}
    print(f"snapshot {out['snapshot_id']} {s} alert={out['health']['alert']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
