"""真实成交价的 BTC 周期权回测（Deribit 历史成交，history.deribit.com）。只做研究，不碰纸面/实盘交易代码。

为什么要它：`options_backtest.py` 用 DVOL（30 天指数）当隐含，跑出的 +$3,946 几乎全是周末口径假象（见 README）。
这里改用**真实历史成交**：Deribit 公共历史接口对已到期合约也返回成交（include_old=true）。

口径
  合约    ：每周五 08:00 UTC 到期的 BTC 期权（周/月/季到期都在周五 08:00）
  入场    ：周五 08:00 UTC（7DTE，FRI7）和周一 08:00 UTC（4DTE，MON4），两种入场分开统计、不混在同一周里
  报价    ：入场前 60 分钟内（不含入场之后，无前视）该到期日每个合约的成交 VWAP（美元 = 成交价 BTC × 成交时指数价）；没有成交就跳过
  行权价  ：平值 = 同时有 call 和 put 成交、离指数最近的行权价；0.15Δ / 0.10Δ / 0.05Δ = 用成交里的隐含波动按 B-S 算 delta，
            取最近的（容差 ±50%×目标 delta）
  结构    ：卖铁鹰（卖 0.15Δ 买 0.05Δ；卖 0.10Δ 买 0.05Δ）、卖铁蝶（卖平值、买 0.10Δ 翼）、买跨式（平值）；
            卖宽跨式 0.15Δ 只作参考（无上限风险，"最大风险"按 10% 标的近似）。每结构 1 张 = 1 BTC
  出场    ：持有到期（按到期前 30 分钟成交里的指数价中位数结算；没有就用永续 1h K 线）；
            提前止盈变体：入场时刻之后，每个整点用此前 2 小时内各腿最近成交价重估，达到最大盈利的 50%（卖方 = 收到权利金的
            一半；买跨式 = 付出权利金的一半）就平。只用该整点之前的成交（无前视）
  费用    ：每腿每次成交 0.03% 标的、封顶该腿权利金 12.5%（options_paper.leg_fee_usd）+ 滑点 = 权利金的 2% / 5% / 10%（默认 5%）；
            到期交割费 0.015% 标的、封顶内在价值 12.5%（只对实值腿）
  统计    ：按结构 × 年份：周数、胜率、权利金、盈亏占最大风险、每周风险回报、周夏普、最大回撤、最差一周、
            "历史最大回撤 ≤ 20%"的每周风险占比及对应周收益、按这个占比 bootstrap 52 周跌穿 −50% 的概率；
            事件周（周内有 FOMC / CPI，日历沿用 options_backtest）vs 安静周

本机连不上 deribit（沙箱代理 403）：有缓存用缓存，没有就提示到 bot 服务器上跑、退出码 2。**没有合成数据模式**；
测试用的确定性夹具只在 tests/ 里，报告会标明"测试夹具"。

用法：python3 -m desk.options_real_backtest --currency BTC --since 2023-01-01 [--until 2026-10-09] [--slippage 5]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .options_backtest import NetworkBlocked, _blocked, event_calendar
from .options_paper import bs_delta, leg_fee_usd

UTC = timezone.utc
HIST_API = "https://history.deribit.com/api/v2/public"
MIN_MS = 60_000
H_MS = 3_600_000
D_MS = 86_400_000
WINDOW_MS = 30 * MIN_MS                 # 结算：到期前 30 分钟
ENTRY_LOOKBACK_MS = 60 * MIN_MS         # 入场报价窗口 = [入场 − 60 分钟, 入场]（review 2026-10-18：原 ±30 分钟用了入场后的成交选行权价/定价）
EXIT_LOOKBACK_MS = 2 * H_MS             # 提前止盈：每腿只认此前 2 小时内的成交
TARGET_DELTAS = (0.15, 0.10, 0.05)
DELTA_TOL = 0.5                          # |Δ − 目标| ≤ 0.5 × 目标
ATM_MAX_DIST = 0.02                      # 平值行权价离指数 ≤ 2%
FEES = {"fee_pct_underlying": 0.03, "fee_cap_pct_premium": 12.5}
DELIVERY_FEE_PCT = 0.015
SLIPPAGES = (2.0, 5.0, 10.0)
DEFAULT_SLIP = 5.0
TAKE_PROFIT = 0.5
STRANGLE_RISK_PCT = 10.0                 # 宽跨式（参考）的"最大风险"近似 = 10% 标的
MAX_DD_LIMIT = 0.20
BOOT_SIMS = 10_000
BOOT_WEEKS = 52
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

# 结构：(名称, 腿列表 [(方向 +1 买 / −1 卖, "C"/"P", 行权价键)], 是否有限风险)
STRATEGIES = {
    "iron_condor_15_05": ([(-1, "C", 0.15), (-1, "P", 0.15), (1, "C", 0.05), (1, "P", 0.05)], True),
    "iron_condor_10_05": ([(-1, "C", 0.10), (-1, "P", 0.10), (1, "C", 0.05), (1, "P", 0.05)], True),
    "iron_fly_10": ([(-1, "C", "atm"), (-1, "P", "atm"), (1, "C", 0.10), (1, "P", 0.10)], True),
    "long_straddle": ([(1, "C", "atm"), (1, "P", "atm")], True),
    "short_strangle_15_REF": ([(-1, "C", 0.15), (-1, "P", 0.15)], False),
}
NAMES = {"iron_condor_15_05": "卖铁鹰 15Δ/5Δ", "iron_condor_10_05": "卖铁鹰 10Δ/5Δ", "iron_fly_10": "卖铁蝶 平值/10Δ翼",
         "long_straddle": "买跨式 平值", "short_strangle_15_REF": "卖宽跨式 15Δ（参考，无上限风险）"}
ENTRIES = {"FRI7": 7, "MON4": 4}         # 入场 = 到期前 N 天的 08:00 UTC


# ---------------------------------------------------------------- 合约名、日程
def parse_instrument(name: str):
    """'BTC-6JAN23-16500-C' → (到期 datetime 08:00 UTC, 行权价, 'C')；不是期权返回 None。"""
    p = name.split("-")
    if len(p) != 4 or p[3] not in ("C", "P"):
        return None
    d = p[1]
    try:
        day, mon, yy = int(d[:-5]), MONTHS[d[-5:-2]], int(d[-2:])
        return datetime(2000 + yy, mon, day, 8, 0, tzinfo=UTC), float(p[2]), p[3]
    except (KeyError, ValueError):
        return None


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def weekly_expiries(since: date, until: date) -> list[datetime]:
    d = since + timedelta(days=(4 - since.weekday()) % 7)
    out = []
    while d <= until:
        out.append(datetime(d.year, d.month, d.day, 8, 0, tzinfo=UTC))
        d += timedelta(days=7)
    return out


def event_kinds_between(a: datetime, b: datetime, events=None) -> list[str]:
    """(a, b] 内的 FOMC / CPI（排期日历沿用 options_backtest）。"""
    if events is None:
        events = event_calendar(a.date() - timedelta(days=1), b.date() + timedelta(days=1), weekends=False)
    return sorted({e.kind for e in events if e.kind in ("CPI", "FOMC") and a < e.at <= b})


# ---------------------------------------------------------------- 数据源：Deribit 历史接口 + parquet 缓存
TRADE_COLS = ("ts", "instrument", "price", "index", "iv", "amount", "trade_id")


def cache_dir(settings) -> Path:
    return Path(settings.data) / "options_cache" / "real"


def _norm(t: dict) -> dict:
    return {"ts": int(t["timestamp"]), "instrument": t["instrument_name"], "price": float(t["price"]),
            "index": float(t.get("index_price") or 0.0), "iv": float(t.get("iv") or 0.0),
            "amount": float(t.get("amount") or 0.0), "trade_id": str(t.get("trade_id", ""))}


def _paged(method: str, params: dict, start_ms: int, end_ms: int, http_get=None) -> list[dict]:
    """按时间正序翻页；按 trade_id 去重。"""
    import httpx
    get = http_get or httpx.get
    out, seen, a = [], set(), start_ms
    while a <= end_ms:
        r = get(f"{HIST_API}/{method}", params={**params, "start_timestamp": a, "end_timestamp": end_ms,
                                                "count": 1000, "sorting": "asc", "include_old": "true"}, timeout=60)
        r.raise_for_status()
        res = r.json()["result"]
        trades = res.get("trades") or []
        for t in trades:
            n = _norm(t)
            if n["trade_id"] not in seen:
                seen.add(n["trade_id"])
                out.append(n)
        if not res.get("has_more") or not trades:
            break
        last = int(trades[-1]["timestamp"])
        a = last if last > a else a + 1
        time.sleep(0.1)
    return out


class DeribitHistory:
    """真实数据源。网络被拦后不再尝试，只读缓存；缓存没有返回 None（那一周跳过）。"""
    is_fixture = False

    def __init__(self, settings, currency: str = "BTC", offline: bool = False):
        self.s, self.cur, self.blocked, self.block_msg = settings, currency, offline, ""
        self.n_fetched = self.n_cached = self.n_missing = 0
        self._candles: dict[int, float] | None = None

    def label(self) -> str:
        net = f"网络不可达，只用缓存（{self.block_msg[:80]}）" if self.blocked else "Deribit 历史接口 history.deribit.com"
        return (f"真实成交 · {net}；缓存 {cache_dir(self.s)}（新取 {self.n_fetched}、读缓存 {self.n_cached}、"
                f"缺 {self.n_missing} 段）")

    def _path(self, key: str) -> Path:
        return cache_dir(self.s) / f"{key}.parquet"

    def _load(self, key: str):
        import pyarrow.parquet as pq
        p = self._path(key)
        if not p.exists():
            return None
        return pq.read_table(p).to_pylist()

    def _save(self, key: str, rows: list[dict]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        cols = {c: [r[c] for r in rows] for c in TRADE_COLS}
        schema = pa.schema([("ts", pa.int64()), ("instrument", pa.string()), ("price", pa.float64()),
                            ("index", pa.float64()), ("iv", pa.float64()), ("amount", pa.float64()),
                            ("trade_id", pa.string())])
        pq.write_table(pa.table(cols, schema=schema), p)

    def _get(self, key: str, method: str, params: dict, a: int, b: int):
        rows = self._load(key)
        if rows is not None:
            self.n_cached += 1
            return rows
        if self.blocked:
            self.n_missing += 1
            return None
        try:
            rows = _paged(method, params, a, b)
        except Exception as e:  # noqa: BLE001
            if _blocked(e):
                self.blocked, self.block_msg = True, str(e)
                self.n_missing += 1
                return None
            raise
        if b < time.time() * 1000 - H_MS:          # 只缓存已经完整过去的时间段
            self._save(key, rows)
        self.n_fetched += 1
        return rows

    def window(self, a: int, b: int):
        """[a, b] 内本币种全部期权成交。"""
        return self._get(f"{self.cur}_win_{a}_{b}", "get_last_trades_by_currency_and_time",
                         {"currency": self.cur, "kind": "option"}, a, b)

    def instrument(self, name: str, a: int, b: int):
        return self._get(f"inst_{name}_{a}_{b}", "get_last_trades_by_instrument_and_time",
                         {"instrument_name": name}, a, b)

    def candle_close(self, t_ms: int) -> float | None:
        """永续 1h K 线在 t 时刻的价格（开始于 t−1h 的那根的收盘）。只读 options_backtest 的缓存，退路用。"""
        if self._candles is None:
            from .options_backtest import load_series
            self._candles = load_series(self.s, f"{self.cur}_perp_1h") or {}
        return self._candles.get(t_ms - H_MS)


# ---------------------------------------------------------------- 报价与选行权价
def median_index(trades: list[dict], a: int, b: int) -> float | None:
    v = [t["index"] for t in trades if a <= t["ts"] <= b and t["index"] > 0]
    return statistics.median(v) if v else None


def quotes_at(trades: list[dict], expiry: datetime, spot: float, at: datetime) -> dict:
    """窗口成交 → {(K, cp): {usd, iv, n, amount, delta}}，只取该到期日。usd = VWAP(价格 BTC × 当时指数)。"""
    acc: dict = {}
    for t in trades:
        p = parse_instrument(t["instrument"])
        if not p or p[0] != expiry or t["amount"] <= 0 or t["price"] <= 0:
            continue
        k = (p[1], p[2])
        a = acc.setdefault(k, {"w": 0.0, "usd": 0.0, "iv": 0.0, "ivw": 0.0, "n": 0, "inst": t["instrument"]})
        idx = t["index"] or spot
        a["w"] += t["amount"]
        a["usd"] += t["price"] * idx * t["amount"]
        if t["iv"] > 0:
            a["iv"] += t["iv"] * t["amount"]
            a["ivw"] += t["amount"]
        a["n"] += 1
    T = max((expiry - at).total_seconds(), 0) / (365 * 86400)
    out = {}
    for (K, cp), a in acc.items():
        iv = a["iv"] / a["ivw"] / 100 if a["ivw"] else None
        out[(K, cp)] = {"usd": a["usd"] / a["w"], "iv": iv, "n": a["n"], "amount": a["w"], "inst": a["inst"],
                        "delta": bs_delta(spot, K, T, iv, cp) if iv else None}
    return out


def select_strikes(quotes: dict, spot: float, deltas=TARGET_DELTAS) -> dict:
    """{'atm': K 或 None, ('C', 0.15): K, ('P', 0.15): K, ...}；找不到的键不出现。"""
    sel: dict = {}
    both = [K for (K, cp) in quotes if cp == "C" and (K, "P") in quotes]
    if both:
        K = min(both, key=lambda k: abs(k - spot))
        if abs(K - spot) / spot <= ATM_MAX_DIST:
            sel["atm"] = K
    for cp in ("C", "P"):
        for d in deltas:
            cands = [(abs(abs(q["delta"]) - d), K) for (K, c), q in quotes.items()
                     if c == cp and q["delta"] is not None and abs(abs(q["delta"]) - d) <= DELTA_TOL * d
                     and ((K > spot) if cp == "C" else (K < spot))]
            if cands:
                sel[(cp, d)] = min(cands)[1]
    return sel


def build_legs(strategy: str, sel: dict, quotes: dict) -> list[dict] | None:
    legs_def, _ = STRATEGIES[strategy]
    legs = []
    for side, cp, key in legs_def:
        K = sel.get("atm") if key == "atm" else sel.get((cp, key))
        if K is None or (K, cp) not in quotes:
            return None
        q = quotes[(K, cp)]
        legs.append({"side": side, "cp": cp, "K": K, "price": q["usd"], "inst": q["inst"]})
    # 翼必须在短腿外侧（否则不是有限风险结构）
    for cp in ("C", "P"):
        s = [l["K"] for l in legs if l["cp"] == cp and l["side"] < 0]
        lo = [l["K"] for l in legs if l["cp"] == cp and l["side"] > 0]
        if s and lo and strategy != "long_straddle":
            if (cp == "C" and not lo[0] > s[0]) or (cp == "P" and not lo[0] < s[0]):
                return None
    return legs


# ---------------------------------------------------------------- 盈亏
def leg_cost(price: float, spot: float, slip_pct: float) -> float:
    """一条腿一次成交（1 张）的手续费 + 滑点。"""
    return leg_fee_usd(price, spot, 1.0, FEES) + slip_pct / 100 * price


def intrinsic(cp: str, K: float, S: float) -> float:
    return max(S - K, 0.0) if cp == "C" else max(K - S, 0.0)


def structure_entry(legs: list[dict], spot: float, slip_pct: float, strategy: str) -> dict:
    credit = sum(-l["side"] * l["price"] for l in legs)          # 正 = 收钱
    cost_in = sum(leg_cost(l["price"], spot, slip_pct) for l in legs)
    if strategy == "long_straddle":
        max_risk = -credit + cost_in
    elif STRATEGIES[strategy][1]:
        widths = []
        for cp in ("C", "P"):
            s = [l["K"] for l in legs if l["cp"] == cp and l["side"] < 0]
            lo = [l["K"] for l in legs if l["cp"] == cp and l["side"] > 0]
            if s and lo:
                widths.append(abs(lo[0] - s[0]))
        # 最坏：一边全亏到翼 − 收到的权利金 + 进场费 + 两条实值腿的交割费上限（按到期标的 ≤ 2 倍入场价算）
        max_risk = max(widths) - credit + cost_in + 2 * DELIVERY_FEE_PCT / 100 * 2 * spot
    else:
        max_risk = STRANGLE_RISK_PCT / 100 * spot
    return {"credit": credit, "cost_in": cost_in, "max_risk": max_risk}


def expiry_pnl(legs: list[dict], spot_in: float, S_T: float, slip_pct: float, strategy: str) -> float:
    """持有到期：Σ 方向 × (内在价值 − 入场价) − 进场费 − 实值腿交割费。"""
    e = structure_entry(legs, spot_in, slip_pct, strategy)
    gross = sum(l["side"] * (intrinsic(l["cp"], l["K"], S_T) - l["price"]) for l in legs)
    deliv = sum(min(DELIVERY_FEE_PCT / 100 * S_T, 0.125 * intrinsic(l["cp"], l["K"], S_T)) for l in legs)
    return gross - e["cost_in"] - deliv


def early_exit(legs: list[dict], leg_trades: dict[str, list[dict]], entry: datetime, expiry: datetime,
               spot_in: float, slip_pct: float, strategy: str) -> dict | None:
    """入场之后每个整点检查一次；只用 ts ≤ 检查时刻、且在入场时刻之后的成交。
    卖方：浮盈 ≥ 收到权利金的 50% 就平；买跨式：浮盈 ≥ 付出权利金的 50%。返回 {at, pnl} 或 None（没触发）。"""
    e = structure_entry(legs, spot_in, slip_pct, strategy)
    target = TAKE_PROFIT * abs(e["credit"])
    if target <= 0:
        return None
    start = ms(entry)
    t = (start // H_MS + 1) * H_MS
    end = ms(expiry) - H_MS
    while t <= end:
        prices, ok = [], True
        for l in legs:
            tr = [x for x in leg_trades.get(l["inst"], []) if start < x["ts"] <= t and x["ts"] > t - EXIT_LOOKBACK_MS
                  and x["price"] > 0]
            if tr:
                last = max(tr, key=lambda x: x["ts"])
                prices.append(last["price"] * (last["index"] or spot_in))
            elif l["side"] > 0 and strategy != "long_straddle":
                prices.append(0.0)               # 翼没成交：按 0 卖回（保守）
            else:
                ok = False
                break
        if ok:
            gross = sum(l["side"] * (p - l["price"]) for l, p in zip(legs, prices))
            if gross >= target:
                cost_out = sum(leg_cost(p, spot_in, slip_pct) for p in prices if p > 0)
                return {"at": t, "pnl": gross - e["cost_in"] - cost_out}
        t += H_MS
    return None


# ---------------------------------------------------------------- 逐周计算
@dataclass
class WeekData:
    expiry: datetime
    entry_kind: str
    entry: datetime
    spot: float
    settle: float | None
    quotes: dict
    sel: dict
    leg_trades: dict = field(default_factory=dict)
    events: list = field(default_factory=list)


def load_week(src, expiry: datetime, entry_kind: str, events) -> tuple[WeekData | None, str]:
    entry = expiry - timedelta(days=ENTRIES[entry_kind])
    a, b = ms(entry) - ENTRY_LOOKBACK_MS, ms(entry)
    win = src.window(a, b)
    if win is None:
        return None, "缺数据"
    spot = median_index(win, a, b) or src.candle_close(ms(entry))
    if not spot:
        return None, "没有指数价"
    quotes = quotes_at(win, expiry, spot, entry)
    if not quotes:
        return None, "入场窗口内该到期日没有成交"
    sel = select_strikes(quotes, spot)
    sw = src.window(ms(expiry) - WINDOW_MS, ms(expiry) + WINDOW_MS)
    settle = (median_index(sw, ms(expiry) - WINDOW_MS, ms(expiry)) if sw else None) or src.candle_close(ms(expiry))
    if not settle:
        return None, "没有到期结算价"
    insts = {q["inst"] for k, q in quotes.items()
             if k[0] in {v for v in sel.values()}}
    lt = {}
    for name in sorted(insts):
        tr = src.instrument(name, ms(entry), ms(expiry))
        lt[name] = tr or []
    return WeekData(expiry, entry_kind, entry, spot, settle, quotes, sel, lt,
                    event_kinds_between(entry, expiry, events)), ""


def week_rows(w: WeekData, slip_pct: float) -> list[dict]:
    out = []
    for strat in STRATEGIES:
        legs = build_legs(strat, w.sel, w.quotes)
        if not legs:
            continue
        e = structure_entry(legs, w.spot, slip_pct, strat)
        if e["max_risk"] <= 0:
            continue
        hold = expiry_pnl(legs, w.spot, w.settle, slip_pct, strat)
        ee = early_exit(legs, w.leg_trades, w.entry, w.expiry, w.spot, slip_pct, strat)
        base = {"strategy": strat, "entry_kind": w.entry_kind, "expiry": w.expiry.date().isoformat(),
                "year": w.expiry.year, "spot": w.spot, "settle": w.settle, "premium": e["credit"],
                "max_risk": e["max_risk"], "event": bool(w.events), "events": ",".join(w.events),
                "strikes": "/".join(f"{l['cp']}{l['K']:.0f}{'+' if l['side'] > 0 else '-'}" for l in legs),
                "slippage_pct": slip_pct}
        out.append({**base, "variant": "hold", "pnl": hold, "r": hold / e["max_risk"], "exit_at": None})
        p = ee["pnl"] if ee else hold
        out.append({**base, "variant": "tp50", "pnl": p, "r": p / e["max_risk"],
                    "exit_at": datetime.fromtimestamp(ee["at"] / 1000, UTC).isoformat() if ee else None})
    return out


def collect(src, since: date, until: date, slippages=SLIPPAGES) -> tuple[list[dict], dict]:
    events = event_calendar(since - timedelta(days=8), until + timedelta(days=1), weekends=False)
    rows, skipped = [], {}
    for x in weekly_expiries(since, until):
        for kind in ENTRIES:
            w, why = load_week(src, x, kind, events)
            if w is None:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            for sp in slippages:
                rows.extend(week_rows(w, sp))
    return rows, skipped


# ---------------------------------------------------------------- 统计
def max_drawdown_additive(rs: list[float]) -> float:
    """累计 r（单位 = 1 个最大风险）的最大回撤。"""
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def compound_dd(rs: list[float], f: float) -> float:
    eq = peak = 1.0
    dd = 0.0
    for r in rs:
        eq *= max(1 + f * max(r, -1.0), 0.0)
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak if peak > 0 else 1.0)
    return dd


def risk_fraction(rs: list[float], limit: float = MAX_DD_LIMIT) -> float:
    """每周拿多少比例的资金当"最大风险"，历史最大回撤刚好 ≤ limit（0.5% 步长，上限 100%）。"""
    best = 0.0
    for i in range(1, 201):
        f = i * 0.005
        if compound_dd(rs, f) <= limit:
            best = f
    return best


def p_ruin(rs: list[float], f: float, weeks: int = BOOT_WEEKS, sims: int = BOOT_SIMS, level: float = 0.5,
           seed: int = 20261018) -> float:
    """bootstrap（有放回抽周）：52 周内资金任一时刻跌到 ≤ 50% 的概率。"""
    if not rs or f <= 0:
        return float("nan")
    rng = random.Random(seed)
    hit = 0
    for _ in range(sims):
        eq = 1.0
        for _ in range(weeks):
            eq *= max(1 + f * max(rng.choice(rs), -1.0), 0.0)
            if eq <= level:
                hit += 1
                break
    return hit / sims


def stats(rows: list[dict], boot: bool = True) -> dict:
    rows = sorted(rows, key=lambda r: r["expiry"])
    rs = [r["r"] for r in rows]
    n = len(rs)
    if not n:
        return {"n": 0}
    mean = statistics.fmean(rs)
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    f = risk_fraction(rs)
    return {"n": n, "win": sum(r > 0 for r in rs) / n, "premium": statistics.fmean(r["premium"] for r in rows),
            "premium_pct": statistics.fmean(r["premium"] / r["spot"] * 100 for r in rows),
            "risk": statistics.fmean(r["max_risk"] for r in rows), "pnl": sum(r["pnl"] for r in rows),
            "total_r": sum(rs), "mean_r": mean, "sharpe": mean / sd if sd > 0 else float("nan"),
            "max_dd": max_drawdown_additive(rs), "worst": min(rs), "f": f, "f_ret": f * mean,
            "p_ruin": p_ruin(rs, f) if boot and n >= 10 else float("nan")}


def _pct(x, nd=1):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{nd}f}%"


def _ruin(a: dict) -> str:
    return "—" if math.isnan(a["p_ruin"]) else f"{a['p_ruin'] * 100:.1f}%"


def _table(groups: list[tuple[str, dict]]) -> list[str]:
    L = ["| 组 | 周数 | 胜率 | 权利金 $（占标的） | 平均最大风险 $ | 盈亏 $ | 盈亏/风险 合计 | 每周风险回报 | 周夏普 | 最大回撤（×风险） | 最差一周 | DD≤20% 的周风险占比 | 对应周收益 | 52 周跌穿 −50% |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, a in groups:
        if not a.get("n"):
            continue
        L.append(f"| {name} | {a['n']} | {a['win'] * 100:.0f}% | {a['premium']:+.0f}（{a['premium_pct']:.2f}%） | "
                 f"{a['risk']:.0f} | {a['pnl']:+.0f} | {a['total_r']:+.2f} | {_pct(a['mean_r'], 2)} | "
                 f"{a['sharpe']:+.2f} | {a['max_dd']:.2f} | {_pct(a['worst'], 0)} | {a['f'] * 100:.1f}% | "
                 f"{_pct(a['f_ret'], 2)} | {_ruin(a)} |")
    return L


def report_md(currency: str, since: date, until: date, rows: list[dict], skipped: dict, source_label: str,
              is_fixture: bool, slip: float = DEFAULT_SLIP) -> str:
    L = []
    if is_fixture:
        L += ["# ⚠ 测试夹具（确定性假数据，不是真实成交）——不要当结果", ""]
    L += [f"# {currency} 周期权真实成交回测（{since} → {until}）", "",
          f"- 数据：{source_label}",
          f"- 默认滑点：每腿权利金的 {slip:g}%；手续费每腿 0.03% 标的、封顶权利金 12.5%；到期交割费 0.015% 封顶内在价值 12.5%",
          f"- 跳过的入场：{', '.join(f'{k} {v}' for k, v in skipped.items()) or '无'}",
          "- 每结构 1 张（1 BTC）。盈亏/风险 = 每周盈亏 ÷ 该周的最大可能亏损；宽跨式的「最大风险」按 10% 标的近似，只作参考。",
          "- 「DD≤20% 的周风险占比」= 每周把资金的这一比例当作最大风险、按历史顺序复利时历史最大回撤 ≤ 20% 的最大比例；"
          "「52 周跌穿 −50%」= 用这个比例有放回抽 52 周、任一时刻资金 ≤ 50% 的概率（10,000 次）。", ""]
    base = [r for r in rows if r["slippage_pct"] == slip]
    for kind in ENTRIES:
        for var, vname in (("hold", "持有到期"), ("tp50", "50% 止盈")):
            sub = [r for r in base if r["entry_kind"] == kind and r["variant"] == var]
            if not sub:
                continue
            L += [f"## {kind}（到期前 {ENTRIES[kind]} 天入场）· {vname}", ""]
            groups = []
            for st in STRATEGIES:
                ss = [r for r in sub if r["strategy"] == st]
                for y in sorted({r["year"] for r in ss}):
                    groups.append((f"{NAMES[st]} {y}", stats([r for r in ss if r["year"] == y], boot=False)))
                groups.append((f"**{NAMES[st]} 全部**", stats(ss)))
            L += _table(groups) + [""]
            L += ["事件周（周内有 FOMC/CPI）vs 安静周：", ""]
            g2 = []
            for st in STRATEGIES:
                ss = [r for r in sub if r["strategy"] == st]
                g2.append((f"{NAMES[st]} 事件周", stats([r for r in ss if r["event"]])))
                g2.append((f"{NAMES[st]} 安静周", stats([r for r in ss if not r["event"]])))
            L += _table(g2) + [""]
    L += ["## 滑点敏感性（全部年份，持有到期）", "", "| 入场 | 结构 | 滑点 | 周数 | 每周风险回报 | 周夏普 | DD≤20% 占比 | 对应周收益 | 52 周跌穿 −50% |",
          "|---|---|---|---|---|---|---|---|---|"]
    for kind in ENTRIES:
        for st in STRATEGIES:
            for sp in sorted({r["slippage_pct"] for r in rows}):
                a = stats([r for r in rows if r["entry_kind"] == kind and r["variant"] == "hold"
                           and r["strategy"] == st and r["slippage_pct"] == sp])
                if a.get("n"):
                    pr = _ruin(a)
                    L.append(f"| {kind} | {NAMES[st]} | {sp:g}% | {a['n']} | {_pct(a['mean_r'], 2)} | {a['sharpe']:+.2f} | "
                             f"{a['f'] * 100:.1f}% | {_pct(a['f_ret'], 2)} | {pr} |")
    L += ["", "## 口径与局限", "",
          "- 入场价 = 入场前 60 分钟成交 VWAP（不含入场之后的成交），不是盘口；真实能成交的价可能更差（滑点 2/5/10% 就是为这个）。"
          "提前止盈只用入场之后、检查时刻之前的成交。",
          "- **「DD≤20% 的周风险占比」、「对应周收益」和「52 周跌穿 −50%」是用同一段数据挑出来再在同一段数据上评估的（样本内），偏乐观**："
          "未来的最大回撤几乎一定比历史见过的更深；bootstrap 只能抽到历史里出现过的最差一周。不能当样本外成绩。",
          "- delta 用成交隐含波动按 B-S（r=0）算，不是交易所的标记 delta。",
          "- 结算价 = 到期前 30 分钟成交里的指数价中位数（Deribit 用 30 分钟 TWAP），没有就用永续 K 线（有基差）。",
          "- 美股（IBIT/SPY）上的对应结构费用、点差、实物交割都不同；这里只回答“真实价格下这些结构在 BTC 周期权上有没有边际”。",
          "- 宽跨式无上限风险，只作参考，不进任何门。"]
    return "\n".join(L) + "\n"


def summary_json(rows: list[dict], slip: float = DEFAULT_SLIP) -> dict:
    out = {}
    for kind in ENTRIES:
        for var in ("hold", "tp50"):
            for st in STRATEGIES:
                a = stats([r for r in rows if r["entry_kind"] == kind and r["variant"] == var and r["strategy"] == st
                           and r["slippage_pct"] == slip])
                if a.get("n"):
                    out[f"{kind}/{var}/{st}"] = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in a.items()}
    return out


# ---------------------------------------------------------------- 入口
def run(settings, src, currency: str, since: date, until: date, out: Path | None = None,
        slip: float = DEFAULT_SLIP) -> tuple[str, list[dict]]:
    slips = tuple(sorted(set(SLIPPAGES) | {slip}))
    rows, skipped = collect(src, since, until, slips)
    if not rows and getattr(src, "blocked", False):
        raise NetworkBlocked(src.block_msg or "deribit 不可达，本地也没有缓存")
    md = report_md(currency, since, until, rows, skipped, src.label(), src.is_fixture, slip)
    tag = "_FIXTURE" if src.is_fixture else ""
    out = out or Path(settings.data) / "reports" / f"options_real_backtest_{currency}{tag}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    js = {"method": "deribit_real_trades", "fixture": bool(src.is_fixture), "currency": currency,
          "since": since.isoformat(), "until": until.isoformat(), "slippage_pct": slip, "skipped": skipped,
          "by_group": summary_json(rows, slip), "written_at": datetime.now(UTC).isoformat()}
    out.with_name(out.stem + "_summary.json").write_text(json.dumps(js, ensure_ascii=False, indent=1))
    return md, rows


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description="真实成交价的 BTC 周期权回测（Deribit 历史成交）")
    ap.add_argument("--currency", default="BTC")
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--until", default=None, help="默认 = 最近一个已到期的周五")
    ap.add_argument("--slippage", type=float, default=DEFAULT_SLIP, help="默认每腿权利金的 5%%；2/5/10 总会一起算")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    s = config.load()
    cur = a.currency.upper()
    since = date.fromisoformat(a.since)
    until = date.fromisoformat(a.until) if a.until else (datetime.now(UTC) - timedelta(hours=9)).date()
    src = DeribitHistory(s, cur, offline=a.offline)
    try:
        md, rows = run(s, src, cur, since, until, a.out, a.slippage)
    except NetworkBlocked as e:
        print(f"本机连不上 history.deribit.com（{str(e)[:100]}），本地也没有缓存。\n"
              f"请到 bot 服务器上跑：python3 -m desk.options_real_backtest --currency {cur} --since {a.since}\n"
              f"成交会缓存到 {cache_dir(s)}/（按入场窗口和合约分文件），之后可以 --offline 离线重算。\n"
              f"这里不会用合成数据代替。")
        return 2
    if src.blocked:
        print(f"（注意：中途网络不可达，只用了缓存；缺 {src.n_missing} 段，对应的周已跳过）")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
