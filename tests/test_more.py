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
    def fake_json(self, url, params=None):
        calls.append(dict(params or {}))
        if params and "token" in params:
            raise h.HttpStatus(403)
        return {"status": 200, "data": [1]}
    monkeypatch.setattr(h.Http, "json", fake_json)
    c = h.Http(token="BAD")
    assert c.finmind("X", None, "2026-01-01", "2026-01-02") == [1] and c.token_state == "invalid"
    assert all("token" not in x for x in calls[1:])
