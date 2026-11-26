import pytest
import datetime as dt, json, tempfile
from pathlib import Path
from pool import calendar as cal, run as runmod, txo
from pool.store import Store
from tests.fixture_http import FixtureHttp
from tests.test_golden import GOLD, NOW, build
from tests.compare import diff
import importlib.util, sys

spec = importlib.util.spec_from_file_location("pool_read", Path(__file__).parents[1] / "tools" / "pool_read.py")
pool_read = importlib.util.module_from_spec(spec); spec.loader.exec_module(pool_read)


def test_idempotent():
    out, d = build()
    again = runmod.run(FixtureHttp(), Store(d), NOW, trigger="test")
    assert again["_changed"] is False and again["snapshot_id"] == out["snapshot_id"]
    rows = Store(d).rows("taiex_daily")
    assert rows[-1]["date"] == "2026-11-20" and len(rows) >= 21


def test_before_due_is_pending():
    now = cal.parse_now("2026-11-20T14:00:00")
    out = runmod.run(FixtureHttp(), Store(tempfile.mkdtemp()), now)
    assert out["items"]["taiex"]["status"] == "pending"
    assert out["items"]["inst_spot"]["status"] == "pending"


def test_expiry_rules():
    d = lambda s: dt.date.fromisoformat(s)
    assert txo.expiry("202611") == d("2026-11-18")      # 第三個週三
    assert txo.expiry("202611W4") == d("2026-11-25")
    assert txo.expiry("202611F3") == d("2026-11-20")
    assert txo.expiry("202612F1") == d("2026-12-04")
    assert txo.expiry("202611F3", [d("2026-11-23")]) == d("2026-11-23")   # 休市順延


def test_pool_read_all_from_pool():
    out, d = build()
    f = tempfile.mktemp()
    code = pool_read.main(["--profile", "chaodilu", "--date", "2026-11-21", "--out", f, "--data-dir", d,
                           "--now", "2026-11-21T06:15:00"], http=FixtureHttp())
    r = json.loads(Path(f).read_text("utf-8"))
    assert code == 0 and r["read"]["via_direct"] == [] and r["derived"]["rv20_annual_pct"] == 48.97


def test_pool_read_through_when_pool_empty():
    d = tempfile.mkdtemp()
    runmod.run(FixtureHttp(), Store(d), cal.parse_now("2026-11-20T14:00:00"))   # 盤中版本：多數 pending
    f = tempfile.mktemp()
    code = pool_read.main(["--profile", "ustw", "--date", "2026-11-21", "--out", f, "--data-dir", d,
                           "--now", "2026-11-21T05:37:00"], http=FixtureHttp())
    r = json.loads(Path(f).read_text("utf-8"))
    assert code == 1 and "inst_spot" in r["read"]["via_direct"]
    assert diff(r["items"]["inst_spot"]["v"], GOLD["items"]["inst_spot"]["v"]) == []


def test_health_and_maintain_import():
    from pool import maintain
    out, d = build()
    h = maintain.health(Store(d), __import__("pool.digest", fromlist=["x"]).load_catalog())
    assert "items" in h and "advice" in h


def test_stdlib_only():
    import re
    root = Path(__file__).parents[1]
    bad = []
    for p in list((root / "pool").rglob("*.py")) + [root / "tools" / "pool_read.py"]:
        for m in re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)", p.read_text("utf-8"), re.M):
            mod = m.group(1)
            if mod not in sys.stdlib_module_names and mod not in ("pool", "tests"):
                bad.append((p.name, mod))
    assert bad == []


def test_finmind_token_4xx_falls_back(monkeypatch):
    from pool import http as h
    calls = []
    def fake_json(self, url, params=None, headers=None):
        assert "token" not in (params or {})
        calls.append(dict(headers or {}))
        if headers and "Authorization" in headers:
            raise h.HttpStatus(403)
        return {"status": 200, "data": [1]}
    monkeypatch.setattr(h.Http, "json", fake_json)
    c = h.Http(token="BAD")
    assert c.finmind("X", None, "2026-01-01", "2026-01-02") == [1] and c.token_state == "invalid"
    assert calls[0] == {"Authorization": "Bearer BAD"}
    assert c.finmind("X", None, "2026-01-01", "2026-01-02") == [1]
    assert len(calls) == 3 and all("Authorization" not in x for x in calls[1:])


