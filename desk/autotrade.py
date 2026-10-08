"""自动下单（真钱）。在网站「设置 → 自动下单」里由账户主人打开；三种模式：
- off：不下单（默认）
- shadow（演习）：到点算好要下的单、推送"本来会下什么"，但不发给交易所
- live（真下单）：按下面的流程真的下单

流程（每个合约、每个周末）：
1. 锁定预测后（重开前 15 分钟）：如果规则说要出手、风控 agent 没有否决、规则没作废 → 挂"只做 maker"的限价单
   （买单挂在买一价、卖单挂在卖一价，不吃单）。仓位 = 本金上限 ÷ 合约数，且不超过盘口深度 10%，杠杆 1 倍。
2. 重开前 1 分钟：没成交的挂单全部撤掉（没成交就当没做）。
3. 重开后 5 分钟：有仓位就市价平掉，记录这一笔的真实盈亏。
4. 累计真实亏损超过"最多亏"→ 自动切回 off 并推送警报。

私钥只用 Hyperliquid 的 API 钱包（只能交易，不能提币），存在服务器 .env 里；网页只显示开头和结尾。
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path

from . import notify
from .ledger import Ledger

log = logging.getLogger("autotrade")
MODES = ("off", "shadow", "live")
DEX = "xyz"


# ------------------------------------------------------------------ settings stored on the server
def _path(settings) -> Path:
    p = Path(settings.data) / "state" / "live.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get(settings) -> dict:
    d = {"mode": "off", "capital_usd": 200.0, "max_loss_usd": 50.0, "updated_by": "", "ts": 0}
    try:
        d.update(json.loads(_path(settings).read_text()))
    except (OSError, ValueError):
        pass
    return d


def put(settings, **kw):
    d = get(settings)
    d.update(kw)
    d["ts"] = time.time()
    _path(settings).write_text(json.dumps(d, ensure_ascii=False))
    return d


def creds() -> tuple[str, str]:
    return os.environ.get("HL_ACCOUNT_ADDRESS", ""), os.environ.get("HL_API_PRIVATE_KEY", "")


def valid_address(a: str) -> bool:
    return isinstance(a, str) and len(a) == 42 and a.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in a[2:])


def valid_key(k: str) -> bool:
    k = k[2:] if k.startswith("0x") else k
    return len(k) == 64 and all(c in "0123456789abcdefABCDEF" for c in k)


# ------------------------------------------------------------------ exchange client (injectable for tests)
class Client:
    """薄封装：只用到这几个功能。"""

    def __init__(self, address: str, key: str):
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        self.address = address
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=["", DEX])
        self.ex = Exchange(Account.from_key(key), constants.MAINNET_API_URL, account_address=address, perp_dexs=["", DEX])

    def balance(self) -> dict:
        main = self.info.user_state(self.address)
        dex = self.info.user_state(self.address, dex=DEX)
        f = lambda s: float(s.get("marginSummary", {}).get("accountValue", 0) or 0)  # noqa: E731
        return {"main": f(main), "xyz": f(dex), "withdrawable_xyz": float(dex.get("withdrawable", 0) or 0)}

    def sz_decimals(self, coin: str) -> int:
        return self.info.asset_to_sz_decimals[self.info.name_to_asset(coin)]

    def position(self, coin: str) -> float:
        st = self.info.user_state(self.address, dex=DEX)
        for p in st.get("assetPositions", []):
            pos = p.get("position", {})
            if pos.get("coin") == coin:
                return float(pos.get("szi", 0) or 0)
        return 0.0

    def set_leverage(self, coin: str, lev: int):
        return self.ex.update_leverage(lev, coin, is_cross=False)

    def post_only(self, coin: str, is_buy: bool, sz: float, px: float):
        return self.ex.order(coin, is_buy, sz, px, {"limit": {"tif": "Alo"}})

    def open_orders(self, coin: str) -> list[dict]:
        return [o for o in self.info.open_orders(self.address, dex=DEX) if o.get("coin") == coin]

    def cancel(self, coin: str, oid: int):
        return self.ex.cancel(coin, oid)

    def close(self, coin: str):
        return self.ex.market_close(coin, slippage=0.02)

    def fills(self, coin: str, since_ms: int) -> list[dict]:
        return [f for f in self.info.user_fills(self.address) if f.get("coin") == coin and f.get("time", 0) >= since_ms]


_client_factory = None   # tests replace this


def client():
    if _client_factory:
        return _client_factory()
    a, k = creds()
    if not (valid_address(a) and valid_key(k)):
        raise RuntimeError("还没有在设置里填 Hyperliquid 钱包地址和 API 私钥")
    return Client(a, k)


# ------------------------------------------------------------------ helpers
def round_px(px: float, sz_dec: int) -> float:
    """Hyperliquid 永续：最多 5 位有效数字，且小数位不超过 6 - szDecimals。"""
    if px <= 0:
        return px
    dec = max(0, 6 - sz_dec)
    sig = 5 - int(math.floor(math.log10(px))) - 1
    return round(px, max(0, min(dec, sig)))


def round_sz(sz: float, sz_dec: int) -> float:
    q = 10 ** sz_dec
    return math.floor(sz * q) / q


def _led(settings) -> Ledger:
    return Ledger(settings.data, "live")


def realized_total(settings) -> float:
    return sum(r.get("pnl_usd", 0) or 0 for r in _led(settings).all() if r["kind"] == "exit")


def _vetoed(settings, name: str) -> str | None:
    """风控 agent 最近 24 小时的否决。"""
    try:
        from . import agents
        r = agents.last_full_run(settings, within_h=24)
    except Exception:  # noqa: BLE001
        return None
    if not r:
        return None
    for v in r.get("rooms", {}).get("risk", {}).get("synth", {}).get("veto") or []:
        if isinstance(v, dict) and str(v.get("name", "")).upper() == name.upper():
            return str(v.get("why", ""))
    return None


# ------------------------------------------------------------------ the three steps
def entry(settings, w, lock: dict | None) -> dict | None:
    cfg = get(settings)
    if cfg["mode"] == "off" or not lock:
        return None
    led = _led(settings)
    if any(r["kind"] == "entry" and r["weekend"] == w.wid for r in led.all()):
        return None
    a = lock.get("action", {})
    name, coin = w.name or lock.get("name"), w.coin or lock.get("coin")
    base = {"mode": cfg["mode"], "coin": coin, "name": name}
    if lock.get("void"):
        return led.append("entry", w.wid, {**base, "skipped": "本周规则作废：" + "；".join(lock.get("void_reasons", []))})
    if not a.get("side"):
        return led.append("entry", w.wid, {**base, "skipped": a.get("reason", "规则说不出手")})
    veto = _vetoed(settings, name)
    if veto:
        return led.append("entry", w.wid, {**base, "skipped": f"风控否决：{veto}"})
    lost = realized_total(settings)
    if lost <= -cfg["max_loss_usd"]:
        put(settings, mode="off")
        notify.push(settings, "自动下单已停止", f"累计真实亏损 ${-lost:.2f}，超过上限 ${cfg['max_loss_usd']:.0f}", priority="high")
        return led.append("entry", w.wid, {**base, "skipped": "累计亏损超过上限，已自动关闭"})
    n = max(1, len(settings.instruments))
    notional = cfg["capital_usd"] / n
    feats = lock.get("features", {})
    depth = min([d for d in (feats.get("bid_depth_usd"), feats.get("ask_depth_usd")) if d] or [0])
    if depth:
        notional = min(notional, depth * settings["risk"]["max_depth_fraction"])
    is_buy = a["side"] > 0
    px_raw = feats.get("best_bid" if is_buy else "best_ask") or a.get("entry_px") or feats.get("dec_mid")
    plan = {**base, "side": "买" if is_buy else "卖", "notional_usd": round(notional, 2), "px": px_raw,
            "reason": a.get("reason", "")}
    if cfg["mode"] == "shadow":
        rec = led.append("entry", w.wid, {**plan, "sent": False})
        notify.push(settings, f"演习：{name} 本来会{plan['side']}", f"约 ${notional:.0f}，限价 {px_raw}（演习模式，没有真下单）")
        return rec
    try:
        c = client()
        dec = c.sz_decimals(coin)
        px = round_px(float(px_raw), dec)
        sz = round_sz(notional / px, dec)
        if sz <= 0:
            return led.append("entry", w.wid, {**plan, "skipped": "金额太小，凑不够最小下单单位"})
        c.set_leverage(coin, 1)
        resp = c.post_only(coin, is_buy, sz, px)
        rec = led.append("entry", w.wid, {**plan, "sent": True, "sz": sz, "px": px, "resp": resp, "t_ms": int(time.time() * 1000)})
        notify.push(settings, f"已挂单：{name} {plan['side']}", f"{sz} 张 @ {px}（约 ${notional:.0f}），开盘前没成交会自动撤单", priority="high")
        return rec
    except Exception as e:  # noqa: BLE001
        log.exception("entry failed")
        notify.push(settings, f"挂单失败：{name}", str(e)[:300], priority="high")
        return led.append("entry", w.wid, {**plan, "sent": False, "error": str(e)[:300]})


def cancel_unfilled(settings, w) -> dict | None:
    led = _led(settings)
    ent = next((r for r in led.all() if r["kind"] == "entry" and r["weekend"] == w.wid), None)
    if not ent or not ent.get("sent") or any(r["kind"] == "cancel" and r["weekend"] == w.wid for r in led.all()):
        return None
    try:
        c = client()
        n = 0
        for o in c.open_orders(ent["coin"]):
            c.cancel(ent["coin"], o["oid"])
            n += 1
        pos = c.position(ent["coin"])
        return led.append("cancel", w.wid, {"coin": ent["coin"], "cancelled": n, "position": pos})
    except Exception as e:  # noqa: BLE001
        notify.push(settings, f"撤单失败：{ent['name']}", str(e)[:300], priority="high")
        return led.append("cancel", w.wid, {"coin": ent["coin"], "error": str(e)[:300]})


def exit_(settings, w) -> dict | None:
    led = _led(settings)
    ent = next((r for r in led.all() if r["kind"] == "entry" and r["weekend"] == w.wid), None)
    if not ent or not ent.get("sent") or any(r["kind"] == "exit" and r["weekend"] == w.wid for r in led.all()):
        return None
    try:
        c = client()
        pos = c.position(ent["coin"])
        resp = c.close(ent["coin"]) if pos else None
        time.sleep(2 if pos and not _client_factory else 0)
        fills = c.fills(ent["coin"], ent.get("t_ms", 0) - 60_000)
        pnl = sum(float(f.get("closedPnl", 0) or 0) - float(f.get("fee", 0) or 0) for f in fills)
        rec = led.append("exit", w.wid, {"coin": ent["coin"], "name": ent["name"], "position_before": pos, "resp": resp,
                                         "n_fills": len(fills), "pnl_usd": round(pnl, 4)})
        msg = (f"{ent['name']}：没有成交，不赚不亏" if not fills else
               f"{ent['name']}：已平仓，这一笔真实盈亏 ${pnl:+.2f}（含手续费）。累计 ${realized_total(settings):+.2f}")
        notify.push(settings, "自动下单结果", msg, priority="high")
        return rec
    except Exception as e:  # noqa: BLE001
        notify.push(settings, f"平仓失败，请立刻去 Hyperliquid 手动平仓：{ent['name']}", str(e)[:300], priority="max")
        return led.append("exit", w.wid, {"coin": ent["coin"], "error": str(e)[:300]})


def summary(settings) -> dict:
    rows = _led(settings).all()
    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(r["weekend"], {})[r["kind"]] = r
    return {"total_usd": realized_total(settings), "weekends": by}
