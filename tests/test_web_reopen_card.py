"""首页「重开真钱单」卡：空账本 / 样例账本 / 坏账本都不能拖垮页面。"""
import importlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from desk import config, reopen_exec as rx

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    monkeypatch.setenv("DESK_ROOT", str(tmp_path))
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_INSECURE", "1")
    monkeypatch.setenv("WEB_SECRET", "x" * 32)
    import desk.web as web
    return config.load(tmp_path), importlib.reload(web)


def test_empty(env):
    s, web = env
    h = web.reopen_card_html(s)
    assert "重开真钱单" in h and "关着" in h and "还没有记录" in h


def test_sample(env):
    s, web = env
    now = datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc)   # 周日 16:00 PT
    ms = int(now.timestamp() * 1000)
    led = rx._led(s)
    ev = "reopen:weekly:2026-10-25:1800"
    led.append("decision", ev, {"event": ev, "name": "SP500", "what": "window_closed", "t_ms": ms})
    led.append("decision", ev, {"event": ev, "name": "BRENTOIL", "what": "entry", "t_ms": ms})
    led.append("open", "p1", {"pos_id": "p1", "coin": "xyz:BRENTOIL", "event": ev, "dir": -1, "notional_target": 500.0, "t_ms": ms - 900000})
    led.append("fill", "p1", {"pos_id": "p1", "cloid": "c1", "signed_sz": -5.0, "px": 100.0, "fee_usd": 0.1, "t_ms": ms - 900000})
    led.append("fill", "p1", {"pos_id": "p1", "cloid": "c2", "signed_sz": 5.0, "px": 99.9, "fee_usd": 0.1, "t_ms": ms})
    led.append("closed", "p1", {"pos_id": "p1", "coin": "xyz:BRENTOIL", "reason": "exit", "pnl_usd": 0.3, "t_ms": ms})
    h = web.reopen_card_html(s, now)
    assert "原油：卖出 $500，15 分钟后买回，赚 $0.30（已扣手续费）" in h
    assert "价格没被推过头，没下单（正常）" in h
    assert "共 1 单，赚 1 单，亏 0 单" in h and "已成交 1 / 20" in h and "/ 上限 $15" in h


def test_corrupt(env):
    s, web = env
    rx._led(s).path.write_text("{not json\n")
    assert "重开真钱单" in web.reopen_card_html(s)
