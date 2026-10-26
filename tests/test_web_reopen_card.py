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


def test_next_event_asia(env):
    s, web = env
    s.raw["reopen"] = {**s.raw["reopen"], "coins": [], "asia_coins": {"kr": ["SMSN"], "jp": [], "hk": [], "cn": []}}
    h = web.reopen_card_html(s, datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))   # 周二 05:00 PT
    assert "三星" in h and "加州 周二 17:01" in h and "北京 周三 08:01" in h


def test_status_line_and_selfcheck(env):
    s, web = env
    rx.write_health(s, event="e", ok=False, failures=["余额不够"])
    h = web.reopen_card_html(s)
    assert "今天跑了 0 场" in h and "成交 0 单" in h
    assert "最近一次自检" in h and "不正常" in h and "余额不够" in h
    rx.write_health(s, ok=True, failures=[])
    assert "正常" in web.reopen_card_html(s)


def test_daily_log(env):
    s, web = env
    now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    ms = int(now.timestamp() * 1000)
    led = rx._led(s)
    ev = "reopen:weekly:2026-10-25:1800"      # 周日 15:00 PT
    ev2 = "reopen:kr_open:2026-10-26:0001"

    def dec(e, n, what, **kw):
        led.append("decision", e, {"event": e, "name": n, "what": what, "t_ms": ms, **kw})
    dec(ev, "BRENTOIL", "entry", overshoot_bps=12.3, eff_k_bps=5.0)
    led.append("open", "p1", {"pos_id": "p1", "coin": "xyz:BRENTOIL", "event": ev, "dir": -1, "notional_target": 500.0, "t_ms": ms - 900000})
    led.append("fill", "p1", {"pos_id": "p1", "cloid": "c1", "signed_sz": -3.0, "px": 161.15, "fee_usd": 0.1, "t_ms": ms - 900000})
    led.append("fill", "p1", {"pos_id": "p1", "cloid": "c2", "signed_sz": 3.0, "px": 161.0, "fee_usd": 0.1, "t_ms": ms})
    led.append("exit_reason", "p1", {"pos_id": "p1", "reason": "time_stop", "t_ms": ms})
    led.append("closed", "p1", {"pos_id": "p1", "coin": "xyz:BRENTOIL", "reason": "time_stop", "pnl_usd": 0.25, "t_ms": ms})
    dec(ev, "GOLD", "entry", overshoot_bps=9.0, eff_k_bps=5.0)
    led.append("open", "p2", {"pos_id": "p2", "coin": "xyz:GOLD", "event": ev, "dir": 1, "notional_target": 500.0, "t_ms": ms})
    led.append("closed", "p2", {"pos_id": "p2", "coin": "xyz:GOLD", "reason": "no_fill", "pnl_usd": 0.0, "t_ms": ms})
    dec(ev, "SILVER", "blocked_retry", why="盘口价差 85 bps 太宽")
    dec(ev, "SILVER", "window_closed", max_overshoot_bps=20.0)
    dec(ev, "CL", "window_closed", max_overshoot_bps=3.2, eff_k_bps=5.0)
    dec(ev2, "SMSN", "no_t0", note="x")
    h = web.reopen_card_html(s, now)
    for t in ("每日明细", "成交 1 笔，合计 +$0.25", "161.15 卖出 $483 → 161 买回", "到时间强制平仓", "挂单没成交，自动撤单",
              "盘口价差 85 bps 太宽（盘口太空，交易不了）", "overshoot 3.2 bps &lt; 门槛 5.0 bps", "价格没跳（没有重开）",
              "周日美股", "韩国开盘", "WTI原油", "三星", "10-25 周日", "overflow-x:auto"):
        assert t in h, t
