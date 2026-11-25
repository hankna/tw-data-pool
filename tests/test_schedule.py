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


@pytest.mark.parametrize("bad", [
    '    - cron: "*/5 * * * *"\n',
    "    - cron: '12 6 * * 1-5'\n",            # 單引號：時鐘讀不到，必須明確失敗而不是默默漏掉
    '    - cron: "12 6 * * 8"\n',              # 星期 8 不合法
    '    - cron: "12 6 * * 5-1"\n',            # 倒序範圍
    '    - cron: "61 6 * * 1-5"\n',
])
def test_unsupported_cron_rejected(bad):
    with pytest.raises(ValueError):
        clock.load_crons(bad)


def _cmds(out):
    return [ln for ln in out.splitlines() if ln.startswith("gh workflow run")]


def test_dry_run_queues_successor_first(capsys):
    assert clock.main(["--dry-run", "--now", "2026-11-25T04:00:00+00:00"]) == 0
    cmds = _cmds(capsys.readouterr().out)
    # 先排下一棒（本棒怎麼結束都有人接），再補跑，再依時段觸發，時段帶精確時間
    assert cmds[0] == "gh workflow run clock.yml"
    assert cmds[1] == "gh workflow run pool.yml -f slot=catch-up"
    assert cmds[2:] == [f"gh workflow run pool.yml -f slot={c} -f slot_at=2026-11-25T{t}:00+08:00"
                        for c, t in [("12 6 * * 1-5", "14:12"), ("22 7 * * 1-5", "15:22"),
                                     ("17 8 * * 1-5", "16:17"), ("32 9 * * 1-5", "17:32")]]


def _recheck_run(tmp_path, monkeypatch, now, ok=False):
    import pool.run as pr
    from pool.store import Store
    if ok:   # 每個項目都拿到目標日的有效值（只驗重抓標記，序列寫入與衍生值換成空操作）
        monkeypatch.setattr(pr.dg, "resolve", lambda name, cfg, ctx, policy: {
            "status": "ok", "asof": pr.dg._target(cfg["group"], ctx.cal), "src": "x", "v": {"close": 1}})
        monkeypatch.setattr(pr, "update_series", lambda *a, **k: None)
        monkeypatch.setattr(pr, "derive", lambda *a, **k: {})
    else:
        monkeypatch.setattr(pr.dg, "resolve", lambda name, cfg, ctx, policy: {"status": "pending", "asof": None})
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(120)]
    days = [d for d in days if d.weekday() < 5]
    monkeypatch.setattr(pr, "load_days", lambda http, store, today, refresh=False: days)
    return pr.run(object(), Store(tmp_path), now, recheck=True, trigger="schedule",
                  scheduled_for="2026-11-25T05:12:00+08:00")


def test_recheck_skipped_when_target_day_changed(tmp_path, monkeypatch):
    # 05:12 的修訂重抓拖到 14:30（已過 11/25 收盤）才跑：目標日已從 11/24 換成 11/25，略過並留紀錄
    out = _recheck_run(tmp_path, monkeypatch, dt.datetime(2026, 11, 25, 14, 30, tzinfo=TW))
    assert any(n.startswith("修訂重抓略過") for n in out["health"]["notes"])
    assert isinstance(out["health"]["bad"], list) and out["health"]["clock_late"] is False


def test_recheck_kept_when_late_but_same_target_day(tmp_path, monkeypatch):
    # 晚了 7 小時但還沒收盤，目標日仍是 11/24：照常重抓
    out = _recheck_run(tmp_path, monkeypatch, dt.datetime(2026, 11, 25, 12, 10, tzinfo=TW))
    assert not any(n.startswith("修訂重抓略過") for n in out["health"]["notes"])


def _http_rejecting(resp):
    from pool.http import Http
    h = Http(token="SECRET-123")
    calls = []

    def fake_json(url, params=None):
        calls.append(dict(params))
        if "token" in params:
            return resp()
        return {"status": 200, "data": [{"x": 1}]}
    h.json = fake_json
    assert h.finmind("TaiwanExchangeRate", "USD", "2026-11-20", "2026-11-25") == [{"x": 1}]
    assert h.token_state == "invalid" and "token" not in calls[-1]
    return h


