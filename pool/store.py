"""data 目錄讀寫：digest、series、run_log、保留規則。"""
from __future__ import annotations
import csv
import datetime as dt
import json
import os
from pathlib import Path
from .optstruct import SERIES_COLS as _OPT_COLS

SERIES = {
    "taiex_daily": ["date", "open", "high", "low", "close", "turnover_bn"],
    "inst_daily": ["date", "foreign_bn", "trust_bn", "dealer_bn", "total_bn"],
    "fut_oi": ["date", "foreign_net_oi", "trust_net_oi", "dealer_net_oi"],
    "opt_pc": ["date", "pc_oi_pct", "pc_vol_pct", "put_oi", "call_oi"],
    "vix_daily": ["date", "close", "close_1min_avg"],
    "margin_daily": ["date", "balance_bn", "short_balance"],
    "opt_struct": _OPT_COLS,
    "tw_calendar": ["date"],
    "availability": ["date", "item", "first_ok_at", "src"],
}
RUN_LOG = ["run_at", "trigger", "scheduled_for", "delay_min", "filled", "pending", "missing", "conflict", "seconds", "note"]
KEEP_DAILY, KEEP_RUNLOG, KEEP_AVAIL_DAYS = 5, 300, 60


class Store:
    def __init__(self, root):
        self.root = Path(root)

    # ---- digest ----
    def read_digest(self):
        p = self.root / "latest" / "digest.json"
        try:
            return json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def write_digest(self, digest: dict, data_date: str):
        text = json.dumps(digest, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
        for rel in ("latest/digest.json", f"daily/{data_date}/digest.json"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, "utf-8")
        days = sorted(d.name for d in (self.root / "daily").iterdir() if d.is_dir())
        for old in days[:-KEEP_DAILY]:
            for f in (self.root / "daily" / old).iterdir():
                f.unlink()
            (self.root / "daily" / old).rmdir()

    # ---- series ----
    def _path(self, name):
        return self.root / "series" / f"{name}.csv"

    def rows(self, name):
        try:
            with open(self._path(name), encoding="utf-8", newline="") as f:
                return list(csv.DictReader(f))
        except OSError:
            return []

    def upsert(self, name, new_rows):
        """依 date（availability 依 date+item）覆蓋同鍵、其餘保留；日期遞增排序。"""
        cols = SERIES[name]
        key = (lambda r: (r["date"], r.get("item"))) if name == "availability" else (lambda r: r["date"])
        cur = {key(r): r for r in self.rows(name)}
        for r in new_rows:
            r = {c: ("" if r.get(c) is None else r.get(c)) for c in cols}
            if name == "availability" and key(r) in cur:
                continue                      # 首次取得時間只記第一次
            cur[key(r)] = {c: str(r[c]) for c in cols}
        out = sorted(cur.values(), key=lambda r: (r["date"], r.get("item") or ""))
        if name == "availability":
            cutoff = (dt.date.today() - dt.timedelta(days=KEEP_AVAIL_DAYS)).isoformat()
            out = [r for r in out if r["date"] >= cutoff]
        p = self._path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            w.writerows(out)

    # ---- run_log ----
    def log_run(self, row: dict):
        p = self.root / "logs" / "run_log.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        old = []
        if p.exists():
            with open(p, encoding="utf-8", newline="") as f:
                r = csv.DictReader(f)
                if r.fieldnames == RUN_LOG:
                    old = list(r)
        old.append({c: str(row.get(c, "")) for c in RUN_LOG})
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=RUN_LOG, lineterminator="\n")
            w.writeheader()
            w.writerows(old[-KEEP_RUNLOG:])
