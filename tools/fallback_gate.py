"""備援排程檢查：GitHub 自己的排程（延遲 6～12 小時才到）只在「這個時段沒有被時鐘處理過」時才執行。

依觸發的 cron 字串（含星期欄位）算出它原本要涵蓋的時段，查 data 分支 logs/run_log.csv 是否已有
trigger=clock 且 scheduled_for 等於該時段的紀錄；有就略過（時鐘已準時處理，05:12 也已帶修訂重抓）。
只看同一個時段：別的時段成功不算。查不到紀錄、日誌讀不到或 cron 無法解析，一律照常執行。
另輸出 slot_at（該時段的精確時間），讓延遲到達的備援以正確的原時段計算延遲與修訂重抓目標日。

用法（GitHub Actions）：python3 code/tools/fallback_gate.py --cron "$SCHED" --data-dir data >> "$GITHUB_OUTPUT"
只用標準函式庫；說明寫到 stderr，stdout 只有 key=value。
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clock  # noqa: E402  （共用 cron 解析與觸發時刻計算，時刻表只有一份）

TW = dt.timezone(dt.timedelta(hours=8))


def slot_time(cron: str, now: dt.datetime) -> dt.datetime | None:
    """這個 cron 在 now 之前（含）最近一次應觸發的時刻；回推 8 天內找不到就回 None。"""
    clock.load_crons(f'    - cron: "{cron}"\n')       # 格式驗證，不合法丟 ValueError
    hits = clock.fires([cron], now - dt.timedelta(days=8), now)
    return hits[-1][0] if hits else None


def clock_done(log_path: Path, slot: dt.datetime) -> bool:
    with open(log_path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("trigger") != "clock" or not r.get("scheduled_for"):
                continue
            try:
                if dt.datetime.fromisoformat(r["scheduled_for"]) == slot:
                    return True
            except ValueError:
                continue
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fallback_gate")
    ap.add_argument("--cron", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--now", help="測試用（ISO）")
    a = ap.parse_args(argv)
    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    try:
        slot = slot_time(a.cron, now)
    except ValueError as e:
        print(f"cron 無法解析（{e}），照常執行", file=sys.stderr)
        print("skip=false")
        return 0
    if slot is None:
        print("找不到原時段，照常執行", file=sys.stderr)
        print("skip=false")
        return 0
    at = slot.astimezone(TW).isoformat(timespec="seconds")
    print(f"slot_at={at}")
    try:
        done = clock_done(Path(a.data_dir) / "logs" / "run_log.csv", slot)
    except OSError as e:
        print(f"讀不到 run_log（{type(e).__name__}），照常執行", file=sys.stderr)
        done = False
    print(f"時段 {at}：{'時鐘已處理，備援略過' if done else '時鐘沒有處理紀錄，照常執行'}", file=sys.stderr)
    print(f"skip={'true' if done else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
