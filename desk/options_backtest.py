"""11b：用 Deribit DVOL（30 天恒定期限隐含波动指数）做 2021–2026 的事件波动率回测。

问题和线上 options.py 一样：宏观事件（FOMC / CPI / NFP）和周末之前，期权隐含的波动是否系统性高于随后实际走出来的波动？
线上用真实期权链（只从 2026-10 起有数据）；这里用 DVOL 当隐含波动的近似，把样本拉长到 5 年。

每个事件：
  隐含 IV   ：事件前 1 小时（T−1h，与线上 event_lead_min 一致）的 DVOL / 100
  实际 RV   ：从 T−1h 起 24 小时的小时收盘价，平方对数收益年化
  跨式盈亏  ：按 IV 用 Black-Scholes 给 24 小时平值跨式定价，窗口结束按内在价值结算；
              名义 `[options.paper] notional_usd` 标的等值，费用与纸面交易员同一套（Deribit 吃单费封顶 + 滑点）
  入场过滤  ：IV / 过去 7 天实际波动 > 门槛才卖；< 1/门槛才买。门槛扫描 1.0 / 1.1 / 1.2 / 1.3 / 1.5
按事件类型 × 年份汇总：几何平均 实际/隐含、卖方胜率、净盈亏、最差一笔、去掉最好 3 笔后的盈亏。

局限（README 里有详细说明）：DVOL 是 30 天恒定期限、不是 24 小时的；没有微笑；小时 K 线算实际波动偏低。
本机连不上 deribit.com 时：有缓存就用缓存，否则提示到 bot 服务器上跑；`--synthetic` 只用于测试，输出会标明"合成数据"。

用法：python3 -m desk.options_backtest --currency BTC --since 2021-01-01 [--out data/reports/options_backtest_BTC.md]
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
API = "https://www.deribit.com/api/v2/public"
H_MS = 3_600_000
THRESHOLDS = (1.0, 1.1, 1.2, 1.3, 1.5)


class NetworkBlocked(RuntimeError):
    """deribit.com 不可达（沙箱代理 403 等）。"""


# ---------------------------------------------------------------- 事件日历（美东时间）
# 来源（2026-10-12 核对）：BLS 各年发布日程 bls.gov/schedule/<year>/home.htm；2025 年 10–12 月按政府停摆后的改期表
# （10 月 CPI 与 10 月非农取消；9 月非农改到 11/20，11 月非农改到 12/16，11 月 CPI 改到 12/18；9 月 CPI 改到 10/24）；
# FOMC：federalreserve.gov/monetarypolicy/fomccalendars.htm（取声明发布日 14:00 ET）。
CPI_DATES = {
    2021: ["01-13", "02-10", "03-10", "04-13", "05-12", "06-10", "07-13", "08-11", "09-14", "10-13", "11-10", "12-10"],
    2022: ["01-12", "02-10", "03-10", "04-12", "05-11", "06-10", "07-13", "08-10", "09-13", "10-13", "11-10", "12-13"],
    2023: ["01-12", "02-14", "03-14", "04-12", "05-10", "06-13", "07-12", "08-10", "09-13", "10-12", "11-14", "12-12"],
    2024: ["01-11", "02-13", "03-12", "04-10", "05-15", "06-12", "07-11", "08-14", "09-11", "10-10", "11-13", "12-11"],
    2025: ["01-15", "02-12", "03-12", "04-10", "05-13", "06-11", "07-15", "08-12", "09-11", "10-24", "12-18"],
    2026: ["01-13", "02-13", "03-11", "04-10", "05-12", "06-10", "07-14", "08-12", "09-11", "10-14", "11-10", "12-10"],
}
NFP_DATES = {
    2021: ["01-08", "02-05", "03-05", "04-02", "05-07", "06-04", "07-02", "08-06", "09-03", "10-08", "11-05", "12-03"],
    2022: ["01-07", "02-04", "03-04", "04-01", "05-06", "06-03", "07-08", "08-05", "09-02", "10-07", "11-04", "12-02"],
    2023: ["01-06", "02-03", "03-10", "04-07", "05-05", "06-02", "07-07", "08-04", "09-01", "10-06", "11-03", "12-08"],
    2024: ["01-05", "02-02", "03-08", "04-05", "05-03", "06-07", "07-05", "08-02", "09-06", "10-04", "11-01", "12-06"],
    2025: ["01-10", "02-07", "03-07", "04-04", "05-02", "06-06", "07-03", "08-01", "09-05", "11-20", "12-16"],
    2026: ["01-09", "02-11", "03-06", "04-03", "05-08", "06-05", "07-02", "08-07", "09-04", "10-02", "11-06", "12-04"],
}
FOMC_DATES = {
    2021: ["01-27", "03-17", "04-28", "06-16", "07-28", "09-22", "11-03", "12-15"],
    2022: ["01-26", "03-16", "05-04", "06-15", "07-27", "09-21", "11-02", "12-14"],
    2023: ["02-01", "03-22", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2024: ["01-31", "03-20", "05-01", "06-12", "07-31", "09-18", "11-07", "12-18"],
    2025: ["01-29", "03-19", "05-07", "06-18", "07-30", "09-17", "10-29", "12-10"],
    2026: ["01-28", "03-18", "04-29", "06-17", "07-29", "09-16", "10-28", "12-09"],
}
EVENT_TIME_ET = {"CPI": dtime(8, 30), "NFP": dtime(8, 30), "FOMC": dtime(14, 0)}


@dataclass(frozen=True)
class Event:
    kind: str          # CPI / NFP / FOMC / WEEKEND
    at: datetime       # 事件时刻（UTC）
    lock: datetime     # 锁定 = at − 1h（UTC）

    @property
    def year(self) -> int:
        return self.at.year


def event_calendar(since: date, until: date, lead_hours: float = 1.0, weekends: bool = True) -> list[Event]:
    out = []
    for kind, table in (("CPI", CPI_DATES), ("NFP", NFP_DATES), ("FOMC", FOMC_DATES)):
        for y, days in table.items():
            for md in days:
                d = date(y, int(md[:2]), int(md[3:]))
                if since <= d <= until:
                    at = datetime.combine(d, EVENT_TIME_ET[kind], ET).astimezone(UTC)
                    out.append(Event(kind, at, at - timedelta(hours=lead_hours)))
    if weekends:
        # 周末："事件"定在周六 00:00 UTC，锁定前 1 小时（周五 23:00 UTC），窗口 24 小时 = 整个周六
        d = since
        while d <= until:
            if d.weekday() == 5:
                at = datetime(d.year, d.month, d.day, 0, 0, tzinfo=UTC)
                out.append(Event("WEEKEND", at, at - timedelta(hours=lead_hours)))
            d += timedelta(days=1)
    return sorted(out, key=lambda e: e.at)


# ---------------------------------------------------------------- 数据：Deribit 公共接口 + parquet 缓存
def cache_dir(settings) -> Path:
    return Path(settings.data) / "options_cache"


def _chunks(start_ms: int, end_ms: int, step_days: int = 30):
    a = start_ms
    while a < end_ms:
        b = min(a + step_days * 86_400_000, end_ms)
        yield a, b
        a = b


def _blocked(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}"
    return "403" in s or "Proxy" in s or "CONNECT" in s or "tunnel" in s.lower()


def fetch_dvol(currency: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """小时 DVOL（收盘值，小数）。"""
    import httpx
    out: dict[int, float] = {}
    try:
        for a, b in _chunks(start_ms, end_ms):
            r = httpx.get(f"{API}/get_volatility_index_data", params={"currency": currency, "start_timestamp": a,
                                                                     "end_timestamp": b, "resolution": 3600}, timeout=60)
            r.raise_for_status()
            for row in r.json()["result"]["data"]:
                out[int(row[0])] = float(row[4]) / 100
            time.sleep(0.2)
    except Exception as e:  # noqa: BLE001
        if _blocked(e):
            raise NetworkBlocked(str(e)) from e
        raise
    return out


def fetch_candles(currency: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """永续小时收盘价（Deribit tradingview 接口）。"""
    import httpx
    out: dict[int, float] = {}
    try:
        for a, b in _chunks(start_ms, end_ms):
            r = httpx.get(f"{API}/get_tradingview_chart_data",
                          params={"instrument_name": f"{currency}-PERPETUAL", "start_timestamp": a, "end_timestamp": b,
                                  "resolution": "60"}, timeout=60)
            r.raise_for_status()
            res = r.json()["result"]
            if res.get("status") == "ok":
                out.update({int(t): float(c) for t, c in zip(res["ticks"], res["close"])})
            time.sleep(0.2)
    except Exception as e:  # noqa: BLE001
        if _blocked(e):
            raise NetworkBlocked(str(e)) from e
        raise
    return out


def save_series(settings, name: str, series: dict[int, float]) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq
    p = cache_dir(settings) / f"{name}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = sorted(series)
    pq.write_table(pa.table({"t": ts, "v": [series[t] for t in ts], "fetched_at": [int(time.time() * 1000)] * len(ts)}), p)
    return p


def load_series(settings, name: str) -> dict[int, float] | None:
    import pyarrow.parquet as pq
    p = cache_dir(settings) / f"{name}.parquet"
    if not p.exists():
        return None
    tb = pq.read_table(p, columns=["t", "v"])
    return dict(zip(tb.column("t").to_pylist(), tb.column("v").to_pylist()))


def get_data(settings, currency: str, since: date, until: date) -> tuple[dict[int, float], dict[int, float], str]:
    """返回 (dvol, candles, 来源说明)。先尝试网络（增量：从缓存末尾往后补），失败就用缓存；都没有 → NetworkBlocked / RuntimeError。"""
    start_ms = int(datetime.combine(since, dtime(0), UTC).timestamp() * 1000) - 8 * 86_400_000   # 多取 8 天算过去 7 天实际波动
    end_ms = int(datetime.combine(until + timedelta(days=2), dtime(0), UTC).timestamp() * 1000)
    end_ms = min(end_ms, int(time.time() * 1000))
    names = {"dvol": f"{currency}_dvol_1h", "px": f"{currency}_perp_1h"}
    cached = {k: load_series(settings, n) or {} for k, n in names.items()}
    try:
        res = {}
        for k, fn in (("dvol", fetch_dvol), ("px", fetch_candles)):
            have = cached[k]
            a = (max(have) + H_MS) if have and min(have) <= start_ms + 2 * 86_400_000 else start_ms
            fresh = fn(currency, a, end_ms) if a < end_ms else {}
            res[k] = {**have, **fresh}
            save_series(settings, names[k], res[k])
        return res["dvol"], res["px"], f"Deribit API（DVOL {len(res['dvol'])} 小时，K 线 {len(res['px'])} 小时；缓存 {cache_dir(settings)}）"
    except NetworkBlocked as e:
        if cached["dvol"] and cached["px"]:
            return cached["dvol"], cached["px"], f"本地缓存 {cache_dir(settings)}（网络不可达：{str(e)[:80]}）"
        raise
    except Exception as e:  # noqa: BLE001
        if cached["dvol"] and cached["px"]:
            return cached["dvol"], cached["px"], f"本地缓存 {cache_dir(settings)}（网络失败：{type(e).__name__} {str(e)[:80]}）"
        raise RuntimeError(f"没有数据：网络失败（{type(e).__name__} {e}），本地也没有缓存") from e


# ---------------------------------------------------------------- 合成数据（只用于测试；输出必须标明）
def synthetic_data(since: date, until: date, seed: int = 7, base_vol: float = 0.55, event_bump: float = 0.10,
                   realized_scale: float = 0.85) -> tuple[dict[int, float], dict[int, float]]:
    """确定性 GBM 小时价 + 人造 DVOL：平时 DVOL = base_vol，事件前 24 小时抬高 event_bump；
    实际波动 = DVOL × realized_scale（默认 0.85：隐含系统性偏贵），事件后 3 小时放大 2 倍。"""
    rng = random.Random(seed)
    evs = event_calendar(since, until, weekends=False)
    ev_ms = sorted(int(e.at.timestamp() * 1000) for e in evs)
    start = int(datetime.combine(since - timedelta(days=8), dtime(0), UTC).timestamp() * 1000)
    end = int(datetime.combine(until + timedelta(days=2), dtime(0), UTC).timestamp() * 1000)
    dvol, px = {}, {}
    p, t = 40000.0, start
    dt = 1 / (365 * 24)
    i = 0
    while t <= end:
        while i < len(ev_ms) and ev_ms[i] < t - 4 * H_MS:
            i += 1
        near_before = i < len(ev_ms) and 0 <= ev_ms[i] - t <= 24 * H_MS
        just_after = i < len(ev_ms) and 0 < t - ev_ms[i] <= 3 * H_MS
        iv = base_vol + (event_bump if near_before else 0.0)
        rv = iv * realized_scale * (2.0 if just_after else 1.0)
        dvol[t] = iv
        px[t] = p
        p *= math.exp(rng.gauss(0, rv * math.sqrt(dt)) - 0.5 * (rv ** 2) * dt)
        t += H_MS
    return dvol, px


# ---------------------------------------------------------------- 每个事件一行
def _at(series: dict[int, float], t_ms: int, back: int = 3) -> float | None:
    k = t_ms // H_MS * H_MS
    for i in range(back):
        v = series.get(k - i * H_MS)
        if v is not None:
            return v
    return None


def realized_vol_hourly(px: dict[int, float], start_ms: int, hours: int) -> float | None:
    k = start_ms // H_MS * H_MS
    pts = [px.get(k + i * H_MS) for i in range(hours + 1)]
    if sum(p is not None for p in pts) < 0.8 * (hours + 1):
        return None
    pts = [p for p in pts if p is not None]
    ss = sum(math.log(b / a) ** 2 for a, b in zip(pts, pts[1:]))
    return math.sqrt(ss / ((len(pts) - 1) / (365 * 24)))


def straddle_pnl(S0: float, S_end: float, iv: float, hours: float, pcfg: dict) -> dict:
    """24 小时平值跨式：按 IV 定价（B-S），结束按内在价值结算；返回卖方(short)/买方(long)净盈亏与费用。"""
    from .options_paper import bs_price, trade_cost_usd
    T = hours / (365 * 24)
    n = pcfg["notional_usd"] / S0
    c, p = bs_price(S0, S0, T, iv, "C"), bs_price(S0, S0, T, iv, "P")
    prem_legs = [c * n, p * n]
    premium = sum(prem_legs)
    pay_legs = [max(S_end - S0, 0.0) * n, max(S0 - S_end, 0.0) * n]
    payout = sum(pay_legs)
    fees = trade_cost_usd(prem_legs, S0, n, pcfg)["total"] + trade_cost_usd(pay_legs, S_end, n, pcfg)["total"]
    return {"premium": premium, "payout": payout, "fees": fees, "short": premium - payout - fees, "long": payout - premium - fees}


def event_rows(events: list[Event], dvol: dict[int, float], px: dict[int, float], pcfg: dict, hours: int = 24,
               trailing_days: int = 7) -> list[dict]:
    rows = []
    for e in events:
        lock_ms = int(e.lock.timestamp() * 1000)
        iv = _at(dvol, lock_ms)
        S0 = _at(px, lock_ms)
        rv = realized_vol_hourly(px, lock_ms, hours)
        S_end = _at(px, lock_ms + hours * H_MS)
        trail = realized_vol_hourly(px, lock_ms - trailing_days * 24 * H_MS, trailing_days * 24)
        if None in (iv, S0, rv, S_end) or iv <= 0 or rv <= 0:
            continue
        st = straddle_pnl(S0, S_end, iv, hours, pcfg)
        rows.append({"kind": e.kind, "year": e.year, "at": e.at.isoformat(), "lock": e.lock.isoformat(),
                     "iv": iv, "rv": rv, "ratio": rv / iv, "log_ratio": math.log(rv / iv),
                     "iv_over_trailing": (iv / trail) if trail else None, "S0": S0, "S_end": S_end,
                     "move_pct": (S_end / S0 - 1) * 100, "vrp": (iv * iv - rv * rv) * hours / (365 * 24), **st})
    return rows


# ---------------------------------------------------------------- 汇总
def aggregate(rows: list[dict], side: str = "short") -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = sorted((r[side] for r in rows), reverse=True)
    out = {"n": n, "geo_ratio": math.exp(statistics.mean(r["log_ratio"] for r in rows)),
           "median_ratio": statistics.median(r["ratio"] for r in rows),
           "hit_rate": sum(1 for x in pnl if x > 0) / n, "net": sum(pnl), "mean": statistics.mean(pnl),
           "worst": min(pnl), "best": max(pnl), "net_ex_best3": sum(pnl[3:]) if n > 3 else 0.0,
           "fees": sum(r["fees"] for r in rows), "premium": sum(r["premium"] for r in rows)}
    return out


def by_kind_year(rows: list[dict], side: str = "short") -> dict[tuple[str, str], dict]:
    out = {}
    kinds = sorted({r["kind"] for r in rows})
    years = sorted({r["year"] for r in rows})
    for k in kinds + ["ALL"]:
        for y in years + ["ALL"]:
            sel = [r for r in rows if (k == "ALL" or r["kind"] == k) and (y == "ALL" or r["year"] == y)]
            if sel:
                out[(k, str(y))] = aggregate(sel, side)
    return out


def threshold_sweep(rows: list[dict], thresholds=THRESHOLDS) -> list[dict]:
    """入场过滤：IV / 过去 7 天实际波动 > thr 才卖跨式；< 1/thr 才买跨式。只用事前可知的量。"""
    out = []
    for thr in thresholds:
        sh = [r for r in rows if r["iv_over_trailing"] is not None and r["iv_over_trailing"] > thr]
        lg = [r for r in rows if r["iv_over_trailing"] is not None and r["iv_over_trailing"] < 1 / thr]
        out.append({"thr": thr, "short": aggregate(sh, "short"), "long": aggregate(lg, "long")})
    return out


# ---------------------------------------------------------------- 报告（中文）
def _f(x, nd=2, sign=False):
    if x is None:
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def _agg_line(a: dict) -> str:
    if not a.get("n"):
        return "| 0 | — | — | — | — | — | — |"
    return (f"| {a['n']} | {a['geo_ratio']:.2f} | {a['hit_rate'] * 100:.0f}% | {a['net']:+.2f} | {a['mean']:+.2f} | "
            f"{a['worst']:+.2f} | {a['net_ex_best3']:+.2f} |")


def report_md(currency: str, since: date, until: date, rows: list[dict], source: str, pcfg: dict, synthetic: bool = False) -> str:
    L = [f"# 期权事件波动率回测（DVOL 近似）：{currency}，{since} → {until}", ""]
    if synthetic:
        L += ["**⚠ 合成数据：下面所有数字都来自人造的价格和 DVOL，只用于验证程序逻辑，没有任何市场含义。**", ""]
    L += [f"生成 {datetime.now(UTC):%Y-%m-%d %H:%M} UTC。数据来源：{source}。",
          f"名义 {pcfg['notional_usd']:.0f} 美元标的等值；费用：每腿 {pcfg['fee_pct_underlying']}% 标的、封顶权利金 {pcfg['fee_cap_pct_premium']}%，"
          f"滑点权利金 {pcfg['slippage_pct']}%，进出各一次。隐含 = 事件前 1 小时 DVOL（30 天恒定期限，近似）；实际 = 之后 24 小时小时收益年化。", ""]
    if not rows:
        L += ["**没有一个事件有完整数据。**"]
        return "\n".join(L)
    kinds = sorted({r["kind"] for r in rows})
    years = sorted({str(r["year"]) for r in rows})
    agg = by_kind_year(rows, "short")
    L += ["## 汇总：卖跨式（赌实际 < 隐含）", "",
          "| 类型 | 年份 | 样本 | 几何均值 实际/隐含 | 卖方胜率 | 净盈亏 $ | 平均 $/笔 | 最差 $ | 去掉最好 3 笔 $ |", "|---|---|---|---|---|---|---|---|---|"]
    for k in kinds + ["ALL"]:
        for y in years + ["ALL"]:
            if (k, y) in agg:
                L.append(f"| {k} | {y} " + _agg_line(agg[(k, y)]))
    aggl = by_kind_year(rows, "long")
    L += ["", "## 汇总：买跨式（赌实际 > 隐含）", "",
          "| 类型 | 年份 | 样本 | 几何均值 实际/隐含 | 买方胜率 | 净盈亏 $ | 平均 $/笔 | 最差 $ | 去掉最好 3 笔 $ |", "|---|---|---|---|---|---|---|---|---|"]
    for k in kinds + ["ALL"]:
        if (k, "ALL") in aggl:
            L.append(f"| {k} | ALL " + _agg_line(aggl[(k, "ALL")]))
    L += ["", "## 入场过滤门槛扫描（IV / 过去 7 天实际波动）", "",
          "| 门槛 | 卖：笔数 | 卖：胜率 | 卖：净 $ | 卖：最差 $ | 买：笔数 | 买：胜率 | 买：净 $ | 买：最差 $ |", "|---|---|---|---|---|---|---|---|---|"]
    for t in threshold_sweep(rows):
        s_, l_ = t["short"], t["long"]
        L.append(f"| {t['thr']} | {s_.get('n', 0)} | {_f(s_['hit_rate'] * 100, 0) + '%' if s_.get('n') else '—'} | "
                 f"{_f(s_.get('net'), 2, True)} | {_f(s_.get('worst'), 2, True)} | {l_.get('n', 0)} | "
                 f"{_f(l_['hit_rate'] * 100, 0) + '%' if l_.get('n') else '—'} | {_f(l_.get('net'), 2, True)} | {_f(l_.get('worst'), 2, True)} |")
    L += ["", "## 逐事件", "", "| 事件 | 时间 (UTC) | 隐含 | 实际 | 实际/隐含 | 24h 涨跌 % | 权利金 $ | 费用 $ | 卖方净 $ | 买方净 $ |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['kind']} | {r['at'][:16].replace('T', ' ')} | {r['iv'] * 100:.0f}% | {r['rv'] * 100:.0f}% | {r['ratio']:.2f} | "
                 f"{r['move_pct']:+.2f} | {r['premium']:.2f} | {r['fees']:.2f} | {r['short']:+.2f} | {r['long']:+.2f} |")
    L += ["", "## 结论（自动生成，看数据说话）", ""] + verdict_lines(rows, agg)
    L += ["", "## 口径与局限", "",
          "- DVOL 是 30 天恒定期限的隐含波动指数，不是 24 小时期权的隐含波动；事件前短期期权的隐含通常比 DVOL 高，所以这里的"
          "\"隐含\"偏低、卖方盈亏偏保守（实际可收的权利金更多），买方盈亏偏乐观。",
          "- 没有波动率微笑：按单一 IV 用 B-S 定平值跨式。",
          "- 实际波动用小时收盘价算，比分钟级偏低（漏掉盘中来回）；线上 options.py 用分钟级指数价。",
          "- 周末样本 = 周六 00:00 UTC 起 24 小时，锁定在周五 23:00 UTC。",
          "- 费用按 Deribit 吃单费（封顶）+ 滑点，内在价值结算当作到期交割；没算 delta 对冲。",
          "- 2025 年 10–12 月 BLS 因停摆改期：10 月 CPI 与 10 月非农取消，其余按改期日。"]
    return "\n".join(L)


def verdict_lines(rows: list[dict], agg: dict) -> list[str]:
    L = []
    a = agg.get(("ALL", "ALL"), {})
    if a.get("n"):
        L.append(f"- 全部 {a['n']} 个样本：几何均值 实际/隐含 = {a['geo_ratio']:.2f}，卖跨式胜率 {a['hit_rate'] * 100:.0f}%，"
                 f"净 {a['net']:+.2f} 美元（去掉最好 3 笔 {a['net_ex_best3']:+.2f}），最差一笔 {a['worst']:+.2f}。")
    for k in ("FOMC", "CPI", "NFP", "WEEKEND"):
        yrs = [(y, v) for (kk, y), v in agg.items() if kk == k and y != "ALL"]
        if not yrs:
            continue
        parts = "，".join(f"{y} 年 {v['geo_ratio']:.2f}（{v['n']} 个，卖方净 {v['net']:+.0f}）" for y, v in sorted(yrs))
        L.append(f"- {k} 按年：{parts}。")
    tot = agg.get(("ALL", "ALL"))
    if tot and tot["n"] >= 20:
        if tot["geo_ratio"] < 0.9 and tot["net_ex_best3"] > 0:
            L.append("- **隐含系统性高于实际，且卖方盈利不靠最好的 3 笔：事件前卖波动的假设在 DVOL 近似下初步成立。**"
                     "但卖方最差一笔要和权利金比：尾部一次能吃掉多少次收益，见上表。")
        elif tot["geo_ratio"] > 1.1:
            L.append("- **实际系统性高于隐含：事件前买波动的假设在 DVOL 近似下初步成立。**")
        else:
            L.append("- 实际与隐含大体相当，或盈利全靠少数几笔：没有可交易的边际。")
    else:
        L.append("- 样本不足 20 个，不下结论。")
    return L


# ---------------------------------------------------------------- 入口
def run(settings, currency: str = "BTC", since: date = date(2021, 1, 1), until: date | None = None,
        synthetic: bool = False, out: Path | None = None, weekends: bool = True) -> tuple[str, list[dict]]:
    until = until or (datetime.now(UTC).date() - timedelta(days=2))
    pcfg = settings["options"]["paper"]
    if synthetic:
        dvol, px = synthetic_data(since, until)
        source = "合成数据（--synthetic，仅测试）"
    else:
        dvol, px, source = get_data(settings, currency, since, until)
    events = event_calendar(since, until, settings["options"]["event_lead_min"] / 60, weekends=weekends)
    rows = event_rows(events, dvol, px, pcfg, settings["options"]["window_hours"])
    md = report_md(currency, since, until, rows, source, pcfg, synthetic=synthetic)
    out = out or Path(settings.data) / "reports" / f"options_backtest_{currency}{'_SYNTHETIC' if synthetic else ''}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    return md, rows


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--currency", default="BTC")
    ap.add_argument("--since", default="2021-01-01")
    ap.add_argument("--until", default=None)
    ap.add_argument("--no-weekends", action="store_true")
    ap.add_argument("--synthetic", action="store_true", help="只用于测试程序逻辑；输出会标明合成数据")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    s = config.load()
    since = date.fromisoformat(a.since)
    until = date.fromisoformat(a.until) if a.until else None
    try:
        md, _ = run(s, a.currency.upper(), since, until, synthetic=a.synthetic, out=a.out, weekends=not a.no_weekends)
    except NetworkBlocked as e:
        print(f"本机连不上 deribit.com（{str(e)[:100]}），本地也没有缓存。\n"
              f"请到 bot 服务器上跑：python3 -m desk.options_backtest --currency {a.currency} --since {a.since}\n"
              f"数据会存到 {cache_dir(s)}/，之后离线也能重算。不要用 --synthetic 的结果当真。")
        return 2
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
