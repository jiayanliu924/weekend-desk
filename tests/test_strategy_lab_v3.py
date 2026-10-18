"""策略实验室 v3（组合层面）：仓位、强度、β 对冲 / 残差信号、挂单平仓、走前挑选、风险统计；每一项的数学 + 不偷看。"""
import math
import random
from datetime import date, datetime, timezone

import pytest

from desk import strategy_lab as sl
from tests.test_strategy_lab import _write_cache, fridays, s  # noqa: F401  (s 是夹具)

UTC = timezone.utc
CTX = {"fee": 14.0, "maker_fee": 6.0, "thr": 50.0, "families": {"美股单票"}, "idx": "XYZ100", "n_universe": 17}
COSTS = {"costs": {"maker_bps": 3.0, "taker_bps": 9.0, "edge_buffer_bps": 2.0}}


def r(fri, name="NVDA", dev=60.0, move=20.0, y=None, fam=None, **kw):
    fam = fam or ("美指" if name in ("XYZ100", "SP500") else "美股单票")
    return {"fri": fri, "name": name, "family": fam, "gran": "15m", "dev_bps": dev, "move_bps": move,
            "y_bps": dev + move if y is None else y, "news": None, **kw}


def random_rows(n_weeks=14, names=("NVDA", "TSLA", "AMD", "META"), seed=3):
    rnd = random.Random(seed)
    rows = []
    for f in fridays(n_weeks):
        idev, imove = rnd.gauss(0, 40), rnd.gauss(0, 20)
        rows.append(r(f, "XYZ100", idev, imove))
        for nm in names:
            b = 1.2
            rows.append(r(f, nm, b * idev + rnd.gauss(0, 60), b * imove + rnd.gauss(0, 30),
                          nx_hi_bps=rnd.gauss(0, 10), nx_lo_bps=rnd.gauss(0, 10) - 5, move2_bps=rnd.gauss(0, 40)))
    return rows


# ---------------------------------------------------------------- ① ② 仓位
def test_alloc_weights_split_by_signals_and_cap_25pct():
    assert sl.v3_weights([("A", 60)], "alloc", 17) == {"A": 0.25}                         # 1 个信号 → 25%，其余现金
    w = sl.v3_weights([(k, 60) for k in "ABCDEFGH"], "alloc", 17)
    assert all(v == pytest.approx(1 / 8) for v in w.values()) and sum(w.values()) == pytest.approx(1.0)
    assert sl.v3_weights([("A", 60), ("B", 500)], "fixed", 17) == {"A": 1 / 17, "B": 1 / 17}
    assert sl.v3_weights([], "alloc", 17) == {}


def test_strength_weights_proportional_capped_at_3x_and_25pct():
    w = sl.v3_weights([("A", 50), ("B", 100), ("C", 400), ("D", 75)], "strength", 17)
    raw = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 1.5}                                          # 400/50 = 8 → 封顶 3
    tot = sum(raw.values())
    for k, v in raw.items():
        assert w[k] == pytest.approx(min(v / tot, 0.25))
    assert sum(w.values()) <= 1.0 + 1e-12


# ---------------------------------------------------------------- ④ 挂单平仓
def test_maker_exit_fill_only_when_next_bar_trades_through():
    base = r("2026-03-06", move=20.0, nx_hi_bps=3.0, nx_lo_bps=-4.0, move2_bps=12.0)
    long_fill = sl.exit_leg(1, base, "maker", 14.0, 6.0)
    assert long_fill == {"gross": 20.0, "fee": 6.0, "maker": "filled"}                      # 平多：最高价越过限价
    short_fill = sl.exit_leg(-1, base, "maker", 14.0, 6.0)
    assert short_fill["maker"] == "filled" and short_fill["gross"] == -20.0               # 平空：最低价越过
    touch = {**base, "nx_hi_bps": 0.0}                                                      # 只碰到不算成交
    miss = sl.exit_leg(1, touch, "maker", 14.0, 6.0)
    assert miss["maker"] == "unfilled" and miss["fee"] == 14.0 and miss["gross"] == 12.0 and miss["slip"] == -8.0
    nodata = sl.exit_leg(1, r("2026-03-06", move=20.0), "maker", 14.0, 6.0)
    assert nodata == {"gross": 20.0, "fee": 14.0, "maker": "nodata"}
    assert sl.exit_leg(1, base, "taker", 14.0, 6.0) == {"gross": 20.0, "fee": 14.0, "maker": None}