def test_finmind_token_rejection_records_http_code_only():
    from pool.http import HttpStatus

    def deny():
        raise HttpStatus(401, "api.finmindtrade.com")
    h = _http_rejecting(deny)
    assert h.token_reason == "HTTP 401（TaiwanExchangeRate）"


def test_finmind_token_rejection_never_records_message_text():
    # FinMind 訊息若回顯金鑰（含截斷或編碼後），一個字都不能進公開資料：只記狀態碼
    h = _http_rejecting(lambda: {"status": 402, "msg": "Your token SECRET-123 is invalid, token=U0VDUkVULTEyMw=="})
    assert h.token_reason == "FinMind status 402（TaiwanExchangeRate）"
    assert "SECRET" not in h.token_reason and "U0VD" not in h.token_reason


# ---- 備援檢查：只有同一時段已由時鐘處理過才略過 ----
gate = _load("fallback_gate", "tools/fallback_gate.py")


def _gate(tmp_path, cron, now, *rows, recheck=False):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "run_log.csv").write_text(
        "run_at,trigger,scheduled_for,delay_min,note\n" + "".join(r + "\n" for r in rows), "utf-8")
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert gate.main(["--cron", cron, "--data-dir", str(tmp_path), "--now", now] + (["--recheck"] if recheck else [])) == 0
    return dict(ln.split("=", 1) for ln in buf.getvalue().split())


def test_gate_skips_only_when_same_slot_done(tmp_path):
    out = _gate(tmp_path, "12 21 * * 1-5", "2026-11-26T12:00:00+08:00",
                "2026-11-26T05:12:40+08:00,clock,2026-11-26T05:12:00+08:00,0.7")
    assert out == {"slot_at": "2026-11-26T05:12:00+08:00", "skip": "true"}


def test_gate_runs_when_that_slot_failed_even_if_later_slot_ok(tmp_path):
    # 05:12 送出失敗、06:02 成功：05:12 的備援到達時仍要照常執行（帶修訂重抓）
    out = _gate(tmp_path, "12 21 * * 1-5", "2026-11-26T12:00:00+08:00",
                "2026-11-26T06:02:40+08:00,clock,2026-11-26T06:02:00+08:00,0.7",
                "2026-11-26T09:00:00+08:00,workflow_dispatch,,")
    assert out == {"slot_at": "2026-11-26T05:12:00+08:00", "skip": "false"}


def test_gate_runs_without_log_and_respects_weekday(tmp_path):
    # 週一 12:00 收到 05:12 的備援：UTC 週日 21:12 不在 1-5，原時段是上週六 05:12（UTC 週五）
    import shutil
    shutil.rmtree(tmp_path / "logs", ignore_errors=True)
    out = _gate(tmp_path, "12 21 * * 1-5", "2026-11-30T12:00:00+08:00")
    assert out == {"slot_at": "2026-11-28T05:12:00+08:00", "skip": "false"}


def test_gate_recheck_slot_needs_successful_recheck(tmp_path):
    slot = "2026-11-26T05:12:40+08:00,clock,2026-11-26T05:12:00+08:00,0.7,"
    run = lambda note: _gate(tmp_path, "12 21 * * 1-5", "2026-11-26T12:00:00+08:00", slot + note, recheck=True)  # noqa: E731
    assert run("recheck=ok;")["skip"] == "true"
    assert run("recheck=skipped;")["skip"] == "true"
    # 05:12 來源暫時故障、沿用原值：12:00 來源已恢復，備援要再查一次修訂
    assert run("recheck=failed:taiex,txo_oi;taiex 重抓失敗，沿用原值")["skip"] == "false"
    assert run("")["skip"] == "false"                       # 舊紀錄沒有標記：照常執行


