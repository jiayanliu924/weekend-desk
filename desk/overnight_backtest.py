"""假设 #23：隔夜（美股每个交易日 16:00 收盘 → 次日 09:30 开盘）是否也有周末那种"链上漂移→重开回归"的边际。

周末逻辑在 desk/clock.py + desk/backfill.py，这里完全不碰它，只是把同一套"收盘价 / 决策价 / 重开价"
的度量搬到每个美国交易日的隔夜窗口上，用回补 K 线做粗略回测（备忘 7.2：事后回补只能用于探索，不计入正式成绩）。

窗口定义（美东时间，自动处理夏令时）：
- 收盘价   ：交易日 D 16:00（感恩节次日、平安夜 13:00 提前收盘）之前最后一根 K 线的收盘价
- 决策价   ：下一交易日 D' 09:30 之前 `decision_lead_min`（默认 15 分钟 → 09:15）最后一根 K 线的收盘价
- 重开价   ：D' 09:30 + `exit_delay_min`（默认 5 分钟）之后第一根 K 线的收盘价（15 分钟 K 线 → 09:45 价）
- 偏离 dev ：决策价 / 收盘价 − 1（bps）；实现变动 y：重开价 / 收盘价 − 1（bps）
- 反向交易 ：|dev| > 门槛时按 −sign(dev) 开仓，持到重开；净收益 = −sign(dev)·(重开价/决策价 − 1) − 全额手续费

只有 D' 是 D 的下一个自然日的窗口才叫"隔夜"；周五→周一是"周末"，跨假日是"假日"，分开统计。
周末窗口另按 config.toml 里每个合约真实的关门/重开时间（clock.weekend_for_friday）再算一遍，和线上策略同口径。

另外多算一个 04:00 ET 的决策价：09:15 已经在盘前交易时段里（预言机可能已吃进盘前真实信息），
04:00 才是"底层真的关着"的最后时刻，两者对比能看出偏离到底是噪音还是盘前信息。

用法：python3 -m desk.overnight_backtest [--days 400] [--notional 2000] [--out data/reports/overnight_backtest.md]
"""
from __future__ import annotations

import argparse
import math
import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# 只跑美股/美指合约（BRENTOIL 23/5、SMSN 韩股时段不同，不属于这个假设）
INSTRUMENTS = ("NVDA", "TSLA", "INTC", "XYZ100", "SP500")
# Hyperliquid candleSnapshot 支持 1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M；每次最多返回 5000 根。
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}
INTERVAL_PREF = ("15m", "5m", "1h")

# NYSE 全天休市（2025 + 2026，手写）
NYSE_HOLIDAYS = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18), date(2025, 5, 26),
    date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
}
# 13:00 提前收盘
NYSE_EARLY_CLOSE = {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24), date(2026, 11, 27), date(2026, 12, 24)}
US_OPEN = dtime(9, 30)
PREMARKET_FREE = dtime(4, 0)   # 盘前交易 04:00 ET 才开始；此前底层完全没有成交


# ---------------------------------------------------------------- 日历
def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def session_close(d: date) -> dtime:
    return dtime(13, 0) if d in NYSE_EARLY_CLOSE else dtime(16, 0)


@dataclass(frozen=True)
class Window:
    day: date              # 收盘所在交易日 D
    next_day: date         # 重开所在交易日 D'
    kind: str              # overnight / weekend / holiday
    close: datetime        # 底层收盘（UTC）
    early_decision: datetime  # 04:00 ET（UTC）——盘前开始前的最后时刻
    decision: datetime     # 09:30 − lead（UTC）
    reopen: datetime       # 09:30（UTC）
    exit: datetime         # 重开 + exit_delay（UTC）