# ---------------------------------------------------------------- ③ β
def test_beta_ols_min_history_clip_and_window():
    fr = fridays(10)
    past = []
    for i, f in enumerate(fr):
        past += [r(f, "XYZ100", y=float(i * 10 - 40)), r(f, "NVDA", y=1.5 * (i * 10 - 40) + 7)]
    assert sl.beta_est(past, "NVDA", "XYZ100") == pytest.approx(1.5)
    assert sl.beta_est(past[:10], "NVDA", "XYZ100") is None                                 # 5 个周末 < 6
    big = [{**p, "y_bps": p["y_bps"] * 10} if p["name"] == "NVDA" else p for p in past]
    assert sl.beta_est(big, "NVDA", "XYZ100") == 3.0                                        # 夹到 3
    assert sl.beta_est(past, "NVDA", None) is None
    # 窗口：最近 6 个周末是 β=0.5，之前是 β=2 → window=6 时只看到 0.5
    mixed = []
    for i, f in enumerate(fr):
        x = float((i % 3) * 20 - 20 + i)
        mixed += [r(f, "XYZ100", y=x), r(f, "NVDA", y=(2.0 if i < 4 else 0.5) * x)]
    assert sl.beta_est(mixed, "NVDA", "XYZ100", window=6) == pytest.approx(0.5)


def _hist(n, b=1.0):
    out = []
    for i, f in enumerate(fridays(n)):
        x = float((i % 4) * 30 - 45)
        out += [r(f, "XYZ100", y=x), r(f, "NVDA", y=b * x), r(f, "TSLA", y=b * x)]
    return out


def test_hedge_nets_index_leg_and_charges_full_fee_on_net():
    past = _hist(8, b=1.0)
    f = "2026-06-26"
    # 只做 NVDA 多（dev −60 → 反向做多），alloc 25%，β=1 → 指数空 25%
    today = [r(f, "XYZ100", dev=0.0, move=100.0), r(f, "NVDA", dev=-60.0, move=30.0), r(f, "TSLA", dev=10.0, move=0.0)]
    wk = sl.v3_week(today, past, ("alloc", "hedge", "taker"), CTX)
    assert wk["hedge_w"] == pytest.approx(-0.25)
    stock = 0.25 * (30.0 - 14.0) / 1e4
    idx = (-0.25 * 100.0 - 0.25 * 14.0) / 1e4
    assert wk["ret"] == pytest.approx(stock + idx) and wk["gross_expo"] == pytest.approx(0.5)
    # 两只反向、β 相同 → 对冲轧差为 0，不付指数手续费
    today2 = [r(f, "XYZ100", dev=0.0, move=100.0), r(f, "NVDA", dev=-60.0, move=30.0), r(f, "TSLA", dev=60.0, move=-10.0)]
    wk2 = sl.v3_week(today2, past, ("alloc", "hedge", "taker"), CTX)
    assert wk2["hedge_w"] == pytest.approx(0.0) and wk2["idx_pnl"] == 0.0
    assert wk2["ret"] == pytest.approx(0.25 * (30 - 14) / 1e4 + 0.25 * (10 - 14) / 1e4)
    # 历史不够 6 个周末 → β=0、不对冲（等于 alloc）
    wk3 = sl.v3_week(today, _hist(4), ("alloc", "hedge", "taker"), CTX)
    assert wk3["hedge_w"] == 0.0 and wk3["ret"] == pytest.approx(stock)


def test_residual_signal_removes_index_part_of_dev():
    past = _hist(8, b=1.0)
    f = "2026-06-26"
    today = [r(f, "XYZ100", dev=40.0, move=0.0), r(f, "NVDA", dev=60.0, move=-10.0), r(f, "TSLA", dev=30.0, move=5.0, y=0.0)]
    wk = sl.v3_week(today, past, ("alloc", "resid", "taker"), CTX)
    # NVDA 残差 60−40=20 → 不做；TSLA 原始 30（不做）但残差 30−40=−10 → 也不做
    assert wk["legs"] == []
    today[0]["dev_bps"] = -40.0                                                             # TSLA 残差 70 → 做空
    wk = sl.v3_week(today, past, ("alloc", "resid", "taker"), CTX)
    assert {(lg["name"], lg["side"]) for lg in wk["legs"]} == {("NVDA", -1), ("TSLA", -1)}  # NVDA 残差 100
    raw = sl.v3_week(today, past, ("alloc", "none", "taker"), CTX)
    assert {lg["name"] for lg in raw["legs"]} == {"NVDA"}                                   # 原始信号只有 NVDA


