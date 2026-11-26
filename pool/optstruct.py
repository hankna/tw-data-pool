"""選擇權籌碼與波動結構（opt_struct）：純函式，不連網。規格見 docs/OPT_STRUCT.md。

盤後才有的三類事實，補盤中「選擇權買賣力道＋隱含波動率」類指標看不到的部分：
  flow ：三大法人與「三大法人以外」在買權、賣權各自的當日買賣淨額與未平倉淨額（誰站在哪一邊）。
  oi   ：未到期契約的未平倉增減、增加最多的履約價（今天成交的是新倉還是平倉）。
  vol  ：價平隱含波動率的期限結構、25 Delta 風險逆轉、波動率風險溢酬、扣掉指數漲跌連動後的波動率殘差。

定價慣例與主機博弈層（tw-index-forecast gl_feature_spec/1）一致：遠期價用買賣權平價反推、r＝0、
tau＝日曆天÷365、價平隱含波動率＝最接近遠期價的履約價，買權與賣權各自反推後取平均。
"""
from __future__ import annotations
import datetime as dt
import math
import statistics
from .util import rnd, iso

SPEC = "opt_struct/1"
PLAYERS = ("dealer", "trust", "foreign")
MIN_SETTLE = 0.1        # 平價法：買權、賣權結算價都要 ≥ 0.1 點
MIN_PRICE_RR = 1.0      # 風險逆轉：結算價低於 1 點的履約價跳動單位雜訊太大，不用
PARITY_N, PARITY_MIN = 5, 3
FRONT_MIN_TD, MONTH_MIN_TD = 2, 5   # 最近到期序列至少還有 2 個交易日；月選至少 5 個交易日
RESID_WIN, RESID_MIN = 60, 30
RESID_MAD_K = 5.0        # 迴歸樣本：VIX 當日變動偏離中位數超過 5 個穩健標準差的日子不納入（官方指數偶有跳動）
VIX_SUSPECT_PCT = 25.0   # 官方台指 VIX 與自算月選價平隱含波動率相差超過 25% 視為疑似異常
IV_MIN, IV_MAX = 0.03, 4.0   # 反推出的隱含波動率超出 3%～400% 視為無效


