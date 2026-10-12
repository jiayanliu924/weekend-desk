""""休市窗口台"回测：把所有"参考市场关着、Hyperliquid xyz 永续还在交易"的窗口放到同一张表里比。

周末逻辑在 desk/clock.py，美股隔夜在 desk/overnight_backtest.py（假设 #23）。这里把同一套
"锚价（底层收盘前最后一根 K 线）/ 决策价 / 重开价"的度量推广到五类窗口，用回补 K 线做粗略回测
（备忘 7.2：事后回补只能用于探索，不计入正式成绩）：

1. 美股隔夜 / 周末 / 假日（XYZ100, SP500, NVDA, TSLA, INTC）：16:00 ET 收盘 → 09:15 决策 → 09:30 重开 → 09:35 平仓
2. 韩股隔夜 / 周末 / 假日（SMSN）：KRX 15:30 KST 收盘 → 次日 08:45 决策 → 09:00 重开 → 09:05 平仓；KRX 假日手写
3. ICE Brent 日间休市（BRENTOIL）：18:00 ET 收盘 → 19:45 决策 → 20:00 重开 → 20:05 平仓；另有周五 18:00 → 周日 18:00 周末窗
4. 宏观事件（events.toml 的 CPI/FOMC/NFP + past_fomc）：锚 = 事件前 4 小时，事件前 60 分钟决策，事件时刻"重开"，
   事件后 30 分钟平仓；所有合约；反向(fade)和顺向(follow)都算
5. 资金费率 carry：拿 Hyperliquid fundingHistory（或本地缓存 / hl_ctx）算持仓期间的实际 carry，作为一列调整项

每个窗口 × 合约：偏离 dev = 决策价/锚价 − 1、重开变动 y = 重开价/锚价 − 1、从决策到平仓的变动 move；
反向/顺向净收益 = ±sign(dev)·move − 全额手续费（吃单 9 + 挂单 3 + 半点差估计）；门槛扫描、命中率、最差一笔、样本数。
最后按"每笔净收益"和"$1000 名义下的累计净收益"给窗口排名。

用法：python3 -m desk.windows_backtest --since 2026-04-01 [--notional 1000] [--half-spread 2] [--out data/reports/windows_backtest.md]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import backtest_lib as bl
from .backtest_lib import INTERVAL_MS, price_at_or_after, price_at_or_before, trade_stats
from .overnight_backtest import overnight_windows

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc

US_EQUITY = ("XYZ100", "SP500", "NVDA", "TSLA", "INTC")
KR_EQUITY = ("SMSN",)
ICE_BRENT = ("BRENTOIL",)
ALL_INSTRUMENTS = US_EQUITY + KR_EQUITY + ICE_BRENT

# ---------------------------------------------------------------- 韩国交易所日历（手写，2025 + 2026；改动前先对 krx.co.kr 核一遍）
KRX_HOLIDAYS = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 27), date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),   # 元旦、春节（含临时假日）
    date(2025, 3, 3), date(2025, 5, 1), date(2025, 5, 5), date(2025, 5, 6), date(2025, 6, 3), date(2025, 6, 6),
    date(2025, 8, 15), date(2025, 10, 3), date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8), date(2025, 10, 9),
    date(2025, 12, 25), date(2025, 12, 31),
    # 2026
    date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18), date(2026, 3, 2), date(2026, 5, 1),
    date(2026, 5, 5), date(2026, 5, 25), date(2026, 6, 3), date(2026, 8, 17), date(2026, 9, 24), date(2026, 9, 25),
    date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 9),   # 10-09 韩文日（周五）
    date(2026, 12, 25), date(2026, 12, 31),
}
KRX_CLOSE = dtime(15, 30)
KRX_OPEN = dtime(9, 0)
KR_DECISION_LEAD_MIN = 15
KR_EXIT_DELAY_MIN = 5

# ---------------------------------------------------------------- ICE Brent（美东时间口径）
# ICE Brent 周日 20:00 ET 开盘连续交易到周五 18:00 ET，周一至周四每天 18:00–20:00 ET 休市两小时。
# 周末窗按任务口径：周五 18:00 → 周日 18:00（config.toml 里 BRENTOIL 的 resume 也是周日 18:00；ICE 官方是 20:00，
# 如要改口径只动下面两个常数）。
ICE_BREAK_CLOSE = dtime(18, 0)
ICE_BREAK_OPEN = dtime(20, 0)
ICE_WEEKEND_RESUME = dtime(18, 0)
ICE_DECISION_LEAD_MIN = 15
ICE_EXIT_DELAY_MIN = 5
# ICE 全天不交易的日子（手写、只列最确定的：元旦、耶稣受难日、圣诞）
ICE_CLOSED = {date(2025, 1, 1), date(2025, 4, 18), date(2025, 12, 25), date(2026, 1, 1), date(2026, 4, 3), date(2026, 12, 25)}

# ---------------------------------------------------------------- 宏观事件
EVENT_ANCHOR_LEAD_MIN = 240   # 锚 = 事件前 4 小时（"事件前仓位漂移"的起点）
EVENT_DECISION_LEAD_MIN = 60
EVENT_EXIT_DELAY_MIN = 30

FAMILY_ORDER = ("美股隔夜", "美股周末", "美股假日", "韩股隔夜", "韩股周末", "韩股假日", "Brent 日间休市", "Brent 周末",
                "事件 CPI", "事件 FOMC", "事件 NFP")


@dataclass(frozen=True)
class CWindow:
    family: str            # 见 FAMILY_ORDER
    name: str              # 合约名
    label: str             # 窗口标签（收盘日 / 事件 id）
    close: datetime        # 锚：底层收盘（UTC）
    decision: datetime     # 决策（UTC）
    reopen: datetime       # 重开（UTC）
    exit: datetime         # 平仓（UTC）

    @property
    def hold_hours(self) -> float:
        return (self.exit - self.decision).total_seconds() / 3600


# ---------------------------------------------------------------- 日历
def is_krx_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in KRX_HOLIDAYS


def next_krx_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_krx_trading_day(d):
        d += timedelta(days=1)
    return d


def _gap_kind(d: date, nd: date) -> str:
    gap = (nd - d).days
    return "隔夜" if gap == 1 else ("周末" if d.weekday() == 4 and gap == 3 else "假日")


# ---------------------------------------------------------------- 窗口生成
def us_windows(first: date, last: date, lead_min: int = 15, exit_delay_min: int = 5, names=US_EQUITY) -> list[CWindow]:
    """美股：直接复用 overnight_backtest 的日历（NYSE 假日、提前收盘、夏令时）。"""
    kinds = {"overnight": "美股隔夜", "weekend": "美股周末", "holiday": "美股假日"}
    out = []
    for w in overnight_windows(first, last, lead_min, exit_delay_min):
        for n in names:
            out.append(CWindow(kinds[w.kind], n, w.day.isoformat(), w.close, w.decision, w.reopen, w.exit))
    return out


def kr_windows(first: date, last: date, lead_min: int = KR_DECISION_LEAD_MIN, exit_delay_min: int = KR_EXIT_DELAY_MIN,
               names=KR_EQUITY) -> list[CWindow]:
    """韩股：KRX 交易日 D 15:30 KST 收盘 → 下一个交易日 D' 09:00 KST 开盘。首尔没有夏令时。"""
    out = []
    d = first
    while d <= last:
        if is_krx_trading_day(d):
            nd = next_krx_trading_day(d)
            if nd > last:
                break
            close = datetime.combine(d, KRX_CLOSE, KST).astimezone(UTC)
            reopen = datetime.combine(nd, KRX_OPEN, KST).astimezone(UTC)
            fam = "韩股" + _gap_kind(d, nd)
            for n in names:
                out.append(CWindow(fam, n, d.isoformat(), close, reopen - timedelta(minutes=lead_min), reopen,
                                   reopen + timedelta(minutes=exit_delay_min)))
        d += timedelta(days=1)
    return out


