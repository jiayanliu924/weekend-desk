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

from . import rawstore


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


def fee_bps(settings, before_ns: int) -> dict:
    """Read growthMode from the latest parameter snapshot before `before_ns` (第八章参数快照)."""
    c = settings["costs"]
    growth = None
    rows = rawstore.query(settings.data, "hl_params", select="payload",
                          sql_where=f"received_at < {before_ns}") if rawstore.has_stream(settings.data, "hl_params") else []
    if rows:
        meta = json.loads(rows[-1][0]).get("asset_meta", {})
        growth = meta.get("growthMode") == "enabled"
    mult = (1 - c["growth_discount"]) if growth else 1.0
    return {"growth_mode": growth, "taker_bps": c["taker_bps"] * mult, "maker_bps": c["maker_bps"] * mult}


def decide_action(features: dict, pred: dict, fees: dict, cfg) -> dict:
    """模拟出手：预期收益（从当前中间价到重开价）超过成本 + 缓冲才出手；挂单进场、吃单离场。"""
    dev, p = features["dev_bps"], pred["pred_bps"]
    edge = p - dev  # 预期价格从现在到重开要走多少 bps
    half_spread = features.get("spread_bps", 0.0) / 2
    cost = fees["maker_bps"] + fees["taker_bps"] + half_spread
    threshold = cost + cfg["costs"]["edge_buffer_bps"]
    if abs(edge) <= threshold:
        return {"side": 0, "units": 0.0, "notional_usd": 0.0, "edge_bps": edge,
                "threshold_bps": threshold, "reason": "预期收益不足以覆盖成本，不出手"}
    side = 1 if edge > 0 else -1
    notional = cfg["risk"]["paper_notional_usd"]
    # 第八章：单笔不超过当时盘口可见深度的 10%（取买卖两侧较薄的一侧，偏保守）
    sides = [features.get("bid_depth_usd"), features.get("ask_depth_usd")]
    depth = min([d for d in sides if d] or [0]) or None
    capped = False
    if depth:
        cap = depth * cfg["risk"]["max_depth_fraction"]
        if notional > cap:
            notional, capped = cap, True
    entry = features.get("best_bid" if side > 0 else "best_ask") or features["dec_mid"]
    return {"side": side, "units": notional / cfg["risk"]["paper_notional_usd"], "notional_usd": notional,
            "entry_px": entry, "edge_bps": edge, "threshold_bps": threshold, "depth_capped": capped,
            "reason": ("判为信息周末，顺向" if features.get("news_weekend") else "判为噪音周末，反向")
                      + ("（单笔已按盘口深度 10% 截断）" if capped else "")}
