"""卡 BC-2：熊市看涨价差（bear call credit spread）在研究室引擎里的定价/出场验证 —— 全合成数据，不碰网络、不读真实数据。

策略：卖 call 1.03、买 call 1.08，dte 38，标的 index_like，入场 trend_R2，默认信用出场
（止盈 0.5×权利金、止损 2.0×权利金、exit_dte 7、max_hold 25、冷却 1）。
夹具沿用 tests/test_options_lab.py 的写法：meta/closes_<U>_full.json + cbbo1m/<U>/<YYYY-MM>.parquet，报价是 BS 价 ± 0.02（或手写常数价），
走 lab.validate_proposal -> lab.to_strategy -> lab._simulate（和 run_dev 同一条路）。所有期望值都在测试里用报价表独立算，不调用引擎函数。
"""
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from desk import options_lab as lab
from desk import options_rules_backtest as v1
from desk import options_v2_backtest as v2

ET = "America/New_York"
SPEC = v2.load_spec()
FEE, SLIP, MULT = 0.65, 0.01, 100


# ---------------------------------------------------------------- 合成数据
def weekdays_between(a: str, b: str) -> list[str]:
    d, end, out = date.fromisoformat(a), date.fromisoformat(b), []
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


Q_END = "2024-03-29"
ALL_DAYS = weekdays_between("2023-10-02", Q_END)
FRIDAYS = [d for d in weekdays_between("2023-10-01", "2024-06-30") if date.fromisoformat(d).weekday() == 4]
STRIKES = list(range(85, 126))


def osi(exp, cp, strike, root="SPY"):
    return f"{root:<6}{exp.replace('-', '')[2:]}{cp}{int(round(strike * 1000)):08d}"


def ordinal(d: str) -> int:
    return date.fromisoformat(d).toordinal()


def base_closes() -> list[float]:
    """前 70 天平在 100，之后每天跌 1：趋势 R2（收盘 < 50 日均线、均线比 10 天前低、20 日涨跌幅 ≤ −5%）会在下跌途中首次触发。"""
    return [100.0 if k < 70 else 100.0 - (k - 69) * 1.0 for k in range(len(ALL_DAYS))]


def first_r2_day(closes: list[float]) -> int:
    """v1 的 R2 信号（含 d 当天价）首次为真的下标 j。研究室按 d−1 的收盘决策，所以入场日 e = j + 1。"""
    return next(i for i in range(len(closes)) if v1.signal("R2", closes, i))


def make_closes(cont=None, over=None) -> tuple[list[float], int, int]:
    """(收盘序列, j, e)。e = j+1 是入场日；cont(n, S_e) = 入场日之后第 n 天的收盘（n ≥ 1），缺省 = 平在入场日收盘。over = {下标: 收盘} 最后覆盖。"""
    c = base_closes()
    j = first_r2_day(c)
    e = j + 1
    s_e = c[e]
    for k in range(e + 1, len(c)):
        c[k] = cont(k - e, s_e) if cont else s_e
    for k, v in (over or {}).items():
        c[k] = v
    return c, j, e


def bs_quote(spot, day, exp, cp, K, sigma=0.20, half=0.02):
    T = (ordinal(exp) - ordinal(day)) / 365
    p = v2.bs_price(spot, K, T, 0.045, sigma, cp)
    return max(p - half, 0.0), p + half


def build_world(root: Path, closes: list[float], quote_fn=None) -> dict:
    """写 root/meta + root/cbbo1m/SPY。quote_fn(day, spot, exp, cp, K) -> (bid, ask) 或 None（该合约该天没有报价）；缺省 = BS 价 ± 0.02。
    返回 qd[(day, osi)] = (bid, ask)，测试用它独立算期望值。"""
    (root / "meta").mkdir(parents=True, exist_ok=True)
    cl = {d: closes[k] for k, d in enumerate(ALL_DAYS)}
    (root / "meta" / "closes_SPY_full.json").write_text(__import__("json").dumps(cl))
    quote_fn = quote_fn or (lambda day, spot, exp, cp, K: bs_quote(spot, day, exp, cp, K))
    qd, by_month = {}, {}
    for k, d in enumerate(ALL_DAYS):
        if d < "2024-01-02":
            continue
        ts = pd.Timestamp(f"{d} 15:59", tz=ET).tz_convert("UTC")
        o = ordinal(d)
        for e in FRIDAYS:
            if not 1 <= ordinal(e) - o <= 62:
                continue
            for K in STRIKES:
                for cp in ("C", "P"):
                    r = quote_fn(d, closes[k], e, cp, K)
                    if r is None:
                        continue
                    sym = osi(e, cp, K)
                    qd[(d, sym)] = r
                    by_month.setdefault(d[:7], []).append({"ts": ts, "symbol": sym, "bid_px": r[0], "ask_px": r[1], "bid_sz": 1, "ask_sz": 1})
    dd = root / "cbbo1m" / "SPY"
    dd.mkdir(parents=True, exist_ok=True)
    for m, rows in by_month.items():
        pd.DataFrame(rows).to_parquet(dd / f"{m}.parquet", index=False)
    return qd


