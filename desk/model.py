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


# 影子策略的固定参数（写死在代码里，改动 = 规则哈希变 = 新实验）。2026-10-15 加：最近 7 个周末反向亏、顺向在大偏离时赚，
# 但 7 个周末太少，不能直接换主策略（过拟合），所以三条一起影子跑、同口径攒样本，见 knowledge/lessons.md 同日条目。
SHADOW_DEV_BPS = 50.0        # follow50 / fade50：|dev| 超过它才出手
REGIME_K = 6                 # regime：看最近 K 个已完成周末"顺势减反向"的实际盈亏符号
SHADOW_LABELS = {
    "follow": "顺势（只在信息周末）",
    "follow50": "顺势 |偏离|>50",
    "fade50": "反向 |偏离|>50",
    "regime": f"切换（近 {REGIME_K} 周末谁赚跟谁）",
}


def _sgn(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def outcome_move_bps(lock: dict, outcome: dict) -> float | None:
    """一个已完成周末"从决策价到重开价"走了多少 bps（顺势为正 = 沿偏离方向继续走）。

    优先用决策中间价 → 重开价；缺中间价时用 y − dev 近似（都相对周五收盘价，差一个 (1+dev) 因子，可忽略）。
    """
    f = lock.get("features") or {}
    if not f.get("ok") or not outcome.get("ok"):
        return None
    dec, reopen = f.get("dec_mid"), outcome.get("reopen_px")
    if dec and reopen:
        return (reopen / dec - 1) * 1e4
    if outcome.get("y_bps") is not None and f.get("dev_bps") is not None:
        return outcome["y_bps"] - f["dev_bps"]
    return None


def regime_pick(history: list[dict], k: int = REGIME_K) -> dict:
    """走前切换：用"严格早于本周末"的最近 k 个周末，算 顺势 − 反向 的实际盈亏之和，正 → 顺势，负 → 反向。

    同一笔偏离上顺势和反向互为镜像、手续费相同，所以 顺势净 − 反向净 = 2·sign(dev)·move，只看符号：
    signal = Σ sign(dev_i)·move_i（i = 最近 k 个）。history 必须按时间顺序且只含过去（调用方负责，不许含本周末）。
    历史不足 k 个 → 默认顺势（对应"最近这段大偏离是真信息"的观察；样本攒够后自动由数据决定）。
    """
    usable = [h for h in history if h.get("dev_bps") is not None and h.get("move_bps") is not None]
    last = usable[-k:] if k > 0 else []
    if len(last) < k:
        return {"pick": "follow", "signal_bps": None, "n": len(last), "k": k,
                "why": f"已完成周末 {len(last)} 个，少于 {k}，默认顺势"}
    sig = sum(_sgn(h["dev_bps"]) * h["move_bps"] for h in last)
    pick = "follow" if sig >= 0 else "fade"
    return {"pick": pick, "signal_bps": sig, "n": len(last), "k": k,
            "why": f"最近 {k} 个周末 顺势−反向 合计 {2 * sig:+.0f} bps → {'顺势' if pick == 'follow' else '反向'}"}


def _no_trade(name: str, reason: str, **extra) -> dict:
    return {"side": 0, "units": 0.0, "notional_usd": 0.0, "strategy": name, "reason": reason, **extra}


def _shadow_trade(features: dict, fees: dict, cfg, name: str, direction: str, **extra) -> dict:
    """direction = follow（pred = 2·dev → edge = +dev）或 fade（pred = 0 → edge = −dev）；仓位/否决/门槛全走 decide_action。"""
    dev = features["dev_bps"]
    a = decide_action(features, {"pred_bps": 2.0 * dev if direction == "follow" else 0.0}, fees, cfg)
    a["strategy"] = name
    a["direction"] = direction
    a["reason"] = f"{SHADOW_LABELS.get(name, name)}：" + a.get("reason", "")
    a.update(extra)
    return a


def shadow_actions(features: dict, fees: dict, cfg, history: list[dict] | None = None) -> dict:
    """并行影子策略：不下真单，只记录"如果这么做、会赚多少"，用来攒证据。主策略 action 不受影响。

    follow（顺势/追涨）：只在"信息周末、偏离过线"时顺着偏离方向做（押这波真新闻继续到重开），
      恰好补上真钱反向套利在信息周末"站一边"的那块。用和反向同等幅度(edge=dev)定仓位，便于同口径对比。
    follow50 / fade50（每个周末都记）：|dev| > 50 bps 才出手，分别顺着 / 逆着偏离。
    regime（每个周末都记）：regime_pick 用这个合约过去 K=6 个已完成周末决定今天顺势还是反向；不足 K 个默认顺势。
      history = 这个合约严格早于本周末的已完成周末 [{dev_bps, move_bps}]，按时间顺序（jobs.job_lock 从账本构造）。
    插针/脱节照样否决（decide_action 内已处理）。攒够样本、全额手续费下也稳定为正，才谈上真钱。
    """
    out: dict = {}
    dev = features.get("dev_bps")
    if dev is None:
        return out
    if features.get("news_weekend"):
        a = decide_action(features, {"pred_bps": 2.0 * dev}, fees, cfg)  # edge = pred - dev = dev，顺着偏离
        a["strategy"] = "follow"
        out["follow"] = a
    big = abs(dev) > SHADOW_DEV_BPS
    for name, direction in (("follow50", "follow"), ("fade50", "fade")):
        out[name] = (_shadow_trade(features, fees, cfg, name, direction) if big else
                     _no_trade(name, f"{SHADOW_LABELS[name]}：|偏离| {abs(dev):.0f} ≤ {SHADOW_DEV_BPS:.0f} bps，不出手"))
    rp = regime_pick(history or [], REGIME_K)
    out["regime"] = _shadow_trade(features, fees, cfg, "regime", rp["pick"], regime=rp)
    return out


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