def test_gate_bad_log_encoding_runs_normally(tmp_path):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "run_log.csv").write_bytes(b"run_at,trigger,scheduled_for\n\xff\xfe\x00bad\n")
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert gate.main(["--cron", "12 6 * * 1-5", "--data-dir", str(tmp_path), "--now", "2026-11-26T18:00:00+08:00"]) == 0
    assert dict(ln.split("=", 1) for ln in buf.getvalue().split()) == {"slot_at": "2026-11-26T14:12:00+08:00", "skip": "false"}


def test_recheck_result_is_tagged_in_run_log(tmp_path, monkeypatch):
    import csv as _csv
    from pool import run as pr
    assert pr._recheck_tag(True, False, ["taiex"]) == "recheck=failed:taiex;"
    assert pr._recheck_tag(True, False, []) == "recheck=ok;" and pr._recheck_tag(False, False, []) == ""
    _recheck_run(tmp_path / "a", monkeypatch, dt.datetime(2026, 11, 25, 14, 30, tzinfo=TW))
    _recheck_run(tmp_path / "b", monkeypatch, dt.datetime(2026, 11, 25, 12, 10, tzinfo=TW))
    _recheck_run(tmp_path / "c", monkeypatch, dt.datetime(2026, 11, 25, 12, 10, tzinfo=TW), ok=True)
    note = lambda d: list(_csv.DictReader(open(d / "logs" / "run_log.csv", encoding="utf-8")))[-1]["note"]  # noqa: E731
    assert note(tmp_path / "a").startswith("recheck=skipped;")
    # 前一交易日的關鍵項目本來就沒抓到、這次仍 pending：不能算重抓成功，備援要再補
    assert note(tmp_path / "b").startswith("recheck=failed:taiex,")
    assert note(tmp_path / "c").startswith("recheck=ok;")


def test_finmind_error_with_token_never_includes_message():
    from pool.http import Http
    from pool.util import SourceError
    h = Http(token="SECRET-123")
    h.json = lambda url, params=None: {"status": 400, "msg": "bad request for SECRET-123"}
    with pytest.raises(SourceError) as ei:
        h.finmind("TaiwanExchangeRate", "USD", "2026-11-20", "2026-11-25")
    assert "SECRET" not in str(ei.value) and "status 400" in str(ei.value)


def test_gate_unparseable_cron_runs_normally(tmp_path):
    assert _gate(tmp_path, "*/5 * * * *", "2026-11-26T12:00:00+08:00") == {"skip": "false"}


def test_new_clock_replays_slot_missed_during_handover(capsys):
    # 上一棒 14:11 結束、這一棒 14:14 才啟動：14:12 要用原時段補送，不是只送 catch-up
    assert clock.main(["--dry-run", "--now", "2026-11-25T06:14:00+00:00", "--budget-min", "30"]) == 0
    cmds = _cmds(capsys.readouterr().out)
    assert cmds[1] == "gh workflow run pool.yml -f slot=12 6 * * 1-5 -f slot_at=2026-11-25T14:12:00+08:00"
    assert not any("catch-up" in c for c in cmds)


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


def test_large_traders_csv_fallback_reports_actual_source():
    from pool import digest as dg
    from pool.sources import Ctx
    ctx = Ctx(http=_Http("20261124", CSV), cal={"tw_last": "2026-11-25"}, now=dt.datetime(2026, 11, 25, 18, 0, tzinfo=TW))
    cat = dg.load_catalog()
    e = dg.resolve("large_traders", cat["items"]["large_traders"], ctx, cat.get("source_policy", "official_first"))
    assert e["status"] == "ok" and "largeTraderFutDown" in e["src"] and "_src" not in e["v"]


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
    data = _setup(tmp_path, monkeypatch)
    _log(data, "2026-11-25T12:40:00+08:00,schedule,2026-11-25T06:32:00+08:00,368.0")   # 盤後時段一次都沒跑
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert r["health"]["alert"] is True and "taiex" in r["read"]["late"]
    assert r["health"]["notes"][0].startswith("資料池未按時更新")
    assert r["taiex_hist"][-1] == ["2026-11-25", 101, 102, 95, 96]
    assert r["derived_via"] == "direct" and r["derived"]["sigma1_vix_pts"] == round(96 * 50 / 100 / 252 ** 0.5, 1)
    assert r["derived"]["rv20_annual_pct"] is not None


