"""回测公用件：K 线抓取/缓存/取价、资金费率历史、以及"按偏离出手"的盈亏统计。

overnight_backtest（假设 #23，美股隔夜）和 windows_backtest（所有"底层休市、链上在交易"的窗口）共用这里的代码，
避免两份复制。备忘 7.2：事后回补的 K 线只能用于粗略探索，不计入正式成绩。
"""
from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc

# Hyperliquid candleSnapshot 支持 1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w 1M；每次最多返回 5000 根。
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}
INTERVAL_PREF = ("15m", "5m", "1h")
HOUR_MS = 3_600_000

RUN_ON_HOST_HINT = (
    "本机连不上 api.hyperliquid.xyz（代理 403）。到能访问 Hyperliquid 的机器（bot 服务器）上跑同一条命令：\n"
    "    cd ~/weekend-desk && python3 -m desk.windows_backtest --since 2026-04-01\n"
    "K 线和资金费率会缓存到 data/backfill/（candles_*.parquet、funding_*.parquet），"
    "之后把这个目录拷回来，本机离线也能重算。"
)


# ---------------------------------------------------------------- K 线取价
def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def price_at_or_before(by_t: dict[int, dict], iv: int, t: datetime, max_back: int = 4) -> float | None:
    """t 时刻（含）之前最后一根已收盘 K 线的收盘价。K 线 [k, k+iv) 的收盘价代表 k+iv 时刻的价格。

    链上没成交的区间 Hyperliquid 可能不返回 K 线，所以最多往前找 max_back 根。
    """
    k = (ms(t) // iv) * iv - iv
    for i in range(max_back):
        c = by_t.get(k - i * iv)
        if c:
            return float(c["c"])
    return None


def price_at_or_after(by_t: dict[int, dict], iv: int, t: datetime, max_fwd: int = 4) -> float | None:
    """收盘时刻 ≥ t 的第一根 K 线的收盘价（重开后第一个可成交价的近似）。"""
    k = math.ceil(ms(t) / iv) * iv - iv
    for i in range(max_fwd):
        c = by_t.get(k + i * iv)
        if c:
            return float(c["c"])
    return None


# ---------------------------------------------------------------- 网络探测
def hyperliquid_reachable(timeout: float = 8.0) -> tuple[bool, str]:
    """能不能打到 Hyperliquid info 接口。沙盒里代理会直接 403，这里把原因带回去好打印提示。"""
    try:
        import httpx
        from . import backfill
        r = httpx.post(backfill.INFO_URL, json={"type": "meta"}, timeout=timeout)
        r.raise_for_status()
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def is_blocked_error(e: Exception | str) -> bool:
    s = str(e)
    return "403" in s or "Proxy" in s or "CONNECT" in s or "connect_rejected" in s


# ---------------------------------------------------------------- 限速（2026-10-15 universe 扫描 39/45 个"周末数 0"的根因）
# Hyperliquid info 接口按 IP 限速：每分钟 1200 权重。candleSnapshot 基础权重 20，另外每返回 60 根 K 线 +1，
# 一次 5000 根 ≈ 104 权重 → 一分钟只够约 10 次；而服务器上的收集器同一个 IP 也在用权重。
# 以前 universe scan 对 45 个候选连着打、没有节流也没有重试：前 6 个拿到 K 线，之后全被 429，
# 429 又被当成"这个周期没有"去试 5m、1h（再吃 429），最后 RuntimeError 被 backtest_candidate 吞成 n=0，
# 报告里只显示"周末数 0"。修法：按权重节流 + 429/5xx 退避重试 + 不在 429 时换周期 + 新鲜缓存直接用 + 报告列出失败原因。
INFO_WEIGHT_BUDGET = 800       # 每分钟自己最多用多少权重（留 400 给收集器等同 IP 的程序）
CANDLE_BASE_WEIGHT = 20
CANDLE_MAX_WEIGHT = CANDLE_BASE_WEIGHT + 5000 // 60 + 1


class Pacer:
    """滑动 60 秒窗口的权重节流。clock/sleep 可注入，测试用假时钟。"""

    def __init__(self, budget: float = INFO_WEIGHT_BUDGET, window_s: float = 60.0, clock=time.monotonic, sleep=time.sleep):
        self.budget, self.window = budget, window_s
        self.clock, self.sleep = clock, sleep
        self.used: list[tuple[float, float]] = []
        self.slept = 0.0

    def _trim(self) -> None:
        now = self.clock()
        self.used = [(t, w) for t, w in self.used if now - t < self.window]

    def wait(self, weight: float) -> None:
        """等到"窗口内已用 + 这次最多会用"不超过预算。"""
        self._trim()
        while self.used and sum(w for _, w in self.used) + weight > self.budget:
            dt = self.window - (self.clock() - self.used[0][0]) + 0.05
            if dt > 0:
                self.sleep(dt)
                self.slept += dt
            self._trim()

    def record(self, weight: float) -> None:
        self.used.append((self.clock(), weight))


PACER = Pacer()


def is_rate_limited(e: Exception | str) -> bool:
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    return code == 429 or "429" in str(e) or "Too Many Requests" in str(e) or "rate limit" in str(e).lower()


def _is_retryable(e: Exception) -> bool:
    if is_rate_limited(e):
        return True
    code = getattr(getattr(e, "response", None), "status_code", None)
    if code is not None and 500 <= code < 600:
        return True
    return type(e).__name__ in ("ReadTimeout", "ConnectTimeout", "RemoteProtocolError", "ReadError", "TimeoutException")


def _httpx_post(payload: dict, timeout: float = 60.0):
    import httpx
    from . import backfill
    r = httpx.post(backfill.INFO_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def info_post(payload: dict, weight: float = CANDLE_MAX_WEIGHT, post=None, pacer: Pacer | None = None,
              retries: int = 5, backoff_s: float = 2.0, items_per_weight: int = 60):
    """节流 + 退避重试的 info 请求。被代理拦（403）不重试——整台机器连不上，重试没用。"""
    post = post or _httpx_post
    pacer = pacer or PACER
    last: Exception | None = None
    for attempt in range(retries + 1):
        pacer.wait(weight)
        try:
            res = post(payload)
        except Exception as e:  # noqa: BLE001
            pacer.record(CANDLE_BASE_WEIGHT)
            last = e
            if is_blocked_error(e) and not is_rate_limited(e):
                raise
            if not _is_retryable(e) or attempt == retries:
                raise
            # 429：等满一个窗口再试（指数退避，封顶 60 秒）
            dt = min(60.0, backoff_s * (2 ** attempt))
            pacer.sleep(dt)
            pacer.slept += dt
            continue
        n = len(res) if isinstance(res, list) else 0
        pacer.record(CANDLE_BASE_WEIGHT + n // items_per_weight)
        return res
    raise last  # pragma: no cover


# ---------------------------------------------------------------- K 线抓取 + 缓存
def fetch_candles_paged(coin: str, interval: str, days: int, post=None, pacer: Pacer | None = None) -> list[dict]:
    """沿用 backfill.fetch_candles 的接口，但 candleSnapshot 一次最多 5000 根，往前翻页直到够 days 天。

    Hyperliquid 每个周期只保留最近 5000 根（15m ≈ 52 天，1h ≈ 208 天），更早的要靠本地缓存累积（save_cache 会合并）。
    """
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    out: dict[int, dict] = {}
    cur_end = end
    for _ in range(60):
        cs = info_post({"type": "candleSnapshot",
                        "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": cur_end}},
                       post=post, pacer=pacer)
        if not cs:
            break
        new = [c for c in cs if int(c["t"]) not in out]
        for c in cs:
            out[int(c["t"])] = c
        if not new or int(cs[0]["t"]) <= start or len(cs) < 5000:
            break   # 不满 5000 根说明这一段已经是能拿到的全部，不用再翻（省一次请求的权重）
        cur_end = int(cs[0]["t"]) - 1
    return [out[t] for t in sorted(out)]


def cache_path(settings, coin: str, interval: str) -> Path:
    return Path(settings.data) / "backfill" / f"candles_{coin.replace(':', '_')}_{interval}.parquet"


def _read_cache(p: Path) -> tuple[list[dict], int]:
    import pyarrow.parquet as pq
    tb = pq.read_table(p).to_pylist()
    fetched = max((int(r.get("fetched_at") or 0) for r in tb), default=0)
    return [{"t": int(r["t"]), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]} for r in tb], fetched


def save_cache(settings, coin: str, interval: str, candles: list[dict], merge: bool = True) -> Path:
    """写缓存；merge=True 时和已有缓存按时间戳合并（新数据覆盖旧的同一根），让历史随每次抓取累积、超过 5000 根上限。"""
    import pyarrow as pa
    import pyarrow.parquet as pq
    p = cache_path(settings, coin, interval)
    p.parent.mkdir(parents=True, exist_ok=True)
    by_t: dict[int, dict] = {}
    if merge and p.exists():
        try:
            old, _ = _read_cache(p)
            by_t.update({int(c["t"]): c for c in old})
        except Exception:  # noqa: BLE001
            pass
    by_t.update({int(c["t"]): c for c in candles})
    rows = [by_t[t] for t in sorted(by_t)]
    pq.write_table(pa.table({
        "t": [int(c["t"]) for c in rows], "o": [float(c["o"]) for c in rows], "h": [float(c["h"]) for c in rows],
        "l": [float(c["l"]) for c in rows], "c": [float(c["c"]) for c in rows], "v": [float(c["v"]) for c in rows],
        "fetched_at": [int(time.time() * 1000)] * len(rows)}), p)
    return p


def load_cache_iv(settings, coin: str, interval: str) -> tuple[list[dict], int] | None:
    """指定周期的缓存 → (candles, fetched_at_ms)；没有 → None。"""
    p = cache_path(settings, coin, interval)
    if not p.exists():
        return None
    return _read_cache(p)


def load_cache(settings, coin: str) -> tuple[list[dict], str] | None:
    """按偏好顺序找本地缓存（含周末 backfill 存的 1h 文件）。"""
    for iv in INTERVAL_PREF:
        got = load_cache_iv(settings, coin, iv)
        if got:
            return got[0], iv
    return None


def get_candles(settings, coin: str, days: int, max_cache_age_h: float | None = None, post=None,
                pacer: Pacer | None = None) -> tuple[list[dict], str, str]:
    """返回 (candles, interval, 来源说明)。网络失败 → 本地缓存；都没有 → 抛异常。

    max_cache_age_h：缓存（任一偏好周期）在这么多小时内抓过就直接用、不打网络（universe 扫描 45 个名字时省权重）。
    被限速（429）时由 info_post 退避重试；重试用完也不换周期（换周期一样被限速，只会更糟）。
    """
    if max_cache_age_h is not None:
        for iv in INTERVAL_PREF:
            got = load_cache_iv(settings, coin, iv)
            if got and got[0] and (time.time() * 1000 - got[1]) < max_cache_age_h * 3600 * 1000:
                return got[0], iv, f"本地缓存 {cache_path(settings, coin, iv).name}（{len(got[0])} 根，{max_cache_age_h:g} 小时内抓过，未重抓）"
    errs = []
    for iv in INTERVAL_PREF:
        try:
            cs = fetch_candles_paged(coin, iv, days) if post is None and pacer is None else \
                fetch_candles_paged(coin, iv, days, post=post, pacer=pacer)
            if cs:
                save_cache(settings, coin, iv, cs)
                merged = load_cache_iv(settings, coin, iv)
                cs_all = merged[0] if merged else cs
                return cs_all, iv, f"Hyperliquid API（{iv} K 线，本次 {len(cs)} 根，合并缓存后 {len(cs_all)} 根）"
            errs.append(f"{iv}: 空")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{iv}: {type(e).__name__} {e}")
            if is_blocked_error(e) or "Proxy" in type(e).__name__ or is_rate_limited(e):
                break  # 整个主机被拦 / 被限速，换周期也没用
    cached = load_cache(settings, coin)
    if cached:
        cs, iv = cached
        return cs, iv, f"本地缓存 {cache_path(settings, coin, iv).name}（{len(cs)} 根；网络失败：{'; '.join(errs)}）"
    raise RuntimeError(f"没有 K 线：网络失败（{'; '.join(errs)}），本地也没有缓存")


# ---------------------------------------------------------------- 资金费率历史
def fetch_funding_history(coin: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
    """Hyperliquid `fundingHistory`：每小时一条 {coin, fundingRate, premium, time}，一次最多约 500 条，往后翻页。"""
    end_ms = end_ms or int(time.time() * 1000)
    out: dict[int, float] = {}
    cur = start_ms
    for _ in range(200):
        rows = info_post({"type": "fundingHistory", "coin": coin, "startTime": cur, "endTime": end_ms},
                         weight=CANDLE_BASE_WEIGHT + 500 // 20, items_per_weight=20)
        if not rows:
            break
        for x in rows:
            out[int(x["time"])] = float(x["fundingRate"])
        last = int(rows[-1]["time"])
        if last <= cur or last >= end_ms:
            break
        cur = last + 1
    return [{"t": t, "rate": out[t]} for t in sorted(out)]


def funding_cache_path(settings, coin: str) -> Path:
    return Path(settings.data) / "backfill" / f"funding_{coin.replace(':', '_')}.parquet"


def save_funding_cache(settings, coin: str, rows: list[dict]) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq
    p = funding_cache_path(settings, coin)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"t": [int(r["t"]) for r in rows], "rate": [float(r["rate"]) for r in rows]}), p)
    return p


