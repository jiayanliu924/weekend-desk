"""跨 dex 同底层配对真下单（真钱，HIGH 风险）。信号逻辑全部来自 desk/pairs_lab.py（import，不复制数学）。

交易：同一只股票在两个 HIP-3 dex 上各有一个永续（例如 io:SNDK / xyz:SNDK）。价差 s = (A/B − 1)·1e4 bps 超过
"走前选出的门槛"→ 做空贵的那条腿、做多便宜的那条腿，**等名义**；价差按该变体的出场规则收敛，或持仓满 max_hold_h → 平掉。

三种模式（存在 data/state/pairs_live.json，和 autotrade 的 live.json 完全分开；默认 off）：
- off：不开新仓。**但只要还有没平的配对，调度器照常每分钟跑 step() 管理平仓 / 止损**（off 不会让仓位变孤儿）。
- shadow（演习）：算好"本来会下什么"，写进 pairs 账本并推送，不给交易所发任何单。
- live：真下单。

安全设计（每一条都有测试）：
1. 每张单都带 cloid，标签以 "pairs-" 开头，写进 pairs 账本（先写"已发出"，再写成交）。交易所要求 cloid 是 16 字节十六进制，
   所以发给交易所的是标签的 sha256 前 128 位；标签 ↔ 十六进制的对应也写在账本里。
2. 平仓只用 reduce-only IOC，数量 = 我们自己账本里记的成交量；**绝不**调用 market_close(coin)。
3. 两腿原子性：两腿都是带滑点上限的 IOC 限价；缺腿 / 部分成交 → 立刻 reduce-only 回补；任何时刻发现某对不足两条非零腿（含两腿之间崩溃）→ 当作失衡立即平。
4. 对账：每个 step 先拿账本净仓位和交易所持仓比。有"结果不明"的单 → 绝不自动当作 no_position 收尾；尽量按交易所观察到的数量认领，认不了就保持打开、停开新仓、每小时（限频）告警。
5. 同一个币同时只允许一对在场；开仓前交易所该币持仓必须为 0（有手动仓位 / 其他配对就不开）；改杠杆回包必须 ok。
6. 估值：盘口买卖价差太宽时不用中间价（用 mark，再退回最近成交价并标记 stale）；盈亏含资金费（userFunding）。
7. 一个 step 用 flock 串行化；抓数据有超时，不会卡死调度循环，也不会妨碍已有仓位的平仓 / 止损。
8. 开仓守卫（只拦新开仓，平仓 / 止损永远照跑）：休市禁开（周五 19:30 → 周日 20:35 纽约时间）、腿不活跃、币被下架 / 暂停。
9. 反向止损 wrong_way：同向价差扩大到入场价差的 wrong_way_mult 倍，或绝对价差 ≥ max_abs_spread_bps → 平仓并锁 24h。
10. 每个 step 把"为什么开 / 没开"写进 data/state/pairs_last_decision.json；python -m desk.pairs_exec --status 用中文打印。
清除"停开新仓"标志：python -m desk.pairs_exec --clear-halt
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
MIN_ORDER_MARGIN = 1.05     # 向下取整到 szDecimals 之后每腿仍要 ≥ 最小下单价值 × 这个系数
EPS = 1e-9
DAY_MS = pl.DAY_MS
HOUR_MS = 3_600_000

DEFAULTS = {
    "interval": "5m", "lookback_days": 17, "warmup_days": 5, "allow": [],
    "max_leg_notional_usd": 100.0, "max_concurrent": 1, "max_total_gross_usd": 200.0,
    "max_pair_loss_usd": 4.0, "daily_loss_stop_usd": 8.0, "leverage": 2, "depth_fraction": 0.10,
    "max_hold_h": 24.0, "slippage_bps": 30.0, "exit_slippage_bps": 60.0, "net_tolerance_frac": 0.05,
    "unwind_retries": 3, "min_balance_usd": 5.0, "fee_bps": 9.0, "cooldown_min": 30,
    "max_mark_spread_bps": 100.0,     # 盘口买卖价差超过这个就不信中间价
    "settled_after_min": 5.0,         # 交易所持仓为 0 而账本非 0 持续多久 → 按已了结处理
    "data_timeout_s": 45.0,           # 抓 K 线 / 发现数据的最长等待
    "lock_h": 24.0,                   # 止损 / 超时出场后锁定该配对多久
    "alert_every_min": 60.0,          # 同类告警的最短间隔
    "repoll_tries": 3,                # 下单回包异常后，重查成交 / 持仓的次数
    "repoll_s": 2.0,                  # 每次重查之间等多久
    "data_reuse_s": 600.0,            # 超时后才完成的抓数据结果，多久之内还能用
    "blackout_start": "FRI 19:30",    # 休市禁开新仓：起点（含）。设成 "" 关闭
    "blackout_end": "SUN 20:35",      # 终点（不含）
    "blackout_tz": "America/New_York",
    "wrong_way_mult": 2.0,            # 同向价差 ≥ 入场价差 × 这个倍数 → 反向止损
    "max_abs_spread_bps": 300.0,      # 绝对价差 ≥ 这个 → 无条件平
    "stale_bars": 6,                  # 腿不活跃检查看最近几根 5m K 线
    "stale_min_zero": 3,              # 其中零成交的根数 ≥ 这个 → 不开
    "stale_move_bps": 10.0,           # 一条腿收盘价没动、另一条动了超过这个 → 不开
}
_sleep = time.sleep                   # 测试里替换


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
    old = get(settings)["mode"]
    d = get(settings)
    d.update(kw)
    d["ts"] = time.time()
    _path(settings).write_text(json.dumps(d, ensure_ascii=False))
    if "mode" in kw and kw["mode"] != old:
        try:
            st = load_state(settings)
            ops = open_pairs(st)
        except Exception:  # noqa: BLE001
            ops = {}
        if ops:
            lines = [f"{p['open']['pair_key']}：{ {c: round(q, 4) for c, q in p['net'].items() if abs(q) > EPS} }" for p in ops.values()]
            notify.push(settings, f"配对模式 {old} → {kw['mode']}，但还有 {len(ops)} 对没平",
                        "；".join(lines) + "。模式切换不会平仓：调度器会继续管理平仓 / 止损直到全部平掉。", priority="max")
    return d


def cfg(settings) -> dict:
    d = dict(DEFAULTS)
    d.update((settings.raw.get("pairs") if hasattr(settings, "raw") else settings.get("pairs", {})) or {})
    d["leverage"] = int(max(1, min(int(float(d["leverage"])), LEV_HARD_CAP)))
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
    """在 autotrade.Client 上加：多 dex、盘口、mark、资金费、按 cloid 查成交、IOC 限价（可 reduce-only、带 cloid）。
    真下单只发生在 ioc() 和 set_leverage()。"""

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

    def mark_px(self, coin: str) -> float | None:
        dex = _dex_of(coin)
        mc = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex}) if dex else self.info.meta_and_asset_ctxs()
        names = [u["name"] for u in mc[0]["universe"]]
        return float(mc[1][names.index(coin)]["markPx"])

    def funding_rows(self, since_ms: int) -> list[dict]:
        out = []
        for r in self.info.user_funding_history(self.address, int(since_ms)) or []:
            d = r.get("delta", {})
            out.append({"coin": d.get("coin"), "usdc": float(d.get("usdc", 0) or 0), "szi": float(d.get("szi", 0) or 0),
                        "time": int(r.get("time", 0))})
        return out

    def fill_for_cloid(self, coin: str, cloid_hex_: str, since_ms: int):
        """→ (成交总量, 均价) 或 None（查不到 / 没成交）。"""
        sz = notional = 0.0
        for f in self.info.user_fills(self.address):
            if f.get("coin") == coin and str(f.get("cloid", "")).lower() == cloid_hex_.lower():
                q = float(f["sz"])
                sz += q
                notional += q * float(f["px"])
        return (sz, notional / sz) if sz > 0 else None

    def set_leverage(self, coin: str, lev: int):
        r = self.ex.update_leverage(int(lev), coin, is_cross=False)   # 真下单路径之一（改杠杆，不成交）
        if not isinstance(r, dict) or r.get("status") != "ok":
            raise RuntimeError(f"改杠杆失败 {coin}: {str(r)[:150]}")
        return r

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
_DEXSET: set = set()      # 只增不减：客户端按它的排序结果缓存，dex 集合变了才重建（每次变化只建一次）


def _make_client(settings, dexs: list[str]):
    if autotrade._client_factory:
        return autotrade._client_factory()
    a, k = autotrade.creds()
    if not (autotrade.valid_address(a) and autotrade.valid_key(k)):
        raise RuntimeError("还没有在设置里填 Hyperliquid 钱包地址和 API 私钥")
    _DEXSET.update(d for d in dexs if d)
    key = (a, tuple(sorted(_DEXSET)))
    if _CLIENT.get("key") != key:          # 每分钟调用一次，别每次都重建（重建会打一堆 meta 请求）
        _CLIENT.update(key=key, c=PairsClient(a, k, list(key[1])))
    return _CLIENT["c"]


# ------------------------------------------------------------------ 账本 → 当前持仓（唯一真相来源 = 我们自己的成交记录）
def _slug(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_")


def load_state(settings) -> dict:
    pairs: dict[str, dict] = {}
    orders: dict[str, dict] = {}
    pending: dict[str, str] = {}
    unknown: dict[str, str] = {}
    locks: dict[str, dict] = {}
    for i, r in enumerate(_led(settings).all()):
        k, pid = r["kind"], r.get("pair_id")
        if k == "open":
            pairs[pid] = {"open": r, "net": {}, "cash": {}, "last_px": {}, "fees": 0.0, "closed": None, "close_req": None,
                          "unknown": [], "fills": [], "peak": {}, "mismatch": {}, "est_px": False}
        elif k == "order":
            orders[r["cloid"]] = {**r, "idx": i}
            pending[r["cloid"]] = pid
        elif k == "fill":
            pending.pop(r["cloid"], None)
            if r.get("resolves"):
                unknown.pop(r["resolves"], None)
            if r.get("unknown"):
                unknown[r["cloid"]] = pid
            elif pid in pairs:
                p, coin = pairs[pid], r["coin"]
                p["net"][coin] = round(p["net"].get(coin, 0.0) + r["signed_sz"], 10)
                p["cash"][coin] = p["cash"].get(coin, 0.0) - r["signed_sz"] * (r.get("px") or 0.0)
                p["fees"] += r.get("fee_usd", 0.0) or 0.0
                p["peak"][coin] = max(p["peak"].get(coin, 0.0), abs(p["net"][coin]))
                p["fills"].append({"idx": i, "coin": coin, "signed_sz": r["signed_sz"]})
                p["mismatch"].pop(coin, None)
                if r.get("px"):
                    p["last_px"][coin] = r["px"]
                if r.get("px_est"):
                    p["est_px"] = True
        elif k == "mismatch" and pid in pairs:
            pairs[pid]["mismatch"].setdefault(r["coin"], r["t_ms"])
        elif k == "mismatch_clear" and pid in pairs:
            pairs[pid]["mismatch"].pop(r["coin"], None)
        elif k == "close_req" and pid in pairs:
            pairs[pid]["close_req"] = r
        elif k == "closed" and pid in pairs:
            pairs[pid]["closed"] = r
        elif k == "lock":
            locks[r["pair_key"]] = r
        elif k == "lock_clear":
            locks.pop(r["pair_key"], None)
    unres = {**pending, **unknown}
    for cl, pid in unres.items():
        if pid in pairs:
            pairs[pid]["unknown"].append(cl)
    return {"pairs": pairs, "orders": orders, "unresolved": sorted(unres), "locks": locks}


def open_pairs(st: dict) -> dict[str, dict]:
    return {pid: p for pid, p in st["pairs"].items() if not p["closed"]}


def has_open(settings) -> bool:
    return bool(open_pairs(load_state(settings)))


def _pair_coins(o: dict) -> tuple[str, str]:
    return o["rich_short"], o["cheap_long"]


def open_dexs(st: dict) -> set[str]:
    out = set()
    for p in open_pairs(st).values():
        for c in (*_pair_coins(p["open"]), *p["net"]):
            if _dex_of(c):
                out.add(_dex_of(c))
    return out


def pair_pnl(p: dict, marks: dict[str, float | None], funding: float = 0.0) -> tuple[float, list[str]]:
    """已实现现金流 − 估算手续费 + 资金费 + 剩余净仓位估值。价格缺失 → 退回最近成交价并列入 stale。→ (盈亏, stale 币列表)。"""
    tot, stale = -p["fees"] + funding, []
    for coin in set(p["cash"]) | set(p["net"]):
        tot += p["cash"].get(coin, 0.0)
        net = p["net"].get(coin, 0.0)
        if abs(net) > EPS:
            m = marks.get(coin)
            if m is None:
                m = p["last_px"].get(coin, 0.0)
                stale.append(coin)
            tot += net * m
    return tot, stale


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
        self._marks: dict[str, float | None] = {}
        self._frows: list | None = None

    def mark(self, coin: str, fallback: float | None = None) -> tuple[float | None, bool]:
        """→ (价格, 是否 stale)。盘口买卖价差太宽 → 不用中间价，改用交易所 mark，再不行退回最近成交价（stale）。"""
        if coin not in self._marks:
            px_ = None
            b = _book(self, coin)
            if b and (b["ask"] - b["bid"]) / ((b["ask"] + b["bid"]) / 2) * 1e4 <= self.cf["max_mark_spread_bps"]:
                px_ = _mid(b)
            if px_ is None and self.c is not None and getattr(self.c, "mark_px", None):
                try:
                    px_ = self.c.mark_px(coin)
                except Exception:  # noqa: BLE001
                    px_ = None
            self._marks[coin] = px_
        v = self._marks[coin]
        return (v, False) if v else (fallback, True)

    def funding(self, p: dict) -> float:
        """这一对自开仓以来的资金费（美元，付出为负）。按我们的峰值仓位占交易所持仓的比例分摊，避免把手动仓位的资金费算进来。"""
        if self._frows is None:
            fn = getattr(self.c, "funding_rows", None) if self.c is not None else None
            try:
                self._frows = fn(self.now_ms - 3 * DAY_MS) if fn else []
            except Exception:  # noqa: BLE001
                self._frows = []
        o, tot = p["open"], 0.0
        for r in self._frows:
            if r["coin"] in p["peak"] and r["time"] >= o["t_ms"]:
                szi = abs(r.get("szi") or 0.0)
                tot += r["usdc"] * (1.0 if szi < EPS else min(1.0, p["peak"][r["coin"]] / szi))
        return tot

    def pair_pnl(self, p: dict) -> tuple[float, list[str]]:
        marks = {}
        for c, q in p["net"].items():
            if abs(q) > EPS:
                v, stale = self.mark(c, None)
                marks[c] = None if stale else v      # None → pair_pnl 退回最近成交价并列入 stale
        return pair_pnl(p, marks, self.funding(p))


def _floor(q: float, dec: int) -> float:
    m = 10 ** dec
    return math.floor(q * m + 1e-9) / m


def _ceil(q: float, dec: int) -> float:
    m = 10 ** dec
    return math.ceil(q * m - 1e-9) / m


def _alert(ctx: Ctx, title: str, body: str, pair_id: str = "-", priority: str = "max"):
    ctx.led.append("alert", pair_id, {"pair_id": pair_id, "title": title, "body": body[:500]})
    notify.push(ctx.settings, title, body[:500], priority=priority)


def _rate_ok(ctx: Ctx, key: str, every_ms: float | None = None) -> bool:
    every = ctx.cf["alert_every_min"] * 60_000 if every_ms is None else every_ms
    ts = dict(get(ctx.settings).get("alert_ts") or {})
    if ctx.now_ms - ts.get(key, -1e18) < every:
        return False
    ts[key] = ctx.now_ms
    put(ctx.settings, alert_ts=ts)
    return True


def _alert_limited(ctx: Ctx, key: str, title: str, body: str, pair_id: str = "-", every_ms: float | None = None):
    if _rate_ok(ctx, key, every_ms):
        _alert(ctx, title, body, pair_id)


def _halt(ctx: Ctx, why: str):
    put(ctx.settings, halt_entries=True, halt_reason=why[:200], halt_alert_ts=ctx.now_ms)


def _note_skip(ctx: Ctx, key: str, bar_t: int, reason: str):
    """同一对、同一根 K 线、同一个原因只记一次，别每分钟刷账本。"""
    for r in reversed(ctx.led.all()):
        if r["kind"] == "skip" and r.get("pair_key") == key:
            if r.get("bar_t") == bar_t and r.get("reason") == reason:
                return
            break
    ctx.led.append("skip", key, {"pair_key": key, "bar_t": bar_t, "reason": reason})


def _book(ctx: Ctx, coin: str) -> dict | None:
    if ctx.c is None:
        return None
    try:
        b = ctx.c.book(coin)
        return b if b and b.get("bid") and b.get("ask") else None
    except Exception:  # noqa: BLE001
        return None


def _mid(b: dict | None) -> float | None:
    return (b["bid"] + b["ask"]) / 2 if b else None


# ------------------------------------------------------------------ 下单（所有真单都经过 _send）
def _send(ctx: Ctx, pair_id: str, coin: str, is_buy: bool, sz: float, px: float, reduce_only: bool, purpose: str) -> float:
    """发一张 IOC 限价单，返回实际成交数量（≥0）。先写"已发出"（含发单前的交易所持仓）再写"成交"。
    异常时：优先按 cloid 查真实成交；查不到但持仓前后确实没变 → 没成交；否则记为"结果不明"（交给对账认领），绝不把限价当成交价。"""
    n = sum(1 for r in ctx.led.all() if r["kind"] == "order" and r.get("pair_id") == pair_id)
    cloid = f"{PREFIX}{pair_id}-{n + 1}"
    try:
        before = float(ctx.c.position(coin))
    except Exception:  # noqa: BLE001
        before = None
    ctx.led.append("order", pair_id, {"pair_id": pair_id, "coin": coin, "cloid": cloid, "cloid_hex": cloid_hex(cloid),
                                      "is_buy": is_buy, "sz": sz, "px": px, "reduce_only": reduce_only, "purpose": purpose,
                                      "pos_before": before, "t_ms": ctx.now_ms})
    sgn = 1.0 if is_buy else -1.0
    fee = ctx.cf["fee_bps"] / 1e4
    try:
        r = ctx.c.ioc(coin, is_buy, sz, px, reduce_only, cloid)
    except Exception as e:  # noqa: BLE001
        log.exception("order exception")
        got, after = None, None
        for attempt in range(max(1, int(ctx.cf["repoll_tries"]))):
            if attempt:
                _sleep(float(ctx.cf["repoll_s"]))
            fn = getattr(ctx.c, "fill_for_cloid", None)
            if fn:
                try:
                    got = fn(coin, cloid_hex(cloid), ctx.now_ms - HOUR_MS)
                except Exception:  # noqa: BLE001
                    got = None
            if got and got[0] and got[1]:
                break
            try:
                after = float(ctx.c.position(coin))
            except Exception:  # noqa: BLE001
                after = None
            if before is not None and after is not None and abs(after - before) > EPS:
                break          # 持仓变了但查不到成交价：不再等，交给对账认领
        if got and got[0] and got[1]:
            f = min(float(got[0]), sz)
            ctx.led.append("fill", pair_id, {"pair_id": pair_id, "coin": coin, "cloid": cloid, "signed_sz": sgn * f, "px": float(got[1]),
                                             "purpose": purpose, "fee_usd": fee * f * float(got[1]), "error": f"异常后按 cloid 查到真实成交：{e}"[:200]})
            return f
        # 持仓没变 + 查不到成交 ≠ 确认没成交（回包丢了、成交可能延迟可见）：一律记为"结果不明"，停开新仓，交给对账
        ctx.led.append("fill", pair_id, {"pair_id": pair_id, "coin": coin, "cloid": cloid, "signed_sz": 0.0, "unknown": True,
                                         "observed_delta": None if before is None or after is None else after - before,
                                         "error": str(e)[:200]})
        _halt(ctx, f"订单结果不明 {cloid}")
        raise UnknownOrderState(f"{coin} {cloid}: {e}") from e
    f = min(max(0.0, float(r.get("filled_sz") or 0.0)), sz)
    avg = r.get("avg_px")
    est = f > 0 and not avg
    avg = float(avg or px)
    ctx.led.append("fill", pair_id, {"pair_id": pair_id, "coin": coin, "cloid": cloid, "signed_sz": sgn * f,
                                     "px": avg if f else None, "px_est": True if est else None, "purpose": purpose,
                                     "fee_usd": fee * f * avg, "error": r.get("error")})
    return f


def _unwind(ctx: Ctx, pair_id: str, coin: str, remaining: float, purpose: str) -> float:
    """把"我们自己"在 coin 上的 remaining（带符号：+ = 我们多、− = 我们空）用 reduce-only IOC 平掉；返回没平掉的剩余。
    数量永远不超过 |remaining|；手动仓位与我们同向时只减我们那一份，反向时 reduce-only 无法安全执行 → 不动并告警（限频）。"""
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
                    _alert_limited(ctx, f"conflict-{pair_id}-{coin}", f"配对平仓冲突：{coin}",
                                   f"交易所净仓位 {net} 与我们要平的方向 {'卖' if sell else '买'} {abs(remaining)} 冲突"
                                   "（手动仓位反向，或交易所已无仓位），reduce-only 无法安全执行，没有下单。请核对。", pair_id)
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
        return {**out, "ok": False, "no_variant": True, "why": "走前今天没有选中变体（历史累计净收益 ≤ 0）"}
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
    data = {"pairs": pairs, "candles": candles, "funding": funding, "halted": halted_coins(raw),
            "funding_t": {c: (time.time(), r) for c, r in funding.items()}}
    _DATA.update(key=key, data=data, settings=str(settings.data))
    return data



_FUT: dict = {}
_EXEC = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pairs-data")


def _run_load(settings, now_ms, cf):
    d = load_data(settings, now_ms, cf)
    if isinstance(d, dict):
        _FUT["res"] = (time.time(), d)
    return d


def _fresh_result(cf):
    r = _FUT.get("res")
    return r[1] if r and time.time() - r[0] < float(cf["data_reuse_s"]) else None


def load_data_limited(settings, now_ms: int, cf: dict) -> dict:
    """抓数据放在后台线程，最多等 data_timeout_s。上一次超时的抓取如果后来完成了（且不到 data_reuse_s），直接用它的结果；
    还没完成就不再提交新的（不堆线程），有可用的旧结果就用旧的，否则报错。已有仓位的管理不受影响。"""
    f = _FUT.get("f")
    if f is not None and _FUT.get("orphan"):
        if f.done():
            _FUT["orphan"] = False
            d = _fresh_result(cf)
            if d is not None and f.exception() is None:
                return d
        else:
            d = _fresh_result(cf)
            if d is not None:
                return d
            raise TimeoutError("上一次数据抓取还没结束")
    f = _EXEC.submit(_run_load, settings, now_ms, cf)
    _FUT["f"] = f
    try:
        return f.result(timeout=float(cf["data_timeout_s"]))
    except concurrent.futures.TimeoutError as e:
        _FUT["orphan"] = True
        d = _fresh_result(cf)
        if d is not None:
            return d
        raise TimeoutError(f"抓数据超过 {cf['data_timeout_s']}s") from e



# ------------------------------------------------------------------ 开仓守卫（只拦新开仓；平仓 / 止损不受影响）
_DOW = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
HALT_FLAGS = ("isDelisted", "halted", "isHalted", "tradingHalted")   # universe 原始数据里可能出现的下架 / 暂停字段


def _mow(spec: str) -> int:
    """"FRI 19:30" → 一周内的第几分钟（周一 00:00 = 0）。"""
    d, hm = spec.split()
    h, m = hm.split(":")
    return _DOW[d.upper()[:3]] * 1440 + int(h) * 60 + int(m)


def in_blackout(cf: dict, now_ms: int) -> bool:
    """休市禁开窗口（本地墙上时间，自动跟随夏令时）：[blackout_start, blackout_end)。时区读不到 → 保守当作禁开。"""
    if not str(cf.get("blackout_start") or "").strip():
        return False
    try:
        loc = datetime.fromtimestamp(now_ms / 1000, timezone.utc).astimezone(ZoneInfo(cf["blackout_tz"]))
        a, b = _mow(cf["blackout_start"]), _mow(cf["blackout_end"])
    except Exception as e:  # noqa: BLE001
        log.warning("blackout config/tz unusable (%s) → 保守禁开", e)
        return True
    m = loc.weekday() * 1440 + loc.hour * 60 + loc.minute
    return a <= m < b if a <= b else (m >= a or m < b)


def halted_coins(raw: dict | None) -> set[str]:
    """从发现的原始 meta 里找下架 / 暂停的币。字段缺失 → 空集（该检查自动跳过）。"""
    out: set[str] = set()
    mcs = list((raw or {}).get("dexs", {}).values()) + [(raw or {}).get("main")]
    for mc in mcs:
        if not mc or not mc[0]:
            continue
        for u in mc[0].get("universe", []) or []:
            if isinstance(u, dict) and any(u.get(f) for f in HALT_FLAGS):
                out.add(u.get("name"))
    return out


def stale_leg(pair: dict, data: dict, cf: dict, now_ms: int) -> str | None:
    """最近 stale_bars 根已收盘 K 线（按时间格，缺的格子算零成交）：任一腿零成交 ≥ stale_min_zero 根，
    或这条腿收盘价全没动而另一条腿动了 > stale_move_bps → 返回描述，否则 None。用的就是信号已经载入的 K 线。"""
    iv_ms = bl.INTERVAL_MS[cf["interval"]]
    n = int(cf["stale_bars"])
    cm = {}
    for coin in (pair["a"], pair["b"]):
        cm[coin] = {int(c["t"]): c for c in data["candles"].get(coin, []) if int(c["t"]) + iv_ms <= now_ms}
    last = max((max(m) for m in cm.values() if m), default=None)
    if last is None or n < 1:
        return None
    slots = [last - k * iv_ms for k in range(n - 1, -1, -1)]
    closes, zeros = {}, {}
    for coin, m in cm.items():
        cs = [m.get(t) for t in slots]
        zeros[coin] = sum(1 for c in cs if c is None or float(c.get("v") or 0) <= 0)
        closes[coin] = [float(c["c"]) for c in cs if c is not None]
    for coin, other in ((pair["a"], pair["b"]), (pair["b"], pair["a"])):
        if zeros[coin] >= int(cf["stale_min_zero"]):
            return f"{coin} 最近 {n} 根里 {zeros[coin]} 根零成交"
        cc, oc = closes[coin], closes[other]
        if len(cc) >= 2 and len(set(cc)) == 1 and len(oc) >= 2 and min(oc) > 0 and (max(oc) - min(oc)) / min(oc) * 1e4 > cf["stale_move_bps"]:
            return f"{coin} 收盘价 {n} 根没动而 {other} 在动"
    return None


# ------------------------------------------------------------------ 开仓
def _locked(st: dict, key: str, now_ms: int) -> bool:
    lk = st["locks"].get(key)
    return bool(lk and now_ms < lk["until_ms"])


def _write_lock(ctx: Ctx, key: str, reason: str):
    ctx.led.append("lock", key, {"pair_key": key, "reason": reason, "t_ms": ctx.now_ms, "until_ms": ctx.now_ms + int(ctx.cf["lock_h"] * HOUR_MS)})


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
    if max(abs(s_live), abs(sig["spread"])) >= float(cf["max_abs_spread_bps"]):
        return {"skipped": "价差异常大，像是报价坏了"}
    # 入场价差取实时盘口价差与信号 K 线价差里绝对值大的那个（同号），避免反向止损拿"偏小的入场价"误触发
    sig = {**sig, "s_entry": s_live if abs(s_live) >= abs(sig["spread"]) else sig["spread"]}
    plan = {"pair": key, "rich_short": rich, "cheap_long": cheap, "notional_usd": round(notional, 2), "spread_bps": round(s_live, 1),
            "thr_bps": round(sig["thr"], 1), "variant": sig["variant"], "bar_t": sig["bar_t"]}
    if ctx.mode == "shadow":
        return {"plan": plan}
    pair_id = None
    try:
        r = _live_open(ctx, pair, sig, st, key, sg, rich, cheap, bk, notional)
        pair_id = r.get("pair_id")
        return r
    except Exception as e:  # noqa: BLE001  任何意外：按账本里的成交把已开的这一对平掉，不留单腿
        log.exception("entry crashed")
        _halt(ctx, f"开仓流程异常 {type(e).__name__}")
        st2 = load_state(ctx.settings)
        mine = [pid for pid, p in open_pairs(st2).items() if p["open"]["pair_key"] == key and p["open"]["t_ms"] == now_ms]
        _alert(ctx, f"配对开仓流程出错：{key}", f"{type(e).__name__}: {e}"[:300], mine[0] if mine else "-")
        for pid in mine:
            _close_pair(ctx, pid, "entry_exception", st2)
        return {"error": "entry_exception", "pair_id": mine[0] if mine else None}


def _live_open(ctx: Ctx, pair: dict, sig: dict, st: dict, key: str, sg: int, rich: str, cheap: str, bk: dict, notional: float) -> dict:
    cf, now_ms = ctx.cf, ctx.now_ms
    # 一个币同一时间只允许一对；交易所上该币必须是 0 仓位（手动仓位 / 其他配对都不碰）
    taken = {c for p in open_pairs(st).values() for c in (*_pair_coins(p["open"]), *p["net"])}
    if taken & {rich, cheap}:
        return {"skipped": f"{sorted(taken & {rich, cheap})} 已被另一对占用（一个币只允许一对）"}
    for c in (rich, cheap):
        try:
            pos = float(ctx.c.position(c))
        except Exception as e:  # noqa: BLE001
            return {"skipped": f"读不到 {c} 的持仓（{type(e).__name__}），不开仓"}
        if abs(pos) > EPS:
            return {"skipped": f"交易所上 {c} 已有仓位 {pos}（手动或其他来源），不开仓、不改杠杆"}
    pf = autotrade._live_preflight(ctx.settings, cf, ctx.c, check_wallet=ctx.real)
    if pf:
        notify.push(ctx.settings, f"配对下单未通过安全检查：{key}", pf, priority="high")
        return {"skipped": pf}
    try:
        avail = ctx.c.balance().get("available")
    except Exception as e:  # noqa: BLE001
        return {"skipped": f"读不到余额（{type(e).__name__}），不开仓"}
    if avail is None:
        return {"skipped": "余额回包里没有 available，不开仓"}
    lev = int(cf["leverage"])
    if 2 * notional / lev > float(avail) * 0.9:
        notional = float(avail) * 0.9 * lev / 2
        if notional < MIN_ORDER_USD:
            return {"skipped": f"保证金不够（可用 ${float(avail):.2f}）"}
    dec = {c: ctx.c.sz_decimals(c) for c in (rich, cheap)}
    legs = {rich: {"buy": False, "px_ref": bk[rich]["bid"], "depth": bk[rich]["bid_depth_usd"]},
            cheap: {"buy": True, "px_ref": bk[cheap]["ask"], "depth": bk[cheap]["ask_depth_usd"]}}
    for c in (rich, cheap):   # 向下取整到 szDecimals 之后，每腿仍要明显高于最小下单价值
        v = _floor(notional / legs[c]["px_ref"], dec[c]) * legs[c]["px_ref"]
        if v < MIN_ORDER_USD * MIN_ORDER_MARGIN:
            return {"skipped": f"{c} 取整后名义 ${v:.2f} < 最小下单 ${MIN_ORDER_USD * MIN_ORDER_MARGIN:.2f}"}
    for c in (rich, cheap):
        try:
            r = ctx.c.set_leverage(c, lev)
        except Exception as e:  # noqa: BLE001
            return {"skipped": f"设置 {c} 杠杆失败（{type(e).__name__}: {str(e)[:100]}），不开仓"}
        if isinstance(r, dict) and r.get("status") not in (None, "ok"):
            return {"skipped": f"设置 {c} 杠杆回包不是 ok：{str(r)[:100]}，不开仓"}
    n_open = sum(1 for r in ctx.led.all() if r["kind"] == "open")
    pair_id = f"{_slug(key)}-{now_ms}-{n_open + 1}"
    ctx.led.append("open", pair_id, {"pair_id": pair_id, "pair_key": key, "a": pair["a"], "b": pair["b"], "sg": sg, "s0": sig["spread"], "s_entry": sig.get("s_entry", sig["spread"]),
                                     "thr": sig["thr"], "variant": sig["variant"], "exit_mode": sig["exit_mode"],
                                     "t_ms": now_ms, "bar_t": sig["bar_t"], "notional_target": notional,
                                     "rich_short": rich, "cheap_long": cheap, "lev": lev})
    first = min((rich, cheap), key=lambda c: legs[c]["depth"])   # 先下较薄的一腿；平手时先下贵的那条（空腿）
    second = cheap if first == rich else rich
    slip = cf["slippage_bps"] / 1e4

    def limit(c):
        return autotrade.round_px(legs[c]["px_ref"] * (1 + slip) if legs[c]["buy"] else legs[c]["px_ref"] * (1 - slip), dec[c])

    try:
        sz1 = _floor(notional / legs[first]["px_ref"], dec[first])
        f1 = _send(ctx, pair_id, first, legs[first]["buy"], sz1, limit(first), False, "entry")
        if f1 <= 0:
            ctx.led.append("closed", pair_id, {"pair_id": pair_id, "reason": "entry_failed:first_leg", "pnl_usd": 0.0, "t_ms": now_ms})
            return {"skipped": "第一腿没成交，另一腿没下", "pair_id": pair_id}
        n1 = f1 * load_state(ctx.settings)["pairs"][pair_id]["last_px"].get(first, legs[first]["px_ref"])
        sz2 = _floor(n1 / legs[second]["px_ref"], dec[second])
        if sz2 > 0:
            _send(ctx, pair_id, second, legs[second]["buy"], sz2, limit(second), False, "entry")
    except UnknownOrderState as e:
        _alert(ctx, f"配对下单状态未知，已停止开新仓：{key}", f"{e}。下一分钟起会按交易所持仓对账认领；请核对 {rich} / {cheap} 的持仓和挂单。", pair_id)
        return {"error": "unknown_order_state", "pair_id": pair_id}
    return _balance_legs(ctx, pair_id, rich, cheap, notional, dec)


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
        excess_sz = min(abs(p["net"].get(big, 0.0)), _ceil(abs(gap) / px[big], dec[big]))   # 回补整个差额（向上取整到一手）；另一腿没成交 = 全部回补
        left = _unwind(ctx, pair_id, big, excess_sz if p["net"].get(big, 0.0) > 0 else -excess_sz, "unwind_excess")
        if abs(left) > EPS:
            break
    p = load_state(ctx.settings)["pairs"][pair_id]
    flat = all(abs(q) < EPS for q in p["net"].values())
    if flat:
        pnl, _ = ctx.pair_pnl(p)
        ctx.led.append("closed", pair_id, {"pair_id": pair_id, "reason": "entry_failed:unwound", "pnl_usd": round(pnl, 4), "t_ms": ctx.now_ms})
        _alert(ctx, f"配对开仓失败，已回补成零仓位：{key}", f"一条腿没成交 / 成交不足，已用 reduce-only 全部回补。估算盈亏 ${pnl:+.2f}（含手续费估算）。", pair_id, "high")
        return {"opened": False, "unwound": True, "pair_id": pair_id}
    if not ok:
        _halt(ctx, "两腿失衡且回补失败")
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
    if all(abs(q) < EPS for q in p2["net"].values()) and not p2["unknown"]:
        pnl, _ = ctx.pair_pnl(p2)
        ctx.led.append("closed", pair_id, {"pair_id": pair_id, "reason": reason, "pnl_usd": round(pnl, 4), "t_ms": ctx.now_ms})
        notify.push(ctx.settings, f"配对已平仓：{key}", f"原因 {reason}，这一笔估算盈亏 ${pnl:+.2f}（含手续费与资金费估算）", priority="high")
        return True
    _alert_limited(ctx, f"closefail-{pair_id}", f"配对平仓未完成，仍有仓位：{key}",
                   f"原因 {reason}。剩余 {p2['net']}。每分钟会重试；也可以手动平这两条腿（只平这个数量）。", pair_id)
    return False


# ------------------------------------------------------------------ 对账 + 管理已有的配对
def _reconcile(ctx: Ctx, pid: str, out: dict) -> bool:
    """账本净仓位 vs 交易所持仓（交易所持仓可能含手动仓位，所以只做"不可能"的判断，不做相等判断）。
    返回 False = 这一对还有解决不了的"结果不明"，调用方不得对它做任何自动收尾。"""
    st = load_state(ctx.settings)
    p = st["pairs"][pid]
    o = p["open"]
    coins = sorted({*_pair_coins(o), *p["net"]})
    E: dict[str, float | None] = {}
    for coin in coins:
        try:
            E[coin] = float(ctx.c.position(coin))
        except Exception:  # noqa: BLE001
            E[coin] = None
    if p["unknown"]:
        for cloid in list(p["unknown"]):
            od = st["orders"].get(cloid)
            if not od:
                continue
            coin = od["coin"]
            same = [u for u in p["unknown"] if st["orders"].get(u, {}).get("coin") == coin]
            if len(same) != 1 or E.get(coin) is None or od.get("pos_before") is None:
                continue          # 同一个币上有多张不明单 / 读不到持仓：认领不了
            later = sum(f["signed_sz"] for f in p["fills"] if f["coin"] == coin and f["idx"] > od["idx"])
            delta = E[coin] - od["pos_before"] - later
            sgn = 1.0 if od["is_buy"] else -1.0
            if delta * sgn < -1e-9 or abs(delta) > od["sz"] + 1e-6:
                continue          # 方向不对 / 超过这张单的数量：可能有手动单混进来，不认领
            ref, _ = ctx.mark(coin, od["px"])
            ctx.led.append("fill", pid, {"pair_id": pid, "coin": coin, "cloid": cloid + "-adopt", "resolves": cloid, "signed_sz": delta,
                                         "px": ref or od["px"], "px_est": True, "purpose": "adopt",
                                         "fee_usd": ctx.cf["fee_bps"] / 1e4 * abs(delta) * (ref or od["px"])})
            _alert(ctx, f"配对对账：已按交易所持仓认领不明订单 {cloid}", f"{coin} 认领 {delta:+.6g}（价格是估算）。", pid, "high")
        st = load_state(ctx.settings)
        p = st["pairs"][pid]
        if p["unknown"]:
            _halt(ctx, f"{pid} 有结果不明的订单")
            _alert_limited(ctx, f"unknown-{pid}", f"配对有结果不明的订单，保持打开：{o['pair_key']}",
                           f"未解决 cloid：{p['unknown'][:5]}。账本净仓 {p['net']}，交易所 {E}。已停开新仓，不会自动收尾；请核对后手动处理。", pid)
            return False
    for coin in coins:     # 交易所持仓超过"开仓前基线 + 账本净仓位"，且最近 1 小时我们在这个币上发过单 → 告警 + 停开新仓
        e = E[coin]
        mine = [od for od in st["orders"].values() if od["coin"] == coin and od.get("pair_id") == pid]
        recent = [od for od in st["orders"].values() if od["coin"] == coin and ctx.now_ms - (od.get("t_ms") or 0) <= HOUR_MS]
        if e is None or not mine or not recent or mine[0].get("pos_before") is None:
            continue
        excess = e - (mine[0]["pos_before"] + p["net"].get(coin, 0.0))
        if abs(excess) > max(1e-6, 0.01 * p["peak"].get(coin, 0.0)):
            _halt(ctx, f"{coin} 交易所持仓超出账本解释")
            _alert_limited(ctx, f"excess-{pid}-{coin}", f"配对对账：{coin} 交易所持仓超出账本能解释的部分",
                           f"交易所 {e:+.6g}，开仓前基线 {mine[0]['pos_before']:+.6g} + 账本 {p['net'].get(coin, 0.0):+.6g}，多出 {excess:+.6g}"
                           "（手动单？还是我们有没记上的成交？）。已停开新仓，请核对。", pid)
    for coin in coins:
        L, e = p["net"].get(coin, 0.0), E[coin]
        if e is None or abs(L) < EPS:
            continue
        if abs(e) < EPS:
            t0 = p["mismatch"].get(coin)
            if t0 is None:
                ctx.led.append("mismatch", pid, {"pair_id": pid, "coin": coin, "t_ms": ctx.now_ms, "ledger": L, "exchange": e})
            elif ctx.now_ms - t0 >= ctx.cf["settled_after_min"] * 60_000:
                px_, _ = ctx.mark(coin, p["last_px"].get(coin))
                px_ = px_ or p["last_px"].get(coin, 0.0)
                ctx.led.append("fill", pid, {"pair_id": pid, "coin": coin, "cloid": f"{PREFIX}{pid}-settle-{coin}", "signed_sz": -L, "px": px_,
                                             "px_est": True, "purpose": "reconcile_settled", "fee_usd": 0.0})
                _alert(ctx, f"配对对账：{coin} 交易所已无仓位，账本按已了结处理", f"账本 {L:+.6g} → 0（强平 / 手动平 / 对账差异），按 {px_} 估价记账，请核对。", pid)
                st = load_state(ctx.settings)
                p = st["pairs"][pid]
        else:
            if coin in p["mismatch"]:
                ctx.led.append("mismatch_clear", pid, {"pair_id": pid, "coin": coin, "t_ms": ctx.now_ms})
            if e * L < 0 or abs(e) < abs(L) - 1e-6:
                _alert_limited(ctx, f"diff-{pid}-{coin}", f"配对对账：{coin} 交易所持仓与账本不符",
                               f"账本 {L:+.6g}，交易所 {e:+.6g}（含手动仓位则正常）。平仓只会 reduce-only、只动我们那一份。", pid)
    return True


def _manage(ctx: Ctx, out: dict):
    """阶段一（不需要行情数据）：逐对管理。一对出错（异常）不影响其他对，也不影响后面的当日止损；出错告警（限频）。"""
    for pid in list(open_pairs(load_state(ctx.settings))):
        try:
            _manage_one(ctx, pid, out)
        except Exception as e:  # noqa: BLE001
            log.exception("manage %s failed", pid)
            _alert_limited(ctx, f"manage-err-{pid}", f"配对管理出错（其他配对不受影响）：{pid}", f"{type(e).__name__}: {e}"[:300], pid)


def _manage_one(ctx: Ctx, pid: str, out: dict):
    cf, now_ms = ctx.cf, ctx.now_ms
    if True:
        if not _reconcile(ctx, pid, out):
            return
        p = load_state(ctx.settings)["pairs"][pid]
        o = p["open"]
        legs = [c for c in _pair_coins(o) if abs(p["net"].get(c, 0.0)) > EPS]
        reason = p["close_req"]["reason"] if p["close_req"] else None
        if not reason and not legs:
            pnl, _ = ctx.pair_pnl(p)
            ctx.led.append("closed", pid, {"pair_id": pid, "reason": "no_position", "pnl_usd": round(pnl, 4), "t_ms": now_ms})
            return
        pnl, stale = ctx.pair_pnl(p)
        if stale:
            _alert_limited(ctx, f"stale-{pid}", f"配对估值用了最近成交价（行情不可用 / 盘口太宽）：{o['pair_key']}",
                           f"{stale} 没有可靠的 mark，按最近成交价计入盈亏（stale）。", pid)
        if not reason and len(legs) < 2:
            reason = "imbalance"      # 单腿（含两腿之间崩溃）：立刻平
        if not reason and pnl <= -cf["max_pair_loss_usd"]:
            reason = "per_pair_loss"
        if not reason and now_ms - o["t_ms"] >= cf["max_hold_h"] * HOUR_MS:
            reason = "max_hold"
        if not reason:
            nts = {c: abs(p["net"].get(c, 0.0)) * (ctx.mark(c, p["last_px"].get(c))[0] or 0.0) for c in _pair_coins(o)}
            if abs(nts[o["rich_short"]] - nts[o["cheap_long"]]) > 3 * cf["net_tolerance_frac"] * max(nts.values()):
                reason = "imbalance"   # 两腿市值差 > 3×容忍度（价格漂移或某一腿没平干净）→ 整对平掉
        if reason:
            done = _close_pair(ctx, pid, reason, load_state(ctx.settings))
            out["exits"].append({"pair_id": pid, "reason": reason, "closed": done})
            if reason in ("per_pair_loss", "max_hold"):
                _write_lock(ctx, o["pair_key"], reason)


def wrong_way(o: dict, s: float | None, cf: dict) -> bool:
    """价差往不利方向走：与入场同号且 |s| ≥ wrong_way_mult × |s_entry|；或 |s| ≥ max_abs_spread_bps（不论方向）。"""
    if s is None:
        return False
    if abs(s) >= float(cf["max_abs_spread_bps"]):
        return True
    se = o.get("s_entry", o.get("s0"))
    return bool(se) and s * se > 0 and abs(s) >= float(cf["wrong_way_mult"]) * abs(se)


def _live_spread(ctx: Ctx, o: dict) -> float | None:
    """实时价差（bps）。用 ctx.mark：盘口太宽时不用中间价；任一腿拿不到可靠价格 → None（不据此止损）。"""
    pa, sa = ctx.mark(o["a"], None)
    pb, sb = ctx.mark(o["b"], None)
    if not pa or not pb or sa or sb:
        return None
    return (pa / pb - 1) * 1e4


def _spread_exits(ctx: Ctx, data: dict, out: dict):
    """阶段二（需要 K 线）：币被下架 / 暂停 → 平；反向止损 wrong_way（K 线价差或实时盘口价差任一触发）→ 平并锁定；价差按该变体的出场规则收敛。"""
    iv_ms = bl.INTERVAL_MS[ctx.cf["interval"]]
    pk = {_pair_key(p): p for p in data["pairs"]}
    halted = set(data.get("halted") or ())
    for pid, p in open_pairs(load_state(ctx.settings)).items():
        o = p["open"]
        if p["unknown"] or p["close_req"]:
            continue
        if halted & {o["a"], o["b"], *p["net"]}:
            done = _close_pair(ctx, pid, "halted", load_state(ctx.settings))
            out["exits"].append({"pair_id": pid, "reason": "halted", "closed": done})
            continue
        s_bar = None
        if o["pair_key"] in pk:
            sg_ = _series(pk[o["pair_key"]], data, iv_ms, ctx.now_ms)
            # 只看开仓之后才收盘的 K 线（序列时刻 = 收盘时刻）：开仓时的信号 K 线是旧的，不能拿来跟实时入场价差比
            if sg_ and sg_[-1][0] >= ctx.now_ms - 3 * iv_ms and sg_[-1][0] > o["t_ms"]:
                s_bar = pl.spread_series(sg_, "same", iv_ms)[-1]
        if wrong_way(o, s_bar, ctx.cf) or wrong_way(o, _live_spread(ctx, o), ctx.cf):
            done = _close_pair(ctx, pid, "wrong_way", load_state(ctx.settings))
            out["exits"].append({"pair_id": pid, "reason": "wrong_way", "closed": done})
            _write_lock(ctx, o["pair_key"], "wrong_way")
            continue
        if s_bar is not None and _exit_due(o["exit_mode"], o["sg"], o["s0"], s_bar):
            reason = f"exit_{o['exit_mode']}"
            done = _close_pair(ctx, pid, reason, load_state(ctx.settings))
            out["exits"].append({"pair_id": pid, "reason": reason, "closed": done})


def _daily_stop(ctx: Ctx, out: dict) -> bool:
    """当日亏损止损：今天已平仓的 + 还开着的（含资金费、stale 估值）≤ −上限 → 全平并切回 off。"""
    st = load_state(ctx.settings)
    day0 = _day_start_ms(ctx.now_ms)
    realized = sum(p["closed"].get("pnl_usd", 0.0) for p in st["pairs"].values() if p["closed"] and p["closed"].get("t_ms", 0) >= day0)
    unreal = sum(ctx.pair_pnl(p)[0] for p in open_pairs(st).values())
    out["day_pnl_usd"] = realized + unreal
    if realized + unreal > -ctx.cf["daily_loss_stop_usd"]:
        return False
    left = 0
    for pid in open_pairs(st):
        if not _close_pair(ctx, pid, "daily_loss_stop", load_state(ctx.settings)):
            left += 1
    if left == 0:
        put(ctx.settings, mode="off")
    else:
        _halt(ctx, "当日亏损止损，仍有仓位没平掉")
    if left == 0 or _rate_ok(ctx, "dailystop"):      # 还没平干净时每小时最多一次
        notify.push(ctx.settings, "配对自动下单：当日亏损止损触发" + ("，已全部平仓并切回 off" if not left else "，仍有仓位没平掉！"),
                    f"今日合计 ${realized + unreal:.2f}，上限 ${ctx.cf['daily_loss_stop_usd']:.0f}", priority="max")
    out["daily_stop"] = True
    return True


# ------------------------------------------------------------------ 一次评估
def step(settings, now: datetime, client=None, data: dict | None = None) -> dict:
    """调度器每分钟调一次。flock 串行化：上一个 step 还没结束就直接跳过。
    off → 没有未平配对就什么都不做；有就只管理平仓 / 止损。shadow → 只记账和推送。live → 对账 → 平仓 / 止损 → 开仓。"""
    lp = _path(settings).with_name("pairs_step.lock")
    with open(lp, "a+") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"mode": get(settings)["mode"], "entries": [], "exits": [], "plans": [], "skips": [], "locked": True}
        try:
            return _step(settings, now, client, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _step(settings, now: datetime, client, data) -> dict:
    mode_cfg = get(settings)
    mode = mode_cfg["mode"]
    out: dict = {"mode": mode, "entries": [], "exits": [], "plans": [], "skips": []}
    st = load_state(settings)
    has_pos = bool(open_pairs(st))
    if mode == "off" and not has_pos:
        return out
    cf = cfg(settings)
    now_ms = int(now.timestamp() * 1000)
    c = client
    if c is None and has_pos:      # 有未平仓位才需要先连交易所；没有仓位的话等拿到发现的 dex 之后一次建好
        try:
            c = _make_client(settings, sorted(open_dexs(st)))
        except Exception as e:  # noqa: BLE001
            notify.push(settings, "配对自动下单无法连接交易所（还有未平仓位！）", str(e)[:300], priority="max")
            out["error"] = str(e)[:300]
            return out
    ctx = Ctx(settings, c, cf, now_ms, mode)
    # ---- 阶段一：已有仓位的管理，先于任何行情数据；live 模式下当日亏损止损无条件在任何开仓之前检查
    if has_pos:
        _manage(ctx, out)
    if (has_pos or mode == "live") and _daily_stop(ctx, out):
        return out
    # ---- 抓数据（有超时；失败不影响上面的管理）
    if data is None:
        try:
            data = load_data_limited(settings, now_ms, cf)
        except Exception as e:  # noqa: BLE001
            log.warning("pairs data: %s", e)
            out["error"] = f"数据不可用：{e}"
            return out
    if has_pos:
        _spread_exits(ctx, data, out)
        if _daily_stop(ctx, out):
            return out
    if mode == "off":
        return out
    # 客户端的 dex = 已有仓位 ∪ 发现到的（shadow 和 live 用同一个集合；集合不变就复用缓存的客户端）
    if client is None:
        try:
            ctx.c = _make_client(settings, sorted(open_dexs(load_state(settings)) | {_dex_of(p[k]) for p in data["pairs"] for k in ("a", "b")}))
        except Exception as e:  # noqa: BLE001
            if mode == "live":
                notify.push(settings, "配对自动下单无法连接交易所", str(e)[:300], priority="high")
                out["error"] = str(e)[:300]
                return out
            ctx.c = None      # shadow 允许没有客户端
    if mode == "shadow":
        return _step_shadow(ctx, data, out)
    st = load_state(settings)
    from . import account_stop
    if account_stop.check(settings).get("tripped"):      # 总账户止损：已开仓位上面已照管，这里只是不开新仓
        out["skips"].append("account_stop")
        _write_decisions(ctx, out, _blanket_rows(ctx, data, "总账户止损已触发，停开新仓"))
        return out
    h = get(settings)
    if h.get("halt_entries"):
        if now_ms - float(h.get("halt_alert_ts", 0) or 0) >= DAY_MS:
            notify.push(settings, "配对自动下单仍处于\"停开新仓\"状态",
                        f"原因：{h.get('halt_reason', '?')}。核对交易所持仓后，运行 python -m desk.pairs_exec --clear-halt 清除。", priority="high")
            put(settings, halt_alert_ts=now_ms)
        out["skips"].append("halt_entries")
        _write_decisions(ctx, out, _blanket_rows(ctx, data, f"停开新仓标志（{h.get('halt_reason') or '?'}）"))
        return out
    pk = {_pair_key(p): p for p in data["pairs"]}
    rows: list[dict] = []
    black, halted = in_blackout(cf, now_ms), set(data.get("halted") or ())
    for key, pair in pk.items():
        st = load_state(settings)
        sig = signal(pair, data, cf, now_ms)
        bar_t = sig.get("bar_t", 0)
        if not sig["ok"]:
            out["skips"].append((key, sig["why"]))
            rows.append(_row(key, sig, "走前判断今天不做" if sig.get("no_variant") else f"数据不可用：{sig['why']}"))
            continue
        if abs(sig["spread"]) <= sig["thr"]:
            if key in st["locks"]:        # 价差已回到门槛以内 → 解除锁定
                ctx.led.append("lock_clear", key, {"pair_key": key, "t_ms": now_ms})
            rows.append(_row(key, sig, f"价差 {abs(sig['spread']):.0f} < 门槛 {sig['thr']:.0f}"))
            continue
        reason = None
        short = None
        live = open_pairs(st)
        last = [p["open"] for p in st["pairs"].values() if p["open"]["pair_key"] == key]
        if _locked(st, key, now_ms):
            reason, short = "止损 / 超时出场后锁定中（24h 或价差回到门槛以内才解除）", "止损锁定中"
        elif any(p["open"]["pair_key"] == key for p in live.values()):
            reason = short = "这一对已有仓位"
        elif last and (last[-1]["bar_t"] == bar_t or now_ms - last[-1]["t_ms"] < cf["cooldown_min"] * 60_000):
            reason = "冷却中 / 同一根 K 线已入场过"
            short = reason
        elif len(live) >= cf["max_concurrent"]:
            reason = short = f"同时持有的配对已达上限 {cf['max_concurrent']}"
        if reason:
            _note_skip(ctx, key, bar_t, reason)
            out["skips"].append((key, reason))
            rows.append(_row(key, sig, "已开仓" if reason == "这一对已有仓位" else short))
            continue
        block = _entry_block(pair, data, cf, now_ms, black, halted)
        if block:
            out["skips"].append((key, block))
            rows.append(_row(key, sig, block))
            continue
        res = _open_pair(ctx, pair, sig, st)
        if res.get("skipped"):
            _note_skip(ctx, key, bar_t, res["skipped"])
            out["skips"].append((key, res["skipped"]))
            rows.append(_row(key, sig, res["skipped"] if res["skipped"].startswith("价差异常大") else f"没开：{res['skipped']}"))
        else:
            out["entries"].append(res)
            ok = bool(res.get("opened"))
            rows.append(_row(key, sig, "已开仓" if ok else f"开仓未成功：{res.get('error') or '一条腿没成交，已回补'}", entered=ok))
        if get(settings).get("halt_entries"):
            break
    _write_decisions(ctx, out, rows)
    return out


def _entry_block(pair: dict, data: dict, cf: dict, now_ms: int, black: bool, halted: set) -> str | None:
    """只拦新开仓的守卫，→ 中文原因或 None。"""
    if black:
        return "休市禁开"
    bad = sorted(halted & {pair["a"], pair["b"]})
    if bad:
        return f"{'、'.join(bad)} 已下架 / 暂停"
    st_ = stale_leg(pair, data, cf, now_ms)
    if st_:
        return f"腿不活跃（{st_}）"
    return None


# ------------------------------------------------------------------ 决策透明度
def _row(key: str, sig: dict, reason: str, entered: bool = False) -> dict:
    r = lambda x: None if x is None else round(float(x), 1)  # noqa: E731
    return {"key": key, "spread_bps": r(sig.get("spread")), "thr_bps": r(sig.get("thr")), "variant": sig.get("variant"),
            "entered": entered, "reason": reason}


def _blanket_rows(ctx: Ctx, data: dict, reason: str) -> list[dict]:
    """整步都不开仓时的决策表：只写原因，不调用 signal()（它可能慢 / 出错，而这里不需要）。"""
    return [_row(_pair_key(pair), {}, reason) for pair in data["pairs"]]


def decision_path(settings) -> Path:
    p = Path(settings.data) / "state" / "pairs_last_decision.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_decisions(ctx: Ctx, out: dict, rows: list[dict]):
    """每个 live / shadow step 写一份决策表 + 一行 INFO 汇总。任何失败都不影响交易。"""
    try:
        n_in = sum(1 for r in rows if r["entered"])
        ratios = [(abs(r["spread_bps"]) / r["thr_bps"], r) for r in rows if r["spread_bps"] is not None and r["thr_bps"]]
        best = max(ratios, key=lambda x: x[0])[1] if ratios else None
        log.info("pairs step[%s]: 检查 %d 对，最大 |价差|/门槛 = %s，开仓 %d 笔", ctx.mode, len(rows),
                 f"{abs(best['spread_bps']):.0f}/{best['thr_bps']:.0f} bps（{best['key']}）" if best else "无", n_in)
        doc = {"ts": datetime.fromtimestamp(ctx.now_ms / 1000, timezone.utc).isoformat(), "ts_ms": ctx.now_ms, "mode": ctx.mode, "pairs": rows}
        decision_path(ctx.settings).write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    except Exception as e:  # noqa: BLE001
        log.warning("decision file: %s", e)


def decision_lines(settings) -> list[str]:
    """--status 用：一对一行，纯中文。"""
    try:
        doc = json.loads(decision_path(settings).read_text())
    except (OSError, ValueError):
        return ["还没有决策记录（live / shadow 模式跑过一个 step 之后才有）"]
    t0 = datetime.fromtimestamp(doc["ts_ms"] / 1000, timezone.utc)
    try:
        ts, zn = t0.astimezone(ZoneInfo("America/New_York")).strftime("%m-%d %H:%M"), "纽约时间"
    except Exception:  # noqa: BLE001
        ts, zn = t0.strftime("%m-%d %H:%M"), "UTC"
    lines = [f"最近一次决策（{ts} {zn}，模式 {doc.get('mode')}）："]
    for r in doc.get("pairs", []):
        sp = "无" if r["spread_bps"] is None else f"{r['spread_bps']:+.0f} bps"
        th = "无" if r["thr_bps"] is None else f"{r['thr_bps']:.0f} bps"
        lines.append(f"  {r['key']}：价差 {sp}，门槛 {th}，方案 {r['variant'] or '无'} → {'开了' if r['entered'] else '没开'}：{r['reason']}")
    if not doc.get("pairs"):
        lines.append("  （没有可检查的配对）")
    return lines


def _step_shadow(ctx: Ctx, data: dict, out: dict) -> dict:
    """演习：算信号和名义，记账 + 推送，不发任何单（不调用 ioc / set_leverage）。守卫（休市 / 下架 / 腿不活跃）同样生效。"""
    cf, led = ctx.cf, ctx.led
    rows: list[dict] = []
    black, halted = in_blackout(cf, ctx.now_ms), set(data.get("halted") or ())
    for pair in data["pairs"]:
        key = _pair_key(pair)
        sig = signal(pair, data, cf, ctx.now_ms)
        if not sig["ok"]:
            rows.append(_row(key, sig, "走前判断今天不做" if sig.get("no_variant") else f"数据不可用：{sig['why']}"))
            continue
        if abs(sig["spread"]) <= sig["thr"]:
            rows.append(_row(key, sig, f"价差 {abs(sig['spread']):.0f} < 门槛 {sig['thr']:.0f}"))
            continue
        if any(r["kind"] == "plan" and r.get("pair_key") == key and r.get("bar_t") == sig["bar_t"] for r in led.all()):
            rows.append(_row(key, sig, "本根 K 线已演习记录过"))
            continue
        block = _entry_block(pair, data, cf, ctx.now_ms, black, halted)
        if block:
            rows.append(_row(key, sig, block))
            continue
        if abs(sig["spread"]) >= float(cf["max_abs_spread_bps"]):
            rows.append(_row(key, sig, "价差异常大，像是报价坏了"))
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
            rows.append(_row(key, sig, f"可下名义 ${notional:.0f} < 交易所最小 ${MIN_ORDER_USD:.0f}"))
            continue
        plan = {"pair_key": key, "bar_t": sig["bar_t"], "rich_short": rich, "cheap_long": cheap, "notional_usd": round(notional, 2),
                "spread_bps": round(sig["spread"], 1), "thr_bps": round(sig["thr"], 1), "variant": sig["variant"],
                "exit_mode": sig["exit_mode"], "max_hold_h": cf["max_hold_h"], "sent": False}
        led.append("plan", key, plan)
        notify.push(ctx.settings, f"配对演习：{key} 本来会开仓",
                    f"空 {rich}、多 {cheap}，每腿约 ${notional:.0f}，价差 {sig['spread']:+.0f} bps（门槛 {sig['thr']:.0f}，变体 {sig['variant']}）。"
                    "演习模式，没有真下单。")
        out["plans"].append(plan)
        rows.append(_row(key, sig, "演习开仓（没有真下单）", entered=True))
    _write_decisions(ctx, out, rows)
    return out


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description="配对自动下单的管理命令")
    ap.add_argument("--clear-halt", action="store_true", help="清除停开新仓标志（先核对交易所持仓和账本）")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--mode", choices=MODES, help="切换配对下单模式：off / shadow（演习）/ live（真下单）")
    a = ap.parse_args(argv)
    s = config.load()
    if a.mode:
        put(s, mode=a.mode, updated_by="cli")
        print(f"配对下单模式已切到 {a.mode}")
    if a.clear_halt:
        put(s, halt_entries=False, halt_reason="")
        print("已清除 halt_entries")
    if a.status or not (a.clear_halt or a.mode):
        st = load_state(s)
        print(json.dumps({**get(s), "open_pairs": {k: {"net": v["net"], "unknown": v["unknown"]} for k, v in open_pairs(st).items()}},
                         ensure_ascii=False, indent=1))
        print("\n".join(decision_lines(s)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