def proposal(**kw):
    p = {"why": "sell call premium after a downtrend, capped by the 1.08 call (bear call credit spread)", "underlyings": ["SPY"], "dte": 38,
         "legs": [{"side": "sell", "type": "call", "strike_pct": 1.03}, {"side": "buy", "type": "call", "strike_pct": 1.08}],
         "entry": [{"name": "trend_R2", "value": True}]}
    p.update(kw)
    return p


def pick_strike(spot: float, pct: float) -> int:
    """引擎的选行权价规则（离 现价×pct 最近，并列取小）的独立写法。"""
    return sorted(STRIKES, key=lambda s: (abs(s - spot * pct), s))[0]


def run_world(tmp_path, closes, quote_fn=None, **pkw):
    """-> (trades, qd, days)。走 validate_proposal -> _simulate（开发段口径）。"""
    root = tmp_path / "data" / "opra"
    qd = build_world(root, closes, quote_fn)
    env = lab.LabEnv.at(tmp_path / "data")
    norm, why = lab.validate_proposal(proposal(**pkw))
    assert norm, why
    sim = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
    return sim["trades"], qd, ALL_DAYS


def legs_of(trade_expiry: str, s_entry: float):
    """(短腿符号, 长腿符号, 短腿行权价, 长腿行权价)"""
    ks, kl = pick_strike(s_entry, 1.03), pick_strike(s_entry, 1.08)
    return osi(trade_expiry, "C", ks), osi(trade_expiry, "C", kl), ks, kl


def expected_entry(qd, day, s_sym, l_sym):
    """主口径入场：卖按 bid×(1−1%)，买按 ask×(1+1%)。返回每股净权利金。"""
    sb, _ = qd[(day, s_sym)]
    _, la = qd[(day, l_sym)]
    return sb * (1 - SLIP) - la * (1 + SLIP)


def raw_cost(qd, day, s_sym, l_sym):
    """平仓成本（每股，不含滑点）：买回短腿 ask − 卖出长腿 bid。"""
    return qd[(day, s_sym)][1] - qd[(day, l_sym)][0]


# ---------------------------------------------------------------- 1. 校验与分类
def test_validate_classifies_vertical_credit_with_default_credit_exits():
    norm, why = lab.validate_proposal(proposal(underlyings="index_like"))
    assert norm, why
    assert norm["structure"] == "vertical" and norm["kind"] == "credit"
    assert norm["underlyings"] == ["QQQ", "SMH", "SOXX", "SPY"]
    assert norm["dte"] == 38 and norm["cooldown_days"] == 1
    assert norm["conditions"] == [{"name": "trend_R2", "value": True}]
    assert norm["exit"] == {"mode": "standard", "exit_dte": 7, "take_profit": 0.5, "stop_loss": 2.0, "max_hold_days": 25}
    s = lab.to_strategy(norm)
    assert s["entry"] == "conditions" and not s.get("net_debit_ok")
    assert (s["take_profit_frac_of_credit"], s["stop_loss_mult_of_credit"], s["exit_dte"], s["max_hold_days"], s["cooldown_days"]) == (0.5, 2.0, 7, 25, 1)
    assert s["expiry"] == {"min_dte": 31, "max_dte": 45, "target_dte": 38}
    assert sorted((l["side"], l["type"], l["strike_pct"]) for l in s["legs"]) == [("buy", "call", 1.08), ("sell", "call", 1.03)]
    assert lab._classify(norm["legs"]) == ("vertical", "credit")


