"""卡 B2：options_v2_backtest — 合成 parquet / 合成收盘，不碰网络、不读真实数据。"""
import copy
import json
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from desk import options_v2_backtest as v2
from desk import options_rules_backtest as v1

SPEC = v2.load_spec()
STRAT = {s["id"]: s for s in SPEC["strategy"]}
FILL = SPEC["fill"]
ET = "America/New_York"
NAN = float("nan")


def weekdays(n, start=date(2024, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = weekdays(80)
RAW = [100.0] * len(DAYS)
EXP = "2024-02-09"           # idx 2 入场时 36 DTE；exit_dte=7 的日子 = 2024-02-02 = idx 23


def osi(exp, cp, strike, root="SPY"):
    return f"{root:<6}{exp.replace('-', '')[2:]}{cp}{int(round(strike * 1000)):08d}"


def row(day, hhmm, sym, bid, ask):
    ts = pd.Timestamp(f"{day} {hhmm}", tz=ET).tz_convert("UTC")
    return {"ts": ts, "symbol": sym, "bid_px": bid, "ask_px": ask, "bid_sz": 1, "ask_sz": 1}


def qrows(sym, idxs, bid, ask, hhmm="15:59"):
    fb = bid if callable(bid) else (lambda k: bid)
    fa = ask if callable(ask) else (lambda k: ask)
    return [row(DAYS[k], hhmm, sym, fb(k), fa(k)) for k in idxs]


def write_q(rows, d, name="2024-01.parquet"):
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(d / name, index=False)


def quotes_from(rows, tmp_path):
    write_q(rows, tmp_path / "SPY")
    return v1.Quotes.from_dir(tmp_path / "SPY")


def mkctx(q, adj=None, raw=None, days=DAYS, und="SPY"):
    return v2.Ctx(und, days, adj or RAW, raw or RAW, q, SPEC)


def always(sid, **kw):
    s = copy.deepcopy(STRAT[sid])
    s["entry"] = "always"
    s.update(kw)
    return s


P95, P90 = osi(EXP, "P", 95), osi(EXP, "P", 90)
C105, C112, C110 = osi(EXP, "C", 105), osi(EXP, "C", 112), osi(EXP, "C", 110)
C100, PUT100 = osi(EXP, "C", 100), osi(EXP, "P", 100)


def spread_rows(idxs=range(2, 31), p95=(1.00, 1.05), p90=(0.30, 0.35), over=None):
    """put 价差的两条腿；over = {idx: (p95, p90)} 覆盖某几天。"""
    over = over or {}
    rows = []
    for k in idxs:
        a, b = over.get(k, (p95, p90))
        rows += qrows(P95, [k], a[0], a[1]) + qrows(P90, [k], b[0], b[1])
    return rows


def first_trade(strat, q, i=2):
    ctx = mkctx(q)
    cands = [c for c in v2.scan_entries(strat, ctx) if c["i"] == i]
    assert cands and cands[0]["legs"], cands
    t, why = v2.build_trade(strat, ctx, cands[0], SPEC)
    assert t is not None, why
    return t


# ---------------------------------------------------------------- Black–Scholes / IV
@pytest.mark.parametrize("cp", ["C", "P"])
@pytest.mark.parametrize("K", [80.0, 95.0, 100.0, 108.0, 130.0])
@pytest.mark.parametrize("T,sig", [(14 / 365, 0.15), (38 / 365, 0.25), (60 / 365, 0.8)])
def test_iv_round_trip(cp, K, T, sig):
    S, r = 100.0, 0.045
    mid = v2.bs_price(S, K, T, r, sig, cp)
    iv = v2.implied_vol(mid, S, K, T, r, cp)
    disc = K * math.exp(-r * T)
    time_value = mid - (max(S - disc, 0) if cp == "C" else max(disc - S, 0))
    if time_value < 1e-6:                                    # 几乎没有时间价值：价格里没有波动信息，反解不出来是对的
        assert iv is None or abs(iv - sig) < 1e-3
        return
    assert iv is not None and iv == pytest.approx(sig, abs=1e-6)


def test_iv_returns_none_when_unsolvable():
    S, K, T, r = 100.0, 100.0, 38 / 365, 0.045
    assert v2.implied_vol(0.0, S, K, T, r, "C") is None                  # 中间价 0
    assert v2.implied_vol(0.5, S, 80.0, T, r, "C") is None               # 低于内在价值 20
    assert v2.implied_vol(2.0, S, 120.0, T, r, "P") is None              # put 深度实值，中间价低于内在价值 20
    assert v2.implied_vol(150.0, S, K, T, r, "C") is None                # 高于上限（现价）
    assert v2.implied_vol(5.0, S, K, 0.0, r, "C") is None                # T = 0
    assert v2.implied_vol(NAN, S, K, T, r, "C") is None
    assert v2.implied_vol(v2.bs_price(S, K, T, r, 0.2, "C"), S, K, T, r, "C") == pytest.approx(0.2, abs=1e-6)     # 对照：正常的能解


def test_iv30_recovers_flat_vol_and_interpolates(tmp_path):
    e1, e2, e3 = "2024-02-02", "2024-02-09", "2024-02-16"          # 从 1/4 起 29 / 36 / 43 天
    day = 2
    o = date.fromisoformat(DAYS[day]).toordinal()

    def build(sig_by_exp):
        rows = []
        for ex, sig in sig_by_exp.items():
            T = (date.fromisoformat(ex).toordinal() - o) / 365
            for cp in ("C", "P"):
                p = v2.bs_price(100.0, 100.0, T, 0.045, sig, cp)
                rows += qrows(osi(ex, cp, 100), [day], p - 0.01, p + 0.01)
        return quotes_from(rows, tmp_path)
    q = build({e1: 0.30, e2: 0.30})
    assert v2.iv30(q, o, 100.0, SPEC) == pytest.approx(0.30, abs=1e-6)
    q = build({e1: 0.20, e2: 0.30})                                  # 30 天夹在 29 与 36 之间：线性插值
    assert v2.iv30(q, o, 100.0, SPEC) == pytest.approx(0.20 + 0.10 / 7, abs=1e-6)
    q = build({e2: 0.20, e3: 0.30})                                  # 30 天在两个到期日同一侧：不外推，取离 30 天近的
    assert v2.iv30(q, o, 100.0, SPEC) == pytest.approx(0.20, abs=1e-6)
    q = build({e2: 0.25})                                            # 只有一个到期日就用它
    assert v2.iv30(q, o, 100.0, SPEC) == pytest.approx(0.25, abs=1e-6)


# ---------------------------------------------------------------- RV 只用之前的收盘
def test_rv20_matches_formula_and_ignores_today_and_future():
    rng = np.random.default_rng(3)
    adj = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 120)))
    i = 60
    base = v2.rv20(adj, i)
    manual = float(np.std(np.diff(np.log(adj[i - 21:i])), ddof=1) * math.sqrt(252))
    assert len(np.diff(np.log(adj[i - 21:i]))) == 20 and base == pytest.approx(manual)
    for mutate in (lambda a: a.__setitem__(i, 1e6), lambda a: a.__setitem__(slice(i, None), [0.001] * (len(a) - i)),
                   lambda a: a.__setitem__(slice(i + 1, None), [1e9] * (len(a) - i - 1))):
        b = list(adj)
        mutate(b)
        assert v2.rv20(b, i) == base                                # 今天和以后的收盘完全不影响
    b = list(adj)
    b[i - 1] *= 1.5
    assert v2.rv20(b, i) != base                                    # d−1 的收盘影响
    b = list(adj)
    b[i - 22] *= 1.5
    assert v2.rv20(b, i) == base                                    # 窗口之外不影响
    assert v2.rv20(adj, 20) is None                                 # 历史不够


