"""重开日报 PDF + 下载路由。"""
import importlib
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import config, reopen_exec as rx

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    monkeypatch.setenv("DESK_ROOT", str(tmp_path))
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_SECRET", "x" * 32)
    monkeypatch.setenv("WEB_INSECURE", "1")
    import desk.web as web
    web = importlib.reload(web)
    s = config.load(tmp_path)
    web.add_user(s, "kea", "correct horse battery")
    return s, web


def _ledger(s):
    ms = int(datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc).timestamp() * 1000)
    led = rx._led(s)
    ev = "reopen:weekly:2026-10-25:1800"
    led.append("decision", ev, {"event": ev, "name": "SP500", "what": "window_closed", "max_overshoot_bps": 2.0, "t_ms": ms})
    led.append("decision", ev, {"event": ev, "name": "BRENTOIL", "what": "entry", "overshoot_bps": 20.0, "eff_k_bps": 12.0, "t_ms": ms})
    led.append("open", "p1", {"pos_id": "p1", "coin": "xyz:BRENTOIL", "event": ev, "dir": -1, "notional_target": 500.0, "t_ms": ms - 900000})
    led.append("fill", "p1", {"pos_id": "p1", "cloid": "c1", "signed_sz": -5.0, "px": 100.0, "fee_usd": 0.1, "t_ms": ms - 900000})
    led.append("fill", "p1", {"pos_id": "p1", "cloid": "c2", "signed_sz": 5.0, "px": 99.9, "fee_usd": 0.1, "t_ms": ms})
    led.append("closed", "p1", {"pos_id": "p1", "coin": "xyz:BRENTOIL", "reason": "exit", "pnl_usd": 0.3, "t_ms": ms})


def test_pdf(env):
    s, _ = env
    _ledger(s)
    from desk import daily_report
    p = daily_report.make_pdf(s, date(2026, 10, 25))
    assert p.name == "重开日报_2026-10-25.pdf" and p.stat().st_size > 0
    try:
        from pypdf import PdfReader
    except ImportError:
        pytest.skip("no pypdf")
    txt = "".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)
    assert "重开日报" in txt and "Kea Liu" in txt


def test_pdf_empty_day(env):
    s, _ = env
    from desk import daily_report
    assert daily_report.make_pdf(s, date(2026, 10, 20)).stat().st_size > 0


def test_route_and_card(env):
    s, web = env
    d = Path(s.data) / "reports" / "daily"
    d.mkdir(parents=True)
    (d / "重开日报_2026-10-25.pdf").write_bytes(b"%PDF-1.4 x")
    (Path(s.data) / "secret.pdf").write_bytes(b"nope")
    c = TestClient(web.app)
    assert c.get("/reports/daily/重开日报_2026-10-25.pdf", follow_redirects=False).status_code == 303
    c.post("/login", data={"username": "kea", "password": "correct horse battery"}, follow_redirects=False)
    assert c.get("/reports/daily/重开日报_2026-10-25.pdf").content.startswith(b"%PDF")
    for bad in ("..%2Fsecret.pdf", "%2e%2e%2fsecret.pdf", "x.txt", "nope.pdf"):
        assert c.get(f"/reports/daily/{bad}").status_code == 404
    assert "下载日报 PDF" in web.reopen_card_html(s) and "2026-10-25" in web.reopen_card_html(s)
