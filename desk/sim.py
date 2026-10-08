"""如果放真钱：按每笔模拟/回测结果，换算成某个本金、杠杆、成交方式下的美元盈亏。

两种数据来源：
  live    系统上线后实时锁定的模拟交易（正式成绩）
  history 上线前用回补 K 线做的历史回测（仅供探索：小时 K 线、无新闻过滤，会偏乐观）

成本口径（备忘 7.5）：挂单进场 + 吃单离场（maker），或进出都吃单（taker）；已按链上 growthMode 打折。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import evaluate, options
from .ledger import Ledger


def _history_weekend_trades(settings, mode: str) -> list[dict]:
    p = Path(settings.data) / "reports" / "history_weekends.json"
    if not p.exists():
        return []
    c = settings["costs"]
    disc = 1 - c["growth_discount"]
    maker, taker = c["maker_bps"] * disc, c["taker_bps"] * disc
    half_spread = 0.5  # bps，周末盘口估计；实时模块用真实盘口
    cost = (maker + taker + half_spread) if mode == "maker" else (2 * taker + 2 * half_spread)
    threshold = maker + taker + half_spread + c["edge_buffer_bps"]
    out = []
    for r in json.loads(p.read_text()):
        dev, y = r["dev_bps"], r["y_bps"]
        nm = r.get("name", "XYZ100")
        if abs(dev) <= threshold:
            out.append({"date": r["weekend"], "name": nm, "side": 0, "net_bps": 0.0, "note": "偏离太小，不出手"})
            continue
        side = -1 if dev > 0 else 1          # H1 无新闻 → 反向押回撤（历史回测没有新闻数据，全按噪音周末处理）
        gross = side * (y - dev)
        out.append({"date": r["weekend"], "name": nm, "side": side, "net_bps": gross - cost,
                    "note": f"链上偏离 {dev:.0f} bps，重开 {y:.0f} bps"})
    return out


def _live_weekend_trades(settings, mode: str) -> list[dict]:
    from .clock import friday_of, name_of
    out = []
    for r in Ledger(settings.data).completed():
        nm, day = name_of(r["weekend"], settings.instruments[0]["name"]), friday_of(r["weekend"])
        if not evaluate.official(r, settings):
            continue
        a, o = r["lock"]["action"], r["outcome"]
        if not a.get("side"):
            out.append({"date": day, "name": nm, "side": 0, "net_bps": 0.0, "note": a.get("reason", "")})
            continue
        bps = o.get("pnl_net_bps", 0.0) if mode == "maker" else o.get("pnl_net_taker_bps", 0.0)
        out.append({"date": day, "name": nm, "side": a["side"], "net_bps": bps, "note": a.get("reason", "")})
    return out


def _live_option_trades(settings) -> list[dict]:
    """假设每次事件前买一个平值跨式（同时买认购和认沽），持有 24 小时。只是估算，系统本身不交易期权。"""
    out = []
    for r in options.completed(Ledger(settings.data, "options")):
        if not options.official(r) or r["lock"]["sample_kind"] != "event":
            continue
        iv, move = r["lock"]["implied_vol"], r["outcome"]["abs_move_bps"]
        premium = 0.798 * iv * math.sqrt(settings["options"]["window_hours"] / (365 * 24)) * 1e4  # 平值跨式成本 ≈ 0.8σ√T
        out.append({"date": r["sid"], "side": 1, "premium_bps": premium, "move_bps": move,
                    "net_bps": (move - premium) / premium * 1e4,   # 以权利金为本金的收益率（bps）
                    "note": f"隐含 {iv*100:.0f}%，实际涨跌 {move:.0f} bps，权利金约 {premium:.0f} bps"})
    return out


def run(settings, capital: float = 2000.0, leverage: float = 2.0, mode: str = "maker", source: str = "live") -> dict:
    capital = max(1.0, float(capital))
    leverage = min(max(0.1, float(leverage)), 30.0)
    trades = (_history_weekend_trades if source == "history" else _live_weekend_trades)(settings, mode)
    trades.sort(key=lambda t: (t["date"], t.get("name", "")))
    n_inst = max(1, len(settings.instruments))
    notional = capital * leverage / n_inst      # 本金平均分给每个合约，避免 7 个合约同时满仓
    per_inst = notional
    eq, peak, mdd, rows = capital, capital, 0.0, []
    for t in trades:
        pnl = notional * t["net_bps"] / 1e4
        eq += pnl
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak else 0)
        rows.append({**t, "pnl_usd": pnl, "equity": eq})
    traded = [r for r in rows if r["side"]]
    total = eq - capital
    weeks = max(1, len({r["date"] for r in rows}))
    by_name: dict[str, dict] = {}
    for r in rows:
        b = by_name.setdefault(r.get("name", "—"), {"n": 0, "traded": 0, "wins": 0, "pnl": 0.0, "worst": 0.0})
        b["n"] += 1
        if r["side"]:
            b["traded"] += 1
            b["wins"] += r["pnl_usd"] > 0
            b["worst"] = min(b["worst"], r["pnl_usd"])
        b["pnl"] += r["pnl_usd"]
    res = {
        "source": source, "mode": mode, "capital": capital, "leverage": leverage, "notional": notional,
        "per_instrument_notional": per_inst, "n_instruments": n_inst, "n_weekends": weeks, "by_name": by_name,
        "n": len(rows), "n_traded": len(traded), "total_usd": total, "return_pct": total / capital * 100,
        "annualized_usd": total / weeks * 52 if rows else 0.0,
        "win_rate": (sum(1 for r in traded if r["pnl_usd"] > 0) / len(traded)) if traded else None,
        "max_drawdown_pct": mdd * 100,
        "worst_usd": min((r["pnl_usd"] for r in traded), default=0.0),
        "best_usd": max((r["pnl_usd"] for r in traded), default=0.0),
        "rows": rows,
    }
    # 极端情形：所有合约同一个周末都押错，开盘反向再走 3%（过去没出现过，但迟早会有）
    res["stress_usd"] = -capital * leverage * 0.03
    res["stress_pct"] = -leverage * 3.0
    res["options"] = _live_option_trades(settings)
    return res