def test_signal_unchanged_by_todays_close():
    rng = np.random.default_rng(5)
    adj = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 100)))
    s = STRAT["F3_cheap_straddle"]
    ctx = mkctx(None, adj=adj, raw=adj, days=weekdays(100))
    sig0 = v2.entry_signal(s, 0.12, ctx.rv(50))
    adj2 = list(adj)
    adj2[50] *= 3
    ctx2 = mkctx(None, adj=adj2, raw=adj2, days=weekdays(100))
    assert v2.entry_signal(s, 0.12, ctx2.rv(50)) == sig0 and ctx2.rv(50) == ctx.rv(50)


def test_entry_signal_thresholds():
    ge, le = STRAT["F2_condor_rich"], STRAT["F3_cheap_straddle"]
    assert v2.entry_signal(ge, 0.24, 0.20) is True and v2.entry_signal(ge, 0.2399, 0.20) is False        # 1.2 倍
    assert v2.entry_signal(le, 0.17, 0.20) is True and v2.entry_signal(le, 0.18, 0.20) is False          # 0.85 倍
    assert v2.entry_signal(ge, None, 0.2) is None and v2.entry_signal(ge, 0.2, None) is None
    assert v2.entry_signal(STRAT["F1_put_spread"], None, None) is True


# ---------------------------------------------------------------- 腿选择 / 流动性
def test_put_spread_credit_and_max_loss_math(tmp_path):
    q = quotes_from(spread_rows(), tmp_path)
    t = first_trade(STRAT["F1_put_spread"], q)
    credit = 1.00 * 0.99 - 0.35 * 1.01                                # 卖 bid、买 ask，各 1% 滑点
    assert t["net_ps"] == pytest.approx(credit, abs=1e-4) and t["kind"] == "credit"
    assert t["max_loss"] == pytest.approx((5 - credit) * 100, abs=0.01)    # 宽度 5 − 权利金，×100
    assert t["legs"] == "卖P95 / 买P90" and t["expiry"] == EXP and t["dte_entry"] == 36
    # 一路没触发止盈止损 -> 7 DTE 平仓（idx 23）；按 ask 买回、bid 卖出，进出各 2 腿 × 0.65
    exit_cost = 1.05 * 1.01 - 0.30 * 0.99
    expect = 100 * credit - 1.30 - 100 * exit_cost - 1.30
    assert t["reason"] == "exit_dte" and t["exit_idx"] == 23 and t["pnl"] == pytest.approx(expect, abs=0.01)
    assert t["ret"] == pytest.approx(expect / t["max_loss"])
    # 参考口径（仅参考）：成交更靠中间价，亏得更少
    assert t["pnl"] < t["pnl_ref0.75"] < t["pnl_ref0.5"]


