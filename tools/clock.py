"""資料池時鐘：不靠 GitHub 排程，準時觸發 pool.yml。

為什麼要有：GitHub Actions 的 schedule 在本 repo 實測延遲 6～12 小時（logs/run_log.csv，
2026-11-23～25 共 18 次，delay_min 387～727），盤後 14:12～17:32 的四個時段要到隔天凌晨才跑，
報告只能全靠讀穿。workflow_dispatch 不經排程佇列，送出後一兩分鐘內就會開跑。

做法：clock.yml 的一個 job 最長跑約 5 小時 45 分。期間依 pool.yml 裡的 cron 時刻表
（唯一真相，不在這裡另寫一份）睡到每個時段，送出 workflow_dispatch（slot＝該 cron 字串、
slot_at＝該時段的精確時間）。
接棒：一啟動就先 dispatch 下一棒，讓它在 concurrency group clock 排隊；本棒不論正常結束、逾時或被取消，
下一棒都會立刻接上。結束時若佇列裡已沒有下一棒（例如被取消），再補送一次。
空窗回補：新一棒啟動時，把前 LOOKBACK_MIN 分鐘內的時段立刻補送（上一棒結束到這一棒啟動之間的時段），
重複送出無害（對帳式抓取）。clock.yml 自己的 cron 只當斷鏈後的備援。

用法：python3 tools/clock.py [--budget-min 345] [--dry-run] [--now ISO]
只用標準函式庫；觸發用 runner 內建的 gh（GH_TOKEN＝GITHUB_TOKEN）。
"""
from __future__ import annotations
import argparse
import datetime as dt
import re
import subprocess
import sys
import time
from pathlib import Path

UTC = dt.timezone.utc
TW = dt.timezone(dt.timedelta(hours=8))
POOL_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pool.yml"
CRON_RE = re.compile(r'^\s*-\s*cron:\s*"([^"]+)"', re.M)
CRON_ANY = re.compile(r'^\s*-\s*cron:', re.M)
LOOKBACK_MIN = 20     # 新一棒啟動時回補的時段範圍（上一棒交棒到本棒啟動通常只有 1～3 分鐘）
GH_TIMEOUT_S = 60     # 單次 gh 呼叫的上限，避免卡住拖垮整條鏈


def load_crons(text: str) -> list[str]:
    """取出 pool.yml 的 schedule cron 字串（只支援「固定分 固定時 * * 星期」這種寫法）。"""
    found = CRON_RE.findall(text)
    if len(found) != len(CRON_ANY.findall(text)):
        raise ValueError("pool.yml 有 cron 不是雙引號寫法，時鐘讀不到；請改成 cron: \"分 時 * * 星期\"")
    out = []
    for c in found:
        f = c.split()
        if (len(f) != 5 or not f[0].isdigit() or not f[1].isdigit() or f[2] != "*" or f[3] != "*"
                or not 0 <= int(f[0]) <= 59 or not 0 <= int(f[1]) <= 23):
            raise ValueError(f"不支援的 cron：{c}")
        _dows(f[4])   # 星期欄位不合法就在這裡丟錯
        out.append(c)
    return out


def _dows(field: str) -> set[int]:
    """cron 星期欄位 → Python weekday（週一＝0）。cron 的 0 與 7 都是週日。"""
    if field == "*":
        return set(range(7))
    s = set()
    for part in field.split(","):
        a, _, b = part.partition("-")
        if not a.isdigit() or (b and not b.isdigit()):
            raise ValueError(f"不支援的星期欄位：{field}")
        lo, hi = int(a), int(b or a)
        if not (0 <= lo <= hi <= 7):
            raise ValueError(f"不支援的星期欄位：{field}")
        for x in range(lo, hi + 1):
            s.add((x - 1) % 7)
    return s


def fires(crons: list[str], start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, str]]:
    """列出 (start, end] 之間每個 cron 的觸發時刻（UTC），依時間排序。"""
    out = []
    d0 = start.astimezone(UTC).date() - dt.timedelta(days=1)
    for c in crons:
        mi, hr, _, _, dw = c.split()
        ok = _dows(dw)
        d = d0
        while True:
            t = dt.datetime.combine(d, dt.time(int(hr), int(mi)), UTC)
            if t > end:
                break
            if t > start and d.weekday() in ok:
                out.append((t, c))
            d += dt.timedelta(days=1)
    return sorted(out)