def test_sold_call_only_is_rejected_as_naked_short(tmp_path):
    only_sold = proposal(legs=[{"side": "sell", "type": "call", "strike_pct": 1.03}])
    norm, why = lab.validate_proposal(only_sold)
    assert norm is None and "naked_short" in why
    # 两条卖腿只有一条买腿封顶：同类型卖腿数 > 买腿数，仍是裸卖
    two_sold = proposal(legs=[{"side": "sell", "type": "call", "strike_pct": 1.03}, {"side": "sell", "type": "call", "strike_pct": 1.05},
                              {"side": "buy", "type": "call", "strike_pct": 1.08}])
    assert lab.validate_proposal(two_sold)[0] is None
    # 买的是 put 不能给卖 call 封顶（类型必须相同）
    wrong_type = proposal(legs=[{"side": "sell", "type": "call", "strike_pct": 1.03}, {"side": "buy", "type": "put", "strike_pct": 0.92}])
    n3, why3 = lab.validate_proposal(wrong_type)
    assert n3 is None and ("naked_short" in why3 or "structure_not_allowed" in why3)
    # 反过来（卖远买近）= 借方价差，不是熊市看涨信用价差
    rev = proposal(legs=[{"side": "buy", "type": "call", "strike_pct": 1.03}, {"side": "sell", "type": "call", "strike_pct": 1.08}])
    n4, why4 = lab.validate_proposal(rev)
    assert n4 and (n4["structure"], n4["kind"]) == ("vertical", "debit")
    # run_dev 层面：无效方案记日志、不记试验、不回测
    env = lab.LabEnv.at(tmp_path / "data")
    r = lab.run_dev(only_sold, "bc2", env)
    assert not r["valid"] and "naked_short" in r["reason"] and r["hash"] is None and r["points"] == -1.0
    assert lab.n_trials(env) == 60


# ---------------------------------------------------------------- 2. 入场权利金、手续费、最大亏损（手写常数报价，精确到分）
def test_entry_credit_fees_and_max_loss_exact(tmp_path):
    closes, j, e = make_closes()
    s_e = closes[e]
    ks, kl = pick_strike(s_e, 1.03), pick_strike(s_e, 1.08)
    assert (s_e, ks, kl) == (94.0, 97, 102)   # 现价 94：94×1.03=96.8 -> 97，94×1.08=101.5 -> 102（宽度 5）

    def qfn(day, spot, exp, cp, K):                 # 常数报价：短腿 2.00/2.10，长腿 0.50/0.56，其它行权价是不会被选中的噪声
        if cp == "C" and K == ks:
            return (2.00, 2.10)
        if cp == "C" and K == kl:
            return (0.50, 0.56)
        return (0.20, 0.24)
    trades, qd, days = run_world(tmp_path, closes, qfn)
    t = trades[0]
    assert t["entry_date"] == days[e] and t["kind"] == "credit"
    assert t["legs"] == f"卖C{ks} / 买C{kl}"
    # 入场：卖短腿 bid 2.00×0.99，买长腿 ask 0.56×1.01
    net = 2.00 * (1 - SLIP) - 0.56 * (1 + SLIP)
    assert t["net_ps"] == pytest.approx(net, abs=1e-4) and net == pytest.approx(1.4144)
    width = kl - ks
    assert t["max_loss"] == pytest.approx(MULT * (width - net), abs=0.01)           # (宽度 5 − 权利金 1.4144)×100 = 358.56
    assert t["max_loss"] == pytest.approx(358.56, abs=0.01)
    # 报价从不变：不触发止盈 / 止损，到 exit_dte（或 max_hold）平仓；出场买短腿 ask×1.01、卖长腿 bid×0.99，4 次 $0.65
    assert t["reason"] in ("exit_dte", "max_hold")
    exit_cost = 2.10 * (1 + SLIP) - 0.50 * (1 - SLIP)
    expected = MULT * (net - exit_cost) - 4 * FEE
    assert t["pnl"] == pytest.approx(expected, abs=0.01) and t["pnl"] == pytest.approx(-23.76, abs=0.01)
    assert t["ret"] == pytest.approx(t["pnl"] / t["max_loss"])
    # 入场当天的报价只来自当天 15:59（独立再算一遍：权利金 = 卖腿 bid − 买腿 ask，各扣 1% 滑点）
    s_sym, l_sym, _, _ = legs_of(t["expiry"], s_e)
    assert expected_entry(qd, t["entry_date"], s_sym, l_sym) == pytest.approx(net)
    # 手续费是每张每条腿每次 $0.65：把手续费设为 0 的反事实，pnl 恰好多 4×0.65
    spec0 = {**SPEC, "fill": {**SPEC["fill"], "fee_per_contract": 0.0}}
    env = lab.LabEnv.at(tmp_path / "data")
    norm, _ = lab.validate_proposal(proposal())
    t0 = lab._simulate(norm, env, spec0, lab.DEV_START, lab.DEV_END)["trades"][0]
    assert t0["pnl"] - t["pnl"] == pytest.approx(4 * FEE, abs=0.01)