def test_take_profit_on_credit(tmp_path):
    q = quotes_from(spread_rows(over={5: ((0.30, 0.35), (0.05, 0.10))}), tmp_path)    # 平仓成本 0.30 ≤ 0.5 × 0.6365
    t = first_trade(STRAT["F1_put_spread"], q)
    credit = 1.00 * 0.99 - 0.35 * 1.01
    assert t["reason"] == "take_profit" and t["exit_idx"] == 5
    assert t["pnl"] == pytest.approx(100 * credit - 1.3 + 100 * (0.05 * 0.99 - 0.35 * 1.01) - 1.3, abs=0.01)


def test_condor_max_loss_uses_wider_side(tmp_path):
    rows = spread_rows(range(2, 31)) + qrows(C105, range(2, 31), 0.80, 0.85) + qrows(C112, range(2, 31), 0.10, 0.15)
    q = quotes_from(rows, tmp_path)
    t = first_trade(always("F2_condor_rich"), q)
    assert t["legs"] == "卖P95 / 买P90 / 卖C105 / 买C112"            # 1.10 × 100 = 110 最近的是 112，翼宽 7；put 翼宽 5
    net = (1.00 * 0.99 - 0.35 * 1.01) + (0.80 * 0.99 - 0.15 * 1.01)
    assert t["net_ps"] == pytest.approx(net, abs=1e-4)
    assert t["max_loss"] == pytest.approx((7 - net) * 100, abs=0.01)    # max(5, 7) − 总权利金


def test_wing_must_differ_from_short_strike(tmp_path):
    rows = spread_rows(range(2, 6)) + qrows(C105, range(2, 6), 0.80, 0.85)    # 只有 105 一个 call 行权价
    q = quotes_from(rows, tmp_path)
    legs, why = v2.select_legs(q, date.fromisoformat(DAYS[2]).toordinal(), 100.0, always("F2_condor_rich"), SPEC)
    assert legs is None and why == "wing_unavailable"
    q = quotes_from(qrows(P95, range(2, 6), 1.0, 1.05), tmp_path)                # 只有 95 的 put：保护腿也落在 95
    legs, why = v2.select_legs(q, date.fromisoformat(DAYS[2]).toordinal(), 100.0, STRAT["F1_put_spread"], SPEC)
    assert legs is None and why == "wing_unavailable"


def test_entry_liquidity_and_missing_side(tmp_path):
    o = date.fromisoformat(DAYS[2]).toordinal()
    q = quotes_from(spread_rows(range(2, 4), p95=(0.70, 1.05)), tmp_path)           # 价差 0.35 > 15% × 0.875
    assert v2.select_legs(q, o, 100.0, STRAT["F1_put_spread"], SPEC)[1] == "spread"
    q = quotes_from(spread_rows(range(2, 4), p95=(NAN, 0.05)), tmp_path)             # 卖腿没有买价
    assert v2.select_legs(q, o, 100.0, STRAT["F1_put_spread"], SPEC)[1] == "no_bid"
    q = quotes_from(spread_rows(range(2, 4), p90=(0.30, NAN)), tmp_path)             # 买腿没有卖价
    assert v2.select_legs(q, o, 100.0, STRAT["F1_put_spread"], SPEC)[1] == "no_ask"
    q = quotes_from(spread_rows(range(2, 4)), tmp_path)                              # 正常
    assert v2.select_legs(q, o, 100.0, STRAT["F1_put_spread"], SPEC)[0] is not None
    rows = qrows(P95, [2], 1.0, 1.05, hhmm="15:40") + qrows(P90, [2], 0.3, 0.35, hhmm="15:40") + qrows(P90, [3], 0.3, 0.35)
    q = quotes_from(rows, tmp_path)                                                  # 当天最新报价 15:40 < 15:50
    assert v2.select_legs(q, o, 100.0, STRAT["F1_put_spread"], SPEC)[1] in ("stale_quote", "no_quotes")


def test_straddle_legs_share_one_strike(tmp_path):
    o = date.fromisoformat(DAYS[2]).toordinal()
    rows = (qrows(osi(EXP, "C", 100), [2], 3.0, 3.05) + qrows(osi(EXP, "C", 101), [2], 2.4, 2.45)
            + qrows(osi(EXP, "P", 99), [2], 2.0, 2.05) + qrows(osi(EXP, "P", 100), [2], 2.8, 2.85) + qrows(osi(EXP, "P", 101), [2], 3.4, 3.45))
    q = quotes_from(rows, tmp_path)
    legs, why = v2.select_legs(q, o, 100.0, always("F3_cheap_straddle"), SPEC)
    assert why == "" and {l["strike"] for l in legs} == {100.0} and {l["type"] for l in legs} == {"C", "P"}
    q = quotes_from(qrows(osi(EXP, "C", 100), [2], 3.0, 3.05) + qrows(osi(EXP, "P", 101), [2], 3.4, 3.45), tmp_path)    # 没有共同行权价
    assert v2.select_legs(q, o, 100.0, always("F3_cheap_straddle"), SPEC)[1] == "no_strike"