def test_recheck_failure_keeps_previous_value():
    out, d = build()
    later = cal.parse_now("2026-11-21T07:00:00")
    again = runmod.run(FixtureHttp(off={"FinMind", "twse.com.tw", "openapi.taifex.com.tw"}), Store(d), later, recheck=True)
    for k in ("taiex", "inst_spot", "margin", "fut_inst"):
        assert again["items"][k]["status"] == "ok" and again["items"][k]["v"] == out["items"][k]["v"], k
    assert any("沿用原值" in n for n in again["health"]["notes"])


def test_pending_turns_missing_after_timeout():
    d = tempfile.mkdtemp()
    early = runmod.run(FixtureHttp(), Store(d), cal.parse_now("2026-11-23T16:00:00"))
    assert early["items"]["taiex"]["status"] == "pending"          # 11/23 尚未公布、未逾時
    late = runmod.run(FixtureHttp(), Store(d), cal.parse_now("2026-11-23T20:00:00"))
    assert late["items"]["taiex"]["status"] == "missing" and late["health"]["alert"] is True


def test_us_holidays():
    days = [dt.date(2026, 11, 1) + dt.timedelta(days=i) for i in range(60)]
    days = [x for x in days if x.weekday() < 5]
    c = cal.compute(cal.parse_now("2026-11-27T06:15:00"), days)
    assert c["us_last"] == "2026-11-25"                                # 11/26 感恩節休市
    assert dt.date(2026, 4, 3) in cal.us_holidays(2026)                # 耶穌受難日
    assert dt.date(2027, 12, 24) in cal.us_holidays(2027)              # 聖誕節逢週六，週五補假


def test_near_month_skips_settling_contract():
    from pool.sources import taifex
    row = lambda m, vol: ["2026/11/18", "TX", m, "1", "1", "1", "1", "0", "0%", str(vol), "1", "1", "", "", "", "", "", "一般"]
    r = taifex._near_row([row("202611", 50000), row("202612", 90000)], dt.date(2026, 11, 18), "一般")
    assert r[2] == "202612"


def test_sched_from_cron():
    s = runmod.sched_from_cron("12 21 * * 1-5", cal.parse_now("2026-11-24T05:40:00"))
    assert s == "2026-11-24T05:12:00+08:00"


def test_pool_read_src_zh_and_history():
    out, d = build()
    f = tempfile.mktemp()
    pool_read.main(["--profile", "panhou", "--date", "2026-11-20", "--out", f, "--data-dir", d,
                    "--now", "2026-11-21T06:15:00"], http=FixtureHttp())
    r = json.loads(Path(f).read_text("utf-8"))
    assert r["items"]["inst_spot"]["src_zh"].startswith("證交所")
    assert r["items"]["vix"]["src_zh"].startswith("期交所")
    assert len(r["taiex_hist"]) >= 21 and r["taiex_hist"][-1][0] == "2026-11-20"



_OFF = {"FinMind", "twse.com.tw", "taifex.com.tw", "openapi.taifex.com.tw", "rate.bot.com.tw"}