def overnight_windows(first: date, last: date, lead_min: int = 15, exit_delay_min: int = 5) -> list[Window]:
    """从 first 到 last（含）每个交易日一条窗口；D' 超出 last 的不要。"""
    out = []
    d = first
    while d <= last:
        if is_trading_day(d):
            nd = next_trading_day(d)
            if nd > last:
                break
            gap = (nd - d).days
            kind = "overnight" if gap == 1 else ("weekend" if d.weekday() == 4 and gap == 3 else "holiday")
            close = datetime.combine(d, session_close(d), ET)
            reopen = datetime.combine(nd, US_OPEN, ET)
            early = datetime.combine(nd, PREMARKET_FREE, ET)
            out.append(Window(d, nd, kind, close.astimezone(UTC), early.astimezone(UTC),
                              (reopen - timedelta(minutes=lead_min)).astimezone(UTC), reopen.astimezone(UTC),
                              (reopen + timedelta(minutes=exit_delay_min)).astimezone(UTC)))
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- K 线取价
def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def price_at_or_before(by_t: dict[int, dict], iv: int, t: datetime, max_back: int = 4) -> float | None:
    """t 时刻（含）之前最后一根已收盘 K 线的收盘价。K 线 [k, k+iv) 的收盘价代表 k+iv 时刻的价格。

    链上没成交的区间 Hyperliquid 可能不返回 K 线，所以最多往前找 max_back 根。
    """
    k = (_ms(t) // iv) * iv - iv
    for i in range(max_back):
        c = by_t.get(k - i * iv)
        if c:
            return float(c["c"])
    return None


def price_at_or_after(by_t: dict[int, dict], iv: int, t: datetime, max_fwd: int = 4) -> float | None:
    """收盘时刻 ≥ t 的第一根 K 线的收盘价（重开后第一个可成交价的近似）。"""
    k = math.ceil(_ms(t) / iv) * iv - iv
    for i in range(max_fwd):
        c = by_t.get(k + i * iv)
        if c:
            return float(c["c"])
    return None


def window_rows(candles: list[dict], windows: list[Window], interval: str, name: str) -> list[dict]:
    iv = INTERVAL_MS[interval]
    by_t = {int(c["t"]): c for c in candles}
    rows = []
    for w in windows:
        p_close = price_at_or_before(by_t, iv, w.close)
        p_early = price_at_or_before(by_t, iv, w.early_decision)
        p_dec = price_at_or_before(by_t, iv, w.decision)
        p_out = price_at_or_after(by_t, iv, w.exit)
        if not all((p_close, p_early, p_dec, p_out)):
            continue
        rows.append({
            "name": name, "kind": w.kind, "day": w.day.isoformat(), "next_day": w.next_day.isoformat(),
            "t_decision": w.decision.isoformat(), "close_px": p_close, "early_px": p_early, "dec_px": p_dec, "reopen_px": p_out,
            "dev_bps": (p_dec / p_close - 1) * 1e4,
            "dev_early_bps": (p_early / p_close - 1) * 1e4,
            "y_bps": (p_out / p_close - 1) * 1e4,
            "move_from_dec_bps": (p_out / p_dec - 1) * 1e4,
            "move_from_early_bps": (p_out / p_early - 1) * 1e4,
        })
    return rows


def weekend_rows_from_config(settings, candles: list[dict], interval: str, inst: dict, first: date, last: date) -> list[dict]:
    """周末窗口按线上策略的口径（config 里每个合约的关门/重开时间）再算一遍，便于直接对比。"""
    from .clock import weekend_for_friday
    iv = INTERVAL_MS[interval]
    by_t = {int(c["t"]): c for c in candles}
    rows = []
    d = first
    while d <= last:
        if d.weekday() == 4:
            w = weekend_for_friday(d, settings, inst)
            p_close = price_at_or_before(by_t, iv, w.close)
            p_dec = price_at_or_before(by_t, iv, w.decision)
            p_out = price_at_or_after(by_t, iv, w.exit)
            if all((p_close, p_dec, p_out)) and w.exit <= datetime.now(UTC):
                rows.append({"name": inst["name"], "kind": "weekend(config)", "day": d.isoformat(),
                             "next_day": w.resume.astimezone(ET).date().isoformat(), "t_decision": w.decision.isoformat(),
                             "close_px": p_close, "dec_px": p_dec, "reopen_px": p_out,
                             "dev_bps": (p_dec / p_close - 1) * 1e4, "y_bps": (p_out / p_close - 1) * 1e4,
                             "move_from_dec_bps": (p_out / p_dec - 1) * 1e4})
        d += timedelta(days=1)
    return rows


# ---------------------------------------------------------------- 反向交易统计
def fade_stats(rows: list[dict], cfg, notional_usd: float = 2000.0, threshold_bps: float | None = None,
               dev_key: str = "dev_bps", move_key: str = "move_from_dec_bps") -> dict:
    """|dev| > 门槛就反向，持到重开。手续费按全额（挂单进 + 吃单出），门槛 = 手续费 + edge_buffer。"""
    c = cfg["costs"]
    fee = c["maker_bps"] + c["taker_bps"]
    thr = threshold_bps if threshold_bps is not None else fee + c["edge_buffer_bps"]
    n = len(rows)
    out = {"n": n, "fee_bps": fee, "threshold_bps": thr, "trades": 0}
    if not n:
        return out
    absdev = sorted(abs(r[dev_key]) for r in rows)
    q = lambda p: absdev[min(n - 1, int(p * n))]  # noqa: E731
    out.update(p50=q(0.5), p75=q(0.75), p90=q(0.9), frac_over=sum(1 for a in absdev if a > thr) / n)
    out["revert_rate_all"] = sum(1 for r in rows if r[dev_key] * r[move_key] < 0) / n
    # 全样本回归系数 beta = y / dev（0 = 偏离全抹掉，1 = 偏离全被确认），最小二乘无截距
    sxx = sum(r[dev_key] ** 2 for r in rows)
    out["beta_y_on_dev"] = (sum(r[dev_key] * r["y_bps"] for r in rows) / sxx) if sxx else float("nan")
    trades = sorted((r for r in rows if abs(r[dev_key]) > thr), key=lambda r: r["t_decision"])
    pnl = []
    for r in trades:
        side = -1 if r[dev_key] > 0 else 1
        gross = side * r[move_key]
        pnl.append({"day": r["day"], "name": r["name"], "dev": r[dev_key], "side": side, "gross_bps": gross,
                    "net_bps": gross - fee, "net_usd": (gross - fee) / 1e4 * notional_usd})
    out["trades"] = len(pnl)
    if not pnl:
        return out
    out["revert_rate"] = sum(1 for p in pnl if p["gross_bps"] > 0) / len(pnl)
    out["win_rate"] = sum(1 for p in pnl if p["net_bps"] > 0) / len(pnl)
    out["avg_net_bps"] = statistics.mean(p["net_bps"] for p in pnl)
    out["median_net_bps"] = statistics.median(p["net_bps"] for p in pnl)
    out["total_net_bps"] = sum(p["net_bps"] for p in pnl)
    out["total_net_usd"] = sum(p["net_usd"] for p in pnl)
    worst = min(pnl, key=lambda p: p["net_bps"])
    out["worst"] = worst
    eq = peak = dd = 0.0
    for p in pnl:
        eq += p["net_usd"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    out["max_drawdown_usd"] = dd
    out["pnl"] = pnl
    return out


# ---------------------------------------------------------------- 数据
def fetch_candles_paged(coin: str, interval: str, days: int) -> list[dict]:
    """沿用 backfill.fetch_candles 的接口，但 candleSnapshot 一次最多 5000 根，往前翻页直到够 days 天。"""
    from . import backfill
    import httpx
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    out: dict[int, dict] = {}
    cur_end = end
    for _ in range(60):
        r = httpx.post(backfill.INFO_URL, json={"type": "candleSnapshot",
                                                "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": cur_end}},
                       timeout=60)
        r.raise_for_status()
        cs = r.json()
        if not cs:
            break
        new = [c for c in cs if int(c["t"]) not in out]
        for c in cs:
            out[int(c["t"])] = c
        if not new or int(cs[0]["t"]) <= start:
            break
        cur_end = int(cs[0]["t"]) - 1
    return [out[t] for t in sorted(out)]


def cache_path(settings, coin: str, interval: str) -> Path:
    return Path(settings.data) / "backfill" / f"candles_{coin.replace(':', '_')}_{interval}.parquet"


def save_cache(settings, coin: str, interval: str, candles: list[dict]) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq
    p = cache_path(settings, coin, interval)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "t": [int(c["t"]) for c in candles], "o": [float(c["o"]) for c in candles], "h": [float(c["h"]) for c in candles],
        "l": [float(c["l"]) for c in candles], "c": [float(c["c"]) for c in candles], "v": [float(c["v"]) for c in candles],
        "fetched_at": [int(time.time() * 1000)] * len(candles)}), p)
    return p