def test_expiry_choice_target_and_override(tmp_path):
    exps = [date.fromisoformat(DAYS[2]).toordinal() + n for n in (20, 31, 37, 44, 50)]
    o = date.fromisoformat(DAYS[2]).toordinal()
    assert v2.pick_expiry(exps, o, SPEC, STRAT["F1_put_spread"]) == o + 37          # 离 38 最近（31..44 里）
    assert v2.pick_expiry(exps, o, SPEC, STRAT["F4_earnings_ironfly"]) == o + 20    # 14..45 里最近的那个
    assert v2.pick_expiry([o + 10, o + 60], o, SPEC, STRAT["F1_put_spread"]) is None


# ---------------------------------------------------------------- 出场：止盈挑流动性，止损不挑
def test_take_profit_blocked_by_wide_spread_but_stop_executes(tmp_path):
    wide_cheap = ((0.05, 0.30), (0.05, 0.10))        # 平仓成本 0.25 ≤ 0.318，但 P95 价差 0.25 太宽 -> 不止盈
    crash = ((3.00, 4.20), (0.50, 0.60))             # 成本 3.70 -> 触发止损；价差很宽也照样按 ask 成交
    q = quotes_from(spread_rows(over={5: wide_cheap, 8: crash}), tmp_path)
    t = first_trade(STRAT["F1_put_spread"], q)
    assert t["reason"] == "stop_loss" and t["exit_idx"] == 8           # idx 5 没有止盈
    credit = 1.00 * 0.99 - 0.35 * 1.01
    expect = 100 * credit - 1.3 + 100 * (0.50 * 0.99 - 4.20 * 1.01) - 1.3
    assert t["pnl"] == pytest.approx(expect, abs=0.01)
    # 同一天价差窄就会止盈（对照）
    q = quotes_from(spread_rows(over={5: ((0.05, 0.10), (0.05, 0.10))}), tmp_path)
    t = first_trade(STRAT["F1_put_spread"], q)
    assert t["reason"] == "take_profit" and t["exit_idx"] == 5


def test_missing_bid_closes_at_zero_and_missing_ask_is_max_loss(tmp_path):
    # 平仓日（idx 23）保护腿没有买价 -> 按 0 卖
    q = quotes_from(spread_rows(over={23: ((1.00, 1.05), (NAN, 0.10))}), tmp_path)
    t = first_trade(STRAT["F1_put_spread"], q)
    credit = 1.00 * 0.99 - 0.35 * 1.01
    assert t["exit_idx"] == 23 and t["pnl"] == pytest.approx(100 * credit - 1.3 - 100 * 1.05 * 1.01 - 1.3, abs=0.01)
    assert not t["no_ask_close"]
    # 空头腿没有卖价 -> 整个价差按最大亏损平仓，并单独计数
    q = quotes_from(spread_rows(over={6: ((1.00, NAN), (0.30, 0.35))}), tmp_path)
    ctx = mkctx(q)
    s = STRAT["F1_put_spread"]
    cands = [c for c in v2.scan_entries(s, ctx) if c["i"] == 2]
    tr, sig, sk = v2.simulate(s, ctx, SPEC, cands)
    t = tr[0]
    assert t["no_ask_close"] and t["exit_idx"] == 6
    assert t["pnl"] == pytest.approx(-t["max_loss"] - 4 * 0.65, abs=0.01)
    ev = v2.evaluate(s, "SPY", tr, sig, sk, SPEC)
    assert ev["n_no_ask_close"] == 1


def test_missing_quotes_defer_then_no_more_quotes(tmp_path):
    # 平仓日没有保护腿报价 -> 顺延到下一个所有腿都有报价的日子
    rows = spread_rows(range(2, 23)) + spread_rows(range(25, 31))
    t = first_trade(STRAT["F1_put_spread"], quotes_from(rows, tmp_path))
    assert t["reason"] == "exit_dte" and t["exit_idx"] == 25
    # 只有入场日有报价：到期前一直没有完整报价 -> 按最大亏损记
    t = first_trade(STRAT["F1_put_spread"], quotes_from(spread_rows(range(2, 4)), tmp_path))
    assert t["reason"] == "no_more_quotes" and t["pnl"] == pytest.approx(-t["max_loss"] - 2 * 0.65, abs=0.01)
    assert t["pnl_lastmark"] > t["pnl"]


def test_debit_straddle_take_profit_and_stop(tmp_path):
    s = always("F3_cheap_straddle")

    def rows_for(over):
        rows = []
        for k in range(2, 20):
            c, p = over.get(k, ((3.00, 3.05), (2.50, 2.55)))
            rows += qrows(C100, [k], *c) + qrows(PUT100, [k], *p)
        return rows
    t = first_trade(s, quotes_from(rows_for({4: ((5.50, 5.55), (3.00, 3.05))}), tmp_path))
    debit = 3.05 * 1.01 + 2.55 * 1.01
    assert t["kind"] == "debit" and t["max_loss"] == pytest.approx(debit * 100, abs=0.01)
    assert t["reason"] == "take_profit" and t["exit_idx"] == 4
    assert t["pnl"] == pytest.approx(-debit * 100 - 1.3 + (5.50 + 3.00) * 0.99 * 100 - 1.3, abs=0.01)
    t = first_trade(s, quotes_from(rows_for({4: ((2.00, 2.05), (1.50, 1.55))}), tmp_path))
    assert t["reason"] == "stop_loss" and t["exit_idx"] == 4 and t["pnl"] < 0
    t = first_trade(s, quotes_from(rows_for({}), tmp_path))
    assert t["reason"] == "max_hold" and t["exit_idx"] == 2 + s["max_hold_days"]