def _next_day(daily_margin=None, daily_raw=None, generated="2026-11-23T17:32:00+08:00", now="2026-11-23T18:23:00",
              latest_margin=None):
    """模擬 11/23（週一）：latest 的 margin 已改成今日 pending，前一交易日 11/20 的值只在 daily 留存。"""
    out, d = build()
    root = Path(d)
    lt = json.loads((root / "latest" / "digest.json").read_text("utf-8"))
    prev = lt["items"]["margin"]
    assert prev["status"] == "ok" and prev["asof"] == "2026-11-20"
    lt["items"]["margin"] = latest_margin or {"status": "pending", "asof": None}
    lt["generated_at"] = generated
    (root / "latest" / "digest.json").write_text(json.dumps(lt, ensure_ascii=False), "utf-8")
    dp = root / "daily" / "2026-11-20" / "digest.json"
    if daily_raw is not None:
        if daily_raw == "DELETE":
            dp.unlink()
        else:
            dp.write_text(daily_raw, "utf-8")
    elif daily_margin is not None:
        dd = json.loads(dp.read_text("utf-8"))
        dd["items"]["margin"] = daily_margin
        dp.write_text(json.dumps(dd, ensure_ascii=False), "utf-8")
    f = tempfile.mktemp()
    pool_read.main(["--profile", "panhou", "--date", "2026-11-23", "--out", f, "--data-dir", d, "--now", now],
                   http=FixtureHttp(off=_OFF))
    return prev, json.loads(Path(f).read_text("utf-8"))


def test_pool_read_allow_prev_reads_previous_daily_digest():
    # 18:23 盤後總覽：今日 margin 21:20 才公布，要拿前一交易日留存
    prev, r = _next_day()
    m = r["items"]["margin"]
    assert m["via"] == "pool" and m["asof"] == "2026-11-20" and "允許前一日" in m["note"]
    assert m["v"] == prev["v"] and "margin" not in r["read"]["missing"]
    assert "margin" not in r["read"]["late"] and "margin" not in r["read"]["late_filled"]


@pytest.mark.parametrize("bad", [
    {"status": "pending", "asof": None},
    {"status": "ok", "asof": "2026-11-19", "v": {"balance_bn": 1}},     # 比前一交易日舊
    {"status": "ok", "asof": "2026-11-23", "v": {"balance_bn": 1}},     # 錯位到目標日之後
    {"status": "ok", "asof": 20261120, "v": {"balance_bn": 1}},         # 型別不符
    ["not", "a", "dict"],
    {"status": "ok", "asof": "2026-11-20", "src": ["TWSE"], "v": {"balance_bn": 1}},   # src 型別不符
    {"status": "ok", "asof": "2026-11-20", "src": "TWSE:MI_MARGN"},                     # 缺 v
])
def test_pool_read_allow_prev_rejects_bad_daily_entry(bad):
    _, r = _next_day(daily_margin=bad)
    assert "margin" in r["read"]["missing"] and r["items"]["margin"]["via"] == "none"


def test_pool_read_allow_prev_misdated_latest_falls_to_daily():
    # latest 有已完成但資料日錯位的值：不能採用，要改用 daily 的前一交易日值
    prev, r = _next_day(latest_margin={"status": "ok", "asof": "2026-11-19", "src": "TWSE:MI_MARGN",
                                       "v": {"balance_bn": 9999}})
    assert r["items"]["margin"]["asof"] == "2026-11-20" and r["items"]["margin"]["v"] == prev["v"]


@pytest.mark.parametrize("raw", ["DELETE", "{not json", "[1, 2]"])
def test_pool_read_allow_prev_survives_missing_or_corrupt_daily(raw):
    _, r = _next_day(daily_raw=raw)
    assert "margin" in r["read"]["missing"] and r["items"]["margin"]["via"] == "none"
    need = json.loads((Path(__file__).parents[1] / "pool" / "catalog.json").read_text("utf-8"))["profiles"]["panhou"]["need"]
    assert set(r["items"]) == set(need)   # 整份報告照常輸出


def test_pool_read_allow_prev_skipped_when_pool_stale():
    _, r = _next_day(generated="2026-11-21T06:00:00+08:00")
    assert r["items"]["margin"]["via"] != "pool"


def test_pool_read_prev_value_after_due_counts_as_late():
    # 22:30 仍只有前一日值、公布後一小時內沒有執行紀錄：要列 late，不能算成當日已補齊
    _, r = _next_day(generated="2026-11-23T22:00:00+08:00", now="2026-11-23T22:30:00")
    assert r["items"]["margin"]["asof"] == "2026-11-20"
    assert "margin" in r["read"]["late"] and "margin" not in r["read"]["late_filled"]
