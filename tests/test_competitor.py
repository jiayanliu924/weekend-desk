"""借鉴竞品 Meridian/Keel 后新增的：稳健公允价/插针检测、资金费率 carry、首损+信心仓位、
全额手续费口径、按周末聚类的显著性，以及自动下单的脱节跳过与联合压测上限。"""
import shutil
from pathlib import Path

import pytest

from desk import config, evaluate, mark, model

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    return config.load(tmp_path)


# ---------------- mark.py ----------------
def test_robust_price_normal_and_wick():
    # 中间价和标记价一致 → 不脱节，稳健价就是中间价
    d = mark.deviation_set(30000.0, 30180.0, 30180.0, 30000.0, dislocation_bps=25.0)
    assert not d["dislocated"] and abs(d["fair_dev_bps"] - 60) < 1e-6
    # 中间价被插针拉到比标记价高很多 → 判为脱节，稳健价改用标记价
    d2 = mark.deviation_set(30000.0, 33000.0, 30180.0, 30000.0, dislocation_bps=25.0)
    assert d2["dislocated"] and d2["robust_px"] == 30180.0
    assert abs(d2["dev_mid_bps"] - 1000) < 1 and abs(d2["fair_dev_bps"] - 60) < 1


def test_funding_carry_sign():
    # 空头（side<0）遇到正资金费率 → 收钱（carry 为正）
    assert mark.funding_carry_bps(-1, 0.0000625, 48) > 0
    # 多头遇到正资金费率 → 付钱（carry 为负）
    assert mark.funding_carry_bps(1, 0.0000625, 48) < 0
    assert mark.annualized_funding_pct(0.0000625) == pytest.approx(0.0000625 * 24 * 365 * 100)


# ---------------- model.decide_action ----------------
def _feats(**kw):
    f = {"dev_bps": 60.0, "spread_bps": 1.0, "bid_depth_usd": 1e6, "ask_depth_usd": 1e6,
         "best_bid": 29990.0, "best_ask": 30010.0, "dec_mid": 30000.0, "news_weekend": False,
         "funding_avg": 0.0, "hold_hours": 48.0, "max_abs_dev_bps": 60.0, "wick_ratio": 1.0,
         "dislocated": False}
    f.update(kw)
    return f


def test_fade_trades_with_first_loss_sizing(s):
    fees = {"maker_bps": 3.0, "taker_bps": 9.0}
    a = model.decide_action(_feats(), {"pred_bps": 0.0}, fees, s)
    assert a["side"] == -1                      # 噪音周末 → 反向卖
    assert a["conf"] == pytest.approx(2.0)      # 信号远超门槛 → 放大到上限
    # 首损上限：0.02 * 2000 / (500/1e4) = 800，仓位不超过它
    assert a["notional_usd"] <= 800 + 1e-6 and a["first_loss_cap_usd"] == pytest.approx(800)


def test_dislocation_vetoes_trade(s):
    fees = {"maker_bps": 3.0, "taker_bps": 9.0}
    a = model.decide_action(_feats(dislocated=True), {"pred_bps": 0.0}, fees, s)
    assert a["side"] == 0 and "插针" in a["reason"]
    b = model.decide_action(_feats(wick_ratio=12.0), {"pred_bps": 0.0}, fees, s)
    assert b["side"] == 0 and "插针" in b["reason"]


def test_high_vol_instrument_sized_smaller(s):
    """同样的信号，周末见过的最大偏离越大（越容易暴动）→ 首损上限越小 → 下得越小。"""
    fees = {"maker_bps": 3.0, "taker_bps": 9.0}
    calm = model.decide_action(_feats(max_abs_dev_bps=300.0), {"pred_bps": 0.0}, fees, s)
    wild = model.decide_action(_feats(max_abs_dev_bps=1800.0), {"pred_bps": 0.0}, fees, s)
    assert wild["first_loss_cap_usd"] < calm["first_loss_cap_usd"]
    assert wild["notional_usd"] < calm["notional_usd"]


# ---------------- evaluate 全额手续费 + 周末聚类 ----------------
def _rec(wk, side=-1, pnl=0.5, full=0.1):
    return {"weekend": wk, "lock": {"void": False, "features": {"ok": True}, "action": {"side": side}},
            "outcome": {"ok": True, "err_model_bps": 5.0, "err_A_bps": 20.0, "err_B_bps": 15.0,
                        "pnl_net_usd": pnl, "pnl_net_taker_usd": pnl - 0.2,
                        "pnl_net_fullfee_usd": full, "direction_hit": True}}


def test_scorecard_fullfee_and_weekend_cluster(s):
    recs = [_rec("2026-06-01|XYZ100"), _rec("2026-06-01|SP500"),
            _rec("2026-06-08|XYZ100"), _rec("2026-06-08|SP500")]
    sc = evaluate.scorecard(recs, s)
    assert sc["n_official"] == 4 and sc["n_weekends"] == 2      # 4 条记录，但只有 2 个日历周末
    assert sc["pnl_net_fullfee_usd_total"] == pytest.approx(0.4)
    assert "p_vs_A_weekend" in sc and "p_vs_B_weekend" in sc