def test_cooldown_days(tmp_path):
    rows = spread_rows(range(0, 31), over={1: ((0.05, 0.10), (0.05, 0.10))})        # idx 0 入场，idx 1 止盈（只有 idx 0..6 在 30–45 DTE 窗口内）
    q = quotes_from(rows, tmp_path)
    ctx = mkctx(q)
    s = STRAT["F1_put_spread"]
    tr, *_ = v2.simulate(s, ctx, SPEC, v2.scan_entries(s, ctx))
    assert tr[0]["entry_idx"] == 0 and tr[0]["exit_idx"] == 1 and tr[1]["entry_idx"] == 2     # 平仓的下一个交易日就能再开
    s5 = dict(s, cooldown_days=5)
    tr, *_ = v2.simulate(s5, ctx, SPEC, v2.scan_entries(s5, ctx))
    assert tr[0]["exit_idx"] == 1 and tr[1]["entry_idx"] == 6                                  # 平仓日 + 5


# ---------------------------------------------------------------- 财报
def test_earnings_entry_dates_amc_bmo():
    ev = [{"date": "2024-01-10", "timing": "amc", "source": "x"}, {"date": "2024-01-12", "timing": "bmo", "source": "x"},
          {"date": "2024-01-13", "timing": "amc", "source": "x"}]           # 周六：不是交易日
    out, bad = v2.earnings_entries(ev, DAYS)
    assert {DAYS[i]: e["timing"] for i, e in out.items()} == {"2024-01-10": "amc", "2024-01-11": "bmo"}     # bmo -> 前一交易日
    assert bad["earnings_date_not_trading_day"] == 1


def test_earnings_straddle_control_trade_dates(tmp_path):
    exp = "2024-01-26"
    rows = []
    for k in (6, 7, 8):
        rows += qrows(osi(exp, "C", 100), [k], 3.0 if k == 6 else 4.5, 3.05 if k == 6 else 4.55) + qrows(osi(exp, "P", 100), [k], 2.9 if k == 6 else 2.0, 2.95 if k == 6 else 2.05)
        rows += qrows(osi("2024-02-09", "C", 100), [k], 5.0, 5.05) + qrows(osi("2024-02-09", "P", 100), [k], 5.0, 5.05)
    q = quotes_from(rows, tmp_path)
    ctx = mkctx(q)
    s = STRAT["F4c_earnings_straddle_control"]
    ev, _ = v2.earnings_entries([{"date": "2024-01-10", "timing": "amc", "source": "x"}], DAYS)
    tr, sig, sk = v2.simulate(s, ctx, SPEC, v2.scan_entries(s, ctx, ev))
    t = tr[0]
    assert (t["entry_date"], t["exit_date"], t["reason"]) == ("2024-01-10", "2024-01-11", "next_session")
    assert t["expiry"] == exp                                                  # 14–45 天里最近的到期日
    assert t["pnl"] == pytest.approx(-(3.05 + 2.95) * 1.01 * 100 + (4.5 + 2.0) * 0.99 * 100 - 2.6, abs=0.01)
    ev, _ = v2.earnings_entries([{"date": "2024-01-11", "timing": "bmo", "source": "x"}], DAYS)       # bmo：前一天 1/10 进
    tr, *_ = v2.simulate(s, ctx, SPEC, v2.scan_entries(s, ctx, ev))
    assert tr[0]["entry_date"] == "2024-01-10" and tr[0]["exit_date"] == "2024-01-11"


def test_ironfly_earnings_exit_missing_leg_is_conservative(tmp_path):
    exp = "2024-01-26"
    rows = []
    legs = [("C", 100, 3.0), ("P", 100, 2.9), ("C", 108, 0.6), ("P", 92, 0.5)]
    for cp, k, mid in legs:
        rows += qrows(osi(exp, cp, k), [6], mid - 0.02, mid + 0.02)
    for cp, k, mid in legs[:3]:               # 次日缺了 P92 保护腿
        rows += qrows(osi(exp, cp, k), [7], mid - 0.02, mid + 0.02)
    q = quotes_from(rows, tmp_path)
    ctx = mkctx(q)
    s = STRAT["F4_earnings_ironfly"]
    ev, _ = v2.earnings_entries([{"date": "2024-01-10", "timing": "amc", "source": "x"}], DAYS)
    tr, *_ = v2.simulate(s, ctx, SPEC, v2.scan_entries(s, ctx, ev))
    t = tr[0]
    assert t["missing_exit_legs"] == 1 and t["exit_idx"] == 7 and t["max_loss"] > 0
    net = (2.98 * .99 + 2.88 * .99) - (0.62 * 1.01 + 0.52 * 1.01)
    assert t["net_ps"] == pytest.approx(net, abs=1e-4) and t["max_loss"] == pytest.approx((8 - net) * 100, abs=0.01)