def load_cache(settings, coin: str) -> tuple[list[dict], str] | None:
    """按偏好顺序找本地缓存（含周末 backfill 存的 1h 文件）。"""
    import pyarrow.parquet as pq
    for iv in INTERVAL_PREF:
        p = cache_path(settings, coin, iv)
        if p.exists():
            tb = pq.read_table(p).to_pylist()
            return [{"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]} for r in tb], iv
    return None


def get_candles(settings, coin: str, days: int) -> tuple[list[dict], str, str]:
    """返回 (candles, interval, 来源说明)。网络失败 → 本地缓存；都没有 → 抛异常。"""
    errs = []
    for iv in INTERVAL_PREF:
        try:
            cs = fetch_candles_paged(coin, iv, days)
            if cs:
                save_cache(settings, coin, iv, cs)
                return cs, iv, f"Hyperliquid API（{iv} K 线，{len(cs)} 根）"
            errs.append(f"{iv}: 空")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{iv}: {type(e).__name__} {e}")
            if "403" in str(e) or "Proxy" in type(e).__name__:
                break  # 整个主机被拦，换周期也没用
    cached = load_cache(settings, coin)
    if cached:
        cs, iv = cached
        return cs, iv, f"本地缓存 {cache_path(settings, coin, iv).name}（{len(cs)} 根；网络失败：{'; '.join(errs)}）"
    raise RuntimeError(f"没有 K 线：网络失败（{'; '.join(errs)}），本地也没有缓存")


# ---------------------------------------------------------------- 报告
def _fmt_stats(st: dict) -> list[str]:
    if not st.get("n"):
        return ["（无样本）"]
    L = [f"- 窗口数 {st['n']}；|dev| 中位 {st['p50']:.1f} / 75 分位 {st['p75']:.1f} / 90 分位 {st['p90']:.1f} bps",
         f"- |dev| > 门槛 {st['threshold_bps']:.0f} bps 的窗口：{st['frac_over'] * 100:.1f}%（{st['trades']} 个）",
         f"- 全样本：重开价向收盘价回走的比例 {st['revert_rate_all'] * 100:.0f}%；回归系数 β(y~dev) = {st['beta_y_on_dev']:.2f}"
         "（0 = 偏离全被抹掉，1 = 偏离全被确认）"]
    if st["trades"]:
        w = st["worst"]
        L += [f"- 反向出手 {st['trades']} 笔：回归率 {st['revert_rate'] * 100:.0f}%，扣全额手续费 {st['fee_bps']:.0f} bps 后胜率 "
              f"{st['win_rate'] * 100:.0f}%，平均 {st['avg_net_bps']:+.1f} bps/笔（中位 {st['median_net_bps']:+.1f}）",
              f"- 累计净收益 {st['total_net_bps']:+.0f} bps = {st['total_net_usd']:+.2f} USD（每笔 $2000 名义）；"
              f"最大回撤 {st['max_drawdown_usd']:.2f} USD",
              f"- 最差一笔：{w['name']} {w['day']} dev {w['dev']:+.0f} bps → 净 {w['net_bps']:+.0f} bps（{w['net_usd']:+.2f} USD）"]
    return L


def _sweep_table(rows: list[dict], cfg, notional: float, dev_key="dev_bps", move_key="move_from_dec_bps") -> list[str]:
    L = ["| 门槛 bps | 笔数 | 回归率 | 胜率 | 平均净 bps | 累计净 USD | 最大回撤 USD |", "|---|---|---|---|---|---|---|"]
    for thr in (14, 25, 50, 100, 200):
        st = fade_stats(rows, cfg, notional, thr, dev_key, move_key)
        if st["trades"]:
            L.append(f"| {thr} | {st['trades']} | {st['revert_rate'] * 100:.0f}% | {st['win_rate'] * 100:.0f}% | "
                     f"{st['avg_net_bps']:+.1f} | {st['total_net_usd']:+.2f} | {st['max_drawdown_usd']:.2f} |")
        else:
            L.append(f"| {thr} | 0 | — | — | — | — | — |")
    return L


def report_md(settings, per_inst: dict[str, dict], notional: float) -> str:
    cfg = settings
    all_on = [r for v in per_inst.values() for r in v["rows"] if r["kind"] == "overnight"]
    all_wk = [r for v in per_inst.values() for r in v["rows"] if r["kind"] == "weekend"]
    all_ho = [r for v in per_inst.values() for r in v["rows"] if r["kind"] == "holiday"]
    all_wc = [r for v in per_inst.values() for r in v["weekend_cfg"]]
    L = ["# 假设 #23：隔夜偏离回归 vs 周末（回补 K 线粗略回测，不计入正式成绩）", "",
         f"生成时间 {datetime.now(UTC):%Y-%m-%d %H:%M} UTC。手续费口径：挂单 {cfg['costs']['maker_bps']:.0f} + 吃单 "
         f"{cfg['costs']['taker_bps']:.0f} bps 全额，门槛再加 edge_buffer {cfg['costs']['edge_buffer_bps']:.0f} bps；"
         f"每笔名义 ${notional:.0f}。", "",
         "## 数据来源", ""]
    for name, v in per_inst.items():
        L.append(f"- {name}：{v['source']}" + (f"，覆盖 {v['first']} → {v['last']}" if v.get("first") else ""))
    if per_inst and all(v["source"].startswith("失败") for v in per_inst.values()):
        L += ["", "**本机既连不上 Hyperliquid，也没有本地缓存，下面全是空表。** 到能访问 api.hyperliquid.xyz 的机器"
              "（bot 服务器）上跑 `python3 -m desk.overnight_backtest`，K 线会存到 data/backfill/，之后离线也能重算。"]
    L += ["", "## 汇总（五个合约合并）", "",
          "### 隔夜（交易日 16:00 ET 收盘 → 次日 09:15 ET 决策 → 09:30 重开）", *_fmt_stats(fade_stats(all_on, cfg, notional)), "",
          "门槛扫描（隔夜，09:15 决策）：", *_sweep_table(all_on, cfg, notional), "",
          "### 隔夜，但决策提前到 04:00 ET（盘前开始前，底层真的关着）", "",
          *_fmt_stats(fade_stats(all_on, cfg, notional, dev_key="dev_early_bps", move_key="move_from_early_bps")), "",
          "门槛扫描（隔夜，04:00 决策）：", *_sweep_table(all_on, cfg, notional, "dev_early_bps", "move_from_early_bps"), "",
          "### 周末，同口径（周五 16:00 ET 收盘 → 周一 09:15 决策 → 09:30 重开）", *_fmt_stats(fade_stats(all_wk, cfg, notional)), "",
          "### 周末，线上策略口径（config.toml 每个合约的关门/重开时间，决策 = 重开前 15 分钟）",
          *_fmt_stats(fade_stats(all_wc, cfg, notional)), "",
          "门槛扫描（周末，线上口径）：", *_sweep_table(all_wc, cfg, notional), "",
          "### 跨假日", *_fmt_stats(fade_stats(all_ho, cfg, notional)), "",
          "## 分合约", "",
          "| 合约 | 窗口 | 数量 | \\|dev\\| p50 | p75 | p90 | >门槛 | 笔数 | 回归率 | 胜率 | 平均净 bps | 累计净 USD | 最大回撤 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, v in per_inst.items():
        for label, rows in (("隔夜 09:15", [r for r in v["rows"] if r["kind"] == "overnight"]),
                            ("周末(同口径)", [r for r in v["rows"] if r["kind"] == "weekend"]),
                            ("周末(线上)", v["weekend_cfg"])):
            st = fade_stats(rows, cfg, notional)
            if not st["n"]:
                L.append(f"| {name} | {label} | 0 | — | — | — | — | — | — | — | — | — | — |")
                continue
            t = st["trades"]
            L.append(f"| {name} | {label} | {st['n']} | {st['p50']:.0f} | {st['p75']:.0f} | {st['p90']:.0f} | "
                     f"{st['frac_over'] * 100:.0f}% | {t} | "
                     + (f"{st['revert_rate'] * 100:.0f}% | {st['win_rate'] * 100:.0f}% | {st['avg_net_bps']:+.1f} | "
                        f"{st['total_net_usd']:+.2f} | {st['max_drawdown_usd']:.2f} |" if t else "— | — | — | — | — |"))
    L += ["", "## 结论", ""]
    L += verdict_lines(fade_stats(all_on, cfg, notional), fade_stats(all_wc, cfg, notional),
                       fade_stats(all_on, cfg, notional, dev_key="dev_early_bps", move_key="move_from_early_bps"))
    L += ["", "## 口径说明", "",
          "- 09:15 ET 已经在美股盘前时段（04:00 起）里，trade.xyz 的预言机可能已经在跟盘前成交价，所以 09:15 的偏离里混着真实信息；"
          "04:00 的决策价才对应\"底层完全关着\"。两张表一起看。",
          "- K 线收盘价代表区间末时刻的价格；缺 K 线时最多向前/向后找 4 根。",
          "- 周末/假日窗口和隔夜窗口分开统计；\"周末(线上)\"用 clock.weekend_for_friday 的关门/重开时间，和模拟盘同口径。",
          "- 回补 K 线没有盘口，手续费按挂单进/吃单出全额计，不含滑点和资金费率。"]
    return "\n".join(L)


def verdict_lines(on: dict, wk: dict, early: dict) -> list[str]:
    if not on.get("n"):
        return ["没有隔夜样本，无法判断。"]
    L = []
    thr = on["threshold_bps"]
    L.append(f"- 隔夜 |dev| 中位 {on['p50']:.0f} bps，{on['frac_over'] * 100:.0f}% 的窗口超过全额成本门槛 {thr:.0f} bps"
             + (f"；周末（线上口径）中位 {wk['p50']:.0f} bps，{wk['frac_over'] * 100:.0f}% 超门槛。" if wk.get("n") else "。"))
    if on["trades"]:
        good = on["avg_net_bps"] > 0 and on["win_rate"] > 0.5
        L.append(f"- 隔夜反向 {on['trades']} 笔，扣全额手续费后平均 {on['avg_net_bps']:+.1f} bps/笔、胜率 {on['win_rate'] * 100:.0f}%：" +
                 ("**偏离幅度够覆盖全额成本，假设 #23 初步成立**（仍需用实时盘口数据复核滑点）。" if good else
                  "**偏离不够覆盖全额往返成本，假设 #23 不成立**（隔夜漂移大多是信息而不是噪音，或者幅度太小）。"))
    else:
        L.append("- 隔夜没有一个窗口的偏离超过全额成本门槛：**假设 #23 不成立**。")
    if early.get("trades"):
        L.append(f"- 04:00 决策版本：{early['trades']} 笔，平均 {early['avg_net_bps']:+.1f} bps/笔、胜率 {early['win_rate'] * 100:.0f}%"
                 "（看这行能分清 09:15 的偏离是盘前信息还是真漂移）。")
    if wk.get("trades"):
        L.append(f"- 对照周末（线上口径）：{wk['trades']} 笔，平均 {wk['avg_net_bps']:+.1f} bps/笔、胜率 {wk['win_rate'] * 100:.0f}%。")
    return L


# ---------------------------------------------------------------- 入口
def run(settings, days: int = 400, notional: float = 2000.0, names=INSTRUMENTS, out: Path | None = None) -> str:
    c = settings["clock"]
    per_inst: dict[str, dict] = {}
    for name in names:
        inst = settings.inst(name)
        if not inst:
            continue
        try:
            cs, iv, src = get_candles(settings, inst["coin"], days)
        except Exception as e:  # noqa: BLE001
            per_inst[name] = {"rows": [], "weekend_cfg": [], "source": f"失败 — {e}"}
            continue
        first = datetime.fromtimestamp(int(cs[0]["t"]) / 1000, tz=ET).date()
        last = datetime.fromtimestamp(int(cs[-1]["t"]) / 1000, tz=ET).date()
        wins = overnight_windows(first, last, c["decision_lead_min"], c["exit_delay_min"])
        per_inst[name] = {"rows": window_rows(cs, wins, iv, name),
                          "weekend_cfg": weekend_rows_from_config(settings, cs, iv, inst, first, last),
                          "source": src, "first": first.isoformat(), "last": last.isoformat()}
    md = report_md(settings, per_inst, notional)
    out = out or Path(settings.data) / "reports" / "overnight_backtest.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    return md


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--notional", type=float, default=2000.0)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    s = config.load()
    print(run(s, a.days, a.notional, out=a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
