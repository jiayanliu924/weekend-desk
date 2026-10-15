"""策略 v2（2026-10-15）：品种族门控、|偏离|>50 门槛、全额手续费出手判断、4 周末停手（含 <4 周末照做）、
停手历史从账本正式记录按 v2 规则重算、影子 v2 / fade50_stocks、实验室 v2 不偷看、看板一行、配置默认。"""
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.conftest import as_v1
from desk import config, jobs, model
from desk import strategy_lab as sl
from desk.clock import weekend_for_friday
from desk.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
DISC = {"maker_bps": 0.3, "taker_bps": 0.9}     # growthMode 打折后的费率（v1 用它，v2 不用）
STOP_REASON = "v2：近 4 个周末该规则累计为负，停手一周"


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    return config.load(tmp_path)


@pytest.fixture
def s1(tmp_path):
    d = tmp_path / "v1"
    d.mkdir()
    shutil.copy(ROOT / "config.toml", d / "config.toml")
    as_v1(d / "config.toml")
    return config.load(d)


def feats(dev, name="NVDA", news=False, spread=2.0):
    mid = 100 * (1 + dev / 1e4)
    return {"ok": True, "name": name, "coin": f"xyz:{name}", "dev_bps": dev, "news_weekend": news, "spread_bps": spread,
            "dec_mid": mid, "best_bid": mid - 0.01, "best_ask": mid + 0.01, "wick_ratio": 1.0, "dislocated": False,
            "funding_avg": 0.0, "hold_hours": 0.3, "bid_depth_usd": 1e6, "ask_depth_usd": 1e6, "max_abs_dev_bps": abs(dev)}


def hist(fris, dev, move, name="NVDA"):
    return [{"weekend": f"{f}|{name}", "name": name, "dev_bps": dev, "move_bps": move} for f in fris]