def test_missing_calendar_skips_without_crash(tmp_path):
    spec = copy.deepcopy(SPEC)
    spec["strategy"] = [dict(s, underlyings=["SPY"]) for s in spec["strategy"] if s["id"] in ("F4_earnings_ironfly", "F4c_earnings_straddle_control")]
    root = tmp_path / "opra"
    write_q(spread_rows(range(0, 10)), root / "cbbo1m" / "SPY")
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "closes_SPY_full.json").write_text(json.dumps({d: 100.0 for d in DAYS}))
    out, trades = v2.run(root, ["SPY"], None, spec, earnings_path=tmp_path / "nope.json", log=lambda *_: None)
    assert trades == [] and {x["status"] for x in out["results"]} == {"no_earnings_calendar"}
    assert any("没有财报日历" in w for w in out["warnings"])
    assert v2.load_earnings(tmp_path / "nope.json") is None
    good = tmp_path / "e.json"
    good.write_text(json.dumps([{"ticker": "SPY", "date": "2024-01-10", "timing": "amc", "source": "u"}, {"ticker": "X", "date": "bad", "timing": "amc"}]))
    assert list(v2.load_earnings(good)) == ["SPY"]


# ---------------------------------------------------------------- 无前视
def test_future_quotes_do_not_change_entry(tmp_path):
    base = spread_rows(range(0, 31))
    q1 = quotes_from(base, tmp_path / "a")
    mutated = []
    rng = np.random.default_rng(0)
    for r in base:
        r = dict(r)
        day = r["ts"].tz_convert(ET).date().isoformat()
        if day > DAYS[2]:
            r["bid_px"], r["ask_px"] = float(rng.uniform(0.01, 9)), float(rng.uniform(9, 20))
        mutated.append(r)
    q2 = quotes_from(mutated, tmp_path / "b")
    s = STRAT["F1_put_spread"]
    t1, t2 = first_trade(s, q1), first_trade(s, q2)
    for k in ("entry_date", "legs", "net_ps", "max_loss", "expiry", "dte_entry", "kind"):
        assert t1[k] == t2[k]
    c1 = [c for c in v2.scan_entries(s, mkctx(q1)) if c["i"] == 2][0]["legs"]
    c2 = [c for c in v2.scan_entries(s, mkctx(q2)) if c["i"] == 2][0]["legs"]
    assert c1 == c2
    assert t1["exit_idx"] != t2["exit_idx"] or t1["pnl"] != t2["pnl"]         # 出场当然会跟着未来报价变（对照）


# ---------------------------------------------------------------- 两遍流程 / extra 文件
def test_extra_needed_format_and_second_pass(tmp_path):
    root = tmp_path / "opra"
    main = spread_rows(range(2, 13))                                         # 主数据 idx 13 以后没有（合约掉出 14–60 DTE）
    write_q(main, root / "cbbo1m" / "SPY")
    extra = spread_rows(range(13, 31))
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "closes_SPY_full.json").write_text(json.dumps({d: 100.0 for d in DAYS}))
    spec = copy.deepcopy(SPEC)
    spec["strategy"] = [dict(always("F1_put_spread"), underlyings=["SPY"])]
    out, tr = v2.run(root, ["SPY"], None, spec, earnings_path=tmp_path / "x.json", log=lambda *_: None)
    p = root / "meta" / "extra_needed_v2.json"
    assert p.exists() and not (root / "meta" / "extra_needed.json").exists()          # 不碰 v1 的清单
    need = json.loads(p.read_text())
    assert set(need) == {"SPY"} and DAYS[13] in need["SPY"] and DAYS[23] in need["SPY"] and DAYS[24] not in need["SPY"]
    assert set(need["SPY"][DAYS[13]]) == {P95.replace(" ", ""), P90.replace(" ", "")}
    assert DAYS[12] not in need["SPY"] and out["unresolved_extra"]["SPY"] > 0
    assert tr[0]["reason"] == "no_more_quotes" and out["warnings"]            # 没补之前：保守口径 + 警告
    assert any("extra_needed_v2" in line for line in v2.next_steps(out, root))
    write_q(extra, root / "cbbo1m_extra_v2" / "SPY")                          # 第二遍：补数据合并进来
    out2, tr2 = v2.run(root, ["SPY"], None, spec, earnings_path=tmp_path / "x.json", log=lambda *_: None)
    assert tr2[0]["reason"] == "exit_dte" and out2["unresolved_extra"]["SPY"] == 0 and v2.next_steps(out2, root) == []


def test_v1_write_extra_needed_default_unchanged(tmp_path):
    p = v1.write_extra_needed(tmp_path, {"SPY": {"2024-01-05": {"SPY240209P00095000"}}})
    assert p.name == "extra_needed.json" and json.loads(p.read_text()) == {"SPY": {"2024-01-05": ["SPY240209P00095000"]}}
    p2 = v1.write_extra_needed(tmp_path, {"SPY": {"2024-01-08": {"SPY240209P00090000"}}}, "extra_needed_v2.json")
    assert p2.name == "extra_needed_v2.json" and list(json.loads(p2.read_text())["SPY"]) == ["2024-01-08"]
    assert list(json.loads(p.read_text())["SPY"]) == ["2024-01-05"]


