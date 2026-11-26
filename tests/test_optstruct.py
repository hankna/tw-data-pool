"""選擇權籌碼與波動結構（opt_struct）：定價、平價遠期、風險逆轉、波動率殘差、未平倉增減、法人部位、接線。"""
import datetime as dt
import json
import math
import random
import tempfile
from pathlib import Path

import pytest

from pool import optstruct as osx, run as runmod
from pool.store import Store
from tests.fixture_http import FixtureHttp
from tests.test_golden import GOLD, NOW, build
from tests.test_more import pool_read

D = dt.date(2026, 11, 20)


# ---------- 定價 ----------
@pytest.mark.parametrize("cp,K", [("call", 40000), ("put", 40000), ("call", 43000), ("put", 37000)])
def test_implied_vol_round_trip(cp, K):
    F, tau, sig = 40000.0, 14 / 365, 0.55
    assert osx.implied_vol(osx.b76(F, K, tau, sig, cp), F, K, tau, cp) == pytest.approx(sig, abs=1e-6)


def test_implied_vol_rejects_price_below_intrinsic():
    assert osx.implied_vol(900, 40000, 39000, 0.05, "call") is None      # 內含價值 1,000
    assert osx.implied_vol(None, 40000, 39000, 0.05, "call") is None


def _chain(F, tau, sig_of):
    return {float(k): {"call": osx.b76(F, k, tau, sig_of(k), "call"), "put": osx.b76(F, k, tau, sig_of(k), "put")}
            for k in range(34000, 46001, 500)}


def test_parity_forward_and_atm_iv_recover_inputs():
    F, tau = 39870.0, 20 / 365
    ch = _chain(F, tau, lambda k: 0.5)
    f, rng = osx.parity_forward(ch)
    assert f == pytest.approx(F, abs=1e-6) and rng < 0.01
    iv, k0 = osx.atm_iv(ch, f, tau)
    assert k0 == 40000 and iv == pytest.approx(0.5, abs=1e-6)


def test_parity_forward_needs_three_valid_strikes():
    assert osx.parity_forward({40000.0: {"call": 500, "put": 480}, 40500.0: {"call": 0.0, "put": 900}}) == (None, None)


def test_risk_reversal_sign_follows_skew():
    F, tau = 40000.0, 20 / 365
    down = osx.risk_reversal(_chain(F, tau, lambda k: 0.5 + (F - k) / F * 1.0), F, tau)    # 履約價越低波動率越高
    flat = osx.risk_reversal(_chain(F, tau, lambda k: 0.5), F, tau)
    assert down["rr"] > 0.05 and abs(flat["rr"]) < 1e-6


# ---------- 序列挑選 ----------
def _rows(specs, d=D):
    out = []
    for c, exp in specs:
        tau = (exp - d).days / 365
        for k in range(38000, 42001, 500):
            for cp in ("call", "put"):
                out.append({"contract": c, "cp": cp, "strike": float(k), "oi": 100, "exp": exp,
                            "settle": osx.b76(40000.0, k, tau, 0.5, cp) if tau > 0 else 0.0})
    return out


def test_pick_series_skips_expiring_and_one_day_left():
    days = [D + dt.timedelta(days=i) for i in range(0, 80) if (D + dt.timedelta(days=i)).weekday() < 5]
    rows = _rows([("202611F3", D), ("202611W4", dt.date(2026, 11, 23)), ("202612W1", dt.date(2026, 12, 2)),
                  ("202612", dt.date(2026, 12, 16)), ("202701", dt.date(2027, 1, 20))])
    pk = osx.pick_series(rows, D, days)
    # F3 當日到期不計；W4 只剩 1 個交易日，往後取 W1
    assert (pk["front"], pk["month"], pk["next"]) == ("202612W1", "202612", "202701")


def test_trading_days_falls_back_to_weekdays_beyond_calendar():
    assert osx.trading_days_to(D, dt.date(2026, 11, 27), []) == 5
    assert osx.trading_days_to(D, D, []) == 0


