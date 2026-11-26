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


def _with_glitch(vix, at):
    """把第 at 個交易日的 VIX 砍半（隔天恢復），模擬 2026-11-24 官方指數的單日跳動。"""
    return [(d, x * 0.5 if i == at else x) for i, (d, x) in enumerate(vix)]


def test_iv_residual_ignores_official_vix_glitch_in_history():
    vix, tai = _vix_taiex()
    d = vix[-1][0]
    clean, hit = osx.iv_residual(vix, tai, d), osx.iv_residual(_with_glitch(vix, len(vix) - 6), tai, d)
    # 砍半與跳回來兩天都被剔除，迴歸係數與當日殘差幾乎不變
    assert clean["dropped"] == 0 and hit["dropped"] == 2 and hit["n"] == 58
    assert hit["beta"] == pytest.approx(clean["beta"], abs=0.25) and hit["z"] == pytest.approx(clean["z"], abs=0.4)
    assert hit["unreliable"] is None


def test_iv_residual_flags_snap_back_day_and_day_after_is_usable():
    vix, tai = _vix_taiex()
    n = len(vix)
    snap = osx.iv_residual(_with_glitch(vix, n - 2), tai, vix[-1][0])            # 昨天砍半、今天跳回來
    assert snap["unreliable"] == "prev_vix_outlier" and snap["z"] > 5
    after = osx.iv_residual(_with_glitch(vix, n - 3), tai, vix[-1][0])           # 前天砍半、昨天跳回來
    assert after["unreliable"] is None and after["dropped"] == 2


def test_real_shock_today_is_not_flagged():
    r = osx.iv_residual(*_vix_taiex(shock=45.0), _vix_taiex()[0][-1][0])        # 前一日正常，今天波動率暴衝
    assert r["unreliable"] is None and r["z"] > 5


def test_calc_flags_official_vix_far_from_own_month_iv():
    days = [D + dt.timedelta(days=i) for i in range(0, 80) if (D + dt.timedelta(days=i)).weekday() < 5]
    rows = _rows([("202612", dt.date(2026, 12, 16)), ("202701", dt.date(2027, 1, 20))])      # 自算月選約 50%
    base = {(w, cp): (10, 4, 100, 60) for w in osx.PLAYERS for cp in ("call", "put")}
    vix, tai = _vix_taiex()
    shift = lambda xs, last: [(("2026-11-20" if i == len(xs) - 1 else d), (last if i == len(xs) - 1 and last else x))  # noqa: E731
                              for i, (d, x) in enumerate(xs)]
    ok = osx.calc(rows, None, _inst("2026-11-20", base), D, None, days, shift(vix, 52.0), shift(tai, None))
    bad = osx.calc(rows, None, _inst("2026-11-20", base), D, None, days, shift(vix, 26.0), shift(tai, None))
    assert ok["vol"]["vix_check"]["suspect"] is False and ok["vol"]["vrp"] is not None
    assert bad["vol"]["vix_check"]["suspect"] is True and bad["vol"]["vix_check"]["diff_pct"] < -25
    assert bad["vol"]["vrp"] is None and bad["vol"]["iv_resid"]["unreliable"] == "vix_suspect"
    row = osx.series_row("2026-11-20", bad)
    assert row["resid_z"] is None and row["vix_diff_pct"] == bad["vol"]["vix_check"]["diff_pct"]   # 不可靠的殘差不進帳本
    assert osx.series_row("2026-11-20", ok)["resid_z"] == ok["vol"]["iv_resid"]["z"]


def test_implausible_iv_is_dropped():
    exp = dt.date(2026, 12, 16)
    rows = _rows([("202612", exp)])
    for r in rows:                      # 結算價被灌成接近履約價：反推出的波動率超過 400%
        r["settle"] = r["strike"] * 0.95 if r["cp"] == "put" else 40000 * 0.95
    assert osx.series_point(rows, "202612", D, exp, 18) is None