def test_reader_no_alert_when_pool_ran_but_exchange_late(tmp_path, monkeypatch):
    # 盤後四個時段都準時跑過（加權一直未公布、digest 沒變所以沒重寫），18:23 讀穿成功：屬交易所晚公布，不算排程失靈
    data = _setup(tmp_path, monkeypatch)
    _log(data, *[f"2026-11-25T{t}:40+08:00,clock,2026-11-25T{t}:00+08:00,0.7" for t in ("14:12", "15:22", "16:17", "17:32")])
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert r["read"]["late"] == [] and r["health"]["alert"] is False
    assert r["derived_via"] == "direct"


def _log(data, *rows):
    (data / "logs").mkdir(exist_ok=True)
    (data / "logs" / "run_log.csv").write_text("run_at,trigger,scheduled_for,delay_min\n" + "".join(r + "\n" for r in rows), "utf-8")


def test_reader_manual_run_does_not_mask_missed_slots(tmp_path, monkeypatch):
    # 盤後四個時鐘時段都沒跑，18:20 有人手動只補 vix：不能把它當成 taiex 已有抓取機會
    data = _setup(tmp_path, monkeypatch)
    _log(data, "2026-11-25T12:40:00+08:00,schedule,,", "2026-11-25T18:20:00+08:00,workflow_dispatch,,")
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert "taiex" in r["read"]["late"] and r["health"]["alert"] is True


def test_reader_late_fallback_does_not_mask_missed_slots(tmp_path, monkeypatch):
    # 盤後時鐘全漏跑，06:32 的備援 cron 拖到 18:20 才執行：已過期限，不算準時抓過
    data = _setup(tmp_path, monkeypatch)
    _log(data, "2026-11-25T18:20:00+08:00,schedule,2026-11-25T06:32:00+08:00,708.0")
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert "taiex" in r["read"]["late"] and r["health"]["alert"] is True


def test_reader_drops_stale_clock_alert_from_digest(tmp_path, monkeypatch):
    # digest 最後一次寫入只帶 05:12 的時鐘延遲警示；晚間報告超出 6 小時窗，不應再發簡訊
    data = _setup(tmp_path, monkeypatch)
    d = json.loads((data / "latest" / "digest.json").read_text("utf-8"))
    d["health"] = {"alert": True, "bad": [], "clock_late": True, "token": "none",
                   "notes": ["時鐘觸發延遲 68.0 分（應於 05:12 執行）"]}
    (data / "latest" / "digest.json").write_text(json.dumps(d), "utf-8")
    _log(data, "2026-11-25T06:20:00+08:00,clock,2026-11-25T05:12:00+08:00,68.0",
         *[f"2026-11-25T{t}:40+08:00,clock,2026-11-25T{t}:00+08:00,0.7" for t in ("14:12", "15:22", "16:17", "17:32")])
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert r["health"]["alert"] is False and r["health"]["notes"] == []


def test_reader_recomputes_vix_sigma_when_vix_read_through(tmp_path, monkeypatch):
    # 加權來自資料池、VIX 讀穿：資料池的 VIX 1σ 算在 VIX 公布前（null），報告要用當日 VIX 重算
    data = _setup(tmp_path, monkeypatch)
    d = json.loads((data / "latest" / "digest.json").read_text("utf-8"))
    d["items"] = {"taiex": {"status": "ok", "asof": "2026-11-25", "src": "TWSE:x",
                            "v": {"open": 1, "high": 1, "low": 1, "close": 96, "chg": 0, "chg_pct": 0, "turnover_bn": 1}}}
    d["derived"] = {"rv20_annual_pct": 9.9, "sigma1_rv_pts": 1, "sigma1_vix_pts": None, "closes_7": [["2026-11-25", 96.0]]}
    (data / "latest" / "digest.json").write_text(json.dumps(d), "utf-8")
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert r["items"]["vix"]["via"] == "direct" and r["derived_via"] == "pool"
    assert r["derived"]["rv20_annual_pct"] == 9.9
    assert r["derived"]["sigma1_vix_pts"] == round(96 * 50 / 100 / 252 ** 0.5, 1)


