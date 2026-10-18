"""跨 dex 同底层配对实验室：同一个底层在两个 HIP-3 dex 上各有一个永续，价差超过成本就做空贵的、做多便宜的，等收敛。

想法来源（2026-10-15 服务器 universe 扫描）：xyz:SNDK / io:SNDK、xyz:NBIS / io:NBIS、xyz:EWY / io:EWY 这类"同名同底层"，
以及 xyz:SP500 / mkts:US500、xyz:XYZ100 / mkts:USTECH 这类"代理对"（不是同一个指数 / 同一个预言机，单独报告）。
两腿方向相反 → 市场中性，理论上能比方向单扛更高杠杆；另外两边资金费率不同，持仓期间吃资金费差。

只做研究，不碰实盘代码（autotrade / execution / positions 一行没改）。

流程：
1. 配对发现：universe.discover()（或缓存 data/universe_raw.json）→ 去掉 dex 前缀按底层分组；别名表 US500≈SP500、USTECH≈XYZ100
   标为"代理对"。两条腿都要过 universe 的流动性门槛（24h 成交额、持仓、impact 点差）。
2. 数据：两条腿的 5m（或 1m / 15m）K 线（backtest_lib 的翻页抓取 + parquet 缓存累积 + 按权重节流）和 fundingHistory，
   只用两腿同一根 K 线都有成交的时刻对齐。
3. 回测：价差 s = (A/B − 1)·1e4 bps（代理对：log 比值减去严格之前 24h 均值）。进场门槛 = 4 笔成交全额成本 + {5, 15, 30, 60}；
   出场 {收敛到 0、回到一半、只按 24h 时间止损}（前两种也带 24h 时间止损）。成本：每笔吃单 9 bps（另算挂单 3 bps 情景）
   + 每腿半个 impact 点差，4 笔成交；资金费按小时逐条结算。
   信号在第 t 根 K 线收盘看到，第 t+1 根收盘成交（不在看到信号的同一个价成交）。
   走前：每个 UTC 日，只用"已经平仓、且平仓早于当天 0 点"的历史交易挑 12 个变体里累计净收益最高的；最高 ≤ 0 → 当天不做。
4. 每对：笔数、命中率、平均净 bps、$1,000/腿 的周盈亏、周 Sharpe、最大回撤、容量估计、使历史最差回撤 ≤ 20% 的杠杆和
   对应周收益、该杠杆下 52 周内 −50% 的 bootstrap 概率。
5. 诚实检查：不偷看、K 线收盘价不是 mark 也不是 mid、腿差压力（第二条腿晚一根 K 线成交）、资金费符号。

用法：python3 -m desk.pairs_lab [--days 60] [--interval 5m] [--warmup 5] [--offline] [--fixture data/universe_raw.json]
沙盒连不上 api.hyperliquid.xyz（代理 403）：打印"去服务器跑"并退回本地缓存；测试只用确定性假数据。
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import backtest_lib as bl
from . import universe as uv

UTC = timezone.utc
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS

ENTRY_BUFFERS = (5, 15, 30, 60)          # 进场门槛 = 4 笔成交成本 + buffer
EXITS = ("conv0", "half", "time24")
EXIT_CN = {"conv0": "收敛到 0", "half": "回到一半", "time24": "只按 24h"}
TIME_STOP_H = 24.0
FEE_SCENARIOS = {"taker": 9.0, "maker": 3.0}   # 每笔成交 bps；头条是 taker
MAX_DD_TARGET = 0.20
LEV_HARD_CAP = 10.0
IMPACT_NOTIONAL_USD = 10_000.0   # 假设：impactPxs 对应的冲击名义（Hyperliquid 未公开 HIP-3 的值，按量级估）
VLM_CAP_FRAC = 0.01              # 每笔每腿不超过较薄那条腿 24h 成交额的 1%
PROXY_WINDOW_H = 24.0
BOOT_N = 5000
BOOT_WEEKS = 52

# 别名 → 规范底层；命中别名的配对是"代理对"（不同指数 / 不同预言机，价差不一定收敛到 0）
ALIASES = {"US500": "SP500", "SPX": "SP500", "SPX500": "SP500", "USA500": "SP500",
           "USTECH": "XYZ100", "USTECH100": "XYZ100", "NAS100": "XYZ100", "US100": "XYZ100", "NDX": "XYZ100"}

RUN_ON_HOST_HINT = (
    "本机连不上 api.hyperliquid.xyz（代理 403）。到能访问 Hyperliquid 的机器（bot 服务器）上跑：\n"
    "    cd ~/weekend-desk && python3 -m desk.pairs_lab --days 60\n"
    "发现结果存 data/universe_raw.json，K 线 / 资金费率缓存到 data/backfill/；拷回来后本机可以\n"
    "    python3 -m desk.pairs_lab --offline\n"
    "离线重算。"
)


# ---------------------------------------------------------------- 1. 配对发现
def canonical(symbol: str) -> tuple[str, bool]:
    """→ (规范底层, 是否经过别名)。"""
    s = symbol.upper()
    return (ALIASES[s], True) if s in ALIASES else (s, False)


def discover_pairs(raw: dict, gate: dict) -> list[dict]:
    """同一规范底层、不同 dex 的两两组合。主 dex（加密）不在 raw['dexs'] 里，天然不参与。"""
    mk = [m for m in uv.parse_markets(raw) if not m.is_delisted and m.dex]
    groups: dict[str, list] = {}
    for m in mk:
        groups.setdefault(canonical(m.symbol)[0], []).append(m)
    out = []
    for und, ms_ in sorted(groups.items()):
        for a, b in itertools.combinations(sorted(ms_, key=lambda m: (m.dex, m.coin)), 2):
            if a.dex == b.dex:
                continue
            proxy = a.symbol.upper() != b.symbol.upper()
            ga, wa = uv.liquidity_gate(a, gate)
            gb, wb = uv.liquidity_gate(b, gate)
            out.append({"underlying": und, "a": a.coin, "b": b.coin, "kind": "proxy" if proxy else "same",
                        "gate_ok": ga and gb, "gate_reasons": [f"{a.coin}: {w}" for w in wa] + [f"{b.coin}: {w}" for w in wb],
                        "legs": {m.coin: _leg_snapshot(m) for m in (a, b)}})
    return out


def _leg_snapshot(m) -> dict:
    mm = (m.mark_px / m.mid_px - 1) * 1e4 if m.mark_px and m.mid_px else None
    return {"dex": m.dex, "mark": m.mark_px, "mid": m.mid_px, "oracle": m.oracle_px, "mark_vs_mid_bps": mm,
            "spread_bps": m.spread_bps, "day_ntl_vlm": m.day_ntl_vlm, "oi_usd": m.oi_usd, "funding": m.funding,
            "max_leverage": m.max_leverage, "only_isolated": m.only_isolated, "growth_mode": m.growth_mode}


# ---------------------------------------------------------------- 2. 数据
def leg_candles(settings, coin: str, interval: str, days: int, fetch: bool) -> tuple[list[dict], str]:
    if fetch:
        try:
            cs = bl.fetch_candles_paged(coin, interval, days)
            if cs:
                bl.save_cache(settings, coin, interval, cs)
                got = bl.load_cache_iv(settings, coin, interval)
                return got[0], f"API {interval}（本次 {len(cs)} 根，合并缓存 {len(got[0])} 根）"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__} {e}"
        else:
            err = "空"
    else:
        err = "离线"
    got = bl.load_cache_iv(settings, coin, interval)
    if got and got[0]:
        return got[0], f"缓存 {interval} {len(got[0])} 根（{err}）"
    return [], f"没有 {interval} K 线（{err}）"


def leg_funding(settings, coin: str, days: int, fetch: bool) -> tuple[list[dict], str]:
    if fetch:
        return bl.get_funding(settings, coin, days)
    rows = bl.load_funding_cache(settings, coin)
    return (rows, f"缓存 {len(rows)} 小时") if rows else ([], "没有资金费率缓存 → 按 0 计")


def align(ca: list[dict], cb: list[dict], iv_ms: int) -> list[tuple[int, float, float]]:
    """两腿同一根 K 线都有成交（v > 0）的时刻。返回 (价格时刻 = K 线收盘时刻 ms, A 收盘, B 收盘)。"""
    a = {int(c["t"]): float(c["c"]) for c in ca if float(c.get("v") or 0) > 0}
    b = {int(c["t"]): float(c["c"]) for c in cb if float(c.get("v") or 0) > 0}
    return [(t + iv_ms, a[t], b[t]) for t in sorted(a.keys() & b.keys())]


def spread_series(series: list[tuple[int, float, float]], kind: str, iv_ms: int) -> list[float | None]:
    """same：(A/B − 1)·1e4。proxy：log(A/B)·1e4 减去严格之前 24h 的均值（窗口不满 → None）。"""
    if kind == "same":
        return [(pa / pb - 1) * 1e4 for _, pa, pb in series]
    lr = [math.log(pa / pb) * 1e4 for _, pa, pb in series]
    win = max(2, int(PROXY_WINDOW_H * 3_600_000 // iv_ms))
    out: list[float | None] = []
    run = 0.0
    for i, x in enumerate(lr):
        if i >= win:
            out.append(x - run / win)
            run -= lr[i - win]
        else:
            out.append(None)
        run += x
    return out


# ---------------------------------------------------------------- 3. 回测
def funding_between(rates: list[dict], a_ms: int, b_ms: int) -> float:
    """(a, b] 内结算的每小时费率之和（小数）。"""
    return sum(r["rate"] for r in rates if a_ms < r["t"] <= b_ms)


def round_trip_cost(fee_fill: float, hs_a: float, hs_b: float) -> float:
    """4 笔成交（两腿各进各出）的成本，单位 = 每腿名义的 bps。"""
    return 2 * (fee_fill + hs_a) + 2 * (fee_fill + hs_b)


def simulate(series, spread, thr: float, exit_mode: str, cost: float, fa: list[dict], fb: list[dict],
             lag_b: int = 0, time_stop_h: float = TIME_STOP_H, notional: float = 1000.0) -> list[dict]:
    """一次只持一笔。第 i 根收盘看到 |s| > thr → 第 i+1 根收盘成交 A，B 在 i+1+lag_b。出场信号同理晚一根成交。

    s > 0 = A 贵：空 A 多 B。盈亏按每腿名义 bps：side_a·ΔA + side_b·ΔB − 成本 + 资金费（−side·Σ费率，正费率多头付）。
    """
    n = len(series)
    out = []
    i = 0
    while i < n:
        s0 = spread[i]
        if s0 is None or abs(s0) <= thr or i + 1 + lag_b >= n:
            i += 1
            continue
        sg = 1 if s0 > 0 else -1
        side_a, side_b = -sg, sg
        ea, eb = i + 1, i + 1 + lag_b
        t_sig = series[i][0]
        j, reason = ea, "eod"
        while j < n:
            sj = spread[j]
            if series[j][0] - series[ea][0] >= time_stop_h * 3_600_000:
                reason = "time"
                break
            if sj is not None and exit_mode == "conv0" and sj * sg <= 0:
                reason = "conv"
                break
            if sj is not None and exit_mode == "half" and sj * sg <= abs(s0) / 2:
                reason = "half"
                break
            j += 1
        xa = min(j + 1, n - 1)
        xb = min(j + 1 + lag_b, n - 1)
        if reason == "eod":
            xa = xb = n - 1
        xb = max(xb, eb)
        ta0, tb0, ta1, tb1 = series[ea][0], series[eb][0], series[xa][0], series[xb][0]
        gross = side_a * (series[xa][1] / series[ea][1] - 1) * 1e4 + side_b * (series[xb][2] / series[eb][2] - 1) * 1e4
        carry = bl.carry_bps(side_a, funding_between(fa, ta0, ta1)) + bl.carry_bps(side_b, funding_between(fb, tb0, tb1))
        net = gross - cost + carry
        out.append({"t_signal": t_sig, "t_entry": min(ta0, tb0), "t_exit": max(ta1, tb1), "day": t_sig // DAY_MS,
                    "s0": s0, "side_a": side_a, "gross_bps": gross, "carry_bps": carry, "cost_bps": cost,
                    "net_bps": net, "net_usd": net / 1e4 * notional, "reason": reason})
        i = max(xa, xb) + 1
    return out


def variants() -> list[tuple[int, str]]:
    return [(k, e) for k in ENTRY_BUFFERS for e in EXITS]


def vname(v: tuple[int, str]) -> str:
    return f"+{v[0]}/{v[1]}"


def run_variants(series, spread, cost, fa, fb, lag_b=0, notional=1000.0) -> dict[str, list[dict]]:
    return {vname(v): simulate(series, spread, cost + v[0], v[1], cost, fa, fb, lag_b, notional=notional) for v in variants()}


def walk_forward(by_v: dict[str, list[dict]], days: list[int], warmup: int) -> tuple[list[dict], dict[int, str | None]]:
    """每个 UTC 日 d（第 warmup 天起）：只看 t_exit < d 的 0 点的交易，挑累计净 bps 最高的变体；≤ 0 → 不做。
    当天成交 = 该变体信号日为 d 的交易（和上一笔已选交易重叠的跳过）。"""
    picks: dict[int, str | None] = {}
    chosen: list[dict] = []
    last_exit = -1
    for d in days[warmup:]:
        cut = d * DAY_MS
        best, best_v = 0.0, None
        for v, tr in by_v.items():
            sc = sum(t["net_bps"] for t in tr if t["t_exit"] < cut)
            if sc > best:
                best, best_v = sc, v
        picks[d] = best_v
        if best_v is None:
            continue
        for t in by_v[best_v]:
            if t["day"] == d and t["t_entry"] > last_exit:
                chosen.append({**t, "variant": best_v})
                last_exit = t["t_exit"]
    return chosen, picks


# ---------------------------------------------------------------- 4. 统计
def weekly(trades: list[dict], t0: int, t1: int) -> list[float]:
    """按平仓时刻落在第几周（从 t0 起每 7 天一格）汇总 $；没交易的周记 0。最后不满一周的那格丢掉。"""
    nw = max(0, (t1 - t0) // WEEK_MS)
    w = [0.0] * nw
    for t in trades:
        k = (t["t_exit"] - t0) // WEEK_MS
        if 0 <= k < nw:
            w[k] += t["net_usd"]
    return w


def max_drawdown(trades: list[dict]) -> float:
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: x["t_exit"]):
        eq += t["net_usd"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def bootstrap_ruin(week_ret: list[float], weeks: int = BOOT_WEEKS, n: int = BOOT_N, loss: float = 0.5, seed: int = 7) -> float | None:
    """有放回抽周收益、复利 52 周，路径上任一时刻净值 ≤ 1 − loss 的比例。周数 < 4 → None。"""
    if len(week_ret) < 4:
        return None
    rng = random.Random(seed)
    hit = 0
    for _ in range(n):
        eq = 1.0
        for _ in range(weeks):
            eq *= max(0.0, 1 + rng.choice(week_ret))
            if eq <= 1 - loss:
                hit += 1
                break
    return hit / n


def capacity_usd(avg_net_bps: float | None, hs_a: float, hs_b: float, vlm_min: float | None) -> float | None:
    """每腿名义上限的粗估：平方根冲击 cost(N) = 半点差·√(N/N0)，4 笔成交新增冲击吃掉全部平均净收益的 N；
    再和"较薄腿 24h 成交额 × 1%"取小。平均净 ≤ 0 → None。"""
    if avg_net_bps is None or avg_net_bps <= 0:
        return None
    h = max(0.5, (hs_a + hs_b) / 2)
    n_imp = IMPACT_NOTIONAL_USD * (1 + avg_net_bps / (4 * h)) ** 2
    return min(n_imp, vlm_min * VLM_CAP_FRAC) if vlm_min else n_imp


def pair_stats(trades: list[dict], t0: int, t1: int, notional: float, lev_cap: float) -> dict:
    st: dict = {"n": len(trades)}
    wk = weekly(trades, t0, t1)
    st["weeks"] = len(wk)
    if not trades:
        return st
    nets = [t["net_bps"] for t in trades]
    st.update(hit=sum(1 for x in nets if x > 0) / len(nets), avg_net_bps=statistics.mean(nets),
              total_usd=sum(t["net_usd"] for t in trades), avg_carry_bps=statistics.mean(t["carry_bps"] for t in trades),
              max_dd_usd=max_drawdown(trades))
    if wk:
        st["week_mean_usd"] = statistics.mean(wk)
        sd = statistics.stdev(wk) if len(wk) > 1 else 0.0
        st["week_sharpe"] = st["week_mean_usd"] / sd if sd > 0 else None
    if st.get("week_mean_usd", 0) > 0 and st["avg_net_bps"] > 0:
        dd_frac = abs(st["max_dd_usd"]) / notional
        lev = MAX_DD_TARGET / dd_frac if dd_frac > 0 else lev_cap
        lev = min(lev, lev_cap)
        st["leverage"] = lev
        st["week_ret_at_lev"] = st["week_mean_usd"] / notional * lev
        st["p_minus50_52w"] = bootstrap_ruin([w / notional * lev for w in wk])
    else:
        st["leverage"] = 0.0
    return st


# ---------------------------------------------------------------- 一对的完整评估
def evaluate_pair(series, kind: str, iv_ms: int, fa, fb, hs_a: float, hs_b: float, warmup: int,
                  notional: float = 1000.0, lev_cap: float = LEV_HARD_CAP, vlm_min: float | None = None) -> dict:
    spread = spread_series(series, kind, iv_ms)
    if not series:
        return {"ok": False, "why": "两腿没有对齐的 K 线"}
    days = sorted({t // DAY_MS for t, _, _ in series})
    if len(days) <= warmup:
        return {"ok": False, "why": f"只有 {len(days)} 天数据，≤ 走前预热 {warmup} 天"}
    t0, t1 = days[warmup] * DAY_MS, series[-1][0]
    res: dict = {"ok": True, "bars": len(series), "days": len(days), "oos_from": t0, "t_last": t1}
    sv = [x for x in spread if x is not None]
    res["spread"] = {"mean": statistics.mean(sv), "sd": statistics.pstdev(sv),
                     "p95_abs": sorted(abs(x) for x in sv)[int(0.95 * (len(sv) - 1))]} if sv else {}
    # 资金费差（A − B，每小时费率），对齐到两边都有的小时
    ra, rb = {r["t"]: r["rate"] for r in fa}, {r["t"]: r["rate"] for r in fb}
    common = sorted(ra.keys() & rb.keys())
    if common:
        diff = [ra[t] - rb[t] for t in common]
        m = statistics.mean(diff)
        res["funding_diff"] = {"hours": len(diff), "mean_bps_per_day": m * 24 * 1e4,
                               "same_sign_frac": sum(1 for x in diff if x * m > 0) / len(diff)}
    for scen, fee in FEE_SCENARIOS.items():
        cost = round_trip_cost(fee, hs_a, hs_b)
        by_v = run_variants(series, spread, cost, fa, fb, notional=notional)
        wf, picks = walk_forward(by_v, days, warmup)
        st = pair_stats(wf, t0, t1, notional, lev_cap)
        st["capacity_usd"] = capacity_usd(st.get("avg_net_bps"), hs_a, hs_b, vlm_min)
        st["cost_bps"] = cost
        st["picks"] = {str(d): v for d, v in picks.items()}
        st["grid"] = {v: {"n": len(tr), "avg_net_bps": statistics.mean([t["net_bps"] for t in tr]) if tr else None,
                          "total_usd": sum(t["net_usd"] for t in tr)} for v, tr in by_v.items()}
        res[scen] = st
        if scen == "taker":
            by_lag = run_variants(series, spread, cost, fa, fb, lag_b=1, notional=notional)
            wf_lag, _ = walk_forward(by_lag, days, warmup)
            res["legging"] = pair_stats(wf_lag, t0, t1, notional, lev_cap)
            res["trades_taker"] = wf
    return res


# ---------------------------------------------------------------- 报告
def _f(x, fmt="+.1f"):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, fmt)


def _usd(x):
    return "—" if x is None else (f"${x / 1e6:.2f}M" if abs(x) >= 1e6 else f"${x / 1e3:.1f}k" if abs(x) >= 1e4 else f"${x:,.0f}")


def _day(ms_: int) -> str:
    return datetime.fromtimestamp(ms_ / 1000, UTC).strftime("%m-%d")


HEAD = ("| 配对 | 天/样本外周 | 笔数 | 命中 | 平均净 bps | 周 $（$1k/腿） | 周 Sharpe | 最大回撤 $ | 容量/腿 | 杠杆(回撤≤20%，样本内挑) | 周收益@杠杆（样本内） | P(52 周内 −50%)（样本内） | 腿差压力 平均净 bps |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def _row(p: dict, scen: str = "taker") -> str:
    r = p["result"]
    name = f"{p['a']} / {p['b']}"
    if not r.get("ok"):
        return f"| {name} | — | — | — | — | — | — | — | — | — | — | — | {r.get('why', '')} |"
    st, lg = r[scen], r.get("legging", {})
    return (f"| {name} | {r['days']}/{st['weeks']} | {st['n']} | {_f(st.get('hit'), '.0%')} | {_f(st.get('avg_net_bps'))} | "
            f"{_f(st.get('week_mean_usd'), '+.2f')} | {_f(st.get('week_sharpe'), '+.2f')} | {_f(st.get('max_dd_usd'), '+.0f')} | "
            f"{_usd(st.get('capacity_usd'))} | {_f(st.get('leverage'), '.1f')}x | {_f(st.get('week_ret_at_lev'), '+.2%')} | "
            f"{_f(st.get('p_minus50_52w'), '.1%')} | {_f(lg.get('avg_net_bps')) if scen == 'taker' else '—'} |")


def report_md(res: dict) -> str:
    L = [f"# 跨 dex 同底层配对实验室（{res['generated_at'][:16]} UTC）", "",
         f"发现数据：{res['discovery_source']}。K 线 {res['interval']}，请求 {res['days']} 天；走前预热 {res['warmup']} 天，"
         f"之后每个 UTC 日只用更早已平仓的交易挑变体（{len(variants())} 个：进场 = 成本 + {{{', '.join(map(str, ENTRY_BUFFERS))}}} bps × "
         f"出场 {{{'、'.join(EXIT_CN.values())}}}，都带 {TIME_STOP_H:g}h 时间止损）。",
         f"成本：4 笔成交 × (吃单 {FEE_SCENARIOS['taker']:g} bps + 该腿半个 impact 点差)；挂单 {FEE_SCENARIOS['maker']:g} bps 情景另列。"
         f"盈亏单位 = 每腿 $1,000 名义（两腿共 $2,000 毛敞口）；资金费逐小时结算已计入。", ""]
    if res.get("errors"):
        L += ["**数据问题**：" + "；".join(res["errors"]), ""]
    same = [p for p in res["pairs"] if p["kind"] == "same" and p["gate_ok"]]
    proxy = [p for p in res["pairs"] if p["kind"] == "proxy" and p["gate_ok"]]
    failed = [p for p in res["pairs"] if not p["gate_ok"]]
    L += ["## 结论", ""]
    good = [p for p in same if p["result"].get("ok") and (p["result"]["taker"].get("week_mean_usd") or 0) > 0
            and (p["result"].get("legging", {}).get("avg_net_bps") or -1) > 0]
    if not same:
        L.append("- 没有两条腿都过流动性门槛的同底层配对。")
    elif not good:
        L.append("- 没有一对在样本外（走前、吃单全额成本、且腿差压力下）为正。按现有数据，这条线不值得上真钱。")
    else:
        L.append(f"- 样本外（吃单全额成本）为正且腿差压力下仍为正：{', '.join(p['a'] + '/' + p['b'] for p in good)}。")
        L.append("- 样本外周数很少（见表"
                 "“天/样本外周”），周 Sharpe 和 bootstrap 概率在 < 8 周时只能当量级参考，不能当证据。")
    L += ["- 上线门槛（建议，和主策略一致的口径）：服务器上累积 ≥ 8 个样本外周、吃单全额成本 + 腿差压力都为正，再考虑小额真钱。", ""]
    L += ["## 同底层配对（吃单 9 bps，头条）", "", *HEAD]
    L += [_row(p) for p in same] or ["| （无） |" + " |" * 12]
    L += ["", "## 同底层配对（挂单 3 bps 情景，只作上限参考：两腿都挂单会更常出现腿差）", "", *HEAD]
    L += [_row(p, "maker") for p in same] or ["| （无） |" + " |" * 12]
    L += ["", "## 代理对（不是同一个指数 / 预言机，单独报告）", "",
          "价差用 log(A/B) 减去**严格之前** 24h 均值，赌的是\"相对基差回归\"，不是\"收敛到 0\"；基差会漂，风险比同底层大。", "", *HEAD]
    L += [_row(p) for p in proxy] or ["| （无） |" + " |" * 12]
    L += ["", "## 每对细节", ""]
    for p in same + proxy:
        r = p["result"]
        L.append(f"### {p['a']} / {p['b']}（{'代理对' if p['kind'] == 'proxy' else '同底层'}）")
        L.append(f"- 数据：{p.get('source', '')}")
        if not r.get("ok"):
            L += [f"- {r.get('why')}", ""]
            continue
        sp, fd = r.get("spread", {}), r.get("funding_diff")
        L.append(f"- 价差 bps：均值 {_f(sp.get('mean'))}、标准差 {_f(sp.get('sd'), '.1f')}、|价差| 95 分位 {_f(sp.get('p95_abs'), '.1f')}；"
                 f"4 笔成本 {r['taker']['cost_bps']:.1f} bps（吃单）。样本外从 {_day(r['oos_from'])} 起。")
        if fd:
            L.append(f"- 资金费差（A − B）：平均 {_f(fd['mean_bps_per_day'], '+.2f')} bps/天，{fd['same_sign_frac']:.0%} 的小时同号"
                     f"（{fd['hours']} 小时）。持仓期间平均 carry {_f(r['taker'].get('avg_carry_bps'), '+.2f')} bps/笔。")
        else:
            L.append("- 资金费：至少一条腿没有数据，carry 按 0 计。")
        g = r["taker"]["grid"]
        top = sorted(g.items(), key=lambda kv: -kv[1]["total_usd"])[:3]
        L.append("- 全样本（含预热，**事后看的，不是成绩**）前 3 变体：" +
                 "；".join(f"{v} {x['n']} 笔 {_f(x['avg_net_bps'])} bps" for v, x in top))
        picks = [v for v in r["taker"]["picks"].values()]
        L.append(f"- 走前每日选择：{sum(1 for v in picks if v)} / {len(picks)} 天有选中变体；"
                 f"最常选 {max(set(v for v in picks if v), key=picks.count) if any(picks) else '—'}。")
        L.append("")
    if failed:
        L += ["## 没过流动性门槛的配对", "", "| 配对 | 原因 |", "|---|---|"]
        L += [f"| {p['a']} / {p['b']} | {'；'.join(p['gate_reasons'])} |" for p in failed]
        L.append("")
    L += ["## 诚实检查", "",
          "- **不偷看**：信号在第 t 根 K 线收盘，成交在第 t+1 根收盘；走前挑变体只用平仓时刻早于当天 0 点的交易"
          "（测试里锁死：改未来数据，过去的选择不变）。代理对的去均值窗口只用严格之前的 K 线。",
          "- **mark vs mid**：K 线收盘价是**最后成交价**，既不是 mark 也不是 mid。薄的腿上最后成交在买卖价之间跳（bid-ask bounce），"
          "会把价差放大、制造假的\"收敛\"。缓解：只用两腿同一根 K 线都有成交的时刻、晚一根成交、每笔成交扣半个 impact 点差。"
          "残余偏差方向是**偏乐观**。发现快照里各腿 mark − mid：" +
          "、".join(f"{c} {_f(l['mark_vs_mid_bps'], '+.1f')} bps" for p in same + proxy for c, l in p["legs"].items()) + "。",
          "- **腿差压力**：第二条腿（B）进出都晚一根 K 线成交；表里最后一列。真实执行两腿不可能同时成交，这一列比头条更接近实盘。",
          "- **资金费符号**：Hyperliquid 费率为正 → 多头付、空头收；一条腿的 carry = −方向 × Σ(持仓期间每小时费率) × 1e4 bps"
          "（backtest_lib.carry_bps，测试锁死）。按开仓名义近似，未按持仓期间 mark 变化调整。",
          "- **杠杆**：\"回撤 ≤ 20%\"用的是历史最差（交易序列）回撤，**杠杆、周收益@杠杆和 P(−50%) 都是在同一段走前交易上挑杠杆再评估的（样本内），偏乐观，不是样本外成绩**；样本外周数少时会低估真实尾部；上限取 min(10x, 两腿 maxLeverage/2)。"
          "两腿分开保证金（HIP-3 多为逐仓）→ 单腿相对开仓价走 1/杠杆 就会被强平，即使另一腿等额盈利。这个风险没进回测。",
          "- **容量**：平方根冲击模型 + 假设的冲击名义 $10k（Hyperliquid 未公开 HIP-3 的 impact 名义）+ 较薄腿 24h 成交额 1%，只是量级。",
          "- **bootstrap**：按周有放回抽样，忽略周内回撤和周间相关；周数 < 4 不算。", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- 运行
def run(settings, raw: dict, discovery_source: str, days: int = 60, interval: str = "5m", warmup: int = 5,
        fetch: bool = True, notional: float = 1000.0, out: Path | None = None) -> tuple[str, dict]:
    iv_ms = bl.INTERVAL_MS[interval]
    gate = uv.gate_cfg(settings)
    pairs = discover_pairs(raw, gate)
    errors = [f"{k or '主 dex'}: {v}" for k, v in (raw.get("errors") or {}).items()]
    cache_c: dict[str, tuple] = {}
    cache_f: dict[str, tuple] = {}
    for p in pairs:
        if not p["gate_ok"]:
            continue
        for c in (p["a"], p["b"]):
            if c not in cache_c:
                cache_c[c] = leg_candles(settings, c, interval, days, fetch)
                cache_f[c] = leg_funding(settings, c, days, fetch)
        la, lb = p["legs"][p["a"]], p["legs"][p["b"]]
        hs_a, hs_b = (la["spread_bps"] or 0) / 2, (lb["spread_bps"] or 0) / 2
        levs = [x for x in (la["max_leverage"], lb["max_leverage"]) if x]
        lev_cap = min([LEV_HARD_CAP] + [x / 2 for x in levs])
        vlm = [x for x in (la["day_ntl_vlm"], lb["day_ntl_vlm"]) if x]
        series = align(cache_c[p["a"]][0], cache_c[p["b"]][0], iv_ms)
        if days:
            cut = series[-1][0] - days * DAY_MS if series else 0
            series = [x for x in series if x[0] >= cut]
        p["source"] = "；".join(f"{c}: K 线 {cache_c[c][1]}，资金费 {cache_f[c][1]}" for c in (p["a"], p["b"]))
        p["result"] = evaluate_pair(series, p["kind"], iv_ms, cache_f[p["a"]][0], cache_f[p["b"]][0], hs_a, hs_b,
                                    warmup, notional, lev_cap, min(vlm) if vlm else None)
    res = {"generated_at": datetime.now(UTC).isoformat(), "discovery_source": discovery_source, "interval": interval,
           "days": days, "warmup": warmup, "pairs": pairs, "errors": errors}
    md = report_md(res)
    out = out or Path(settings.data) / "reports" / "pairs_lab.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    slim = json.loads(json.dumps(res, default=str))
    for p in slim["pairs"]:
        p.get("result", {}).pop("trades_taker", None)
    out.with_suffix(".json").write_text(json.dumps(slim, ensure_ascii=False, indent=1))
    return md, res


def load_raw(settings, fixture: str | None, fetch: bool) -> tuple[dict | None, str, bool]:
    """→ (raw, 来源说明, 能否抓 K 线)。"""
    cached = Path(settings.data) / "universe_raw.json"
    if fixture:
        raw = json.loads(Path(fixture).read_text())
        return raw, f"离线文件 {fixture}（抓于 {raw.get('fetched_at')}）", fetch
    if fetch:
        ok, why = bl.hyperliquid_reachable()
        if ok:
            raw = uv.discover()
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(raw, ensure_ascii=False))
            return raw, f"Hyperliquid 实时发现（{raw.get('fetched_at', '')[:16]}）", True
        print(f"[pairs_lab] Hyperliquid 不可达（{why}）。", file=sys.stderr)
        print(RUN_ON_HOST_HINT, file=sys.stderr)
    if cached.exists():
        raw = json.loads(cached.read_text())
        return raw, f"本地缓存 {cached}（抓于 {raw.get('fetched_at')}）", False
    return None, "", False


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--interval", choices=("1m", "5m", "15m"), default="5m",
                    help="Hyperliquid 每个周期只保留最近 5000 根：1m ≈ 3.5 天、5m ≈ 17 天、15m ≈ 52 天；更早靠缓存累积")
    ap.add_argument("--warmup", type=int, default=5, help="前多少个 UTC 日只做训练")
    ap.add_argument("--notional", type=float, default=1000.0, help="每腿名义 $")
    ap.add_argument("--offline", action="store_true", help="不连网络，只用 data/ 缓存")
    ap.add_argument("--fixture", type=str, default=None, help="用保存的 universe_raw.json 做发现")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    s = config.load()
    t = time.time()
    raw, src, fetch = load_raw(s, a.fixture, not a.offline)
    if raw is None:
        print("[pairs_lab] 没有发现数据（data/universe_raw.json 不存在），到服务器上跑。", file=sys.stderr)
        return 1
    md, _ = run(s, raw, src, a.days, a.interval, a.warmup, fetch, a.notional, a.out)
    print(md)
    print(f"\n[pairs_lab] 写入 {a.out or Path(s.data) / 'reports' / 'pairs_lab.md'}（{time.time() - t:.0f}s）", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 测试用确定性 fixture（不是真数据）
def fixture_raw() -> dict:
    """仿 Hyperliquid 结构：xyz / io / mkts 三个 dex。SNDK、NBIS 同底层；SP500≈US500、XYZ100≈USTECH 代理对；
    io:EWY 太薄（门槛不过）；xyz:AAPL 只有一个 dex。"""
    def u(name, lev=10):
        return {"name": name, "szDecimals": 2, "maxLeverage": lev}

    def c(px, oi, vlm, spread_bps=4.0, funding=0.00001, mark_off_bps=0.0):
        half = px * spread_bps / 2e4
        return {"funding": str(funding), "openInterest": str(oi), "dayNtlVlm": str(vlm), "oraclePx": str(px),
                "markPx": str(px * (1 + mark_off_bps / 1e4)), "midPx": str(px), "impactPxs": [str(px - half), str(px + half)]}
    return {"fetched_at": "2026-10-15T12:00:00+00:00",
            "perp_dexs": [None, {"name": "xyz"}, {"name": "io"}, {"name": "mkts"}],
            "dexs": {
                "xyz": [{"universe": [u("xyz:SNDK"), u("xyz:NBIS"), u("xyz:EWY"), u("xyz:SP500", 20), u("xyz:XYZ100", 20), u("xyz:AAPL")]},
                        [c(120, 20000, 3e6), c(90, 20000, 2e6), c(60, 20000, 1e6), c(6500, 400, 5e6), c(25000, 100, 5e6), c(230, 8000, 3e6)]],
                "io": [{"universe": [u("io:SNDK"), u("io:NBIS"), u("io:EWY")]},
                       [c(120.1, 10000, 1e6, 6.0, 0.00003, 1.0), c(90, 10000, 8e5), c(60, 100, 5000)]],
                "mkts": [{"universe": [u("mkts:US500", 20), u("mkts:USTECH", 20)]},
                         [c(6510, 300, 2e6), c(25050, 80, 2e6)]]},
            "main": [{"universe": [u("BTC", 50)]}, [c(65000, 1000, 1e9)]], "errors": {}}


if __name__ == "__main__":
    raise SystemExit(main())