# ---------------------------------------------------------------- 风险统计
def test_drawdown_leverage_and_scaling():
    rets = [0.1, -0.1, 0.05]
    assert sl.max_drawdown(rets) == pytest.approx(0.1)
    assert sl.max_drawdown(rets, 2.0) == pytest.approx(0.2)
    assert sl.max_drawdown([-0.6], 2.0) == 1.0                                              # 1 + L·r ≤ 0 → 爆仓
    assert sl.leverage_for_dd(rets, 0.20) == pytest.approx(2.0)
    assert sl.leverage_for_dd([0.01, 0.02]) == sl.L_MAX                                     # 没有亏损周 → 搜索上限
    st1 = sl.port_stats([0.02, -0.04, 0.05, 0.0], paths=200)
    assert st1["sharpe_a"] == pytest.approx(st1["sharpe_w"] * math.sqrt(52))
    assert st1["t"] == pytest.approx(st1["sharpe_w"] * math.sqrt(4))
    assert st1["ret_at_lev"] == pytest.approx(st1["mean"] * st1["lev"])
    assert st1["dd_at_lev"] <= 0.20 + 1e-12
    # 杠杆不改变 Sharpe：收益 ×3 → 均值、标准差 ×3，Sharpe 不变，L 变成 1/3
    st3 = sl.port_stats([3 * x for x in [0.02, -0.04, 0.05, 0.0]], paths=200)
    assert st3["sharpe_w"] == pytest.approx(st1["sharpe_w"]) and st3["sd"] == pytest.approx(3 * st1["sd"])
    assert st3["lev"] == pytest.approx(st1["lev"] / 3, abs=sl.L_STEP)


def test_ruin_probability_bootstrap():
    assert sl.ruin_prob([-0.02], 1.0, paths=50) == 1.0                                      # 0.98^52 ≈ 0.35 < 0.5
    assert sl.ruin_prob([-0.01], 1.0, paths=50) == 0.0                                      # 0.99^52 ≈ 0.59
    assert sl.ruin_prob([0.01, 0.02], 5.0, paths=50) == 0.0
    a = sl.ruin_prob([0.05, -0.06, 0.01], 3.0, paths=500)
    assert a == sl.ruin_prob([0.05, -0.06, 0.01], 3.0, paths=500) and 0.0 < a < 1.0         # 固定种子可复现


# ---------------------------------------------------------------- ⑤ 走前挑选 + 不偷看
def test_selector_uses_only_past_weeks_and_skips_nonpositive():
    wk = fridays(6)
    ser = {("a",): {w: {"ret": x} for w, x in zip(wk, [0.01, 0.012, 0.011, -0.5, 0.0, 0.0])},
           ("b",): {w: {"ret": x} for w, x in zip(wk, [0.001, 0.003, 0.002, 0.5, 0.0, 0.0])},
           ("c",): {w: {"ret": -0.01} for w in wk}}
    picks = sl.v3_select(ser, wk, 3)
    assert picks[wk[0]] is None and picks[wk[2]] is None                                    # 不足 3 个过去周末
    assert picks[wk[3]] == ("a",)                                                           # 不知道本周 a 会 −50%
    assert picks[wk[4]] == ("b",)                                                           # 看到之后才换
    assert all(p != ("c",) for p in picks.values())                                         # 过去均值 ≤ 0 永不选