# ---------- 接線（2026-11-20 真實樣本；當日 202611F3 到期） ----------
def test_wired_item_matches_official_put_call_oi():
    out, d = build()
    e = out["items"]["opt_struct"]
    assert e["status"] == "ok" and e["asof"] == "2026-11-20" and e["v"]["spec"] == "opt_struct/1"
    v, pc = e["v"], GOLD["items"]["pc_ratio"]["v"]
    # 未到期契約的未平倉合計，必須等於期交所公布的賣權買權比口數（當日到期的 F3 已排除）
    assert (v["oi"]["put_oi"], v["oi"]["call_oi"]) == (pc["put_oi"], pc["call_oi"]) and v["oi"]["pc_match"] is True
    assert v["oi"]["put_chg"] is not None and "202611F3" not in v["oi"]["added"]
    assert v["oi"]["expiring_today"] == ["202611F3"]
    assert v["vol"]["vix_check"]["suspect"] is False and abs(v["vol"]["vix_check"]["diff_pct"]) < 10
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


def test_before_1730_waits_for_next_slot_instead_of_locking_partial_result():
    from pool import calendar as cal
    from pool.util import SourceError

    class NoPrev(FixtureHttp):
        def bytes(self, url, params=None, post=None):
            if "optDataDown" in url and post["queryStartDate"] == "2026/11/19":
                raise SourceError("模擬前一日行情暫時取不到")
            return super().bytes(url, params, post)
    d = tempfile.mkdtemp()
    early = runmod.run(NoPrev(), Store(d), cal.parse_now("2026-11-20T16:20:00"), trigger="test")
    assert early["items"]["opt_struct"]["status"] == "pending"                     # 16:17 時段：不定案，等 17:32
    later = runmod.run(FixtureHttp(), Store(d), cal.parse_now("2026-11-20T17:35:00"), trigger="test")
    assert later["items"]["opt_struct"]["status"] == "ok" and later["items"]["opt_struct"]["v"]["oi"]["put_chg"] is not None


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


# ---------- Codex 合併後覆核 #9 的 7 項指正（2026-11-26） ----------
def test_atm_iv_rejects_out_of_range_leg_before_averaging(monkeypatch):
    # 買權反推 1%、賣權 5%：原本平均成「有效」的 3%，現在 1% 先被剔除，只剩 5%
    ch = {40000.0: {"call": 1.0, "put": 2.0}}
    seq = iter([0.01, 0.05])
    monkeypatch.setattr(osx, "implied_vol", lambda *a, **k: next(seq))
    iv, k0 = osx.atm_iv(ch, 40000.0, 0.05)
    assert k0 == 40000 and iv == pytest.approx(0.05)
    seq2 = iter([0.01, 5.0])                       # 兩邊都超界：沒有價平 IV
    monkeypatch.setattr(osx, "implied_vol", lambda *a, **k: next(seq2))
    assert osx.atm_iv(ch, 40000.0, 0.05)[0] is None


def test_rr_interpolation_skips_out_of_range_strikes():
    F, tau = 40000.0, 20 / 365
    good = _chain(F, tau, lambda k: 0.5)
    base = osx.risk_reversal(good, F, tau)
    bad = {k: dict(v) for k, v in good.items()}
    # 把一個價外賣權的價格灌到反推超過 400% 的程度：這個點不能進內插
    k_bad = 37000.0
    bad[k_bad]["put"] = osx.b76(F, k_bad, tau, 4.5, "put")
    assert osx.implied_vol(bad[k_bad]["put"], F, k_bad, tau, "put") > osx.IV_MAX
    got = osx.risk_reversal(bad, F, tau)
    assert got is not None and got["put25"] == pytest.approx(base["put25"], abs=1e-6)


def _series(n=70, seed=11):
    vix, tai = _vix_taiex(n=n, seed=seed)
    days = [dt.date.fromisoformat(d) for d, _ in tai]
    return vix, tai, days


def test_residual_never_pairs_across_a_missing_trading_day():
    vix, tai, days = _series(n=40)                 # 視窗不滿 60：缺一天時樣本數的變化看得出來
    d = vix[-1][0]
    full = osx.iv_residual(vix, tai, d, days=days)
    # 前一交易日缺 VIX：當日沒有可比的基準，不能拿兩天前當昨天
    assert osx.iv_residual(vix[:-2] + vix[-1:], tai, d, days=days) is None
    # 加權缺前一交易日也一樣
    assert osx.iv_residual(vix, tai[:-2] + tai[-1:], d, days=days) is None
    # 視窗中間缺一天：那一天前後兩個觀測都不算，樣本少 2 筆，而不是多出一筆兩天的變動
    mid = len(vix) - 20
    holey = osx.iv_residual(vix[:mid] + vix[mid + 1:], tai, d, days=days)
    # 缺一天：缺口兩側的兩個觀測都不成立，樣本少 2 筆；若誤把兩天變動當成一天，只會少 1 筆
    assert holey["n"] + holey["dropped"] == full["n"] + full["dropped"] - 2
    # 沒有交易日曆時，加權的日期仍能揭露缺口
    assert osx.iv_residual(vix[:-2] + vix[-1:], tai, d) is None


