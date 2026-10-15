"""按合约平分本金的分母 = 主策略实际会出手的合约数（model.tradable_instruments）：v2 只算 us_equity，v1 = 全部合约。"""
import shutil
from datetime import date
from pathlib import Path

import pytest

from desk import autotrade, config, model, sim
from desk.clock import weekend_for_friday
from tests.conftest import as_v1

ROOT = Path(__file__).resolve().parent.parent
ADDED = ["HOOD", "PLTR", "MSFT", "MU", "AMZN", "CRWV", "SNDK", "CRCL", "META", "GOOGL", "AAPL", "MSTR", "AMD", "CBRS"]


def _load(tmp_path, monkeypatch, v1: bool):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    if v1:
        as_v1(tmp_path / "config.toml")
    monkeypatch.setenv("DESK_ROOT", str(tmp_path))
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    return config.load(tmp_path)


def _lock(name):
    return {"action": {"side": -1, "reason": "v2 反向", "entry_px": 100.0},
            "features": {"best_bid": 99.9, "best_ask": 100.1, "bid_depth_usd": 1e7, "ask_depth_usd": 1e7, "dec_mid": 100.0},
            "void": False, "void_reasons": [], "name": name, "coin": f"xyz:{name}"}


@pytest.mark.parametrize("v1, n_tradable", [(False, 17), (True, 21)])
def test_capital_split_by_tradable_instruments(tmp_path, monkeypatch, v1, n_tradable):
    s = _load(tmp_path, monkeypatch, v1)
    assert len(s.instruments) == 21
    names = [i["name"] for i in model.tradable_instruments(s)]
    assert len(names) == n_tradable
    if not v1:
        assert names == ["NVDA", "TSLA", "INTC"] + ADDED            # v2：只有 us_equity，配置顺序不变
    else:
        assert names == [i["name"] for i in s.instruments]          # v1：全部合约，行为不变

    capital = 1700.0
    autotrade.put(s, mode="shadow", capital_usd=capital)
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("NVDA"))
    rec = autotrade.entry(s, w, _lock("NVDA"))
    assert rec["sent"] is False
    assert rec["notional_usd"] == pytest.approx(round(capital / n_tradable, 2))

    r = sim.run(s, capital=capital, leverage=2, mode="maker", source="history")
    assert r["n_instruments"] == n_tradable
    assert r["per_instrument_notional"] == pytest.approx(capital * 2 / n_tradable)


def test_v2_joint_cap_uses_tradable_count(tmp_path, monkeypatch):
    """联合首损预算也按会出手的合约数均摊：worst 很大时 notional = joint_first_loss_frac × 本金 / 17 / worst。"""
    s = _load(tmp_path, monkeypatch, v1=False)
    autotrade.put(s, mode="shadow", capital_usd=1700)
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("MU"))
    lock = _lock("MU")
    lock["features"]["max_abs_dev_bps"] = 5000.0                    # 50%：联合预算比平分额更紧
    rec = autotrade.entry(s, w, lock)
    frac = s["risk"].get("joint_first_loss_frac", 0.25)
    assert rec["notional_usd"] == pytest.approx(round(frac * 1700 / 17 / 0.5, 2))
    assert rec["notional_usd"] < 1700 / 17