def ice_windows(first: date, last: date, lead_min: int = ICE_DECISION_LEAD_MIN, exit_delay_min: int = ICE_EXIT_DELAY_MIN,
                names=ICE_BRENT) -> list[CWindow]:
    """ICE Brent：周一至周四 18:00 → 20:00 ET 日间休市；周五 18:00 → 周日 18:00 ET 周末。"""
    out = []
    d = first
    while d <= last:
        wd = d.weekday()
        if wd <= 3 and d not in ICE_CLOSED and (d + timedelta(days=1)) not in ICE_CLOSED:
            close = datetime.combine(d, ICE_BREAK_CLOSE, ET)
            reopen = datetime.combine(d, ICE_BREAK_OPEN, ET)
            fam = "Brent 日间休市"
        elif wd == 4 and d not in ICE_CLOSED:
            close = datetime.combine(d, ICE_BREAK_CLOSE, ET)
            reopen = datetime.combine(d + timedelta(days=2), ICE_WEEKEND_RESUME, ET)
            fam = "Brent 周末"
        else:
            d += timedelta(days=1)
            continue
        if reopen.date() > last:
            break
        for n in names:
            out.append(CWindow(fam, n, d.isoformat(), close.astimezone(UTC),
                               (reopen - timedelta(minutes=lead_min)).astimezone(UTC), reopen.astimezone(UTC),
                               (reopen + timedelta(minutes=exit_delay_min)).astimezone(UTC)))
        d += timedelta(days=1)
    return out


