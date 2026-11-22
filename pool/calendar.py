"""交易日曆與各群組的目標日。時區固定 UTC+8（台灣無日光節約）。"""
from __future__ import annotations
import datetime as dt
from .util import parse_date, iso

TZ = dt.timezone(dt.timedelta(hours=8))


def now_tw() -> dt.datetime:
    return dt.datetime.now(TZ)


def parse_now(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s)
    return t.replace(tzinfo=TZ) if t.tzinfo is None else t.astimezone(TZ)


def fetch_days(http, today: dt.date) -> list[dt.date]:
    rows = http.finmind("TaiwanStockTradingDate", None,
                        iso(today - dt.timedelta(days=60)), iso(today + dt.timedelta(days=90)))
    return sorted({parse_date(r["date"]) for r in rows})


def enough(days: list[dt.date], today: dt.date) -> bool:
    """日曆要涵蓋到今天之後至少 10 天，否則視為過期、需重抓。"""
    return bool(days) and days[-1] >= today + dt.timedelta(days=10) and days[0] <= today - dt.timedelta(days=20)


def _easter(y: int) -> dt.date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo = (h + l - 7 * m + 114) // 31
    return dt.date(y, mo, (h + l - 7 * m + 114) % 31 + 1)


def _nth(y, m, wd, n):
    d = dt.date(y, m, 1)
    d += dt.timedelta(days=(wd - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last(y, m, wd):
    d = (dt.date(y, m + 1, 1) if m < 12 else dt.date(y + 1, 1, 1)) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - wd) % 7)


def _observed(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=1) if d.weekday() == 5 else d + dt.timedelta(days=1) if d.weekday() == 6 else d


def us_holidays(y: int) -> set:
    """紐約證交所全日休市（規則推算）：元旦、金恩紀念日、總統日、耶穌受難日、陣亡將士紀念日、
    六月節、獨立紀念日、勞動節、感恩節、聖誕節。元旦逢週六不補假（NYSE 規則）。"""
    s = {_nth(y, 1, 0, 3), _nth(y, 2, 0, 3), _easter(y) - dt.timedelta(days=2), _last(y, 5, 0),
         _observed(dt.date(y, 6, 19)), _observed(dt.date(y, 7, 4)), _nth(y, 9, 0, 1), _nth(y, 11, 3, 4),
         _observed(dt.date(y, 12, 25))}
    nd = dt.date(y, 1, 1)
    if nd.weekday() != 5:
        s.add(_observed(nd))
    return s


def compute(now: dt.datetime, days: list[dt.date]) -> dict:
    today = now.date()
    done = [d for d in days if d < today or (d == today and now.time() >= dt.time(13, 45))]
    if len(done) < 2:
        raise ValueError("交易日曆不足")
    tw_last, tw_prev = done[-1], done[-2]
    nxt = [d for d in days if d > tw_last]
    tw_next = nxt[0] if nxt else None
    # 夜盤：S 為最近一個「夜盤已於隔日 05:00 結束」的交易日；歸屬日 N 為 S 之後的下一個交易日
    ended = [d for d in days if dt.datetime.combine(d + dt.timedelta(days=1), dt.time(5, 0), TZ) <= now]
    s = ended[-1]
    after = [d for d in days if d > s]
    night_trade = after[0] if after else None
    night_session = f"{s.month:02d}/{s.day:02d} 15:00～{(s + dt.timedelta(days=1)).month:02d}/{(s + dt.timedelta(days=1)).day:02d} 05:00"
    # 美股：X 日收盤在台灣 (X+1) 05:00 之後可得
    x = (now - dt.timedelta(hours=5)).date() - dt.timedelta(days=1)
    while x.weekday() >= 5 or x in us_holidays(x.year):
        x -= dt.timedelta(days=1)
    return {
        "tw_last": iso(tw_last), "tw_prev": iso(tw_prev),
        "tw_next": iso(tw_next) if tw_next else None,
        "us_last": iso(x),
        "_night_start": iso(s),
        "_night_trade": iso(night_trade) if night_trade else None,
        "_night_session": night_session,
    }


def due_at(group: str, hhmm: str, cal: dict) -> dt.datetime:
    """各群組某項目的預計公布時間點（台灣時間）。"""
    h, m = map(int, hhmm.split(":"))
    if group == "tw_close":
        base = parse_date(cal["tw_last"])
    elif group == "tw_night":   # 夜盤 05:10＝夜盤開始日的隔天
        base = parse_date(cal["_night_start"]) + dt.timedelta(days=1)
    else:                        # us_close：X+1 日台灣時間
        base = parse_date(cal["us_last"]) + dt.timedelta(days=1)
    return dt.datetime.combine(base, dt.time(h, m), TZ)