# ---------------------------------------------------------------- 3. 标的上涨冲破 1.08：止损先触发、亏损封顶
def test_gradual_rally_stop_at_2x_credit_is_honored(tmp_path):
    closes, j, e = make_closes(cont=lambda n, s: s + 1.5 * n)     # 每天涨 1.5：慢慢冲过短腿，再冲过长腿
    trades, qd, days = run_world(tmp_path, closes)
    t = trades[0]
    s_e = closes[e]
    s_sym, l_sym, ks, kl = legs_of(t["expiry"], s_e)
    net = expected_entry(qd, days[e], s_sym, l_sym)
    assert t["net_ps"] == pytest.approx(net, abs=1e-4)
    # 独立找“第一个 平仓成本 − 权利金 ≥ 2×权利金”的日子（也确认没有更早的止盈）
    first_stop = None
    for k in range(e + 1, len(days)):
        c = raw_cost(qd, days[k], s_sym, l_sym)
        assert not (c <= 0.5 * net + 1e-9 and first_stop is None), "止盈条件不该在上涨路径上先成立"
        if c - net >= 2 * net - 1e-9:
            first_stop = k
            break
    assert first_stop is not None and first_stop > e + 1                  # 不是第一天就爆（这条路径是“逐步”冲过去）
    assert t["reason"] == "stop_loss" and t["exit_idx"] == first_stop and t["exit_date"] == days[first_stop]
    # 前几天都没到止损线
    for k in range(e + 1, first_stop):
        assert raw_cost(qd, days[k], s_sym, l_sym) - net < 2 * net - 1e-9
    # 亏损 ≈ 2×权利金 + 摩擦，远小于最大亏损；并且一定是亏
    assert t["pnl"] < 0
    assert t["pnl"] <= -(2 * net * MULT) + 4 * FEE            # 至少亏到止损线（成交价比报价差一点，所以是 ≤）
    assert t["pnl"] >= -t["max_loss"] - 4 * FEE - 5.0         # 没有越过最大亏损（加摩擦容差）
    assert t["pnl"] > -0.97 * t["max_loss"]                   # 止损在到期价外 / 刚进价内时触发，没有等到亏满


def test_gap_through_long_strike_loss_capped_at_max_loss(tmp_path):
    closes, j, e = make_closes(cont=lambda n, s: s * 1.15)          # 入场后第一天直接跳空到 1.15 倍，远在 1.08 之上
    trades, qd, days = run_world(tmp_path, closes)
    t = trades[0]
    s_e = closes[e]
    s_sym, l_sym, ks, kl = legs_of(t["expiry"], s_e)
    assert closes[e + 1] > kl > ks
    assert t["reason"] == "stop_loss" and t["exit_idx"] == e + 1      # 止损在第一个能平的日子触发（不会拖）
    net = t["net_ps"]
    # 亏损 ≈ 价差满额：不会少于止损线，也不会超过 最大亏损 + 手续费 + 出场滑点（摩擦）
    assert t["pnl"] <= -(2 * net * MULT)
    assert t["pnl"] < -0.9 * t["max_loss"]
    exit_cost = (qd[(days[e + 1], s_sym)][1] * (1 + SLIP) - qd[(days[e + 1], l_sym)][0] * (1 - SLIP))
    assert t["pnl"] == pytest.approx(MULT * (net - exit_cost) - 4 * FEE, abs=0.01)
    # max_loss = (宽度 − 权利金)×100 不含手续费，也不含出场滑点；出场滑点是每条腿权利金的 1%（深度实值腿的权利金很大），所以 pnl 允许比 −max_loss 再低
    # “手续费 + 出场滑点”这么多，不能更多（定价口径，见 BC-2 报告的“待裁定”一条）
    exit_slip = MULT * SLIP * (qd[(days[e + 1], s_sym)][1] + qd[(days[e + 1], l_sym)][0])
    assert t["pnl"] >= -t["max_loss"] - 4 * FEE - exit_slip - 0.05