def load_event_times(settings) -> list[tuple[str, str, datetime]]:
    """(id, type, 事件时刻 UTC)：events.toml 的 events + past_fomc（后者按 FOMC 处理）。"""
    from .options import load_events
    tz = ZoneInfo(settings["clock"]["timezone"])
    ev = load_events(settings.root)
    out = []
    for e in ev.get("events", []):
        at = datetime.strptime(e["at"], "%Y-%m-%d %H:%M").replace(tzinfo=tz).astimezone(UTC)
        out.append((e["id"], e["type"], at))
    for s in ev.get("past_fomc", []):
        at = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz).astimezone(UTC)
        out.append((f"FOMC-{at.astimezone(tz):%Y-%m-%d}", "FOMC", at))
    seen, uniq = set(), []
    for eid, typ, at in sorted(out, key=lambda x: x[2]):
        if at not in seen:
            seen.add(at)
            uniq.append((eid, typ, at))
    return uniq


def event_windows(settings, first: date, last: date, names=ALL_INSTRUMENTS, now: datetime | None = None) -> list[CWindow]:
    """宏观事件：锚 = 事件 −4h，决策 = 事件 −60min，重开 = 事件，平仓 = 事件 +30min。只要已经过去的。"""
    now = now or datetime.now(UTC)
    out = []
    for eid, typ, at in load_event_times(settings):
        exit_ = at + timedelta(minutes=EVENT_EXIT_DELAY_MIN)
        if exit_ > now or not (first <= at.astimezone(ET).date() <= last):
            continue
        for n in names:
            out.append(CWindow(f"事件 {typ}", n, eid, at - timedelta(minutes=EVENT_ANCHOR_LEAD_MIN),
                               at - timedelta(minutes=EVENT_DECISION_LEAD_MIN), at, exit_))
    return out


def windows_for(settings, name: str, first: date, last: date, lead_min: int, exit_delay_min: int,
                now: datetime | None = None) -> list[CWindow]:
    """某个合约在 [first, last] 范围里的全部窗口。"""
    out: list[CWindow] = []
    if name in US_EQUITY:
        out += us_windows(first, last, lead_min, exit_delay_min, (name,))
    if name in KR_EQUITY:
        out += kr_windows(first, last, names=(name,))
    if name in ICE_BRENT:
        out += ice_windows(first, last, names=(name,))
    out += event_windows(settings, first, last, (name,), now)
    return out


