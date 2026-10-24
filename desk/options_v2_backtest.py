"""卡 B2：预先登记的美股期权策略 v2（knowledge/options_strategies_v2.toml）的真实报价回测引擎。

规格（价差腿、IV/RV 定义、成交口径、出场、分段、过关标准、仓位档位）全部读 toml，本文件不改任何数字。
复用 v1（desk/options_rules_backtest.py）的 Quotes / read_month / parse_osi / read_closes / fetch_closes / clusters / boot_p /
tstat / seg_of / extra-needed 两遍机制；数据与 v1 相同（opra_fetch 下的 data/opra/cbbo1m/<UND>/<YYYY-MM>.parquet + 补数据）。

口径与解释（都写进报告；toml 没写死的地方按保守方向取）：
- 现价 = 当天 IEX 日线不复权收盘（v1 同样当 15:59 的代理）；IV30 的 Black–Scholes 反解、选行权价都用它。
  RV20 只用 d−21 … d−1 的拆股调整收盘（共 20 个日对数收益 d−20 … d−1，样本标准差 ×√252），d 当天的收盘不进 RV。
- IV30：当天 ≥15:50 ET（半天市 12:50）的最后一条报价中间价；到期最接近 30 天的两个到期日各取最接近现价的 call / put 反解后平均
  （只有一边能反解就用那一边），再按到期天数线性插值到 30 天；30 天落在两个到期日同一侧时不外推，取离 30 天近的那个。
  利率固定 4.5%，不考虑分红；T = 日历天数 / 365。
- 腿选择：每笔一个到期日（[expiry]：离 38 天最近；财报单：14–45 天里最近的那个）；每条腿取该到期日有报价的行权价里最接近 现价 × strike_pct 的；
  同一个 strike_pct 的腿共用同一行权价（跨式 / 铁蝶中间腿，取 call 和 put 都有报价的行权价里最接近现价的）；
  同类型（都是 put 或都是 call）的腿行权价必须和 strike_pct 同序且互不相同，否则整单跳过（wing_unavailable）。
- 流动性（每条腿，入场和止盈出场）：(ask−bid) ≤ max(15% × 中间价, $0.05)，且报价 ≥15:50 ET；卖腿还要有买价，买腿要有卖价，否则跳过并记原因。
  止损 / 到期 / 持满天数 / 财报次日出场不做流动性检查，照主口径成交（卖在 bid、买在 ask）。
- 主口径成交：卖按 bid×(1−1%)，买按 ask×(1+1%)，每张每条腿每次 $0.65（开仓一次、平仓一次，到期前平仓的腿都收）；
  参考口径（仅参考）：中间价 ± f × 半价差（f = 0.75 / 0.5），同样的 1% 滑点和手续费，走和主口径同一条路径（同样的入场日、出场日、出场原因）。
- 决策用的价格不含手续费：权利金 credit（净收）/ debit（净付）= 入场成交价（含 1% 滑点）；平仓成本 = 空头腿 ask 之和 − 多头腿 bid 之和（原始报价，不含滑点和手续费）。
  止盈（信用）：平仓成本 ≤ (1 − take_profit_frac_of_credit) × credit（0.5 时与 toml 注释“≤ 收到权利金的 50%”一致）；
  止损（信用）：平仓成本 − credit ≥ stop_loss_mult_of_credit × credit；
  止盈（借方）：价值 ≥ (1 + take_profit_frac_of_debit) × debit；止损：价值 ≤ (1 − stop_loss_frac_of_debit) × debit。
  同一天先判止盈（需流动性）再判止损，再判持满 / 到期；财报单第二个交易日收盘无条件平。
- 最大亏损（每张 ×100，不含手续费）：信用价差 = 宽度 − credit；铁鹰 / 铁蝶 = max(两边宽度) − credit；借方结构 = debit。（代码用到期收益曲线的最小值统一算）。
  单笔收益率 = 盈亏 ÷ 最大亏损。
- 缺报价（两遍流程）：平仓日某条腿没有报价 -> 顺延到所有腿都有报价的下一个交易日（财报单例外：次日不顺延，缺的腿按卖腿 bid=0 / 买回腿无卖价处理）；
  多头腿无买价按 bid=0；空头腿买回时无卖价 -> 该价差按最大亏损平仓并单独计数（n_no_ask_close）；补数据后仍一直没有完整报价 ->
  按最大亏损（借方 = 付出的权利金全亏）记，reason = no_more_quotes，单独计数，pnl_lastmark 只是按最后一次完整报价平仓的参考列；
  数据末尾仍开着的仓位按最后一次完整报价记（open_at_end）。
- 冷却：平仓后的下一个交易日起可再开（cooldown_days = 1）；cooldown_days = 5 的 F3 要等 5 个交易日。max_hold_days 按交易日数。
- 财报：knowledge/earnings_history.json（[{"ticker","date","timing":"amc|bmo","source"}]）；amc 当天收盘进，bmo 前一交易日收盘进，
  次一交易日收盘出。文件不存在 -> F4 / F4c 报“没有财报日历”并跳过。
- 仓位换算：每笔风险 = 档位 × 当时账户净值，张数允许小数；同时持仓 ≤ max_open；同一天开的 index_like 标的持仓（含跨式）合计 ≤ 档位 × correlated_cap_mult 笔。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import tomllib
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .options_rules_backtest import (SAMPLE_START, TEST_START, TRAIN_END, Quotes, boot_p, clusters, fetch_closes,
                                     fnum, pct, read_closes, read_month, seg_of, split_warnings, tstat, usd, write_extra_needed)

ROOT_DIR = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT_DIR / "knowledge" / "options_strategies_v2.toml"
EARNINGS_PATH = ROOT_DIR / "knowledge" / "earnings_history.json"
EXPECTED_SPEC_SHA16 = "9531c41e7262e5a1"          # P2 登记的 sha256 前 16 位；不一致 = toml 被改过，报告里警告
NEEDED_FILE = "extra_needed_v2.json"
EXTRA_SUB_V2 = "cbbo1m_extra_v2"
EXTRA_SUBS = ("cbbo1m_extra", EXTRA_SUB_V2)       # v1 的补数据也读（同一 (合约,日) 以主数据优先）
CRASH_WINDOWS = {"2024-08-01..2024-08-16": ("2024-08-01", "2024-08-16"), "2025-04-01..2025-04-30": ("2025-04-01", "2025-04-30")}
N_TRIALS = 60
BONF = 0.05 / N_TRIALS
TOO_GOOD_RET = 0.40
TOO_GOOD_WIN = 0.95
TOO_GOOD_MIN_N = 30
TOO_GOOD_SPREAD_WIN = 0.90          # 价差类另加（裁定 2026-10-25）：胜率 > 90% 且 ≥20 笔且每笔平均 > 最大亏损的 25%
TOO_GOOD_SPREAD_N = 20
TOO_GOOD_SPREAD_RET = 0.25
INCOMPLETE_FRAC = 0.05              # 某组 > 5% 的笔以 no_more_quotes / stop_on_missing 收尾 -> 整份报告“未完成”
KELLY_BIG_LOSS = 0.90               # 有一笔亏掉 ≥90% 最大亏损 -> 凯利封顶降一档
INT_ACCOUNT = 5000.0                # 整数张仿真的小账户
MULT = 100
E0 = 100_000.0
EPS = 1e-9

REASON_ZH = {"take_profit": "止盈", "stop_loss": "止损", "max_hold": "持满天数", "exit_dte": "临近到期", "next_session": "财报后第一个交易日收盘",
             "no_more_quotes": "之后一直没有完整报价，按最大亏损记", "open_at_end": "到数据末尾还没平，按最后一次完整报价算"}
ZH_SLICE = {"train": "训练段", "test": "考试段", "2024-08-01..2024-08-16": "暴跌窗口 2024-08", "2025-04-01..2025-04-30": "暴跌窗口 2025-04"}
SKIP_ZH = [("no_signal_data", "算不出 IV/RV"), ("no_expiry", "没有合适到期日"), ("no_strike", "没有行权价"),
           ("wing_unavailable", "保护腿选不出（同一行权价/顺序不对）"), ("no_ask", "买腿没有卖价"), ("no_bid", "卖腿没有买价"),
           ("stale_quote", "报价早于15:50"), ("spread", "价差超流动性线"), ("no_credit", "收不到权利金"),
           ("bad_structure", "报价异常（权利金≥宽度）"), ("no_next_session", "数据里没有次日"), ("cooldown_or_open", "持仓/冷却期"),
           ("earnings_date_not_trading_day", "财报日不是交易日"), ("no_quotes", "当天没有报价")]


# ---------------------------------------------------------------- 规格
def load_spec(path: Path = SPEC_PATH) -> dict:
    with open(path, "rb") as f:
        d = tomllib.load(f)
    d["_sha16"] = spec_sha16(path)        # 非 toml 字段：读进来的文件的哈希，报告里和登记值对照
    return d


def spec_sha16(path: Path = SPEC_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def universe_of(strat: dict, spec: dict) -> list[str]:
    u = strat["underlyings"]
    if isinstance(u, list):
        return list(u)
    return list(spec["universe"][u])


# ---------------------------------------------------------------- Black–Scholes 与隐含波动
def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, cp: str) -> float:
    """欧式 BS，无分红。cp = 'C' / 'P'。T<=0 -> 内在价值；sigma<=0 -> 远期内在价值。"""
    if T <= 0:
        return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)
    disc = K * math.exp(-r * T)
    if sigma <= 0:
        return max(S - disc, 0.0) if cp == "C" else max(disc - S, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    if cp == "C":
        return S * _ncdf(d1) - disc * _ncdf(d2)
    return disc * _ncdf(-d2) - S * _ncdf(-d1)


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    return S * _npdf(d1) * math.sqrt(T)


def implied_vol(mid: float, S: float, K: float, T: float, r: float, cp: str) -> float | None:
    """牛顿 + 二分保护的反解。中间价不高于（折现后的）内在价值 / 高于上限 / 不收敛 -> None。
    用折现内在价值而不是裸内在价值当下限：深度实值的欧式 put 理论价本来就可以低于 K−S。"""
    vals = (mid, S, K, T, r)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals) or mid <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    disc = K * math.exp(-r * T)
    lo_p = max(S - disc, 0.0) if cp == "C" else max(disc - S, 0.0)     # BS 的“内在价值”= 折现后的内在价值（波动→0 时的价格）
    hi_p = S if cp == "C" else disc
    if mid <= lo_p + 1e-12 or mid >= hi_p - 1e-12:                      # 中间价不高于内在价值 / 不低于上限 -> 无解
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(S, K, T, r, hi, cp) < mid or bs_price(S, K, T, r, lo, cp) > mid:
        return None
    x = 0.3
    for _ in range(200):
        diff = bs_price(S, K, T, r, x, cp) - mid
        if abs(diff) < 1e-10:
            return x
        if diff > 0:
            hi = x
        else:
            lo = x
        v = bs_vega(S, K, T, r, x)
        nx = x - diff / v if v > 1e-10 else 0.5 * (lo + hi)
        if not (lo < nx < hi):
            nx = 0.5 * (lo + hi)
        if abs(nx - x) < 1e-14:
            x = nx
            break
        x = nx
    return x if abs(bs_price(S, K, T, r, x, cp) - mid) < 1e-6 else None


def rv20(adj: list[float], i: int, window: int = 20) -> float | None:
    """第 i 个交易日的 RV20：日对数收益 d−20 … d−1（共 window 个，需要 d−21 … d−1 的收盘），样本标准差 ×√252。
    只读 adj[i-window-1 : i]，d 当天及以后的收盘不影响结果。"""
    if i < window + 1:
        return None
    seg = adj[i - window - 1:i]
    if any((not math.isfinite(v)) or v <= 0 for v in seg):
        return None
    r = np.diff(np.log(np.asarray(seg, float)))
    return float(r.std(ddof=1) * math.sqrt(252))


def iv_at(q: Quotes, day: int, spot: float, spec: dict, tgt: int) -> tuple[float | None, tuple[int, ...]]:
    """(IV, 用到的到期天数们)。toml [vol] 的 IV30 定义推广到任意目标天数 tgt：到期最接近 tgt 天的两个到期日，各取最接近现价的 call / put
    （新鲜报价的中间价）反解后平均，按到期天数线性插值到 tgt 天（卡 L1 的期限斜率用 tgt=55 与 30；tgt=30 时与 v2 的 IV30 逐位相同）。"""
    v = spec["vol"]
    rows = q.day_rows(day)
    if rows.empty:
        return None, ()
    cut = q.stale_cut(day)
    rows = rows[(rows["qmin"] >= cut) & (rows["bid"] > 0) & rows["ask"].notna() & (rows["ask"] >= rows["bid"])]
    if rows.empty:
        return None, ()
    dte = rows["exp"] - day
    rows = rows[dte >= 1]
    if rows.empty:
        return None, ()
    exps = sorted(rows["exp"].unique(), key=lambda e: (abs((int(e) - day) - tgt), int(e)))[:2]
    pts = []
    for e in exps:
        ivs = []
        for cp in ("C", "P"):
            sub = rows[(rows["exp"] == e) & (rows["cp"] == cp)]
            if sub.empty:
                continue
            cand = sorted(sub["strike"].unique(), key=lambda s: (abs(s - spot), s))[:3]
            for k in cand:
                r = sub[sub["strike"] == k].iloc[0]
                iv = implied_vol((float(r["bid"]) + float(r["ask"])) / 2, spot, float(k), (int(e) - day) / 365.0, v["rate"], cp)
                if iv is not None:
                    ivs.append(iv)
                    break
        if ivs:
            pts.append((int(e) - day, sum(ivs) / len(ivs)))
    if not pts:
        return None, ()
    if len(pts) == 1:
        return pts[0][1], (pts[0][0],)
    (d1, i1), (d2, i2) = sorted(pts)
    if tgt <= d1:
        return i1, (d1,)
    if tgt >= d2:
        return i2, (d2,)
    w = (tgt - d1) / (d2 - d1)
    return i1 + w * (i2 - i1), (d1, d2)


def iv30(q: Quotes, day: int, spot: float, spec: dict) -> float | None:
    """toml [vol]：到期最接近 30 天的两个到期日，各取最接近现价的 call / put（新鲜报价的中间价）反解后平均，按到期天数线性插值到 30 天。"""
    return iv_at(q, day, spot, spec, spec["vol"]["iv_target_days"])[0]


# ---------------------------------------------------------------- 腿选择与流动性
def leg_liquid(bid: float, ask: float, fill: dict) -> bool:
    if not math.isfinite(ask) or ask <= 0:
        return False
    mid = (bid + ask) / 2
    if mid <= 0:
        return False
    return (ask - bid) <= max(fill["max_leg_spread_pct"] * mid, fill["min_leg_spread_abs"]) + EPS


def pick_expiry(exps: list[int], day: int, spec: dict, strat: dict) -> int | None:
    ov = strat.get("expiry_override")
    if ov:
        c = sorted(e for e in exps if ov["min_dte"] <= e - day <= ov["max_dte"])
        return c[0] if c else None           # pick = "nearest"：窗口内最近的那个到期日
    ex = strat.get("expiry") or spec["expiry"]          # 卡 L1：研究室方案自带到期窗口（target_dte ± 7，夹在 14–60 内）
    c = [e for e in exps if ex["min_dte"] <= e - day <= ex["max_dte"]]
    return sorted(c, key=lambda e: (abs((e - day) - ex["target_dte"]), e))[0] if c else None


def select_legs(q: Quotes, day: int, spot: float, strat: dict, spec: dict) -> tuple[list[dict] | None, str]:
    """返回 (legs, "") 或 (None, 原因)。legs 里每条腿带 side/type/pct/symbol/exp/strike/bid/ask/qmin。"""
    rows = q.day_rows(day)
    if rows.empty:
        return None, "no_quotes"
    e = pick_expiry([int(x) for x in rows["exp"].unique()], day, spec, strat)
    if e is None:
        return None, "no_expiry"
    rows = rows[rows["exp"] == e]
    specs = strat["legs"]
    # 同一 strike_pct 的腿共用行权价（跨式 / 铁蝶中间腿）：取这些腿的类型都有报价的行权价里最接近 现价×pct 的
    strike_of: dict[float, float] = {}
    for pct_ in sorted({l["strike_pct"] for l in specs}):
        types = {("C" if l["type"] == "call" else "P") for l in specs if l["strike_pct"] == pct_}
        ks = None
        for t in types:
            s = set(rows[rows["cp"] == t]["strike"].unique())
            ks = s if ks is None else ks & s
        if not ks:
            return None, "no_strike"
        strike_of[pct_] = sorted(ks, key=lambda s: (abs(s - spot * pct_), s))[0]
    # 同类型腿：行权价顺序必须和 strike_pct 一致且互不相同（保护腿不能和空头腿同一行权价）
    for t in ("call", "put"):
        pcs = sorted({l["strike_pct"] for l in specs if l["type"] == t})
        for a, b in zip(pcs, pcs[1:]):
            if not strike_of[a] < strike_of[b]:
                return None, "wing_unavailable"
    fill = spec["fill"]
    cut = q.stale_cut(day)
    legs = []
    for l in specs:
        t = "C" if l["type"] == "call" else "P"
        r = rows[(rows["cp"] == t) & (rows["strike"] == strike_of[l["strike_pct"]])].iloc[0]
        bid, ask, qm = float(r["bid"]), float(r["ask"]), int(r["qmin"])
        leg = {"side": l["side"], "type": t, "pct": l["strike_pct"], "symbol": str(r["symbol"]), "exp": int(e),
               "strike": float(strike_of[l["strike_pct"]]), "bid": bid, "ask": ask, "qmin": qm}
        if l["side"] == "buy" and (not math.isfinite(ask) or ask <= 0):
            return None, "no_ask"
        if l["side"] == "sell" and bid <= 0:
            return None, "no_bid"
        if qm < cut:
            return None, "stale_quote"
        if not leg_liquid(bid, ask, fill):
            return None, "spread"
        legs.append(leg)
    return legs, ""


# ---------------------------------------------------------------- 结构、成交、盈亏
def payoff_min(legs: list[dict]) -> float:
    """到期收益曲线（不含权利金）的最小值（<=0 的“最大负债”取负）。逐段线性，在 0、各行权价、很大的价格上取值即可。"""
    ks = [l["strike"] for l in legs]
    pts = sorted({0.0, *ks, max(ks) * 100.0})

    def pay(S):
        tot = 0.0
        for l in legs:
            v = max(S - l["strike"], 0.0) if l["type"] == "C" else max(l["strike"] - S, 0.0)
            tot += v if l["side"] == "buy" else -v
        return tot
    return min(pay(S) for S in pts)


def fill_price(side: str, bid: float, ask: float, mode, slip: float) -> float:
    """side=卖(sell)=成交在 bid 一侧，买(buy)在 ask 一侧；mode = 'primary' 或半价差比例 f。单边报价（无买价 / 无卖价）一律用主口径。"""
    if side == "sell":
        base = bid
        if mode != "primary" and bid > 0 and math.isfinite(ask):
            base = (bid + ask) / 2 - mode * (ask - bid) / 2
        return max(base, 0.0) * (1 - slip)
    base = ask
    if mode != "primary" and bid > 0 and math.isfinite(ask):
        base = (bid + ask) / 2 + mode * (ask - bid) / 2
    return base * (1 + slip)


def entry_net_ps(legs: list[dict], mode, slip: float) -> float:
    """每股净现金流：卖腿收入 − 买腿支出（含滑点，不含手续费）。>0 = 收 credit，<0 = 付 debit。"""
    tot = 0.0
    for l in legs:
        p = fill_price(l["side"], l["bid"], l["ask"], mode, slip)
        tot += p if l["side"] == "sell" else -p
    return tot


def cost_at(legs: list[dict], quotes: list, L_ps: float) -> tuple[float, bool]:
    """当天原始报价下的平仓成本（每股）：空头腿 ask 之和 − 多头腿 bid 之和。空头腿没有卖价 -> 按最大负债 L 计并返回 True。
    quotes 里 None（该腿当天没报价）：多头腿按 bid=0，空头腿按没有卖价。"""
    cost, flag = 0.0, False
    for l, qt in zip(legs, quotes):
        bid, ask = (qt[0], qt[1]) if qt is not None else (0.0, float("nan"))
        if l["side"] == "sell":
            if not math.isfinite(ask) or ask <= 0:
                flag = True
            else:
                cost += ask
        else:
            cost -= max(bid, 0.0)
    return (L_ps, True) if flag else (cost, False)


def settle(legs: list[dict], ex: dict, mode, fill: dict, L_ps: float) -> float:
    """一张合约（×100）的净盈亏 $。ex = {never, flag, quotes, synthetic_cost_ps}。"""
    slip, fee = fill["slippage_pct"], fill["fee_per_contract"]
    n = len(legs)
    cash = MULT * entry_net_ps(legs, mode, slip) - fee * n
    if ex.get("never"):
        return cash - MULT * L_ps
    if ex.get("synthetic_cost_ps") is not None:        # 缺腿日止损：平仓总成本（已含平仓手续费）由 find_exit 给出
        return cash - MULT * ex["synthetic_cost_ps"]
    if ex.get("flag"):
        return cash - MULT * L_ps - fee * n
    tot = 0.0
    for l, qt in zip(legs, ex["quotes"]):
        bid, ask = (qt[0], qt[1]) if qt is not None else (0.0, float("nan"))
        if l["side"] == "buy":
            tot += fill_price("sell", bid, ask, mode, slip)
        else:
            tot -= fill_price("buy", bid, ask, mode, slip)
    return cash + MULT * tot - fee * n


# ---------------------------------------------------------------- 出场
def leg_series(q: Quotes, legs: list[dict], entry_ord: int) -> list[dict]:
    out = []
    for l in legs:
        out.append({qd: (bid, ask, qmin) for qd, bid, ask, qmin in q.sym_quotes(l["symbol"], entry_ord)})
    return out


def lb_cost_ps(legs: list[dict], spot: float) -> float:
    """平仓成本下界（每股）：按当天标的收盘价算的结构到期内在价值的相反数 = Σ空头腿内在价值 − Σ多头腿内在价值。
    价差的真实平仓成本只会比它多一点时间价值差（深度实值时差几分钱），所以拿它当“至少要付这么多”的下界。"""
    tot = 0.0
    for l in legs:
        intr = max(spot - l["strike"], 0.0) if l["type"] == "C" else max(l["strike"] - spot, 0.0)
        tot += intr if l["side"] == "sell" else -intr
    return tot


def find_exit(strat: dict, legs: list[dict], ser: list[dict], entry_idx: int, ords: list[int], q: Quotes,
              net_ps: float, L_ps: float, fill: dict, spots: list[float] | None = None) -> dict:
    """entry_idx 之后逐个交易日检查。返回 {idx, reason, quotes, flag, never, marks{idx: 成本}, last_full, missing[, synthetic_cost_ps]}。
    所有出场路径（止盈 / 止损 / 持满 / 临近到期 / 财报次日）只认 ≥15:50 ET（半天市 12:50）的报价；过期报价算缺失。
    缺腿日（任何一条腿没有可用报价）：不做止盈；信用结构用当天收盘价算平仓成本下界，若下界已经击穿止损线，
    就按 max(下界 + 平仓手续费, 止损价估计) 平仓，封顶最大亏损（reason = stop_on_missing）；借方结构从内在价值推不出“已击穿”，
    只在盯市里按内在价值（更差者）估。时间到期（持满 / 临近到期）顺延到下一个所有腿都有可用报价的日子。"""
    n_days = len(ords)
    exp = legs[0]["exp"]
    earn = strat.get("exit") == "next_session_close"
    credit_kind = net_ps > 0
    max_hold, exit_dte = strat.get("max_hold_days"), strat.get("exit_dte")
    fee_ps = fill["fee_per_contract"] * len(legs) / MULT
    marks: dict[int, float] = {}
    last_full = None
    pending = None
    last_cost = cost_at(legs, [(l["bid"], l["ask"], 0) for l in legs], L_ps)[0]
    for k in range(entry_idx + 1, n_days):
        o = ords[k]
        if o > exp:
            break
        cut = q.stale_cut(o)
        qs = [x if (x is not None and x[2] >= cut) else None for x in (s.get(o) for s in ser)]
        n_missing = sum(x is None for x in qs)
        if earn:        # 公布后第一个交易日 15:59 无条件平；缺的腿按保守口径处理（不顺延）
            cost, flag = cost_at(legs, qs, L_ps)
            return {"idx": k, "reason": "next_session", "quotes": [(x[0], x[1]) if x else None for x in qs], "flag": flag,
                    "never": False, "marks": marks, "last_full": last_full, "missing": n_missing}
        tr = None
        if max_hold and k - entry_idx >= max_hold:
            tr = "max_hold"
        elif exit_dte is not None and exp - o <= exit_dte:
            tr = "exit_dte"
        if n_missing:
            lb = lb_cost_ps(legs, spots[k]) if spots is not None else None
            if lb is not None:
                if credit_kind and lb - net_ps >= strat["stop_loss_mult_of_credit"] * net_ps - EPS:
                    stop_px = (1 + strat["stop_loss_mult_of_credit"]) * net_ps
                    cost = min(L_ps, max(lb + fee_ps, stop_px))
                    return {"idx": k, "reason": "stop_on_missing", "quotes": None, "flag": False, "never": False,
                            "synthetic_cost_ps": cost, "marks": marks, "last_full": last_full, "missing": n_missing}
                last_cost = max(last_cost, lb)           # 盯市：上一个标记和内在价值下界里更差的那个
                marks[k] = last_cost
            if tr and pending is None:
                pending = tr             # 时间到了但有腿没有可用报价 -> 顺延到所有腿都有可用报价的下一个交易日
            continue
        cost, flag = cost_at(legs, qs, L_ps)
        marks[k] = last_cost = cost
        quotes = [(x[0], x[1]) for x in qs]
        last_full = (k, quotes)
        done = lambda why: {"idx": k, "reason": why, "quotes": quotes, "flag": flag, "never": False, "marks": marks,  # noqa: E731
                            "last_full": last_full, "missing": 0}
        if pending:
            return done(pending)
        good = all(leg_liquid(x[0], x[1], fill) for x in qs)       # 新鲜度上面已经筛过
        if credit_kind:
            if good and cost <= (1 - strat["take_profit_frac_of_credit"]) * net_ps + EPS:
                return done("take_profit")
            if cost - net_ps >= strat["stop_loss_mult_of_credit"] * net_ps - EPS:
                return done("stop_loss")
        else:
            debit, value = -net_ps, -cost
            if good and value >= debit * (1 + strat["take_profit_frac_of_debit"]) - EPS:
                return done("take_profit")
            if value <= debit * (1 - strat["stop_loss_frac_of_debit"]) + EPS:
                return done("stop_loss")
        if tr:
            return done(tr)
    if last_full and last_full[0] == n_days - 1:
        k, quotes = last_full
        cost, flag = cost_at(legs, [(a, b, 0) for a, b in quotes], L_ps)
        return {"idx": k, "reason": "open_at_end", "quotes": quotes, "flag": flag, "never": False, "marks": marks,
                "last_full": last_full, "missing": 0}
    # 之后再也没有完整可用报价：从“第一天开始没有报价”那天起按最大亏损记（不拖到到期）
    first_dark = (last_full[0] if last_full else entry_idx) + 1
    return {"idx": min(first_dark, n_days - 1), "reason": "no_more_quotes", "quotes": None, "flag": False,
            "never": True, "marks": marks, "last_full": last_full, "missing": 0}


# ---------------------------------------------------------------- 信号
def entry_signal(strat: dict, iv: float | None, rv: float | None) -> bool | None:
    """True / False；需要 IV 或 RV 但算不出来 -> None（记 no_signal_data）。"""
    e = strat["entry"]
    if e == "always":
        return True
    if e in ("iv_rv_ge", "iv_rv_le"):
        if iv is None or rv is None or rv <= 0:
            return None
        ratio = iv / rv
        return ratio >= strat["iv_rv_threshold"] if e == "iv_rv_ge" else ratio <= strat["iv_rv_threshold"]
    raise ValueError(e)


def load_earnings(path: Path | None = None) -> dict[str, list[dict]] | None:
    """{ticker: [{date, timing, source}]}；文件不存在 / 读不了 -> None（F4 / F4c 报“没有财报日历”）。"""
    p = Path(path) if path else EARNINGS_PATH
    if not p.exists():
        return None
    try:
        rows = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None
    out: dict[str, list[dict]] = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            if r["timing"] not in ("amc", "bmo"):
                continue
            date.fromisoformat(r["date"])
            out.setdefault(r["ticker"], []).append({"date": r["date"], "timing": r["timing"], "source": r.get("source", "")})
        except Exception:  # noqa: BLE001
            continue
    return out


def earnings_entries(events: list[dict], days: list[str]) -> tuple[dict[int, dict], dict[str, list[str]]]:
    """({入场日序号: 事件}, {跳过原因: [财报日]})；amc -> 当天收盘进，bmo -> 前一交易日收盘进。财报日不在交易日历里 -> 记跳过。"""
    pos = {d: i for i, d in enumerate(days)}
    out, bad = {}, {}
    for ev in sorted(events, key=lambda e: e["date"]):
        i = pos.get(ev["date"])
        if i is None:
            if SAMPLE_START <= ev["date"] <= days[-1]:
                bad.setdefault("earnings_date_not_trading_day", []).append(ev["date"])
            continue
        if ev["timing"] == "bmo":
            i -= 1
        if i >= 0 and days[i] >= SAMPLE_START:
            out[i] = ev
    return out, bad


# ---------------------------------------------------------------- 一个 (策略, 标的) 的模拟
class Ctx:
    """某标的的数据：交易日、拆股调整收盘（RV）、不复权收盘（现价）、报价。IV/RV 逐日缓存。"""

    def __init__(self, und: str, days: list[str], adj: list[float], raw: list[float], q: Quotes, spec: dict):
        self.und, self.days, self.adj, self.raw, self.q, self.spec = und, days, adj, raw, q, spec
        self.ords = [date.fromisoformat(d).toordinal() for d in days]
        self._iv: dict[int, float | None] = {}

    def iv(self, i: int) -> float | None:
        if i not in self._iv:
            self._iv[i] = iv30(self.q, self.ords[i], self.raw[i], self.spec)
        return self._iv[i]

    def rv(self, i: int) -> float | None:
        return rv20(self.adj, i, self.spec["vol"]["rv_window"])

    def iv_peek(self, i: int) -> float | None:
        """只看缓存、不触发计算（给报告里记 iv30 用）。"""
        return self._iv.get(i)


def scan_entries(strat: dict, ctx: Ctx, earn_events: dict[int, dict] | None = None, first: str | None = None,
                 last: str | None = None, signal_fn=None) -> list[dict]:
    """每个信号日 -> {i, legs 或 None, why, iv, rv}。不看冷却（冷却在 simulate 里），所以也是补数据清单的超集。
    卡 L1 的两个可选扩展（默认值下行为与 B2 完全一致）：
    first / last = 入场日的闭区间（YYYY-MM-DD；first 不得早于 SAMPLE_START）；
    signal_fn(strat, ctx, i) -> True / False / None（None = 算不出，记 no_signal_data）：给了就用它代替 entry 字段的内置信号
    （研究室的多条件入场；财报入场日（earnings_eve）仍由 earn_events 给出，signal_fn 再叠加其余条件）。"""
    out = []
    for i, d in enumerate(ctx.days):
        if d < SAMPLE_START or (first is not None and d < first):
            continue
        if last is not None and d > last:
            break
        if strat["entry"] == "earnings_eve":
            if earn_events is None or i not in earn_events:
                continue
            sig, iv, rv = True, None, None
            if i + 1 >= len(ctx.days):
                out.append({"i": i, "legs": None, "why": "no_next_session"})
                continue
            if signal_fn is not None:
                sig = signal_fn(strat, ctx, i)
                if sig is None:
                    out.append({"i": i, "legs": None, "why": "no_signal_data"})
                    continue
                if not sig:
                    continue
        elif signal_fn is not None:
            sig = signal_fn(strat, ctx, i)
            iv, rv = ctx.iv_peek(i), ctx.rv(i)
            if sig is None:
                out.append({"i": i, "legs": None, "why": "no_signal_data"})
                continue
            if not sig:
                continue
        else:
            need = strat["entry"] != "always"
            iv = ctx.iv(i) if need else None
            rv = ctx.rv(i) if need else None
            sig = entry_signal(strat, iv, rv)
            if sig is None:
                out.append({"i": i, "legs": None, "why": "no_signal_data"})
                continue
            if not sig:
                continue
        legs, why = select_legs(ctx.q, ctx.ords[i], ctx.raw[i], strat, ctx.spec)
        out.append({"i": i, "legs": legs, "why": why, "iv": iv, "rv": rv})
    return out


def build_trade(strat: dict, ctx: Ctx, c: dict, spec: dict) -> tuple[dict | None, str]:
    legs, i = c["legs"], c["i"]
    fill = spec["fill"]
    slip = fill["slippage_pct"]
    net = entry_net_ps(legs, "primary", slip)
    L = -payoff_min(legs)
    has_sell = any(l["side"] == "sell" for l in legs)
    if has_sell and net <= 0 and not strat.get("net_debit_ok"):
        return None, "no_credit"
    if strat.get("net_debit_ok") and net > 0:         # 卡 L1：借方价差（多头腿离平值更近）收到权利金 = 报价异常
        return None, "bad_structure"
    credit_kind = net > 0
    max_loss_ps = L - net
    if max_loss_ps <= 0:
        return None, "bad_structure"
    ser = leg_series(ctx.q, legs, ctx.ords[i])
    ex = find_exit(strat, legs, ser, i, ctx.ords, ctx.q, net, L, fill, ctx.raw)
    pnl = settle(legs, ex, "primary", fill, L)
    refs = {str(f): settle(legs, ex, f, fill, L) for f in fill["ref_fill_fracs"]}
    nl = len(legs)
    alt = pnl
    if ex["never"] and ex["last_full"]:
        k, qts = ex["last_full"]
        alt = settle(legs, {"never": False, "flag": False, "quotes": qts}, "primary", fill, L)
    # 每日盯市（主口径，原始报价，含两次手续费），给仓位换算算回撤用
    cost0, _ = cost_at(legs, [(l["bid"], l["ask"], 0) for l in legs], L)
    fees = 2 * nl * fill["fee_per_contract"]
    mtm = {ctx.days[i]: MULT * (net - cost0) - fees}
    for k, cst in ex["marks"].items():
        mtm[ctx.days[k]] = MULT * (net - cst) - fees
    mtm[ctx.days[ex["idx"]]] = pnl
    max_loss = MULT * max_loss_ps
    t = {"strategy": strat["id"], "und": ctx.und, "kind": "credit" if credit_kind else "debit",
         "entry_date": ctx.days[i], "exit_date": ctx.days[ex["idx"]], "entry_idx": i, "exit_idx": ex["idx"],
         "expiry": date.fromordinal(legs[0]["exp"]).isoformat(), "dte_entry": legs[0]["exp"] - ctx.ords[i],
         "legs": " / ".join(f"{'卖' if l['side'] == 'sell' else '买'}{l['type']}{l['strike']:g}" for l in legs),
         "net_ps": round(net, 4), "max_loss": round(max_loss, 2), "pnl": round(pnl, 2), "ret": pnl / max_loss,
         "pnl_lastmark": round(alt, 2), "reason": ex["reason"], "no_ask_close": bool(ex["flag"] and not ex["never"]),
         "missing_exit_legs": int(ex.get("missing", 0)), "iv30": c.get("iv"), "rv20": c.get("rv"),
         "mtm": mtm}
    for f, v in refs.items():
        t[f"pnl_ref{f}"] = round(v, 2)
        t[f"ret_ref{f}"] = v / max_loss
    return t, ""


def day_labels(day: str) -> list[str]:
    """一个日期属于哪些切片：训练段 / 考试段，外加落在哪个暴跌窗口里（窗口与训练/考试段有重叠，所以是多标签）。"""
    labs = [seg_of(day)]
    labs += [name for name, (a, b) in CRASH_WINDOWS.items() if a <= day <= b]
    return labs


def add_skip(skips: Counter, split: dict | None, reason: str, day: str) -> None:
    skips[reason] += 1
    if split is not None:
        for lab in day_labels(day):
            split.setdefault(lab, Counter())[reason] += 1


def simulate(strat: dict, ctx: Ctx, spec: dict, cands: list[dict], split: dict | None = None) -> tuple[list[dict], list[int], dict]:
    """split（可选的传出参数）：{切片: Counter(原因)}，切片 = train / test / 两个暴跌窗口。"""
    cd = strat.get("cooldown_days", 1)
    sig_days, trades, skips = [], [], Counter()
    blocked = -1                  # 可再开的最早交易日序号 = 平仓日 + cooldown_days
    for c in cands:
        i = c["i"]
        day = ctx.days[i]
        if c.get("why") == "no_signal_data":      # 算不出 IV/RV：不算信号日
            add_skip(skips, split, "no_signal_data", day)
            continue
        sig_days.append(i)
        if i < blocked:
            add_skip(skips, split, "cooldown_or_open", day)
            continue
        if c["legs"] is None:
            add_skip(skips, split, c["why"], day)
            continue
        t, why = build_trade(strat, ctx, c, spec)
        if t is None:
            add_skip(skips, split, why, day)
            continue
        trades.append(t)
        blocked = t["exit_idx"] + cd
    return trades, sig_days, dict(skips)


def extra_needed_for(strat: dict, ctx: Ctx, cands: list[dict]) -> dict[str, set]:
    """第一遍产物：每个能选出腿的信号日（不管冷却，取超集）的每条腿，从入场日+1 到计划最晚出场日里主数据没有报价的 (日, 合约)。"""
    out: dict[str, set] = {}
    n = len(ctx.days)
    for c in cands:
        if c["legs"] is None:
            continue
        i = c["i"]
        exp = c["legs"][0]["exp"]
        if strat.get("exit") == "next_session_close":
            last = min(i + 1, n - 1)
        else:
            last = min(i + strat["max_hold_days"], n - 1)
        for l in c["legs"]:
            occ = l["symbol"].replace(" ", "")
            for k in range(i + 1, last + 1):
                if ctx.ords[k] > exp:
                    break
                if not ctx.q.has_main(l["symbol"], ctx.ords[k]):
                    out.setdefault(ctx.days[k], set()).add(occ)
                if strat.get("exit_dte") is not None and exp - ctx.ords[k] <= strat["exit_dte"]:
                    break
    return out


def still_missing(q: Quotes, need: dict[str, set]) -> int:
    """补数据（主+extra）之后仍然没有报价的 (合约, 日) 个数。"""
    n = 0
    for day, syms in need.items():
        o = date.fromisoformat(day).toordinal()
        for occ in syms:
            sym = occ[:-15].ljust(6) + occ[-15:]
            idx = q.by_sym.get(sym)
            if idx is None:
                n += 1
                continue
            days = q.all["day"].values[idx]
            a = int(np.searchsorted(days, o))
            if not (a < len(days) and int(days[a]) == o):
                n += 1
    return n


# ---------------------------------------------------------------- 统计
def basic(trades: list[dict], key: str = "pnl", rkey: str = "ret") -> dict:
    if not trades:
        return {"trades": 0, "win_rate": None, "total_pnl": 0.0, "avg_pnl": None, "avg_ret": None}
    p = [t[key] for t in trades]
    r = [t[rkey] for t in trades]
    return {"trades": len(p), "win_rate": sum(x > 0 for x in p) / len(p), "total_pnl": round(sum(p), 2),
            "avg_pnl": round(sum(p) / len(p), 2), "avg_ret": float(np.mean(r))}


def streak_dd(trades: list[dict]) -> tuple[int, float]:
    ts = sorted(trades, key=lambda t: (t["exit_date"], t["entry_date"]))
    mx = cur = 0
    cum = peak = dd = 0.0
    for t in ts:
        cur = cur + 1 if t["pnl"] <= 0 else 0
        mx = max(mx, cur)
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return mx, round(dd, 2)


def window_pnl(trades: list[dict], a: str, b: str) -> dict:
    real = [t for t in trades if a <= t["exit_date"] <= b]
    ov = [t for t in trades if t["entry_date"] <= b and t["exit_date"] >= a]
    return {"realized_pnl": round(sum(t["pnl"] for t in real), 2), "realized_trades": len(real),
            "overlap_pnl": round(sum(t["pnl"] for t in ov), 2), "overlap_trades": len(ov)}


def week_key(day: str) -> str:
    iso = date.fromisoformat(day).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def weeks_between(a: str, b: str) -> list[str]:
    """a、b 之间（含）所有 ISO 日历周。"""
    d, end, out = date.fromisoformat(a), date.fromisoformat(b), []
    while d <= end:
        k = week_key(d.isoformat())
        if not out or out[-1] != k:
            out.append(k)
        d += timedelta(days=1)
    return out


def weekly_pnl(trades: list[dict], weeks: list[str] | None = None) -> dict[str, float]:
    """按入场所在的 ISO 日历周汇总盈亏；没有交易的周记 0（时间块自助法要把空周也放进去）。"""
    out = {w: 0.0 for w in (weeks or [])}
    for t in trades:
        k = week_key(t["entry_date"])
        out[k] = out.get(k, 0.0) + t["pnl"]
    return out


def evaluate(strat: dict, und: str, trades: list[dict], sig_idx: list[int], skips: dict, spec: dict, status: str = "ok",
             skips_split: dict | None = None, weeks: list[str] | None = None) -> dict:
    """weeks = 考试段的全部 ISO 日历周（含没交易的周）；不给就取考试段第一笔到最后一笔之间的周。"""
    ps = spec["pass"]
    fr = spec["fill"]["ref_fill_fracs"]
    tr = [t for t in trades if seg_of(t["entry_date"]) == "train"]
    te = [t for t in trades if seg_of(t["entry_date"]) == "test"]
    mx, dd = streak_dd(trades)
    cl_te, cl_tr = clusters([t["entry_idx"] for t in te]), clusters([t["entry_idx"] for t in tr])
    if weeks is None and te:
        weeks = weeks_between(min(t["entry_date"] for t in te), max(t["entry_date"] for t in te))
    wk = weekly_pnl(te, weeks)
    wvals = [wk[k] for k in sorted(wk)]
    # 时间块自助法：以日历周为块，整周重抽（周内盈亏先加总），不再把每笔当独立
    p, ts = boot_p(wvals), tstat(wvals)
    b_all, b_tr, b_te = basic(trades), basic(tr), basic(te)
    c1 = b_te["total_pnl"] > 0
    c2 = len(cl_te) >= ps["min_clusters"]
    c3a = b_all["total_pnl"] > 0                 # 裁定 2026-10-25：整个样本扣费后净盈亏 > 0
    c3b = b_tr["total_pnl"] > 0                  # 且训练段总盈亏 > 0（取代“训练段平均每笔同号”）
    c4 = p is not None and p < ps["p_shadow"]
    crit = bool(c1 and c2 and c3a and c3b and c4)
    ml_ret = lambda b: (b["avg_ret"] or 0)       # noqa: E731
    spread_kind = bool(strat["legs"] and any(l["side"] == "sell" for l in strat["legs"]))
    too_good = bool(b_te["trades"] and (ml_ret(b_te) > TOO_GOOD_RET or ((b_te["win_rate"] or 0) > TOO_GOOD_WIN and b_te["trades"] >= TOO_GOOD_MIN_N)))
    if spread_kind:      # 价差 / 铁鹰另加：胜率 > 90% 且 ≥20 笔且每笔平均 > 最大亏损的 25%（考试段或整样本任一）
        for b in (b_te, b_all):
            if b["trades"] >= TOO_GOOD_SPREAD_N and (b["win_rate"] or 0) > TOO_GOOD_SPREAD_WIN and ml_ret(b) > TOO_GOOD_SPREAD_RET:
                too_good = True
    shadow = bool(crit and not too_good)
    reasons = Counter(t["reason"] for t in trades)
    worst = min(trades, key=lambda t: t["pnl"]) if trades else None
    ref = {}
    for f in fr:
        k = str(f)
        ref[k] = {"note": "仅参考，不作为结论", "all": basic(trades, f"pnl_ref{k}", f"ret_ref{k}"), "test": basic(te, f"pnl_ref{k}", f"ret_ref{k}")}
    sample_note = ""
    if strat["entry"] == "earnings_eve" and not c2:
        sample_note = "样本不够（考试段财报簇数 < 10），只能当探索，不能下结论"
    return {"strategy": strat["id"], "und": und, "status": status, "kind": "credit" if spread_kind else "debit",
            "all": b_all, "train": b_tr, "test": b_te, "ref": ref,
            "worst_trade": ({k: worst[k] for k in ("entry_date", "exit_date", "legs", "pnl", "ret", "reason")} if worst else None),
            "max_consec_losses": mx, "max_drawdown": dd,
            "windows": {k: window_pnl(trades, a, b) for k, (a, b) in CRASH_WINDOWS.items()},
            "signal_days": len(sig_idx), "signal_clusters": len(clusters(sig_idx)),
            "traded_clusters_train": len(cl_tr), "traded_clusters_test": len(cl_te), "test_weeks": len(wvals),
            "test_week_tstat": ts, "test_week_boot_p": p, "bonferroni": BONF,
            "crit_test_pnl_positive": bool(c1), "crit_clusters_ge_min": bool(c2), "crit_whole_net_positive": bool(c3a),
            "crit_train_total_positive": bool(c3b), "crit_boot_p_lt_shadow": bool(c4),
            "shadow_criteria_met": crit, "too_good": too_good, "shadow_pass": shadow, "blocked_by": [],
            "live_stat_ok": bool(shadow and p is not None and p < BONF), "live_paper_route": shadow,
            "sample_note": sample_note, "skips": skips,
            "skips_split": {k: dict(v) for k, v in (skips_split or {}).items()}, "reasons": dict(reasons),
            "n_no_ask_close": sum(1 for t in trades if t["no_ask_close"]), "n_no_more_quotes": reasons.get("no_more_quotes", 0),
            "n_stop_on_missing": reasons.get("stop_on_missing", 0),
            "n_missing_exit_leg": sum(t["missing_exit_legs"] for t in trades)}


def _block(x: dict, why: str) -> None:
    x["shadow_pass"] = False
    x["live_stat_ok"] = False
    x["live_paper_route"] = False
    if why not in x["blocked_by"]:
        x["blocked_by"].append(why)


def apply_gates(results: list[dict], completeness: dict | None = None) -> dict:
    """跨组合的闸门，改 results 里的过关标志，返回 {control, completeness}。
    ① 对照组 F4c（财报日买跨式，文献预计亏）在主口径下考试段或整样本合计 > 0 -> 红色警报，F4 全部不许过。
    ② 数据不完整（见 completeness_check）-> 整份报告“未完成”，所有过关标志置 False。"""
    f4c = [x for x in results if x["strategy"].startswith("F4c") and x["status"] == "ok"]
    ctrl = {"test_pnl": round(sum(x["test"]["total_pnl"] for x in f4c), 2), "all_pnl": round(sum(x["all"]["total_pnl"] for x in f4c), 2),
            "n_trades": sum(x["all"]["trades"] for x in f4c)}
    ctrl["alarm"] = bool(ctrl["test_pnl"] > 0 or ctrl["all_pnl"] > 0)
    if ctrl["alarm"]:
        for x in results:
            if x["strategy"].startswith("F4_"):
                _block(x, "control_group_profit")
    comp = completeness or {"complete": True, "reasons": []}
    if not comp["complete"]:
        for x in results:
            _block(x, "incomplete")
    return {"control": ctrl, "completeness": comp}


def completeness_check(results: list[dict], root: Path, unresolved: dict[str, int], max_bad_frac: float = INCOMPLETE_FRAC) -> dict:
    """两遍流程的完整性闸门：
    ① 某 (策略, 标的) 里 > 5% 的笔以 no_more_quotes 或 stop_on_missing 收尾 -> 不完整；
    ② 补数据没跑过：该标的还有缺的 (合约, 日)，且 meta/extra_needed_v2.json 比 cbbo1m_extra_v2/<标的>/ 里最新的文件新（或根本没有补数据）-> 不完整。"""
    reasons = []
    for x in results:
        n = x["all"]["trades"]
        bad = x.get("n_no_more_quotes", 0) + x.get("n_stop_on_missing", 0)
        if n and bad / n > max_bad_frac:
            reasons.append(f"{x['strategy']}/{x['und']}：{n} 笔里 {bad} 笔（{bad / n * 100:.0f}%）以“之后没报价 / 缺腿日止损”收尾，超过 {max_bad_frac * 100:.0f}%")
    nf = root / "meta" / NEEDED_FILE
    for u, miss in unresolved.items():
        if not miss or not nf.exists():
            continue
        files = list((root / EXTRA_SUB_V2 / u).glob("*.parquet")) if (root / EXTRA_SUB_V2 / u).exists() else []
        newest = max((f.stat().st_mtime for f in files), default=None)
        if newest is None or newest < nf.stat().st_mtime:
            reasons.append(f"{u}：还有 {miss} 个(合约,日)没有报价，补数据还没跑（{NEEDED_FILE} 比 {EXTRA_SUB_V2}/{u}/ 里的数据新）")
    return {"complete": not reasons, "reasons": reasons}


# ---------------------------------------------------------------- 仓位换算
def kelly_f(rets: list[float]) -> float | None:
    """每笔“最大亏损占账户比例” f 的凯利最优（最大化 mean ln(1 + f r)）；样本 < 5 笔 -> None；没有正期望 -> 0。"""
    r = np.asarray([x for x in rets if math.isfinite(x)], float)
    if len(r) < 5:
        return None
    if r.mean() <= 0:
        return 0.0
    rmin = float(r.min())
    fmax = min(1.0, 0.999 / -rmin) if rmin < 0 else 1.0
    grid = np.linspace(0, fmax, 2001)[1:]
    g = np.log1p(np.outer(grid, r)).mean(axis=1)
    j = int(np.argmax(g))
    return float(grid[j]) if g[j] > 0 else 0.0


def kelly_capped(trades: list[dict], spec: dict) -> dict:
    """四分之一凯利（训练段估计）：只用训练段的每笔收益率估凯利比例，× kelly_fraction，
    硬封顶 = 最高一档（10%）；样本里只要有一笔亏掉 ≥90% 最大亏损，封顶降到 5%（中间一档）。拿去用在考试段。"""
    sz = spec["sizing"]
    tiers = sorted(sz["risk_per_trade"])
    train_rets = [t["ret"] for t in trades if seg_of(t["entry_date"]) == "train"]
    kf = kelly_f(train_rets)
    big_loss = any(t["ret"] <= -KELLY_BIG_LOSS for t in trades)
    cap = tiers[-2] if big_loss and len(tiers) > 1 else tiers[-1]
    quarter = kf * sz["kelly_fraction"] if kf is not None else None
    used = min(quarter, cap) if quarter is not None else None
    return {"label": "四分之一凯利（训练段估计）", "n_train_trades": len(train_rets), "kelly_full": kf, "quarter": quarter, "cap": cap,
            "cap_reason": "有一笔亏掉 ≥90% 最大亏损，封顶降到 5%" if big_loss else "封顶 = 最高一档 10%",
            "fraction": used, "capped": bool(quarter is not None and quarter > cap)}


def size_account(trades: list[dict], cal: list[str], tier: float, spec: dict, index_like: set[str],
                 order: dict | None = None, e0: float = E0, integer: bool = False) -> dict:
    """每笔在入场时冒 tier × 当时净值 的最大亏损；integer=True 时张数向下取整（取整成 0 张就不做，记 round_to_0）。
    同时持仓 ≤ max_open；index_like 标的的持仓（所有当前还开着的，不只是同一天开的）合计 ≤ correlated_cap_mult 笔。
    净值逐日盯市（缺报价日用更差的标记，见 find_exit），已平仓按实际盈亏。"""
    sz = spec["sizing"]
    order = order or {}
    by_entry: dict[str, list[dict]] = {}
    for t in sorted(trades, key=lambda t: (t["entry_date"], order.get(t["strategy"], 99), order.get(t["und"], 99), t["und"])):
        by_entry.setdefault(t["entry_date"], []).append(t)
    open_: list[dict] = []
    realized = 0.0
    eq: list[float] = []
    skipped, taken = Counter(), 0
    for day in cal:
        keep = []
        for p in open_:
            t = p["t"]
            if t["exit_date"] <= day:
                realized += p["n"] * t["pnl"]
            else:
                p["last"] = t["mtm"].get(day, p["last"])
                keep.append(p)
        open_ = keep
        e_pre = e0 + realized + sum(p["n"] * p["last"] for p in open_)
        for t in by_entry.get(day, []):
            if len(open_) >= sz["max_open"]:
                skipped["max_open"] += 1
                continue
            is_idx = t["und"] in index_like
            if is_idx and sum(1 for p in open_ if p["t"]["und"] in index_like) >= sz["correlated_cap_mult"]:
                skipped["correlated_cap"] += 1
                continue
            if e_pre <= 0 or t["max_loss"] <= 0:
                skipped["no_equity"] += 1
                continue
            n = tier * e_pre / t["max_loss"]
            if integer:
                n = math.floor(n + 1e-9)
                if n <= 0:
                    skipped["round_to_0"] += 1
                    continue
            open_.append({"t": t, "n": n, "last": t["mtm"].get(day, 0.0)})
            taken += 1
        eq.append(e0 + realized + sum(p["n"] * p["last"] for p in open_))
    attempted = taken + skipped["round_to_0"]
    return {"tier": tier, "equity": eq, "taken": taken, "skipped": dict(skipped),
            "zero_share": (skipped["round_to_0"] / attempted if integer and attempted else None)}


def _segment_stats(cal: list[str], eq: list[float], start: int, e0: float = E0) -> dict:
    """cal[start:] 这一段的月收益（中位 / 均值）、最差一周、最大回撤；基准 = 段前一天净值（start=0 时用初始净值）。"""
    if start >= len(cal):
        return {"months": 0, "monthly_mean": None, "monthly_median": None, "worst_week": None, "worst_week_label": None, "max_drawdown": None, "total_return": None}
    base = eq[start - 1] if start > 0 else e0
    months: dict[str, float] = {}
    weeks: dict[tuple, float] = {}
    peak, dd = base, 0.0
    for d, v in zip(cal[start:], eq[start:]):
        dt = date.fromisoformat(d)
        months[d[:7]] = v
        iso = dt.isocalendar()
        weeks[(iso[0], iso[1])] = v
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak > 0 else 0)

    def rets(m):
        prev, out = base, []
        for k in sorted(m):
            out.append((k, m[k] / prev - 1 if prev > 0 else -1.0))
            prev = m[k]
        return out
    mr, wr = rets(months), rets(weeks)
    ww = min(wr, key=lambda x: x[1])
    return {"months": len(mr), "monthly_mean": float(np.mean([x[1] for x in mr])), "monthly_median": float(np.median([x[1] for x in mr])),
            "worst_week": ww[1], "worst_week_label": f"{ww[0][0]}-W{ww[0][1]:02d}", "max_drawdown": dd, "total_return": eq[-1] / base - 1}


def sizing_report(trades: list[dict], cal: list[str], spec: dict, label: str, index_like: set[str], order: dict | None = None) -> dict:
    sz = spec["sizing"]
    ti = next((i for i, d in enumerate(cal) if d >= TEST_START), len(cal))
    kc = kelly_capped(trades, spec)
    tiers = []
    for tier in sz["risk_per_trade"]:
        s = size_account(trades, cal, tier, spec, index_like, order)
        si = size_account(trades, cal, tier, spec, index_like, order, e0=INT_ACCOUNT, integer=True)
        tiers.append({"tier": tier, "taken": s["taken"], "skipped": s["skipped"],
                      "test": _segment_stats(cal, s["equity"], ti), "full": _segment_stats(cal, s["equity"], 0),
                      "integer": {"account": INT_ACCOUNT, "taken": si["taken"], "skipped": si["skipped"], "zero_share": si["zero_share"],
                                  "mostly_zero": bool(si["zero_share"] is not None and si["zero_share"] > 0.5),
                                  "test": _segment_stats(cal, si["equity"], ti, INT_ACCOUNT), "full": _segment_stats(cal, si["equity"], 0, INT_ACCOUNT)}})
    ksim = None
    if kc["fraction"]:
        s = size_account(trades, cal, kc["fraction"], spec, index_like, order)
        ksim = {"fraction": kc["fraction"], "taken": s["taken"], "skipped": s["skipped"],
                "test": _segment_stats(cal, s["equity"], ti), "full": _segment_stats(cal, s["equity"], 0)}
    return {"label": label, "n_trades": len(trades), "n_test_trades": sum(1 for t in trades if seg_of(t["entry_date"]) == "test"),
            "kelly": kc, "kelly_sim": ksim, "tiers": tiers}


# ---------------------------------------------------------------- 驱动
def _load_dir(d: Path, last_day: str | None) -> pd.DataFrame | None:
    """一个标的目录下的月 parquet -> 报价表。last_day 给了就**不读**文件名月份晚于它的文件，并把读进来的行里晚于 last_day 的丢掉
    （卡 L1：开发段回测不许加载金库段报价）。"""
    parts = []
    if d is not None and d.exists():
        cut = date.fromisoformat(last_day).toordinal() if last_day else None
        for p in sorted(d.glob("*.parquet")):
            if last_day and p.stem[:7] > last_day[:7]:
                continue
            m = read_month(p)
            if cut is not None:
                m = m[m["day"] <= cut]
            if len(m):
                parts.append(m)
            del m
    return pd.concat(parts, ignore_index=True) if parts else None


def load_quotes(root: Path, und: str, last_day: str | None = None, extra_subs: tuple = EXTRA_SUBS) -> Quotes | None:
    """last_day（可选）：报价只加载到这一天（含）。默认 None = 全部（B2 行为）。"""
    if last_day is None:
        main = Quotes._load(root / "cbbo1m" / und)
        ex = [x for x in (Quotes._load(root / sub / und) for sub in extra_subs) if x is not None]
    else:
        main = _load_dir(root / "cbbo1m" / und, last_day)
        ex = [x for x in (_load_dir(root / sub / und, last_day) for sub in extra_subs) if x is not None]
    if main is None:
        return None
    return Quotes(main, pd.concat(ex, ignore_index=True) if ex else None)


def write_needed_if_changed(root: Path, new: dict[str, dict[str, set]]) -> bool:
    """写 meta/extra_needed_v2.json，但内容没变就不动文件（否则每跑一遍回测它的修改时间都比补数据新，完整性闸门会永远报“补数据没跑”）。"""
    p = root / "meta" / NEEDED_FILE
    cur = json.loads(p.read_text()) if p.exists() else {}
    changed = False
    for u, dd in new.items():
        m = {k: set(v) for k, v in cur.get(u, {}).items()}
        for day, syms in dd.items():
            m.setdefault(day, set()).update(syms)
        if cur.get(u) != {k: sorted(v) for k, v in sorted(m.items())}:
            changed = True
    if changed:
        write_extra_needed(root, new, NEEDED_FILE)
    return changed


def run(root: Path, unds: list[str] | None, strat_ids: list[str] | None, spec: dict, earnings_path: Path | None = None,
        log=print, quotes_loader=None) -> tuple[dict, list[dict]]:
    strats = [s for s in spec["strategy"] if not strat_ids or s["id"] in strat_ids or s["id"].split("_")[0] in strat_ids]
    earn = load_earnings(earnings_path)
    warnings: list[str] = []
    sha = spec.get("_sha16")
    if sha and sha != EXPECTED_SPEC_SHA16:
        warnings.append(f"toml 的 sha256 前 16 位是 {sha}，和登记的 {EXPECTED_SPEC_SHA16} 不一致：规格被改过，这不再是预先登记的 v2")
    all_unds = sorted({u for s in strats for u in universe_of(s, spec)}, key=lambda u: spec["universe"]["all"].index(u) if u in spec["universe"]["all"] else 99)
    if unds:
        all_unds = [u for u in all_unds if u in unds]
    results, trades_all, extra_rows, unresolved = [], [], {}, {}
    cal: set[str] = set()
    loader = quotes_loader or (lambda u: load_quotes(root, u))
    for u in all_unds:
        mine = [s for s in strats if u in universe_of(s, spec)]
        log(f"[{u}] ...")
        try:
            raw_d, adj_d = read_closes(root, u)
        except FileNotFoundError:
            warnings.append(f"{u}: 没有日线收盘（先跑 --fetch-closes）")
            for s in mine:
                results.append(_stub(s, u, "no_data", spec))
            continue
        warnings += split_warnings(u, raw_d, adj_d)
        days = sorted(set(adj_d) & set(raw_d))
        q = loader(u)
        if q is None:
            warnings.append(f"{u}: 没有报价数据")
            for s in mine:
                results.append(_stub(s, u, "no_data", spec))
            continue
        ctx = Ctx(u, days, [adj_d[d] for d in days], [raw_d[d] for d in days], q, spec)
        cal.update(d for d in days if d >= SAMPLE_START)
        test_weeks = weeks_between(max(TEST_START, days[0]), days[-1]) if days and days[-1] >= TEST_START else []
        need: dict[str, set] = {}
        for s in mine:
            if s["entry"] == "earnings_eve":
                if earn is None:
                    results.append(_stub(s, u, "no_earnings_calendar", spec))
                    continue
                ev, bad = earnings_entries(earn.get(u, []), days)
                if not ev:
                    results.append(_stub(s, u, "no_earnings_events", spec))
                    continue
                cands = scan_entries(s, ctx, ev)
            else:
                cands = scan_entries(s, ctx)
            for day, syms in extra_needed_for(s, ctx, cands).items():
                need.setdefault(day, set()).update(syms)
            split: dict = {}
            tr, sig, sk = simulate(s, ctx, spec, cands, split)
            if s["entry"] == "earnings_eve":
                for k, dates in bad.items():
                    sk[k] = sk.get(k, 0) + len(dates)
                    for dd in dates:
                        for lab in day_labels(dd):
                            split.setdefault(lab, Counter())[k] += 1
            results.append(evaluate(s, u, tr, sig, sk, spec, skips_split=split, weeks=test_weeks))
            trades_all += tr
        write_needed_if_changed(root, {u: need})
        extra_rows[u] = q.n_extra
        unresolved[u] = still_missing(q, need)
        del q, ctx
    if earn is None and any(s["entry"] == "earnings_eve" for s in strats):
        warnings.append("没有财报日历（knowledge/earnings_history.json 不存在）：F4 / F4c 跳过，等卡 E1 补上再跑")
    n_zero = sum(x.get("n_no_more_quotes", 0) for x in results)
    if n_zero:
        warnings.append(f"{n_zero} 笔仓位平仓前一直没有完整报价，按最大亏损记；若还没跑过 `opra_fetch --extra --needed-file {NEEDED_FILE} --extra-sub {EXTRA_SUB_V2} --download --yes`，先补数据再看结论")
    n_sm = sum(x.get("n_stop_on_missing", 0) for x in results)
    if n_sm:
        warnings.append(f"{n_sm} 笔在缺腿日按“内在价值下界”触发了止损（stop_on_missing，保守口径，单独计数）")
    n_na = sum(x.get("n_no_ask_close", 0) for x in results)
    if n_na:
        warnings.append(f"{n_na} 笔平仓时空头腿没有卖价，按该价差最大亏损平仓（保守口径，单独计数）")
    comp = completeness_check(results, root, unresolved)
    gates = apply_gates(results, comp)
    index_like = set(spec["universe"]["index_like"])
    sizing = sizing_sets(trades_all, sorted(cal), results, spec, index_like)
    out = {"gates": gates, "spec_version": "v2", "spec_sha256_16": spec.get("_sha16"), "sample": [SAMPLE_START, "latest"], "train_end": TRAIN_END, "test_start": TEST_START,
           "n_trials": N_TRIALS, "bonferroni": BONF, "extra_rows": extra_rows, "unresolved_extra": unresolved,
           "results": results, "sizing": sizing, "warnings": warnings}
    return out, trades_all


def _stub(strat: dict, und: str, status: str, spec: dict) -> dict:
    ev = evaluate(strat, und, [], [], {status: 1}, spec, status=status)
    return ev


def sizing_sets(trades: list[dict], cal: list[str], results: list[dict], spec: dict, index_like: set[str]) -> list[dict]:
    order = {s["id"]: k for k, s in enumerate(spec["strategy"])}
    order.update({u: k for k, u in enumerate(spec["universe"]["index_like"])})
    passed = {(x["strategy"], x["und"]) for x in results if x["shadow_pass"]}
    sets = []
    if passed:
        sets.append(("过影子门槛的组合合计", [t for t in trades if (t["strategy"], t["und"]) in passed], True))
    sets.append(("全部策略合计（示意：未过关的也算进来）", trades, False))
    for s in spec["strategy"]:
        sets.append((f"{s['id']}（该策略全部标的，示意）", [t for t in trades if t["strategy"] == s["id"]], False))
    out = []
    for lab, tr, is_passed in sets:
        r = sizing_report(tr, cal, spec, lab, index_like, order) if tr else {"label": lab, "n_trades": 0, "n_test_trades": 0, "kelly": None, "kelly_sim": None, "tiers": []}
        r["passed_set"] = is_passed
        out.append(r)
    return out


# ---------------------------------------------------------------- 报告（中文）
RULING_ZH = ("**过关标准澄清 —— Opus 裁定 2026-10-25（看结果前）**：影子门槛在 toml 原有四条（考试段主口径总盈亏 > 0；考试段簇数 ≥ 10；考试段 p < 0.10；不是“好得离谱”）之外，"
             "再加两条——**整个样本主口径扣费后净盈亏 > 0**，且**训练段总盈亏 > 0**（取代“训练段平均每笔同号”）。训练段总盈亏为负的策略，即使考试段为正也不能过。"
             "两次暴跌窗口（2024-08-01 至 08-16、2025-04-01 至 04-30）必须按“持仓期间盈亏”（和窗口有重叠的每一笔的整笔盈亏）展示在报告顶部。"
             "p 值用按日历周的时间块自助法（整周重抽），不再把每笔当独立。")


def _ret(x) -> str:
    return "-" if x is None else f"{x * 100:+.1f}%"


def _mm(te: dict) -> str:
    """月收益：中位数在前，均值放括号。"""
    return f"{_ret(te['monthly_median'])}（均值 {_ret(te['monthly_mean'])}）"


def _dd(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def banner_lines(out: dict) -> list[str]:
    g = out.get("gates") or {}
    L = []
    comp = g.get("completeness") or {"complete": True, "reasons": []}
    if not comp["complete"]:
        L.append("**【未完成：先补数据】** 下面所有“过关”标志都已强制置为“否”，数字只能当草稿看。原因：" + "；".join(comp["reasons"]))
    ctrl = g.get("control")
    if ctrl and ctrl["alarm"]:
        L.append(f"**【红色警报：对照组赚钱 → 先查 bug】** F4c（财报日买跨式，文献预计扣成本后亏）在主口径下考试段合计 {ctrl['test_pnl']:+,.0f}、整样本合计 {ctrl['all_pnl']:+,.0f} 美元，大于 0。"
                 "这说明回测有 bug 或口径错，F4（财报铁蝶）全部不许过。")
    return L


def _sizing_pick(out: dict) -> tuple[dict | None, str]:
    sets = out["sizing"]
    s = next((x for x in sets if x.get("passed_set")), None)
    if s:
        return s, "过关组合"
    return next((x for x in sets if x["label"].startswith("全部策略")), None), "全部策略合计；没过关，仅示意"


def summary_lines(out: dict) -> list[str]:
    res = out["results"]
    passed = [x for x in res if x["shadow_pass"]]
    live = [x for x in passed if x["live_stat_ok"]]
    n = len(res)
    ok = sum(1 for x in res if x["status"] == "ok" and x["all"]["trades"] > 0)
    fp = "（28 个组合按 10% 的影子门槛，纯靠运气平均也会有 28×0.10≈2.8 个“通过”；影子通过不是证明，只是进入影子记账继续观察）"
    if passed:
        l1 = (f"1. 本次跑了 {n} 个（策略,标的）组合（全套共 28 个），其中跑出交易的有 {ok} 个，按预先登记标准过了“影子记账”门槛的有 {len(passed)} 个："
              + "、".join(f"{x['strategy']}/{x['und']}" for x in passed) + "；其中统计上够上真钱门槛（p<0.05/60）的有 "
              + (f"{len(live)} 个。" if live else "0 个（要再走“影子 + 模拟账户满 8 周”那条路）。") + fp)
    elif ok == 0:
        l1 = "1. 现在还没有可用的报价数据（或日线收盘），一个组合都没跑出交易——先按下面“下一步”补数据，这份报告里的数字都是空的。" + fp
    else:
        l1 = f"1. 本次跑了 {n} 个（策略,标的）组合（全套共 28 个），其中跑出交易的有 {ok} 个，按预先登记标准一个都没过影子门槛（不进影子记账）。" + fp
    s, lab = _sizing_pick(out)
    lines = [l1]
    tiers = s["tiers"] if s else []
    if not tiers:
        lines += [f"{k}. 每笔最多亏账户 {int(t * 100)}% 的档位：没有交易，算不出月收益。" for k, t in zip((2, 3, 4), (0.02, 0.05, 0.10))]
        lines.append("5. 最大回撤：没有交易，算不出。")
        return lines
    for k, t in zip((2, 3, 4), tiers):
        te, it = t["test"], t["integer"]
        zero = ("；$5,000 账户取整张后大多数交易是 0 张，这一档在小账户上做不了" if it["mostly_zero"] else
                (f"；$5,000 账户取整张：月中位 {_ret(it['test']['monthly_median'])}（均值 {_ret(it['test']['monthly_mean'])}），{(it['zero_share'] or 0) * 100:.0f}% 的单取整成 0 张"))
        lines.append(f"{k}. 每笔最多亏账户 {t['tier'] * 100:.0f}%（{lab}）：考试段月收益中位数 {_ret(te['monthly_median'])}（均值 {_ret(te['monthly_mean'])}），最差一周 {_ret(te['worst_week'])}{zero}。")
    dd = "、".join(f"{t['tier'] * 100:.0f}% 档 {_dd(t['test']['max_drawdown'])}" for t in tiers)
    full = "、".join(_dd(t["full"]["max_drawdown"]) for t in tiers)
    kc = s.get("kelly")
    kt = (f"；四分之一凯利（训练段估计）= {('每笔冒账户 %.1f%%' % (kc['fraction'] * 100)) if kc and kc['fraction'] is not None else '样本不足'}" if kc else "")
    lines.append(f"5. 最大回撤（考试段）：{dd}；整个样本（含 2024-08 日元平仓、2025-04 关税暴跌）三档分别是 {full}{kt}。三档都列出，不挑最好的一档当标题。")
    return lines


def crash_lines(out: dict) -> list[str]:
    names = list(CRASH_WINDOWS)
    L = ["## 两次暴跌窗口（持仓期间盈亏：和窗口有重叠的每一笔的整笔盈亏，主口径，每笔 1 张）", "",
         "| 策略 | 标的 | " + " | ".join(f"{n}（盈亏$ / 笔数）" for n in names) + " |", "|---|---|" + "---|" * len(names)]
    tot = {n: [0.0, 0] for n in names}
    for x in out["results"]:
        if x["status"] != "ok" or not x["all"]["trades"]:
            continue
        cells = []
        for n in names:
            w = x["windows"][n]
            tot[n][0] += w["overlap_pnl"]
            tot[n][1] += w["overlap_trades"]
            cells.append(f"{usd(w['overlap_pnl'])} / {w['overlap_trades']}")
        L.append(f"| {x['strategy']} | {x['und']} | " + " | ".join(cells) + " |")
    L.append("| **合计** | | " + " | ".join(f"**{usd(tot[n][0])} / {tot[n][1]}**" for n in names) + " |")
    L.append("")
    return L


def write_report(out: dict, spec: dict, path: Path) -> None:
    res = out["results"]
    L = ["# 期权策略 v2 回测报告（真实历史买卖报价，预先登记）", ""]
    b = banner_lines(out)
    if b:
        L += b + [""]
    L += summary_lines(out) + [""]
    L += [RULING_ZH, ""]
    L += crash_lines(out)
    L += [f"样本 {SAMPLE_START} → 最近一个交易日；训练段到 {TRAIN_END}，**考试段 {TEST_START} → 现在**；所有结论只看考试段、主口径（卖在买价、买在卖价，扣全部成本）。",
          f"一共试了 {N_TRIALS} 次（v1 32 + v2 28），统计显著性按 Bonferroni 校正：单次 p 要小于 {BONF:.5f}。规格文件 sha256 前 16 位 = {out['spec_sha256_16']}（登记值 {EXPECTED_SPEC_SHA16}）。", "",
          "## 先读这几条（口径和局限）", "",
          "- 现价用当天 IEX 日线不复权收盘近似 15:59；IV30 用 Black–Scholes 从 ≥15:50 的报价中间价反解（利率 4.5%，不考虑分红）；RV20 只用 d−21…d−1 的收盘（20 个日对数收益），当天收盘不进。",
          "- 价差/铁鹰/铁蝶的最大亏损 = 宽度 − 收到的权利金（铁鹰取两边宽度的大者），借方策略 = 付出的权利金；“每笔收益率”= 盈亏 ÷ 最大亏损，跨策略可比。",
          "- 入场、止盈要求每条腿价差 ≤ max(15% 中间价, 0.05 美元)；**所有**出场（止盈、止损、持满、临近到期、财报次日）只认 ≥15:50 ET（半天市 12:50）的报价，过期报价算缺失。",
          "- 缺腿日（任何一条腿没有可用报价）：不许止盈；信用结构用当天标的收盘价算平仓成本的内在价值下界，若下界已击穿止损线，按 max(下界 + 手续费, 止损价估计) 平仓并封顶最大亏损（记 stop_on_missing，单独计数）；"
          "借方结构从内在价值推不出“已击穿”，所以缺腿日不触发止损，只在盯市里用更差的标记。持满/临近到期的时间出场顺延到下一个所有腿都有可用报价的日子。",
          "- 平仓时空头腿没有卖价 -> 按该价差最大亏损平仓；多头腿没有买价 -> 按 0；一直没有完整可用报价 -> 从第一天没报价那天起按最大亏损记（不是拖到到期）。这几类都单独计数。",
          "- 盯市（算仓位回撤）：缺腿日用“上一个标记”和“内在价值下界”里更差的那个。",
          "- “参考口径”（成交在中间价 ± 75% / 50% 半价差）**仅参考，不作为结论**：只在参考口径下才赚钱的策略，下一步是在 Alpaca 模拟账户实测成交价，不是上真钱。",
          "- “好得离谱”：考试段每笔平均 > 最大亏损的 40%，或胜率 > 95% 且 ≥30 笔；价差/铁鹰另加：胜率 > 90% 且 ≥20 笔且每笔平均 > 最大亏损的 25%。先当 bug 查，不算过关。",
          "- 完整性闸门：某组 > 5% 的笔以“之后没报价 / 缺腿日止损”收尾，或补数据还没跑 -> 整份报告标“未完成”，所有过关标志置否。",
          "- 同一份报告每次另存一份带时间戳的副本（options_v2_YYYYMMDD_HHMM.md / .json）。", ""]
    for w in out["warnings"]:
        L.append(f"- 警告：{w}")
    if out["warnings"]:
        L.append("")
    unres = {u: n for u, n in out.get("unresolved_extra", {}).items() if n}
    if unres:
        L += ["- 补数据状态：以下标的还有 (合约,日) 没有报价（没补过，或补完仍然没有）：" + "、".join(f"{u} {n} 个" for u, n in unres.items()), ""]
    for s in spec["strategy"]:
        rr = [x for x in res if x["strategy"] == s["id"]]
        if not rr:
            continue
        L += [f"## {s['id']}", "", f"**是什么**：{s['why']}", ""]
        notes = {x["status"] for x in rr if x["status"] != "ok"}
        if "no_earnings_calendar" in notes:
            L += ["**没有财报日历**（knowledge/earnings_history.json 不存在），这条策略跳过。", ""]
        if "no_data" in notes:
            L += ["有标的没有报价/收盘数据，见警告。", ""]
        L += ["| 标的 | 笔数 | 胜率 | 每笔平均收益(占最大亏损) | 总盈亏$(每笔1张) | 训练段$ | 考试段$ | 考试段簇数 | p值(按周块自助法,单边) | 最差一笔$ | 最长连亏 | 2024-08窗口$(持仓期间) | 2025-04窗口$(持仓期间) | 平仓无卖价/无后续报价/缺腿止损 | 影子门槛 | 上真钱统计门槛 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for x in rr:
            a = x["all"]
            w = x["windows"]
            verdict = "好得离谱:先查bug" if x["too_good"] else ("**过**" if x["shadow_pass"] else "没过")
            if x["shadow_criteria_met"] and x["blocked_by"]:
                verdict = "标准达到但被闸门挡住：" + "、".join({"incomplete": "数据未完成", "control_group_profit": "对照组赚钱"}.get(k, k) for k in x["blocked_by"])
            if x["sample_note"]:
                verdict += "（样本不够）"
            if x["status"] != "ok":
                verdict = {"no_earnings_calendar": "没有财报日历", "no_data": "没有数据", "no_earnings_events": "没有该标的财报"}.get(x["status"], x["status"])
            live = "是" if x["live_stat_ok"] else "否"
            wt = x["worst_trade"]["pnl"] if x["worst_trade"] else None
            L.append(f"| {x['und']} | {a['trades']} | {pct(a['win_rate'])} | {_ret(a['avg_ret'])} | {usd(a['total_pnl'])} | {usd(x['train']['total_pnl'])} | {usd(x['test']['total_pnl'])} | "
                     f"{x['traded_clusters_test']} | {fnum(x['test_week_boot_p'], '.3f')} | {usd(wt)} | {x['max_consec_losses']} | "
                     f"{usd(w['2024-08-01..2024-08-16']['overlap_pnl'])} | {usd(w['2025-04-01..2025-04-30']['overlap_pnl'])} | "
                     f"{x['n_no_ask_close']}/{x['n_no_more_quotes']}/{x['n_stop_on_missing']} | {verdict} | {live} |")
        L.append("")
        L += ["参考口径（**仅参考，不作为结论**）：每笔平均收益 / 考试段总盈亏$（成交在中间价 ± 半价差 × 比例）", "",
              "| 标的 | " + " | ".join(f"{float(k) * 100:.0f}%半价差" for k in rr[0]["ref"]) + " |", "|---|" + "---|" * len(rr[0]["ref"])]
        for x in rr:
            L.append(f"| {x['und']} | " + " | ".join(f"{_ret(v['all']['avg_ret'])} / {usd(v['test']['total_pnl'])}" for v in x["ref"].values()) + " |")
        L.append("")
    L += ["## 被跳过的信号（每个策略x标的，按原因计数）", "",
          "| 策略 | 标的 | 信号日 | 实际开仓 | " + " | ".join(n for _, n in SKIP_ZH) + " | 状态 |", "|---|---|---|---|" + "---|" * (len(SKIP_ZH) + 1)]
    for x in res:
        L.append(f"| {x['strategy']} | {x['und']} | {x['signal_days']} | {x['all']['trades']} | " + " | ".join(str(x["skips"].get(k, 0)) for k, _ in SKIP_ZH) + f" | {x['status']} |")
    L += ["", "### 被跳过的信号，按训练段 / 考试段 / 暴跌窗口拆开", "",
          "暴跌窗口和训练段/考试段有重叠（两个窗口都落在训练段里），所以一个跳过可能同时计入两行。只列有跳过的行。", "",
          "| 策略 | 标的 | 切片 | " + " | ".join(n for _, n in SKIP_ZH) + " |", "|---|---|---|" + "---|" * len(SKIP_ZH)]
    for x in res:
        for lab in ("train", "test", *CRASH_WINDOWS):
            sp = x.get("skips_split", {}).get(lab)
            if sp:
                L.append(f"| {x['strategy']} | {x['und']} | {ZH_SLICE.get(lab, lab)} | " + " | ".join(str(sp.get(k, 0)) for k, _ in SKIP_ZH) + " |")
    L.append("")
    L += ["## 小结", ""]
    passed = [x for x in res if x["shadow_pass"]]
    L.append(f"{len(res)} 个（策略, 标的）组合里过影子门槛的有 {len(passed)} 个：" + ("、".join(f"{x['strategy']}/{x['und']}" for x in passed) if passed else "无") + "。"
             "28 个组合按 10% 的影子门槛，纯靠运气平均会有 28×0.10≈2.8 个“通过”；影子通过不是证明。")
    L.append("影子门槛 = ① 考试段主口径总盈亏 > 0；② 考试段簇数 ≥ 10；③ 整个样本净盈亏 > 0 且训练段总盈亏 > 0；④ 考试段按周块自助法 p < 0.10；且不是“好得离谱”、数据完整、对照组没赚钱。"
             "上真钱的统计门槛另加 p < 0.05/60，或者影子 + Alpaca 模拟账户满 8 周且实际成交价和回测主口径差距 < 30%（后一条这里算不了，要线上实测）。没过的不进影子记账。")
    L += ["", "## 仓位换算：把“每笔收益率”翻译成账户的月收益 / 回撤", "",
          "做法：每笔在入场时冒“档位 × 当时账户净值”的最大亏损；同时最多持 "
          f"{spec['sizing']['max_open']} 笔；SPY/QQQ/SOXX/SMH 视为完全相关，**所有当前还开着的**这类持仓合计最多 {spec['sizing']['correlated_cap_mult']} 笔（= 单档 × {spec['sizing']['correlated_cap_mult']} 的风险，不只算同一天开的）；"
          "净值逐日按盯市估（缺腿日用更差的标记）。月收益标题用**中位数**，均值放括号。月收益、最差一周、最大回撤都是**考试段**（2025-07 起）的；“整样本”包含 2024-08 和 2025-04 两次暴跌。"
          f"第一张表是 10 万美元账户、张数可以是小数；第二张表是 ${INT_ACCOUNT:,.0f} 小账户、张数向下取整（取整成 0 张就不做，会写出比例，大多数是 0 张的档位在小账户上做不了）。"
          "**四分之一凯利（训练段估计）**：只用训练段每笔收益率估凯利比例 × 1/4，拿去用在考试段；硬封顶 = 最高一档 10%，样本里只要有一笔亏掉 ≥90% 最大亏损就封顶 5%（把各笔当独立重复的赌局，不考虑同时持仓，仍偏乐观）。", ""]
    for s in out["sizing"]:
        L += [f"### {s['label']}", ""]
        if not s["tiers"]:
            L += ["没有交易。", ""]
            continue
        kc = s["kelly"]
        kl = ("样本不足（训练段 < 5 笔）" if kc["kelly_full"] is None else
              f"训练段凯利 {kc['kelly_full']:.3f}，× 1/4 = {kc['quarter']:.3f}，{kc['cap_reason']}，实际用 {kc['fraction'] * 100:.1f}%" + ("（被封顶）" if kc["capped"] else ""))
        L.append(f"交易 {s['n_trades']} 笔（考试段 {s['n_test_trades']} 笔）。{kc['label']}：{kl}。" + ("" if s.get("passed_set") else "（这一组没过关，只是示意“如果硬做会怎样”。）"))
        L += ["", "10 万美元账户，张数可小数：", "",
              "| 每笔风险档位 | 开仓笔数 | 因持仓上限/相关上限跳过 | 考试段月收益 中位（均值） | 考试段最差一周 | 考试段最大回撤 | 整样本最差一周 | 整样本最大回撤 |", "|---|---|---|---|---|---|---|---|"]
        for t in s["tiers"]:
            te, fu = t["test"], t["full"]
            sk = "、".join(f"{ {'max_open': '持仓上限', 'correlated_cap': '相关上限', 'no_equity': '净值耗尽'}.get(k, k)} {v}" for k, v in t["skipped"].items()) or "0"
            L.append(f"| {t['tier'] * 100:.0f}% | {t['taken']} | {sk} | {_mm(te)} | {_ret(te['worst_week'])} | {_dd(te['max_drawdown'])} | {_ret(fu['worst_week'])} | {_dd(fu['max_drawdown'])} |")
        ks = s.get("kelly_sim")
        if ks:
            te, fu = ks["test"], ks["full"]
            L.append(f"| 四分之一凯利（训练段估计）{ks['fraction'] * 100:.1f}% | {ks['taken']} | - | {_mm(te)} | {_ret(te['worst_week'])} | {_dd(te['max_drawdown'])} | {_ret(fu['worst_week'])} | {_dd(fu['max_drawdown'])} |")
        L += ["", f"${INT_ACCOUNT:,.0f} 账户，张数向下取整：", "",
              "| 每笔风险档位 | 开仓笔数 | 取整成 0 张的比例 | 其他跳过 | 考试段月收益 中位（均值） | 考试段最大回撤 | 说明 |", "|---|---|---|---|---|---|---|"]
        for t in s["tiers"]:
            it = t["integer"]
            sk = "、".join(f"{ {'max_open': '持仓上限', 'correlated_cap': '相关上限', 'no_equity': '净值耗尽'}.get(k, k)} {v}" for k, v in it["skipped"].items() if k != "round_to_0") or "0"
            note = "大多数交易取整后是 0 张，这一档在小账户上做不了" if it["mostly_zero"] else ""
            L.append(f"| {t['tier'] * 100:.0f}% | {it['taken']} | {pct(it['zero_share'])} | {sk} | {_mm(it['test'])} | {_dd(it['test']['max_drawdown'])} | {note} |")
        L.append("")
    path.write_text("\n".join(L) + "\n")


def next_steps(out: dict, root: Path) -> list[str]:
    unres = {u: n for u, n in out.get("unresolved_extra", {}).items() if n}
    if not unres:
        return []
    ul = " ".join(unres)
    return ["下一步（补持仓合约的平仓报价，补完再跑一遍回测；补完仍缺的是 Databento 也没有的报价，回测按保守口径处理，可以不管）：",
            f"  python -m desk.opra_fetch --extra --needed-file {NEEDED_FILE} --extra-sub {EXTRA_SUB_V2} --underlyings {ul} --plan",
            f"  python -m desk.opra_fetch --extra --needed-file {NEEDED_FILE} --extra-sub {EXTRA_SUB_V2} --underlyings {ul} --download --yes",
            "  python -m desk.options_v2_backtest"]


def write_outputs(out: dict, trades: list[dict], spec: dict, rdir: Path, now: datetime | None = None) -> list[Path]:
    """写 latest（options_v2.md / .json / _trades.csv）和一份带时间戳的副本 options_v2_YYYYMMDD_HHMM.md / .json。"""
    rdir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M")
    js = json.dumps(out, ensure_ascii=False, indent=1, default=str)
    (rdir / "options_v2.json").write_text(js)
    (rdir / f"options_v2_{stamp}.json").write_text(js)
    write_report(out, spec, rdir / "options_v2.md")
    ts_md = rdir / f"options_v2_{stamp}.md"
    ts_md.write_text((rdir / "options_v2.md").read_text())
    cols = ["strategy", "und", "kind", "legs", "entry_date", "exit_date", "expiry", "dte_entry", "net_ps", "max_loss", "pnl", "ret", "pnl_lastmark",
            "reason", "no_ask_close", "missing_exit_legs", "iv30", "rv20"] + sorted({k for t in trades for k in t if k.startswith(("pnl_ref", "ret_ref"))})
    with open(rdir / "options_v2_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)
    return [rdir / "options_v2.md", ts_md]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="desk.options_v2_backtest")
    ap.add_argument("--strategies", nargs="+", help="F1 F1f F2 F3 F4 F4c 或完整 id")
    ap.add_argument("--underlyings", nargs="+")
    ap.add_argument("--fetch-closes", action="store_true", help="只拉 8 个标的的日线收盘（Alpaca 只读）后退出")
    ap.add_argument("--earnings", help="财报日历 json，默认 knowledge/earnings_history.json")
    ap.add_argument("--root", help="默认 <data>/opra")
    ap.add_argument("--out", help="报告目录，默认 <data>/reports")
    a = ap.parse_args(argv)
    from . import config
    s = config.load()
    root = Path(a.root) if a.root else s.data / "opra"
    rdir = Path(a.out) if a.out else s.data / "reports"
    spec = load_spec()
    if a.fetch_closes:
        fetch_closes(root, a.underlyings or spec["universe"]["all"])
        return 0
    out, trades = run(root, a.underlyings, a.strategies, spec, Path(a.earnings) if a.earnings else None)
    paths = write_outputs(out, trades, spec, rdir)
    print(f"完成：{len(out['results'])} 个组合，{len(trades)} 笔 -> {paths[0]}（副本 {paths[1].name}）、options_v2.json")
    for line in banner_lines(out) + summary_lines(out):
        print(line)
    for w in out["warnings"]:
        print(f"警告：{w}")
    steps = next_steps(out, root)
    if steps:
        print("\n".join(steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