# ---------- 未平倉增減 ----------
def test_oi_change_excludes_expiring_and_ranks_added_strikes():
    exp_w, exp_m = dt.date(2026, 11, 25), dt.date(2026, 12, 16)
    r = lambda c, cp, k, oi, exp: {"contract": c, "cp": cp, "strike": float(k), "oi": oi, "settle": 1.0, "exp": exp}  # noqa: E731
    prev = [r("202611F3", "put", 39000, 9999, D), r("202611W4", "put", 39000, 100, exp_w), r("202612", "call", 41000, 50, exp_m)]
    cur = [r("202611F3", "put", 39000, 8888, D), r("202611W4", "put", 39000, 400, exp_w),
           r("202611W4", "put", 38500, 250, exp_w), r("202612", "call", 41000, 20, exp_m)]
    o = osx.oi_change(cur, prev, D)
    assert (o["put_oi"], o["call_oi"], o["put_chg"], o["call_chg"]) == (650, 20, 550, -30)
    assert o["added"]["202611W4"]["put"] == [[39000, 300, 400], [38500, 250, 250]]
    assert o["added"]["202612"]["call"] == []            # 減少的不列
    assert osx.oi_change(cur, None, D)["put_chg"] is None


# ---------- 法人部位 ----------
def _inst(date, vals):
    return [{"date": date, "cp": cp, "who": who, "buy": b, "sell": s, "long_oi": lo, "short_oi": so}
            for (who, cp), (b, s, lo, so) in vals.items()]


def test_flow_others_is_mirror_of_institutions():
    base = {(w, cp): (10, 4, 100, 60) for w in osx.PLAYERS for cp in ("call", "put")}
    today = dict(base)
    today[("dealer", "put")] = (5, 30, 80, 140)
    f = osx.flow(_inst("2026-11-19", base) + _inst("2026-11-20", today), D, dt.date(2026, 11, 19))
    assert f["dealer"]["put"] == {"trade_net": -25, "oi_long": 80, "oi_short": 140, "oi_net": -60, "oi_net_chg": -100}
    assert f["others"]["put"]["oi_net"] == -(-60 + 40 + 40) and f["others"]["put"]["oi_net_chg"] == 100
    assert f["others"]["put"]["trade_net"] == -(-25 + 6 + 6)
    assert f["dealer_opt_net"] == 40 + (-60)
    assert osx.flow(_inst("2026-11-19", base), D, None) is None            # 當日沒有資料


# ---------- 波動率殘差 ----------
def _vix_taiex(n=70, beta=-3.0, shock=0.0, seed=7):
    rnd_ = random.Random(seed)
    d0, c, v = dt.date(2026, 6, 1), 40000.0, 30.0
    tai, vix = [(d0.isoformat(), c)], [(d0.isoformat(), v)]
    for i in range(1, n + 1):
        r = rnd_.gauss(0, 1.5)
        e = rnd_.gauss(0, 2.0) + (shock if i == n else 0.0)
        c *= 1 + r / 100
        v *= math.exp((beta * r + e) / 100)
        d = (d0 + dt.timedelta(days=i)).isoformat()
        tai.append((d, c))
        vix.append((d, v))
    return vix, tai


def test_iv_residual_detects_shock_beyond_index_move():
    vix, tai = _vix_taiex(shock=-12.0)
    r = osx.iv_residual(vix, tai, vix[-1][0])
    assert r["n"] == 60 and r["beta"] == pytest.approx(-3.0, abs=0.6)
    assert r["z"] < -3 and r["resid_pts"] < 0
    calm = osx.iv_residual(*_vix_taiex(shock=0.0), vix[-1][0])
    assert abs(calm["z"]) < 3


def test_iv_residual_has_no_lookahead_and_needs_history():
    vix, tai = _vix_taiex()
    d = vix[-1][0]
    a = osx.iv_residual(vix, tai, d)
    bumped = vix[:-1] + [(d, vix[-1][1] * 1.3)]
    b = osx.iv_residual(bumped, tai, d)
    assert a["beta"] == b["beta"] and a["expected_pct"] == b["expected_pct"] and b["resid_pct"] > a["resid_pct"]
    assert osx.iv_residual(vix[-20:], tai[-20:], d) is None                 # 不足 30 個交易日
    assert osx.iv_residual(vix[:-1], tai, d) is None                        # 當日沒有 VIX