def test_snap_back_uses_previous_day_reconciliation_when_available():
    vix, tai, days = _series()
    n = len(vix)
    spike = [(dd, x * 2.0 if i == n - 2 else x) for i, (dd, x) in enumerate(vix)]   # 昨天真的暴衝、今天回落
    d = vix[-1][0]
    stat_only = osx.iv_residual(spike, tai, d, days=days)                  # 沒有前一日行情：統計規則會標跳回日
    assert stat_only["unreliable"] == "prev_vix_outlier"
    real = osx.iv_residual(spike, tai, d, days=days, prev_suspect=False)  # 昨天官方值與自算值一致：是真行情
    assert real["unreliable"] is None
    glitch = osx.iv_residual(vix, tai, d, days=days, prev_suspect=True)   # 昨天官方值疑似異常：今天是跳回日
    assert glitch["unreliable"] == "prev_vix_outlier"


def test_rv20_needs_close_on_data_date():
    vix, tai, days = _series()
    d = tai[-1][0]
    assert osx.realized_vol20(tai, d) is not None
    assert osx.realized_vol20(tai[:-1], d) is None                 # 當日收盤缺漏：不拿前一日的 RV 當今天


def test_iv30_interpolates_total_variance_and_flags_against_30_day_level():
    d = dt.date(2026, 11, 26)
    pts = [{"exp": "2026-12-02", "iv_atm": 61.11}, {"exp": "2026-12-16", "iv_atm": 55.35}, {"exp": "2027-01-20", "iv_atm": 47.76}]
    v30 = osx.iv30(pts, d)
    t1, t2, t30 = 20 / 365, 55 / 365, 30 / 365
    w = 0.5535 ** 2 * t1 + (0.4776 ** 2 * t2 - 0.5535 ** 2 * t1) * (t30 - t1) / (t2 - t1)
    assert v30 == pytest.approx(math.sqrt(w / t30) * 100, abs=1e-9) and 47.76 < v30 < 55.35
    assert osx.iv30(pts[:1], d) == pytest.approx(61.11)            # 只有單邊：取最近的一個
    assert osx.iv30([], d) is None and osx.iv30([{"exp": "2026-11-26", "iv_atm": 50}], d) is None
    # 月選剩 5 天、次月低很多時，舊做法（只比月選）會誤標，30 天內插不會
    near = [{"exp": "2026-12-03", "iv_atm": 80.0}, {"exp": "2027-01-20", "iv_atm": 50.0}]
    chk = osx.vix_check(56.0, near, d)
    assert chk["suspect"] is False and (56.0 / 80.0 - 1) * 100 < -25


def test_pc_mismatch_is_not_published():
    from pool import calendar as cal
    from pool.util import NotYet, SourceError
    out, d = build()
    assert out["items"]["opt_struct"]["v"]["oi"]["pc_match"] is True

    class DropRows(FixtureHttp):
        """逐履約價檔少一列：合計偏低，和賣權買權比對不上。"""
        def bytes(self, url, params=None, post=None):
            raw = super().bytes(url, params, post)
            if "optDataDown" in url and post["queryStartDate"] == "2026/11/20":
                import gzip
                lines = raw.decode("big5", "ignore").splitlines()
                # 刪掉第一列「月選 202612、賣權、未平倉大於 0」的資料
                hit = next(i for i, l in enumerate(lines) if len(f := l.split(",")) > 17 and f[2].strip() == "202612"
                           and f[4] == "賣權" and f[17] == "一般" and f[11].isdigit() and int(f[11]) > 0)
                return "\r\n".join(lines[:hit] + lines[hit + 1:]).encode("big5")
            return raw
    late = runmod.run(DropRows(), Store(tempfile.mkdtemp()), NOW, trigger="test")
    assert late["items"]["opt_struct"]["status"] == "missing"
    assert any("不符" in n for n in late["items"]["opt_struct"].get("notes", []))     # 確認是因為口數不符
    early = runmod.run(DropRows(), Store(tempfile.mkdtemp()), cal.parse_now("2026-11-20T16:20:00"), trigger="test")
    assert early["items"]["opt_struct"]["status"] == "pending"