def dispatch(workflow: str, fields: dict, dry: bool) -> bool:
    cmd = ["gh", "workflow", "run", workflow]
    for k, v in fields.items():
        cmd += ["-f", f"{k}={v}"]
    print(" ".join(cmd), flush=True)
    if dry:
        return True
    for i in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S)
            if r.returncode == 0:
                return True
            err = r.stderr.strip()[:200]
        except subprocess.TimeoutExpired:
            err = f"逾時 {GH_TIMEOUT_S} 秒"
        print(f"觸發失敗（第 {i + 1} 次）：{err}", flush=True)
        time.sleep(20)
    return False


def successor_queued() -> bool:
    """concurrency group clock 裡是否已有排隊中的下一棒。查不到就當沒有（寧可多送一次，重複的排隊會互相取代）。"""
    try:
        r = subprocess.run(["gh", "run", "list", "--workflow", "clock.yml", "--limit", "10",
                            "--json", "status", "--jq", ".[].status"],
                           capture_output=True, text=True, timeout=GH_TIMEOUT_S)
        return r.returncode == 0 and any(x in ("pending", "queued", "waiting", "requested")
                                         for x in r.stdout.split())
    except Exception:  # noqa: BLE001
        return False


def _slot(t: dt.datetime, c: str) -> dict:
    return {"slot": c, "slot_at": t.astimezone(TW).isoformat(timespec="seconds")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="clock")
    ap.add_argument("--budget-min", type=float, default=345)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="測試用：起始時間（ISO）")
    a = ap.parse_args(argv)
    crons = load_crons(POOL_YML.read_text("utf-8"))
    start = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(UTC)
    clock0 = time.monotonic()
    now = lambda: start + dt.timedelta(seconds=time.monotonic() - clock0)   # noqa: E731
    stop = start + dt.timedelta(minutes=a.budget_min)
    plan = fires(crons, start, stop)
    back = fires(crons, start - dt.timedelta(minutes=LOOKBACK_MIN), start)
    print(f"時鐘啟動 {start.astimezone(TW):%F %H:%M}（台灣），預計觸發 {len(plan)} 次，"
          f"{stop.astimezone(TW):%F %H:%M} 交棒", flush=True)
    # 先排好下一棒：本棒不論怎麼結束，下一棒都會在 concurrency 佇列裡接上
    queued = dispatch("clock.yml", {}, a.dry_run)
    # 啟動補跑：空窗內的時段帶原時段補送（含 05:12 的修訂重抓）；沒有就送一次一般補跑。對帳式抓取，重複無害
    failed = 0
    for t, c in back:
        if not dispatch("pool.yml", _slot(t, c), a.dry_run):
            failed += 1
            print(f"::error::回補時段 {t.astimezone(TW):%F %H:%M} 送出失敗，請在 6 小時內人工補送："
                  f"gh workflow run pool.yml -f slot='{c}' -f slot_at={_slot(t, c)['slot_at']}", flush=True)
    if not back and not dispatch("pool.yml", {"slot": "catch-up"}, a.dry_run):
        failed += 1
    for t, c in plan:
        wait = (t - now()).total_seconds()
        if wait > 0 and not a.dry_run:
            time.sleep(wait)
        if not dispatch("pool.yml", _slot(t, c), a.dry_run):
            failed += 1
            print(f"::error::時段 {t.astimezone(TW):%F %H:%M} 送出失敗，請在 6 小時內人工補送："
                  f"gh workflow run pool.yml -f slot='{c}' -f slot_at={_slot(t, c)['slot_at']}", flush=True)
    if not a.dry_run:
        rest = (stop - now()).total_seconds()
        if rest > 0:
            time.sleep(rest)
        if not successor_queued():
            queued = dispatch("clock.yml", {}, a.dry_run)
    if not queued:
        print("交棒失敗：時鐘鏈中斷，等 clock.yml 的備援 cron 重新啟動", flush=True)
    return 1 if (failed or not queued) else 0

if __name__ == "__main__":
    sys.exit(main())
