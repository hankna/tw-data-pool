import json, tempfile
from pathlib import Path
from pool import calendar as cal, run as runmod
from pool.store import Store
from tests.fixture_http import FixtureHttp
from tests.compare import diff

GOLD = json.loads((Path(__file__).parent / "golden" / "digest_2026-11-20.json").read_text("utf-8"))
NOW = cal.parse_now("2026-11-21T06:35:00")


def build(off=(), d=None):
    d = d or tempfile.mkdtemp()
    out = runmod.run(FixtureHttp(off=off), Store(d), NOW, trigger="test")
    return out, d


def test_golden():
    out, _ = build()
    assert diff(out, GOLD) == []


def test_official_down_falls_back_to_finmind():
    out, _ = build(off={"twse.com.tw", "openapi.taifex.com.tw"})
    # 證交所、期交所 OpenAPI 全失效：三大法人、期貨法人、融資仍由 FinMind 取得且數字相同
    for k in ("inst_spot", "fut_inst", "margin"):
        assert out["items"][k]["status"] == "ok", k
        assert diff(out["items"][k]["v"], GOLD["items"][k]["v"]) == [], k
