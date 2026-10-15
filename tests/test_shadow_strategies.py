"""影子策略 follow50 / fade50 / regime（10/15 起每个周末都记）：方向、门槛、regime 默认与切换、只用本合约过去的周末、
outcome 给每条影子算全额手续费盈亏、看板"影子策略对照"。"""
import shutil
from datetime import date
from pathlib import Path

import pytest

from desk import config, evaluate, jobs, model
from desk.clock import weekend_for_friday
from desk.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
FEES = {"maker_bps": 3.0, "taker_bps": 9.0}


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    return config.load(tmp_path)


def feats(dev, news=False):
    return {"ok": True, "dev_bps": dev, "news_weekend": news, "spread_bps": 2.0, "dec_mid": 100 * (1 + dev / 1e4),
            "best_bid": 100 * (1 + dev / 1e4) - 0.01, "best_ask": 100 * (1 + dev / 1e4) + 0.01, "wick_ratio": 1.0,
            "dislocated": False, "funding_avg": 0.0, "hold_hours": 0.3, "bid_depth_usd": 1e6, "ask_depth_usd": 1e6,
            "max_abs_dev_bps": abs(dev)}


@pytest.mark.parametrize("dev,f50,d50,reg", [(80, 1, -1, 1), (-80, -1, 1, -1), (30, 0, 0, 1), (-30, 0, 0, -1),
                                             (51, 1, -1, 1), (50, 0, 0, 1), (5, 0, 0, 0)])
def test_follow50_fade50_regime_sides(s, dev, f50, d50, reg):
    sh = model.shadow_actions(feats(dev), FEES, s)        # 没有历史 → regime 默认顺势
    assert sh["follow50"]["side"] == f50 and sh["fade50"]["side"] == d50
    assert sh["regime"]["side"] == reg and sh["regime"]["regime"]["pick"] == "follow"
    assert "follow" not in sh                              # 安静周末没有"信息周末顺势"
    if f50:
        assert sh["follow50"]["notional_usd"] > 0 and sh["follow50"]["entry_px"]
    else:
        assert "≤ 50" in sh["follow50"]["reason"]


def test_news_weekend_keeps_original_follow_shadow(s):
    sh = model.shadow_actions(feats(-70, news=True), FEES, s)
    assert sh["follow"]["side"] == -1 and set(sh) == {"follow", "follow50", "fade50", "regime", "v2", "fade50_stocks"}


def test_primary_action_unchanged_by_shadows(s):
    f = feats(80)
    before = model.decide_action(f, {"pred_bps": 0.0}, FEES, s)
    model.shadow_actions(f, FEES, s, [{"dev_bps": 50, "move_bps": -30}] * 6)
    assert model.decide_action(f, {"pred_bps": 0.0}, FEES, s) == before


def test_regime_pick_default_switch_and_only_last_k():
    assert model.regime_pick([], 6)["pick"] == "follow"
    five = [{"dev_bps": 50, "move_bps": -30}] * 5
    assert model.regime_pick(five, 6)["pick"] == "follow" and model.regime_pick(five, 6)["n"] == 5   # 不足 K → 默认顺势
    six = five + [{"dev_bps": -40, "move_bps": 10}]       # 都是反向赚（偏离被抹回）
    rp = model.regime_pick(six, 6)
    assert rp["pick"] == "fade" and rp["signal_bps"] == pytest.approx(-160)
    # 只看最近 K 个：很早以前顺势大赚不影响
    old_win = [{"dev_bps": 50, "move_bps": 1000}] * 3
    assert model.regime_pick(old_win + six, 6)["pick"] == "fade"
    sh = model.shadow_actions(feats(80), FEES, {"risk": config.load(ROOT)["risk"], "costs": config.load(ROOT)["costs"]}, six)
    assert sh["regime"]["side"] == -1 and sh["regime"]["direction"] == "fade"


def _rec(led, wid, dev, dec_mid, reopen, ok=True):
    led.append("lock", wid, {"features": {"ok": ok, "dev_bps": dev, "dec_mid": dec_mid, "fri_close": 100.0},
                             "action": {"side": 0}, "prediction": {"pred_bps": 0}})
    led.append("outcome", wid, {"ok": True, "reopen_px": reopen, "y_bps": (reopen / 100 - 1) * 1e4})


