"""臺指選擇權逐履約價未平倉：到期日判斷、排除當日到期契約、最大未平倉。

到期規則（期交所）：月選＝到期月第三個週三；W 週選＝該月第 n 個週三；F 週選＝該月第 n 個週五。
遇休市順延到下一個交易日（以交易日曆為準）。"""
from __future__ import annotations
import calendar as _cal
import datetime as dt
import re

_RE = re.compile(r"^(\d{4})(\d{2})(?:([WF])(\d))?$")


def _nth_weekday(y: int, m: int, wd: int, n: int) -> dt.date:
    cnt = 0
    for day in range(1, _cal.monthrange(y, m)[1] + 1):
        d = dt.date(y, m, day)
        if d.weekday() == wd:
            cnt += 1
            if cnt == n:
                return d
    raise ValueError("無此週次")


def expiry(contract: str, days: list[dt.date] | None = None) -> dt.date | None:
    m = _RE.match(contract.strip())
    if not m:
        return None
    y, mo, kind, n = int(m[1]), int(m[2]), m[3], m[4]
    try:
        if kind is None:
            d = _nth_weekday(y, mo, 2, 3)
        elif kind == "W":
            d = _nth_weekday(y, mo, 2, int(n))
        else:
            d = _nth_weekday(y, mo, 4, int(n))
    except ValueError:
        return None
    if days:
        later = [x for x in days if x >= d]
        if later:
            d = later[0]
    return d


def calc(rows, d: dt.date, days=None) -> dict:
    """rows: (contract, 'call'|'put', strike, oi) 的可疊代物；d：資料日。"""
    by = {}
    for c, cp, k, oi in rows:
        c = str(c).strip()
        if "/" in c or oi is None:
            continue
        by.setdefault(c, []).append((cp, float(k), int(oi)))
    exp_of = {c: expiry(c, days) for c in by}
    expiring = sorted(c for c, e in exp_of.items() if e is not None and e <= d)
    live = {c: v for c, v in by.items() if c not in expiring and exp_of[c] is not None}
    sums = {"call": 0, "put": 0}
    for v in live.values():
        for cp, _, oi in v:
            sums[cp] += oi
    monthly = min((c for c in live if len(c) == 6), key=lambda c: exp_of[c], default=None)
    weekly = min((c for c in live if len(c) > 6), key=lambda c: exp_of[c], default=None)

    def top(c):
        out = {}
        for cp in ("call", "put"):
            xs = sorted(((k, oi) for p, k, oi in live[c] if p == cp and oi > 0), key=lambda t: (-t[1], t[0]))[:3]
            out[cp] = [[int(k) if k == int(k) else k, oi] for k, oi in xs]
        return out

    return {
        "expiring_today": expiring,
        "monthly": monthly,
        "weekly": weekly,
        "top": {c: top(c) for c in (monthly, weekly) if c},
        "oi_sum_excl_expiring": sums,
    }