def test_v3_future_data_never_changes_past_or_current_decisions():
    rows = random_rows()
    ctx = sl.v3_ctx(rows, COSTS)
    cut = fridays(14)[9]
    rnd = random.Random(11)
    fut = [{**x, "dev_bps": rnd.gauss(0, 300), "move_bps": rnd.gauss(0, 300), "y_bps": rnd.gauss(0, 300)}
           if x["fri"] > cut else x for x in rows]
    for cb in sl.v3_grid() + [sl.V3_REPORT["base"]]:
        a, b = sl.v3_series(rows, cb, ctx), sl.v3_series(fut, cb, ctx)
        for w in a:
            if w <= cut:
                assert a[w]["ret"] == b[w]["ret"] and a[w]["legs"] == b[w]["legs"], (cb, w)
    wks = sorted({x["fri"] for x in rows})
    pa = sl.v3_select({cb: sl.v3_series(rows, cb, ctx) for cb in sl.v3_grid()}, wks, 4)
    pb = sl.v3_select({cb: sl.v3_series(fut, cb, ctx) for cb in sl.v3_grid()}, wks, 4)
    assert all(pa[w] == pb[w] for w in wks if w <= cut)


def test_current_week_outcome_does_not_change_current_weights_sides_or_beta():
    rows = random_rows()
    ctx = sl.v3_ctx(rows, COSTS)
    last = fridays(14)[-1]
    alt = [{**x, "move_bps": x["move_bps"] + 500, "y_bps": x["y_bps"] * -7, "nx_hi_bps": 1.0} if x["fri"] == last else x
           for x in rows]
    for cb in sl.v3_grid():
        a = sl.v3_series(rows, cb, ctx)[last]
        b = sl.v3_series(alt, cb, ctx)[last]
        key = lambda wk: [(lg["name"], lg["w"], lg["side"], lg["beta"]) for lg in wk["legs"]]  # noqa: E731
        assert key(a) == key(b) and a["hedge_w"] == b["hedge_w"], cb
    # 而把本周末 y 喂给 β（偷看）确实会改 β——夹具能抓到偷看
    past = sl._past(rows, last)
    peek = [x for x in alt if x["fri"] <= last]
    assert sl.beta_est(past, "NVDA", "XYZ100") != sl.beta_est(peek, "NVDA", "XYZ100")


def test_evaluate_v3_oos_window_and_all_rows_present():
    rows = random_rows()
    res = sl.evaluate_v3(rows, COSTS, warmup=8, paths=200)
    assert res["oos_from"] == fridays(14)[8] and len(res["oos"]) == 6
    assert set(res["stats"]) == set(sl.V3_REPORT) | {"select"}
    for k, st in res["stats"].items():
        assert st["n_weekends"] == 6, k
    assert all(res["picks"][w] is None for w in fridays(14)[:8])
    # base 每笔 1/N（N = 4 只单票）
    legs = [lg for w in res["oos"] for lg in sl.v3_series(rows, sl.V3_REPORT["base"], res["ctx"])[w]["legs"]]
    assert legs and all(lg["w"] == pytest.approx(0.25) for lg in legs)
    md = "\n".join(sl.v3_section_cn(res))
    assert "杠杆不改变 Sharpe" in md and "⑤ 走前挑选" in md


def test_attach_exit_bars_reads_next_bar_after_exit(s):  # noqa: F811
    now = datetime(2026, 10, 14, tzinfo=UTC)
    fn = lambda k: 100.0 + (k // 900_000) % 7 * 0.1   # noqa: E731
    inst = s.inst("NVDA")
    _write_cache(s, inst["coin"], "15m", datetime(2026, 8, 23, tzinfo=UTC), now, fn)
    from desk.backtest_lib import load_cache_iv
    cbi = {"15m": load_cache_iv(s, inst["coin"], "15m")[0]}
    rows = sl.weekend_rows(s, inst, cbi, now)
    sl.attach_exit_bars(s, inst, cbi, rows)
    assert rows and all("move2_bps" in x for x in rows)
    x = rows[-1]
    from desk.clock import weekend_for_friday
    w = weekend_for_friday(date.fromisoformat(x["fri"]), s, inst)
    by_t = {int(c["t"]): c for c in cbi["15m"]}
    k, bar = sl._bar_at_or_after(by_t, 900_000, w.exit)
    nxt = by_t[k + 900_000]
    p_x, p_d = bar["c"], bar["c"] / (1 + x["move_bps"] / 1e4)
    assert x["nx_hi_bps"] == pytest.approx((nxt["h"] / p_x - 1) * 1e4)
    assert x["move2_bps"] == pytest.approx((nxt["c"] / p_d - 1) * 1e4)
    assert k + 900_000 >= int(w.exit.timestamp() * 1000)                                    # 下一根在平仓之后