def test_regime_history_is_this_instrument_strictly_before_this_weekend(s):
    led = Ledger(s.data)
    _rec(led, "2026-09-25|NVDA", 50, 100.5, 100.2)     # move = (100.2/100.5 − 1)·1e4 ≈ −29.9
    _rec(led, "2026-10-02|NVDA", -40, 99.6, 99.9)
    _rec(led, "2026-10-02|TSLA", 90, 100.9, 101.5)      # 别的合约：不算
    _rec(led, "2026-10-09|NVDA", 10, 100.1, 100.0)      # 本周末（已有结果也不许用）
    _rec(led, "2026-10-16|NVDA", 10, 100.1, 100.0)      # 未来
    _rec(led, "2026-09-18|NVDA", 10, 100.1, 100.0, ok=False)   # 特征不全：跳过
    inst = s.inst("NVDA")
    h = jobs.regime_history(s, led, weekend_for_friday(date(2026, 10, 9), s, inst))
    assert [x["weekend"] for x in h] == ["2026-09-25|NVDA", "2026-10-02|NVDA"]
    assert h[0]["move_bps"] == pytest.approx((100.2 / 100.5 - 1) * 1e4)


def _official(wid, side, pnl, shadow):
    return {"weekend": wid, "lock": {"void": False, "features": {"ok": True}, "action": {"side": side}},
            "outcome": {"ok": True, "pnl_net_fullfee_usd": pnl, "direction_hit": pnl > 0 if side else None, "shadow": shadow}}


def test_shadow_table_and_scorecard_for_new_strategies(s):
    recs = [_official("2026-10-02|NVDA", -1, 5.0, {"follow50": {"side": 1, "pnl_net_fullfee_usd": -8.0, "pnl_net_usd": -7.0, "direction_hit": False},
                                                  "fade50": {"side": -1, "pnl_net_fullfee_usd": 5.0, "pnl_net_usd": 6.0, "direction_hit": True},
                                                  "regime": {"side": 1, "pnl_net_fullfee_usd": -8.0, "pnl_net_usd": -7.0, "direction_hit": False}}),
            _official("2026-10-02|TSLA", 0, 0.0, {"follow50": {"side": 0, "pnl_net_fullfee_usd": 0.0},
                                                 "fade50": {"side": 0, "pnl_net_fullfee_usd": 0.0},
                                                 "regime": {"side": -1, "pnl_net_fullfee_usd": 3.0, "pnl_net_usd": 3.5, "direction_hit": True}}),
            _official("2026-10-09|NVDA", 1, -2.0, {})]                     # 旧记录：没有新影子
    rows = {r["strategy"]: r for r in evaluate.shadow_table(recs, s)}
    assert list(rows) == ["primary", "follow", "follow50", "fade50", "regime", "v2", "fade50_stocks"]
    assert rows["primary"]["n_trades"] == 2 and rows["primary"]["pnl_net_fullfee_usd_total"] == pytest.approx(3.0)
    assert rows["primary"]["n_weekends"] == 2 and rows["primary"]["hit_rate"] == pytest.approx(0.5)
    assert rows["follow50"]["n_trades"] == 1 and rows["follow50"]["pnl_net_fullfee_usd_total"] == pytest.approx(-8.0)
    assert rows["regime"]["n_trades"] == 2 and rows["regime"]["n_weekends"] == 1 and rows["regime"]["hit_rate"] == pytest.approx(0.5)
    assert rows["follow"]["n_trades"] == 0
    sc = evaluate.shadow_scorecard(recs, s, "fade50")
    assert sc["label"] == model.SHADOW_LABELS["fade50"] and "fade50" not in sc["verdict"] and "1 个日历周末" in sc["verdict"]
    assert "信息周末样本" in evaluate.shadow_scorecard(recs, s, "follow")["verdict"]   # 原来的顺势文案不变


from tests.test_web import env  # noqa: E402,F401  —— 复用网页测试的登录环境


def test_overview_shows_shadow_comparison_table(env):  # noqa: F811
    from fastapi.testclient import TestClient
    s, web = env
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    r = c.get("/")
    assert r.status_code == 200 and "影子策略对照" in r.text
    for lab in ("主策略（实际 action）", model.SHADOW_LABELS["follow50"], model.SHADOW_LABELS["fade50"], model.SHADOW_LABELS["regime"],
                model.SHADOW_LABELS["v2"], model.SHADOW_LABELS["fade50_stocks"], "当前主策略："):
        import html
        assert html.escape(lab) in r.text, lab