def fridays(n, start=date(2026, 8, 7)):
    return [(start + timedelta(days=7 * i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------- 配置 / 品种族
def test_repo_default_is_v2_and_v1_still_available(s, s1):
    sc = model.strategy_cfg(config.load(ROOT))
    assert sc["version"] == "v2" and sc["families_enabled"] == ["us_equity"]
    assert sc["fade_threshold_bps"] == 50 and sc["stop_lookback_weekends"] == 4
    assert model.strategy_cfg({"costs": {}})["version"] == "v1"           # 没有 [strategy] 段 → 旧行为
    f = feats(80, "XYZ100")
    assert model.decide_action(f, {"pred_bps": 0.0}, DISC, s1) == model.decide_action_core(f, {"pred_bps": 0.0}, DISC, s1)
    assert model.decide_action(f, {"pred_bps": 0.0}, DISC, s1)["side"] == -1


def test_every_configured_instrument_maps_to_a_family(s):
    fam = {i["name"]: model.instrument_family(i["coin"]) for i in s.instruments}
    core = {"XYZ100": "us_index", "SP500": "us_index", "BRENTOIL": "metals_energy", "SMSN": "kr_equity",
            "NVDA": "us_equity", "TSLA": "us_equity", "INTC": "us_equity"}
    assert {k: fam[k] for k in core} == core
    # 2026-10-15 扩展的美股单票：全部必须认成 us_equity（按 coin、也按裸名），配置里不许有认不出的合约
    added = ["HOOD", "PLTR", "MSFT", "MU", "AMZN", "CRWV", "SNDK", "CRCL", "META", "GOOGL", "AAPL", "MSTR", "AMD", "CBRS"]
    assert set(fam) == set(core) | set(added)
    for n in added:
        assert fam[n] == "us_equity" and model.instrument_family(n) == "us_equity", n
    assert model.instrument_family("NVDA") == "us_equity" and model.instrument_family(None) == "unknown"


# ---------------------------------------------------------------- 门控 / 门槛 / 全额成本
@pytest.mark.parametrize("name", ["XYZ100", "SP500", "BRENTOIL", "SMSN"])
def test_v2_stands_aside_outside_enabled_families(s, name):
    a = model.decide_action(feats(150, name), {"pred_bps": 0.0}, DISC, s)
    assert a["side"] == 0 and a["reason"] == "v2：该品种族样本外未通过，不出手"


@pytest.mark.parametrize("dev,side", [(50, 0), (50.5, -1), (-50, 0), (-80, 1), (120, -1), (30, 0), (-49.9, 0)])
def test_v2_threshold_and_direction(s, dev, side):
    a = model.decide_action(feats(dev, "TSLA"), {"pred_bps": 0.0}, DISC, s)
    assert a["side"] == side
    if side:
        assert a["direction"] == "fade" and a["entry_px"] and a["notional_usd"] > 0 and a["reason"].startswith("v2：")


def test_v2_fades_even_on_news_weekend_and_ignores_prediction(s):
    a = model.decide_action(feats(90, "INTC", news=True), {"pred_bps": 500.0}, DISC, s)
    assert a["side"] == -1


def test_v2_decision_uses_full_fees_not_growth_discount(s, s1):
    f = feats(60, "NVDA", spread=2.0)
    a2 = model.decide_action(f, {"pred_bps": 0.0}, DISC, s)
    assert a2["threshold_bps"] == pytest.approx(3 + 9 + 1 + 2) and a2["fee_basis"] == "full"
    a1 = model.decide_action(f, {"pred_bps": 0.0}, DISC, s1)
    assert a1["threshold_bps"] == pytest.approx(0.3 + 0.9 + 1 + 2)
    # 点差很宽：全额成本 3+9+50+2 = 64 > 60 → v2 不做；v1 按打折费率 0.3+0.9+50+2 = 53.2 < 60 → 会做
    wide = feats(60, "NVDA", spread=100.0)
    assert model.decide_action(wide, {"pred_bps": 0.0}, DISC, s)["side"] == 0
    assert model.decide_action(wide, {"pred_bps": 0.0}, DISC, s1)["side"] == -1


def test_v2_still_vetoes_wicks(s):
    f = feats(80, "NVDA")
    f["dislocated"] = True
    a = model.decide_action(f, {"pred_bps": 0.0}, DISC, s)
    assert a["side"] == 0 and "插针" in a["reason"]


# ---------------------------------------------------------------- 停手
def test_stop_triggers_after_four_losing_weekends(s):
    fr = fridays(4)
    lose = hist(fr, 80, 10) + hist(fr, -70, -5, "TSLA")        # 反向：偏离 +80 做空，又涨 10 → 亏
    a = model.decide_action(feats(90, "NVDA"), {"pred_bps": 0.0}, DISC, s, v2_history=lose)
    assert a["side"] == 0 and a["reason"].startswith(STOP_REASON) and a["stop"]["stop"]
    assert a["stop"]["sum_bps"] == pytest.approx(4 * ((-10 - 14) + (-5 - 14)))   # 半点差缺省按 2 bps


def test_no_stop_with_fewer_than_four_weekends_of_history(s):
    lose = hist(fridays(3), 80, 200)                          # 3 个周末亏得很惨
    a = model.decide_action(feats(90, "NVDA"), {"pred_bps": 0.0}, DISC, s, v2_history=lose)
    assert a["side"] == -1 and a["stop"]["n"] == 3 and not a["stop"]["stop"]
    assert model.decide_action(feats(90, "NVDA"), {"pred_bps": 0.0}, DISC, s)["side"] == -1   # 没有历史 → 照做


def test_stop_uses_only_last_four_and_weekends_without_trades_count_as_zero(s):
    fr = fridays(6)
    h = hist(fr[:2], 80, 500) + hist(fr[2:], 80, 10)          # 早年大赚不救最近 4 周末
    assert model.decide_action(feats(90), {"pred_bps": 0.0}, DISC, s, v2_history=h)["side"] == 0
    quiet = hist(fr[2:5], 20, 300) + hist(fr[5:], 80, -40)     # 3 周末不出手（0）+ 1 周末赚 → 合计 > 0
    a = model.decide_action(feats(90), {"pred_bps": 0.0}, DISC, s, v2_history=quiet)
    assert a["side"] == -1 and a["stop"]["n"] == 4 and a["stop"]["sum_bps"] == pytest.approx(40 - 14)


def test_stop_recomputes_v2_rule_pnl_not_what_primary_did(s):
    """历史里是"顺势赚"的周末（主策略当时是不是做了无所谓）：按 v2 规则（反向）重算是亏 → 停手。"""
    h = hist(fridays(4), 80, 30)
    assert model.v2_weekend_pnls(h, s) == [(f, pytest.approx(-30 - 14)) for f in fridays(4)]
    assert model.decide_action(feats(90), {"pred_bps": 0.0}, DISC, s, v2_history=h)["side"] == 0


def _rec(led, wid, dev, dec_mid, reopen, void=False, ok=True, action_side=0):
    led.append("lock", wid, {"void": void, "features": {"ok": ok, "dev_bps": dev, "dec_mid": dec_mid, "fri_close": 100.0,
                                                        "spread_bps": 2.0, "dislocated": False, "wick_ratio": 1.0},
                             "action": {"side": action_side}, "prediction": {"pred_bps": 0}})
    led.append("outcome", wid, {"ok": True, "reopen_px": reopen, "y_bps": (reopen / 100 - 1) * 1e4})


def test_job_history_is_same_family_official_strictly_before(s):
    led = Ledger(s.data)
    _rec(led, "2026-09-18|NVDA", 80, 100.8, 100.9)
    _rec(led, "2026-09-25|TSLA", 80, 100.8, 100.9, action_side=-1)
    _rec(led, "2026-09-25|XYZ100", 80, 100.8, 100.0)          # 别的族：不算
    _rec(led, "2026-10-02|INTC", 80, 100.8, 100.9, void=True)  # 作废：不是正式记录
    _rec(led, "2026-10-02|NVDA", 80, 100.8, 100.9)
    _rec(led, "2026-10-09|TSLA", 80, 100.8, 100.0)            # 本周末：不许用
    _rec(led, "2026-10-16|NVDA", 80, 100.8, 100.0)            # 未来
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("NVDA"))
    hh = jobs.v2_history(s, led, w)                            # 测试目录里没有 K 线缓存 → 缺回补，只用账本
    h = hh["rows"]
    assert [x["weekend"] for x in h] == ["2026-09-18|NVDA", "2026-09-25|TSLA", "2026-10-02|NVDA"]
    assert {x["source"] for x in h} == {"ledger"} and hh["backfill_missing"] and hh["n_ledger_weekends"] == 3
    assert h[0]["move_bps"] == pytest.approx((100.9 / 100.8 - 1) * 1e4) and h[0]["spread_bps"] == 2.0
    a = model.decide_action(feats(90), {"pred_bps": 0.0}, DISC, s, v2_history=hh)
    assert a["side"] == -1 and not a["stop"]["stop"] and a["stop"]["backfill_missing"]       # 3 个周末 → 照做
    assert "停手判断只用了 3 个周末（缺回补数据）" in a["reason"]
    wx = weekend_for_friday(date(2026, 10, 9), s, s.inst("SP500"))
    assert [x["weekend"] for x in jobs.v2_history(s, led, wx)["rows"]] == ["2026-09-25|XYZ100"]


# ---------------------------------------------------------------- 影子
def test_v2_and_fade50_stocks_shadows_always_recorded(s, s1):
    lose = hist(fridays(4), 80, 10)
    for cfg in (s, s1):
        sh = model.shadow_actions(feats(90, "NVDA"), DISC, cfg, [], v2_history=lose)
        assert {"v2", "fade50_stocks", "follow50", "fade50", "regime"} <= set(sh)
        assert sh["v2"]["side"] == 0 and sh["v2"]["reason"].startswith(STOP_REASON)
        assert sh["fade50_stocks"]["side"] == -1 and sh["fade50_stocks"]["strategy"] == "fade50_stocks"
        assert sh["fade50_stocks"]["reason"].startswith(model.SHADOW_LABELS["fade50_stocks"])
        sx = model.shadow_actions(feats(90, "XYZ100"), DISC, cfg, [])
        assert sx["v2"]["side"] == 0 and sx["fade50_stocks"]["side"] == 0 and sx["fade50"]["side"] == -1


# ---------------------------------------------------------------- 实验室
def lrow(fri, name="NVDA", fam="美股单票", dev=80.0, move=10.0):
    return {"fri": fri, "name": name, "family": fam, "gran": "1h", "dev_bps": dev, "move_bps": move, "y_bps": dev + move,
            "news": None}


def test_lab_v2_gating_threshold_and_short_history():
    fr = fridays(3)
    rows = [lrow(f, dev=80, move=50) for f in fr] + [lrow(fr[0], "XYZ100", "美指", dev=200)] + [lrow(fr[2], "TSLA", dev=40)]
    dec = sl.decisions(rows, 14.0, 8)
    assert [dec[("v2", f, "NVDA")] for f in fr] == [-1, -1, -1]      # < 4 个周末 → 照做（即使在亏）
    assert dec[("v2", fr[0], "XYZ100")] == 0 and dec[("v2", fr[2], "TSLA")] == 0


def test_lab_v2_stop_does_not_peek_at_current_weekend():
    """前 4 个周末反向亏（move 顺着偏离 +10）→ 第 5 个周末停手。本周末结果再好（偷看会让合计转正）也必须停；
    反过来前 4 个周末赚 → 照做，本周末巨亏也不能让它停。"""
    fr = fridays(5)
    rows = [lrow(f, move=10) for f in fr[:4]] + [lrow(fr[4], move=-5000)]
    dec = sl.decisions(rows, 14.0, 8)
    assert dec[("v2", fr[4], "NVDA")] == 0 and dec[("fade50", fr[4], "NVDA")] == -1
    peek = sl.v2_state(rows, "美股单票", 14.0, sl.v2_params())          # 把本周末也算进去 → 最近 4 个合计 > 0
    assert not peek["stop"]
    rows2 = [lrow(f, move=-30) for f in fr[:4]] + [lrow(fr[4], move=5000)]
    assert sl.decisions(rows2, 14.0, 8)[("v2", fr[4], "NVDA")] == -1
    assert sl.v2_state(rows2, "美股单票", 14.0, sl.v2_params())["stop"]
    res = sl.evaluate_rows(rows, {"costs": {"maker_bps": 3.0, "taker_bps": 9.0, "edge_buffer_bps": 2.0}}, warmup=2)
    assert res["v2_states"][("美股单票", fr[4])]["stop"] and not res["v2_states"][("美股单票", fr[3])]["stop"]
    assert [t["fri"] for t in res["trades"]["v2"]] == fr[:4]
    sec = "\n".join(sl.v2_section_cn(res))
    assert "停手 1 个" in sec and "上线门槛" in sec and "$/周末" in sec


# ---------------------------------------------------------------- 看板
def test_strategy_line(s, s1):
    from desk import web
    assert web.strategy_line(s) == "当前主策略：v2（只做美股单票、偏离>50 反向、全额成本、近 4 周末为负就停）"
    assert web.strategy_line(s1).startswith("当前主策略：v1")


# ---------------------------------------------------------------- 端到端：job_lock / job_outcome 在 v2 下
def test_job_lock_under_v2_end_to_end(s):
    from tests.test_weekend import FRI, run_weekend, seed_market
    w = weekend_for_friday(FRI, s)                                  # 默认合约 XYZ100 = 美指
    seed_market(s, w, dev_bps=60, reopen_bps=5, with_news=False)
    lock, out = run_weekend(s, w)
    assert lock["action"]["side"] == 0 and lock["action"]["reason"] == "v2：该品种族样本外未通过，不出手"
    assert lock["action"]["strategy_version"] == "v2"
    assert lock["shadow"]["fade50"]["side"] == -1 and lock["shadow"]["v2"]["side"] == 0
    assert {"v2", "fade50_stocks"} <= set(out["shadow"]) and out["pnl_net_usd"] == 0.0


# ---------------------------------------------------------------- 停手历史 = 账本 + 回补补缺（和实验室一致）
def bfrow(fri, name, dev, move, gran="15m"):
    return {"fri": fri, "name": name, "dev_bps": dev, "move_bps": move, "spread_bps": None, "source": f"backfill_{gran}"}


def test_backfill_fills_gaps_and_ledger_wins_on_overlap(s):
    led = Ledger(s.data)
    _rec(led, "2026-09-25|NVDA", 80, 100.8, 100.9)            # 账本：反向亏（move ≈ +9.9）
    _rec(led, "2026-10-02|TSLA", 80, 100.8, 100.9)
    bf = [bfrow("2026-09-11", "NVDA", 80, 20), bfrow("2026-09-11", "INTC", -90, -10),   # 空档 → 用回补
          bfrow("2026-09-18", "TSLA", 80, 30, "1h"),
          bfrow("2026-09-25", "NVDA", 80, -500), bfrow("2026-09-25", "INTC", 80, -500),  # 和账本重叠 → 账本赢（整个周末）
          bfrow("2026-10-09", "NVDA", 80, -500)]                                         # 本周末 → 不许用
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("NVDA"))
    hh = jobs.v2_history(s, led, w, backfill_rows=bf)
    src = {(x["fri"], x["name"]): x["source"] for x in hh["rows"]}
    assert src == {("2026-09-11", "NVDA"): "backfill_15m", ("2026-09-11", "INTC"): "backfill_15m",
                   ("2026-09-18", "TSLA"): "backfill_1h", ("2026-09-25", "NVDA"): "ledger", ("2026-10-02", "TSLA"): "ledger"}
    assert not hh["backfill_missing"] and hh["n_ledger_weekends"] == 2 and hh["n_backfill_weekends"] == 2
    a = model.decide_action(feats(90), {"pred_bps": 0.0}, DISC, s, v2_history=hh)
    mv = (100.9 / 100.8 - 1) * 1e4
    exp = (-20 - 14) + (-10 - 14) + (-30 - 14) + 2 * (-mv - 13)   # 账本行有点差 2 → 半点差 1 → 成本 13
    st = a["stop"]
    assert st["sum_bps"] == pytest.approx(exp) and st["stop"] and a["side"] == 0 and a["reason"].startswith(STOP_REASON)
    assert [(x["fri"], x["source"]) for x in st["weekends"]] == [("2026-09-11", "backfill_15m"), ("2026-09-18", "backfill_1h"),
                                                                ("2026-09-25", "ledger"), ("2026-10-02", "ledger")]
    assert st["weekends"][0]["names"] == ["INTC", "NVDA"] and st["sources"] == ["backfill_15m", "backfill_1h", "ledger"]
    assert "停手判断只用了" not in a["reason"]
    # 族外合约直接不出手，不算停手
    assert "stop" not in model.decide_action(feats(90, "XYZ100"), {"pred_bps": 0.0}, DISC, s, v2_history=hh)


def test_stop_recorded_even_when_dev_below_threshold(s):
    hh = {"rows": [bfrow(f, "NVDA", 80, 10) for f in fridays(4)], "backfill_missing": False}
    a = model.decide_action(feats(20), {"pred_bps": 0.0}, DISC, s, v2_history=hh)
    assert a["side"] == 0 and a["stop"]["stop"] and len(a["stop"]["weekends"]) == 4


def test_live_stop_matches_lab_on_same_backfill(s):
    """磁盘上有 K 线缓存、账本为空：实盘停手历史（jobs.v2_history 读盘）和实验室 v2_state 每个周末给出同一个停/不停和同一个合计。"""
    import math
    from datetime import datetime, timezone
    from tests.test_strategy_lab import _write_cache
    UTC = timezone.utc
    now = datetime(2026, 10, 14, tzinfo=UTC)
    for i, nm in enumerate(("NVDA", "TSLA", "INTC")):
        fn = lambda k, i=i: 100.0 * (1 + 0.012 * math.sin(k / 3.6e6 / (7 + 3 * i)) + 0.004 * math.sin(k / 3.6e6 / 2.3))  # noqa: E731
        coin = s.inst(nm)["coin"]
        _write_cache(s, coin, "1h", datetime(2026, 6, 1, tzinfo=UTC), now, fn)
        _write_cache(s, coin, "15m", datetime(2026, 8, 23, tzinfo=UTC), now, fn)
    rows, _, _ = sl.build_rows(s, now, None, False, names=["NVDA", "TSLA", "INTC"])
    fris = sorted({r["fri"] for r in rows})
    assert len(fris) >= 10 and any(abs(r["dev_bps"]) > 50 for r in rows)
    led = Ledger(s.data)
    v2p = sl.v2_params(s)
    n_stop = 0
    for fri in fris[2:]:
        w = weekend_for_friday(date.fromisoformat(fri), s, s.inst("NVDA"))
        hh = jobs.v2_history(s, led, w)
        assert not hh["backfill_missing"] and {x["source"][:9] for x in hh["rows"]} == {"backfill_"}
        live = model.v2_stop_from_history(hh, s, 4)
        lab = sl.v2_state(sl._past(rows, fri), "美股单票", 14.0, v2p)
        assert live["stop"] == lab["stop"], fri
        assert (live["sum_bps"] is None and lab["sum_bps"] is None) or live["sum_bps"] == pytest.approx(lab["sum_bps"]), fri
        n_stop += live["stop"]
    assert 0 < n_stop < len(fris) - 2          # 夹具里停手和不停手都出现过，比较才有意义
