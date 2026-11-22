"""小工具：數字解析、四捨五入、日期轉換。"""
from __future__ import annotations
import datetime as dt
import math
from decimal import Decimal, ROUND_HALF_UP


class NotYet(Exception):
    """來源正常，但目標日的資料尚未公布。"""


class SourceError(Exception):
    """來源失敗（連線、格式、檢核不過）。"""


def num(s):
    """'1,234.5'、' -12 '、'+3' → float；空白、'-'、'--' → None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace(",", "").replace("%", "").strip()
    if t in ("", "-", "--", "---", "N/A", "X"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def rnd(x, n=2):
    """四捨五入到 n 位（以十進位逢五進位，避免 2 進位浮點誤差）；None 原樣回傳。"""
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    q = Decimal(1).scaleb(-n)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


def iso(d) -> str:
    return d.isoformat() if isinstance(d, dt.date) else str(d)


def ymd(d) -> str:
    return iso(d).replace("-", "")


def parse_date(s) -> dt.date:
    s = str(s).strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return dt.date.fromisoformat(s)