# ---------------------------------------------------------------- 取价成行
def window_rows(candles: list[dict], windows: list[CWindow], interval: str, funding: list[dict] | None = None) -> list[dict]:
    iv = INTERVAL_MS[interval]
    by_t = {int(c["t"]): c for c in candles}
    rows = []
    for w in windows:
        p_close = price_at_or_before(by_t, iv, w.close)
        p_dec = price_at_or_before(by_t, iv, w.decision)
        p_out = price_at_or_after(by_t, iv, w.exit)
        if not all((p_close, p_dec, p_out)):
            continue
        rows.append({
            "family": w.family, "name": w.name, "label": w.label, "day": w.label, "t_decision": w.decision.isoformat(),
            "close_px": p_close, "dec_px": p_dec, "reopen_px": p_out, "hold_hours": w.hold_hours,
            "dev_bps": (p_dec / p_close - 1) * 1e4,
            "y_bps": (p_out / p_close - 1) * 1e4,
            "move_from_dec_bps": (p_out / p_dec - 1) * 1e4,
            "funding_sum": bl.funding_sum(funding or [], w.decision, w.exit),
            # 整个休市期（锚 → 平仓）的费率之和：参考"如果从关门就拿着"的 carry，不进净收益
            "funding_sum_window": bl.funding_sum(funding or [], w.close, w.exit),
        })
    return rows


