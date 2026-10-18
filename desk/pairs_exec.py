"""跨 dex 同底层配对真下单（真钱，HIGH 风险）。信号逻辑全部来自 desk/pairs_lab.py（import，不复制数学）。

交易：同一只股票在两个 HIP-3 dex 上各有一个永续（例如 io:SNDK / xyz:SNDK）。价差 s = (A/B − 1)·1e4 bps 超过
"走前选出的门槛"→ 做空贵的那条腿、做多便宜的那条腿，**等名义**；价差按该变体的出场规则收敛，或持仓满 max_hold_h → 平掉。

三种模式（存在 data/state/pairs_live.json，和 autotrade 的 live.json 完全分开；默认 off）：
- off：什么都不做。
- shadow（演习）：算好"本来会下什么"，写进 pairs 账本并推送，不给交易所发任何单。
- live：真下单。

安全设计（每一条都有测试）：
1. 每张单都带 cloid，标签以 "pairs-" 开头，写进 pairs 账本（先写"已发出"，再写成交）。交易所要求 cloid 是 16 字节十六进制，
   所以发给交易所的是标签的 sha256 前 128 位；标签 ↔ 十六进制的对应也写在账本里。
2. 平仓只用 reduce-only IOC，数量 = 我们自己账本里记的成交量；**绝不**调用 market_close(coin)（它会把整个净仓位包括手动单一起平掉）。
3. 两腿原子性：两腿都是带滑点上限的 IOC 限价。先下盘口较薄的一腿；第二腿按第一腿的实际成交名义定量。任何一腿没成交 / 部分成交 →
   立刻用 reduce-only IOC 回补多出来的那一腿，直到两腿名义差 ≤ 容忍度；失败会重试并推送最高优先级警报，永不静默留下单腿。
4. 下单异常（超时等）→ 用持仓前后差额判断到底成没成；判断不了就停止开新仓并告警（不盲目反向下单，那可能动到手动仓位）。
5. 复用 autotrade 的 _live_preflight（只许 API 钱包私钥 + 余额下限），亏损看门狗是同一套思路：已实现 + 未实现合计超过上限 → 平仓并切回 off。
   autotrade.kill_check 本身会 market_close 且只认 live 账本，所以这里是配对账本版的等价实现，没有改动它。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from . import autotrade, notify
from . import backtest_lib as bl
from . import pairs_lab as pl
from . import universe as uv
from .ledger import Ledger

log = logging.getLogger("pairs_exec")
MODES = autotrade.MODES
PREFIX = "pairs-"
LEV_HARD_CAP = 2.0          # 每腿杠杆硬上限（配置再大也按 2）
DEPTH_FRAC_CAP = 0.10       # 名义 ≤ 第一档深度的 10%（配置再大也按 10%）
MIN_ORDER_USD = 10.0        # Hyperliquid 最小下单价值
EPS = 1e-9
DAY_MS = pl.DAY_MS

DEFAULTS = {
    "interval": "5m", "lookback_days": 17, "warmup_days": 5, "allow": [],
    "max_leg_notional_usd": 100.0, "max_concurrent": 1, "max_total_gross_usd": 200.0,
    "max_pair_loss_usd": 4.0, "daily_loss_stop_usd": 8.0, "leverage": 2, "depth_fraction": 0.10,
    "max_hold_h": 24.0, "slippage_bps": 30.0, "exit_slippage_bps": 60.0, "net_tolerance_frac": 0.05,
    "unwind_retries": 3, "min_balance_usd": 5.0, "fee_bps": 9.0, "cooldown_min": 30,
}


# ------------------------------------------------------------------ 模式开关（和 autotrade 的 live.json 分开）
def _path(settings) -> Path:
    p = Path(settings.data) / "state" / "pairs_live.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get(settings) -> dict:
    d = {"mode": "off", "halt_entries": False, "updated_by": "", "ts": 0}
    try:
        d.update(json.loads(_path(settings).read_text()))
    except (OSError, ValueError):
        pass
    if d.get("mode") not in MODES:
        d["mode"] = "off"
    return d


def put(settings, **kw) -> dict:
    d = get(settings)
    d.update(kw)
    d["ts"] = time.time()
    _path(settings).write_text(json.dumps(d, ensure_ascii=False))
    return d


def cfg(settings) -> dict:
    d = dict(DEFAULTS)
    d.update((settings.raw.get("pairs") if hasattr(settings, "raw") else settings.get("pairs", {})) or {})
    d["leverage"] = max(1.0, min(float(d["leverage"]), LEV_HARD_CAP))
    d["depth_fraction"] = max(0.0, min(float(d["depth_fraction"]), DEPTH_FRAC_CAP))
    d["max_concurrent"] = max(0, int(d["max_concurrent"]))
    return d


def _led(settings) -> Ledger:
    return Ledger(settings.data, "pairs")


# ------------------------------------------------------------------ 交易所客户端（真实；测试注入假的）
def _dex_of(coin: str) -> str:
    return coin.split(":")[0] if ":" in coin else ""


def cloid_hex(label: str) -> str:
    return "0x" + hashlib.sha256(label.encode()).hexdigest()[:32]


class PairsClient(autotrade.Client):
    """在 autotrade.Client 上加：多 dex、盘口、IOC 限价（可 reduce-only、带 cloid）。真下单只发生在 ioc() 和 set_leverage()。"""

    def __init__(self, address: str, key: str, dexs: list[str]):
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        self.address = address
        names = ["", *sorted({d for d in dexs if d})]
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=names)
        self.ex = Exchange(Account.from_key(key), constants.MAINNET_API_URL, account_address=address, perp_dexs=names)

    def position(self, coin: str) -> float:
        st = self.info.user_state(self.address, dex=_dex_of(coin))
        for p in st.get("assetPositions", []):
            pos = p.get("position", {})
            if pos.get("coin") == coin:
                return float(pos.get("szi", 0) or 0)
        return 0.0

    def book(self, coin: str) -> dict:
        lv = self.info.l2_snapshot(coin)["levels"]
        b, a = lv[0][0], lv[1][0]
        bid, ask = float(b["px"]), float(a["px"])
        return {"bid": bid, "ask": ask, "bid_depth_usd": bid * float(b["sz"]), "ask_depth_usd": ask * float(a["sz"])}

    def set_leverage(self, coin: str, lev: int):
        return self.ex.update_leverage(int(lev), coin, is_cross=False)   # 真下单路径之一（改杠杆，不成交）

    def ioc(self, coin: str, is_buy: bool, sz: float, px: float, reduce_only: bool, cloid: str) -> dict:
        from hyperliquid.utils.types import Cloid
        resp = self.ex.order(coin, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only,   # 真下单路径
                             cloid=Cloid.from_str(cloid_hex(cloid)))
        out = {"filled_sz": 0.0, "avg_px": None, "error": None, "raw": resp}
        try:
            if resp.get("status") != "ok":
                out["error"] = str(resp)[:200]
                return out
            for s in resp["response"]["data"]["statuses"]:
                if "filled" in s:
                    out["filled_sz"] += float(s["filled"]["totalSz"])
                    out["avg_px"] = float(s["filled"]["avgPx"])
                elif "error" in s:
                    out["error"] = str(s["error"])[:200]
        except (KeyError, TypeError, ValueError) as e:
            out["error"] = f"解析回包失败 {e}: {str(resp)[:150]}"
        return out


_CLIENT: dict = {}


def _make_client(settings, dexs: list[str]):
    if autotrade._client_factory:
        return autotrade._client_factory()
    a, k = autotrade.creds()
    if not (autotrade.valid_address(a) and autotrade.valid_key(k)):
        raise RuntimeError("还没有在设置里填 Hyperliquid 钱包地址和 API 私钥")
    key = (a, tuple(dexs))
    if _CLIENT.get("key") != key:          # 每分钟调用一次，别每次都重建（重建会打一堆 meta 请求）
        _CLIENT.update(key=key, c=PairsClient(a, k, dexs))
    return _CLIENT["c"]


# ------------------------------------------------------------------ 账本 → 当前持仓（唯一真相来源 = 我们自己的成交记录）
def load_state(settings) -> dict:
    pairs: dict[str, dict] = {}
    sent: dict[str, dict] = {}
    done: set[str] = set()
    for r in _led(settings).all():
        k, pid = r["kind"], r.get("pair_id")
        if k == "open":
            pairs[pid] = {"open": r, "net": {}, "cash": {}, "last_px": {}, "fees": 0.0, "closed": None, "close_req": None}
        elif k == "fill" and pid in pairs:
            p, coin = pairs[pid], r["coin"]
            p["net"][coin] = round(p["net"].get(coin, 0.0) + r["signed_sz"], 10)
            p["cash"][coin] = p["cash"].get(coin, 0.0) - r["signed_sz"] * (r.get("px") or 0.0)
            p["fees"] += r.get("fee_usd", 0.0) or 0.0
            if r.get("px"):
                p["last_px"][coin] = r["px"]
        elif k == "close_req" and pid in pairs:
            pairs[pid]["close_req"] = r
        elif k == "closed" and pid in pairs:
            pairs[pid]["closed"] = r
        if k == "order":
            sent[r["cloid"]] = r
        elif k == "fill":
            done.add(r["cloid"])
    return {"pairs": pairs, "unresolved": [c for c in sent if c not in done]}


def open_pairs(st: dict) -> dict[str, dict]:
    return {pid: p for pid, p in st["pairs"].items() if not p["closed"]}


def has_open(settings) -> bool:
    return bool(open_pairs(load_state(settings)))


def pair_pnl(p: dict, marks: dict[str, float]) -> float | None:
    """已实现现金流 − 估算手续费 + 剩余净仓位按中间价估值。需要的价格缺失 → None。"""
    tot = -p["fees"]
    for coin in set(p["cash"]) | set(p["net"]):
        net = p["net"].get(coin, 0.0)
        tot += p["cash"].get(coin, 0.0)
        if abs(net) > EPS:
            if marks.get(coin) is None:
                return None
            tot += net * marks[coin]
    return tot


def _day_start_ms(now_ms: int) -> int:
    return now_ms // DAY_MS * DAY_MS


# ------------------------------------------------------------------ 小工具
class UnknownOrderState(Exception):
    pass


class Ctx:
    def __init__(self, settings, c, cf, now_ms, mode):
        self.settings, self.c, self.cf, self.now_ms, self.mode = settings, c, cf, now_ms, mode
        self.led = _led(settings)
        self.real = isinstance(c, PairsClient)


def _floor(q: float, dec: int) -> float:
    m = 10 ** dec
    return math.floor(q * m + 1e-9) / m


def _ceil(q: float, dec: int) -> float:
    m = 10 ** dec
    return math.ceil(q * m - 1e-9) / m


def _alert(ctx: Ctx, title: str, body: str, pair_id: str = "-", priority: str = "max"):
    ctx.led.append("alert", pair_id, {"pair_id": pair_id, "title": title, "body": body[:500]})
    notify.push(ctx.settings, title, body[:500], priority=priority)


def _note_skip(ctx: Ctx, key: str, bar_t: int, reason: str):
    """同一对、同一根 K 线、同一个原因只记一次，别每分钟刷账本。"""
    for r in reversed(ctx.led.all()):
        if r["kind"] == "skip" and r.get("pair_key") == key:
            if r.get("bar_t") == bar_t and r.get("reason") == reason:
                return
            break
    ctx.led.append("skip", key, {"pair_key": key, "bar_t": bar_t, "reason": reason})


def _book(ctx: Ctx, coin: str) -> dict | None:
    try:
        b = ctx.c.book(coin)
        return b if b and b.get("bid") and b.get("ask") else None
    except Exception:  # noqa: BLE001
        return None


def _mid(b: dict | None) -> float | None:
    return (b["bid"] + b["ask"]) / 2 if b else None


# ------------------------------------------------------------------ 下单（所有真单都经过 _send）
def _send(ctx: Ctx, pair_id: str, coin: str, is_buy: bool, sz: float, px: float, reduce_only: bool, purpose: str) -> float:
    """发一张 IOC 限价单，返回实际成交数量（≥0）。先写"已发出"再写"成交"。无法判断成交与否 → UnknownOrderState。"""
    n = sum(1 for r in ctx.led.all() if r["kind"] == "order" and r.get("pair_id") == pair_id)
    cloid = f"{PREFIX}{pair_id}-{n + 1}"
    ctx.led.append("order", pair_id, {"pair_id": pair_id, "coin": coin, "cloid": cloid, "cloid_hex": cloid_hex(cloid),
                                      "is_buy": is_buy, "sz": sz, "px": px, "reduce_only": reduce_only, "purpose": purpose})
    try:
        before = ctx.c.position(coin)
    except Exception:  # noqa: BLE001
        before = None
    try:
        r = ctx.c.ioc(coin, is_buy, sz, px, reduce_only, cloid)
    except Exception as e:  # noqa: BLE001
        log.exception("order exception")
        after = None
        try:
            after = ctx.c.position(coin)
        except Exception:  # noqa: BLE001
            pass
        if before is None or after is None:
            ctx.led.append("fill", pair_id, {"pair_id": pair_id, "coin": coin, "cloid": cloid, "signed_sz": 0.0,
                                             "unknown": True, "error": str(e)[:200]})
            put(ctx.settings, halt_entries=True)
            raise UnknownOrderState(f"{coin} {cloid}: {e}") from e
        delta = after - before
        f = min(sz, max(0.0, delta if is_buy else -delta))
        px_f = px
        ctx.led.append("fill", pair_id, {"pair_id": pair_id, "coin": coin, "cloid": cloid,
                                         "signed_sz": f if is_buy else -f, "px": px_f, "purpose": purpose,
                                         "fee_usd": ctx.cf["fee_bps"] / 1e4 * f * px_f, "error": f"异常后按持仓差额判断：{e}"[:200]})
        return f
    f = max(0.0, float(r.get("filled_sz") or 0.0))
    f = min(f, sz)
    avg = float(r.get("avg_px") or px)
    ctx.led.append("fill", pair_id, {"pair_id": pair_id, "coin": coin, "cloid": cloid, "signed_sz": f if is_buy else -f,
                                     "px": avg if f else None, "purpose": purpose, "fee_usd": ctx.cf["fee_bps"] / 1e4 * f * avg,
                                     "error": r.get("error")})
    return f


def _unwind(ctx: Ctx, pair_id: str, coin: str, remaining: float, purpose: str) -> float:
    """把"我们自己"在 coin 上的 remaining（带符号：+ = 我们多、− = 我们空）用 reduce-only IOC 平掉；返回没平掉的剩余。
    数量永远不超过 |remaining|；手动仓位与我们同向时只减我们那一份，反向时 reduce-only 无法安全执行 → 不动并告警。"""
    cf = ctx.cf
    try:
        dec = ctx.c.sz_decimals(coin)
    except Exception:  # noqa: BLE001
        dec = 2
    for k in range(int(cf["unwind_retries"])):
        if abs(remaining) < EPS:
            return 0.0
        sell = remaining > 0
        qty = abs(remaining)
        try:
            net = ctx.c.position(coin)
            reducible = max(0.0, net) if sell else max(0.0, -net)
            if reducible + EPS < qty:
                qty = reducible
                if qty < EPS:
                    _alert(ctx, f"配对平仓冲突：{coin}", f"交易所净仓位 {net} 与我们要平的方向 {'卖' if sell else '买'} {abs(remaining)} 冲突"
                           "（手动仓位反向？），reduce-only 无法安全执行，没有下单。请手动处理。", pair_id)
                    return remaining
        except Exception:  # noqa: BLE001
            pass   # 读不到持仓：reduce-only 本身保证不会加仓，照常发
        if qty < abs(remaining) - EPS:   # 只平得了一部分（手动仓位限制）：向下取整到 szDecimals
            qty = _floor(qty, dec)
        if qty < EPS:
            return remaining
        b = _book(ctx, coin)
        if not b:
            continue
        slip = cf["exit_slippage_bps"] * (2 ** k) / 1e4
        px = autotrade.round_px(b["bid"] * (1 - slip) if sell else b["ask"] * (1 + slip), dec)
        try:
            f = _send(ctx, pair_id, coin, not sell, qty, px, True, purpose)
        except UnknownOrderState:
            return remaining
        remaining -= f if sell else -f
        remaining = 0.0 if abs(remaining) < EPS else remaining
    return remaining


# ------------------------------------------------------------------ 信号（全部来自 pairs_lab）
def _pair_key(p: dict) -> str:
    return f"{p['a']}|{p['b']}"


def _series(pair: dict, data: dict, iv_ms: int, now_ms: int):
    """只用已收盘的 K 线（t + 周期 ≤ now）。"""
    def closed(coin):
        return [c for c in data["candles"].get(coin, []) if int(c["t"]) + iv_ms <= now_ms]
    return pl.align(closed(pair["a"]), closed(pair["b"]), iv_ms)


def signal(pair: dict, data: dict, cf: dict, now_ms: int) -> dict:
    """→ {"ok": True, bar_t, spread, thr, variant, exit_mode, cost} 或 {"ok": False, "why"}。"""
    iv_ms = bl.INTERVAL_MS[cf["interval"]]
    series = _series(pair, data, iv_ms, now_ms)
    if len(series) < 3:
        return {"ok": False, "why": "K 线不足"}
    if series[-1][0] < now_ms - 3 * iv_ms:
        return {"ok": False, "why": "最新 K 线太旧（数据断档）"}
    spread = pl.spread_series(series, "same", iv_ms)
    s_now, bar_t = spread[-1], series[-1][0]
    out = {"ok": True, "bar_t": bar_t, "spread": s_now, "series_n": len(series)}
    la, lb = pair["legs"][pair["a"]], pair["legs"][pair["b"]]
    cost = pl.round_trip_cost(pl.FEE_SCENARIOS["taker"], (la["spread_bps"] or 0) / 2, (lb["spread_bps"] or 0) / 2)
    d = now_ms // DAY_MS
    days = sorted({t // DAY_MS for t, _, _ in series} | {d})
    if len(days) <= cf["warmup_days"]:
        return {**out, "ok": False, "why": f"只有 {len(days)} 天数据，≤ 走前预热 {cf['warmup_days']} 天"}
    fa, fb = data.get("funding", {}).get(pair["a"], []), data.get("funding", {}).get(pair["b"], [])
    by_v = pl.run_variants(series, spread, cost, fa, fb)
    _, picks = pl.walk_forward(by_v, days, cf["warmup_days"])
    best = picks.get(d)
    if not best:
        return {**out, "ok": False, "why": "走前今天没有选中变体（历史累计净收益 ≤ 0）"}
    v = next(v for v in pl.variants() if pl.vname(v) == best)
    return {**out, "thr": cost + v[0], "variant": best, "exit_mode": v[1], "cost": cost}


def _exit_due(exit_mode: str, sg: int, s0: float, s_now: float | None) -> bool:
    if s_now is None:
        return False
    if exit_mode == "conv0":
        return s_now * sg <= 0
    if exit_mode == "half":
        return s_now * sg <= abs(s0) / 2
    return False   # time24：只按 max_hold


# ------------------------------------------------------------------ 数据（真实环境）
_DATA: dict = {}


def load_data(settings, now_ms: int, cf: dict) -> dict:
    """发现（缓存 6 小时）+ 每根新 K 线抓一次两腿 K 线 + 资金费率（缓存 1 小时）。同一根 K 线内重复调用走内存。"""
    iv_ms = bl.INTERVAL_MS[cf["interval"]]
    key = (str(settings.data), now_ms // iv_ms)
    if _DATA.get("key") == key:
        return _DATA["data"]
    cached = Path(settings.data) / "universe_raw.json"
    raw = None
    if cached.exists() and time.time() - cached.stat().st_mtime < 6 * 3600:
        raw = json.loads(cached.read_text())
    if raw is None:
        try:
            raw = uv.discover()
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(raw, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            raw = json.loads(cached.read_text()) if cached.exists() else None
    if raw is None:
        raise RuntimeError("没有 universe 发现数据")
    pairs = [p for p in pl.discover_pairs(raw, uv.gate_cfg(settings)) if p["kind"] == "same" and p["gate_ok"]]
    if cf["allow"]:
        pairs = [p for p in pairs if _pair_key(p) in cf["allow"]]
    candles, funding = {}, {}
    prev = _DATA.get("data", {}) if _DATA.get("settings") == str(settings.data) else {}
    for p in pairs:
        for coin in (p["a"], p["b"]):
            if coin in candles:
                continue
            try:
                candles[coin] = bl.fetch_candles_paged(coin, cf["interval"], cf["lookback_days"])
            except Exception as e:  # noqa: BLE001
                log.warning("candles %s: %s", coin, e)
                candles[coin] = []
            old = prev.get("funding_t", {}).get(coin)
            if old and time.time() - old[0] < 3600:
                funding[coin] = old[1]
            else:
                funding[coin] = bl.get_funding(settings, coin, cf["lookback_days"])[0]
    data = {"pairs": pairs, "candles": candles, "funding": funding,
            "funding_t": {c: (time.time(), r) for c, r in funding.items()}}
    _DATA.update(key=key, data=data, settings=str(settings.data))
    return data


# ------------------------------------------------------------------ 开仓
def _open_pair(ctx: Ctx, pair: dict, sig: dict, st: dict) -> dict:
    cf, now_ms = ctx.cf, ctx.now_ms
    key = _pair_key(pair)
    sg = 1 if sig["spread"] > 0 else -1
    rich, cheap = (pair["a"], pair["b"]) if sg > 0 else (pair["b"], pair["a"])   # s>0 = A 贵：空 A 多 B
    bk = {c: _book(ctx, c) for c in (rich, cheap)}
    if not all(bk.values()):
        return {"skipped": "读不到盘口"}
    s_live = (_mid(bk[pair["a"]]) / _mid(bk[pair["b"]]) - 1) * 1e4
    if s_live * sg <= 0 or abs(s_live) <= sig["thr"]:
        return {"skipped": f"实时价差 {s_live:+.1f} bps 已不超过门槛 {sig['thr']:.1f}"}
    # --- 名义（每腿，美元）：各项上限取最小
    depth = min(bk[rich]["bid_depth_usd"], bk[cheap]["ask_depth_usd"])      # 卖贵的吃买一，买便宜的吃卖一
    n_depth = cf["depth_fraction"] * depth
    live = open_pairs(st)
    gross = sum(abs(q) * p["last_px"].get(c, 0.0) for p in live.values() for c, q in p["net"].items())
    n_gross = (cf["max_total_gross_usd"] - gross) / 2
    notional = min(cf["max_leg_notional_usd"], n_depth, n_gross)
    why = {"max_leg": cf["max_leg_notional_usd"], "depth": n_depth, "gross": n_gross}
    if notional < MIN_ORDER_USD:
        return {"skipped": f"可下名义 ${max(notional, 0):.2f} < 交易所最小 ${MIN_ORDER_USD:.0f}（{', '.join(f'{k}=${v:.0f}' for k, v in why.items())}）"}
    plan = {"pair": key, "rich_short": rich, "cheap_long": cheap, "notional_usd": round(notional, 2), "spread_bps": round(s_live, 1),
            "thr_bps": round(sig["thr"], 1), "variant": sig["variant"], "bar_t": sig["bar_t"]}
    if ctx.mode == "shadow":
        return {"plan": plan}
    # --- live：预检（API 钱包 + 余额下限）、保证金
    pf = autotrade._live_preflight(ctx.settings, cf, ctx.c, check_wallet=ctx.real)
    if pf:
        notify.push(ctx.settings, f"配对下单未通过安全检查：{key}", pf, priority="high")
        return {"skipped": pf}
    try:
        avail = ctx.c.balance().get("available")
    except Exception:  # noqa: BLE001
        avail = None
    if avail is not None and 2 * notional / cf["leverage"] > avail * 0.9:
        notional = avail * 0.9 * cf["leverage"] / 2
        if notional < MIN_ORDER_USD:
            return {"skipped": f"保证金不够（可用 ${avail:.2f}）"}
    pair_id = f"{pair['underlying']}-{now_ms}"
    try:
        return _enter_live(ctx, pair, sig, key, sg, rich, cheap, bk, notional, pair_id)
    except Exception as e:  # noqa: BLE001  任何意外：按账本里的成交把这一对平掉，不留单腿
        log.exception("entry crashed")
        if not any(r["kind"] == "open" and r.get("pair_id") == pair_id for r in ctx.led.all()):
            ctx.led.append("open", pair_id, {"pair_id": pair_id, "pair_key": key, "a": pair["a"], "b": pair["b"], "sg": sg,
                                             "s0": sig["spread"], "thr": sig["thr"], "variant": sig["variant"],
                                             "exit_mode": sig["exit_mode"], "t_ms": now_ms, "bar_t": sig["bar_t"],
                                             "notional_target": notional, "rich_short": rich, "cheap_long": cheap})
        put(ctx.settings, halt_entries=True)
        _alert(ctx, f"配对开仓流程出错，正在按账本平掉：{key}", f"{type(e).__name__}: {e}"[:300], pair_id)
        _close_pair(ctx, pair_id, "entry_exception", load_state(ctx.settings))
        return {"error": "entry_exception", "pair_id": pair_id}


def _enter_live(ctx: Ctx, pair: dict, sig: dict, key: str, sg: int, rich: str, cheap: str, bk: dict, notional: float, pair_id: str) -> dict:
    cf, now_ms = ctx.cf, ctx.now_ms
    dec = {c: ctx.c.sz_decimals(c) for c in (rich, cheap)}
    ctx.led.append("open", pair_id, {"pair_id": pair_id, "pair_key": key, "a": pair["a"], "b": pair["b"], "sg": sg, "s0": sig["spread"],
                                     "thr": sig["thr"], "variant": sig["variant"], "exit_mode": sig["exit_mode"],
                                     "t_ms": now_ms, "bar_t": sig["bar_t"], "notional_target": notional,
                                     "rich_short": rich, "cheap_long": cheap})
    for c in (rich, cheap):
        ctx.c.set_leverage(c, int(cf["leverage"]))
    # 先下较薄的一腿（更可能失败；它失败时另一腿根本没碰）
    legs = {rich: {"buy": False, "px_ref": bk[rich]["bid"], "depth": bk[rich]["bid_depth_usd"]},
            cheap: {"buy": True, "px_ref": bk[cheap]["ask"], "depth": bk[cheap]["ask_depth_usd"]}}
    first = min((rich, cheap), key=lambda c: legs[c]["depth"])   # 平手时先下贵的那条（空腿）
    second = cheap if first == rich else rich
    slip = cf["slippage_bps"] / 1e4

    def limit(c):
        return autotrade.round_px(legs[c]["px_ref"] * (1 + slip) if legs[c]["buy"] else legs[c]["px_ref"] * (1 - slip), dec[c])

    filled, avg = {}, {}
    try:
        sz1 = _floor(notional / legs[first]["px_ref"], dec[first])
        if sz1 <= 0:
            ctx.led.append("closed", pair_id, {"pair_id": pair_id, "reason": "entry_failed:lot", "pnl_usd": 0.0, "t_ms": now_ms})
            return {"skipped": "金额太小凑不够最小下单单位"}
        filled[first] = _send(ctx, pair_id, first, legs[first]["buy"], sz1, limit(first), False, "entry")
        if filled[first] <= 0:
            ctx.led.append("closed", pair_id, {"pair_id": pair_id, "reason": "entry_failed:first_leg", "pnl_usd": 0.0, "t_ms": now_ms})
            return {"skipped": "第一腿没成交，另一腿没下", "pair_id": pair_id}
        n1 = filled[first] * _last_fill_px(ctx, pair_id, first)
        sz2 = _floor(n1 / legs[second]["px_ref"], dec[second])
        filled[second] = _send(ctx, pair_id, second, legs[second]["buy"], sz2, limit(second), False, "entry") if sz2 > 0 else 0.0
    except UnknownOrderState as e:
        _alert(ctx, f"配对下单状态未知，已停止开新仓：{key}", f"{e}。请立刻去 Hyperliquid 核对 {rich} / {cheap} 的持仓和挂单。", pair_id)
        return {"error": "unknown_order_state", "pair_id": pair_id}
    return _balance_legs(ctx, pair_id, rich, cheap, notional, dec)


def _last_fill_px(ctx: Ctx, pair_id: str, coin: str) -> float:
    return load_state(ctx.settings)["pairs"][pair_id]["last_px"].get(coin, 0.0)


def _balance_legs(ctx: Ctx, pair_id: str, rich: str, cheap: str, notional: float, dec: dict) -> dict:
    """两腿成交后：名义差超过容忍度 → 回补多出来的那条腿（reduce-only）。永不留下单腿。"""
    cf = ctx.cf
    key = load_state(ctx.settings)["pairs"][pair_id]["open"]["pair_key"]
    ok = False
    for _ in range(int(cf["unwind_retries"]) + 1):
        p = load_state(ctx.settings)["pairs"][pair_id]
        px = {c: p["last_px"].get(c, 0.0) for c in (rich, cheap)}
        nt = {c: abs(p["net"].get(c, 0.0)) * px[c] for c in (rich, cheap)}
        lot = max(10 ** -dec[c] * px[c] for c in (rich, cheap))
        tol = max(cf["net_tolerance_frac"] * notional, lot)
        gap = nt[rich] - nt[cheap]
        if abs(gap) <= tol:
            ok = True
            break
        big = rich if gap > 0 else cheap
        excess_sz = min(abs(p["net"][big]), _ceil(abs(gap) / px[big], dec[big]))   # 回补整个差额（向上取整到一手），另一腿没成交时 = 全部回补
        left = _unwind(ctx, pair_id, big, excess_sz if p["net"][big] > 0 else -excess_sz, "unwind_excess")
        if abs(left) > EPS:
            break
    p = load_state(ctx.settings)["pairs"][pair_id]
    flat = all(abs(q) < EPS for q in p["net"].values())
    if flat:
        pnl = pair_pnl(p, {})
        ctx.led.append("closed", pair_id, {"pair_id": pair_id, "reason": "entry_failed:unwound", "pnl_usd": round(pnl or 0.0, 4), "t_ms": ctx.now_ms})
        _alert(ctx, f"配对开仓失败，已回补成零仓位：{key}", f"一条腿没成交 / 成交不足，已用 reduce-only 全部回补。估算盈亏 ${pnl or 0:+.2f}（含手续费估算）。", pair_id, "high")
        return {"opened": False, "unwound": True, "pair_id": pair_id}
    if not ok:
        put(ctx.settings, halt_entries=True)
        _alert(ctx, f"配对两腿名义不平衡且回补失败：{key}", f"当前净仓 {p['net']}。已停止开新仓，下一分钟会继续尝试平掉这一对；请检查 Hyperliquid。", pair_id)
        ctx.led.append("close_req", pair_id, {"pair_id": pair_id, "reason": "imbalance", "t_ms": ctx.now_ms})
        return {"opened": True, "balanced": False, "pair_id": pair_id}
    if any(r["kind"] == "order" and r.get("purpose") == "unwind_excess" and r.get("pair_id") == pair_id for r in ctx.led.all()):
        _alert(ctx, f"配对部分成交，已回补多余腿：{key}", f"净仓 {p['net']}，两腿名义已在容忍度内，按缩小后的仓位继续持有。", pair_id, "high")
    else:
        notify.push(ctx.settings, f"配对已开仓：{key}", f"空 {rich} 多 {cheap}，净仓 {p['net']}", priority="high")
    return {"opened": True, "balanced": True, "pair_id": pair_id}


# ------------------------------------------------------------------ 平仓
def _close_pair(ctx: Ctx, pair_id: str, reason: str, st: dict) -> bool:
    """按账本里记的数量把两腿都 reduce-only 平掉。返回是否完全平掉。"""
    p = st["pairs"][pair_id]
    key = p["open"]["pair_key"]
    if not p["close_req"]:
        ctx.led.append("close_req", pair_id, {"pair_id": pair_id, "reason": reason, "t_ms": ctx.now_ms})
    for coin, q in sorted(p["net"].items(), key=lambda kv: -abs(kv[1])):
        if abs(q) > EPS:
            _unwind(ctx, pair_id, coin, q, "exit")
    p2 = load_state(ctx.settings)["pairs"][pair_id]
    if all(abs(q) < EPS for q in p2["net"].values()):
        pnl = pair_pnl(p2, {})
        ctx.led.append("closed", pair_id, {"pair_id": pair_id, "reason": reason, "pnl_usd": round(pnl, 4), "t_ms": ctx.now_ms})
        notify.push(ctx.settings, f"配对已平仓：{key}", f"原因 {reason}，这一笔估算盈亏 ${pnl:+.2f}（含手续费估算）", priority="high")
        return True
    _alert(ctx, f"配对平仓未完成，仍有仓位：{key}", f"原因 {reason}。剩余 {p2['net']}。每分钟会重试；也可以手动平这两条腿（只平这个数量）。", pair_id)
    return False


# ------------------------------------------------------------------ 一次评估
def step(settings, now: datetime, client=None, data: dict | None = None) -> dict:
    """调度器每分钟调一次。off → 什么都不做。shadow → 只记账和推送。live → 风控 → 平仓 → 开仓。"""
    mode_cfg = get(settings)
    mode = mode_cfg["mode"]
    out: dict = {"mode": mode, "entries": [], "exits": [], "plans": [], "skips": []}
    if mode == "off":
        return out
    cf = cfg(settings)
    now_ms = int(now.timestamp() * 1000)
    if data is None:
        try:
            data = load_data(settings, now_ms, cf)
        except Exception as e:  # noqa: BLE001
            log.warning("pairs data: %s", e)
            out["error"] = f"数据不可用：{e}"
            data = {"pairs": [], "candles": {}, "funding": {}}
    c = client
    if c is None:
        try:
            c = _make_client(settings, sorted({_dex_of(p[k]) for p in data["pairs"] for k in ("a", "b")}))
        except Exception as e:  # noqa: BLE001
            if mode == "live":
                notify.push(settings, "配对自动下单无法连接交易所", str(e)[:300], priority="high")
                out["error"] = str(e)[:300]
                return out
            c = None   # shadow 允许没有客户端（用 K 线收盘价估）
    ctx = Ctx(settings, c, cf, now_ms, mode)
    if mode == "shadow":
        return _step_shadow(ctx, data, out)
    st = load_state(settings)
    if st["unresolved"] and not mode_cfg.get("halt_entries"):
        put(settings, halt_entries=True)
        _alert(ctx, "配对有订单结果不明", f"这些 cloid 发出后没有成交记录：{st['unresolved'][:5]}。已停止开新仓，请核对交易所。")
        mode_cfg = get(settings)
    pk = {_pair_key(p): p for p in data["pairs"]}
    iv_ms = bl.INTERVAL_MS[cf["interval"]]
    # ---- 1) 已有的配对：清空仓位的收尾、两腿失衡、止损、超时、价差收敛
    marks_cache: dict[str, float | None] = {}

    def mark(coin):
        if coin not in marks_cache:
            marks_cache[coin] = _mid(_book(ctx, coin))
        return marks_cache[coin]

    for pid, p in list(open_pairs(st).items()):
        o = p["open"]
        if all(abs(q) < EPS for q in p["net"].values()) and now_ms - o["t_ms"] > 5 * 60_000:
            ctx.led.append("closed", pid, {"pair_id": pid, "reason": "no_position", "pnl_usd": round(-p["fees"], 4), "t_ms": now_ms})
            continue
        reason = p["close_req"]["reason"] if p["close_req"] else None
        marks = {coin: mark(coin) for coin in p["net"]}
        pnl = pair_pnl(p, marks)
        if not reason and pnl is not None and pnl <= -cf["max_pair_loss_usd"]:
            reason = "per_pair_loss"
        if not reason and now_ms - o["t_ms"] >= cf["max_hold_h"] * 3_600_000:
            reason = "max_hold"
        if not reason:
            nts = {c_: abs(q) * (marks.get(c_) or p["last_px"].get(c_, 0)) for c_, q in p["net"].items()}
            if len(nts) == 2 and abs(nts[o["rich_short"]] - nts[o["cheap_long"]]) > 3 * cf["net_tolerance_frac"] * max(nts.values()):
                reason = "imbalance"   # 两腿市值差 > 3×容忍度（价格漂移或某一腿没平干净）→ 整对平掉
        if not reason and o["pair_key"] in pk:
            sg_ = _series(pk[o["pair_key"]], data, iv_ms, now_ms)
            if sg_ and sg_[-1][0] >= now_ms - 3 * iv_ms:
                s_now = pl.spread_series(sg_, "same", iv_ms)[-1]
                if _exit_due(o["exit_mode"], o["sg"], o["s0"], s_now):
                    reason = f"exit_{o['exit_mode']}"
        if reason:
            done = _close_pair(ctx, pid, reason, st)
            out["exits"].append({"pair_id": pid, "reason": reason, "closed": done})
    st = load_state(settings)
    # ---- 2) 当日亏损止损：今天已平仓的 + 还开着的未实现 ≤ −上限 → 全平并切回 off
    day0 = _day_start_ms(now_ms)
    realized = sum(p["closed"].get("pnl_usd", 0.0) for p in st["pairs"].values() if p["closed"] and p["closed"].get("t_ms", 0) >= day0)
    unreal = 0.0
    for pid, p in open_pairs(st).items():
        v = pair_pnl(p, {coin: mark(coin) for coin in p["net"]})
        unreal += v if v is not None else 0.0
    out["day_pnl_usd"] = realized + unreal
    if realized + unreal <= -cf["daily_loss_stop_usd"]:
        left = 0
        for pid in open_pairs(st):
            if not _close_pair(ctx, pid, "daily_loss_stop", load_state(settings)):
                left += 1
        if left == 0:
            put(settings, mode="off")
        else:
            put(settings, halt_entries=True)
        notify.push(settings, "配对自动下单：当日亏损止损触发" + ("，已全部平仓并切回 off" if not left else "，仍有仓位没平掉！"),
                    f"今日合计 ${realized + unreal:.2f}，上限 ${cf['daily_loss_stop_usd']:.0f}", priority="max")
        out["daily_stop"] = True
        return out
    # ---- 3) 开仓
    if get(settings).get("halt_entries"):
        out["skips"].append("halt_entries")
        return out
    for key, pair in pk.items():
        st = load_state(settings)
        sig = signal(pair, data, cf, now_ms)
        bar_t = sig.get("bar_t", 0)
        if not sig["ok"]:
            out["skips"].append((key, sig["why"]))
            continue
        if abs(sig["spread"]) <= sig["thr"]:
            continue
        reason = None
        live = open_pairs(st)
        last = [p["open"] for p in st["pairs"].values() if p["open"]["pair_key"] == key]
        if any(p["open"]["pair_key"] == key for p in live.values()):
            reason = "这一对已有仓位"
        elif last and (last[-1]["bar_t"] == bar_t or now_ms - last[-1]["t_ms"] < cf["cooldown_min"] * 60_000):
            reason = "冷却中 / 同一根 K 线已入场过"
        elif len(live) >= cf["max_concurrent"]:
            reason = f"同时持有的配对已达上限 {cf['max_concurrent']}"
        if reason:
            _note_skip(ctx, key, bar_t, reason)
            out["skips"].append((key, reason))
            continue
        res = _open_pair(ctx, pair, sig, st)
        if res.get("skipped"):
            _note_skip(ctx, key, bar_t, res["skipped"])
            out["skips"].append((key, res["skipped"]))
        else:
            out["entries"].append(res)
    return out


def _step_shadow(ctx: Ctx, data: dict, out: dict) -> dict:
    """演习：算信号和名义，记账 + 推送，不发任何单（不调用 ioc / set_leverage）。"""
    cf, led = ctx.cf, ctx.led
    for pair in data["pairs"]:
        key = _pair_key(pair)
        sig = signal(pair, data, cf, ctx.now_ms)
        if not sig["ok"] or abs(sig["spread"]) <= sig["thr"]:
            continue
        if any(r["kind"] == "plan" and r.get("pair_key") == key and r.get("bar_t") == sig["bar_t"] for r in led.all()):
            continue
        sg = 1 if sig["spread"] > 0 else -1
        rich, cheap = (pair["a"], pair["b"]) if sg > 0 else (pair["b"], pair["a"])
        bk = {c: _book(ctx, c) if ctx.c else None for c in (rich, cheap)}
        if all(bk.values()):
            n_depth = cf["depth_fraction"] * min(bk[rich]["bid_depth_usd"], bk[cheap]["ask_depth_usd"])
        else:
            n_depth = float("inf")   # 没有盘口（没钱包）：深度上限未知
        notional = min(cf["max_leg_notional_usd"], n_depth, cf["max_total_gross_usd"] / 2)
        if notional < MIN_ORDER_USD:
            continue
        plan = {"pair_key": key, "bar_t": sig["bar_t"], "rich_short": rich, "cheap_long": cheap, "notional_usd": round(notional, 2),
                "spread_bps": round(sig["spread"], 1), "thr_bps": round(sig["thr"], 1), "variant": sig["variant"],
                "exit_mode": sig["exit_mode"], "max_hold_h": cf["max_hold_h"], "sent": False}
        led.append("plan", key, plan)
        notify.push(ctx.settings, f"配对演习：{key} 本来会开仓",
                    f"空 {rich}、多 {cheap}，每腿约 ${notional:.0f}，价差 {sig['spread']:+.0f} bps（门槛 {sig['thr']:.0f}，变体 {sig['variant']}）。"
                    "演习模式，没有真下单。")
        out["plans"].append(plan)
    return out