def test_stop_honored_before_later_spike_can_do_more_damage(tmp_path):
    """先涨一点、第 3 天过止损线、第 4 天再暴涨 +30：止损在第 3 天执行，后面的暴涨不进这笔的亏损。"""
    path = {1: 0.0, 2: 4.0, 3: 9.0}
    closes, j, e = make_closes(cont=lambda n, s: s + path.get(n, 30.0))
    trades, qd, days = run_world(tmp_path, closes)
    t = trades[0]
    s_sym, l_sym, ks, kl = legs_of(t["expiry"], closes[e])
    net = expected_entry(qd, days[e], s_sym, l_sym)
    assert raw_cost(qd, days[e + 2], s_sym, l_sym) - net < 2 * net            # 第 2 天还没到线
    assert raw_cost(qd, days[e + 3], s_sym, l_sym) - net >= 2 * net           # 第 3 天到线
    assert t["reason"] == "stop_loss" and t["exit_idx"] == e + 3
    spike = MULT * (net - (raw_cost(qd, days[e + 4], s_sym, l_sym) + 0.0))    # 如果拖到第 4 天会是什么
    assert t["pnl"] > spike and t["pnl"] <= -(2 * net * MULT) + 4 * FEE


# ---------------------------------------------------------------- 4. 下跌 / 横盘：止盈或时间价值衰减，盈亏为正
def test_falling_underlying_hits_take_profit_at_half_credit(tmp_path):
    closes, j, e = make_closes(cont=lambda n, s: s - 0.5 * n)
    trades, qd, days = run_world(tmp_path, closes)
    t = trades[0]
    s_sym, l_sym, ks, kl = legs_of(t["expiry"], closes[e])
    net = expected_entry(qd, days[e], s_sym, l_sym)
    assert t["reason"] == "take_profit" and t["pnl"] > 0
    k = t["exit_idx"]
    assert raw_cost(qd, days[k], s_sym, l_sym) <= 0.5 * net + 1e-9          # 出场日平仓成本 ≤ 权利金的 50%
    for kk in range(e + 1, k):                                              # 之前没有一天够得着止盈线
        assert raw_cost(qd, days[kk], s_sym, l_sym) > 0.5 * net - 1e-9
    assert t["pnl"] <= MULT * net - 4 * FEE + 1e-6                          # 不可能赚得比“全部权利金 − 手续费”多
    assert t["pnl"] > 0.3 * MULT * net - 4 * FEE                            # 至少收回将近一半
    assert t["ret"] > 0


def test_flat_underlying_decays_profitably(tmp_path):
    closes, j, e = make_closes()                                           # 入场后价格不动
    trades, qd, days = run_world(tmp_path, closes)
    t = trades[0]
    s_sym, l_sym, ks, kl = legs_of(t["expiry"], closes[e])
    net = expected_entry(qd, days[e], s_sym, l_sym)
    assert t["reason"] in ("take_profit", "exit_dte", "max_hold") and t["pnl"] > 0 and t["ret"] > 0
    assert t["pnl"] <= MULT * net - 4 * FEE + 1e-6
    # 出场价独立算：买回短腿 ask×1.01、卖出长腿 bid×0.99
    k = t["exit_idx"]
    cost = qd[(days[k], s_sym)][1] * (1 + SLIP) - qd[(days[k], l_sym)][0] * (1 - SLIP)
    assert t["pnl"] == pytest.approx(MULT * (net - cost) - 4 * FEE, abs=0.01)


# ---------------------------------------------------------------- 5. trend_R2 只用 d−1 及以前的收盘
def _ctx(closes):
    return lab.LabCtx("SPY", ALL_DAYS, list(closes), list(closes), None, SPEC)