# ---------------------------------------------------------------- 排名
def rank_table(rows: list[dict], cfg, notional: float, half_spread: float, by: str = "avg_net_bps") -> list[dict]:
    """每个（窗口类型 × 合约 × 方向）一行，按 by 从高到低排。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["family"], r["name"]), []).append(r)
    out = []
    for (fam, name), rs in groups.items():
        for mode in ("fade", "follow"):
            st = trade_stats(rs, cfg, notional, mode=mode, half_spread_bps=half_spread, carry_key="funding_sum")
            if not st["trades"]:
                continue
            out.append({"family": fam, "name": name, "mode": mode, "n": st["n"], "trades": st["trades"],
                        "hit_rate": st["hit_rate"], "win_rate": st["win_rate"], "avg_net_bps": st["avg_net_bps"],
                        "total_net_usd": st["total_net_usd"], "avg_carry_bps": st["avg_carry_bps"],
                        "total_net_carry_usd": st["total_net_carry_usd"], "worst_bps": st["worst"]["net_bps"],
                        "worst_label": st["worst"]["day"], "max_dd_usd": st["max_drawdown_usd"]})
    out.sort(key=lambda x: (x[by], x["total_net_usd"]), reverse=True)
    return out


def _family_key(f: str) -> tuple[int, str]:
    return (FAMILY_ORDER.index(f) if f in FAMILY_ORDER else 99, f)


def family_table(rows: list[dict], cfg, notional: float, half_spread: float) -> list[dict]:
    """每个窗口类型（所有合约合并）× 方向一行。"""
    fams = sorted({r["family"] for r in rows}, key=_family_key)
    out = []
    for fam in fams:
        rs = [r for r in rows if r["family"] == fam]
        fw = [r["funding_sum_window"] for r in rs if r.get("funding_sum_window") is not None]
        window_carry_short = (sum(fw) / len(fw) * 1e4) if fw else None   # 整个休市期空头平均能收的 carry（bps）
        for mode in ("fade", "follow"):
            st = trade_stats(rs, cfg, notional, mode=mode, half_spread_bps=half_spread, carry_key="funding_sum")
            out.append({"family": fam, "mode": mode, "st": st, "window_carry_short_bps": window_carry_short})
    return out


# ---------------------------------------------------------------- 报告
MODE_CN = {"fade": "反向", "follow": "顺向"}


def _f(x, fmt="+.1f"):
    return "—" if x is None else format(x, fmt)


def _sweep(rows: list[dict], cfg, notional: float, half_spread: float, mode: str) -> list[str]:
    L = ["| 门槛 bps | 笔数 | 命中率 | 胜率 | 平均净 bps | 累计净 USD | 含 carry USD | 最大回撤 USD |", "|---|---|---|---|---|---|---|---|"]
    for thr in (14, 25, 50, 100, 200):
        st = trade_stats(rows, cfg, notional, thr, mode=mode, half_spread_bps=half_spread, carry_key="funding_sum")
        if st["trades"]:
            L.append(f"| {thr} | {st['trades']} | {st['hit_rate'] * 100:.0f}% | {st['win_rate'] * 100:.0f}% | "
                     f"{st['avg_net_bps']:+.1f} | {st['total_net_usd']:+.2f} | {st['total_net_carry_usd']:+.2f} | "
                     f"{st['max_drawdown_usd']:.2f} |")
        else:
            L.append(f"| {thr} | 0 | — | — | — | — | — | — |")
    return L


def summary_cn(rows: list[dict], ranked: list[dict], fams: list[dict], cfg, half_spread: float) -> list[str]:
    """中文结论：哪类窗口值得做、哪类不值得。"""
    fee = cfg["costs"]["maker_bps"] + cfg["costs"]["taker_bps"] + half_spread
    thr = fee + cfg["costs"]["edge_buffer_bps"]
    if not rows:
        return ["没有任何窗口样本（没有 K 线）。", "", bl.RUN_ON_HOST_HINT]
    L = [f"- 共 {len(rows)} 个窗口样本，{len({r['family'] for r in rows})} 类窗口，{len({r['name'] for r in rows})} 个合约；"
         f"全额成本 {fee:.0f} bps（吃单 {cfg['costs']['taker_bps']:.0f} + 挂单 {cfg['costs']['maker_bps']:.0f} + 半点差 {half_spread:.0f}），"
         f"出手门槛 {thr:.0f} bps。"]
    good = [x for x in ranked if x["avg_net_bps"] > 0 and x["win_rate"] >= 0.5 and x["trades"] >= 5]
    if good:
        top = good[0]
        L.append(f"- 排名第一：**{top['family']} / {top['name']} / {MODE_CN[top['mode']]}**，{top['trades']} 笔，"
                 f"平均 {top['avg_net_bps']:+.1f} bps/笔，胜率 {top['win_rate'] * 100:.0f}%，"
                 f"累计 {top['total_net_usd']:+.2f} USD（含 carry {top['total_net_carry_usd']:+.2f}）。")
        L.append("- 扣全额成本后平均为正、胜率 ≥ 50%、样本 ≥ 5 笔的组合：" +
                 "；".join(f"{x['family']}/{x['name']}/{MODE_CN[x['mode']]}（{x['avg_net_bps']:+.0f} bps × {x['trades']}）" for x in good[:8]))
    else:
        L.append("- **没有一个（窗口 × 合约 × 方向）在扣全额成本后同时满足：平均为正、胜率 ≥ 50%、样本 ≥ 5 笔。**"
                 "休市窗口的偏离大多不够覆盖往返成本，或者方向不稳定。")
    for x in fams:
        st = x["st"]
        if st["trades"]:
            L.append(f"- {x['family']}（{MODE_CN[x['mode']]}）：{st['n']} 个窗口，|dev| 中位 {st['p50']:.0f} bps，出手 {st['trades']} 笔，"
                     f"平均 {st['avg_net_bps']:+.1f} bps，胜率 {st['win_rate'] * 100:.0f}%，累计 {st['total_net_usd']:+.2f} USD，"
                     f"carry 平均 {st['avg_carry_bps']:+.1f} bps/笔。")
    fade_tot = sum(x["st"].get("total_net_usd", 0) for x in fams if x["mode"] == "fade")
    fol_tot = sum(x["st"].get("total_net_usd", 0) for x in fams if x["mode"] == "follow")
    L.append(f"- 全部窗口合计：反向 {fade_tot:+.2f} USD，顺向 {fol_tot:+.2f} USD（同一偏离两边下注互为镜像，差的是两份手续费）。"
             + ("反向整体占优。" if fade_tot > fol_tot else "顺向整体占优——偏离里信息多于噪音。"))
    ev = [x for x in fams if x["family"].startswith("事件") and x["st"]["trades"]]
    if ev:
        best = max(ev, key=lambda x: x["st"]["avg_net_bps"])
        L.append(f"- 宏观事件窗里最好的是 {best['family']} {MODE_CN[best['mode']]}：平均 {best['st']['avg_net_bps']:+.1f} bps × "
                 f"{best['st']['trades']} 笔；事件窗样本少，只能看方向。")
    return L


def report_md(settings, per_inst: dict[str, dict], notional: float, half_spread: float, since: date) -> str:
    cfg = settings
    rows = [r for v in per_inst.values() for r in v["rows"]]
    ranked = rank_table(rows, cfg, notional, half_spread)
    fams = family_table(rows, cfg, notional, half_spread)
    L = [f"# 休市窗口回测（{since} 起；回补 K 线粗略回测，不计入正式成绩）", "",
         f"生成时间 {datetime.now(UTC):%Y-%m-%d %H:%M} UTC。每笔名义 ${notional:.0f}。手续费全额：吃单 {cfg['costs']['taker_bps']:.0f} + "
         f"挂单 {cfg['costs']['maker_bps']:.0f} + 半点差 {half_spread:.0f} bps；门槛再加 edge_buffer {cfg['costs']['edge_buffer_bps']:.0f} bps。", "",
         "## 数据来源", ""]
    for name, v in per_inst.items():
        L.append(f"- {name}：K 线 {v['source']}" + (f"，覆盖 {v['first']} → {v['last']}" if v.get("first") else "")
                 + f"；资金费率 {v.get('funding_source', '—')}")
    if per_inst and all(v["source"].startswith("失败") for v in per_inst.values()):
        L += ["", "**本机既连不上 Hyperliquid，也没有本地缓存，下面全是空表。**", "", bl.RUN_ON_HOST_HINT]
    L += ["", "## 结论（中文）", "", *summary_cn(rows, ranked, fams, cfg, half_spread), "",
          "## 窗口排名（窗口类型 × 合约 × 方向，按每笔净收益从高到低）", "",
          f"| # | 窗口 | 合约 | 方向 | 窗口数 | 笔数 | 命中率 | 胜率 | 平均净 bps | 累计净 USD(${notional:.0f}) | carry bps/笔 | 含 carry USD | 最差一笔 | 最大回撤 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(ranked, 1):
        L.append(f"| {i} | {x['family']} | {x['name']} | {MODE_CN[x['mode']]} | {x['n']} | {x['trades']} | {x['hit_rate'] * 100:.0f}% | "
                 f"{x['win_rate'] * 100:.0f}% | {x['avg_net_bps']:+.1f} | {x['total_net_usd']:+.2f} | {x['avg_carry_bps']:+.1f} | "
                 f"{x['total_net_carry_usd']:+.2f} | {x['worst_bps']:+.0f} ({x['worst_label']}) | {x['max_dd_usd']:.2f} |")
    if not ranked:
        L.append("| — | （没有一笔超过门槛的交易） | | | | | | | | | | | | |")
    L += ["", "## 按窗口类型汇总（所有合约合并）", "",
          "| 窗口 | 方向 | 窗口数 | \\|dev\\| p50 | p75 | p90 | >门槛 | β(y~dev) | 笔数 | 命中率 | 胜率 | 平均净 bps | 累计净 USD | 含 carry USD | 休市期空头 carry bps |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in fams:
        st = x["st"]
        if not st["n"]:
            continue
        beta = st.get("beta_y_on_dev")
        beta_s = "—" if beta is None or beta != beta else f"{beta:.2f}"
        L.append(f"| {x['family']} | {MODE_CN[x['mode']]} | {st['n']} | {st['p50']:.0f} | {st['p75']:.0f} | {st['p90']:.0f} | "
                 f"{st['frac_over'] * 100:.0f}% | {beta_s} | {st['trades']} | "
                 + (f"{st['hit_rate'] * 100:.0f}% | {st['win_rate'] * 100:.0f}% | {st['avg_net_bps']:+.1f} | {st['total_net_usd']:+.2f} | "
                    f"{st['total_net_carry_usd']:+.2f} |" if st["trades"] else "— | — | — | — | — |")
                 + f" {_f(x['window_carry_short_bps'])} |")
    L += ["", "## 门槛扫描（按窗口类型）", ""]
    for fam in sorted({r["family"] for r in rows}, key=_family_key):
        rs = [r for r in rows if r["family"] == fam]
        for mode in ("fade", "follow"):
            L += [f"### {fam} · {MODE_CN[mode]}", *_sweep(rs, cfg, notional, half_spread, mode), ""]
    L += ["## 口径说明", "",
          "- 锚价 = 底层收盘前最后一根 K 线收盘价；决策价 = 重开前 15 分钟（事件：事件前 60 分钟）最后一根 K 线；"
          "重开价 = 平仓时刻之后第一根 K 线收盘价。缺 K 线时最多向前/向后找 4 根。",
          "- 美股隔夜 09:15 ET 已在盘前时段，预言机可能已吃进盘前信息（见 overnight_backtest 的 04:00 对照）。",
          "- 韩股：KRX 15:30 收盘 → 09:00 开盘，首尔无夏令时；KRX 假日为手写表。",
          "- Brent：ICE 周一至周四 18:00–20:00 ET 休市两小时；周末按周五 18:00 → 周日 18:00 ET（ICE 官方重开 20:00，常数可改）。",
          f"- 事件：锚 = 事件前 {EVENT_ANCHOR_LEAD_MIN // 60} 小时，决策 = 事件前 {EVENT_DECISION_LEAD_MIN} 分钟，"
          f"平仓 = 事件后 {EVENT_EXIT_DELAY_MIN} 分钟；反向 = 赌事件前漂移被抹掉，顺向 = 赌它延续。",
          "- carry = (决策, 平仓] 内结算的每小时资金费率之和，空头收、多头付；没有费率数据时按 0。"
          "决策到平仓只有 20 分钟时往往跨不到整点结算，carry ≈ 0；\"休市期空头 carry\"列给的是从关门一直拿到平仓的费率之和，"
          "用来判断整段休市里哪一边更挤。",
          "- 回补 K 线没有盘口，手续费按吃单 + 挂单 + 半点差估计全额计，不含滑点。"]
    return "\n".join(L)


# ---------------------------------------------------------------- 入口
def run(settings, since: date, notional: float = 1000.0, half_spread: float | None = None, names=ALL_INSTRUMENTS,
        out: Path | None = None, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    c = settings["clock"]
    half_spread = settings["costs"].get("half_spread_bps", 2.0) if half_spread is None else half_spread
    days = max(1, (now.date() - since).days + 2)
    per_inst: dict[str, dict] = {}
    for name in names:
        inst = settings.inst(name)
        if not inst:
            continue
        try:
            cs, iv, src = bl.get_candles(settings, inst["coin"], days)
        except Exception as e:  # noqa: BLE001
            per_inst[name] = {"rows": [], "source": f"失败 — {e}"}
            continue
        funding, fsrc = bl.get_funding(settings, inst["coin"], days)
        first = max(since, datetime.fromtimestamp(int(cs[0]["t"]) / 1000, tz=ET).date())
        last = datetime.fromtimestamp(int(cs[-1]["t"]) / 1000, tz=ET).date()
        wins = [w for w in windows_for(settings, name, first, last, c["decision_lead_min"], c["exit_delay_min"], now)
                if w.exit <= now]
        per_inst[name] = {"rows": window_rows(cs, wins, iv, funding), "source": src, "funding_source": fsrc,
                          "first": first.isoformat(), "last": last.isoformat()}
    md = report_md(settings, per_inst, notional, half_spread, since)
    out = out or Path(settings.data) / "reports" / "windows_backtest.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    return md


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", type=date.fromisoformat, default=date(2026, 4, 1))
    ap.add_argument("--notional", type=float, default=1000.0)
    ap.add_argument("--half-spread", type=float, default=None, help="半点差估计 bps（默认 config costs.half_spread_bps 或 2）")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    s = config.load()
    ok, why = bl.hyperliquid_reachable()
    if not ok:
        print(f"[windows_backtest] Hyperliquid 不可达（{why}）。先试本地缓存 data/backfill/。", file=sys.stderr)
        print(bl.RUN_ON_HOST_HINT, file=sys.stderr)
    print(run(s, a.since, a.notional, a.half_spread, out=a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
