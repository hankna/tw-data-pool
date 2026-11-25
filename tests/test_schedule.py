"""時鐘時刻表、大額交易人 CSV 備援、報告端「未按時更新」警示與本機衍生值。"""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


clock = _load("clock", "tools/clock.py")
UTC = dt.timezone.utc
TW = dt.timezone(dt.timedelta(hours=8))


def test_crons_come_from_pool_yml():
    crons = clock.load_crons(clock.POOL_YML.read_text("utf-8"))
    assert len(crons) == 9 and "12 6 * * 1-5" in crons and "12 21 * * 1-5" in crons


def test_weekday_afternoon_slots_in_taiwan_time():
    crons = clock.load_crons(clock.POOL_YML.read_text("utf-8"))
    start = dt.datetime(2026, 11, 25, 12, 0, tzinfo=TW)          # 週三中午
    got = [t.astimezone(TW).strftime("%H:%M") for t, _ in clock.fires(crons, start, start + dt.timedelta(hours=10))]
    assert got == ["14:12", "15:22", "16:17", "17:32", "21:22", "21:52"]


def test_night_slots_follow_utc_weekday():
    crons = clock.load_crons(clock.POOL_YML.read_text("utf-8"))
    # 台灣週六 05:12＝UTC 週五 21:12，要觸發；台灣週日、週一清晨不觸發
    start = dt.datetime(2026, 11, 28, 0, 0, tzinfo=TW)
    got = [t.astimezone(TW).strftime("%m/%d %H:%M") for t, _ in clock.fires(crons, start, start + dt.timedelta(days=2, hours=12))]
    assert got == ["11/28 05:12", "11/28 06:02", "11/28 06:32"]


def test_unsupported_cron_rejected():
    with pytest.raises(ValueError):
        clock.load_crons('    - cron: "*/5 * * * *"\n')


def test_dry_run_hands_over(capsys):
    assert clock.main(["--dry-run", "--now", "2026-11-25T04:00:00+00:00"]) == 0
    out = capsys.readouterr().out
    assert "slot=catch-up" in out and out.strip().endswith("gh workflow run clock.yml")
    assert out.count("gh workflow run pool.yml") == 5


# ---- 大額交易人：OpenAPI 晚一天時改讀 CSV ----
class _Http:
    def __init__(self, open_date, csv_text):
        self.open_date, self.csv_text = open_date, csv_text

    def json(self, url, params=None):
        return [{"Date": self.open_date, "Contract": "TX", "SettlementMonth": "999912", "TypeOfTraders": t,
                 "Top5Buy": "1", "Top5Sell": "2", "Top10Buy": "3", "Top10Sell": "4", "OIOfMarket": "5"} for t in ("0", "1")]

    def bytes(self, url, params=None, post=None):
        assert "largeTraderFutDown" in url and post["queryStartDate"] == "2026/11/25"
        return self.csv_text.encode("big5")


CSV = ("日期,商品(契約),商品名稱(契約名稱),到期月份(週別),交易人類別,前五大交易人買方,前五大交易人賣方,前十大交易人買方,前十大交易人賣方,全市場未沖銷部位數\n"
       "2026/11/25,TX     ,臺股期貨,202612  ,0,97585,69106,107269,96581,137886\n"
       "2026/11/25,TX     ,臺股期貨,999999  ,0,97592,70972,108908,98606,141906\n"
       "2026/11/25,TX     ,臺股期貨,999999  ,1,97592,70972,106853,98606,141906\n")


def _ctx(http):
    from pool.sources import Ctx
    return Ctx(http=http, cal={"tw_last": "2026-11-25"}, now=dt.datetime(2026, 11, 25, 18, 0, tzinfo=TW))