def test_trend_r2_decision_uses_only_closes_up_to_previous_day():
    closes, j, e = make_closes()
    norm, _ = lab.validate_proposal(proposal())
    s = lab.to_strategy(norm)
    base = _ctx(closes)
    assert v1.signal("R2", closes, j) and not any(v1.signal("R2", closes, k) for k in range(j))
    assert lab.cond_signal(s, base, j) is False             # 信号在 j 收盘才成立：j 当天还不能据此入场
    assert lab.cond_signal(s, base, e) is True              # 入场日是 j+1
    # d 当天的收盘暴涨：v1 的“含当天”信号会翻成 False，研究室的决策不变（仍按 d−1 判断 = True）
    c2 = list(closes)
    c2[e] = closes[e] * 1.5
    assert v1.signal("R2", c2, e) is False
    assert lab.cond_signal(s, _ctx(c2), e) is True
    # d 当天以及以后的收盘全部乱改，结果也不变
    c3 = list(closes)
    c3[e:] = [1e9] * (len(c3) - e)
    assert lab.cond_signal(s, _ctx(c3), e) is True
    # 反过来：d−1 = j 的收盘被改成平的（信号消失），则 d=e 的决策翻成 False（决策确实在读 d−1）
    c5 = list(closes)
    c5[j] = 100.0
    assert v1.signal("R2", c5, j) is False
    assert lab.cond_signal(s, _ctx(c5), e) is False


def test_trend_r2_entry_day_is_day_after_first_signal_and_ignores_entry_day_close(tmp_path):
    closes, j, e = make_closes()
    over_c, _, _ = make_closes(over={e: closes[e] * 1.05})    # 入场日当天收盘涨 5%（含当天价的信号会在这天变假；行权价仍在合成链 85–125 内）
    assert v1.signal("R2", over_c, e) is False
    trades, qd, days = run_world(tmp_path, over_c)
    assert trades and trades[0]["entry_date"] == days[e]      # 照样在 j+1 入场
    assert all(t["entry_date"] != days[j] for t in trades)     # j 当天（信号刚成立的那天）不入场
    # 入场用 d 当天的价格（收盘 15:59 报价）：现价取入场日自己的（被抬高后的）收盘来选行权价
    ks_expected = pick_strike(over_c[e], 1.03)
    assert f"{ks_expected:g}" in trades[0]["legs"]


# ---------------------------------------------------------------- 6. 出场日缺报价：按更差的一边记，不按最好情况
def _tp_day_world(tmp_path):
    closes, j, e = make_closes(cont=lambda n, s: s - 0.5 * n)
    trades, qd, days = run_world(tmp_path / "base", closes)
    t = trades[0]
    assert t["reason"] == "take_profit"
    return closes, j, e, t, qd, days


def test_missing_long_leg_quote_on_take_profit_day_defers_and_marks_worse_way(tmp_path):
    (tmp_path / "base").mkdir()
    closes, j, e, t, qd, days = _tp_day_world(tmp_path)
    k = t["exit_idx"]
    s_sym, l_sym, ks, kl = legs_of(t["expiry"], closes[e])
    drop = (days[k], l_sym)

    def qfn(day, spot, exp, cp, K):
        if (day, osi(exp, cp, K)) == drop:
            return None
        return bs_quote(spot, day, exp, cp, K)
    (tmp_path / "miss").mkdir()
    trades2, qd2, _ = run_world(tmp_path / "miss", closes, qfn)
    t2 = trades2[0]
    assert drop not in qd2
    # 缺腿日不能止盈（长腿没有 bid = 不知道能卖多少）：出场推迟到所有腿都有报价的下一个交易日以后
    assert t2["exit_idx"] > k and t2["entry_date"] == t["entry_date"]
    # 盯市取“上一个标记 vs 下界”里更差的：缺报价那天的 mtm 不比前一天好，而对照（有报价）那天明显更好
    d_prev, d_k = days[k - 1], days[k]
    assert t2["mtm"][d_k] <= t2["mtm"][d_prev] + 1e-9
    assert t["mtm"][d_k] > t2["mtm"][d_k]
    # 缺报价的那次出场没有发生在“最好情况”价上：它不会比对照组更赚
    assert t2["reason"] == "take_profit" and t2["exit_idx"] == k + 1 or t2["pnl"] <= t["pnl"] + 1e-9   # 之后的某天重新够到止盈线也行，但成交价是那天的真实报价
    assert t2["missing_exit_legs"] == 0                                        # 实际出场日所有腿都有报价