# ---------- 定價 ----------
def _phi(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def b76(F: float, K: float, tau: float, sig: float, cp: str) -> float:
    """Black-76 價格（r＝0）。cp：'call' 或 'put'。"""
    if tau <= 0 or sig <= 0:
        return max(F - K, 0.0) if cp == "call" else max(K - F, 0.0)
    s = sig * math.sqrt(tau)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    d2 = d1 - s
    return F * _phi(d1) - K * _phi(d2) if cp == "call" else K * _phi(-d2) - F * _phi(-d1)


def delta(F: float, K: float, tau: float, sig: float, cp: str) -> float:
    s = sig * math.sqrt(tau)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    return _phi(d1) if cp == "call" else _phi(d1) - 1.0


def implied_vol(price, F: float, K: float, tau: float, cp: str):
    """二分法反推；價格低於內含價值或超出上限時回傳 None。"""
    if price is None or tau <= 0 or F <= 0 or K <= 0:
        return None
    intrinsic = max(F - K, 0.0) if cp == "call" else max(K - F, 0.0)
    upper = F if cp == "call" else K
    if price <= intrinsic + 1e-9 or price >= upper:
        return None
    lo, hi = 0.005, 6.0
    if b76(F, K, tau, hi, cp) < price:
        return None
    for _ in range(70):
        mid = 0.5 * (lo + hi)
        if b76(F, K, tau, mid, cp) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------- 單一序列 ----------
def chain_of(rows, contract: str) -> dict:
    """{履約價: {'call': 結算價, 'put': 結算價}}"""
    ch = {}
    for r in rows:
        if r["contract"] == contract and r["settle"] is not None:
            ch.setdefault(r["strike"], {})[r["cp"]] = r["settle"]
    return ch


def parity_forward(ch: dict):
    """買賣權平價反推遠期價：取 |C−P| 最小的 5 個履約價，(K＋C−P) 的中位數。回傳 (F, 離散度基點) 或 (None, None)。"""
    ok = [(abs(v["call"] - v["put"]), k, v) for k, v in ch.items()
          if v.get("call") is not None and v.get("put") is not None and v["call"] >= MIN_SETTLE and v["put"] >= MIN_SETTLE]
    if len(ok) < PARITY_MIN:
        return None, None
    ok.sort(key=lambda t: (t[0], t[1]))
    fs = [k + v["call"] - v["put"] for _, k, v in ok[:PARITY_N]]
    F = statistics.median(fs)
    return F, (max(fs) - min(fs)) / F * 1e4


def atm_iv(ch: dict, F: float, tau: float):
    """最接近遠期價的履約價，買權與賣權隱含波動率的平均（任一邊反推失敗就只用另一邊）。"""
    both = [k for k, v in ch.items() if v.get("call") is not None and v.get("put") is not None]
    if not both:
        return None, None
    k0 = min(both, key=lambda k: (abs(k - F), k))
    ivs = [x for x in (implied_vol(ch[k0]["call"], F, k0, tau, "call"), implied_vol(ch[k0]["put"], F, k0, tau, "put")) if x]
    return (sum(ivs) / len(ivs) if ivs else None), k0


def _iv_at_delta(ch: dict, F: float, tau: float, cp: str, target: float = 0.25):
    """價外那一側，|Delta| 內插到 target 的隱含波動率。"""
    pts = []
    for k, v in ch.items():
        if (cp == "put" and k >= F) or (cp == "call" and k <= F):
            continue
        p = v.get(cp)
        if p is None or p < MIN_PRICE_RR:
            continue
        sig = implied_vol(p, F, k, tau, cp)
        if sig:
            pts.append((abs(delta(F, k, tau, sig, cp)), sig))
    pts.sort()
    for (d0, s0), (d1, s1) in zip(pts, pts[1:]):
        if d0 <= target <= d1 and d1 > d0:
            return s0 + (s1 - s0) * (target - d0) / (d1 - d0)
    return None


def risk_reversal(ch: dict, F: float, tau: float):
    """25 Delta 風險逆轉＝25Δ 賣權隱含波動率 − 25Δ 買權隱含波動率（正值＝下檔保護比上檔貴）。"""
    p, c = _iv_at_delta(ch, F, tau, "put"), _iv_at_delta(ch, F, tau, "call")
    if p is None or c is None or not (IV_MIN <= p <= IV_MAX and IV_MIN <= c <= IV_MAX):
        return None
    return {"put25": p, "call25": c, "rr": p - c}


# ---------- 序列挑選 ----------
def trading_days_to(d: dt.date, exp: dt.date, days) -> int:
    """(d, exp] 之間的交易日數；日曆涵蓋不到的部分以週一至週五估。"""
    if exp <= d:
        return 0
    known = [x for x in (days or []) if d < x <= exp]
    last = max([x for x in (days or []) if x <= exp], default=d)
    n = len(known)
    if not days or days[-1] < exp:      # 日曆不夠長：尾段用平日補
        x = max(last, d) + dt.timedelta(days=1)
        while x <= exp:
            n += x.weekday() < 5
            x += dt.timedelta(days=1)
    return n


def pick_series(rows, d: dt.date, days) -> dict:
    """front＝最近到期且距到期 ≥ 2 個交易日；month＝最近月選且 ≥ 5 個交易日；next＝month 之後的下一個月選。"""
    exps = {}
    for r in rows:
        if r["exp"] is not None and r["exp"] > d and "/" not in r["contract"]:
            exps[r["contract"]] = r["exp"]
    td = {c: trading_days_to(d, e, days) for c, e in exps.items()}
    order = sorted(exps, key=lambda c: (exps[c], c))
    front = next((c for c in order if td[c] >= FRONT_MIN_TD), None)
    monthly = [c for c in order if len(c) == 6]
    month = next((c for c in monthly if td[c] >= MONTH_MIN_TD), None)
    nxt = None
    if month:
        later = [c for c in monthly if exps[c] > exps[month]]
        nxt = later[0] if later else None
    return {"front": front, "month": month, "next": nxt, "_exp": exps, "_td": td}


def series_point(rows, contract, d: dt.date, exp: dt.date, td: int):
    ch = chain_of(rows, contract)
    F, rng = parity_forward(ch)
    if F is None:
        return None
    tau = (exp - d).days / 365.0
    iv, k0 = atm_iv(ch, F, tau)
    if iv is None or not IV_MIN <= iv <= IV_MAX:
        return None
    return {"contract": contract, "exp": iso(exp), "td": td, "F": rnd(F, 1), "f_range_bp": rnd(rng, 1),
            "atm_strike": int(k0) if k0 == int(k0) else k0, "iv_atm": rnd(iv * 100), "_ch": ch, "_F": F, "_tau": tau}


# ---------- 未平倉增減 ----------
def oi_change(cur, prev, d: dt.date) -> dict:
    """未到期契約（到期日 > d）的買權、賣權未平倉合計與對前一交易日增減；最近週選與月選各自增加最多的履約價。
    前一日沒有的契約視為 0（新掛牌）；prev 為 None 時增減欄位為 None。"""
    live = {r["contract"] for r in cur if r["exp"] is not None and r["exp"] > d and "/" not in r["contract"]}
    tot = {"call": 0, "put": 0}
    now = {}
    for r in cur:
        if r["contract"] in live and r["oi"] is not None:
            tot[r["cp"]] += r["oi"]
            now[(r["contract"], r["cp"], r["strike"])] = r["oi"]
    expiring = sorted({r["contract"] for r in cur if r["exp"] is not None and r["exp"] == d and "/" not in r["contract"]})
    out = {"call_oi": tot["call"], "put_oi": tot["put"], "call_chg": None, "put_chg": None, "expiring_today": expiring,
           "added": {}}
    if prev is None:
        return out
    was = {(r["contract"], r["cp"], r["strike"]): r["oi"] for r in prev if r["contract"] in live and r["oi"] is not None}
    chg = {"call": 0, "put": 0}
    for key, oi in now.items():
        chg[key[1]] += oi - was.get(key, 0)
    out["call_chg"], out["put_chg"] = chg["call"], chg["put"]
    exp_of = {r["contract"]: r["exp"] for r in cur if r["contract"] in live}
    weekly = min((c for c in live if len(c) > 6), key=lambda c: exp_of[c], default=None)
    monthly = min((c for c in live if len(c) == 6), key=lambda c: exp_of[c], default=None)
    for c in (weekly, monthly):
        if not c:
            continue
        out["added"][c] = {}
        for cp in ("call", "put"):
            xs = sorted(((oi - was.get((c, cp, k), 0), k, oi) for (cc, p, k), oi in now.items() if cc == c and p == cp),
                        key=lambda t: (-t[0], t[1]))
            out["added"][c][cp] = [[int(k) if k == int(k) else k, int(dd), int(oi)] for dd, k, oi in xs[:3] if dd > 0]
    return out


# ---------- 法人部位 ----------
def flow(inst_rows, d: dt.date, prev_day) -> dict:
    """inst_rows：{'date','cp','who','buy','sell','long_oi','short_oi'}（who ∈ dealer、trust、foreign）。
    回傳各身分在買權、賣權的 trade_net（當日買−賣）、oi_net（買方未平倉−賣方未平倉）、oi_net_chg；
    others＝三大法人合計的反向（散戶與一般法人；每一口買方必有一口賣方）。
    dealer_opt_net＝自營商買權淨額＋賣權淨額（正＝選擇權淨買方）。"""
    by = {}
    for r in inst_rows:
        by.setdefault(r["date"], {})[(r["who"], r["cp"])] = r
    cur, prev = by.get(iso(d)), by.get(iso(prev_day)) if prev_day else None
    if not cur or len(cur) < 6:
        return None
    out = {}
    for who in PLAYERS:
        out[who] = {}
        for cp in ("call", "put"):
            r = cur[(who, cp)]
            net = r["long_oi"] - r["short_oi"]
            p = prev.get((who, cp)) if prev else None
            out[who][cp] = {"trade_net": r["buy"] - r["sell"], "oi_long": r["long_oi"], "oi_short": r["short_oi"],
                            "oi_net": net, "oi_net_chg": (net - (p["long_oi"] - p["short_oi"])) if p else None}
    out["others"] = {}
    for cp in ("call", "put"):
        chgs = [out[w][cp]["oi_net_chg"] for w in PLAYERS]
        out["others"][cp] = {"trade_net": -sum(out[w][cp]["trade_net"] for w in PLAYERS),
                             "oi_net": -sum(out[w][cp]["oi_net"] for w in PLAYERS),
                             "oi_net_chg": None if any(c is None for c in chgs) else -sum(chgs)}
    out["dealer_opt_net"] = out["dealer"]["call"]["oi_net"] + out["dealer"]["put"]["oi_net"]
    return out


# ---------- 波動率殘差 ----------
def _robust_keep(ys):
    """回傳 (中位數, 穩健標準差)；穩健標準差＝1.4826×絕對離差中位數。"""
    med = statistics.median(ys)
    return med, 1.4826 * statistics.median(abs(y - med) for y in ys)


def iv_residual(vix_rows, taiex_rows, d: str, win: int = RESID_WIN, min_n: int = RESID_MIN):
    """台指 VIX 當日變動扣掉「指數漲跌能解釋的部分」之後的殘差。
    作法：y＝100×ln(VIX_t÷VIX_{t−1})，x＝加權指數當日報酬 %；用 d 之前 win 個交易日做最小平方法（不含 d，防前視），
    殘差＝y_d −（a＋b×x_d），z＝殘差÷視窗內殘差標準差。負值＝波動率跌得比指數漲跌能解釋的多。
    官方 VIX 偶有單日跳動（2026-11-24 公布 26.18，前後兩日為 55.36、58.38）：視窗內 y 偏離中位數超過
    RESID_MAD_K 個穩健標準差的日子不進迴歸（dropped 記錄剔除幾天）。前一交易日被剔除、當日又反向大幅變動時，
    當日變動多半只是指數跳回來，標 unreliable＝'prev_vix_outlier'，報告不判讀。"""
    v = {r[0]: float(r[1]) for r in vix_rows if r[1] not in (None, "")}
    c = {r[0]: float(r[1]) for r in taiex_rows if r[1] not in (None, "")}
    dates = sorted(x for x in set(v) & set(c) if x <= d)
    if len(dates) < min_n + 2 or dates[-1] != d:
        return None
    obs = []
    for a, b in zip(dates, dates[1:]):
        if v[a] > 0 and v[b] > 0 and c[a] > 0:
            obs.append((b, (c[b] / c[a] - 1) * 100, 100 * math.log(v[b] / v[a])))
    if not obs or obs[-1][0] != d:
        return None
    today, raw = obs[-1], obs[:-1][-win:]
    if len(raw) < min_n:
        return None
    med, rsd = _robust_keep([o[2] for o in raw])
    out_of = (lambda y: abs(y - med) > RESID_MAD_K * rsd) if rsd > 0 else (lambda y: False)
    hist = [o for o in raw if not out_of(o[2])]
    if len(hist) < min_n:
        return None
    mx, my = statistics.fmean(o[1] for o in hist), statistics.fmean(o[2] for o in hist)
    sxx = sum((o[1] - mx) ** 2 for o in hist)
    if sxx <= 0:
        return None
    beta = sum((o[1] - mx) * (o[2] - my) for o in hist) / sxx
    alpha = my - beta * mx
    res = [o[2] - (alpha + beta * o[1]) for o in hist]
    sd = statistics.stdev(res)
    if sd <= 0:
        return None
    exp_pct = alpha + beta * today[1]
    resid = today[2] - exp_pct
    prev_v = v[dates[-2]]
    snap_back = out_of(raw[-1][2]) and out_of(today[2]) and (raw[-1][2] - med) * (today[2] - med) < 0
    return {"n": len(hist), "dropped": len(raw) - len(hist), "beta": rnd(beta, 3), "ret_pct": rnd(today[1]),
            "vix_chg_pct": rnd(today[2]), "expected_pct": rnd(exp_pct), "resid_pct": rnd(resid),
            "resid_pts": rnd(prev_v * (math.exp(today[2] / 100) - math.exp(exp_pct / 100))), "z": rnd(resid / sd),
            "unreliable": "prev_vix_outlier" if snap_back else None}


def realized_vol20(taiex_rows, d: str):
    cl = [float(r[1]) for r in sorted(taiex_rows) if r[0] <= d and r[1] not in (None, "")]
    if len(cl) < 21:
        return None
    c = cl[-21:]
    return statistics.stdev([(c[i] / c[i - 1] - 1) * 100 for i in range(1, 21)]) * math.sqrt(252)


# ---------- 組裝 ----------
def calc(cur, prev, inst_rows, d: dt.date, prev_day, days, vix_rows, taiex_rows) -> dict:
    """cur、prev：逐履約價列 {'contract','cp','strike','settle','oi','exp'}（prev 可為 None）。
    vix_rows、taiex_rows：[(日期, 收盤)]，含 d 當日。"""
    pk = pick_series(cur, d, days)
    pts = {}
    for role in ("front", "month", "next"):
        c = pk[role]
        pts[role] = series_point(cur, c, d, pk["_exp"][c], pk["_td"][c]) if c else None
    vol = {r: ({k: v for k, v in p.items() if not k.startswith("_")} if p else None) for r, p in pts.items()}
    f, m, n = pts["front"], pts["month"], pts["next"]
    vol["front_minus_month"] = rnd(f["iv_atm"] - m["iv_atm"]) if f and m and f["contract"] != m["contract"] else None
    vol["month_minus_next"] = rnd(m["iv_atm"] - n["iv_atm"]) if m and n else None
    rr = risk_reversal(m["_ch"], m["_F"], m["_tau"]) if m else None
    vol["rr25"] = {"contract": m["contract"], "put25": rnd(rr["put25"] * 100), "call25": rnd(rr["call25"] * 100),
                   "rr": rnd(rr["rr"] * 100)} if rr else None
    ds = iso(d)
    vx = next((float(r[1]) for r in vix_rows if r[0] == ds and r[1] not in (None, "")), None)
    rv = realized_vol20(taiex_rows, ds)
    vol["vix"], vol["rv20"] = vx, rnd(rv)
    # 官方 VIX 與自算月選價平隱含波動率互相檢查：差太多時 VIX 相關的兩個數字（vrp、殘差）不可靠
    chk = None
    if vx is not None and m:
        diff = (vx / m["iv_atm"] - 1) * 100
        chk = {"iv_month": m["iv_atm"], "diff_pct": rnd(diff), "suspect": abs(diff) > VIX_SUSPECT_PCT}
    vol["vix_check"] = chk
    bad = bool(chk and chk["suspect"])
    vol["vrp"] = rnd(vx - rv) if vx is not None and rv is not None and not bad else None
    res = iv_residual(vix_rows, taiex_rows, ds)
    if res and bad:
        res["unreliable"] = "vix_suspect"
    vol["iv_resid"] = res
    return {"spec": SPEC, "flow": flow(inst_rows, d, prev_day), "oi": oi_change(cur, prev, d), "vol": vol}


def series_row(asof: str, v: dict) -> dict:
    """寫進 series/opt_struct.csv 的一列（影子期累積用）。"""
    g = lambda *path: _dig(v, path)   # noqa: E731
    unrel = g("vol", "iv_resid", "unreliable")   # 不可靠的殘差不進帳本，免得日後檢定被官方指數跳動污染
    row = {"date": asof, "iv_front": g("vol", "front", "iv_atm"), "iv_month": g("vol", "month", "iv_atm"),
           "iv_next": g("vol", "next", "iv_atm"), "rr25": g("vol", "rr25", "rr"), "vix": g("vol", "vix"),
           "rv20": g("vol", "rv20"), "vrp": g("vol", "vrp"), "resid_pct": None if unrel else g("vol", "iv_resid", "resid_pct"),
           "resid_z": None if unrel else g("vol", "iv_resid", "z"), "vix_diff_pct": g("vol", "vix_check", "diff_pct"), "call_oi_chg": g("oi", "call_chg"), "put_oi_chg": g("oi", "put_chg")}
    for who in PLAYERS + ("others",):
        for cp in ("call", "put"):
            row[f"{who}_{cp}_trade"] = g("flow", who, cp, "trade_net")
            row[f"{who}_{cp}_net"] = g("flow", who, cp, "oi_net")
    return row


SERIES_COLS = (["date", "iv_front", "iv_month", "iv_next", "rr25", "vix", "rv20", "vrp", "resid_pct", "resid_z",
                "vix_diff_pct", "call_oi_chg", "put_oi_chg"]
               + [f"{w}_{cp}_{k}" for w in PLAYERS + ("others",) for cp in ("call", "put") for k in ("trade", "net")])


def _dig(v, path):
    for p in path:
        if not isinstance(v, dict) or v.get(p) is None:
            return None
        v = v[p]
    return v
