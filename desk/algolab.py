"""算法室的回测引擎：Agent 只能提"方案"（受限的 JSON 格式），数字全部由这里的代码算。

为什么这样设计：大模型自己报的回测数字不可信（会编、会挑好看的）。所以 Agent 只能写方案，
代码在历史数据上回测，并且：
- 前 2/3 的周末/日子用来"训练"（Agent 可以看），后 1/3 只用来"考试"（样本外）；
- 记下累计试过多少个方案，按试的次数提高显著性门槛（试得越多，越容易碰巧撞到好结果）；
- 永远和"现行规则"（H1：偏离超过门槛就反向）比。

周末方案格式：
{"kind": "weekend", "name": "...", "instruments": ["NVDA", ...] 或 "all", "direction": "fade"|"follow",
 "min_abs_dev_bps": 数字, "max_abs_dev_bps": 数字, "size": "flat"|"proportional"}
期权方案格式：
{"kind": "options", "name": "...", "side": "short"|"long", "when": "control"|"event"|"all",
 "min_implied": 0到3的年化波动（如 0.4 = 40%）, "max_implied": 数字}
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

TRAIN_FRAC = 2 / 3
OPT_COST_PCT = 0.10       # 期权一买一卖（跨式两腿）的手续费+价差，按标的价格的 0.10% 粗算
STRADDLE_K = 0.8          # 跨式价值 ≈ 0.8 · S · σ · √T


def _load(settings, name):
    p = Path(settings.data) / "reports" / name
    return json.loads(p.read_text()) if p.exists() else None


def weekend_cost_bps(settings) -> tuple[float, float]:
    c = settings["costs"]
    disc = 1 - c["growth_discount"]
    maker, taker = c["maker_bps"] * disc, c["taker_bps"] * disc
    cost = maker + taker + 0.5
    return cost, cost + c["edge_buffer_bps"]


def _split(dates: list[str]) -> str:
    ds = sorted(set(dates))
    if len(ds) < 3:
        return ds[-1] if ds else ""
    return ds[int(len(ds) * TRAIN_FRAC)]           # first test date


def _stats(xs: list[float]) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0, "mean": None, "hit": None, "t": None, "sum": 0.0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    t = m / (sd / math.sqrt(n)) if sd > 0 else None
    return {"n": n, "mean": m, "hit": sum(1 for x in xs if x > 0) / n, "t": t, "sum": sum(xs)}


def _p_from_t(t):
    return None if t is None else 0.5 * math.erfc(t / math.sqrt(2))     # one-sided, normal approx


def validate(spec: dict) -> tuple[dict | None, str]:
    if not isinstance(spec, dict):
        return None, "不是对象"
    k = spec.get("kind")
    name = str(spec.get("name", "未命名"))[:40]
    try:
        if k == "weekend":
            inst = spec.get("instruments", "all")
            if inst != "all":
                inst = [str(x).upper() for x in inst][:7]
            d = spec.get("direction", "fade")
            if d not in ("fade", "follow"):
                return None, "direction 只能是 fade/follow"
            lo = max(0.0, float(spec.get("min_abs_dev_bps", 0)))
            hi = float(spec.get("max_abs_dev_bps", 1e9))
            size = spec.get("size", "flat")
            if size not in ("flat", "proportional"):
                size = "flat"
            return {"kind": k, "name": name, "instruments": inst, "direction": d,
                    "min_abs_dev_bps": lo, "max_abs_dev_bps": hi, "size": size}, ""
        if k == "options":
            side = spec.get("side", "short")
            when = spec.get("when", "all")
            if side not in ("short", "long") or when not in ("control", "event", "all", "friday"):
                return None, "side/when 取值不对"
            return {"kind": k, "name": name, "side": side, "when": when,
                    "min_implied": float(spec.get("min_implied", 0)), "max_implied": float(spec.get("max_implied", 9))}, ""
    except (TypeError, ValueError) as e:
        return None, f"数字格式不对：{e}"
    return None, "kind 只能是 weekend/options"


def backtest_weekend(settings, spec: dict) -> dict:
    rows = _load(settings, "history_weekends.json") or []
    cost, threshold = weekend_cost_bps(settings)
    split = _split([r["weekend"] for r in rows])
    tr, te, base_tr, base_te = [], [], [], []
    for r in rows:
        nm = r.get("name", "XYZ100")
        dev, y = r["dev_bps"], r["y_bps"]
        # 现行规则（基线）：偏离超过门槛就反向
        if abs(dev) > threshold:
            b = -math.copysign(1, dev) * (y - dev) - cost
            (base_te if r["weekend"] >= split else base_tr).append(b)
        if spec["instruments"] != "all" and nm not in spec["instruments"]:
            continue
        if not (max(spec["min_abs_dev_bps"], threshold) < abs(dev) <= spec["max_abs_dev_bps"]):
            continue
        side = -math.copysign(1, dev) if spec["direction"] == "fade" else math.copysign(1, dev)
        w = min(3.0, abs(dev) / 50) if spec["size"] == "proportional" else 1.0
        pnl = w * (side * (y - dev) - cost)
        (te if r["weekend"] >= split else tr).append(pnl)
    return {"unit": "万分点/笔", "split": split, "train": _stats(tr), "test": _stats(te),
            "baseline_train": _stats(base_tr), "baseline_test": _stats(base_te), "cost": cost}


def backtest_options(settings, spec: dict) -> dict:
    h = _load(settings, "history_options.json") or {}
    rows = [dict(r, grp="event") for r in h.get("events", [])] + [dict(r, grp="control") for r in h.get("control", [])]
    split = _split([r["date"] for r in rows])
    tr, te, base_tr, base_te = [], [], [], []
    for r in rows:
        iv, ratio = r.get("implied"), r.get("ratio")
        if iv is None or ratio is None:
            continue
        # 一天期跨式（单位：标的价格的 %）：卖方赚 0.8·σi·√(1/365)·(1−实际/隐含)，减成本
        edge = STRADDLE_K * iv * math.sqrt(1 / 365) * (1 - ratio) * 100
        base = edge - OPT_COST_PCT                      # 基线：每天都卖
        (base_te if r["date"] >= split else base_tr).append(base)
        if spec["when"] == "friday":
            from datetime import date as _d
            if _d.fromisoformat(r["date"][:10]).weekday() != 4:      # Deribit 周度期权周五 08:00 UTC 到期
                continue
        elif spec["when"] != "all" and r["grp"] != spec["when"]:
            continue
        if not (spec["min_implied"] <= iv <= spec["max_implied"]):
            continue
        pnl = (edge if spec["side"] == "short" else -edge) - OPT_COST_PCT
        (te if r["date"] >= split else tr).append(pnl)
    return {"unit": "标的价格的%/笔", "split": split, "train": _stats(tr), "test": _stats(te),
            "baseline_train": _stats(base_tr), "baseline_test": _stats(base_te), "cost": OPT_COST_PCT,
            "note": "粗略近似：隐含波动用 30 天波动率指数代替一天期，没算极端行情下卖方的爆仓风险"}


def _trials_path(settings) -> Path:
    p = Path(settings.data) / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p / "algo_trials.jsonl"


def n_trials(settings) -> int:
    p = _trials_path(settings)
    return sum(1 for _ in p.open()) if p.exists() else 0


def run_spec(settings, raw: dict, who: str, run_id: str) -> dict:
    spec, err = validate(raw)
    if not spec:
        return {"spec": raw, "error": err}
    res = backtest_weekend(settings, spec) if spec["kind"] == "weekend" else backtest_options(settings, spec)
    with _trials_path(settings).open("a") as f:
        f.write(json.dumps({"ts": time.time(), "run": run_id, "who": who, "spec": spec,
                            "test_mean": res["test"]["mean"], "test_n": res["test"]["n"]}, ensure_ascii=False) + "\n")
    n = n_trials(settings)
    p = _p_from_t(res["test"]["t"])
    res["p_test"] = p
    res["trials_total"] = n
    res["bar"] = 0.05 / max(1, n)
    tm, bm = res["test"]["mean"], res["baseline_test"]["mean"]
    res["verdict"] = ("样本外笔数太少（<8），不算数" if res["test"]["n"] < 8 else
                      "样本外亏钱" if (tm or 0) <= 0 else
                      "样本外赚钱但不如现行规则" if bm is not None and tm <= bm else
                      "样本外赚钱且好于现行规则，但还没过多次尝试门槛" if p is None or p > res["bar"] else
                      "样本外显著好于现行规则（过了多次尝试门槛）")
    return {"spec": spec, "result": res}


def _f(x, nd=2):
    return "—" if x is None else f"{x:+.{nd}f}"


def describe(r: dict) -> str:
    """一行文字，喂回给 Agent，也显示在网页上。"""
    if r.get("error"):
        return f"方案无效：{r['error']}"
    s, x = r["spec"], r["result"]
    tr, te, bt = x["train"], x["test"], x["baseline_test"]
    hit = "—" if te["hit"] is None else f"{te['hit'] * 100:.0f}%"
    ps = "—" if x["p_test"] is None else f"{x['p_test']:.3f}"
    return (f"「{s['name']}」训练段 {tr['n']} 笔 平均 {_f(tr['mean'])}；考试段（{x['split']} 之后）{te['n']} 笔 平均 {_f(te['mean'])}、"
            f"胜率 {hit}、t={_f(te['t'])}；现行规则考试段平均 {_f(bt['mean'])}（单位 {x['unit']}）。"
            f"累计试过 {x['trials_total']} 个方案，显著门槛 p<{x['bar']:.4f}，本方案 p={ps}。结论：{x['verdict']}")


def data_summary(settings) -> str:
    rows = _load(settings, "history_weekends.json") or []
    h = _load(settings, "history_options.json") or {}
    cost, thr = weekend_cost_bps(settings)
    split_w = _split([r["weekend"] for r in rows])
    split_o = _split([r["date"] for r in h.get("events", []) + h.get("control", [])])
    return (f"可回测数据：周末 {len(rows)} 行（7 个合约 × 约 28 个周末，字段：合约、周五收盘到决策时的链上偏离 dev、开盘相对周五的变化 y），"
            f"考试段从 {split_w} 开始；单笔成本 {cost:.2f} 万分点，出手门槛 {thr:.2f}。"
            f"期权 {len(h.get('events', []))} 个事件日 + {len(h.get('control', []))} 个普通日（字段：隐含波动、实际/隐含比值），考试段从 {split_o} 开始。"
            f"训练段的逐笔数据你看不到，只能看汇总；考试段结果由代码给出。")