def _pool_taiex(data):
    d = json.loads((data / "latest" / "digest.json").read_text("utf-8"))
    d["items"] = {"taiex": {"status": "ok", "asof": "2026-11-25", "src": "TWSE:x",
                            "v": {"open": 1, "high": 1, "low": 1, "close": 96, "chg": 0, "chg_pct": 0, "turnover_bn": 1}}}
    d["generated_at"] = "2026-11-25T14:20:00+08:00"
    (data / "latest" / "digest.json").write_text(json.dumps(d), "utf-8")


def test_reader_flags_slots_missed_even_if_filled_late(tmp_path, monkeypatch):
    # 盤後時鐘全漏跑，18:20 晚到的備援補齊加權：資料雖來自資料池，排程仍失靈，要發警示並註明已補齊
    data = _setup(tmp_path, monkeypatch)
    _pool_taiex(data)
    _log(data, "2026-11-25T18:20:00+08:00,schedule,2026-11-25T06:32:00+08:00,708.0")
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert r["items"]["taiex"]["via"] == "pool" and r["read"]["late_filled"] == ["taiex"]
    assert r["health"]["alert"] is True and any(n.startswith("資料池排程失靈但已補齊") for n in r["health"]["notes"])


def test_reader_manual_only_log_is_not_on_time_evidence(tmp_path, monkeypatch):
    # 日誌只有 14:20 手動補抓（digest 也在 14:20 寫入）：不能拿 generated_at 當成加權準時抓過
    data = _setup(tmp_path, monkeypatch)
    _pool_taiex(data)
    (data / "latest" / "digest.json").write_text(
        (data / "latest" / "digest.json").read_text("utf-8").replace('"items": {"taiex"', '"items": {"x"'), "utf-8")
    _log(data, "2026-11-25T14:20:00+08:00,workflow_dispatch,,")
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert "taiex" in r["read"]["late"]


def test_reader_skips_schedule_check_without_log(tmp_path, monkeypatch):
    r = _read(tmp_path, _setup(tmp_path, monkeypatch), "2026-11-25T18:23:00+08:00")
    assert r["read"]["late"] == [] and r["read"]["late_filled"] == []
    assert any("讀不到資料池執行紀錄" in n for n in r["read"]["notes"])


def test_reader_flags_recent_late_clock_run_only(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch)
    _log(data, "2026-11-25T06:20:00+08:00,clock,2026-11-25T05:12:00+08:00,68.0",
         "2026-11-25T17:33:00+08:00,clock,2026-11-25T16:17:00+08:00,76.0")
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert r["health"]["alert"] is True
    notes = [n for n in r["health"]["notes"] if n.startswith("時鐘觸發延遲")]
    assert notes == ["時鐘觸發延遲 76.0 分（應於 11-25 16:17 執行）"]   # 05:12 那次已超過 6 小時，不再重複警示


def test_reader_direct_close_replaces_stale_same_day_bar(tmp_path, monkeypatch):
    # 序列裡已有當日舊 K 棒（收盤 100），讀穿取得修訂值 96：序列與衍生值都要用 96
    data = _setup(tmp_path, monkeypatch)
    p = data / "series" / "taiex_daily.csv"
    p.write_text(p.read_text("utf-8") + "2026-11-25,100,101,99,100,1000\n", "utf-8")
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert r["taiex_hist"][-1] == ["2026-11-25", 101, 102, 95, 96]
    assert [h[0] for h in r["taiex_hist"]].count("2026-11-25") == 1
    assert r["derived"]["closes_7"][-1] == ["2026-11-25", 96.0]