def load_funding_cache(settings, coin: str) -> list[dict] | None:
    import pyarrow.parquet as pq
    p = funding_cache_path(settings, coin)
    if not p.exists():
        return None
    return [{"t": int(r["t"]), "rate": float(r["rate"])} for r in pq.read_table(p).to_pylist()]


def funding_from_ctx(settings, coin: str, start_ms: int) -> list[dict]:
    """没有网络时退而求其次：从收集器存的 hl_ctx（activeAssetCtx 里的 funding，每小时费率）按小时取最后一条。"""
    import json
    from . import rawstore
    if not rawstore.has_stream(settings.data, "hl_ctx"):
        return []
    rows = rawstore.query(settings.data, "hl_ctx", select="received_at, payload",
                          sql_where=f"key = '{coin}' AND received_at >= {start_ms * 1_000_000}")
    by_hour: dict[int, float] = {}
    for r, p in rows:
        try:
            f = float(json.loads(p)["data"]["ctx"]["funding"])
        except (KeyError, TypeError, ValueError):
            continue
        by_hour[(r // 1_000_000) // HOUR_MS * HOUR_MS] = f
    return [{"t": t, "rate": by_hour[t]} for t in sorted(by_hour)]


def get_funding(settings, coin: str, days: int) -> tuple[list[dict], str]:
    """返回 (每小时费率列表, 来源说明)。网络 → 缓存 → hl_ctx → 空。"""
    start = int(time.time() * 1000) - days * 86400 * 1000
    try:
        rows = fetch_funding_history(coin, start)
        if rows:
            old = load_funding_cache(settings, coin) or []
            merged = {r["t"]: r["rate"] for r in old}
            merged.update({r["t"]: r["rate"] for r in rows})
            rows = [{"t": t, "rate": merged[t]} for t in sorted(merged)]
            save_funding_cache(settings, coin, rows)
            return rows, f"Hyperliquid fundingHistory（{len(rows)} 小时）"
        err = "空"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__} {e}"
    cached = load_funding_cache(settings, coin)
    if cached:
        return cached, f"本地缓存 {funding_cache_path(settings, coin).name}（{len(cached)} 小时；网络失败：{err}）"
    ctx = funding_from_ctx(settings, coin, start)
    if ctx:
        return ctx, f"hl_ctx 原始流（{len(ctx)} 小时；网络失败：{err}）"
    return [], f"无资金费率数据（网络失败：{err}；没有缓存也没有 hl_ctx）→ carry 按 0 计"


def funding_sum(rates: list[dict], a: datetime, b: datetime) -> float | None:
    """(a, b] 内结算的每小时资金费率之和（小数）。空头收 +sum，多头付 −sum。没有数据 → None。"""
    if not rates:
        return None
    a_ms, b_ms = ms(a), ms(b)
    return sum(r["rate"] for r in rates if a_ms < r["t"] <= b_ms)


def carry_bps(side: int, fsum: float | None) -> float:
    """持仓期间资金费率盈亏（bps，站在我们这一侧）：费率为正时多头付、空头收。"""
    if not fsum or not side:
        return 0.0
    return -side * fsum * 1e4


# ---------------------------------------------------------------- 按偏离出手的盈亏统计
def trade_stats(rows: list[dict], cfg, notional_usd: float = 1000.0, threshold_bps: float | None = None,
                dev_key: str = "dev_bps", move_key: str = "move_from_dec_bps", mode: str = "fade",
                half_spread_bps: float = 0.0, carry_key: str | None = None) -> dict:
    """|dev| > 门槛就出手，持到重开后平仓。

    mode="fade"：按 −sign(dev) 开仓（赌偏离被抹掉）；mode="follow"：按 +sign(dev)（赌偏离被确认）。
    手续费按全额：挂单进 maker + 吃单出 taker + 半个点差估计；门槛默认 = 手续费 + edge_buffer。
    carry_key 给出该行"(决策, 平仓] 内资金费率之和"的列名时，另算一列含 carry 的净收益。
    """
    c = cfg["costs"]
    fee = c["maker_bps"] + c["taker_bps"] + half_spread_bps
    thr = threshold_bps if threshold_bps is not None else fee + c["edge_buffer_bps"]
    sgn = -1 if mode == "fade" else 1
    n = len(rows)
    out = {"n": n, "fee_bps": fee, "threshold_bps": thr, "trades": 0, "mode": mode}
    if not n:
        return out
    absdev = sorted(abs(r[dev_key]) for r in rows)
    q = lambda p: absdev[min(n - 1, int(p * n))]  # noqa: E731
    out.update(p50=q(0.5), p75=q(0.75), p90=q(0.9), frac_over=sum(1 for a in absdev if a > thr) / n)
    out["revert_rate_all"] = sum(1 for r in rows if r[dev_key] * r[move_key] < 0) / n
    # 全样本回归系数 beta = y / dev（0 = 偏离全抹掉，1 = 偏离全被确认），最小二乘无截距
    sxx = sum(r[dev_key] ** 2 for r in rows)
    out["beta_y_on_dev"] = (sum(r[dev_key] * r["y_bps"] for r in rows) / sxx) if sxx and "y_bps" in rows[0] else float("nan")
    trades = sorted((r for r in rows if abs(r[dev_key]) > thr), key=lambda r: r["t_decision"])
    pnl = []
    for r in trades:
        side = sgn * (1 if r[dev_key] > 0 else -1)
        gross = side * r[move_key]
        carry = carry_bps(side, r.get(carry_key)) if carry_key else 0.0
        net = gross - fee
        pnl.append({"day": r.get("day") or r.get("label"), "name": r["name"], "dev": r[dev_key], "side": side,
                    "gross_bps": gross, "net_bps": net, "net_usd": net / 1e4 * notional_usd,
                    "carry_bps": carry, "net_carry_bps": net + carry, "net_carry_usd": (net + carry) / 1e4 * notional_usd})
    out["trades"] = len(pnl)
    if not pnl:
        return out
    out["revert_rate"] = sum(1 for p in pnl if p["gross_bps"] > 0) / len(pnl)
    out["hit_rate"] = out["revert_rate"]
    out["win_rate"] = sum(1 for p in pnl if p["net_bps"] > 0) / len(pnl)
    out["avg_net_bps"] = statistics.mean(p["net_bps"] for p in pnl)
    out["median_net_bps"] = statistics.median(p["net_bps"] for p in pnl)
    out["total_net_bps"] = sum(p["net_bps"] for p in pnl)
    out["total_net_usd"] = sum(p["net_usd"] for p in pnl)
    out["avg_carry_bps"] = statistics.mean(p["carry_bps"] for p in pnl)
    out["total_net_carry_usd"] = sum(p["net_carry_usd"] for p in pnl)
    out["avg_net_carry_bps"] = statistics.mean(p["net_carry_bps"] for p in pnl)
    out["worst"] = min(pnl, key=lambda p: p["net_bps"])
    eq = peak = dd = 0.0
    for p in pnl:
        eq += p["net_usd"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    out["max_drawdown_usd"] = dd
    out["pnl"] = pnl
    return out