def test_gap_up_with_missing_short_leg_stops_at_worse_of_bound_and_stop_price(tmp_path):
    closes, j, e = make_closes(cont=lambda n, s: s * 1.15)
    s_sym_base = None
    # 先拿到短腿符号：按独立规则推
    ks = pick_strike(closes[e], 1.03)
    kl = pick_strike(closes[e], 1.08)

    def qfn(day, spot, exp, cp, K):
        # 入场后第一天（跳空日）短腿没有任何报价；长腿正常
        if day == ALL_DAYS[e + 1] and cp == "C" and K == ks:
            return None
        return bs_quote(spot, day, exp, cp, K)
    trades, qd, days = run_world(tmp_path, closes, qfn)
    t = trades[0]
    assert t["entry_date"] == days[e]
    # 内在价值下界已经击穿止损线 -> stop_on_missing，按 max(下界+手续费, 止损价) 封顶最大亏损平仓；绝不是“没数据就不动 / 按最好情况”
    assert t["reason"] == "stop_on_missing" and t["exit_idx"] == e + 1
    net = t["net_ps"]
    assert t["pnl"] <= -(2 * net * MULT)                            # 至少亏到止损线
    assert t["pnl"] >= -t["max_loss"] - 2 * FEE - 0.01              # 封顶在最大亏损（+ 入场手续费，出场手续费已被 min(L, …) 吞掉）
    width = kl - ks
    assert width > 0 and t["pnl"] == pytest.approx(-t["max_loss"] - 2 * FEE, abs=0.02)   # 下界 = 价差满额，所以正好是最大亏损


def test_no_quotes_ever_after_entry_books_max_loss_not_last_mark(tmp_path):
    closes, j, e = make_closes(cont=lambda n, s: s - 0.5 * n)         # 路径本来是赚的
    ks = pick_strike(closes[e], 1.03)

    def qfn(day, spot, exp, cp, K):
        if day > ALL_DAYS[e] and cp == "C" and K == ks:               # 入场后短腿再也没有报价
            return None
        return bs_quote(spot, day, exp, cp, K)
    trades, qd, days = run_world(tmp_path, closes, qfn)
    t = trades[0]
    assert t["reason"] == "no_more_quotes"
    assert t["pnl"] == pytest.approx(-t["max_loss"] - 2 * FEE, abs=0.01)   # 按最大亏损记（含入场 2 条腿手续费），不是赚钱的那个“最后标记”
    assert t["pnl_lastmark"] >= t["pnl"]                                    # 对照栏：最后标记不会比保守口径更差


def test_exit_dte_day_missing_quote_defers_instead_of_exiting_at_best_price(tmp_path):
    closes, j, e = make_closes()
    ks, kl = pick_strike(closes[e], 1.03), pick_strike(closes[e], 1.08)

    def const(day, spot, exp, cp, K):
        if cp == "C" and K == ks:
            return (2.00, 2.10)
        if cp == "C" and K == kl:
            return (0.50, 0.56)
        return (0.20, 0.24)
    (tmp_path / "a").mkdir()
    trades, qd, days = run_world(tmp_path / "a", closes, const)
    t = trades[0]
    # 独立算时间出场日：min(满 25 个交易日, 离到期 ≤ 7 个日历日)
    exp_o = ordinal(t["expiry"])
    kd = next(k for k in range(e + 1, len(days)) if exp_o - ordinal(days[k]) <= 7)
    expected_k = min(kd, e + 25)
    assert t["exit_idx"] == expected_k and t["reason"] in ("exit_dte", "max_hold")
    l_sym = osi(t["expiry"], "C", kl)

    def const_missing(day, spot, exp, cp, K):
        if day == days[expected_k] and osi(exp, cp, K) == l_sym:
            return None
        return const(day, spot, exp, cp, K)
    (tmp_path / "b").mkdir()
    t2 = run_world(tmp_path / "b", closes, const_missing)[0][0]
    assert t2["exit_idx"] == expected_k + 1 and t2["reason"] == t["reason"]    # 顺延到下一个所有腿都有报价的交易日
    assert t2["missing_exit_legs"] == 0 and t2["pnl"] == pytest.approx(t["pnl"], abs=0.01)  # 常数报价：顺延不改变价格，但也没在缺报价的日子成交