def test_reader_ignores_pool_derived_from_other_day(tmp_path, monkeypatch):
    from pool import digest as dg
    data = _setup(tmp_path, monkeypatch)
    d = json.loads((data / "latest" / "digest.json").read_text("utf-8"))
    d["items"] = {"taiex": {"status": "ok", "asof": "2026-11-25", "src": "TWSE:x",
                            "v": {"open": 1, "high": 1, "low": 1, "close": 96, "chg": 0, "chg_pct": 0, "turnover_bn": 1}}}
    d["derived"] = {"rv20_annual_pct": 9.9, "sigma1_rv_pts": 1, "sigma1_vix_pts": 1, "closes_7": [["2026-11-24", 100.0]]}
    (data / "latest" / "digest.json").write_text(json.dumps(d), "utf-8")
    r = _read(tmp_path, data, "2026-11-25T18:23:00+08:00")
    assert r["items"]["taiex"]["via"] == "pool"
    assert r["derived_via"] == "direct" and r["derived"]["rv20_annual_pct"] != 9.9


def test_reader_no_alert_soon_after_due(tmp_path, monkeypatch):
    # 加權預計 14:10 公布，14:30 還要讀穿屬正常，不算排程失靈
    r = _read(tmp_path, _setup(tmp_path, monkeypatch), "2026-11-25T14:30:00+08:00")
    assert "taiex" not in r["read"]["late"]


# ---- 新台幣匯率：臺灣銀行官方優先、FinMind 備援 ----
def test_usd_twd_prefers_bank_of_taiwan():
    from tests.test_golden import build, GOLD
    from tests.compare import diff
    out, _ = build()
    e = out["items"]["usd_twd"]
    assert e["status"] == "ok" and e["src"].startswith("BOT:") and e["asof"] == "2026-11-20"
    assert diff(e["v"], GOLD["items"]["usd_twd"]["v"]) == []


def test_usd_twd_falls_back_to_finmind():
    from tests.test_golden import build, GOLD
    from tests.compare import diff
    out, _ = build(off={"rate.bot.com.tw"})
    e = out["items"]["usd_twd"]
    assert e["status"] == "ok" and e["src"].startswith("FinMind:")
    assert diff(e["v"], GOLD["items"]["usd_twd"]["v"]) == []


HDR = "幣別,匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天,匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天"
# 測試用合成資料（多幣別、遠期欄非零），用來確認欄位索引；不是實際牌價
MULTI = (HDR + "\n"
         "USD,本行買入,32.72500,33.05000,33.05900,32.99700,32.91700,32.83300,32.74200,32.65700,32.57400,"
         "本行賣出,33.39500,33.20000,33.16300,33.10900,33.03400,32.95900,32.87700,32.80000,32.72400,\n"
         "HKD,本行買入,4.07100,4.19200,4.19400,4.18900,4.18300,4.17700,4.17100,4.16500,4.15800,"
         "本行賣出,4.27500,4.26200,4.25500,4.25300,4.24700,4.24100,4.23700,4.23200,4.22700,\n")


class _BotHttp:
    def __init__(self, text, fname="ExchangeRate@202611201600.csv"):
        self.text, self.last_headers = text, ({"content-disposition": f"attachment; filename={fname}"} if fname else {})

    def bytes(self, url, params=None, post=None):
        assert url.endswith("/2026-11-20")
        return self.text.encode("utf-8-sig")


def _bot(text, now=dt.datetime(2026, 11, 20, 17, 0, tzinfo=TW), **kw):
    from pool.sources import Ctx, bot
    return bot.usd_twd(Ctx(http=_BotHttp(text, **kw), cal={"tw_last": "2026-11-20"}, now=now))


def test_bot_reads_spot_columns_from_multi_currency_csv():
    v, d = _bot(MULTI)
    assert v == {"spot_buy": 33.05, "spot_sell": 33.2, "mid": 33.13} and d == dt.date(2026, 11, 20)