# ---------------------------------------------------------------- 统计 / 过关标志
def mk(entry, exit_, pnl, idx, ml=500.0, reason="exit_dte"):
    return {"entry_date": entry, "exit_date": exit_, "pnl": pnl, "ret": pnl / ml, "entry_idx": idx, "reason": reason,
            "no_ask_close": False, "missing_exit_legs": 0, "legs": "卖P95 / 买P90", "pnl_ref0.75": pnl + 10, "ret_ref0.75": (pnl + 10) / ml,
            "pnl_ref0.5": pnl + 20, "ret_ref0.5": (pnl + 20) / ml}


def test_evaluate_segments_windows_and_flags():
    s = STRAT["F1_put_spread"]
    trades = [mk("2024-07-25", "2024-08-05", -300, 1), mk("2025-06-30", "2025-07-10", 40, 100), mk("2025-04-10", "2025-04-20", -80, 50)]
    trades += [mk(f"2025-{7 + k // 20:02d}-{1 + k % 20:02d}", "2025-12-30", 60 + k % 3, 1000 + 10 * k) for k in range(12)]
    ev = v2.evaluate(s, "SPY", trades, [t["entry_idx"] for t in trades], {"spread": 2}, SPEC)
    assert ev["train"]["trades"] == 3 and ev["test"]["trades"] == 12 and ev["traded_clusters_test"] == 12
    assert ev["windows"]["2024-08-01..2024-08-16"]["realized_pnl"] == -300 and ev["windows"]["2025-04-01..2025-04-30"]["realized_pnl"] == -80
    assert ev["worst_trade"]["pnl"] == -300 and ev["max_consec_losses"] == 2
    assert ev["crit_test_pnl_positive"] and ev["crit_clusters_ge_min"] and ev["crit_boot_p_lt_shadow"]
    assert ev["crit_train_same_sign"] is False and ev["shadow_pass"] is False           # 训练段平均每笔为负
    assert ev["ref"]["0.75"]["note"] == "仅参考，不作为结论" and ev["bonferroni"] == pytest.approx(0.05 / 60)
    good = [mk("2024-03-01", "2024-03-10", 30, 5)] + trades[3:]
    ev = v2.evaluate(s, "SPY", good, [t["entry_idx"] for t in good], {}, SPEC)
    assert ev["shadow_pass"] and ev["live_stat_ok"]                                       # p ≈ 1e-4 < 0.05/60
    few = good[:6]
    ev = v2.evaluate(s, "SPY", few, [], {}, SPEC)
    assert not ev["crit_clusters_ge_min"] and not ev["shadow_pass"]
    big = [mk("2024-03-01", "2024-03-10", 30, 5)] + [mk(f"2025-{7 + k // 20:02d}-{1 + k % 20:02d}", "2025-12-30", 250 + k, 1000 + 10 * k) for k in range(12)]
    ev = v2.evaluate(s, "SPY", big, [], {}, SPEC)
    assert ev["too_good"] and ev["shadow_criteria_met"] and not ev["shadow_pass"]         # 平均 > 最大亏损的 40% -> 先当 bug 查
    ev = v2.evaluate(STRAT["F4_earnings_ironfly"], "NVDA", [], [], {}, SPEC)
    assert ev["sample_note"] and ev["all"]["trades"] == 0 and not ev["shadow_pass"]


def test_too_good_by_win_rate():
    s = STRAT["F1_put_spread"]
    tr = [mk("2024-03-01", "2024-03-10", 5, 5)] + [mk(f"2025-{7 + k // 20:02d}-{1 + k % 20:02d}", "2025-12-30", 5 + k % 2, 1000 + 10 * k) for k in range(32)]
    ev = v2.evaluate(s, "SPY", tr, [], {}, SPEC)
    assert ev["test"]["win_rate"] == 1.0 and ev["test"]["avg_ret"] < 0.4 and ev["too_good"]


# ---------------------------------------------------------------- 仓位换算
def ft(day, und="SPY", strat="F1_put_spread", pnl=100.0, ml=500.0, exit_="2025-08-29"):
    return {"strategy": strat, "und": und, "entry_date": day, "exit_date": exit_, "pnl": pnl, "max_loss": ml, "ret": pnl / ml,
            "mtm": {day: 0.0, exit_: pnl}}


CAL = [d for d in weekdays(600, date(2025, 6, 2))]


def test_sizing_one_trade_scaling():
    r = v2.size_account([ft("2025-07-01", exit_="2025-07-02")], CAL, 0.05, SPEC, {"SPY"})
    assert r["taken"] == 1 and r["equity"][-1] == pytest.approx(100_000 + 0.05 * 100_000 / 500 * 100)      # 10 张 × 100 = +1%
    r = v2.size_account([ft("2025-07-01", exit_="2025-07-02", pnl=-500)], CAL, 0.02, SPEC, {"SPY"})
    assert r["equity"][-1] == pytest.approx(98_000)                                                       # 满亏 = 档位