# ---------- 接線（2026-11-20 真實樣本；當日 202611F3 到期） ----------
def test_wired_item_matches_official_put_call_oi():
    out, d = build()
    e = out["items"]["opt_struct"]
    assert e["status"] == "ok" and e["asof"] == "2026-11-20" and e["v"]["spec"] == "opt_struct/1"
    v, pc = e["v"], GOLD["items"]["pc_ratio"]["v"]
    # 未到期契約的未平倉合計，必須等於期交所公布的賣權買權比口數（當日到期的 F3 已排除）
    assert (v["oi"]["put_oi"], v["oi"]["call_oi"]) == (pc["put_oi"], pc["call_oi"]) and v["oi"]["pc_match"] is True
    assert v["oi"]["put_chg"] is not None and "202611F3" not in v["oi"]["added"]
    vol = v["vol"]
    assert vol["front"]["contract"] != "202611F3" and vol["front"]["td"] >= 2
    assert vol["month"]["contract"] == "202612" and vol["next"]["contract"] == "202701"
    # 自算的月選價平隱含波動率應與官方台指 VIX 同一量級
    assert abs(vol["month"]["iv_atm"] - GOLD["items"]["vix"]["v"]["close"]) < 8
    assert vol["month"]["f_range_bp"] < 40 and vol["rr25"] is not None
    f = v["flow"]
    for cp in ("call", "put"):
        assert f["others"][cp]["oi_net"] == -sum(f[w][cp]["oi_net"] for w in osx.PLAYERS)
    # 外資買賣權淨未平倉與既有項目 opt_inst 相同（兩個獨立來源）
    assert f["foreign"]["call"]["oi_net"] == GOLD["items"]["opt_inst"]["v"]["foreign"]["call_net_oi"]
    assert f["foreign"]["put"]["oi_net"] == GOLD["items"]["opt_inst"]["v"]["foreign"]["put_net_oi"]
    rows = Store(d).rows("opt_struct")
    assert rows[-1]["date"] == "2026-11-20" and float(rows[-1]["iv_month"]) == vol["month"]["iv_atm"]
    assert set(rows[-1]) == set(osx.SERIES_COLS)


def test_missing_previous_day_keeps_item_with_empty_changes():
    class NoPrev(FixtureHttp):
        def bytes(self, url, params=None, post=None):
            if "optDataDown" in url and post["queryStartDate"] == "2026/11/19":
                from pool.util import SourceError
                raise SourceError("模擬前一日行情取不到")
            return super().bytes(url, params, post)
    out = runmod.run(NoPrev(), Store(tempfile.mkdtemp()), NOW, trigger="test")
    e = out["items"]["opt_struct"]
    assert e["status"] == "ok" and e["v"]["oi"]["put_chg"] is None and e["v"]["oi"]["put_oi"] > 0


def test_source_down_is_not_an_alert():
    out, _ = build(off={"optDataDown"})
    assert out["items"]["opt_struct"]["status"] == "missing"
    assert "opt_struct" not in out["health"]["bad"] and out["health"]["alert"] is False


def test_reader_panhou_profile_serves_and_summarises_item(capsys):
    out, d = build()
    f = tempfile.mktemp()
    code = pool_read.main(["--profile", "panhou", "--date", "2026-11-20", "--out", f, "--data-dir", d,
                           "--now", "2026-11-20T18:23:00"], http=FixtureHttp())
    r = json.loads(Path(f).read_text("utf-8"))
    e = r["items"]["opt_struct"]
    assert code in (0, 1) and e["status"] == "ok" and "三大法人買賣權分計" in e["src_zh"]
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("opt_struct｜"))
    assert "自營商" in line and "價平 IV" in line and "25Δ 風險逆轉" in line


def test_reader_reads_through_with_same_definition():
    d = tempfile.mkdtemp()
    runmod.run(FixtureHttp(), Store(d), __import__("pool.calendar", fromlist=["x"]).parse_now("2026-11-20T14:00:00"))
    f = tempfile.mktemp()
    pool_read.main(["--profile", "panhou", "--date", "2026-11-20", "--out", f, "--data-dir", d,
                    "--now", "2026-11-20T18:23:00"], http=FixtureHttp())
    r = json.loads(Path(f).read_text("utf-8"))
    full, _ = build()
    assert r["items"]["opt_struct"]["via"] == "direct"
    assert r["items"]["opt_struct"]["v"]["vol"]["month"] == full["items"]["opt_struct"]["v"]["vol"]["month"]
    assert r["items"]["opt_struct"]["v"]["flow"] == full["items"]["opt_struct"]["v"]["flow"]
