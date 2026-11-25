"""來源解析函式。每個函式 f(ctx) 回傳 (v, asof)，或丟出 NotYet／SourceError。"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field
from ..util import parse_date, iso


@dataclass
class Ctx:
    http: object
    cal: dict
    now: dt.datetime
    days: list = field(default_factory=list)
    series: object = None            # store.Store，提供 rows(name)
    items: dict = field(default_factory=dict)   # 已解出的項目（供 basis、pc 備援使用）
    side: dict = field(default_factory=dict)    # 副產品：要寫進 series 的資料
    _cache: dict = field(default_factory=dict)

    def d(self, key: str) -> dt.date:
        return parse_date(self.cal[key])

    def not_before(self, D: dt.date, hhmm: str, what: str):
        """目標日是今天、且台灣時間還沒到 hhmm，就丟 NotYet。一般排程有 due 擋著，但讀穿與強制重抓會直接呼叫
        來源函式；盤中可能拿到尚未收盤的數值（例如臺銀牌價一天掛牌多次），在這裡統一擋掉。"""
        from ..util import NotYet
        tw = dt.timezone(dt.timedelta(hours=8))
        now = self.now.astimezone(tw) if self.now.tzinfo else self.now
        h, m = map(int, hhmm.split(":"))
        if D == now.date() and now.time() < dt.time(h, m):
            raise NotYet(f"{what}：{iso(D)} 當日數值 {hhmm} 後才收錄")

    def cached(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]