def test_large_traders_falls_back_to_csv():
    from pool.sources import taifex
    v, d = taifex.large_traders(_ctx(_Http("20261124", CSV)))
    assert d == dt.date(2026, 11, 25)
    assert v["all"] == {"top5_buy": 97592, "top5_sell": 70972, "top10_buy": 108908, "top10_sell": 98606}
    assert v["specific"]["top10_buy"] == 106853 and v["market_oi"] == 141906


def test_large_traders_prefers_openapi_when_current():
    from pool.sources import taifex
    v, _ = taifex.large_traders(_ctx(_Http("20261125", "")))
    assert v["all"]["top5_buy"] == 1


def test_large_traders_not_yet_when_both_old():
    from pool.sources import taifex
    from pool.util import NotYet
    with pytest.raises(NotYet):
        taifex.large_traders(_ctx(_Http("20261124", CSV.replace("2026/11/25", "2026/11/24"))))


# ---- 報告端：資料池過時要警示，讀穿的加權要補進序列並算衍生值 ----
def _setup(tmp_path, monkeypatch):
    from pool import digest as dg
    from pool.util import iso

    data = tmp_path / "data"
    (data / "latest").mkdir(parents=True)
    (data / "series").mkdir()
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(70)]
    days = [d for d in days if d.weekday() < 5]
    (data / "series" / "tw_calendar.csv").write_text("date\n" + "\n".join(iso(d) for d in days) + "\n", "utf-8")
    hist = [d for d in days if d < dt.date(2026, 11, 25)]
    rows = ["date,open,high,low,close,turnover_bn"] + [f"{iso(d)},100,101,99,{100 + (i % 3)},1000" for i, d in enumerate(hist)]
    (data / "series" / "taiex_daily.csv").write_text("\n".join(rows) + "\n", "utf-8")
    (data / "latest" / "digest.json").write_text(json.dumps({
        "snapshot_id": "old00000", "generated_at": "2026-11-25T12:45:00+08:00", "items": {},
        "derived": None, "health": {"alert": False, "token": "none", "notes": []}}), "utf-8")

    def fake_resolve(name, cfg, ctx, policy):
        if name == "taiex":
            return {"status": "ok", "asof": "2026-11-25", "src": "TWSE:x",
                    "v": {"open": 101, "high": 102, "low": 95, "close": 96, "chg": -4, "chg_pct": -4.0, "turnover_bn": 1}}
        if name == "vix":
            return {"status": "ok", "asof": "2026-11-25", "src": "TAIFEX:file", "v": {"close": 50.0, "chg": 1}}
        return {"status": "missing", "asof": None}
    monkeypatch.setattr(dg, "resolve", fake_resolve)
    return data


def _read(tmp_path, data, now):
    pr = _load("pool_read", "tools/pool_read.py")
    out = tmp_path / "p.json"
    pr.main(["--profile", "panhou", "--date", "2026-11-25", "--now", now,
             "--data-dir", str(data), "--out", str(out)], http=object())
    return json.loads(out.read_text("utf-8"))


def test_reader_flags_late_pool_and_derives_locally(tmp_path, monkeypatch):
    r = _read(tmp_path, _setup(tmp_path, monkeypatch), "2026-11-25T18:23:00+08:00")
    assert r["health"]["alert"] is True and "taiex" in r["read"]["late"]
    assert r["health"]["notes"][0].startswith("資料池未按時更新")
    assert r["taiex_hist"][-1] == ["2026-11-25", 101, 102, 95, 96]
    assert r["derived_via"] == "direct" and r["derived"]["sigma1_vix_pts"] == round(96 * 50 / 100 / 252 ** 0.5, 1)
    assert r["derived"]["rv20_annual_pct"] is not None


def test_reader_no_alert_soon_after_due(tmp_path, monkeypatch):
    # 加權預計 14:10 公布，14:30 還要讀穿屬正常，不算排程失靈
    r = _read(tmp_path, _setup(tmp_path, monkeypatch), "2026-11-25T14:30:00+08:00")
    assert "taiex" not in r["read"]["late"]
