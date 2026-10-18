"""策略实验室：用全部可得的周末历史，按"走前（walk-forward）"口径比较 反向 / 顺向 / 新闻条件 / 走前切换。

为什么要有它（2026-10-15）：最近 ~7 个周末（15m K 线只回到 08-23）反向亏、|偏离| 大时顺向赚；而更早的 28 周末回补
显示反向 +$205、胜率 59%。像是行情切换（战争 / 高波动），但 7 个周末太少，直接换主策略 = 对 7 个点过拟合。
这里把两段数据合起来，只用"严格早于交易那个周末"的信息做选择，样本外（OOS）结果才是头条。

数据（全部来自 data/backfill/ 的缓存，能连 Hyperliquid 时先刷新再合并；备忘 7.2：回补 K 线只供探索，不计入正式成绩）：
  - 1h K 线 candles_<coin>_1h.parquet —— `python -m desk backfill` 存的（Hyperliquid 只保留最近 5000 根 ≈ 208 天 ≈ 28 个周末）
  - 15m K 线 candles_<coin>_15m.parquet —— windows_backtest / universe 存的（≈ 52 天）；save_cache 现在会合并累积
  同一个周末两种都有时用 15m；只有 1h 时用 1h，误差见"口径说明"。
  - 新闻标签：账本 lock 记录里的 features.news_weekend（只有上线后的周末有；没有的不进"新闻条件"变体）

变体（全额手续费：挂单 3 + 吃单 9 + 半点差 2 = 14 bps；门槛 16 = 成本 + buffer 2）：
  fadeT / followT    T ∈ {16, 50, 100, 150}：|dev| > T 时逆 / 顺偏离方向。固定规则，没有要拟合的参数。
  news16             有新闻标签的周末：信息周末顺向、安静周末反向（|dev| > 16）
  regimeK            K ∈ {4, 6, 8}：这个合约过去 K 个周末"顺势 − 反向"实际盈亏之和的符号决定今天顺/反（model.regime_pick，
                     和实盘影子同一个函数）；不足 K 个默认顺势
  wf_select          每个品种族、每个周末：在 8 个固定变体里挑"过去所有周末累计净收益最高"的那个；最高的也 ≤ 0 → 不做
  v2                 （2026-10-15）主策略 v2 的回测：只做 [strategy].families_enabled（默认美股单票）、|dev| > 50 反向；
                     停手：同族严格更早的最近 K=4 个周末按同一规则的净 bps 之和 < 0 → 这周末不做（model.v2_stop，和实盘同一个函数）；
                     不足 4 个周末照常做。和"美股单票 / 反向 偏离>50"那一行之差 = 停手规则的效果。
  v3（2026-10-18，只在实验室）同一个单票反向>50 信号，按本金 % 记周收益：① 按信号平分 ② 按强度 ③ β 对冲 / 残差信号
                     ④ 挂单平仓 ⑤ 走前挑选；报告 Sharpe、回撤、20% 回撤下的杠杆和 52 周 −50% 概率（见 evaluate_v3）。
样本外 = 全局第 warmup 个日历周末之后（默认 8）。所有选择只看严格更早的周末（_past 构造；测试里用"改未来数据、
过去的决定不变"和"偷看会改答案"的夹具锁死）。有效样本按日历周末数算（同一周末的合约高度相关）。

用法：python3 -m desk.strategy_lab [--since 2026-03-01] [--warmup 8] [--offline] [--out data/reports/strategy_lab.md]
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import backtest_lib as bl
from .backtest_lib import INTERVAL_MS, price_at_or_after, price_at_or_before
from .clock import friday_of, name_of, weekend_for_friday
from .model import regime_pick, strategy_cfg, v2_stop

UTC = timezone.utc
THRESHOLDS = (16, 50, 100, 150)
KS = (4, 6, 8)
BASE_T = 16
RECENT_FROM = "2026-08-21"   # 15m K 线从 08-23 起；这之后的周末 = "最近 7 个周末"
FAMILY_ORDER = ("美指", "美股单票", "Brent", "韩股", "全部")
NAME_FAMILY = {"XYZ100": "美指", "SP500": "美指", "NVDA": "美股单票", "TSLA": "美股单票", "INTC": "美股单票",
               "BRENTOIL": "Brent", "SMSN": "韩股"}
SCHED_FAMILY = {"us_index": "美指", "us_equity": "美股单票", "metals_energy": "Brent", "kr_equity": "韩股"}
FETCH_DAYS = {"15m": 60, "1h": 215}


def fixed_variants() -> list[str]:
    return [f"{m}{t}" for m in ("fade", "follow") for t in THRESHOLDS]


def all_variants() -> list[str]:
    return fixed_variants() + [f"news{BASE_T}"] + [f"regime{k}" for k in KS] + ["wf_select", "v2"]


# 标签里不能有 "|"（会把 markdown 表格切开）；"偏离>50" 指 |dev| > 50 bps
VARIANT_CN = {**{f"fade{t}": f"反向 偏离>{t}" for t in THRESHOLDS}, **{f"follow{t}": f"顺向 偏离>{t}" for t in THRESHOLDS},
              f"news{BASE_T}": "新闻条件（信息→顺，安静→反）", **{f"regime{k}": f"走前切换 K={k}" for k in KS},
              "wf_select": "走前挑最优固定变体", "v2": "v2（单票反向>50 + 4 周末停手）"}


def family_of(inst: dict) -> str:
    if inst["name"] in NAME_FAMILY:
        return NAME_FAMILY[inst["name"]]
    from .universe import classify
    key, _ = classify(inst["coin"], inst["coin"].split(":")[0] if ":" in inst["coin"] else "")
    return SCHED_FAMILY.get(key, "其他")


def _sgn(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


# ---------------------------------------------------------------- 数据
def load_candles(settings, coin: str, fetch: bool) -> tuple[dict[str, list[dict]], list[str]]:
    """{interval: candles}；fetch=True 时先抓（节流 + 退避）再和缓存合并。"""
    out, notes = {}, []
    for iv in ("15m", "1h"):
        if fetch:
            try:
                cs = bl.fetch_candles_paged(coin, iv, FETCH_DAYS[iv])
                if cs:
                    bl.save_cache(settings, coin, iv, cs)
            except Exception as e:  # noqa: BLE001
                notes.append(f"{iv} 抓取失败：{type(e).__name__} {e}")
        got = bl.load_cache_iv(settings, coin, iv)
        if got and got[0]:
            out[iv] = got[0]
    return out, notes


def _skip_holiday(family: str, fri: date) -> bool:
    from .overnight_backtest import NYSE_HOLIDAYS
    from .windows_backtest import KRX_HOLIDAYS
    if family in ("美股单票", "美指"):
        return fri in NYSE_HOLIDAYS or (family == "美股单票" and fri + timedelta(days=3) in NYSE_HOLIDAYS)
    if family == "韩股":
        return fri in KRX_HOLIDAYS or fri + timedelta(days=3) in KRX_HOLIDAYS
    return False


def weekend_rows(settings, inst: dict, candles_by_iv: dict[str, list[dict]], now: datetime,
                 since: date | None = None) -> list[dict]:
    """每个已完成周末一行：dev（决策/关门）、move（平仓/决策）、y（平仓/关门），粒度优先 15m、退回 1h。"""
    fam = family_of(inst)
    idx = {iv: {int(c["t"]): c for c in cs} for iv, cs in candles_by_iv.items() if cs}
    if not idx:
        return []
    t0 = min(min(b) for b in idx.values())
    first = datetime.fromtimestamp(t0 / 1000, tz=UTC).date()
    fri = first + timedelta(days=(4 - first.weekday()) % 7)
    if since:
        fri = max(fri, since + timedelta(days=(4 - since.weekday()) % 7))
    rows = []
    while True:
        w = weekend_for_friday(fri, settings, inst)
        if w.exit > now:
            break
        if not _skip_holiday(fam, fri):
            for iv in ("15m", "1h"):
                by_t = idx.get(iv)
                if not by_t:
                    continue
                ivm = INTERVAL_MS[iv]
                p_c = price_at_or_before(by_t, ivm, w.close)
                p_d = price_at_or_before(by_t, ivm, w.decision, max_back=max(2, 7_200_000 // ivm))
                p_x = price_at_or_after(by_t, ivm, w.exit)
                if p_c and p_d and p_x:
                    rows.append({"fri": fri.isoformat(), "name": inst["name"], "family": fam, "gran": iv,
                                 "dev_bps": (p_d / p_c - 1) * 1e4, "move_bps": (p_x / p_d - 1) * 1e4,
                                 "y_bps": (p_x / p_c - 1) * 1e4, "news": None})
                    break
        fri += timedelta(days=7)
    return rows


def ledger_news(settings) -> dict[str, bool]:
    """{"周五日期|合约": news_weekend}，来自账本 lock 记录（只有上线后的周末有）。"""
    from .ledger import Ledger
    out = {}
    default = settings.instruments[0]["name"]
    for r in Ledger(settings.data).all():
        if r.get("kind") != "lock":
            continue
        f = r.get("features") or {}
        if "news_weekend" in f:
            out[f"{friday_of(r['weekend'])}|{name_of(r['weekend'], default)}"] = bool(f["news_weekend"])
    return out


# ---------------------------------------------------------------- 走前引擎
def _past(rows: list[dict], fri: str) -> list[dict]:
    """严格早于 fri 的行——走前引擎里所有"选择"只能看这个。"""
    return [r for r in rows if r["fri"] < fri]


def _net(side: int, r: dict, fee: float) -> float:
    return side * r["move_bps"] - fee


def fixed_side(variant: str, r: dict) -> int:
    mode = "fade" if variant.startswith("fade") else "follow"
    t = float(variant[len(mode):])
    if abs(r["dev_bps"]) <= t:
        return 0
    return (-1 if mode == "fade" else 1) * _sgn(r["dev_bps"])


def wf_choice(past: list[dict], family: str, fee: float, min_weekends: int) -> str | None:
    """走前挑选：过去（同族）累计净 bps 最高的固定变体；过去不足 min_weekends 个周末或最高 ≤ 0 → None（不做）。"""
    fam_rows = [r for r in past if family == "全部" or r["family"] == family]
    if len({r["fri"] for r in fam_rows}) < min_weekends:
        return None
    best, best_v = None, 0.0
    for v in fixed_variants():
        tot = sum(_net(s, r, fee) for r in fam_rows if (s := fixed_side(v, r)))
        if tot > best_v:
            best, best_v = v, tot
    return best


def v2_params(cfg=None) -> dict:
    """v2 的参数（不管 [strategy].version 是哪个，实验室都按 v2 参数回测）：族名换成实验室的中文族名。"""
    sc = strategy_cfg(cfg) if cfg is not None else strategy_cfg({})
    return {"families": {SCHED_FAMILY.get(k, k) for k in sc["families_enabled"]},
            "threshold": float(sc["fade_threshold_bps"]), "k": int(sc["stop_lookback_weekends"])}


def v2_state(past: list[dict], family: str, fee: float, v2p: dict) -> dict:
    """v2 停手状态：同族、严格早于本周末（past 由 _past 构造）的每个周末按"反向 |dev|>阈值"的净 bps 合计 → model.v2_stop。"""
    by: dict[str, float] = {}
    for p in past:
        if p["family"] != family:
            continue
        net = (-_sgn(p["dev_bps"]) * p["move_bps"] - fee) if abs(p["dev_bps"]) > v2p["threshold"] else 0.0
        by[p["fri"]] = by.get(p["fri"], 0.0) + net
    return v2_stop(sorted(by.items()), v2p["k"])


def v2_side(r: dict, past: list[dict], fee: float, v2p: dict, state: dict | None = None) -> int:
    if r["family"] not in v2p["families"] or abs(r["dev_bps"]) <= max(v2p["threshold"], BASE_T):
        return 0
    st = state if state is not None else v2_state(past, r["family"], fee, v2p)
    return 0 if st["stop"] else -_sgn(r["dev_bps"])


def decide(variant: str, r: dict, past: list[dict], fee: float, wf_pick: str | None = None,
           v2p: dict | None = None, v2st: dict | None = None) -> int:
    """单行在某变体下的方向（+1 多 / −1 空 / 0 不做）。past 只含严格早于 r 的周末。"""
    if variant in fixed_variants():
        return fixed_side(variant, r)
    if variant == "v2":
        return v2_side(r, past, fee, v2p or v2_params(), v2st)
    if abs(r["dev_bps"]) <= BASE_T:
        return 0
    if variant.startswith("news"):
        if r.get("news") is None:
            return 0
        return (1 if r["news"] else -1) * _sgn(r["dev_bps"])
    if variant.startswith("regime"):
        k = int(variant[len("regime"):])
        hist = [{"dev_bps": p["dev_bps"], "move_bps": p["move_bps"]}
                for p in sorted((p for p in past if p["name"] == r["name"]), key=lambda p: p["fri"])]
        pick = regime_pick(hist, k)["pick"]
        return (1 if pick == "follow" else -1) * _sgn(r["dev_bps"])
    if variant == "wf_select":
        return fixed_side(wf_pick, r) if wf_pick else 0
    raise ValueError(variant)


def decisions(rows: list[dict], fee: float, min_weekends: int, v2p: dict | None = None) -> dict[tuple[str, str, str], int]:
    """{(variant, fri, name): side} —— 每个周末只把 _past(rows, fri) 交给决策函数。"""
    out = {}
    v2p = v2p or v2_params()
    for fri in sorted({r["fri"] for r in rows}):
        past = _past(rows, fri)
        today = [r for r in rows if r["fri"] == fri]
        fams = {r["family"] for r in today}
        picks = {fam: wf_choice(past, fam, fee, min_weekends) for fam in fams}
        states = {fam: v2_state(past, fam, fee, v2p) for fam in fams}
        for r in today:
            for v in all_variants():
                out[(v, fri, r["name"])] = decide(v, r, past, fee, picks.get(r["family"]), v2p, states[r["family"]])
    return out


def v2_states(rows: list[dict], fee: float, v2p: dict) -> dict[tuple[str, str], dict]:
    """{(family, fri): v2 停手状态}（只算启用的族），报告用。"""
    out = {}
    for fri in sorted({r["fri"] for r in rows}):
        past = _past(rows, fri)
        for fam in sorted({r["family"] for r in rows if r["fri"] == fri} & v2p["families"]):
            out[(fam, fri)] = v2_state(past, fam, fee, v2p)
    return out


def wf_picks(rows: list[dict], fee: float, min_weekends: int) -> dict[tuple[str, str], str | None]:
    """{(family, fri): 走前选中的固定变体}，报告用。"""
    out = {}
    for fri in sorted({r["fri"] for r in rows}):
        past = _past(rows, fri)
        for fam in sorted({r["family"] for r in rows if r["fri"] == fri}):
            out[(fam, fri)] = wf_choice(past, fam, fee, min_weekends)
    return out


# ---------------------------------------------------------------- 统计
def stats(trades: list[dict], notional: float) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0, "n_weekends": 0}
    by_wk: dict[str, float] = {}
    for t in trades:
        by_wk[t["fri"]] = by_wk.get(t["fri"], 0.0) + t["net_usd"]
    wk_series = [by_wk[k] for k in sorted(by_wk)]
    eq = peak = dd = 0.0
    for x in wk_series:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    total = sum(t["net_usd"] for t in trades)
    top2 = sum(sorted((x for x in wk_series if x > 0), reverse=True)[:2])
    nw = len(wk_series)
    m = statistics.mean(wk_series)
    sd = statistics.stdev(wk_series) if nw > 1 else 0.0
    t_wk = (m / (sd / math.sqrt(nw))) if sd > 0 else float("nan")
    worst = min(trades, key=lambda t: t["net_bps"])
    return {"n": n, "n_weekends": nw, "hit_rate": sum(1 for t in trades if t["gross_bps"] > 0) / n,
            "avg_net_bps": statistics.mean(t["net_bps"] for t in trades), "total_usd": total,
            "worst_bps": worst["net_bps"], "worst_label": f"{worst['fri']} {worst['name']}", "max_dd_usd": dd,
            "ex_top2_usd": total - top2, "t_weekend": t_wk}


MIN_OOS_WEEKENDS = 5


def verdict(st: dict) -> str:
    if not st.get("n"):
        return "没有出手"
    if st["n_weekends"] < MIN_OOS_WEEKENDS:
        return f"样本太少（{st['n_weekends']} 个周末 < {MIN_OOS_WEEKENDS}）"
    if st["total_usd"] <= 0:
        return "样本外为负"
    if st["ex_top2_usd"] <= 0:
        return "样本外为正，但靠 1–2 个周末"
    return "通过：样本外为正且不是靠 1–2 个周末"


def evaluate_rows(rows: list[dict], cfg, warmup: int = 8, notional: float = 1000.0, half_spread: float = 2.0) -> dict:
    c = cfg["costs"]
    fee = c["maker_bps"] + c["taker_bps"] + half_spread
    weekends = sorted({r["fri"] for r in rows})
    oos_from = weekends[warmup] if len(weekends) > warmup else None
    v2p = v2_params(cfg)
    dec = decisions(rows, fee, warmup, v2p)
    by_key = {(r["fri"], r["name"]): r for r in rows}
    trades: dict[str, list[dict]] = {v: [] for v in all_variants()}
    for (v, fri, name), side in dec.items():
        if not side:
            continue
        r = by_key[(fri, name)]
        gross = side * r["move_bps"]
        net = gross - fee
        trades[v].append({"fri": fri, "name": name, "family": r["family"], "gran": r["gran"], "side": side,
                          "dev_bps": r["dev_bps"], "gross_bps": gross, "net_bps": net, "net_usd": net / 1e4 * notional,
                          "oos": oos_from is not None and fri >= oos_from})
    res: dict = {"fee_bps": fee, "weekends": weekends, "oos_from": oos_from, "warmup": warmup, "notional": notional,
                 "oos": {}, "ins": {}, "recent": {}, "early": {}, "picks": wf_picks(rows, fee, warmup),
                 "v2p": v2p, "v2_states": v2_states(rows, fee, v2p)}
    for fam in FAMILY_ORDER:
        for v in all_variants():
            ts = [t for t in trades[v] if fam == "全部" or t["family"] == fam]
            res["ins"][(fam, v)] = stats(ts, notional)
            res["oos"][(fam, v)] = stats([t for t in ts if t["oos"]], notional)
            res["recent"][(fam, v)] = stats([t for t in ts if t["fri"] >= RECENT_FROM], notional)
            res["early"][(fam, v)] = stats([t for t in ts if t["fri"] < RECENT_FROM], notional)
    # 事后诸葛亮：全样本上最好的固定变体（这就是过拟合会给你的数字），对照走前挑选的样本外
    res["hindsight"] = {}
    for fam in FAMILY_ORDER:
        best = max(fixed_variants(), key=lambda v: res["ins"][(fam, v)].get("total_usd", float("-inf")))
        res["hindsight"][fam] = best
    res["trades"] = trades
    return res


# ---------------------------------------------------------------- v3：组合层面（每周末收益 = 本金的 %）
# 2026-10-18。只在实验室里，不改实盘决策路径。目标：同一个信号（美股单票 |dev|>50 反向，全额成本），提高单位风险收益。
#   base      每笔 1/N（N = 样本里的单票数，17），= 原来 "$1000/笔" 的口径换成本金 %
#   alloc     本周末出手的单票平分本金：1/n_signals，每只封顶 25%（信号少时剩下的留现金）
#   strength  权重 ∝ min(|dev|/50, 3)，本周末归一到 1，再每只封顶 25%（超出部分留现金，不再分配）
#   hedge     alloc + 用指数永续（XYZ100，没有就 SP500）对冲：β 只用严格更早的周末估；同一周末各单票的对冲先轧差，只交易净额
#   resid     同 hedge，但入场信号用残差偏离 dev − β·dev_指数（|残差|>50 才做、方向逆残差）
#   maker     alloc + 平仓挂单：限价 = 重开后那一根的收盘（"重开后中间价"的近似），下一根 K 线穿过（严格越过）限价才算成交，
#             成交 → 挂单费（3+3、无半点差）；没成交 → 下一根收盘吃单（≈ 重开 +30 分钟，全额 14）
#   select    走前挑选：每个周末在 2 种仓位 × 3 种对冲 × 2 种平仓 = 12 个组合里，挑"过去周末周收益 Sharpe 最高"的；
#             过去不足 warmup 个周末、或最高的过去均值 ≤ 0 → 不做
V3_CAP = 0.25
V3_STRENGTH_MAX = 3.0
BETA_WINDOW = 20          # 估 β 用最近多少个过去周末
BETA_MIN = 6              # 少于这么多个过去周末 → 不对冲（β = 0）
BETA_CLIP = (0.0, 3.0)
INDEX_PREF = ("XYZ100", "SP500")
DD_CAP = 0.20
L_MAX = 20.0              # 杠杆搜索上限（不代表交易所允许这么高）
L_STEP = 0.05
BOOT_WEEKS = 52
BOOT_PATHS = 10_000
RUIN_LEVEL = 0.5          # "−50% 路径" = 52 周内任意一周末净值 ≤ 起始本金的 50%
V3_SIZINGS = ("alloc", "strength")
V3_HEDGES = ("none", "hedge", "resid")
V3_EXITS = ("taker", "maker")
V3_REPORT = {"base": ("fixed", "none", "taker"), "alloc": ("alloc", "none", "taker"),
             "strength": ("strength", "none", "taker"), "hedge": ("alloc", "hedge", "taker"),
             "resid": ("alloc", "resid", "taker"), "maker": ("alloc", "none", "maker")}
V3_CN = {"base": "基准：每笔 1/N", "alloc": "① 按信号平分（封顶 25%）", "strength": "② 按信号强度 min(偏离/50, 3)",
         "hedge": "③ alloc + β 对冲", "resid": "③' 残差偏离信号 + β 对冲", "maker": "④ alloc + 挂单平仓",
         "select": "⑤ 走前挑选（12 组合）"}


def v3_grid() -> list[tuple[str, str, str]]:
    return [(a, h, x) for a in V3_SIZINGS for h in V3_HEDGES for x in V3_EXITS]


def _bar_at_or_after(by_t: dict[int, dict], iv: int, t: datetime, max_fwd: int = 4) -> tuple[int, dict] | None:
    """和 backtest_lib.price_at_or_after 同一根 K 线，但返回 (开盘时间, K 线)。"""
    k = math.ceil(bl.ms(t) / iv) * iv - iv
    for i in range(max_fwd):
        c = by_t.get(k + i * iv)
        if c:
            return k + i * iv, c
    return None


def attach_exit_bars(settings, inst: dict, candles_by_iv: dict[str, list[dict]], rows: list[dict]) -> None:
    """给这个合约的行补上平仓后下一根 K 线（同粒度）：nx_hi_bps / nx_lo_bps 相对平仓价，move2_bps = 下一根收盘相对决策价。
    只给挂单平仓实验用；weekend_rows 本身不变（实盘停手回补也用它）。"""
    idx = {iv: {int(c["t"]): c for c in cs} for iv, cs in candles_by_iv.items() if cs}
    for r in rows:
        if r["name"] != inst["name"] or r["gran"] not in idx:
            continue
        by_t, ivm = idx[r["gran"]], INTERVAL_MS[r["gran"]]
        w = weekend_for_friday(date.fromisoformat(r["fri"]), settings, inst)
        got = _bar_at_or_after(by_t, ivm, w.exit)
        if not got:
            continue
        k, bar = got
        nxt = by_t.get(k + ivm)
        if not nxt:
            continue
        p_x = float(bar["c"])
        p_d = p_x / (1 + r["move_bps"] / 1e4)
        r["nx_hi_bps"] = (float(nxt["h"]) / p_x - 1) * 1e4
        r["nx_lo_bps"] = (float(nxt["l"]) / p_x - 1) * 1e4
        r["move2_bps"] = (float(nxt["c"]) / p_d - 1) * 1e4


def index_name(rows: list[dict]) -> str | None:
    names = {r["name"] for r in rows}
    return next((n for n in INDEX_PREF if n in names), None)


def beta_est(past: list[dict], name: str, idx: str | None, window: int = BETA_WINDOW, min_n: int = BETA_MIN) -> float | None:
    """单票对指数的 β：严格更早周末（past 由 _past 构造）的 y_bps（关门→平仓）做 OLS 斜率，取最近 window 个周末，
    少于 min_n 个或指数没波动 → None；结果夹在 BETA_CLIP。用 y 而不是 20 分钟的 move：样本少，周末全程的方差大、估得稳。"""
    if not idx:
        return None
    iy = {p["fri"]: p["y_bps"] for p in past if p["name"] == idx}
    pairs = sorted((p["fri"], p["y_bps"], iy[p["fri"]]) for p in past if p["name"] == name and p["fri"] in iy)[-window:]
    if len(pairs) < min_n:
        return None
    xs, ys = [p[2] for p in pairs], [p[1] for p in pairs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    if vx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    return min(max(b, BETA_CLIP[0]), BETA_CLIP[1])


def v3_weights(sigs: list[tuple[str, float]], sizing: str, n_universe: int, thr: float = 50.0,
               cap: float = V3_CAP) -> dict[str, float]:
    """sigs = [(合约, |信号偏离| bps)]（只含本周末出手的）→ {合约: 本金占比}。总和 ≤ 1。"""
    if not sigs:
        return {}
    if sizing == "fixed":
        return {k: min(1.0 / max(1, n_universe), cap) for k, _ in sigs}
    if sizing == "alloc":
        return {k: min(1.0 / len(sigs), cap) for k, _ in sigs}
    if sizing == "strength":
        raw = {k: min(abs(d) / thr, V3_STRENGTH_MAX) for k, d in sigs}
        tot = sum(raw.values())
        return {k: min(v / tot, cap) for k, v in raw.items()}
    raise ValueError(sizing)


def exit_leg(side: int, r: dict, exit_mode: str, fee: float, maker_fee: float) -> dict:
    """单笔平仓：返回 gross / fee（bps，相对这笔名义）和挂单状态。挂单：平多 = 卖限价，下一根最高价 > 限价才成交；平空反之。"""
    if exit_mode == "taker" or "nx_hi_bps" not in r:
        return {"gross": side * r["move_bps"], "fee": fee, "maker": None if exit_mode == "taker" else "nodata"}
    through = r["nx_hi_bps"] > 0 if side > 0 else r["nx_lo_bps"] < 0
    if through:
        return {"gross": side * r["move_bps"], "fee": maker_fee, "maker": "filled"}
    return {"gross": side * r["move2_bps"], "fee": fee, "maker": "unfilled",
            "slip": side * (r["move2_bps"] - r["move_bps"])}


def v3_week(today: list[dict], past: list[dict], combo: tuple[str, str, str], ctx: dict) -> dict:
    """一个周末在某组合下的收益（本金的比例）。today = 本周末所有行（决策时只用 dev，move 是结果），past = 严格更早的行。"""
    sizing, hedge, exit_mode = combo
    stocks = [r for r in today if r["family"] in ctx["families"]]
    idx_row = next((r for r in today if r["name"] == ctx["idx"]), None)
    betas = {}
    if hedge != "none" and idx_row is not None:
        for r in stocks:
            betas[r["name"]] = beta_est(past, r["name"], ctx["idx"]) or 0.0
    sigs, sides = [], {}
    for r in stocks:
        d = r["dev_bps"]
        if hedge == "resid" and r["name"] in betas:
            d = d - betas[r["name"]] * idx_row["dev_bps"]
        if abs(d) > max(ctx["thr"], BASE_T):
            sigs.append((r["name"], abs(d)))
            sides[r["name"]] = -_sgn(d)
    w = v3_weights(sigs, sizing, ctx["n_universe"], ctx["thr"])
    by = {r["name"]: r for r in stocks}
    legs, ret, H = [], 0.0, 0.0
    for name, wt in w.items():
        s, r = sides[name], by[name]
        leg = exit_leg(s, r, exit_mode, ctx["fee"], ctx["maker_fee"])
        leg.update(name=name, w=wt, side=s, net=leg["gross"] - leg["fee"], beta=betas.get(name, 0.0))
        legs.append(leg)
        ret += wt * leg["net"] / 1e4
        H -= s * wt * leg["beta"]
    idx_pnl = 0.0
    if H and idx_row is not None:
        idx_pnl = (H * idx_row["move_bps"] - abs(H) * ctx["fee"]) / 1e4
    return {"ret": ret + idx_pnl, "legs": legs, "hedge_w": H, "idx_pnl": idx_pnl,
            "gross_expo": sum(w.values()) + abs(H)}


def v3_series(rows: list[dict], combo: tuple[str, str, str], ctx: dict) -> dict[str, dict]:
    """{周五: v3_week}——每个日历周末（含不出手的，收益 0）；每个周末只把 _past(rows, fri) 交给决策。"""
    out = {}
    for fri in sorted({r["fri"] for r in rows}):
        out[fri] = v3_week([r for r in rows if r["fri"] == fri], _past(rows, fri), combo, ctx)
    return out


def _sharpe(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    sd = statistics.stdev(xs)
    return statistics.mean(xs) / sd if sd > 0 else None


def v3_select(series: dict[tuple, dict[str, dict]], weekends: list[str], min_weekends: int) -> dict[str, tuple | None]:
    """{周五: 选中的组合 或 None}：只用严格更早周末的周收益（这些周收益本身也只用了更早的数据）。"""
    picks = {}
    for i, fri in enumerate(weekends):
        prev = weekends[:i]
        best, best_s = None, None
        if len(prev) >= min_weekends:
            for combo, ser in series.items():
                xs = [ser[w]["ret"] for w in prev]
                sh = _sharpe(xs)
                if sh is None or statistics.mean(xs) <= 0:
                    continue
                if best_s is None or sh > best_s:
                    best, best_s = combo, sh
        picks[fri] = best
    return picks


def max_drawdown(rets: list[float], lev: float = 1.0) -> float:
    """复利净值的最大回撤（0.2 = −20%）；某周 1 + L·r ≤ 0 → 1（爆仓）。"""
    eq = peak = 1.0
    dd = 0.0
    for r in rets:
        g = 1 + lev * r
        if g <= 0:
            return 1.0
        eq *= g
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    return dd


def leverage_for_dd(rets: list[float], cap: float = DD_CAP, l_max: float = L_MAX, step: float = L_STEP) -> float:
    """历史最大回撤 ≤ cap 的最大杠杆（从 0 往上按 step 走，第一次超过就停）；没有亏损周 → l_max。"""
    lev, n = 0.0, 1
    while n * step <= l_max + 1e-12:
        if max_drawdown(rets, n * step) > cap:
            break
        lev = n * step
        n += 1
    return lev


def ruin_prob(rets: list[float], lev: float, weeks: int = BOOT_WEEKS, paths: int = BOOT_PATHS,
              level: float = RUIN_LEVEL, seed: int = 20261018) -> float | None:
    """独立重抽样（iid bootstrap）周收益 × 杠杆，52 周内净值任意时刻 ≤ level 的路径比例。固定种子，可复现。"""
    if not rets:
        return None
    rng = random.Random(seed)
    hit = 0
    for _ in range(paths):
        eq = 1.0
        for _ in range(weeks):
            eq *= max(0.0, 1 + lev * rng.choice(rets))
            if eq <= level:
                hit += 1
                break
    return hit / paths


def port_stats(rets: list[float], paths: int = BOOT_PATHS) -> dict:
    n = len(rets)
    if not n:
        return {"n_weekends": 0}
    m = statistics.mean(rets)
    sd = statistics.stdev(rets) if n > 1 else 0.0
    sh = m / sd if sd > 0 else None
    lev = leverage_for_dd(rets)
    return {"n_weekends": n, "mean": m, "sd": sd, "sharpe_w": sh, "sharpe_a": sh * math.sqrt(52) if sh is not None else None,
            "max_dd": max_drawdown(rets), "worst": min(rets), "t": sh * math.sqrt(n) if sh is not None else None,
            "lev": lev, "ret_at_lev": m * lev, "dd_at_lev": max_drawdown(rets, lev),
            "p_ruin": ruin_prob(rets, lev, paths=paths) if lev > 0 else 0.0}


def v3_ctx(rows: list[dict], cfg, half_spread: float = 2.0) -> dict:
    c = cfg["costs"]
    v2p = v2_params(cfg)
    return {"fee": c["maker_bps"] + c["taker_bps"] + half_spread, "maker_fee": 2 * c["maker_bps"],
            "thr": v2p["threshold"], "families": v2p["families"], "idx": index_name(rows),
            "n_universe": len({r["name"] for r in rows if r["family"] in v2p["families"]})}


def evaluate_v3(rows: list[dict], cfg, warmup: int = 8, half_spread: float = 2.0, paths: int = BOOT_PATHS) -> dict:
    ctx = v3_ctx(rows, cfg, half_spread)
    weekends = sorted({r["fri"] for r in rows})
    oos_from = weekends[warmup] if len(weekends) > warmup else None
    oos = [w for w in weekends if oos_from and w >= oos_from]
    combos = set(v3_grid()) | set(V3_REPORT.values())
    series = {cb: v3_series(rows, cb, ctx) for cb in combos}
    picks = v3_select({cb: series[cb] for cb in v3_grid()}, weekends, warmup)
    sel = {w: (series[picks[w]][w] if picks[w] else {"ret": 0.0, "legs": [], "hedge_w": 0.0, "idx_pnl": 0.0, "gross_expo": 0.0})
           for w in weekends}
    named = {k: series[cb] for k, cb in V3_REPORT.items()} | {"select": sel}
    out = {"ctx": ctx, "weekends": weekends, "oos_from": oos_from, "oos": oos, "picks": picks, "stats": {}, "extra": {}}
    for k, ser in named.items():
        rets = [ser[w]["ret"] for w in oos]
        out["stats"][k] = port_stats(rets, paths)
        legs = [lg for w in oos for lg in ser[w]["legs"]]
        ex = {"trades_per_wk": len(legs) / max(1, len(oos)),
              "gross_expo": statistics.mean(ser[w]["gross_expo"] for w in oos) if oos else 0.0,
              "hedge_pct": sum(ser[w]["idx_pnl"] for w in oos) / max(1, len(oos)),
              "avg_beta": statistics.mean(lg["beta"] for lg in legs) if legs else 0.0}
        mk = [lg for lg in legs if lg.get("maker") in ("filled", "unfilled")]
        if mk:
            filled = [lg for lg in mk if lg["maker"] == "filled"]
            ex["maker"] = {"n": len(mk), "fill_rate": len(filled) / len(mk),
                           "nodata": sum(1 for lg in legs if lg.get("maker") == "nodata"),
                           "saved_pct_wk": sum(lg["w"] * (ctx["fee"] - lg["fee"]) for lg in filled) / 1e4 / max(1, len(oos)),
                           "slip_pct_wk": sum(lg["w"] * lg["slip"] for lg in mk if lg["maker"] == "unfilled") / 1e4 / max(1, len(oos)),
                           "slip_bps_unfilled": statistics.mean(lg["slip"] for lg in mk if lg["maker"] == "unfilled")
                           if len(filled) < len(mk) else None}
        out["extra"][k] = ex
    return out


def _p(x, fmt="+.2f"):
    return "—" if x is None else format(x * 100, fmt) + "%"


V3_HEAD = ("| 变体 | 样本外周末 | 周均 % | 周标准差 % | 周 Sharpe | 年化 Sharpe | 最大回撤（1x） | 最差周末 | t(按周末) | "
           "杠杆 L（历史回撤≤20%） | L 下周收益 % | 52 周 −50% 概率（L） | 笔/周末 | 平均总敞口 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def v3_section_cn(r3: dict) -> list[str]:
    ctx = r3["ctx"]
    L = [f"同一个信号（{'、'.join(sorted(ctx['families']))} |dev|>{ctx['thr']:.0f} 反向，全额成本 {ctx['fee']:.0f} bps，挂单平仓成交时 "
         f"{ctx['maker_fee']:.0f} bps），换成**本金 %**：每个样本外日历周末一笔周收益（不出手 = 0）。N = {ctx['n_universe']} 只单票；"
         f"对冲指数 = {ctx['idx'] or '无（没有指数行，③ 等于 ①）'}。全部决策只看严格更早的周末。", "", *V3_HEAD]
    for k in list(V3_REPORT) + ["select"]:
        st, ex = r3["stats"][k], r3["extra"][k]
        if not st.get("n_weekends"):
            L.append(f"| {V3_CN[k]} | 0 |" + " — |" * 12)
            continue
        L.append(f"| {V3_CN[k]} | {st['n_weekends']} | {_p(st['mean'], '+.3f')} | {_p(st['sd'], '.3f')} | {_f(st['sharpe_w'], '+.2f')} | "
                 f"{_f(st['sharpe_a'], '+.2f')} | {_p(st['max_dd'], '.2f')} | {_p(st['worst'], '+.2f')} | {_f(st['t'], '+.2f')} | "
                 + (f"{st['lev']:.2f}{'（到上限）' if st['lev'] >= L_MAX else ''} | {_p(st['ret_at_lev'], '+.2f')} | "
                    f"{_p(st['p_ruin'], '.1f')} |" if st["mean"] > 0 else "—（周均 ≤ 0，不该加杠杆） | — | — |")
                 + f" {ex['trades_per_wk']:.1f} | {ex['gross_expo']:.2f} |")
    L.append("")
    for k in ("hedge", "resid"):
        ex = r3["extra"][k]
        L.append(f"- {V3_CN[k]}：平均 β {ex['avg_beta']:.2f}，指数腿（轧差后、含全额成本）平均每周末 {_p(ex['hedge_pct'], '+.3f')}。")
    mk = r3["extra"]["maker"].get("maker")
    if mk:
        L.append(f"- {V3_CN['maker']}：样本外 {mk['n']} 笔挂单，成交率 {mk['fill_rate'] * 100:.0f}%"
                 + (f"（另有 {mk['nodata']} 笔没有下一根 K 线，按吃单算）" if mk["nodata"] else "")
                 + f"；省下手续费 {_p(mk['saved_pct_wk'], '+.3f')}/周末，没成交那部分等 15 分钟的价格代价 {_p(mk['slip_pct_wk'], '+.3f')}/周末"
                 + (f"（没成交的平均 {mk['slip_bps_unfilled']:+.1f} bps/笔）" if mk["slip_bps_unfilled"] is not None else "")
                 + f"；净 {_p(mk['saved_pct_wk'] + mk['slip_pct_wk'], '+.3f')}/周末。1h 粒度的早期周末「下一根」是 1 小时，成交率偏乐观。")
    oos_picks = [r3["picks"][w] for w in r3["oos"]]
    cnt: dict[str, int] = {}
    for p in oos_picks:
        lab = "不做" if p is None else "/".join(p)
        cnt[lab] = cnt.get(lab, 0) + 1
    L.append("- ⑤ 走前挑选在样本外各周末选的组合（仓位/对冲/平仓）：" + "；".join(f"{k} × {v}" for k, v in sorted(cnt.items(), key=lambda x: -x[1])) + "。")
    L += ["- **杠杆不改变 Sharpe：** L 倍杠杆把周均收益、周标准差、回撤都放大 L 倍（回撤因复利略多于 L 倍），单位风险收益不变。"
          "想提高单位风险收益只能靠 ①–④ 这种改变收益分布本身的办法；L 只是选一个能承受的风险档位。",
          f"- L 的定义：在这 {len(r3['oos'])} 个样本外周末的复利净值上，历史最大回撤 ≤ {DD_CAP * 100:.0f}% 的最大杠杆（步长 {L_STEP}，上限 {L_MAX:.0f}，"
          f"上限只是搜索边界、不代表交易所允许）。**L 是用同一段样本外数据挑的 → 偏乐观**：未来的最大回撤几乎一定比这 {len(r3['oos'])} 周里见过的更深。",
          f"- −50% 概率：把样本外周收益独立重抽 {BOOT_WEEKS} 周 × {BOOT_PATHS:,} 条路径、乘 L、复利，净值任意时刻 ≤ 起始 50% 的比例。"
          "iid 重抽忽略了周与周之间的相关和没见过的尾部（最差也只能抽到样本外里最差的那周），**真实概率更高**。",
          "- 有效样本：同一周末的单票高度相关，独立样本 ≈ 周末数；t(按周末) 这里含不出手的周末（收益 0），和上面按出手周末算的 t 口径不同。",
          f"- 多重比较：①–④ 加 12 组合挑选，表里最好的一行有一部分是运气；⑤ 是唯一只看过去做选择的那一行，以它为准。"]
    return L


# ---------------------------------------------------------------- 旧口径复算（仪表盘"过去 28 个周末 $+204.90"）
def legacy_check(settings, candles_1h: dict[str, list[dict]]) -> dict | None:
    """复算 sim._history_weekend_trades 的口径：1h K 线，"决策价" = 重开那一刻的 K 线收盘，出场 = 重开后 1 小时，
    手续费按 growthMode 打一折（挂单 0.3 + 吃单 0.9 + 半点差 0.5 = 1.7 bps），门槛 3.7 bps；再按全额手续费重算一遍。"""
    from . import backfill
    rows = []
    for inst in settings.instruments:
        cs = candles_1h.get(inst["coin"])
        if cs:
            rows += backfill.weekend_table(settings, sorted(cs, key=lambda c: int(c["t"])), inst)
    if not rows:
        return None
    c = settings["costs"]
    disc = 1 - c["growth_discount"]
    legacy_cost = c["maker_bps"] * disc + c["taker_bps"] * disc + 0.5
    full_cost = c["maker_bps"] + c["taker_bps"] + 2.0

    def run(cost: float, thr: float) -> dict:
        nets = []
        for r in rows:
            if abs(r["dev_bps"]) <= thr:
                continue
            side = -_sgn(r["dev_bps"])
            nets.append((r["weekend"], side * (r["y_bps"] - r["dev_bps"]) - cost))
        n = len(nets)
        return {"n": n, "n_weekends": len({w for w, _ in nets}), "win": (sum(1 for _, x in nets if x > 0) / n) if n else None,
                "avg": (sum(x for _, x in nets) / n) if n else None, "total_1000": sum(x for _, x in nets) / 1e4 * 1000}
    return {"n_rows": len(rows), "n_weekends": len({r["weekend"] for r in rows}),
            "first": min(r["weekend"] for r in rows), "last": max(r["weekend"] for r in rows),
            "legacy_cost": legacy_cost, "full_cost": full_cost,
            "legacy": run(legacy_cost, legacy_cost + c["edge_buffer_bps"]),
            "full": run(full_cost, full_cost + c["edge_buffer_bps"])}


# ---------------------------------------------------------------- 报告
def _f(x, fmt="+.1f", dash="—"):
    return dash if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, fmt)


def _row(v: str, st: dict, label: str | None = None) -> str:
    lab = label or VARIANT_CN[v]
    if not st.get("n"):
        return f"| {lab} | 0 | 0 | — | — | — | — | — | — | — | 没有出手 |"
    return (f"| {lab} | {st['n']} | {st['n_weekends']} | {st['hit_rate'] * 100:.0f}% | {st['avg_net_bps']:+.1f} | "
            f"{st['total_usd']:+.2f} | {st['worst_bps']:+.0f} ({st['worst_label']}) | {st['max_dd_usd']:.2f} | "
            f"{st['ex_top2_usd']:+.2f} | {_f(st['t_weekend'], '+.2f')} | {verdict(st)} |")


HEAD = ("| 变体 | 笔数 | 周末数 | 命中率 | 平均净 bps | 累计 $（$1000/笔） | 最差一笔 | 最大回撤 $ | 去掉最好 2 周末 $ | t(按周末) | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|---|")


def summary_cn(res: dict, rows: list[dict]) -> list[str]:
    L = []
    if not rows:
        return ["没有任何周末样本（没有 K 线缓存）。到服务器上跑：`cd ~/weekend-desk && python3 -m desk.strategy_lab`。"]
    nw = len(res["weekends"])
    L.append(f"- 共 {len(rows)} 个（合约 × 周末）样本、{nw} 个日历周末（{res['weekends'][0]} → {res['weekends'][-1]}）；"
             f"前 {res['warmup']} 个周末只做训练，样本外从 {res['oos_from'] or '—'} 起，共 "
             f"{len([w for w in res['weekends'] if res['oos_from'] and w >= res['oos_from']])} 个周末。全额成本 {res['fee_bps']:.0f} bps/笔。")
    passed = [(fam, v) for fam in FAMILY_ORDER for v in all_variants() if verdict(res["oos"][(fam, v)]).startswith("通过")]
    if passed:
        L.append("- **样本外通过（为正且不是靠 1–2 个周末）的组合：** " +
                 "；".join(f"{fam} / {VARIANT_CN[v]}（{res['oos'][(fam, v)]['total_usd']:+.0f} $，"
                           f"{res['oos'][(fam, v)]['n_weekends']} 个周末）" for fam, v in passed))
    else:
        L.append("- **没有任何（品种族 × 变体）在样本外同时做到：为正、去掉最好 2 个周末仍为正、≥ 5 个周末。**")
    n_tests = len(FAMILY_ORDER) * len(all_variants())
    L.append(f"- 注意多重比较：{n_tests} 个组合里，就算全是噪音，也会有几个碰巧为正。一两个通过不等于找到了钱；"
             "同一结论要在多个品种族、走前选择（wf_select / regime）里都站得住才算。")
    for fam in FAMILY_ORDER:
        o = res["oos"]
        bits = []
        for v in ("fade16", "fade50", "follow50", "follow100", f"news{BASE_T}", "regime6", "wf_select"):
            st = o[(fam, v)]
            bits.append(f"{VARIANT_CN[v]} {_f(st.get('total_usd'), '+.0f')}$/{st.get('n_weekends', 0)}周末")
        hb = res["hindsight"][fam]
        L.append(f"- {fam}（样本外）：" + "；".join(bits) +
                 f"。事后看全样本最好的是「{VARIANT_CN[hb]}」（样本内 {_f(res['ins'][(fam, hb)].get('total_usd'), '+.0f')} $），"
                 f"走前挑选在样本外拿到 {_f(o[(fam, 'wf_select')].get('total_usd'), '+.0f')} $——两者之差就是过拟合的代价。")
    fr, fe = res["recent"][("全部", "fade16")], res["early"][("全部", "fade16")]
    wr, we = res["recent"][("全部", "follow50")], res["early"][("全部", "follow50")]
    flipped = (fe.get("avg_net_bps") is not None and fr.get("avg_net_bps") is not None
               and _sgn(fe["avg_net_bps"]) != _sgn(fr["avg_net_bps"]))
    n_recent = len([w for w in res["weekends"] if w >= RECENT_FROM])
    L.append(f"- 分时段（全部品种，样本内口径）：反向 偏离>16 在 {RECENT_FROM} 之前 {_f(fe.get('avg_net_bps'))} bps/笔（{fe.get('n', 0)} 笔），"
             f"之后 {_f(fr.get('avg_net_bps'))} bps/笔（{fr.get('n', 0)} 笔）；顺向 偏离>50 之前 {_f(we.get('avg_net_bps'))}，"
             f"之后 {_f(wr.get('avg_net_bps'))}。"
             + (f"反向的符号翻转了，像行情切换；但之后那段只有 {n_recent} 个周末，切换本身也可能是噪音。" if flipped
                else "反向前后没有翻转符号。"))
    wf_ok = [v for v in [f"regime{k}" for k in KS] + ["wf_select"] if verdict(res["oos"][("全部", v)]).startswith("通过")]
    if wf_ok:
        ok_names = "、".join(VARIANT_CN[v] for v in wf_ok)
        L.append("- **结论：** 只看过去的规则里「" + ok_names + "」在全部品种的样本外通过。"
                 "这是能用的证据，但仍是回补 K 线；先让实盘影子（看板「影子策略对照」）同口径攒够样本再考虑换主策略，不要直接切。")
    else:
        L.append("- **结论：主策略不盲目换。** 只看过去的规则（regime K=4/6/8、wf_select）在全部品种的样本外都没有通过；"
                 + ("固定的顺向 偏离>50 在最近这段好看，但那正是让我们注意到它的数据，拿它当证据就是过拟合。" if (wr.get("avg_net_bps") or 0) > 0 else "")
                 + "实盘先把 follow50 / fade50 / regime 三条影子同口径攒样本（看板「影子策略对照」）。")
    return L


def v2_section_cn(res: dict) -> list[str]:
    """"v2 和对照"：v2 vs 同族的固定反向>阈值（= v2 去掉停手）vs 走前挑选，样本外；停手省下/错过多少；分时段；$ 量级。"""
    v2p = res["v2p"]
    fams = [f for f in FAMILY_ORDER if f in v2p["families"]]
    thr, k = v2p["threshold"], v2p["k"]
    fade_v = f"fade{int(thr)}" if float(int(thr)) == thr and f"fade{int(thr)}" in fixed_variants() else None
    L = [f"v2 = 走前挑选（wf_select）在{'、'.join(fams) or '—'}上自己选中的「反向 偏离>{thr:.0f}」，只做这些族，"
         f"再加一条事先写死的停手：同族最近 {k} 个已完成周末按同一规则的全额成本净值之和 < 0 → 停一周（不足 {k} 个照做）。"
         f"成本 {res['fee_bps']:.0f} bps/笔。停手只看严格更早的周末。", ""]
    L += [*HEAD]
    for fam in fams:
        L.append(_row("v2", res["oos"][(fam, "v2")], f"{fam} · {VARIANT_CN['v2']}"))
        if fade_v:
            L.append(_row(fade_v, res["oos"][(fam, fade_v)], f"{fam} · {VARIANT_CN[fade_v]}（不停手）"))
        L.append(_row("wf_select", res["oos"][(fam, "wf_select")], f"{fam} · {VARIANT_CN['wf_select']}"))
    L.append(_row("v2", res["oos"][("全部", "v2")], f"全部 · {VARIANT_CN['v2']}"))
    L.append("")
    for fam in fams:
        oos_w = [w for w in res["weekends"] if res["oos_from"] and w >= res["oos_from"]]
        stopped = [w for w in oos_w if (res["v2_states"].get((fam, w)) or {}).get("stop")]
        if fade_v:
            skipped = sum(t["net_usd"] for t in res["trades"][fade_v] if t["family"] == fam and t["fri"] in stopped)
            L.append(f"- {fam}：样本外 {len(oos_w)} 个周末里停手 {len(stopped)} 个"
                     + (f"（{', '.join(stopped)}）" if 0 < len(stopped) <= 8 else "")
                     + (f"；这些周末不停手的话会是 {skipped:+.2f} $（负 = 停手省下，正 = 停手错过）。" if stopped else "。"))
        st, e, r = res["oos"][(fam, "v2")], res["early"][(fam, "v2")], res["recent"][(fam, "v2")]
        L.append(f"- {fam} 分时段（样本内口径）：v2 在 {RECENT_FROM} 之前 {_f(e.get('avg_net_bps'))} bps/笔（{e.get('n', 0)} 笔），"
                 f"之后 {_f(r.get('avg_net_bps'))} bps/笔（{r.get('n', 0)} 笔）"
                 + (f"；不停手的 {VARIANT_CN[fade_v]} 之前 {_f(res['early'][(fam, fade_v)].get('avg_net_bps'))}、"
                    f"之后 {_f(res['recent'][(fam, fade_v)].get('avg_net_bps'))}。" if fade_v else "。"))
        if st.get("n"):
            per_wk = st["total_usd"] / max(1, len(oos_w))
            L.append(f"- 量级：样本外每笔 ${res['notional']:,.0f} 名义，v2 合计 {st['total_usd']:+.2f} $ / {len(oos_w)} 个周末 ≈ "
                     f"{per_wk:+.2f} $/周末（按每 ${res['notional']:,.0f} 名义）。判定：{verdict(st)}。")
    L.append("- 上线门槛：实盘影子 v2 在 ≥ 8 个日历周末上全额手续费为正，**且**这里样本外仍通过；只满足一个不动真钱。")
    return L


def report_md(settings, res: dict, rows: list[dict], sources: dict[str, str], legacy: dict | None,
              since: date | None, r3: dict | None = None) -> str:
    L = [f"# 策略实验室：走前比较 反向 / 顺向 / 新闻条件 / 切换 / v2（{datetime.now(UTC):%Y-%m-%d %H:%M} UTC）", "",
         "回补 K 线，**只供探索、不计入正式成绩**（备忘 7.2）。头条是**样本外**（走前：每个周末的选择只看严格更早的周末）。", "",
         "## 结论（中文）", "", *summary_cn(res, rows), ""]
    if rows:
        L += ["## v2 和对照", "", *v2_section_cn(res), ""]
    if rows and r3 is not None:
        L += ["## v3：仓位、对冲、挂单平仓（本金 %，走前）", "", *v3_section_cn(r3), ""]
    L += ["## 数据", ""]
    for name, src in sources.items():
        rs = [r for r in rows if r["name"] == name]
        g = {iv: sum(1 for r in rs if r["gran"] == iv) for iv in ("15m", "1h")}
        span = f"{rs[0]['fri']} → {rs[-1]['fri']}" if rs else "—"
        L.append(f"- {name}（{family_of(settings.inst(name) or {'name': name, 'coin': name})}）：{len(rs)} 个周末（15m {g['15m']}，1h {g['1h']}），{span}；{src}")
    n_news = sum(1 for r in rows if r.get("news") is not None)
    L.append(f"- 新闻标签（账本 lock 的 news_weekend）：{n_news} 行有标签" + ("，新闻条件变体样本极少，只能看方向。" if n_news < 20 else "。"))
    if since:
        L.append(f"- 只用 {since} 之后的周末（--since）。")
    if not rows:
        L += ["", "**没有任何样本。** 本机连不上 Hyperliquid 时需要 data/backfill/ 的缓存；到服务器上跑一次即可。", "",
              bl.RUN_ON_HOST_HINT.replace("desk.windows_backtest --since 2026-04-01", "desk.strategy_lab")]
        return "\n".join(L)
    for title, key in (("样本外（头条）", "oos"), ("样本内（全样本，对照用；固定变体在这里挑 = 过拟合）", "ins")):
        L += ["", f"## {title}", ""]
        for fam in FAMILY_ORDER:
            L += [f"### {fam}", "", *HEAD]
            L += [_row(v, res[key][(fam, v)]) for v in all_variants()]
            L.append("")
    L += ["## 走前挑选每个周末选了什么", "", "| 周末 | " + " | ".join(f for f in FAMILY_ORDER if f != "全部") + " |",
          "|---|" + "---|" * (len(FAMILY_ORDER) - 1)]
    for fri in res["weekends"]:
        cells = [VARIANT_CN.get(res["picks"].get((f, fri)) or "", "不做" if (f, fri) in res["picks"] else "—")
                 for f in FAMILY_ORDER if f != "全部"]
        L.append(f"| {fri}{' (样本外)' if res['oos_from'] and fri >= res['oos_from'] else ''} | " + " | ".join(cells) + " |")
    L += ["", f"## 分时段：{RECENT_FROM} 之前 vs 之后（全部变体，样本内口径）", "",
          "| 变体 | 之前 笔数/周末 | 之前 平均净 bps | 之前 累计 $ | 之后 笔数/周末 | 之后 平均净 bps | 之后 累计 $ |", "|---|---|---|---|---|---|---|"]
    for v in all_variants():
        e, r = res["early"][("全部", v)], res["recent"][("全部", v)]
        L.append(f"| {VARIANT_CN[v]} | {e.get('n', 0)}/{e.get('n_weekends', 0)} | {_f(e.get('avg_net_bps'))} | {_f(e.get('total_usd'), '+.2f')} | "
                 f"{r.get('n', 0)}/{r.get('n_weekends', 0)} | {_f(r.get('avg_net_bps'))} | {_f(r.get('total_usd'), '+.2f')} |")
    L += ["", "## 仪表盘\"过去 28 个周末\"那个数是怎么来的（旧口径复算）", ""]
    if legacy:
        lg, fu = legacy["legacy"], legacy["full"]
        L += [f"来源：`python -m desk backfill` → `backfill.fetch_candles(coin, '1h', 200 天)` → `backfill.weekend_table` → "
              f"`data/reports/history_weekends.json` → `sim._history_weekend_trades`。这里用同一份 1h K 线复算："
              f"{legacy['n_rows']} 行、{legacy['n_weekends']} 个周末（{legacy['first']} → {legacy['last']}）。", "",
              "| 口径 | 成本 bps/笔 | 出手 | 周末数 | 胜率 | 平均净 bps | $1000/笔累计 |", "|---|---|---|---|---|---|---|",
              f"| 旧口径（growthMode 打一折 + 半点差 0.5） | {legacy['legacy_cost']:.1f} | {lg['n']} | {lg['n_weekends']} | "
              f"{_f(lg['win'] and lg['win'] * 100, '.0f')}% | {_f(lg['avg'])} | {lg['total_1000']:+.2f} |",
              f"| 全额手续费（挂 3 + 吃 9 + 半点差 2） | {legacy['full_cost']:.1f} | {fu['n']} | {fu['n_weekends']} | "
              f"{_f(fu['win'] and fu['win'] * 100, '.0f')}% | {_f(fu['avg'])} | {fu['total_1000']:+.2f} |", "",
              "旧口径三处偏乐观：(1) 手续费按 growthMode 打一折（1.7 bps，全额是 14）；(2) \"决策价\"取的是**重开那一刻**收盘的 1h K 线，"
              "出场是重开后 1 小时——吃的是「重开跳动 + 重开后一小时的漂移」，不是实盘的\"重开前 15 分钟进、重开后 5 分钟出\"；(3) 仪表盘按 $2,000×2 倍÷7 "
              "≈ $571/笔，不是 $1000。"]
    else:
        L.append("没有 1h K 线缓存，复算不了。")
    L += ["", "## 口径说明", "",
          f"- 每笔：|dev| 过门槛时开仓，净 = 方向·move − 全额成本 {res['fee_bps']:.0f} bps（挂单 3 + 吃单 9 + 半点差 2）；门槛 16 = 成本 + buffer 2。"
          "不含资金费率 carry（决策→平仓只有 20 分钟 / 1h 粒度约 2 小时，carry ≈ 0）、不含滑点。",
          "- 15m 粒度：关门价 = 关门前最后一根收盘；决策价 = 重开前 15 分钟之前最后一根（周末成交稀时最多往前找 2 小时）；"
          "平仓价 = 重开后 5 分钟之后第一根收盘（≈ 重开 +15 分钟）。",
          "- **1h 粒度（较早的周末）误差**：决策价是重开前 15 分钟之前最后一根**整点**收盘，实际 ≈ 重开前 60–75 分钟；"
          "平仓价 ≈ 重开后 60 分钟。也就是说 1h 行比实盘早进场约 1 小时、晚出场约 55 分钟——多吃了重开后第一小时的漂移；"
          "韩股 09:01 重开时决策价 ≈ 08:00、平仓 ≈ 10:00。所以早期（1h）和近期（15m）的差别里有一部分是口径，不全是行情。",
          "- 假日：美股单票跳过周五或周一是 NYSE 假日的周末，美指跳过周五假日，韩股跳过周五/周一 KRX 假日；Brent 不跳。",
          "- 走前：样本外 = 全局第 warmup 个日历周末之后；regime 和 wf_select 每个周末只看严格更早的周末；"
          "wf_select 至少要有 warmup 个过去周末才出手。固定变体没有要拟合的参数，但\"挑哪一个\"本身就是拟合——所以看 wf_select。",
          "- 有效样本按日历周末算：同一周末的 7 个合约会被同一个周一事件推同向；t(按周末) = 每个周末合计盈亏的均值 / 标准误。",
          "- 判定：通过 = 样本外累计 > 0、去掉最好的 2 个周末仍 > 0、且 ≥ 5 个周末。"]
    return "\n".join(L)


# ---------------------------------------------------------------- 入口
def build_rows(settings, now: datetime, since: date | None, fetch: bool, names=None) -> tuple[list[dict], dict, dict]:
    rows, sources, c1h = [], {}, {}
    for inst in settings.instruments:
        if names and inst["name"] not in names:
            continue
        cbi, notes = load_candles(settings, inst["coin"], fetch)
        if "1h" in cbi:
            c1h[inst["coin"]] = cbi["1h"]
        src = "，".join(f"{iv} 缓存 {len(cs)} 根" for iv, cs in cbi.items()) or "没有缓存"
        sources[inst["name"]] = src + (f"（{'；'.join(notes)}）" if notes else "")
        got = weekend_rows(settings, inst, cbi, now, since)
        attach_exit_bars(settings, inst, cbi, got)
        rows += got
    news = ledger_news(settings)
    for r in rows:
        r["news"] = news.get(f"{r['fri']}|{r['name']}")
    rows.sort(key=lambda r: (r["fri"], r["name"]))
    return rows, sources, c1h


def run(settings, since: date | None = None, warmup: int = 8, notional: float = 1000.0, half_spread: float = 2.0,
        fetch: bool = True, out: Path | None = None, now: datetime | None = None, names=None,
        capture: dict | None = None) -> str:
    """capture：传一个空 dict，会把 v3 结果放进 capture["r3"]（给 labs_auto 用，不影响报告）。"""
    now = now or datetime.now(UTC)
    rows, sources, c1h = build_rows(settings, now, since, fetch, names)
    res = evaluate_rows(rows, settings, warmup, notional, half_spread)
    r3 = evaluate_v3(rows, settings, warmup, half_spread) if rows else None
    if capture is not None:
        capture["r3"] = r3
    legacy = legacy_check(settings, c1h)
    md = report_md(settings, res, rows, sources, legacy, since, r3)
    out = out or Path(settings.data) / "reports" / "strategy_lab.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    return md


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", type=date.fromisoformat, default=None, help="只用这天之后的周末")
    ap.add_argument("--warmup", type=int, default=8, help="前多少个日历周末只做训练（默认 8）")
    ap.add_argument("--notional", type=float, default=1000.0)
    ap.add_argument("--half-spread", type=float, default=2.0)
    ap.add_argument("--offline", action="store_true", help="不连网络，只用 data/backfill/ 缓存")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    s = config.load()
    fetch = not a.offline
    if fetch:
        ok, why = bl.hyperliquid_reachable()
        if not ok:
            print(f"[strategy_lab] Hyperliquid 不可达（{why}），只用本地缓存。", file=sys.stderr)
            fetch = False
    print(run(s, a.since, a.warmup, a.notional, a.half_spread, fetch, a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
