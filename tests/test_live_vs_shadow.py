"""G2：/admin「周末线：影子 vs 真钱 逐笔对账」卡。每种真钱状态都要在页面上看得到；
文件为空、坏了都只显示"还没有数据"，不能报错。"""
import importlib
import shutil
from pathlib import Path

import pytest

from desk import config
from desk.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
TITLE = "周末线：影子 vs 真钱 逐笔对账（最近 4 个周末）"


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


def _shadow(s, wid, side, notional, pnl_full):
    """一个官方周末的主策略 action + 全额手续费结果（写进 experiments.jsonl）。"""
    led = Ledger(s.data)
    led.append("lock", wid, {"features": {"ok": True, "dev_bps": 50.0, "dec_mid": 100.0, "fri_close": 100.0},
                             "action": {"side": side, "notional_usd": notional, "entry_px": 100.0, "reason": "测试"},
                             "shadow": {}, "void": False, "prediction": {"pred_bps": 0.0}})
    led.append("outcome", wid, {"ok": True, "y_bps": 0.0, "pnl_net_usd": pnl_full,
                                "pnl_net_fullfee_usd": pnl_full, "direction_hit": True})


def _live(s, kind, wid, body):
    Ledger(s.data, "live").append(kind, wid, body)


def test_each_real_state_is_shown(env):
    s, web = env
    F, P = "2026-10-16", "2026-10-09"
    _shadow(s, f"{F}|NVDA", 1, 100.0, 10.0)      # 成交，但真钱只下了一半 → 规模小
    _shadow(s, f"{F}|TSLA", -1, 100.0, -4.0)     # 挂单没成交被撤
    _shadow(s, f"{F}|INTC", 1, 100.0, 6.0)       # 被交易所拒
    _shadow(s, f"{F}|SP500", 1, 100.0, 2.0)      # 风控否决，没下单
    _shadow(s, f"{F}|XYZ100", -1, 100.0, -1.0)   # 账本里没有记录
    _shadow(s, f"{P}|NVDA", 1, 100.0, 5.0)       # 没下单（nolive）
    _shadow(s, "2026-10-02|NVDA", 0, 0.0, 0.0)   # 不出手的周末，仍算在最近 4 个周末里
    _shadow(s, "2026-09-25|NVDA", 0, 0.0, 0.0)
    _shadow(s, "2026-09-11|TSLA", -1, 100.0, 4.0)  # 第 5 个周末，不在窗口里

    _live(s, "entry", f"{F}|NVDA", {"mode": "live", "coin": "NVDA", "name": "NVDA", "side": "买",
                                    "notional_usd": 50.0, "sent": True, "sz": 0.5, "px": 100.0})
    _live(s, "cancel", f"{F}|NVDA", {"coin": "NVDA", "cancelled": 0, "position": 0.5, "filled_sz": 0.5, "entry_avg_px": 100.0})
    _live(s, "exit", f"{F}|NVDA", {"coin": "NVDA", "name": "NVDA", "position_before": 0.5, "n_fills": 2,
                                   "pnl_usd": -3.5, "fills_complete": True})
    _live(s, "entry", f"{F}|TSLA", {"mode": "live", "coin": "TSLA", "name": "TSLA", "side": "卖",
                                    "notional_usd": 60.0, "sent": True, "sz": 0.6, "px": 100.0})
    _live(s, "cancel", f"{F}|TSLA", {"coin": "TSLA", "cancelled": 1, "position": 0.0, "filled_sz": 0.0})
    _live(s, "entry", f"{F}|INTC", {"mode": "live", "coin": "INTC", "name": "INTC", "side": "买", "notional_usd": 60.0,
                                    "sent": True, "sz": 0.6, "px": 100.0, "exch_error": "Insufficient margin to place order"})
    _live(s, "entry", f"{F}|SP500", {"mode": "live", "coin": "SP500", "name": "SP500", "sent": False,
                                     "skipped": "风控否决：CPI 当天"})
    _live(s, "nolive", f"{P}|NVDA", {"reason": "账户可用保证金太低"})

    html = web.live_vs_shadow_card(s)
    assert TITLE in html
    for cell in ["<td>成交</td>", "<td>挂单没成交被撤</td>", "<td>被交易所拒：Insufficient margin to place order</td>",
                 "<td>没下单：风控否决：CPI 当天</td>", "<td>没下单：账户可用保证金太低</td>", "<td>没有记录</td>"]:
        assert cell in html, cell
    assert "2026-09-11" not in html                                   # 窗口外的周末不进表
    assert "影子 6 笔，真钱成交 1 笔" in html
    assert "差额主要来自「规模小」（-13.50 $）" in html
    assert "：没成交 1 笔、没下单 4 笔、规模小 1 笔、滑点 0 笔" in html
    assert "+18.00" in html and "-3.50" in html and "-21.50" in html  # 合计行：影子 / 真钱 / 差额


def test_no_files_shows_no_data_without_error(env):
    s, web = env
    html = web.live_vs_shadow_card(s)
    assert "还没有数据" in html and TITLE in html


def test_broken_live_file_shows_no_data(env):
    s, web = env
    _shadow(s, "2026-10-16|NVDA", 1, 100.0, 10.0)
    Ledger(s.data, "live").path.write_text('{"kind": "entry", 坏了\n{"kind": "cancel"}\n')   # 中间一行坏：Ledger.all() 会抛错
    html = web.live_vs_shadow_card(s)
    assert "还没有数据" in html and TITLE in html


def test_broken_experiments_file_shows_no_data(env):
    s, web = env
    Ledger(s.data).path.write_text("坏\n坏\n")
    assert "还没有数据" in web.live_vs_shadow_card(s)
