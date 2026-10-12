"""纸面期权交易员（11a）：叠在 options.job_lock / job_outcome 之上，只记账、不下单。

每个样本（事件前 60 分钟 / 每日对照）锁定时：
  1. 从锁定时刻的期权快照里挑"刚好盖住 24 小时窗口"的到期日，取平值 call+put 的标记价
  2. 同时模拟三种结构：A 卖跨式、B 买跨式、C 不动。名义金额 = 标的等值 `[options.paper] notional_usd`
     （Deribit BTC 期权 1 张 = 1 BTC，纸面允许小数张）
  3. 手续费按 Deribit 吃单：每条腿 0.03% 标的，封顶权利金的 12.5%；再加权利金 0.5% 滑点。进出各算一次。
  4. 卖跨式另给一个"一次再平衡的 delta 对冲"近似：锁定时按 Black-Scholes delta（用锁定的隐含波动）对冲，
     窗口中点用指数价再平衡一次，窗口结束平掉对冲。
  5. 选边规则 v1（写死）：同组（事件类型或对照组）已有 ≥ min_history 个结果 → 均值 实际/隐含 < 0.9 选 A，
     > 1.1 选 B，否则 C。没有历史也记下 A、B 的盈亏，只是选 C。
窗口结束时：优先用窗口结束时刻的期权快照按标记价平仓；快照里找不到（已到期等）就用指数价算内在价值。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

log = logging.getLogger("options.paper")
UTC = timezone.utc
STRUCTURES = ("A", "B", "C")
NAMES = {"A": "卖跨式", "B": "买跨式", "C": "不动"}


# ---------------------------------------------------------------- Black-Scholes（无风险利率取 0，加密期权惯例）
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sigma: float, cp: str) -> float:
    """欧式期权价格（美元/单位标的）。T 年，sigma 年化小数。T 或 sigma 为 0 时退化成内在价值。"""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if cp == "C":
        return S * norm_cdf(d1) - K * norm_cdf(d2)
    return K * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, sigma: float, cp: str) -> float:
    if T <= 0 or sigma <= 0:
        itm = S > K if cp == "C" else S < K
        return (1.0 if itm else 0.0) * (1 if cp == "C" else -1)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1) if cp == "C" else norm_cdf(d1) - 1.0


def straddle_delta(S: float, K: float, T: float, sigma: float) -> float:
    return bs_delta(S, K, T, sigma, "C") + bs_delta(S, K, T, sigma, "P")


# ---------------------------------------------------------------- 费用
def leg_fee_usd(premium_usd: float, underlying_usd: float, n_contracts: float, pcfg: dict) -> float:
    """一条腿一次成交的手续费：0.03% × 标的 × 张数，封顶该腿权利金的 12.5%。premium_usd 为该腿总权利金。"""
    raw = pcfg["fee_pct_underlying"] / 100 * underlying_usd * n_contracts
    cap = pcfg["fee_cap_pct_premium"] / 100 * premium_usd
    return min(raw, cap)


def trade_cost_usd(leg_premiums_usd: list[float], underlying_usd: float, n_contracts: float, pcfg: dict) -> dict:
    """一次成交（进或出）所有腿的费用：手续费 + 滑点（权利金的 slippage_pct%）。"""
    fee = sum(leg_fee_usd(p, underlying_usd, n_contracts, pcfg) for p in leg_premiums_usd)
    slip = pcfg["slippage_pct"] / 100 * sum(leg_premiums_usd)
    return {"fee": fee, "slippage": slip, "total": fee + slip}


# ---------------------------------------------------------------- 选到期日 / 平值
def pick_expiry(items: list[dict], lock: datetime, window_hours: float):
    """最早一个到期时刻 ≥ 窗口结束的到期日；返回 (expiry, 该到期日的 items)。没有就 (None, [])。"""
    from .options import parse_name
    need = lock + timedelta(hours=window_hours)
    by_exp: dict[datetime, list[dict]] = {}
    for it in items:
        p = parse_name(it["n"])
        if p:
            by_exp.setdefault(p[0], []).append(it)
    cands = sorted(e for e in by_exp if e >= need)
    if not cands:
        return None, []
    return cands[0], by_exp[cands[0]]


def atm_legs(rows: list[dict]) -> dict | None:
    """同一到期日里离标的最近、且 call/put 都有的行权价；返回 {K, S, C: item, P: item}。"""
    from .options import parse_name
    by_k: dict[float, dict] = {}
    for it in rows:
        p = parse_name(it["n"])
        if p:
            by_k.setdefault(p[1], {})[p[2]] = it
    S = rows[0]["u"]
    ok = [k for k, v in by_k.items() if "C" in v and "P" in v]
    if not ok:
        return None
    K = min(ok, key=lambda k: abs(k - S))
    return {"K": K, "S": S, "C": by_k[K]["C"], "P": by_k[K]["P"]}


def mark_usd(it: dict, S: float, K: float, T: float, cp: str) -> float:
    """一条腿的标记价（美元/单位标的）。快照存了 Deribit 标记价（BTC 计价）就用它；否则买卖中间价；再否则按隐含波动算。"""
    m = it.get("m")
    if m is not None:
        return float(m) * S
    b, a = it.get("b"), it.get("a")
    if b and a:
        return (float(b) + float(a)) / 2 * S
    return bs_price(S, K, T, float(it["iv"]) / 100, cp)


# ---------------------------------------------------------------- 选边规则 v1（写死）
def paper_group(lock: dict) -> str:
    """分组：事件样本按事件类型（CPI/FOMC/NFP），每日样本沿用 options.group_of（control / daily_with_event）。"""
    from .options import group_of
    return lock["event_type"] if lock.get("sample_kind") == "event" else group_of(lock)


def choose_side(ratios: list[float], pcfg: dict, min_history: int) -> tuple[str, str]:
    """返回 (结构, 理由)。ratios = 同组历史 实际/隐含。"""
    n = len(ratios)
    if n < min_history:
        return "C", f"同组历史只有 {n} 个（需要 {min_history}），不选边，只记录 A/B 的纸面盈亏"
    mean = sum(ratios) / n
    if mean < pcfg["short_below"]:
        return "A", f"同组 {n} 个历史样本均值 实际/隐含 = {mean:.2f} < {pcfg['short_below']}：隐含偏贵，卖跨式"
    if mean > pcfg["long_above"]:
        return "B", f"同组 {n} 个历史样本均值 实际/隐含 = {mean:.2f} > {pcfg['long_above']}：隐含偏便宜，买跨式"
    return "C", f"同组 {n} 个历史样本均值 实际/隐含 = {mean:.2f}，在 {pcfg['short_below']}–{pcfg['long_above']} 之间：不动"


def history_ratios(led, group: str) -> list[float]:
    from .options import completed, official
    return [r["outcome"]["ratio"] for r in completed(led) if official(r) and paper_group(r["lock"]) == group]


# ---------------------------------------------------------------- 锁定时：建仓
def plan(settings, led, lock: dict, items: list[dict], lock_at: datetime) -> dict:
    """在锁定记录里加一个 paper 字典：三种结构的仓位、权利金、进场费用、最大亏损、选了哪个及理由。"""
    o = settings["options"]
    pcfg = o["paper"]
    group = paper_group(lock)
    ratios = history_ratios(led, group)
    chosen, reason = choose_side(ratios, pcfg, o["min_history"])
    base = {"chosen": chosen, "reason": reason, "group": group, "n_history": len(ratios),
            "notional_usd": pcfg["notional_usd"], "ok": False}
    expiry, rows = pick_expiry(items, lock_at, o["window_hours"])
    if expiry is None:
        return {**base, "problems": ["快照里没有盖住 24 小时窗口的到期日"]}
    legs = atm_legs(rows)
    if not legs:
        return {**base, "problems": ["该到期日没有同时有 call 和 put 的平值行权价"]}
    S, K = legs["S"], legs["K"]
    T = (expiry - lock_at).total_seconds() / (365 * 86400)
    n = pcfg["notional_usd"] / S
    c_px, p_px = mark_usd(legs["C"], S, K, T, "C"), mark_usd(legs["P"], S, K, T, "P")
    leg_prem = [c_px * n, p_px * n]
    premium = sum(leg_prem)
    cost_in = trade_cost_usd(leg_prem, S, n, pcfg)
    sigma = lock.get("implied_vol") or (float(legs["C"]["iv"]) + float(legs["P"]["iv"])) / 200
    # 卖跨式压力亏损：标的走出 stress_sigmas 个窗口标准差（用锁定隐含波动），减去收到的权利金，再加费用
    move = pcfg["stress_sigmas"] * sigma * math.sqrt(o["window_hours"] / (365 * 24)) * S
    delta0 = straddle_delta(S, K, T, sigma)
    paper = {**base, "ok": True, "expiry": expiry.isoformat(), "strikes": {"call": K, "put": K}, "S0": S, "T_years": T,
             "n_contracts": n, "legs": {"call": legs["C"]["n"], "put": legs["P"]["n"]},
             "marks_usd": {"call": c_px, "put": p_px}, "iv_legs": {"call": legs["C"]["iv"], "put": legs["P"]["iv"]},
             "sigma": sigma, "premium": premium, "fees_entry": cost_in,
             # 卖跨式仓位 delta = −delta0·n，对冲持 +delta0·n 标的
             "hedge": {"delta0": delta0, "units0": delta0 * n},
             "max_loss": {"A": max(move * n - premium, 0.0) + cost_in["total"], "B": premium + cost_in["total"], "C": 0.0,
                          "A_note": f"按 {pcfg['stress_sigmas']:.0f} 个隐含标准差（±{move / S * 100:.1f}%）压力测试，卖跨式理论上无上限"}}
    return paper


# ---------------------------------------------------------------- 结束时：结算
def _nearest(px: list[tuple[int, float]], t_ns: int) -> float | None:
    return min(px, key=lambda x: abs(x[0] - t_ns))[1] if px else None


def settle(settings, lock: dict, px: list[tuple[int, float]], a_ns: int, b_ns: int, end_items: list[dict] | None) -> dict:
    """结算三种结构。px = 窗口内指数序列 [(ns, price)]；end_items = 窗口结束时的期权快照（可为 None）。"""
    o = settings["options"]
    pcfg = o["paper"]
    pp = lock.get("paper") or {}
    out = {"chosen": pp.get("chosen", "C"), "reason": pp.get("reason"), "group": pp.get("group"),
           "n_history": pp.get("n_history", 0), "ok": False}
    if not pp.get("ok"):
        return {**out, "pnl": {"A_unhedged": 0.0, "A_hedged": 0.0, "B": 0.0, "C": 0.0}, "problems": pp.get("problems", ["锁定时没有纸面仓位"])}
    S0, K, n, sigma = pp["S0"], pp["strikes"]["call"], pp["n_contracts"], pp["sigma"]
    expiry = datetime.fromisoformat(pp["expiry"])
    end_dt = datetime.fromtimestamp(b_ns / 1e9, tz=UTC)
    S_mid, S_end = _nearest(px, (a_ns + b_ns) // 2), _nearest(px, b_ns)
    if S_end is None:
        return {**out, "pnl": {"A_unhedged": 0.0, "A_hedged": 0.0, "B": 0.0, "C": 0.0}, "problems": ["窗口内没有指数价"]}
    T_end = max((expiry - end_dt).total_seconds() / (365 * 86400), 0.0)
    # 平仓价：优先窗口结束快照里的同一合约标记价，否则内在价值
    by_name = {it["n"]: it for it in (end_items or [])}
    c_it, p_it = by_name.get(pp["legs"]["call"]), by_name.get(pp["legs"]["put"])
    if c_it and p_it and T_end > 0:
        S_ref = c_it["u"]
        c_out, p_out = mark_usd(c_it, S_ref, K, T_end, "C"), mark_usd(p_it, S_ref, K, T_end, "P")
        method = "窗口结束快照标记价"
    else:
        c_out, p_out = max(S_end - K, 0.0), max(K - S_end, 0.0)
        method = "指数内在价值"
    leg_out = [c_out * n, p_out * n]
    payout = sum(leg_out)
    cost_out = trade_cost_usd(leg_out, S_end, n, pcfg)
    fees_in = pp["fees_entry"]["total"]
    fees = fees_in + cost_out["total"]
    premium = pp["premium"]
    a_un = premium - payout - fees
    b_ = payout - premium - fees
    # 一次再平衡的 delta 对冲（卖跨式仓位 delta = −delta·n，对冲持 +delta·n 标的）
    T_mid = max((expiry - datetime.fromtimestamp((a_ns + b_ns) / 2e9, tz=UTC)).total_seconds() / (365 * 86400), 0.0)
    h0 = pp["hedge"]["units0"]
    h1 = straddle_delta(S_mid, K, T_mid, sigma) * n if S_mid is not None else h0
    hedge_pnl = h0 * ((S_mid if S_mid is not None else S_end) - S0) + h1 * (S_end - (S_mid if S_mid is not None else S_end))
    hedge_traded = abs(h0) * S0 + abs(h1 - h0) * (S_mid or S_end) + abs(h1) * S_end
    hedge_fee = pcfg["hedge_fee_pct"] / 100 * hedge_traded
    a_h = a_un + hedge_pnl - hedge_fee
    pnl = {"A_unhedged": a_un, "A_hedged": a_h, "B": b_, "C": 0.0}
    chosen = out["chosen"]
    chosen_pnl = pnl["A_unhedged"] if chosen == "A" else pnl[chosen]
    return {**out, "ok": True, "pnl": pnl, "chosen_pnl": chosen_pnl, "premium": premium, "payout": payout,
            "fees": {"entry": fees_in, "exit": cost_out["total"], "hedge": hedge_fee, "total": fees},
            "max_loss": pp["max_loss"], "expiry": pp["expiry"], "strikes": pp["strikes"], "settle_method": method,
            "S0": S0, "S_mid": S_mid, "S_end": S_end, "move_pct": (S_end / S0 - 1) * 100,
            "hedge": {"units0": h0, "units1": h1, "pnl": hedge_pnl, "fee": hedge_fee}}


# ---------------------------------------------------------------- 文案（中文大白话）
def _usd(x) -> str:
    return "—" if x is None else f"{x:+.2f} 美元"


def lock_text(lock: dict) -> str:
    pp = lock.get("paper")
    if not pp:
        return ""
    if not pp.get("ok"):
        return "纸面期权：没建仓——" + "；".join(pp.get("problems", []))
    iv = lock.get("implied_vol")
    L = [f"纸面期权（名义 {pp['notional_usd']:.0f} 美元 ≈ {pp['n_contracts']:.4f} 张）：隐含波动 {iv * 100:.1f}%（年化），"
         f"到期 {pp['expiry'][:10]}，行权价 {pp['strikes']['call']:.0f}，现价 {pp['S0']:.0f}。",
         f"A 卖跨式：收权利金 {pp['premium']:.2f} 美元，进场费用 {pp['fees_entry']['total']:.2f}，"
         f"最坏（{pp['max_loss']['A_note']}）亏 {pp['max_loss']['A']:.2f}。",
         f"B 买跨式：付权利金 {pp['premium']:.2f} 美元，最多亏 {pp['max_loss']['B']:.2f}。",
         "C 不动：0。",
         f"选了 {pp['chosen']} {NAMES[pp['chosen']]}：{pp['reason']}。"]
    return "\n".join(L)


def outcome_text(lock: dict, out: dict) -> str:
    pp = out.get("paper")
    if not pp:
        return ""
    if not pp.get("ok"):
        return "纸面期权：没结算——" + "；".join(pp.get("problems", []))
    p = pp["pnl"]
    rv = out.get("realized_vol")
    return (f"纸面期权结算（{pp['settle_method']}，标的 {pp['move_pct']:+.2f}%）：实际波动 {rv * 100:.1f}%。\n"
            f"A 卖跨式 {_usd(p['A_unhedged'])}（对冲后 {_usd(p['A_hedged'])}）；B 买跨式 {_usd(p['B'])}；C 不动 0。\n"
            f"选的是 {pp['chosen']} {NAMES[pp['chosen']]}：{_usd(pp['chosen_pnl'])}。费用合计 {pp['fees']['total']:.2f} 美元。"
            f"同组第 {pp['n_history'] + 1} 个样本。")


# ---------------------------------------------------------------- 汇总（网页卡片）
def summary(settings) -> dict:
    from .ledger import Ledger
    from .options import completed
    led = Ledger(settings.data, "options")
    recs = [r for r in completed(led) if r["outcome"].get("paper", {}).get("ok")]
    keys = ("A_unhedged", "A_hedged", "B", "C", "chosen")
    tot = {k: 0.0 for k in keys}
    by_type: dict[str, dict] = {}
    chosen_rows = []
    worst = None
    for r in recs:
        pp = r["outcome"]["paper"]
        et = pp.get("group") or r["lock"]["event_type"]
        d = by_type.setdefault(et, {k: 0.0 for k in keys} | {"n": 0})
        d["n"] += 1
        for k in keys[:4]:
            tot[k] += pp["pnl"][k]
            d[k] += pp["pnl"][k]
        tot["chosen"] += pp["chosen_pnl"]
        d["chosen"] += pp["chosen_pnl"]
        if pp["chosen"] != "C":
            chosen_rows.append(pp["chosen_pnl"])
        for k in ("A_unhedged", "B"):
            if worst is None or pp["pnl"][k] < worst["pnl"]:
                worst = {"sid": r["sid"], "structure": k, "pnl": pp["pnl"][k]}
    return {"n": len(recs), "total": tot, "by_type": by_type, "n_chosen": len(chosen_rows),
            "hit_rate": (sum(1 for x in chosen_rows if x > 0) / len(chosen_rows)) if chosen_rows else None,
            "hit_rate_A": (sum(1 for r in recs if r["outcome"]["paper"]["pnl"]["A_unhedged"] > 0) / len(recs)) if recs else None,
            "worst": worst}


def summary_text(c: dict) -> str:
    if not c["n"]:
        return "还没有结算过的纸面仓位。"
    t = c["total"]
    L = [f"{c['n']} 个样本累计：A 卖跨式 {t['A_unhedged']:+.2f}（对冲后 {t['A_hedged']:+.2f}）、B 买跨式 {t['B']:+.2f}、"
         f"按规则选边 {t['chosen']:+.2f} 美元（实际选边 {c['n_chosen']} 次"
         + (f"，胜率 {c['hit_rate'] * 100:.0f}%" if c["hit_rate"] is not None else "") + "）。",
         f"卖跨式单次赚钱比例 {c['hit_rate_A'] * 100:.0f}%。"]
    if c["worst"]:
        w = c["worst"]
        L.append(f"最差一笔：{w['sid']} {NAMES[w['structure'][0]]} {w['pnl']:+.2f} 美元。")
    return "\n".join(L)
