"""回补历史 K 线做描述统计（备忘 7.7 第 3–4 周）。

按备忘 7.2：事后回补的历史只能用于粗略探索，不能用于正式评估，所以存放在 data/backfill，不进原始层。
"""
from __future__ import annotations

import statistics
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from .clock import weekend_for_friday

INFO_URL = "https://api.hyperliquid.xyz/info"


def fetch_candles(coin: str, interval: str = "1h", days: int = 200) -> list[dict]:
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    r = httpx.post(INFO_URL, json={"type": "candleSnapshot",
                                   "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": end}},
                   timeout=60)
    r.raise_for_status()
    return r.json()


def save(settings, coin: str, candles: list[dict]) -> Path:
    d = Path(settings.data) / "backfill"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"candles_{coin.replace(':', '_')}_1h.parquet"
    pq.write_table(pa.table({
        "t": [c["t"] for c in candles], "o": [float(c["o"]) for c in candles], "h": [float(c["h"]) for c in candles],
        "l": [float(c["l"]) for c in candles], "c": [float(c["c"]) for c in candles], "v": [float(c["v"]) for c in candles],
        "fetched_at": [int(time.time() * 1000)] * len(candles)}), p)
    return p


def weekend_table(settings, candles: list[dict], inst: dict | None = None) -> list[dict]:
    """Per weekend: price at external close, price at resume (≈ 链上价 at decision), price 1h after resume.

    Times not on the hour (e.g. 韩股 09:01) are floored to the hour, since these are hourly candles.
    """
    from datetime import datetime, timezone
    by_t = {c["t"]: c for c in candles}
    H = 3600 * 1000
    floor = lambda dt: int(dt.timestamp() * 1000) // H * H  # noqa: E731
    rows = []
    day = datetime.fromtimestamp(candles[0]["t"] / 1000, tz=timezone.utc).date()
    last = datetime.fromtimestamp(candles[-1]["t"] / 1000, tz=timezone.utc).date()
    name = inst["name"] if inst else settings.instruments[0]["name"]
    while day <= last:
        if day.weekday() == 4:
            w = weekend_for_friday(day, settings, inst)
            k_close = floor(w.close) - H        # candle ending at external close
            k_dec = floor(w.resume) - H         # candle ending at resume
            k_out = floor(w.resume)             # candle ending 1h after resume
            if all(k in by_t for k in (k_close, k_dec, k_out)):
                f, d, o = float(by_t[k_close]["c"]), float(by_t[k_dec]["c"]), float(by_t[k_out]["c"])
                rows.append({"weekend": day.isoformat(), "name": name, "dev_bps": (d / f - 1) * 1e4, "y_bps": (o / f - 1) * 1e4,
                             "err_A": abs(o / f - 1) * 1e4, "err_B": abs(o / d - 1) * 1e4})
        day += timedelta(days=1)
    return rows


