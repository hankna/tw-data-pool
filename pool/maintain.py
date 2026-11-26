"""每週維護：更新交易日曆、產生 health.json（公布時間與排程延遲分布、建議調整）、年初封存 series。
用法：python -m pool.maintain --data-dir data"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import statistics
from . import calendar as cal_mod, digest as dg
from .http import Http
from .run import load_days
from .store import Store, SERIES
from .util import parse_date


def _p90(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.9))] if xs else None


def health(store: Store, cat: dict) -> dict:
    by = {}
    for r in store.rows("availability"):
        t = dt.datetime.fromisoformat(r["first_ok_at"])
        by.setdefault(r["item"], []).append(t.hour * 60 + t.minute)
    items, advice = {}, []
    for n, mins in sorted(by.items()):
        p = _p90(mins)
        due = cat["items"].get(n, {}).get("due")
        items[n] = {"n": len(mins), "p90_hhmm": f"{p // 60:02d}:{p % 60:02d}", "due": due}
        if due and len(mins) >= 5:
            dh, dm = map(int, due.split(":"))
            if p > dh * 60 + dm + 45:
                advice.append(f"{n}：90% 的日子到 {p // 60:02d}:{p % 60:02d} 才取得，預計時間 {due} 偏早，建議延後")
    delays = []
    try:
        with open(store.root / "logs" / "run_log.csv", encoding="utf-8") as f:
            delays = [float(r["delay_min"]) for r in csv.DictReader(f) if r.get("delay_min")]
    except OSError:
        pass
    return {"generated_at": cal_mod.now_tw().isoformat(timespec="seconds"), "items": items,
            "schedule_delay_min": {"n": len(delays), "median": statistics.median(delays) if delays else None, "p90": _p90(delays)},
            "advice": advice}


def archive_old_years(store: Store, today: dt.date):
    """只追加的 series：每年 1 月把前一年（含）以前的列搬到 series/archive/YYYY/。"""
    if today.month != 1:
        return
    for name in ("taiex_daily", "inst_daily", "fut_oi", "opt_pc", "vix_daily", "margin_daily", "opt_struct"):
        rows = store.rows(name)
        old = [r for r in rows if parse_date(r["date"]).year < today.year - 1]
        if not old:
            continue
        for y in sorted({parse_date(r["date"]).year for r in old}):
            p = store.root / "series" / "archive" / str(y) / f"{name}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=SERIES[name], lineterminator="\n")
                w.writeheader()
                w.writerows([r for r in old if parse_date(r["date"]).year == y])
        keep = [r for r in rows if parse_date(r["date"]).year >= today.year - 1]   # 留前一年與今年，rv20 等才不斷
        with open(store._path(name), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SERIES[name], lineterminator="\n")
            w.writeheader()
            w.writerows(keep)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    a = ap.parse_args(argv)
    store, cat = Store(a.data_dir), dg.load_catalog()
    now = cal_mod.now_tw()
    load_days(Http(), store, now.date(), refresh=True)
    h = health(store, cat)
    p = store.root / "logs" / "health.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(h, ensure_ascii=False, indent=1) + "\n", "utf-8")
    archive_old_years(store, now.date())
    print("維護完成；建議調整：" + ("；".join(h["advice"]) or "無"))


if __name__ == "__main__":
    main()
