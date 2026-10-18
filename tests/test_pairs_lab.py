"""跨 dex 配对实验室（desk/pairs_lab.py）：配对发现与门槛、对齐、成本与资金费符号、不偷看、腿差压力、杠杆 / bootstrap、离线端到端。
全部是确定性假数据，不是真行情。"""
import json
import math
import shutil
from pathlib import Path

import pytest

from desk import backtest_lib as bl
from desk import config
from desk import pairs_lab as pl
from desk import universe as uv

ROOT = Path(__file__).resolve().parent.parent
IV = bl.INTERVAL_MS["5m"]
T0 = 1_760_000_000_000 // pl.DAY_MS * pl.DAY_MS   # 对齐到 UTC 0 点


@pytest.fixture
def s(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    return config.load(tmp_path)


def series_from(spreads_bps, base=100.0):
    """B 恒为 base，A = base·(1 + s/1e4)。"""
    return [(T0 + (i + 1) * IV, base * (1 + x / 1e4), base) for i, x in enumerate(spreads_bps)]


def oscillating(days, amp=80.0, period_bars=24):
    """每 period_bars 根出现一次 +amp 的价差、之后归零：同底层可收敛的理想情形。"""
    n = days * 288
    return [amp if (i % period_bars) in (0, 1) else 0.0 for i in range(n)]


# ---------------------------------------------------------------- 发现
def test_discover_pairs_groups_by_underlying_and_flags_proxy():
    g = dict(uv.DEFAULT_GATE)
    pairs = pl.discover_pairs(pl.fixture_raw(), g)
    by = {(p["a"], p["b"]): p for p in pairs}
    assert by[("io:SNDK", "xyz:SNDK")]["kind"] == "same"
    assert by[("io:NBIS", "xyz:NBIS")]["kind"] == "same"
    assert by[("mkts:US500", "xyz:SP500")]["kind"] == "proxy"
    assert by[("mkts:USTECH", "xyz:XYZ100")]["kind"] == "proxy"
    assert by[("mkts:US500", "xyz:SP500")]["underlying"] == "SP500"
    # 薄的一条腿 → 整对不过
    ewy = by[("io:EWY", "xyz:EWY")]
    assert not ewy["gate_ok"] and all(r.startswith("io:EWY") for r in ewy["gate_reasons"])
    assert by[("io:SNDK", "xyz:SNDK")]["gate_ok"]
    # 单 dex 的名字和主 dex（加密）不成对
    assert not any("AAPL" in p["a"] or "BTC" in p["a"] for p in pairs)


def test_canonical_aliases():
    assert pl.canonical("US500") == ("SP500", True)
    assert pl.canonical("ustech") == ("XYZ100", True)
    assert pl.canonical("SNDK") == ("SNDK", False)


# ---------------------------------------------------------------- 对齐
def test_align_requires_both_legs_traded():
    ca = [{"t": 0, "c": 1, "v": 1}, {"t": IV, "c": 2, "v": 0}, {"t": 2 * IV, "c": 3, "v": 1}]
    cb = [{"t": 0, "c": 1, "v": 1}, {"t": IV, "c": 2, "v": 5}, {"t": 3 * IV, "c": 3, "v": 1}]
    assert pl.align(ca, cb, IV) == [(IV, 1.0, 1.0)]   # 价格时刻 = K 线收盘


def test_proxy_spread_demeaned_with_strictly_past_window():
    ser = [(T0 + i * IV, 100.0 * 1.01, 100.0) for i in range(400)]   # 恒定 100 bps 基差
    sp = pl.spread_series(ser, "proxy", IV)
    win = int(24 * 3_600_000 // IV)
    assert all(x is None for x in sp[:win])
    assert all(abs(x) < 1e-6 for x in sp[win:])
    # 改当前值只影响当前点，不影响窗口均值（严格之前）
    ser2 = ser[:]
    ser2[win] = (ser[win][0], 100.0 * 1.02, 100.0)
    sp2 = pl.spread_series(ser2, "proxy", IV)
    assert sp2[win] == pytest.approx(math.log(1.02 / 1.01) * 1e4)


# ---------------------------------------------------------------- 成本、方向、资金费
def test_round_trip_cost_four_fills():
    assert pl.round_trip_cost(9.0, 2.0, 3.0) == pytest.approx(2 * 11 + 2 * 12)
    assert pl.round_trip_cost(3.0, 0, 0) == 12.0


def test_simulate_shorts_rich_leg_fills_next_bar_and_converges():
    sp = [0, 0, 100, 100, 100, 0, 0, 0]
    ser = series_from(sp)
    tr = pl.simulate(ser, pl.spread_series(ser, "same", IV), thr=50, exit_mode="conv0", cost=36, fa=[], fb=[])
    assert len(tr) == 1
    t = tr[0]
    assert t["side_a"] == -1                       # A 贵 → 空 A
    assert t["t_signal"] == ser[2][0] and t["t_entry"] == ser[3][0]   # 下一根成交
    # 进场价 = 第 3 根（仍 +100），信号在第 5 根（0）→ 第 6 根成交：空 A 赚 ≈ 100 bps
    assert t["gross_bps"] == pytest.approx(-(1.0 / 1.01 - 1) * 1e4)
    assert t["net_bps"] == pytest.approx(t["gross_bps"] - 36)


def test_funding_sign_convention():
    """正费率：多头付、空头收。空 A（费率 +1bp/h）+ 多 B（费率 0）持 3 小时 → +3 bps carry。"""
    assert bl.carry_bps(-1, 0.0003) == pytest.approx(3.0)
    assert bl.carry_bps(+1, 0.0003) == pytest.approx(-3.0)
    sp = [0] + [100] * 40 + [0] * 5
    ser = series_from(sp)
    fa = [{"t": T0 + h * 3_600_000, "rate": 0.0001} for h in range(10)]
    tr = pl.simulate(ser, pl.spread_series(ser, "same", IV), 50, "conv0", 0.0, fa, [])
    t = tr[0]
    hours = sum(1 for r in fa if t["t_entry"] < r["t"] <= t["t_exit"])
    assert hours >= 2 and t["carry_bps"] == pytest.approx(hours * 1.0)


def test_time_stop_and_half_exit():
    sp = [0, 100] + [100] * 400
    ser = series_from(sp)
    spr = pl.spread_series(ser, "same", IV)
    t = pl.simulate(ser, spr, 50, "conv0", 0, [], [])[0]
    assert t["reason"] == "time" and t["t_exit"] - t["t_entry"] >= 24 * 3_600_000
    sp = [0, 100, 100, 40, 40, 0]
    ser = series_from(sp)
    t = pl.simulate(ser, pl.spread_series(ser, "same", IV), 50, "half", 0, [], [])[0]
    assert t["reason"] == "half"


def test_legging_stress_second_leg_one_bar_late():
    sp = [0, 100, 0, 0, 0, 0]
    ser = series_from(sp)
    spr = pl.spread_series(ser, "same", IV)
    base = pl.simulate(ser, spr, 50, "conv0", 0, [], [])
    lag = pl.simulate(ser, spr, 50, "conv0", 0, [], [], lag_b=1)
    # 价差只持续 1 根：下一根成交时已经收敛 → 基线 0；B 只是常数，腿差也 0。A 的价差那一根已错过。
    assert base[0]["gross_bps"] == pytest.approx(0.0) and lag[0]["t_entry"] == base[0]["t_entry"]
    # B 在动的情形：B 晚一根成交价格不同
    ser2 = [(t, pa, pb * (1 + 0.001 * i)) for i, (t, pa, pb) in enumerate(ser)]
    l2 = pl.simulate(ser2, pl.spread_series(ser2, "same", IV), 50, "conv0", 0, [], [], lag_b=1)
    b2 = pl.simulate(ser2, pl.spread_series(ser2, "same", IV), 50, "conv0", 0, [], [])
    assert l2[0]["gross_bps"] != pytest.approx(b2[0]["gross_bps"])


# ---------------------------------------------------------------- 走前不偷看
def test_walk_forward_does_not_peek():
    days = 12
    sp = oscillating(days)
    ser = series_from(sp)
    spr = pl.spread_series(ser, "same", IV)
    dlist = sorted({t // pl.DAY_MS for t, _, _ in ser})
    by_v = pl.run_variants(ser, spr, 36.0, [], [])
    _, picks = pl.walk_forward(by_v, dlist, 5)
    # 改第 10 天之后的数据（价差变成持续不收敛），之前的选择不能变
    sp2 = sp[:10 * 288] + [80.0] * (len(sp) - 10 * 288)
    ser2 = series_from(sp2)
    by_v2 = pl.run_variants(ser2, pl.spread_series(ser2, "same", IV), 36.0, [], [])
    _, picks2 = pl.walk_forward(by_v2, dlist, 5)
    for d in dlist[5:10]:
        assert picks[d] == picks2[d]
    # 预热期内不交易；预热后有正收益的变体被选上
    assert all(d not in picks for d in dlist[:5])
    assert any(v for v in picks.values())


def test_walk_forward_ignores_trades_still_open():
    """在当天 0 点前开、0 点后才平的交易，不能参与当天的挑选。"""
    tr = {"v": [{"t_entry": 0, "t_exit": 2 * pl.DAY_MS + 1, "day": 0, "net_bps": 100.0, "net_usd": 10.0}]}
    _, picks = pl.walk_forward(tr, [0, 1, 2, 3], 1)
    assert picks[1] is None and picks[2] is None and picks[3] == "v"


# ---------------------------------------------------------------- 统计
def test_weekly_and_drawdown():
    tr = [{"t_exit": 1, "net_usd": 5.0}, {"t_exit": 2, "net_usd": -8.0}, {"t_exit": pl.WEEK_MS + 3, "net_usd": 2.0}]
    assert pl.weekly(tr, 0, 2 * pl.WEEK_MS + 10) == [-3.0, 2.0]
    assert pl.max_drawdown(tr) == -8.0


def test_leverage_caps_drawdown_and_bootstrap():
    tr = [{"t_exit": k * pl.WEEK_MS + 1, "net_usd": x, "net_bps": x * 10, "carry_bps": 0.0}
          for k, x in enumerate([10, -20, 15, 12, -5, 9, 11, 8])]
    st = pl.pair_stats(tr, 0, 8 * pl.WEEK_MS + 5, 1000.0, lev_cap=50.0)
    assert st["max_dd_usd"] == -20.0
    assert st["leverage"] == pytest.approx(0.20 / 0.02)       # 回撤 2% → 10x 恰好 20%
    assert st["week_ret_at_lev"] == pytest.approx(st["week_mean_usd"] / 1000 * 10)
    assert 0.0 <= st["p_minus50_52w"] <= 1.0
    st2 = pl.pair_stats(tr, 0, 8 * pl.WEEK_MS + 5, 1000.0, lev_cap=3.0)
    assert st2["leverage"] == 3.0
    # 确定性：同种子同结果；全负 → 高概率
    assert pl.bootstrap_ruin([-0.2] * 5) == 1.0
    assert pl.bootstrap_ruin([0.01, 0.02, -0.01, 0.0]) == pl.bootstrap_ruin([0.01, 0.02, -0.01, 0.0]) == 0.0
    assert pl.bootstrap_ruin([0.1, 0.1]) is None


def test_negative_edge_gets_zero_leverage_and_no_capacity():
    tr = [{"t_exit": k * pl.WEEK_MS + 1, "net_usd": -1.0, "net_bps": -10.0, "carry_bps": 0.0} for k in range(5)]
    st = pl.pair_stats(tr, 0, 5 * pl.WEEK_MS + 5, 1000.0, 10.0)
    assert st["leverage"] == 0.0
    assert pl.capacity_usd(-1.0, 2, 2, 1e6) is None
    assert pl.capacity_usd(16.0, 2, 2, 1e6) == pytest.approx(min(10_000 * 9, 1e4))


# ---------------------------------------------------------------- 端到端（离线：缓存里的假 K 线）
def _write_fake_cache(s, coin_a, coin_b, sp, base=100.0):
    n = len(sp)
    ca = [{"t": T0 + i * IV, "o": 1, "h": 1, "l": 1, "c": base * (1 + sp[i] / 1e4), "v": 1.0} for i in range(n)]
    cb = [{"t": T0 + i * IV, "o": 1, "h": 1, "l": 1, "c": base, "v": 1.0} for i in range(n)]
    bl.save_cache(s, coin_a, "5m", ca, merge=False)
    bl.save_cache(s, coin_b, "5m", cb, merge=False)
    bl.save_funding_cache(s, coin_a, [{"t": T0 + h * 3_600_000, "rate": 0.00002} for h in range(n // 12)])
    bl.save_funding_cache(s, coin_b, [{"t": T0 + h * 3_600_000, "rate": 0.0} for h in range(n // 12)])


def test_end_to_end_offline_report(s, tmp_path):
    _write_fake_cache(s, "io:SNDK", "xyz:SNDK", oscillating(21, amp=120.0))
    md, res = pl.run(s, pl.fixture_raw(), "fixture", days=60, interval="5m", warmup=5, fetch=False,
                     out=tmp_path / "pairs.md")
    sndk = next(p for p in res["pairs"] if p["a"] == "io:SNDK")
    r = sndk["result"]
    assert r["ok"] and r["taker"]["n"] > 0 and r["taker"]["avg_net_bps"] > 0
    assert r["maker"]["cost_bps"] < r["taker"]["cost_bps"]
    assert "legging" in r
    fd = r["funding_diff"]                              # A 费率 2e-5/h，B 0 → +4.8 bps/天
    assert fd["hours"] > 0 and fd["mean_bps_per_day"] == pytest.approx(4.8)
    assert r["taker"]["avg_carry_bps"] >= 0             # 价差 > 0 → 空 A；A 正费率 → 空头收
    nbis = next(p for p in res["pairs"] if p["a"] == "io:NBIS")
    assert not nbis["result"]["ok"]                     # 没缓存 → 说明原因，不崩
    assert "代理对" in md and "诚实检查" in md and "没过流动性门槛" in md and "io:EWY" in md
    js = json.loads((tmp_path / "pairs.json").read_text())
    assert "trades_taker" not in js["pairs"][0].get("result", {})


def test_load_raw_unreachable_prints_hint(s, monkeypatch, capsys):
    monkeypatch.setattr(bl, "hyperliquid_reachable", lambda timeout=8.0: (False, "ProxyError: 403"))
    raw, _, fetch = pl.load_raw(s, None, True)
    assert raw is None and not fetch
    assert "python3 -m desk.pairs_lab --days 60" in capsys.readouterr().err
    (Path(s.data) / "universe_raw.json").write_text(json.dumps(pl.fixture_raw()))
    raw, src, fetch = pl.load_raw(s, None, True)
    assert raw and "本地缓存" in src and fetch is False
