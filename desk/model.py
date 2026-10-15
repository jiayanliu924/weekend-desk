"""预测模型、基线与模拟出手规则（备忘 7.1、7.5、7.7）。

预测目标：外部价格恢复后的第一个价格，相对周五收盘价的变动（bps）。
基线 A：周五收盘价不变（预测 0）。
基线 B：直接用决策时刻的链上中间价（预测 = dev）。
H1 原样规则（v0）：无实质新闻 → 偏离被抹掉（预测 0）；有实质新闻 → 偏离被确认（预测 dev）。
攒够 min_train 个已完成周末后：线性模型 y = a·dev + b·dev·有新闻（无截距，按时间顺序只用过去的周末拟合）。
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

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
    "v2": "v2（单票反向>50 + 近 4 周末为负停手）",
    "fade50_stocks": "单票反向>50（不停手，对照 v2 的停手效果）",
}

# ---------------------------------------------------------------- 策略版本（2026-10-15，见 knowledge/lessons.md 同日条目）
# v1 = 原来的 H1 规则（安静反向 / 信息顺向，门槛按链上 growthMode 费率）。
# v2 = 实验室走前挑选（wf_select）自己在美股单票上选中、且样本外通过的那条：|偏离| > 50 反向；只做 families_enabled 里的
#      品种族；出手门槛按全额手续费；再加一条事先写死（不按结果调）的停手规则：这个品种族最近 K=4 个已完成周末、
#      按 v2 规则算的全额手续费盈亏之和 < 0 → 本周末不出手；历史不足 K 个周末 → 照常出手。
STRATEGY_DEFAULTS = {"version": "v1", "families_enabled": ["us_equity"], "fade_threshold_bps": 50.0,
                     "stop_lookback_weekends": 4}
FAMILY_CN = {"us_equity": "美股单票", "us_index": "美指", "metals_energy": "Brent/金属能源", "kr_equity": "韩股"}


def strategy_cfg(cfg) -> dict:
    """[strategy] 段（缺省 = v1，旧配置/测试里的 dict 配置行为不变）。"""
    try:
        raw = cfg["strategy"]
    except (KeyError, TypeError):
        raw = None
    return {**STRATEGY_DEFAULTS, **(raw or {})}


def instrument_family(name_or_coin: str | None) -> str:
    """合约 → 品种族（复用 universe 的分类：XYZ100/SP500 → us_index，BRENTOIL → metals_energy，SMSN → kr_equity，
    NVDA/TSLA/INTC → us_equity）。认不出 → 'unknown'。"""
    if not name_or_coin:
        return "unknown"
    from .universe import classify
    key, _ = classify(name_or_coin, name_or_coin.split(":")[0] if ":" in name_or_coin else "", _family_overrides())
    return key


@functools.lru_cache(maxsize=1)
def _family_overrides() -> dict:
    """knowledge/universe_overrides.toml 里只取 [markets]（人工确认过的时间表），和 universe scan 同一份表；
    不取 [exclude] / [dex_default]——它们管"进不进候选"，不改一个已经在配置里的合约属于哪个族。"""
    from .universe import load_overrides
    try:
        return {"markets": load_overrides(Path(__file__).resolve().parent.parent).get("markets", {})}
    except Exception:   # noqa: BLE001 — 覆盖文件坏了不该让下单路径崩；命名规则照常生效
        return {}


def tradable_instruments(settings) -> list[dict]:
    """主策略实际会出手的合约（按合约平分本金时的分母）。v1：全部合约；v2：品种族在 families_enabled 里的合约
    （和 decide_v2 同一个品种族映射）。"""
    insts = list(settings.instruments)
    sc = strategy_cfg(settings)
    if sc["version"] != "v2":
        return insts
    fams = set(sc["families_enabled"])
    return [i for i in insts if features_family({"coin": i.get("coin"), "name": i.get("name")}) in fams]


def features_family(features: dict) -> str:
    fam = instrument_family(features.get("coin"))
    return fam if fam != "unknown" else instrument_family(features.get("name"))


def full_fees(cfg) -> dict:
    """全额手续费（不打 growthMode 折扣）：v2 的出手判断用它。"""
    c = cfg["costs"]
    return {"maker_bps": c["maker_bps"], "taker_bps": c["taker_bps"], "growth_mode": None, "full_fee": True}


def v2_rule_net_bps(h: dict, cfg) -> float:
    """一条已完成记录"如果当时按 v2 规则做"的全额手续费净 bps（不出手 = 0）。
    h = {dev_bps, move_bps, spread_bps?, dislocated?, wick_ratio?}；成本 = 挂单 + 吃单 + 半点差（缺点差按 2 bps）。"""
    sc = strategy_cfg(cfg)
    dev, mv = h.get("dev_bps"), h.get("move_bps")
    if dev is None or mv is None or abs(dev) <= sc["fade_threshold_bps"]:
        return 0.0
    if h.get("dislocated") or (h.get("wick_ratio") or 1.0) > cfg["risk"].get("wick_ratio_veto", 6.0):
        return 0.0
    c = cfg["costs"]
    hs = h["spread_bps"] / 2 if h.get("spread_bps") is not None else 2.0
    if abs(dev) <= c["maker_bps"] + c["taker_bps"] + hs + c["edge_buffer_bps"]:
        return 0.0
    return -_sgn(dev) * mv - (c["maker_bps"] + c["taker_bps"] + hs)


def v2_stop(weekend_pnls: list[tuple[str, float]], k: int) -> dict:
    """停手规则（事先写死，K 不按结果调）：weekend_pnls = [(周五日期, 这个品种族这个周末按 v2 规则的合计净 bps)]，
    只含严格早于本周末的已完成周末（调用方负责）。最近 k 个之和 < 0 → 停手；不足 k 个 → 照常出手。"""
    last = sorted(weekend_pnls)[-k:] if k > 0 else []
    if len(last) < k:
        return {"stop": False, "n": len(last), "k": k, "sum_bps": None,
                "why": f"该品种族已完成周末 {len(last)} 个，少于 {k}，照常出手"}
    tot = sum(x for _, x in last)
    return {"stop": tot < 0, "n": len(last), "k": k, "sum_bps": tot,
            "why": f"近 {k} 个周末按 v2 规则全额手续费合计 {tot:+.0f} bps"}


def v2_weekend_pnls(history: list[dict], cfg) -> list[tuple[str, float]]:
    """history = 这个品种族、严格早于本周末的已完成记录 [{weekend|fri, dev_bps, move_bps, ...}] → 每个日历周末的合计净 bps。"""
    by: dict[str, float] = {}
    for h in history:
        fri = (h.get("fri") or h["weekend"])[:10]
        by[fri] = by.get(fri, 0.0) + v2_rule_net_bps(h, cfg)
    return sorted(by.items())


def v2_stop_from_history(history, cfg, k: int) -> dict:
    """history = 行列表，或 jobs.v2_history 给的 {"rows": [...], "backfill_missing": bool, ...}。
    每行 {weekend|fri, dev_bps, move_bps, source: ledger|backfill_15m|backfill_1h, name}；按日历周末合计 v2 规则净 bps → v2_stop，
    并记下最近 k 个周末各自的来源、合约、净 bps（写进 lock，事后能查停手是被哪些周末触发的）。"""
    meta = history if isinstance(history, dict) else {}
    rows = (history.get("rows") if isinstance(history, dict) else history) or []
    by: dict[str, float] = {}
    src: dict[str, set] = {}
    names: dict[str, set] = {}
    for h in rows:
        fri = (h.get("fri") or h["weekend"])[:10]
        by[fri] = by.get(fri, 0.0) + v2_rule_net_bps(h, cfg)
        src.setdefault(fri, set()).add(h.get("source", "ledger"))
        names.setdefault(fri, set()).add(h.get("name") or (h.get("weekend") or "").split("|")[-1])
    st = v2_stop(sorted(by.items()), k)
    last = sorted(by)[-k:] if k > 0 else []
    st["weekends"] = [{"fri": f, "net_bps": by[f], "source": "+".join(sorted(src[f])), "names": sorted(names[f])} for f in last]
    st["sources"] = sorted(set().union(*(src[f] for f in last))) if last else []
    if meta.get("backfill_missing"):
        st["backfill_missing"] = True
        st["why"] += f"；停手判断只用了 {st['n']} 个周末（缺回补数据）"
    return st


def decide_v2(features: dict, cfg, history=None, use_stop: bool = True) -> dict:
    """v2：只做 families_enabled 里的品种族；|偏离| > fade_threshold_bps 才反向；门槛按全额手续费（decide_action_core
    里 cost = 挂单 + 吃单 + 半点差、再加 buffer）；use_stop 时近 K 个周末按 v2 规则为负就停手。仓位/插针否决照旧。"""
    sc = strategy_cfg(cfg)
    fam = features_family(features)
    dev = features["dev_bps"]
    if fam not in sc["families_enabled"]:
        return {**_no_trade("v2", "v2：该品种族样本外未通过，不出手"), "edge_bps": -dev, "family": fam}
    thr = float(sc["fade_threshold_bps"])
    k = int(sc["stop_lookback_weekends"])
    stop = v2_stop_from_history(history, cfg, k) if use_stop else None
    if abs(dev) <= thr:
        return {**_no_trade("v2", f"v2：|偏离| {abs(dev):.0f} ≤ {thr:.0f} bps，不出手"), "edge_bps": -dev, "family": fam,
                **({"stop": stop} if stop else {})}
    if use_stop:
        if stop["stop"]:
            return {**_no_trade("v2", f"v2：近 {k} 个周末该规则累计为负，停手一周（{stop['why']}）"),
                    "edge_bps": -dev, "family": fam, "stop": stop}
    a = decide_action_core(features, {"pred_bps": 0.0}, full_fees(cfg), cfg)
    a["strategy"] = "v2"
    a["family"] = fam
    a["fee_basis"] = "full"
    if stop is not None:
        a["stop"] = stop
    if a["side"]:
        a["direction"] = "fade"
        a["reason"] = (f"v2：{FAMILY_CN.get(fam, fam)} |偏离| {abs(dev):.0f} > {thr:.0f} bps，反向（全额成本门槛 "
                       f"{a['threshold_bps']:.0f} bps）" + (f"；{stop['why']}" if stop else ""))
    else:
        a["reason"] = "v2：" + a.get("reason", "")
    return a


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
    """direction = follow（pred = 2·dev → edge = +dev）或 fade（pred = 0 → edge = −dev）；仓位/否决/门槛全走 decide_action_core。"""
    dev = features["dev_bps"]
    a = decide_action_core(features, {"pred_bps": 2.0 * dev if direction == "follow" else 0.0}, fees, cfg)
    a["strategy"] = name
    a["direction"] = direction
    a["reason"] = f"{SHADOW_LABELS.get(name, name)}：" + a.get("reason", "")
    a.update(extra)
    return a


def shadow_actions(features: dict, fees: dict, cfg, history: list[dict] | None = None,
                   v2_history: list[dict] | None = None) -> dict:
    """并行影子策略：不下真单，只记录"如果这么做、会赚多少"，用来攒证据。主策略 action 不受影响。

    follow（顺势/追涨）：只在"信息周末、偏离过线"时顺着偏离方向做（押这波真新闻继续到重开），
      恰好补上真钱反向套利在信息周末"站一边"的那块。用和反向同等幅度(edge=dev)定仓位，便于同口径对比。
    follow50 / fade50（每个周末都记）：|dev| > 50 bps 才出手，分别顺着 / 逆着偏离。
    regime（每个周末都记）：regime_pick 用这个合约过去 K=6 个已完成周末决定今天顺势还是反向；不足 K 个默认顺势。
      history = 这个合约严格早于本周末的已完成周末 [{dev_bps, move_bps}]，按时间顺序（jobs.job_lock 从账本构造）。
    v2（每个周末都记，不管主策略是哪个版本）：decide_v2，含停手；v2_history = 同品种族严格早于本周末的已完成正式记录。
    fade50_stocks（每个周末都记）：同 v2 但没有停手——两条之差就是停手规则的效果。
    插针/脱节照样否决（decide_action_core 内已处理）。攒够样本、全额手续费下也稳定为正，才谈上真钱。
    """
    out: dict = {}
    dev = features.get("dev_bps")
    if dev is None:
        return out
    if features.get("news_weekend"):
        a = decide_action_core(features, {"pred_bps": 2.0 * dev}, fees, cfg)  # edge = pred - dev = dev，顺着偏离
        a["strategy"] = "follow"
        out["follow"] = a
    big = abs(dev) > SHADOW_DEV_BPS
    for name, direction in (("follow50", "follow"), ("fade50", "fade")):
        out[name] = (_shadow_trade(features, fees, cfg, name, direction) if big else
                     _no_trade(name, f"{SHADOW_LABELS[name]}：|偏离| {abs(dev):.0f} ≤ {SHADOW_DEV_BPS:.0f} bps，不出手"))
    rp = regime_pick(history or [], REGIME_K)
    out["regime"] = _shadow_trade(features, fees, cfg, "regime", rp["pick"], regime=rp)
    for name, use_stop in (("v2", True), ("fade50_stocks", False)):
        a = decide_v2(features, cfg, v2_history, use_stop=use_stop)
        a["strategy"] = name
        if name != "v2":
            a["reason"] = a["reason"].replace("v2：", f"{SHADOW_LABELS[name]}：", 1)
        out[name] = a
    return out


def decide_action(features: dict, pred: dict, fees: dict, cfg, v2_history: list[dict] | None = None) -> dict:
    """主策略出手。[strategy].version = "v2" → decide_v2（只做通过的品种族、|偏离|>50 反向、全额成本、4 周末停手；
    v2_history 见 decide_v2）；"v1"（或没有 [strategy] 段）→ 原 H1 规则 decide_action_core（fees 按链上费率）。"""
    if strategy_cfg(cfg)["version"] == "v2":
        a = decide_v2(features, cfg, v2_history, use_stop=True)
        a["strategy_version"] = "v2"
        return a
    return decide_action_core(features, pred, fees, cfg)


def decide_action_core(features: dict, pred: dict, fees: dict, cfg) -> dict:
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