def baseline_report(settings, rows: list[dict], title: str = "") -> str:
    if not rows:
        return "没有足够的历史 K 线。"
    n = len(rows)
    absdev = sorted(abs(r["dev_bps"]) for r in rows)
    q = lambda p: absdev[min(n - 1, int(p * n))]  # noqa: E731
    lines = [
        f"# {title or '基线'}历史表现（回补 K 线，仅供粗略探索，不计入正式成绩）", "",
        f"样本：{n} 个周末（{rows[0]['weekend']} 至 {rows[-1]['weekend']}），小时 K 线近似。", "",
        f"- 基线 A（周五收盘价不变）重开误差中位数：{statistics.median(r['err_A'] for r in rows):.1f} bps",
        f"- 基线 B（周日链上价）重开误差中位数：{statistics.median(r['err_B'] for r in rows):.1f} bps",
        f"- B 比 A 更准的周末：{sum(1 for r in rows if r['err_B'] < r['err_A'])}/{n}",
        f"- 链上偏离方向与重开方向一致：{sum(1 for r in rows if (r['dev_bps'] > 0) == (r['y_bps'] > 0))}/{n}",
        f"- '普通周末'偏离幅度（绝对值）：中位 {q(0.5):.0f} bps，75 分位 {q(0.75):.0f} bps，90 分位 {q(0.9):.0f} bps", "",
        "| 周末 | 链上偏离 bps | 重开变动 bps | A 误差 | B 误差 |", "|---|---|---|---|---|",
    ]
    lines += [f"| {r['weekend']} | {r['dev_bps']:.0f} | {r['y_bps']:.0f} | {r['err_A']:.0f} | {r['err_B']:.0f} |" for r in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- options (第九章) exploration
DRB = "https://www.deribit.com/api/v2/public"


def _chunks(days: int, chunk_days: int = 30):
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    t = start
    while t < end:
        yield t, min(t + chunk_days * 86400 * 1000, end)
        t += chunk_days * 86400 * 1000


def fetch_dvol(currency: str = "BTC", days: int = 200) -> dict[int, float]:
    out = {}
    for a, b in _chunks(days):
        r = httpx.get(f"{DRB}/get_volatility_index_data", params={"currency": currency, "start_timestamp": a,
                                                                 "end_timestamp": b, "resolution": 3600}, timeout=60)
        for row in r.json()["result"]["data"]:
            out[int(row[0])] = float(row[4]) / 100
    return out


def fetch_perp(currency: str = "BTC", days: int = 200) -> dict[int, float]:
    out = {}
    for a, b in _chunks(days):
        r = httpx.get(f"{DRB}/get_tradingview_chart_data", params={"instrument_name": f"{currency}-PERPETUAL",
                                                                  "start_timestamp": a, "end_timestamp": b,
                                                                  "resolution": "60"}, timeout=60).json()["result"]
        out.update({int(t): float(c) for t, c in zip(r["ticks"], r["close"])})
    return out


def options_history(settings, dvol: dict[int, float], perp: dict[int, float]) -> str:
    """粗略探索：用 DVOL（30 天隐含波动指数）当隐含波动的近似，小时价格算实际波动。不计入正式成绩。"""
    import math
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from .options import load_events
    H = 3600 * 1000
    tz = ZoneInfo(settings["clock"]["timezone"])
    fomc = [int(datetime.strptime(x, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp() * 1000) // H * H
            for x in load_events(settings.root).get("past_fomc", [])]

    def sample(t0):
        if t0 not in dvol:
            return None
        pts = [perp.get(t0 + i * H) for i in range(25)]
        if any(p is None for p in pts):
            return None
        ss = sum(math.log(b / a) ** 2 for a, b in zip(pts, pts[1:]))
        rv = math.sqrt(ss / (1 / 365))
        return rv / dvol[t0]

    ev = [(t, sample(t - H)) for t in fomc]
    ev = [(t, r) for t, r in ev if r]
    ev_days = {datetime.fromtimestamp(t / 1000, tz=timezone.utc).date() for t, _ in ev}
    ctl, ctl_rows = [], []
    for t in sorted(dvol):
        d = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        if d.hour == 8 and d.date() not in ev_days:
            r = sample(t)
            if r:
                ctl.append(r)
                ctl_rows.append({"date": d.date().isoformat(), "kind": "普通日", "implied": dvol[t], "ratio": r})
    import json as _json
    out_dir = Path(settings.data) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ev_rows = [{"date": datetime.fromtimestamp(t / 1000, tz=tz).date().isoformat(), "kind": "FOMC",
                "implied": dvol.get(t - H), "ratio": r} for t, r in ev]
    (out_dir / "history_options.json").write_text(_json.dumps({"events": ev_rows, "control": ctl_rows}))
    gm = lambda xs: math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else float("nan")  # noqa: E731
    lines = ["# 期权事件波动率：历史粗看（仅供探索，不计入正式成绩）", "",
             "口径：隐含波动用 Deribit DVOL（30 天）近似，实际波动用 BTC 永续小时收盘价算 24 小时窗口。"
             "DVOL 把事件摊薄在 30 天里，所以这里只能看方向，正式研究用系统实时记录的短期期权。", "",
             f"- 美联储议息日：{len(ev)} 个，平均 实际/隐含 = {gm([r for _, r in ev]):.2f}",
             f"- 普通日（每天 08:00 UTC 起 24 小时）：{len(ctl)} 个，平均 实际/隐含 = {gm(ctl):.2f}", "",
             "| 议息日 | 实际/隐含 |", "|---|---|"]
    lines += [f"| {datetime.fromtimestamp(t / 1000, tz=tz):%Y-%m-%d} | {r:.2f} |" for t, r in ev]
    return "\n".join(lines)
