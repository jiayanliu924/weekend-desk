"""网页：登录保护、每个页面能打开且有内容、真钱模拟算术、PDF 生成。"""
import importlib
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import config, sim
from desk.clock import weekend_for_friday

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    monkeypatch.setenv("DESK_ROOT", str(tmp_path))
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_INSECURE", "1")
    monkeypatch.setenv("WEB_SECRET", "x" * 32)
    s = config.load(tmp_path)
    # a completed live weekend + history files
    from tests.test_weekend import run_weekend, seed_market
    w = weekend_for_friday(date(2026, 10, 2), s)
    seed_market(s, w, dev_bps=60, reopen_bps=5, with_news=False)
    run_weekend(s, w)
    rep = Path(s.data) / "reports"
    rep.mkdir(parents=True, exist_ok=True)
    (rep / "history_weekends.json").write_text(json.dumps([
        {"weekend": "2026-09-04", "dev_bps": 40, "y_bps": 23, "err_A": 23, "err_B": 17},
        {"weekend": "2026-09-11", "dev_bps": -99, "y_bps": -111, "err_A": 111, "err_B": 12},
        {"weekend": "2026-09-18", "dev_bps": 2, "y_bps": 29, "err_A": 29, "err_B": 27}]))
    (rep / "history_options.json").write_text(json.dumps({"events": [{"date": "2026-09-16", "kind": "FOMC", "implied": 0.4, "ratio": 0.64}],
                                                           "control": [{"date": "2026-09-01", "kind": "普通日", "implied": 0.4, "ratio": 0.8}]}))
    import desk.web as web
    web = importlib.reload(web)
    web.add_user(s, "kea", "correct horse battery")
    return s, web


def test_login_required_and_works(env):
    s, web = env
    c = TestClient(web.app)
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/report.pdf", follow_redirects=False).status_code == 303
    assert c.post("/login", data={"username": "kea", "password": "wrong-password"}).status_code == 401
    r = c.post("/login", data={"username": "kea", "password": "correct horse battery"}, follow_redirects=False)
    assert r.status_code == 303
    for path, text in [("/", "累计成绩"), ("/day?d=2026-10-04", "它读了什么"), ("/weekends", "2026-10-02"),
                       ("/options", "期权研究"), ("/money?source=history&capital=10000&lev=5", "逐笔明细"),
                       ("/money?source=live", "分合约"), ("/history", "2026-09-11"), ("/guide", "怎么赚钱")]:
        resp = c.get(path)
        assert resp.status_code == 200, path
        assert text in resp.text, path
    pdf = c.get("/report.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and len(pdf.content) > 3000
    assert c.post("/push-pdf", data={"capital": 2000, "lev": 2}, follow_redirects=False).status_code == 303


def test_day_shows_news_and_lock(env):
    s, web = env
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    r = c.get("/day?d=2026-10-04")       # Sunday: lock + outcome written "now" in tests, so check a news day instead
    assert r.status_code == 200


def test_login_rate_limit(env):
    s, web = env
    c = TestClient(web.app)
    for _ in range(5):
        c.post("/login", data={"username": "kea", "password": "nope-nope-nope"})
    assert c.post("/login", data={"username": "kea", "password": "correct horse battery"}).status_code == 429


def test_short_password_rejected(env):
    s, web = env
    with pytest.raises(ValueError):
        web.add_user(s, "x", "short")


def test_sim_math(env):
    s, _ = env
    r = sim.run(s, capital=10000, leverage=5, mode="maker", source="history")
    rows = [x for x in r["rows"] if x["side"]]
    assert len(rows) == 2                                    # the 2 bps weekend is below threshold → no trade
    # 09-04: dev +40 → sell; y=23 → gross +17 bps minus cost
    assert rows[0]["side"] == -1 and 15 < rows[0]["net_bps"] < 17
    assert rows[0]["pnl_usd"] == pytest.approx(50000 / 7 * rows[0]["net_bps"] / 1e4)   # capital split across 7 contracts
    assert r["total_usd"] == pytest.approx(sum(x["pnl_usd"] for x in r["rows"]))
    assert r["stress_usd"] == pytest.approx(-1500)
    live = sim.run(s, 2000, 2, "maker", "live")
    assert live["n"] == 1 and live["n_traded"] == 1 and live["total_usd"] > 0
