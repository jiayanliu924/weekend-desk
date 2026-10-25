"""重开 forced-flow 真钱执行器（真钱，HIGH 风险）。信号 / 事件时间全部来自 desk/reopen_study.py（import，不复制）。

机制（和研究一致）：每个重开事件（日线 18:00 ET 周一到周四；周日重开：美股 20:05 ET，SP500/XYZ100/BRENTOIL 18:00 ET），
T0 = 价格重新开始更新的那一刻（reopen_study.detect_t0）。S = sign(G)，G = 现价 oracle / T0 前 5 秒 oracle − 1。
当 S·(mid/oracle − 1)·1e4 > k_bps（重开后冲过头）→ 在 −S 方向、本方最优价挂**一张** post-only (ALO) 限价单；T0+120s 没成交就撤。
出场：成交后立刻在当时的 oracle 价挂 post-only 限价平仓；10 分钟没成交 → reduce-only IOC（mid ± 滑点上限）；硬时间止损 T0+15 分钟。

和研究**不完全相同**的地方（实盘拿不到未来信息）：
1. 研究的 ref = T0+120s 的 oracle（事后才知道）。实盘用"决策那一刻的当前 oracle"当 ref，G 和过冲都以它为准；S 每次轮询重算。
2. 只有 detect_t0 判定为"真的跳变"（inferred=True）才交易；研究里 inferred=False（取名义时间后第一个样本）的事件，实盘不碰。
3. 样本来源：开始前用 reopen_study.read_ctx 读 rawstore 里的 hl_ctx 做预热（含 T0 前 10 分钟，detect_t0 需要），之后每 ~1 秒轮询 REST
   的 metaAndAssetCtxs，只有 (oracle, mid, oi) 变化才追加一行（模仿 ws 的 activeAssetCtx 每次更新一行）。
4. 研究用"成交流穿过挂价"模拟成交；实盘是真成交。部分成交：一有成交就撤掉剩余挂单，按已成交量走出场。
5. 出场 ALO 若会吃单被拒（oracle 已越过盘口）→ 直接 reduce-only IOC（价格更好）。

代码硬风控（配置只能更严，不能放宽；agent 不能覆盖）：每币最多 1 仓、同时最多 3 仓、单笔名义封顶 $1000（默认 $500）、杠杆固定 1x 逐仓、
当日亏损止损 ≤ $15（已实现 + 未实现，ET 自然日）、累计亏损止损 ≤ $50（触发写 halt_entries 标志，需人工 --clear-halt）、余额下限、
halt 标志 / 总账户止损 / 币被下架暂停 / 数据超过 10 秒没更新 → 不开新仓。手续费按满额计（maker 3 bps、taker 9 bps，不算返佣）。

开关 data/state/reopen_live.json：{"mode":"off"|"live"}，默认 off。off = 干跑：只写决策日志，不发任何单。**这不是影子策略**，只是 user 打开前的安全默认值。
但 off 时仍会管理已经存在的仓位（平仓 / 止损）。
CLI：python -m desk.reopen_exec --status | --set-live | --set-off | --clear-halt | --once
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import autotrade, notify
from . import reopen_study as rs
from .ledger import Ledger

log = logging.getLogger("reopen_exec")
_MISSING_WARNED: set = set()
ET = ZoneInfo("America/New_York")
PREFIX = "reopen-"
MODES = ("off", "live")
NS = rs.NS
EPS = 1e-9
MIN_ORDER_USD = 10.0
# ---- 代码硬上限（配置再宽松也按这些）
NOTIONAL_HARD_CAP = 1000.0
CONCURRENT_HARD_CAP = 3
PER_COIN_MAX = 1
DAILY_STOP_CAP = 15.0
CUM_STOP_CAP = 50.0
STALE_CAP_S = 10.0
ENTRY_WINDOW_CAP_S = 120.0
HARD_STOP_CAP_S = 900.0
EXIT_WAIT_CAP_S = 600.0
DEPTH_FRAC_CAP = 0.10
LEVERAGE = 1
# 限速：metaAndAssetCtxs 权重 20，Hyperliquid 每 IP 1200 权重/分钟。1 秒轮询 = 1200/分钟（单这一项就打满，
# 还和 pairs_exec / collector 共用 IP）。下限 2 秒（≤600/分钟），默认 3 秒（≈400/分钟 + 管理单 ~50/分钟），留一半余量。
POLL_FLOOR_S = 2.0
BACKOFF_MAX_S = 30.0


def is_rate_limited(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 429 or "429" in str(e)[:200]


def pnl_snapshot_path(settings) -> Path:
    p = Path(settings.data) / "state" / "reopen_pnl.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

DEFAULTS = {
    "coins": ["SP500", "XYZ100", "BRENTOIL"],   # 研究事件表里的名字；其他币等判定后再加
    "k_bps": 10.0, "notional_usd": 500.0, "max_concurrent": 3,
    "daily_loss_stop_usd": 15.0, "cum_loss_stop_usd": 50.0, "min_balance_usd": 5.0,
    "poll_s": 3.0, "pre_s": 120.0, "t0_wait_s": 600.0, "entry_window_s": 120.0,
    "exit_wait_s": 600.0, "hard_stop_s": 900.0, "stale_s": 10.0,
    "exit_slippage_bps": 50.0, "max_book_spread_bps": 100.0, "depth_fraction": 0.10,
    "asia_coins": {"kr": [], "jp": [], "hk": [], "cn": []},   # 亚洲股票重开：{kr/jp/hk/cn: [币名]}，空列表 = 不盯
    "notify": False,            # 手机推送默认关（Kea 不要）；记录和日志照写
    "fee_maker_bps": 3.0, "fee_taker_bps": 9.0, "ioc_attempts": 12,
}


# ------------------------------------------------------------------ 开关 / 配置
def _path(settings) -> Path:
    p = Path(settings.data) / "state" / "reopen_live.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get(settings) -> dict:
    d = {"mode": "off", "halt_entries": False, "halt_reason": "", "updated_by": "", "ts": 0}
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
    p = _path(settings)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    tmp.replace(p)
    return d


def cfg(settings) -> dict:
    d = dict(DEFAULTS)
    raw = settings.raw if hasattr(settings, "raw") else settings
    d.update(raw.get("reopen") or {})
    d["coins"] = [str(c) for c in (d.get("coins") or [])]
    ac = d.get("asia_coins") if isinstance(d.get("asia_coins"), dict) else {}
    d["asia_coins"] = {m: [str(c) for c in (ac.get(m) or [])] for m in rs.ASIA}
    f = lambda k: float(d[k])  # noqa: E731
    d["k_bps"] = max(0.0, f("k_bps"))
    d["notional_usd"] = max(0.0, min(f("notional_usd"), NOTIONAL_HARD_CAP))
    d["max_concurrent"] = max(0, min(int(f("max_concurrent")), CONCURRENT_HARD_CAP))
    d["daily_loss_stop_usd"] = max(0.0, min(f("daily_loss_stop_usd"), DAILY_STOP_CAP))
    d["cum_loss_stop_usd"] = max(0.0, min(f("cum_loss_stop_usd"), CUM_STOP_CAP))
    d["stale_s"] = max(1.0, min(f("stale_s"), STALE_CAP_S))
    d["entry_window_s"] = max(1.0, min(f("entry_window_s"), ENTRY_WINDOW_CAP_S))
    d["hard_stop_s"] = max(60.0, min(f("hard_stop_s"), HARD_STOP_CAP_S))
    d["exit_wait_s"] = max(1.0, min(f("exit_wait_s"), EXIT_WAIT_CAP_S))
    d["poll_s"] = max(POLL_FLOOR_S, f("poll_s"))
    d["depth_fraction"] = max(0.0, min(f("depth_fraction"), DEPTH_FRAC_CAP))
    return d


def _led(settings) -> Ledger:
    return Ledger(settings.data, "reopen")


def cloid_hex(label: str) -> str:
    return "0x" + hashlib.sha256(label.encode()).hexdigest()[:32]


# ------------------------------------------------------------------ 交易所客户端（真实；测试注入假的）
CTX_TTL_S = 2.5
SIGNED_GAP_S = 0.002
_CTX_LOCK = threading.Lock()
_CTX_CACHE: dict = {"t": 0.0, "mc": None}
_CTX_BACKOFF = {"until": 0.0}
_ORDER_LOCK = threading.Lock()
_LAST_SIGNED = {"t": 0.0}


def _signed(fn):
    """进程级下单锁：同一时刻只有一个签名动作，且两次之间至少隔 SIGNED_GAP_S（防 nonce 撞车）。"""
    with _ORDER_LOCK:
        gap = SIGNED_GAP_S - (time.monotonic() - _LAST_SIGNED["t"])
        if gap > 0:
            time.sleep(gap)
        try:
            return fn()
        finally:
            _LAST_SIGNED["t"] = time.monotonic()


def _shared_ctx(fetch):
    """所有 runner / 线程共享的 metaAndAssetCtxs：2.5 秒缓存 + 全局 429 退避。"""
    with _CTX_LOCK:
        now = time.monotonic()
        if _CTX_CACHE["mc"] is not None and now - _CTX_CACHE["t"] < CTX_TTL_S:
            return _CTX_CACHE["mc"]
        if now < _CTX_BACKOFF["until"]:
            raise RuntimeError("429 shared backoff active")
        try:
            mc = fetch()
        except Exception as e:  # noqa: BLE001
            if is_rate_limited(e):
                _CTX_BACKOFF["until"] = time.monotonic() + min(BACKOFF_MAX_S, max(POLL_FLOOR_S, 5.0))
            raise
        _CTX_CACHE.update(t=time.monotonic(), mc=mc)
        return mc


class ReopenClient(autotrade.Client):
    """autotrade.Client 上加：整个 dex 的 ctx、盘口、ALO/IOC 限价（带 cloid）、按 oid 查单、撤单。key=None → 只读（干跑不需要钱包）。"""

    def __init__(self, address: str | None, key: str | None):
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        self.address = address
        names = ["", autotrade.DEX]
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=names)
        self.ex = None
        if key:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            self.ex = Exchange(Account.from_key(key), constants.MAINNET_API_URL, account_address=address, perp_dexs=names)

    def ctxs(self, coins: list[str]) -> dict:
        mc = _shared_ctx(lambda: self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": autotrade.DEX}))
        names = [u["name"] for u in mc[0]["universe"]]
        out = {}
        idx = {n: i for i, n in enumerate(names)}
        for c in coins:
            if c not in idx:                                   # 名字不在交易所名单里：只跳过这一个，别的照常
                if c not in _MISSING_WARNED:
                    _MISSING_WARNED.add(c)
                    log.warning("coin %s not on dex; skipped", c)
                continue
            x = mc[1][idx[c]]
            out[c] = {"oracle": float(x["oraclePx"]), "mid": float(x.get("midPx") or x["markPx"]), "oi": float(x["openInterest"])}
        return out

    def book(self, coin: str) -> dict:
        lv = self.info.l2_snapshot(coin)["levels"]
        b, a = lv[0][0], lv[1][0]
        bid, ask = float(b["px"]), float(a["px"])
        return {"bid": bid, "ask": ask, "bid_depth_usd": bid * float(b["sz"]), "ask_depth_usd": ask * float(a["sz"])}

    def set_leverage(self, coin: str, lev: int):
        r = _signed(lambda: self.ex.update_leverage(int(lev), coin, is_cross=False))      # 逐仓
        if not isinstance(r, dict) or r.get("status") != "ok":
            raise RuntimeError(f"改杠杆失败 {coin}: {str(r)[:150]}")
        return r

    def _order(self, coin, is_buy, sz, px, reduce_only, cloid, tif) -> dict:
        from hyperliquid.utils.types import Cloid
        resp = _signed(lambda: self.ex.order(coin, is_buy, sz, px, {"limit": {"tif": tif}}, reduce_only=reduce_only,  # 真下单路径
                                             cloid=Cloid.from_str(cloid_hex(cloid))))
        out = {"filled_sz": 0.0, "avg_px": None, "oid": None, "error": None, "raw": resp}
        try:
            if resp.get("status") != "ok":
                out["error"] = str(resp)[:200]
                return out
            for s in resp["response"]["data"]["statuses"]:
                if "filled" in s:
                    out["filled_sz"] += float(s["filled"]["totalSz"])
                    out["avg_px"] = float(s["filled"]["avgPx"])
                    out["oid"] = s["filled"].get("oid")
                elif "resting" in s:
                    out["oid"] = s["resting"]["oid"]
                elif "error" in s:
                    out["error"] = str(s["error"])[:200]
        except (KeyError, TypeError, ValueError) as e:
            out["error"] = f"解析回包失败 {e}: {str(resp)[:150]}"
        return out

    def alo(self, coin, is_buy, sz, px, reduce_only, cloid) -> dict:
        return self._order(coin, is_buy, sz, px, reduce_only, cloid, "Alo")

    def ioc(self, coin, is_buy, sz, px, reduce_only, cloid) -> dict:
        return self._order(coin, is_buy, sz, px, reduce_only, cloid, "Ioc")

    def cancel(self, coin: str, oid, cloid: str | None = None):
        if oid is not None:
            return _signed(lambda: self.ex.cancel(coin, int(oid)))
        from hyperliquid.utils.types import Cloid
        return _signed(lambda: self.ex.cancel_by_cloid(coin, Cloid.from_str(cloid_hex(cloid))))

    def order_info(self, oid) -> dict | None:
        r = self.info.query_order_by_oid(self.address, int(oid))
        o = (r or {}).get("order") or {}
        if not o:
            return None
        inner = o.get("order") or {}
        return {"status": o.get("status"), "orig_sz": float(inner.get("origSz", 0) or 0), "sz": float(inner.get("sz", 0) or 0)}

    def fill_for_cloid(self, coin: str, cloid_hex_: str, since_ms: int):
        sz = notional = 0.0
        for f in self.info.user_fills(self.address):
            if f.get("coin") == coin and str(f.get("cloid", "")).lower() == cloid_hex_.lower():
                q = float(f["sz"])
                sz += q
                notional += q * float(f["px"])
        return (sz, notional / sz) if sz > 0 else None


def _make_client(settings, need_trading: bool):
    if autotrade._client_factory:
        return autotrade._client_factory()
    a, k = autotrade.creds()
    ok = autotrade.valid_address(a) and autotrade.valid_key(k)
    if need_trading and not ok:
        raise RuntimeError("还没有在设置里填 Hyperliquid 钱包地址和 API 私钥")
    return ReopenClient(a if ok else None, k if ok else None)


# ------------------------------------------------------------------ 事件表（时间全部来自 reopen_study）
@dataclass
class Event:
    kind: str                    # "weekly" | "daily_18"
    day: date                    # 事件的 ET 日期
    nominal_ns: int
    names: list = field(default_factory=list)

    @property
    def key(self) -> str:
        hhmm = datetime.fromtimestamp(self.nominal_ns / NS, timezone.utc).astimezone(ET).strftime("%H%M")
        return f"reopen:{self.kind}:{self.day.isoformat()}:{hhmm}"


FEE_TTL_S = 6 * 3600
FEE_MARGIN_BPS = 3.0
_FEE_CACHE: dict = {"t": 0.0, "rt": None, "src": "full"}


def round_trip_fee_bps(c, now_s: float | None = None) -> tuple[float, str]:
    """实际 maker 进 + maker 出 的 bps（userFees，缓存 6h）；查不到 → 满额 2×3 bps。"""
    now_s = time.time() if now_s is None else now_s
    if _FEE_CACHE["rt"] is not None and now_s - _FEE_CACHE["t"] < FEE_TTL_S:
        return _FEE_CACHE["rt"], _FEE_CACHE["src"]
    full = 2 * DEFAULTS["fee_maker_bps"]
    rt, src = full, "full(fallback)"
    try:
        r = c.info.post("/info", {"type": "userFees", "user": c.address})
        mk = float(r["userAddRate"]) * 1e4
        if 0 <= mk <= 20:
            rt, src = 2 * mk, "userFees"
    except Exception:  # noqa: BLE001
        pass
    _FEE_CACHE.update(t=now_s, rt=rt, src=src)
    return rt, src


def eff_k_bps(cf: dict, c=None) -> float:
    rt, _ = round_trip_fee_bps(c) if c is not None else (2 * DEFAULTS["fee_maker_bps"], "full")
    return max(cf["k_bps"], rt + FEE_MARGIN_BPS)


def coin_of(settings, name: str) -> str:
    inst = settings.inst(name)
    return inst["coin"] if inst else f"xyz:{name}"


def events_on(day: date, cf: dict) -> list[Event]:
    """某个 ET 日期上、配置里的币的事件，按名义时刻分组。weekly = 周日；daily_18 = 周一到周四（周日 18:00 归 weekly）。"""
    want = set(cf["coins"])
    rows = [r for r in rs.gen_events("weekly", day, day) if r[0] == "weekly"]
    rows += [r for r in rs.gen_events("daily", day, day) if r[0] == "daily_18"]
    by: dict = {}
    for kind, d, name in rows:
        if name in want:
            by.setdefault((kind, rs.nominal_ns(d, name, kind=kind)), []).append(name)
    # 亚洲事件按市场本地日期生成；Event.day 仍是 ET 日期 -> 取本地日期 day-1..day+1 里名义时刻落在该 ET 日的
    asia = cf.get("asia_coins") or {}
    if any(asia.values()):
        for dd in (day - timedelta(days=1), day, day + timedelta(days=1)):
            for kind, d, name in rs.gen_events("asia", dd, dd, asia):
                nom = rs.nominal_ns(d, name, kind=kind)
                if datetime.fromtimestamp(nom / NS, timezone.utc).astimezone(ET).date() == day:
                    by.setdefault((kind, nom), []).append(name)
    return [Event(k, day, nom, sorted(set(ns))) for (k, nom), ns in sorted(by.items(), key=lambda kv: kv[0][1])]


def next_events(now: datetime, cf: dict, days: int = 9) -> list[Event]:
    d0 = now.astimezone(ET).date()
    out = [e for i in range(-1, days) for e in events_on(d0 + timedelta(days=i), cf)]
    return [e for e in out if e.nominal_ns / NS >= now.timestamp() - 180]


def event_from_key(key: str, cf: dict) -> Event | None:
    try:
        _, kind, ds, hhmm = key.split(":")
        day = date.fromisoformat(ds)
    except ValueError:
        return None
    return next((e for e in events_on(day, cf) if e.kind == kind and e.key == key), None)


# ------------------------------------------------------------------ 账本 → 仓位状态（唯一真相来源 = 我们自己的记录）
def load_state(settings) -> dict:
    pos: dict[str, dict] = {}
    for i, r in enumerate(_led(settings).all()):
        k, pid = r["kind"], r.get("pos_id")
        if k == "open":
            pos[pid] = {"open": r, "orders": {}, "net": 0.0, "cash": 0.0, "fees": 0.0, "recorded": {}, "ended": set(),
                        "first_fill_ms": None, "last_px": None, "closed": None, "exit_reason": None}
        elif pid not in pos:
            continue
        elif k == "order":
            pos[pid]["orders"][r["cloid"]] = r
        elif k == "order_end":
            pos[pid]["ended"].add(r["cloid"])
        elif k == "fill":
            p = pos[pid]
            p["net"] = round(p["net"] + r["signed_sz"], 10)
            p["cash"] -= r["signed_sz"] * (r.get("px") or 0.0)
            p["fees"] += r.get("fee_usd", 0.0) or 0.0
            p["recorded"][r["cloid"]] = p["recorded"].get(r["cloid"], 0.0) + abs(r["signed_sz"])
            p["last_px"] = r.get("px") or p["last_px"]
            p["first_fill_ms"] = p["first_fill_ms"] or r["t_ms"]
        elif k == "exit_reason":
            pos[pid]["exit_reason"] = pos[pid]["exit_reason"] or r["reason"]
        elif k == "closed":
            pos[pid]["closed"] = r
    return {"pos": pos}


def open_positions(st: dict) -> dict:
    return {pid: p for pid, p in st["pos"].items() if not p["closed"]}


def has_open(settings) -> bool:
    return bool(open_positions(load_state(settings)))


def _day_start_ms(now_ms: int) -> int:
    d = datetime.fromtimestamp(now_ms / 1000, ET).date()
    return int(datetime.combine(d, dtime(0, 0), ET).timestamp() * 1000)


def realized(st: dict, since_ms: int = 0) -> float:
    return sum(p["closed"].get("pnl_usd", 0.0) for p in st["pos"].values() if p["closed"] and p["closed"].get("t_ms", 0) >= since_ms)


# ------------------------------------------------------------------ 一次管理 / 评估的上下文
class MCtx:
    def __init__(self, settings, c, cf, now_ms, dry):
        self.settings, self.c, self.cf, self.now_ms, self.dry = settings, c, cf, now_ms, dry
        self.led = _led(settings)
        self.real = isinstance(c, ReopenClient)
        self._book: dict = {}
        self._ctx: dict = {}

    def book(self, coin):
        if coin not in self._book:
            try:
                b = self.c.book(coin)
                self._book[coin] = b if b and b.get("bid") and b.get("ask") else None
            except Exception:  # noqa: BLE001
                self._book[coin] = None
        return self._book[coin]

    def oracle(self, coin):
        if coin not in self._ctx:
            try:
                self._ctx[coin] = float(self.c.ctxs([coin])[coin]["oracle"])
            except Exception:  # noqa: BLE001
                self._ctx[coin] = None
        return self._ctx[coin]

    def mark(self, coin, fallback):
        b = self.book(coin)
        return (b["bid"] + b["ask"]) / 2 if b else fallback


def _push(settings, title: str, body: str, priority: str = "default"):
    """手机推送：默认关（[reopen] notify = false）。关着时只写日志，不发。"""
    try:
        if not cfg(settings).get("notify"):
            log.info("reopen push suppressed: %s | %s", title, body[:200])
            return False
    except Exception:  # noqa: BLE001
        return False
    return notify.push(settings, title, body, priority=priority)


def _alert(m: MCtx, title: str, body: str, pid: str = "-", priority: str = "max"):
    m.led.append("alert", pid, {"pos_id": pid, "title": title, "body": body[:500], "t_ms": m.now_ms})
    _push(m.settings, title, body[:500], priority=priority)


def _halt(m: MCtx, why: str):
    put(m.settings, halt_entries=True, halt_reason=why[:200])


def _fee(m: MCtx, purpose: str, sz: float, px: float) -> float:
    bps = m.cf["fee_maker_bps"] if purpose in ("entry", "exit_alo") else m.cf["fee_taker_bps"]
    return bps / 1e4 * sz * px


def pos_pnl(m: MCtx, p: dict) -> float:
    mk = m.mark(p["open"]["coin"], p["last_px"] or 0.0) if abs(p["net"]) > EPS else 0.0
    return p["cash"] + p["net"] * mk - p["fees"]


def pnl_state(m: MCtx, st: dict) -> dict:
    unreal = sum(pos_pnl(m, p) for p in open_positions(st).values())
    day = realized(st, _day_start_ms(m.now_ms)) + unreal
    cum = realized(st) + unreal
    try:   # 给 account_stop 用：未平仓位的浮动盈亏快照（它没有交易所连接）
        tmp = pnl_snapshot_path(m.settings).with_suffix(".tmp")
        tmp.write_text(json.dumps({"unrealized_usd": unreal, "n_open": len(open_positions(st)), "t_ms": m.now_ms}))
        tmp.replace(pnl_snapshot_path(m.settings))
    except OSError:
        pass
    return {"day": day, "cum": cum, "daily_hit": day <= -m.cf["daily_loss_stop_usd"], "cum_hit": cum <= -m.cf["cum_loss_stop_usd"]}


# ------------------------------------------------------------------ 下单（所有真单经过 _place）
def _place(m: MCtx, pid: str, coin: str, is_buy: bool, sz: float, px: float, reduce_only: bool, purpose: str, tif: str) -> dict:
    """写"已发出" → 发单 → 写结果。IOC 的成交立刻记账；ALO 的成交由 _sync 轮询记账。异常 → 结果不明：停开新仓，留给 _sync 按 cloid 认领。"""
    n = sum(1 for r in m.led.all() if r["kind"] == "order" and r.get("pos_id") == pid)
    cloid = f"{PREFIX}{pid}-{purpose}-{n + 1}"
    m.led.append("order", pid, {"pos_id": pid, "coin": coin, "cloid": cloid, "cloid_hex": cloid_hex(cloid), "is_buy": is_buy, "sz": sz,
                                "px": px, "reduce_only": reduce_only, "purpose": purpose, "tif": tif, "oid": None, "t_ms": m.now_ms})
    fn = m.c.alo if tif == "Alo" else m.c.ioc
    try:
        r = fn(coin, is_buy, sz, px, reduce_only, cloid)
    except Exception as e:  # noqa: BLE001
        log.exception("order exception")
        _halt(m, f"订单结果不明 {cloid}")
        _alert(m, f"重开下单状态不明：{coin}", f"{cloid}: {e}。已停开新仓；会按 cloid 查成交认领，请核对持仓与挂单。", pid)
        return {"cloid": cloid, "error": str(e)[:200], "unknown": True, "filled_sz": 0.0}
    r = dict(r)
    r["cloid"] = cloid
    if r.get("oid") is not None:
        m.led.append("order_oid", pid, {"pos_id": pid, "cloid": cloid, "oid": r["oid"]})
    if tif == "Ioc" or r.get("error"):
        f = min(max(0.0, float(r.get("filled_sz") or 0.0)), sz)
        if f > 0:
            _record_fill(m, pid, coin, cloid, is_buy, f, float(r.get("avg_px") or px), purpose, est=not r.get("avg_px"))
        m.led.append("order_end", pid, {"pos_id": pid, "cloid": cloid, "why": "ioc_done" if not r.get("error") else f"error:{r['error']}"})
    return r


def _record_fill(m: MCtx, pid: str, coin: str, cloid: str, is_buy: bool, sz: float, px: float, purpose: str, est: bool = False):
    sgn = 1.0 if is_buy else -1.0
    m.led.append("fill", pid, {"pos_id": pid, "coin": coin, "cloid": cloid, "signed_sz": sgn * sz, "px": px, "px_est": True if est else None,
                               "purpose": purpose, "fee_usd": _fee(m, purpose, sz, px), "t_ms": m.now_ms})
    side = "买" if is_buy else "卖"
    _push(m.settings, f"重开成交：{coin} {side} {sz:g} @ {px:g}", f"{purpose}，名义约 ${sz * px:.0f}，仓位 {pid}", priority="high")


def _oids(settings, pid) -> dict:
    return {r["cloid"]: r["oid"] for r in _led(settings).all() if r["kind"] == "order_oid" and r.get("pos_id") == pid}


def _sync(m: MCtx, pid: str, cloid: str):
    """把一张单在交易所的成交同步进账本；单已终结（filled / canceled / rejected）→ 写 order_end。"""
    p = load_state(m.settings)["pos"][pid]
    od = p["orders"][cloid]
    if cloid in p["ended"]:
        return
    coin, oid = od["coin"], _oids(m.settings, pid).get(cloid)
    status, filled = None, None
    if oid is not None:
        try:
            info = m.c.order_info(oid)
        except Exception:  # noqa: BLE001
            info = None
        if info:
            status = info["status"]
            filled = info["orig_sz"] if status == "filled" else max(0.0, info["orig_sz"] - info["sz"])
    g = None
    if filled is None or filled > p["recorded"].get(cloid, 0.0) + EPS:
        try:
            g = m.c.fill_for_cloid(coin, od["cloid_hex"], m.now_ms - 3_600_000)
        except Exception:  # noqa: BLE001
            g = None
        if filled is None:
            filled = g[0] if g else 0.0
    rec = p["recorded"].get(cloid, 0.0)
    if filled > rec + EPS:
        delta = min(filled, od["sz"]) - rec
        if delta > EPS:
            _record_fill(m, pid, coin, cloid, od["is_buy"], delta, float(g[1]) if g else od["px"], od["purpose"], est=g is None)
    if status in ("filled", "canceled", "rejected", "marginCanceled", "scheduledCancel") or (status is None and oid is None and filled and filled >= od["sz"] - EPS):
        m.led.append("order_end", pid, {"pos_id": pid, "cloid": cloid, "why": status or "filled_by_cloid"})


def _cancel(m: MCtx, pid: str, cloid: str):
    p = load_state(m.settings)["pos"][pid]
    if cloid in p["ended"]:
        return
    oid = _oids(m.settings, pid).get(cloid)
    try:
        m.c.cancel(p["orders"][cloid]["coin"], oid, cloid)
    except Exception as e:  # noqa: BLE001
        log.warning("cancel %s: %s", cloid, e)
    _sync(m, pid, cloid)       # 撤单与成交赛跑：先记账最终成交，再看是否终结
    p = load_state(m.settings)["pos"][pid]
    if cloid not in p["ended"] and oid is None:
        m.led.append("order_end", pid, {"pos_id": pid, "cloid": cloid, "why": "cancel_sent_no_oid"})


def _close_pos(m: MCtx, pid: str, reason: str):
    p = load_state(m.settings)["pos"][pid]
    pnl = p["cash"] - p["fees"]
    m.led.append("closed", pid, {"pos_id": pid, "coin": p["open"]["coin"], "reason": reason, "pnl_usd": round(pnl, 4), "t_ms": m.now_ms})
    if reason != "no_fill":
        stop = reason in ("time_stop", "daily_loss_stop", "cum_loss_stop", "account_stop")
        _push(m.settings, f"重开已平仓：{p['open']['coin']}" + ("（止损）" if stop else ""),
                    f"原因 {reason}，这一笔 ${pnl:+.2f}（含满额手续费估算），仓位 {pid}", priority="max" if stop else "high")


# ------------------------------------------------------------------ 管理已有仓位（每秒一次；每步重读账本）
def manage_all(m: MCtx) -> dict:
    out = {"closed": [], "stops": []}
    st = load_state(m.settings)
    ops = open_positions(st)
    if not ops:
        return out
    ps = pnl_state(m, st)
    force = None
    if ps["cum_hit"]:
        force = "cum_loss_stop"
        if not get(m.settings)["halt_entries"]:
            _halt(m, f"累计亏损止损 ${ps['cum']:.2f}")
            _alert(m, "重开：累计亏损止损触发，停开新仓", f"累计 ${ps['cum']:.2f}，上限 −${m.cf['cum_loss_stop_usd']:.0f}。需人工 --clear-halt。")
    elif ps["daily_hit"]:
        force = "daily_loss_stop"
        if not any(r["kind"] == "alert" and r.get("title", "").startswith("重开：当日亏损止损") and r.get("t_ms", 0) >= _day_start_ms(m.now_ms)
                   for r in m.led.all()):
            _alert(m, "重开：当日亏损止损触发", f"今日 ${ps['day']:.2f}，上限 −${m.cf['daily_loss_stop_usd']:.0f}，平掉全部重开仓位。")
    if not force:
        try:
            from . import account_stop
            if account_stop.check(m.settings).get("tripped"):
                force = "account_stop"
        except Exception:  # noqa: BLE001
            log.exception("account_stop check")
    if force:
        out["stops"].append(force)
    for pid in list(ops):
        try:
            if _manage_one(m, pid, force):
                out["closed"].append(pid)
        except Exception as e:  # noqa: BLE001
            log.exception("manage %s", pid)
            _alert(m, f"重开仓位管理出错：{pid}", f"{type(e).__name__}: {e}"[:300], pid, "high")
    return out


def _manage_one(m: MCtx, pid: str, force: str | None) -> bool:
    cf, now = m.cf, m.now_ms
    st = load_state(m.settings)["pos"][pid]
    o, coin = st["open"], st["open"]["coin"]
    ent = o["entry_cloid"]
    # ---- 入场单：同步成交；有成交 / 到期 / 被强制 → 撤掉剩余挂单
    for cl in list(st["orders"]):                          # 先把所有未终结的单（含超时状态未知的 exit_ioc）同步一遍
        if cl not in st["ended"] and cl != ent:
            _sync(m, pid, cl)
    st = load_state(m.settings)["pos"][pid]
    if ent not in st["ended"]:
        _sync(m, pid, ent)
        st = load_state(m.settings)["pos"][pid]
        if ent not in st["ended"] and (abs(st["net"]) > EPS or now >= o["entry_deadline_ms"] or force):
            _cancel(m, pid, ent)
            st = load_state(m.settings)["pos"][pid]
        if ent not in st["ended"]:
            return False
    net = st["net"]
    exits = [od for od in st["orders"].values() if od["purpose"] == "exit_alo"]
    alo = exits[0] if exits else None
    if alo:
        if alo["cloid"] not in st["ended"]:
            _sync(m, pid, alo["cloid"])
            st = load_state(m.settings)["pos"][pid]
        net = st["net"]
    if abs(net) < EPS:
        for cl in list(st["orders"]):                      # 残留未终结的单（理论上不该有）→ 撤掉再收尾
            if cl not in st["ended"]:
                _cancel(m, pid, cl)
        st = load_state(m.settings)["pos"][pid]
        if any(cl not in st["ended"] for cl in st["orders"]):
            return False
        _close_pos(m, pid, "no_fill" if st["first_fill_ms"] is None else (st["exit_reason"] or "oracle_exit"))
        return True
    # ---- 持仓中
    reason = force
    if not reason and now >= o["t0_ms"] + cf["hard_stop_s"] * 1000:
        reason = "time_stop"
    alo_active = bool(alo) and alo["cloid"] not in st["ended"]
    if not reason and alo and not alo_active:
        reason = "exit_alo_ended"
    if not reason and alo_active and now >= alo["t_ms"] + cf["exit_wait_s"] * 1000:
        reason = "exit_timeout"
    sell = net > 0
    b = m.book(coin)
    dec = _dec(m, coin)
    if dec is None:
        log.warning("szDecimals unknown for %s; skip manage cycle", coin)
        return False
    if reason:
        if alo_active:
            _cancel(m, pid, alo["cloid"])
            st = load_state(m.settings)["pos"][pid]
            net = st["net"]
            if abs(net) < EPS:
                return False                               # 撤单时恰好成交完：下一步收尾
        if st["exit_reason"] is None:
            m.led.append("exit_reason", pid, {"pos_id": pid, "reason": reason, "t_ms": now})
        n_ioc = sum(1 for od in st["orders"].values() if od["purpose"] == "exit_ioc")
        if n_ioc >= int(cf["ioc_attempts"]):
            _alert_rl(m, pid, f"重开平仓多次失败：{coin}", f"{n_ioc} 次 reduce-only IOC 都没平掉，剩余 {net:+g}。请手动处理。")
            return False
        if not b:
            return False
        slip = cf["exit_slippage_bps"] * (2 ** min(n_ioc, 3)) / 1e4
        mid = (b["bid"] + b["ask"]) / 2
        px = round_px_dir(mid * (1 - slip) if sell else mid * (1 + slip), dec, down=sell)
        q = autotrade.round_sz(abs(net), dec)
        if q <= 0:
            log.warning("exit size rounds to 0 for %s (net %g)", coin, net)
            return False
        _place(m, pid, coin, not sell, q, px, True, "exit_ioc", "Ioc")
        st = load_state(m.settings)["pos"][pid]
        if abs(st["net"]) < EPS:
            _close_pos(m, pid, st["exit_reason"] or reason)
            return True
        return False
    if not alo:                                            # 刚成交：在当前 oracle 价挂 post-only 平仓单（reduce-only）
        orc = m.oracle(coin)
        if not orc or not b:
            return False
        px = autotrade.round_px(orc, dec)
        crosses = px <= b["bid"] if sell else px >= b["ask"]
        if crosses:                                        # ALO 会吃单被拒 → 价格比 oracle 更好，直接 IOC
            m.led.append("exit_reason", pid, {"pos_id": pid, "reason": "oracle_crossed_book", "t_ms": now})
            return _manage_one(m, pid, "oracle_crossed_book")
        q = autotrade.round_sz(abs(net), dec)
        if q <= 0:
            log.warning("exit size rounds to 0 for %s (net %g)", coin, net)
            return False
        _place(m, pid, coin, not sell, q, px, True, "exit_alo", "Alo")
    return False


def _alert_rl(m: MCtx, pid: str, title: str, body: str, every_ms: int = 300_000):
    last = max((r.get("t_ms", 0) for r in m.led.all() if r["kind"] == "alert" and r.get("title") == title and r.get("pos_id") == pid), default=-1e18)
    if m.now_ms - last >= every_ms:
        _alert(m, title, body, pid)


_DEC_CACHE: dict = {}


def _dec(m: MCtx, coin: str) -> int | None:
    """szDecimals；成功后按币缓存。失败 → None（调用方本轮跳过），不再瞎猜 2。"""
    if coin in _DEC_CACHE:
        return _DEC_CACHE[coin]
    try:
        d = int(m.c.sz_decimals(coin))
    except Exception as e:  # noqa: BLE001
        log.warning("sz_decimals %s failed: %s", coin, e)
        return None
    _DEC_CACHE[coin] = d
    return d


def _px_decimals(px: float, sz_dec: int) -> int:
    dec = max(0, 6 - sz_dec)
    sig = 5 - int(math.floor(math.log10(px))) - 1
    return max(0, min(dec, sig))


def round_px_dir(px: float, sz_dec: int, down: bool) -> float:
    """保守取整：down=True（卖）只往下，False（买）只往上；仍满足 HL 5 位有效数字 / 小数位规则。"""
    r = autotrade.round_px(px, sz_dec)
    if px <= 0 or r == px:
        return r
    if down and r > px:
        r = round(r - 10 ** -_px_decimals(r, sz_dec), _px_decimals(r, sz_dec))
        r = autotrade.round_px(r, sz_dec)
    elif not down and r < px:
        d = _px_decimals(r, sz_dec)
        r = autotrade.round_px(round(r + 10 ** -d, d), sz_dec)
    return r


# ------------------------------------------------------------------ 开仓
def entry_block(m: MCtx, st: dict, coin: str, last_ok_ms: float | None) -> str | None:
    """只拦新开仓的守卫 → 中文原因或 None。顺序：标志 → 数据新鲜度 → 亏损 → 持仓数。"""
    h = get(m.settings)
    if h.get("halt_entries"):
        return f"halt_entries 标志（{h.get('halt_reason') or '?'}）"
    if coin in _halted(m.settings):
        return f"{coin} 已下架 / 暂停"
    if last_ok_ms is None or m.now_ms - last_ok_ms > m.cf["stale_s"] * 1000:
        return f"行情数据超过 {m.cf['stale_s']:.0f} 秒没更新"
    ps = pnl_state(m, st)
    if ps["cum_hit"]:
        _halt(m, f"累计亏损止损 ${ps['cum']:.2f}")
        _alert(m, "重开：累计亏损止损触发，停开新仓", f"累计 ${ps['cum']:.2f}，上限 −${m.cf['cum_loss_stop_usd']:.0f}。需人工 --clear-halt。")
        return "累计亏损止损"
    if ps["daily_hit"]:
        return f"当日亏损止损（${ps['day']:.2f}）"
    try:
        from . import account_stop
        if account_stop.check(m.settings).get("tripped"):
            return "总账户止损已触发"
    except Exception as e:  # noqa: BLE001  读不到 = 失明，不开仓
        return f"总账户止损检查失败（{type(e).__name__}）"
    ops = open_positions(st)
    if sum(1 for p in ops.values() if p["open"]["coin"] == coin) >= PER_COIN_MAX:
        return f"{coin} 已有仓位（每币最多 {PER_COIN_MAX}）"
    if len(ops) >= m.cf["max_concurrent"]:
        return f"同时持仓已达上限 {m.cf['max_concurrent']}"
    return None


def _halted(settings) -> set:
    try:
        from . import pairs_exec
        p = Path(settings.data) / "universe_raw.json"
        return pairs_exec.halted_coins(json.loads(p.read_text())) if p.exists() else set()
    except Exception:  # noqa: BLE001
        return set()


def try_enter(m: MCtx, ev: Event, coin: str, S: int, oracle: float, mid: float, t0_ns: int, last_ok_ms: float | None) -> dict:
    """→ {"entered": bool, "why": str, "retry": bool}。retry=True 表示只是暂时被拦（数据过期），下次轮询还可以再试。"""
    cf = m.cf
    st = load_state(m.settings)
    if m.dry:
        return {"entered": False, "why": "干跑（off）：本来会下单，没有下", "retry": False, "dry": True}
    with _TICK_LOCK:                                         # 守卫检查 → 记 open → 下单 整段加锁，并行 runner 不会超 max_concurrent
        st = load_state(m.settings)
        why = entry_block(m, st, coin, last_ok_ms)
        if why:
            return {"entered": False, "why": why, "retry": "没更新" in why}
        b = m.book(coin)
        if not b or b["bid"] >= b["ask"]:
            return {"entered": False, "why": "读不到有效盘口", "retry": True}
        spread = (b["ask"] - b["bid"]) / ((b["ask"] + b["bid"]) / 2) * 1e4
        if spread > cf["max_book_spread_bps"]:
            return {"entered": False, "why": f"盘口价差 {spread:.0f} bps 太宽", "retry": True}
        is_buy = S < 0                                          # 方向 −S：S>0（冲高）→ 卖；S<0（砸低）→ 买
        px_ref = b["bid"] if is_buy else b["ask"]               # 挂在本方最优价（ALO，不吃单）
        depth = b["bid_depth_usd"] if is_buy else b["ask_depth_usd"]
        notional = min(cf["notional_usd"], NOTIONAL_HARD_CAP, cf["depth_fraction"] * depth)
        dec = _dec(m, coin)
        if dec is None:
            return {"entered": False, "why": "读不到 szDecimals", "retry": True}
        px = autotrade.round_px(px_ref, dec)
        sz = autotrade.round_sz(notional / px, dec)
        if sz * px < MIN_ORDER_USD * 1.05:
            return {"entered": False, "why": f"名义 ${sz * px:.2f} 低于最小下单（盘口深度 ${depth:.0f}）", "retry": False}
        try:
            pf = autotrade._live_preflight(m.settings, cf, m.c, check_wallet=m.real)
            if pf:
                _push(m.settings, f"重开下单未通过安全检查：{coin}", pf, priority="high")
                return {"entered": False, "why": pf, "retry": False}
            avail = m.c.balance().get("available")
            if avail is None or float(avail) < sz * px * 1.1 / LEVERAGE:
                return {"entered": False, "why": f"保证金不够（可用 {avail}）", "retry": False}
            pos = float(m.c.position(coin))
            if abs(pos) > EPS:
                return {"entered": False, "why": f"交易所上 {coin} 已有仓位 {pos}（手动或其他来源），不开仓", "retry": False}
            m.c.set_leverage(coin, LEVERAGE)
        except Exception as e:  # noqa: BLE001
            return {"entered": False, "why": f"下单前检查失败（{type(e).__name__}: {str(e)[:100]}）", "retry": False}
        n_open = sum(1 for r in m.led.all() if r["kind"] == "open")
        pid = f"{re.sub(r'[^A-Za-z0-9]+', '_', coin).strip('_')}-{m.now_ms}-{n_open + 1}"
        entry_cloid = f"{PREFIX}{pid}-entry-1"
        m.led.append("open", pid, {"pos_id": pid, "coin": coin, "event": ev.key, "S": S, "dir": -S, "t0_ms": int(t0_ns // 1_000_000),
                                   "entry_deadline_ms": int(t0_ns // 1_000_000 + cf["entry_window_s"] * 1000), "entry_cloid": entry_cloid,
                                   "notional_target": sz * px, "oracle_ref": oracle, "mid_at_signal": mid, "t_ms": m.now_ms})
        r = _place(m, pid, coin, is_buy, sz, px, False, "entry", "Alo")
        if r.get("error") and not r.get("unknown"):
            m.led.append("closed", pid, {"pos_id": pid, "coin": coin, "reason": "no_fill", "pnl_usd": 0.0, "t_ms": m.now_ms})
            return {"entered": False, "why": f"ALO 被拒：{r['error']}", "retry": False}
    _push(m.settings, f"重开挂单：{coin} {'买' if is_buy else '卖'} {sz:g} @ {px:g}",
                f"过冲 {S * (mid / oracle - 1) * 1e4:.1f} bps > {cf['k_bps']:g}，名义约 ${sz * px:.0f}，T0+{cf['entry_window_s']:.0f}s 没成交就撤", priority="high")
    return {"entered": True, "why": "已挂 ALO", "retry": False, "pos_id": pid}


# ------------------------------------------------------------------ 事件运行器
class Runner:
    def __init__(self, settings, ev: Event, client, cf: dict, dry: bool, clock=time.time, sleep=time.sleep, seed_fn=None):
        self.settings, self.ev, self.c, self.cf, self.dry = settings, ev, client, cf, dry
        self.clock, self.sleep = clock, sleep
        self.seed_fn = seed_fn if seed_fn is not None else rs.read_ctx
        self.coins = {n: coin_of(settings, n) for n in ev.names}
        self.samples = {n: [] for n in ev.names}
        self.t0: dict = {}
        self.final: dict = {}            # name -> 结论（已决策 / 窗口过了 / 没找到 T0）
        self.max_ovr = {n: 0.0 for n in ev.names}
        self.last_ok_ms: float | None = None
        self.backoff_s = 0.0
        self.logged: set = set()
        self.led = _led(settings)

    # ---- 日志
    def _log(self, name: str, what: str, **kw):
        self.led.append("decision", self.ev.key, {"event": self.ev.key, "name": name, "coin": self.coins.get(name), "what": what,
                                                  "dry": self.dry, "t_ms": int(self.clock() * 1000), **kw})
        log.info("reopen %s %s: %s %s", self.ev.key, name, what, kw)

    def _once(self, name, what, **kw):
        if (name, what) not in self.logged:
            self.logged.add((name, what))
            self._log(name, what, **kw)

    # ---- 预热 + 轮询
    def seed(self):
        now_ns = int(self.clock() * NS)
        for n, coin in self.coins.items():
            try:
                self.samples[n] = sorted(self.seed_fn(self.settings, coin, self.ev.nominal_ns - 600 * NS, now_ns))
            except Exception as e:  # noqa: BLE001
                log.warning("seed %s: %s", coin, e)
                self.samples[n] = []

    def poll(self, m: MCtx) -> dict:
        """→ {name: (oracle, mid)}；成功才更新 last_ok_ms（数据新鲜度）。"""
        cur = {}
        try:
            cx = self.c.ctxs(list(self.coins.values()))
            for n, coin in self.coins.items():
                x = cx.get(coin)
                if x is None:
                    continue
                row = (int(self.clock() * NS), float(x["oracle"]), float(x["mid"]), float(x.get("oi", 0.0)))
                s = self.samples[n]
                if not s or row[0] > s[-1][0]:            # 每次成功轮询都记一行（值不变也记），冻结 oracle 的币也能凑够 5 个前置步
                    s.append(row)
                cur[n] = (row[1], row[2])
            self.last_ok_ms = self.clock() * 1000
            self.backoff_s = 0.0
        except Exception as e:  # noqa: BLE001
            if is_rate_limited(e):
                self.backoff_s = min(BACKOFF_MAX_S, max(self.cf["poll_s"], self.backoff_s * 2))
                log.warning("ctx poll 429, backoff %.0fs", self.backoff_s)
            else:
                log.warning("ctx poll failed: %s", e)
        return cur

    def tick(self) -> bool:
        """一次轮询 + 决策 + 仓位管理。→ True = 这个事件已经全部结束（可以退出）。"""
        now_ms = int(self.clock() * 1000)
        now_ns = now_ms * 1_000_000
        m = MCtx(self.settings, self.c, self.cf, now_ms, self.dry)
        cur = self.poll(m)
        for n in self.coins:
            if n in self.final or n not in cur:
                continue
            t0, ok = rs.detect_t0(self.samples[n], self.ev.nominal_ns, min_ns=self.ev.nominal_ns - 60 * NS)
            if ok and t0 is not None:
                if n not in self.t0:
                    self._log(n, "t0_detected", t0_ns=t0, lag_s=round((t0 - self.ev.nominal_ns) / NS, 1))
                self.t0[n] = t0
            if n not in self.t0:
                if now_ns > self.ev.nominal_ns + int(self.cf["t0_wait_s"] * NS):
                    self.final[n] = "no_t0"
                    self._log(n, "no_t0", note="名义时间后一直没出现价格跳变；研究里这类事件取回退 T0，实盘不碰")
                continue
            t0 = self.t0[n]
            if now_ns > t0 + int(self.cf["entry_window_s"] * NS):
                self.final[n] = "window_closed"
                self._log(n, "window_closed", max_overshoot_bps=round(self.max_ovr[n], 1))
                continue
            if now_ns < t0:
                continue
            if n not in cur:
                continue
            oracle, mid = cur[n]
            pre = rs.Series(self.samples[n], t0).at(-5, 1)
            if not pre or not oracle:
                continue
            G = oracle / pre - 1
            S = rs._sign(G)
            if S == 0:
                continue
            ovr = S * (mid / oracle - 1) * 1e4
            self.max_ovr[n] = max(self.max_ovr[n], ovr)
            ek = eff_k_bps(self.cf, m.c if m.real else None)
            if ovr <= ek:
                continue
            res = try_enter(m, self.ev, self.coins[n], S, oracle, mid, t0, self.last_ok_ms)
            if res["entered"] or not res.get("retry"):
                self.final[n] = "entered" if res["entered"] else ("dry_signal" if res.get("dry") else "blocked")
                self._log(n, "entry" if res["entered"] else ("would_enter" if res.get("dry") else "blocked"),
                          S=S, G=G, overshoot_bps=round(ovr, 1), eff_k_bps=round(ek, 2), oracle=oracle, mid=mid, why=res["why"], pos_id=res.get("pos_id"))
            else:
                self._once(n, "blocked_retry", why=res["why"])
        with _TICK_LOCK:
            manage_all(MCtx(self.settings, self.c, self.cf, now_ms, self.dry))
        mine = [p for p in open_positions(load_state(self.settings)).values() if p["open"]["event"] == self.ev.key]
        return len(self.final) == len(self.coins) and not mine

    def run(self, immediate: bool = False) -> None:
        start = self.ev.nominal_ns / NS - self.cf["pre_s"]
        while not immediate and self.clock() < start:
            self.sleep(min(5.0, max(0.1, start - self.clock())))
        self.seed()
        end = self.ev.nominal_ns / NS + max(self.cf["t0_wait_s"], 0) + self.cf["entry_window_s"] + self.cf["hard_stop_s"] + 120
        while self.clock() < end:
            t = self.clock()
            try:
                if self.tick():
                    return
            except Exception as e:  # noqa: BLE001
                log.exception("reopen tick failed")
                if is_rate_limited(e):
                    self.backoff_s = min(BACKOFF_MAX_S, max(self.cf["poll_s"], self.backoff_s * 2))
            self.sleep(max(0.05, self.cf["poll_s"] + self.backoff_s - (self.clock() - t)))
        left = open_positions(load_state(self.settings))
        if left:
            _push(self.settings, "重开事件结束时仍有未平仓位", f"{list(left)}。管理线程会继续看守。", priority="max")


_TICK_LOCK = threading.RLock()


def run_event(settings, ev: Event, client=None, dry: bool | None = None, **kw) -> Runner:
    cf = cfg(settings)
    mode = get(settings)["mode"]
    dry = (mode != "live") if dry is None else dry
    c = client or _make_client(settings, need_trading=not dry or has_open(settings))
    r = Runner(settings, ev, c, cf, dry, **kw)
    r.run()
    return r


def manage_loop(settings, client=None, clock=time.time, sleep=time.sleep, max_s: float = 1500.0):
    """没有事件线程在跑、但账本里还有未平仓位（比如进程重启）时的看守线程。"""
    cf = cfg(settings)
    c = client or _make_client(settings, need_trading=True)
    t_end = clock() + max_s
    backoff = 0.0
    while clock() < t_end and has_open(settings):
        try:
            with _TICK_LOCK:
                manage_all(MCtx(settings, c, cf, int(clock() * 1000), False))
            backoff = 0.0
        except Exception as e:  # noqa: BLE001
            log.exception("reopen manage tick")
            if is_rate_limited(e):
                backoff = min(BACKOFF_MAX_S, max(cf["poll_s"], backoff * 2))
        sleep(cf["poll_s"] + backoff)


# ------------------------------------------------------------------ 调度器接入（和 pairs_exec 一样跑在主进程里）
_THREADS: dict = {}


def due(now: datetime, settings, done: set) -> list[str]:
    """事件名义时刻前 5 分钟起到后 3 分钟内，每个事件一个 key；有未平仓位时每分钟一个看守 key。mode=off 也唤醒（干跑写决策日志）。"""
    out: list[str] = []
    cf = cfg(settings)
    if cf["coins"] or any(cf["asia_coins"].values()):
        d0 = now.astimezone(ET).date()
        for i in (-1, 0, 1):
            for ev in events_on(d0 + timedelta(days=i), cf):
                nom = ev.nominal_ns / NS
                if nom - 300 <= now.timestamp() < nom + 180 and ev.key not in done:
                    out.append(ev.key)
        for k in [k for k in done if k.startswith("reopen:") and k.split(":")[2] < (d0 - timedelta(days=3)).isoformat()]:
            done.discard(k)
    slot = int(now.timestamp() // 60)
    if has_open(settings) and f"reopenmgr:{slot}" not in done:
        out.append(f"reopenmgr:{slot}")
    for k in [k for k in done if k.startswith("reopenmgr:") and int(k[10:]) < slot - 3]:
        done.discard(k)
    return out


def _alive(name: str) -> bool:
    t = _THREADS.get(name)
    return bool(t and t.is_alive())


def launch(settings, key: str):
    """调度器调用：立刻返回，真正的轮询在守护线程里（不占 job_loop，不拖慢周末策略 / 配对任务）。"""
    if key.startswith("reopenmgr:"):
        if has_open(settings) and not any(_alive(n) for n in list(_THREADS)):
            _spawn("mgr", lambda: manage_loop(settings), settings)
        return None
    ev = event_from_key(key, cfg(settings))
    if ev is None:
        log.warning("reopen: 找不到事件 %s", key)
        return None
    if not _alive(key):
        _spawn(key, lambda: run_event(settings, ev), settings)
    return None


def _spawn(name: str, fn, settings):
    def body():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log.exception("reopen thread %s", name)
            _push(settings, "重开执行线程出错", f"{name}: {type(e).__name__}: {e}"[:300], priority="high")
    t = threading.Thread(target=body, name=f"reopen-{name}", daemon=True)
    _THREADS[name] = t
    t.start()


# ------------------------------------------------------------------ CLI
def status_lines(settings) -> list[str]:
    cf, g, st = cfg(settings), get(settings), load_state(settings)
    m = MCtx(settings, None, cf, int(time.time() * 1000), True)
    realized_day, realized_cum = realized(st, _day_start_ms(m.now_ms)), realized(st)
    lines = [f"模式 {g['mode']}（off = 干跑，只写决策日志）；halt_entries={g['halt_entries']} {g.get('halt_reason') or ''}",
             f"币 {cf['coins']}，k={cf['k_bps']:g} bps（有效 k={eff_k_bps(cf, None):g}，下单时按实际 maker 费率往返+3 重算），每笔 ${cf['notional_usd']:.0f}，同时 ≤ {cf['max_concurrent']}，"
             f"当日止损 ${cf['daily_loss_stop_usd']:.0f}，累计止损 ${cf['cum_loss_stop_usd']:.0f}",
             f"已实现：今日 ${realized_day:+.2f}，累计 ${realized_cum:+.2f}（未平仓位的浮盈亏要连交易所才算）"]
    for pid, p in open_positions(st).items():
        lines.append(f"未平：{pid} {p['open']['coin']} 净仓 {p['net']:+g}，入场事件 {p['open']['event']}")
    for e in next_events(datetime.now(timezone.utc), cf)[:3]:
        lines.append(f"下一个事件：{e.key}  {datetime.fromtimestamp(e.nominal_ns / NS, ET):%m-%d %H:%M ET}  {e.names}")
    dec = [r for r in _led(settings).all() if r["kind"] == "decision"][-8:]
    for r in dec:
        lines.append(f"  决策 {datetime.fromtimestamp(r['t_ms'] / 1000, ET):%m-%d %H:%M:%S} {r['name']} {r['what']} {r.get('why') or ''}")
    return lines


def group_xyz(names: list[str], cf_coins: dict | None = None) -> dict:
    """币名分组：已配置的亚洲名 / 已知 xyz 清单（ASIA_COINS 证据）/ 其他。纯函数，便于测试。"""
    g: dict = {"已配置/已知 韩国": [], "已配置/已知 日本": [], "已配置/已知 香港": [], "已配置/已知 中国A股": [], "其他": []}
    lab = {"kr": "已配置/已知 韩国", "jp": "已配置/已知 日本", "hk": "已配置/已知 香港", "cn": "已配置/已知 中国A股"}
    known = {m: set(rs.ASIA_COINS.get(m, [])) | set((cf_coins or {}).get(m, [])) for m in lab}
    for n in sorted(names):
        base = n.split(":")[-1]
        m = next((m for m in lab if base in known[m]), None)
        g[lab[m] if m else "其他"].append(base)
    return g


def list_xyz() -> int:
    """只读：xyz dex meta（metaAndAssetCtxs）里的全部币名。不建钱包、不下单。"""
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=["", autotrade.DEX])
    mc = info.post("/info", {"type": "metaAndAssetCtxs", "dex": autotrade.DEX})
    names = [u["name"] for u in mc[0]["universe"] if not u.get("isDelisted")]
    for label, ns in group_xyz(names).items():
        if ns:
            print(f"{label} ({len(ns)}): {' '.join(ns)}")
    print(f"共 {len(names)} 个。把亚洲股票填进 config.toml [reopen] asia_coins = {{ kr=[...], jp=[...], hk=[...], cn=[...] }}")
    return 0


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description="重开 forced-flow 真钱执行器")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--list-xyz", action="store_true", help="只读：列出 xyz dex 所有币名（按市场分组），方便填 [reopen].asia_coins；不下单")
    ap.add_argument("--set-live", action="store_true", help="打开真下单（mode=live）")
    ap.add_argument("--set-off", action="store_true", help="关闭（mode=off，干跑；已有仓位仍会管理到平完）")
    ap.add_argument("--clear-halt", action="store_true", help="清除 halt_entries 标志（先核对持仓和账本）")
    ap.add_argument("--once", action="store_true", help="现在就跑一个事件做干跑测试：名义时刻 = 现在 + pre_s，强制不下单，跑到窗口结束")
    a = ap.parse_args(argv)
    if a.list_xyz:
        return list_xyz()
    s = config.load()
    if a.set_live and a.set_off:
        ap.error("--set-live 和 --set-off 不能一起用")
    if a.set_live or a.set_off:
        put(s, mode="live" if a.set_live else "off", updated_by="cli")
        print(f"重开执行器模式已切到 {get(s)['mode']}")
    if a.clear_halt:
        put(s, halt_entries=False, halt_reason="")
        print("已清除 halt_entries")
    if a.once:
        cf = cfg(s)
        nom = (int(time.time()) + int(cf["pre_s"])) * NS
        ev = Event("test", datetime.now(ET).date(), nom, [c for c in cf["coins"]])
        r = run_event(s, ev, dry=True)
        print(json.dumps({"final": r.final, "max_overshoot_bps": r.max_ovr}, ensure_ascii=False, indent=1))
    if a.status or not (a.set_live or a.set_off or a.clear_halt or a.once):
        print("\n".join(status_lines(s)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