def test_bot_rejects_swapped_columns_wrong_date_and_bad_numbers():
    from pool.util import SourceError
    swapped = MULTI.replace("現金,即期", "即期,現金")            # 上游調換現金與即期欄：標記仍在原位，必須擋下
    for text, kw in [(swapped, {}), (MULTI, {"fname": "ExchangeRate@202611191600.csv"}),
                     (MULTI, {"fname": None}), (MULTI, {"fname": "rates.csv"}),   # 缺檔名或格式改變：無法確認資料日
                     (MULTI.replace("33.05000", "N/A"), {})]:
        with pytest.raises(SourceError):
            _bot(text, **kw)


def test_bot_accepts_url_encoded_filename():
    v, _ = _bot(MULTI, fname="ExchangeRate%40202611201600.csv")
    assert v["spot_buy"] == 33.05
    v, _ = _bot(MULTI, fname="\"ExchangeRate@202611201600.csv\"")
    assert v["spot_sell"] == 33.2


def test_unexpected_exception_text_never_reaches_digest_or_log(capsys):
    # 非 SourceError 的例外（例如轉型錯誤）訊息可能夾帶上游原文：只記例外類型
    from pool import digest as dg
    from pool.sources import Ctx

    def boom(ctx):
        raise ValueError("could not convert 'SECRET-123' to float")
    cat = dg.load_catalog()
    ctx = Ctx(http=None, cal={"tw_last": "2026-11-20"}, now=dt.datetime(2026, 11, 21, 9, 0, tzinfo=TW))
    orig = dict(dg.REG["usd_twd"])
    try:
        dg.REG["usd_twd"] = {"BOT": boom, "FinMind": lambda c: ({"spot_buy": 32.89, "spot_sell": 32.99, "mid": 32.94}, dt.date(2026, 11, 20))}
        e = dg.resolve("usd_twd", cat["items"]["usd_twd"], ctx, "official_first")
        dg.REG["usd_twd"] = {"BOT": boom, "FinMind": boom}
        e2 = dg.resolve("usd_twd", cat["items"]["usd_twd"], ctx, "official_first")
    finally:
        dg.REG["usd_twd"] = orig
    err = capsys.readouterr().err
    assert e["status"] == "ok" and "BOT：ValueError" in err and "SECRET" not in err
    assert e2["notes"] == ["BOT：ValueError", "FinMind：ValueError"]


def test_fallback_reason_goes_to_log_not_digest(capsys):
    from tests.test_golden import build
    out, _ = build(off={"rate.bot.com.tw"})
    assert out["items"]["usd_twd"]["src"].startswith("FinMind:") and "notes" not in out["items"]["usd_twd"]
    assert "[來源備援] usd_twd 改用 FinMind：BOT：" in capsys.readouterr().err


def test_bot_no_data_text_and_zero_spot_are_not_yet():
    from pool.util import NotYet
    with pytest.raises(NotYet):
        _bot("很抱歉，本次查詢找不到任何一筆資料！", fname="XRT.csv")
    with pytest.raises(NotYet):
        _bot(MULTI.replace("33.05000", "0.00000"))


def test_usd_twd_not_collected_intraday_even_when_read_through():
    # 臺銀一天掛牌多次：當日 16:30 前，讀穿或強制重抓都不能把盤中牌價當成當日 ok
    from pool import digest as dg
    from pool.sources import Ctx
    from pool.util import NotYet
    with pytest.raises(NotYet):
        _bot(MULTI, now=dt.datetime(2026, 11, 20, 15, 0, tzinfo=TW))
    cat = dg.load_catalog()
    ctx = Ctx(http=_BotHttp(MULTI), cal={"tw_last": "2026-11-20"}, now=dt.datetime(2026, 11, 20, 15, 0, tzinfo=TW))
    ctx.http.finmind = lambda *a: [{"date": "2026-11-20", "spot_buy": 32.89, "spot_sell": 32.99}]
    e = dg.resolve("usd_twd", cat["items"]["usd_twd"], ctx, "official_first")
    assert e["status"] == "pending"
    v, _ = _bot(MULTI, now=dt.datetime(2026, 11, 21, 9, 0, tzinfo=TW))   # 隔天讀前一日不受門檻影響
    assert v["spot_buy"] == 33.05
