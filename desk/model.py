"""预测模型、基线与模拟出手规则（备忘 7.1、7.5、7.7）。

预测目标：外部价格恢复后的第一个价格，相对周五收盘价的变动（bps）。
基线 A：周五收盘价不变（预测 0）。
基线 B：直接用决策时刻的链上中间价（预测 = dev）。
H1 原样规则（v0）：无实质新闻 → 偏离被抹掉（预测 0）；有实质新闻 → 偏离被确认（预测 dev）。
攒够 min_train 个已完成周末后：线性模型 y = a·dev + b·dev·有新闻（无截距，按时间顺序只用过去的周末拟合）。
"""
from __future__ import annotations

import json

import numpy as np

from . import mark, rawstore


def predict(features: dict, history: list[dict], cfg) -> dict:
    dev = features["dev_bps"]
    news = 1.0 if features.get("news_weekend") else 0.0
    min_train = cfg["model"]["min_train"]
    usable = [h for h in history if h.get("y_bps") is not None and h.get("dev_bps") is not None]
    if len(usable) < min_train:
        pred = dev * news
        return {"pred_bps": pred, "model_version": "h1-rule-v0",
                "model_note": f"已完成周末 {len(usable)} 个，少于 {min_train}，按 H1 原样规则",
                "coef": {"quiet": 0.0, "news": 1.0}}
    X = np.array([[h["dev_bps"], h["dev_bps"] * (1.0 if h.get("news_weekend") else 0.0)] for h in usable])
    y = np.array([h["y_bps"] for h in usable])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    a, b = float(coef[0]), float(coef[1])
    return {"pred_bps": a * dev + b * dev * news, "model_version": f"ols-v1-n{len(usable)}",
            "model_note": "线性模型，只用过去的周末拟合", "coef": {"quiet": a, "news": a + b}}


def fee_bps(settings, before_ns: int, coin: str | None = None) -> dict:
    """Read growthMode from the latest parameter snapshot before `before_ns` (第八章参数快照)."""
    c = settings["costs"]
    growth = None
    where = f"received_at < {before_ns}" + (f" AND key = '{coin}'" if coin else "")
    rows = rawstore.query(settings.data, "hl_params", select="payload",
                          sql_where=where) if rawstore.has_stream(settings.data, "hl_params") else []
    if rows:
        meta = json.loads(rows[-1][0]).get("asset_meta", {})
        growth = meta.get("growthMode") == "enabled"
    mult = (1 - c["growth_discount"]) if growth else 1.0
    return {"growth_mode": growth, "taker_bps": c["taker_bps"] * mult, "maker_bps": c["maker_bps"] * mult}


def _worst_adverse_bps(features: dict, risk) -> float:
    """这个合约一笔最坏不良波动的估计（bps），用于首损上限。

    取"配置下限"和"本周末实际见过的最大偏离"两者的较大值——波动越大的合约（如韩股单票）下得越小。
    """
    floor = risk.get("worst_adverse_bps", 500.0)
    seen = features.get("max_abs_dev_bps") or 0.0
    return max(floor, seen)


def decide_action(features: dict, pred: dict, fees: dict, cfg) -> dict:
    """模拟出手：预期收益（从当前价到重开价）超过成本 + 缓冲才出手；挂单进场、吃单离场。

    在原来的基础上加了三条（借鉴竞品 Meridian/Keel 文档，见 desk/mark.py）：
    1. 插针/盘口脱节检测：中间价和标记价差太远，或周末有瞬时插针 → 本周不出手（防 SK 海力士式被冤枉）。
    2. 资金费率 carry：持仓期间空头收/多头付，折进出手门槛（对我们有利就降门槛，不利就抬门槛）。
    3. 首损 + 信心仓位：一笔最坏亏损不超过本金的 first_loss_frac；信号越强下得越多，勉强过线下得越少。
    """
    risk = cfg["risk"]
    dev, p = features["dev_bps"], pred["pred_bps"]
    edge = p - dev  # 预期价格从现在到重开要走多少 bps
    half_spread = features.get("spread_bps", 0.0) / 2
    cost = fees["maker_bps"] + fees["taker_bps"] + half_spread
    base_threshold = cost + cfg["costs"]["edge_buffer_bps"]

    # 1) 插针 / 盘口脱节 → 不出手
    wick = features.get("wick_ratio") or 1.0
    if features.get("dislocated") or wick > risk.get("wick_ratio_veto", 6.0):
        why = "中间价和标记价脱节（疑似插针）" if features.get("dislocated") else f"周末出现瞬时插针（插针比 {wick:.0f}）"
        return {"side": 0, "units": 0.0, "notional_usd": 0.0, "edge_bps": edge,
                "threshold_bps": base_threshold, "dislocated": bool(features.get("dislocated")),
                "reason": f"{why}，本周不出手（防被冤枉爆仓）"}

    # 2) 资金费率 carry：先按 edge 的方向估一侧，折进门槛
    side0 = 1 if edge > 0 else -1
    carry = mark.funding_carry_bps(side0, features.get("funding_avg"), features.get("hold_hours", 0.0))
    threshold = max(cost, base_threshold - carry)  # 有利的 carry 降门槛，不利的抬门槛，但不低于真实成本
    if abs(edge) <= threshold:
        return {"side": 0, "units": 0.0, "notional_usd": 0.0, "edge_bps": edge, "threshold_bps": threshold,
                "funding_carry_bps": carry, "reason": "预期收益（含资金费率）不足以覆盖成本，不出手"}
    side = 1 if edge > 0 else -1

    # 3) 仓位：信心缩放 → 首损上限 → 盘口深度 10%，取最小
    lo, hi = risk.get("conf_min", 0.5), risk.get("conf_max", 2.0)
    conf = max(lo, min(hi, abs(edge) / threshold)) if threshold > 0 else 1.0
    notional = risk["paper_notional_usd"] * conf
    worst = _worst_adverse_bps(features, risk)
    first_loss_cap = risk.get("first_loss_frac", 0.02) * risk["capital_usd"] / (worst / 1e4)
    notional = min(notional, first_loss_cap)
    capped_fl = notional >= first_loss_cap - 1e-9
    sides = [features.get("bid_depth_usd"), features.get("ask_depth_usd")]
    depth = min([d for d in sides if d] or [0]) or None
    capped_depth = False
    if depth:
        cap = depth * risk["max_depth_fraction"]
        if notional > cap:
            notional, capped_depth = cap, True
    entry = features.get("best_bid" if side > 0 else "best_ask") or features["dec_mid"]
    extra = []
    if capped_fl:
        extra.append("已按首损上限截断")
    if capped_depth:
        extra.append("已按盘口深度 10% 截断")
    return {"side": side, "units": notional / risk["paper_notional_usd"], "notional_usd": notional,
            "entry_px": entry, "edge_bps": edge, "threshold_bps": threshold, "funding_carry_bps": carry,
            "conf": conf, "first_loss_cap_usd": first_loss_cap, "worst_adverse_bps": worst,
            "depth_capped": capped_depth,
            "reason": ("判为信息周末，顺向" if features.get("news_weekend") else "判为噪音周末，反向")
                      + ("（" + "；".join(extra) + "）" if extra else "")}