def test_sizing_correlated_cap_same_day_index_like_only():
    day = "2025-07-01"
    tr = [ft(day, "SPY"), ft(day, "QQQ"), ft(day, "SOXX"), ft(day, "SMH"), ft(day, "SPY", strat="F2_condor_rich"), ft(day, "NVDA", strat="F3_cheap_straddle")]
    r = v2.size_account(tr, CAL, 0.02, SPEC, set(SPEC["universe"]["index_like"]),
                        {"F1_put_spread": 0, "F2_condor_rich": 1, "F3_cheap_straddle": 3, "SPY": 0, "QQQ": 1, "SOXX": 2, "SMH": 3})
    assert r["taken"] == 4 and r["skipped"] == {"correlated_cap": 2}               # 指数类最多 3 笔（= 单档 × 3），单票不受这条限制
    r = v2.size_account([ft("2025-07-01", "SPY"), ft("2025-07-02", "SPY")], CAL, 0.02, SPEC, {"SPY"})
    assert r["taken"] == 2                                                         # 不同天不算同一天


def test_sizing_max_open_cap():
    tr = [ft(CAL[10 + k], "NVDA", exit_="2025-12-30") for k in range(8)]
    r = v2.size_account(tr, CAL, 0.02, SPEC, set(), None)
    assert r["taken"] == 6 and r["skipped"] == {"max_open": 2}
    tr = [ft(CAL[10 + k], "NVDA", exit_=CAL[10 + k + 1]) for k in range(8)]       # 一天就平：不占满
    assert v2.size_account(tr, CAL, 0.02, SPEC, set())["taken"] == 8


def test_segment_stats_and_kelly():
    cal = ["2025-07-01", "2025-07-31", "2025-08-29"]
    st = v2._segment_stats(cal, [100_000, 101_000, 99_990], 0)
    assert st["months"] == 2 and st["monthly_mean"] == pytest.approx(0.0, abs=1e-12) and st["monthly_median"] == pytest.approx(0.0, abs=1e-12)
    assert st["max_drawdown"] == pytest.approx(0.01) and st["worst_week"] == pytest.approx(-0.01)
    assert v2._segment_stats(cal, [1, 1, 1], 5)["monthly_mean"] is None
    rng = np.random.default_rng(1)
    good = list(rng.choice([0.4, -1.0], p=[0.8, 0.2], size=200))
    f = v2.kelly_f(good)
    assert 0 < f < 1
    assert v2.kelly_f([-0.5, 0.1, -0.2, 0.05, -0.3, 0.1]) == 0.0
    assert v2.kelly_f([0.1, 0.2]) is None


def test_sizing_report_quarter_kelly_and_tiers():
    trades = [ft(CAL[40 + 3 * k], "NVDA", pnl=(60.0 if k % 5 else -500.0), exit_=CAL[41 + 3 * k]) for k in range(30)]
    rep = v2.sizing_report(trades, CAL, SPEC, "t", set())
    assert [t["tier"] for t in rep["tiers"]] == [0.02, 0.05, 0.10]
    assert rep["kelly_quarter"] == pytest.approx(rep["kelly_full"] * 0.25) and rep["n_test_trades"] == 30
    assert all(t["test"]["monthly_mean"] is not None for t in rep["tiers"])
    m = [t["test"]["max_drawdown"] for t in rep["tiers"]]
    assert m[0] < m[1] < m[2]                                                            # 档位越大回撤越大


# ---------------------------------------------------------------- 端到端：报告顶部 5 行大白话
def test_end_to_end_report_and_json(tmp_path):
    root = tmp_path / "opra"
    write_q(spread_rows(range(0, 31)), root / "cbbo1m" / "SPY")
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "closes_SPY_full.json").write_text(json.dumps({d: 100.0 for d in DAYS}))
    out, trades = v2.run(root, ["SPY"], ["F1"], SPEC, earnings_path=tmp_path / "x.json", log=lambda *_: None)
    assert len(out["results"]) == 1 and trades and out["spec_sha256_16"] == v2.EXPECTED_SPEC_SHA16 and not any("sha256" in w for w in out["warnings"])
    rdir = tmp_path / "reports"
    v2.write_outputs(out, trades, SPEC, rdir)
    md = (rdir / "options_v2.md").read_text()
    lines = md.splitlines()
    assert lines[0].startswith("# ") and [l[:2] for l in lines[2:7]] == ["1.", "2.", "3.", "4.", "5."] and lines[7] == ""
    assert "仅参考" in md and "F1_put_spread" in md and "四分之一凯利" in md
    js = json.loads((rdir / "options_v2.json").read_text())
    assert js["spec_version"] == "v2" and js["n_trials"] == 60 and js["results"][0]["strategy"] == "F1_put_spread"
    assert (rdir / "options_v2_trades.csv").read_text().startswith("strategy,und")
    assert len(v2.summary_lines(out)) == 5


def test_main_cli_prints_next_commands(tmp_path, capsys, monkeypatch):
    root = tmp_path / "opra"
    write_q(spread_rows(range(2, 13)), root / "cbbo1m" / "SPY")
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "closes_SPY_full.json").write_text(json.dumps({d: 100.0 for d in DAYS}))
    assert v2.main(["--root", str(root), "--out", str(tmp_path / "rep"), "--underlyings", "SPY", "--strategies", "F1",
                    "--earnings", str(tmp_path / "none.json")]) == 0
    out = capsys.readouterr().out
    assert "python -m desk.opra_fetch --extra --needed-file extra_needed_v2.json --extra-sub cbbo1m_extra_v2" in out and "--download --yes" in out
    assert (tmp_path / "rep" / "options_v2.md").exists()
