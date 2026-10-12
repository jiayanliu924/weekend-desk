"""仓位看守（#12）：盯住账户里的**所有**仓位——包括你自己手动开的——你不用再盯 Hyperliquid、不用再发截图。

做什么：
1. 每 30 秒（config `[positions] poll_sec`）用 Hyperliquid 公开的 info 接口（只读，不用私钥）读一次账户：
   仓位（clearinghouseState）、挂单（openOrders / frontendOpenOrders，看 TP/SL 触发单）、标记价。
   每次快照原样存进原始层 `hl_positions`；有仓位的合约再单独存一条 30 秒标记价到 `hl_positions_mark`
   （10/11 的教训：没有逐笔价格，事后说不清 SL 100.12 到底有没有被打到）。
2. 登记簿 data/state/positions.json：不是 bot 自己开的仓位一律自动登记为 external（source=manual）。
   每个仓位可以设规则：SL、软下限、软上限、停留分钟、退出时刻、开仓理由、事件状态。
   命令行：python -m desk.positions set BRENTOIL --sl 100.12 --soft-low 101.5 --soft-high 102.5 --dwell-min 10 \
             --exit-at "2026-10-12T11:30-07:00" --open-rule "..." --event-status ongoing
   网页：设置页"仓位规则"表单。
3. 规则引擎（纯函数、有单元测试、不联网）：快照 + 规则 → 事件：
   SOFT_LOW_BROKEN（跌破软下限并停留 ≥ 停留分钟）、SOFT_HIGH_RECLAIMED（收复软上限）、EXIT_TIME_REACHED（到退出时刻）、
   SL_LIKELY_HIT（仓位消失且最后标记价已过 SL）、POSITION_CLOSED（仓位消失但没过 SL）、NEW_EXTERNAL_POSITION（新出现的手动仓位）。
   每个状态转换只提醒一次（状态记在登记簿里，重启不重复）。
   event_status=ongoing（事件还在进行中）：停留时间翻倍，且除非价格已到 SL 的 0.5% 以内，否则不发软下限提醒（10/11 教训）。
4. 提醒走 desk.notify.push（和周报同一频道），大白话：合约、方向、数量、入场价、标记价、浮盈亏、哪条规则、该怎么做。
   **只提醒，绝不下单。** close_reduce_only() 只是占位，等账户主人和老板都点头、并且换成"只能交易"的 API 钱包后再实现。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import notify

log = logging.getLogger("positions")
INFO_URL = "https://api.hyperliquid.xyz/info"
STREAM = "hl_positions"
MARK_STREAM = "hl_positions_mark"
DEXS = ("", "xyz")
EVENTS = ("SOFT_LOW_BROKEN", "SOFT_HIGH_RECLAIMED", "EXIT_TIME_REACHED", "SL_LIKELY_HIT", "POSITION_CLOSED",
          "NEW_EXTERNAL_POSITION")
EVENT_STATUS = ("", "ongoing", "concluded")
RULE_KEYS = ("sl", "soft_low", "soft_high", "dwell_min", "exit_at", "open_rule", "event_status")
SL_NEAR_FRAC = 0.005      # ongoing 模式下：价格离 SL 不到 0.5% 才发软下限提醒
SL_HIT_TOL_FRAC = 0.003   # 仓位消失时最后标记价离 SL 不到 0.3% 也算"很可能被 SL 打掉"


# ------------------------------------------------------------------ settings / registry
def cfg(settings) -> dict:
    d = {"poll_sec": 30, "flush_every_polls": 10}
    try:
        d.update(settings["positions"])
    except (KeyError, TypeError):
        pass
    return d


def address() -> str:
    from .autotrade import creds, valid_address
    a, _ = creds()
    return a if valid_address(a) else ""


def _path(settings) -> Path:
    p = Path(settings.data) / "state" / "positions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load(settings) -> dict:
    try:
        d = json.loads(_path(settings).read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault("positions", {})
    return d


def save(settings, reg: dict) -> None:
    reg["ts"] = time.time()
    tmp = _path(settings).with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(_path(settings))


def resolve_coin(settings, name_or_coin: str) -> str:
    """BRENTOIL → xyz:BRENTOIL（按 config 里的合约表）；找不到就原样返回。"""
    try:
        i = settings.inst(name_or_coin)
    except Exception:  # noqa: BLE001
        i = None
    return i["coin"] if i else name_or_coin


def short_name(coin: str) -> str:
    return coin.split(":")[-1]


def parse_rule(**kw) -> dict:
    """把命令行/表单字段整理成规则；空字符串 = 不设。时间统一存成带时区的 ISO 和毫秒。"""
    r: dict = {}
    for k in ("sl", "soft_low", "soft_high"):
        v = kw.get(k)
        if v not in (None, ""):
            r[k] = float(v)
    v = kw.get("dwell_min")
    if v not in (None, ""):
        r["dwell_min"] = max(0.0, float(v))
    v = kw.get("exit_at")
    if v not in (None, ""):
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("exit_at 必须带时区，例如 2026-10-12T11:30-07:00")
        r["exit_at"] = dt.isoformat()
        r["exit_at_ms"] = int(dt.timestamp() * 1000)
    v = kw.get("open_rule")
    if v not in (None, ""):
        r["open_rule"] = str(v).strip()
    v = kw.get("event_status")
    if v not in (None, ""):
        if v not in EVENT_STATUS:
            raise ValueError(f"event_status 只能是 ongoing 或 concluded，不是 {v!r}")
        r["event_status"] = v
    return r


def set_rule(settings, name_or_coin: str, clear: bool = False, **kw) -> dict:
    """设置/更新一个仓位的规则。仓位还没出现时也可以先设（登记为 pending，等它出现）。"""
    coin = resolve_coin(settings, name_or_coin)
    reg = load(settings)
    ent = reg["positions"].setdefault(coin, {"coin": coin, "name": short_name(coin), "source": "manual", "open": False,
                                            "rule": {}, "state": {}})
    rule = {} if clear else dict(ent.get("rule", {}))
    rule.update(parse_rule(**kw))
    ent["rule"] = rule
    # 规则变了，让和规则相关的"已提醒过"标记重新来（新的规则 = 新的状态机）
    st = ent.setdefault("state", {})
    for k in ("below_low_since_ms", "low_fired", "above_high", "exit_fired"):
        st.pop(k, None)
    ent["rule_updated_at"] = _iso(int(time.time() * 1000))
    save(settings, reg)
    return ent


# ------------------------------------------------------------------ fetch (read-only, public API)
def fetch_raw(addr: str, post=None) -> dict:
    """读公开接口。post(json) → dict/list；默认 httpx。只读，没有任何签名。"""
    if post is None:
        import httpx
        post = lambda body: httpx.post(INFO_URL, json=body, timeout=20).json()  # noqa: E731
    raw: dict = {"t_ms": int(time.time() * 1000), "address": addr, "states": {}, "orders": {}, "front": {}, "ctxs": {}}
    for dex in DEXS:
        extra = {"dex": dex} if dex else {}
        raw["states"][dex] = post({"type": "clearinghouseState", "user": addr, **extra})
        try:
            raw["orders"][dex] = post({"type": "openOrders", "user": addr, **extra})
        except Exception as e:  # noqa: BLE001
            raw["orders"][dex] = {"error": str(e)[:200]}
        try:
            raw["front"][dex] = post({"type": "frontendOpenOrders", "user": addr, **extra})
        except Exception as e:  # noqa: BLE001
            raw["front"][dex] = {"error": str(e)[:200]}
        try:
            raw["ctxs"][dex] = post({"type": "metaAndAssetCtxs", **extra})
        except Exception as e:  # noqa: BLE001
            raw["ctxs"][dex] = {"error": str(e)[:200]}
    return raw


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_snapshot(raw: dict) -> dict:
    """原始接口返回 → 统一结构：
    {"t_ms", "positions": {coin: {coin, name, side, size, entry_px, mark_px, unrealized_usd, liq_px, leverage}},
     "marks": {coin: mark_px}, "tpsl": {coin: {"tp": px|None, "sl": px|None}}}"""
    t_ms = int(raw.get("t_ms") or time.time() * 1000)
    marks: dict[str, float] = {}
    for dex, mc in (raw.get("ctxs") or {}).items():
        if not isinstance(mc, list) or len(mc) < 2:
            continue
        uni, ctx = mc[0].get("universe", []), mc[1]
        for u, c in zip(uni, ctx):
            m = _f(c.get("markPx"))
            if m is not None:
                marks[u["name"]] = m
    positions: dict[str, dict] = {}
    for dex, st in (raw.get("states") or {}).items():
        if not isinstance(st, dict):
            continue
        for ap in st.get("assetPositions", []) or []:
            p = ap.get("position", {}) if isinstance(ap, dict) else {}
            coin, szi = p.get("coin"), _f(p.get("szi"), 0.0)
            if not coin or not szi:
                continue
            lev = p.get("leverage") or {}
            positions[coin] = {
                "coin": coin, "name": short_name(coin), "side": "long" if szi > 0 else "short", "size": abs(szi),
                "entry_px": _f(p.get("entryPx")), "mark_px": marks.get(coin),
                "unrealized_usd": _f(p.get("unrealizedPnl"), 0.0), "liq_px": _f(p.get("liquidationPx")),
                "leverage": lev.get("value") if isinstance(lev, dict) else lev, "dex": dex,
            }
    tpsl: dict[str, dict] = {}
    for dex, fo in (raw.get("front") or {}).items():
        if not isinstance(fo, list):
            continue
        for o in fo:
            if not isinstance(o, dict) or not o.get("isTrigger"):
                continue
            kind = str(o.get("orderType", ""))
            slot = tpsl.setdefault(o.get("coin", ""), {"tp": None, "sl": None})
            px = _f(o.get("triggerPx"))
            if "Stop" in kind:
                slot["sl"] = px
            elif "Take Profit" in kind:
                slot["tp"] = px
    return {"t_ms": t_ms, "positions": positions, "marks": marks, "tpsl": tpsl}


# ------------------------------------------------------------------ rule engine (pure)
def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def effective_dwell_ms(rule: dict) -> int:
    d = float(rule.get("dwell_min", 10) or 0)
    if rule.get("event_status") == "ongoing":
        d *= 2
    return int(d * 60_000)


def near_sl(mark: float, sl: float | None, side: str, frac: float = SL_NEAR_FRAC) -> bool:
    if sl is None or mark is None:
        return False
    return mark <= sl * (1 + frac) if side == "long" else mark >= sl * (1 - frac)


def past_sl(mark: float | None, sl: float | None, side: str, tol: float = SL_HIT_TOL_FRAC) -> bool:
    if sl is None or mark is None:
        return False
    return mark <= sl * (1 + tol) if side == "long" else mark >= sl * (1 - tol)


def step(entry: dict, pos: dict | None, mark: float | None, now_ms: int) -> tuple[dict, list[dict]]:
    """一个已登记仓位走一步：(登记项, 当前快照里的仓位或 None, 标记价, 现在) → (新登记项, 事件列表)。
    纯函数：不读文件、不联网。每种转换只发一次（标记记在 state 里）。"""
    e = json.loads(json.dumps(entry))   # deep copy
    rule, st = e.setdefault("rule", {}), e.setdefault("state", {})
    events: list[dict] = []
    side = (pos or e).get("side", "long")

    def fire(kind: str, **extra):
        ev = {"kind": kind, "coin": e["coin"], "name": e.get("name", short_name(e["coin"])), "t_ms": now_ms,
              "at": _iso(now_ms), "side": side, "size": (pos or e).get("size"), "entry_px": (pos or e).get("entry_px"),
              "mark_px": mark if mark is not None else st.get("last_mark"),
              "unrealized_usd": (pos or {}).get("unrealized_usd"), "rule": {k: rule.get(k) for k in RULE_KEYS if rule.get(k) is not None},
              **extra}
        events.append(ev)
        e["last_event"] = ev

    if pos is None:
        if e.get("open"):
            e["open"] = False
            e["closed_at"] = _iso(now_ms)
            last = st.get("last_mark")
            if past_sl(last, rule.get("sl"), side):
                fire("SL_LIKELY_HIT", last_mark=last)
            else:
                fire("POSITION_CLOSED", last_mark=last)
        return e, events

    # 仓位在：同步快照里的事实
    if not e.get("open"):
        e["open"] = True
        e["first_seen"] = e.get("first_seen") or _iso(now_ms)
        e.pop("closed_at", None)
        st.pop("below_low_since_ms", None)
        st.pop("low_fired", None)
        st.pop("exit_fired", None)
        st.pop("above_high", None)
    for k in ("side", "size", "entry_px", "mark_px", "unrealized_usd", "liq_px", "leverage"):
        if k in pos:
            e[k] = pos[k]
    e["last_seen"] = _iso(now_ms)
    if mark is None:
        return e, events
    st["last_mark"], st["last_mark_ms"] = mark, now_ms

    # 软下限：跌破并停留 ≥ dwell
    low = rule.get("soft_low")
    if low is not None:
        if mark < low:
            since = st.setdefault("below_low_since_ms", now_ms)
            held = now_ms - since
            if held >= effective_dwell_ms(rule) and not st.get("low_fired"):
                suppressed = rule.get("event_status") == "ongoing" and not near_sl(mark, rule.get("sl"), side)
                if not suppressed:
                    st["low_fired"] = True
                    fire("SOFT_LOW_BROKEN", dwell_min=held / 60_000, soft_low=low)
        else:
            st.pop("below_low_since_ms", None)
            st["low_fired"] = False
    # 软上限：从下面收复（第一次见到时只记状态，不提醒）
    high = rule.get("soft_high")
    if high is not None:
        above = mark >= high
        prev = st.get("above_high")
        if prev is False and above:
            fire("SOFT_HIGH_RECLAIMED", soft_high=high)
        st["above_high"] = above
    # 退出时刻
    ex = rule.get("exit_at_ms")
    if ex is not None and now_ms >= ex and not st.get("exit_fired"):
        st["exit_fired"] = True
        fire("EXIT_TIME_REACHED", exit_at=rule.get("exit_at"))
    return e, events


def process(reg: dict, snap: dict, bot_coins: set[str] | frozenset[str] = frozenset(), now_ms: int | None = None) -> tuple[dict, list[dict]]:
    """整本登记簿走一步：登记新出现的手动仓位、更新已登记的、收集事件。纯函数。"""
    now_ms = now_ms if now_ms is not None else snap["t_ms"]
    reg = json.loads(json.dumps(reg))
    reg.setdefault("positions", {})
    events: list[dict] = []
    snap_pos, marks = snap.get("positions", {}), snap.get("marks", {})
    for coin, p in snap_pos.items():
        if coin not in reg["positions"]:
            reg["positions"][coin] = {"coin": coin, "name": short_name(coin), "source": "bot" if coin in bot_coins else "manual",
                                      "open": False, "rule": {}, "state": {}}
            is_new_external = coin not in bot_coins
        else:
            ent = reg["positions"][coin]
            is_new_external = not ent.get("open") and ent.get("source") != "bot"
            if ent.get("source") != "bot" and coin in bot_coins:
                ent["source"] = "bot"
                is_new_external = False
        ent, evs = step(reg["positions"][coin], p, marks.get(coin, p.get("mark_px")), now_ms)
        if is_new_external:
            ev = {"kind": "NEW_EXTERNAL_POSITION", "coin": coin, "name": short_name(coin), "t_ms": now_ms, "at": _iso(now_ms),
                  "side": p.get("side"), "size": p.get("size"), "entry_px": p.get("entry_px"), "mark_px": ent.get("mark_px"),
                  "unrealized_usd": p.get("unrealized_usd"), "rule": {k: ent["rule"].get(k) for k in RULE_KEYS if ent["rule"].get(k) is not None},
                  "tpsl": snap.get("tpsl", {}).get(coin)}
            evs = [ev] + evs
            ent["last_event"] = evs[-1]
        ent["tpsl"] = snap.get("tpsl", {}).get(coin)
        reg["positions"][coin] = ent
        events += evs
    for coin, ent in list(reg["positions"].items()):
        if coin not in snap_pos and ent.get("open"):
            ent, evs = step(ent, None, marks.get(coin), now_ms)
            reg["positions"][coin] = ent
            events += evs
    return reg, events


# ------------------------------------------------------------------ messages (plain Chinese)
def advice(ev: dict) -> str:
    k, side = ev["kind"], ev.get("side", "long")
    if k == "SOFT_LOW_BROKEN":
        return "按规则该市价平" if side == "long" else "空头：跌破对你有利，拿着；但别忘了你自己设的 SL"
    if k == "SOFT_HIGH_RECLAIMED":
        return "拿着（已收复软上限）" if side == "long" else "空头：价格收复软上限对你不利，按规则该市价平"
    if k == "EXIT_TIME_REACHED":
        return "到退出时刻，该走了"
    if k == "SL_LIKELY_HIT":
        return "仓位已不在，很可能被 SL 打掉了；去 Hyperliquid 确认成交记录"
    if k == "POSITION_CLOSED":
        return "仓位已不在（最后标记价没到 SL），应该是手动平的或被 TP 带走；去确认一下"
    if k == "NEW_EXTERNAL_POSITION":
        return "这是手动开的仓，程序只盯不动。请给它设规则：python -m desk.positions set <合约> --sl … 或网页设置页"
    return "拿着"


TITLES = {"SOFT_LOW_BROKEN": "跌破软下限", "SOFT_HIGH_RECLAIMED": "收复软上限", "EXIT_TIME_REACHED": "到退出时刻",
          "SL_LIKELY_HIT": "仓位消失，疑似止损", "POSITION_CLOSED": "仓位已平", "NEW_EXTERNAL_POSITION": "发现新的手动仓位"}


def _px(x):
    return "—" if x is None else (f"{x:,.4g}" if abs(x) < 1 else f"{x:,.2f}")


def message(ev: dict) -> tuple[str, str, str]:
    """→ (标题, 正文, 优先级)。大白话，不用术语。"""
    k = ev["kind"]
    side = "多" if ev.get("side") == "long" else "空"
    rule = ev.get("rule", {})
    why = {
        "SOFT_LOW_BROKEN": f"价格跌破软下限 {_px(rule.get('soft_low'))} 并停留了 {ev.get('dwell_min', 0):.0f} 分钟" + ("（事件进行中，停留时间已翻倍）" if rule.get("event_status") == "ongoing" else ""),
        "SOFT_HIGH_RECLAIMED": f"价格从下面重新站上软上限 {_px(rule.get('soft_high'))}",
        "EXIT_TIME_REACHED": f"到了你定的退出时刻 {rule.get('exit_at', '')}",
        "SL_LIKELY_HIT": f"仓位不见了，最后一次标记价 {_px(ev.get('last_mark'))}，SL 设在 {_px(rule.get('sl'))}",
        "POSITION_CLOSED": f"仓位不见了，最后一次标记价 {_px(ev.get('last_mark'))}" + (f"，SL 设在 {_px(rule.get('sl'))}" if rule.get("sl") is not None else ""),
        "NEW_EXTERNAL_POSITION": "账户里出现了一个不是 bot 开的仓位" + (f"，交易所挂着 TP {_px(ev['tpsl'].get('tp'))} / SL {_px(ev['tpsl'].get('sl'))}" if ev.get("tpsl") else "，交易所没挂 TP/SL"),
    }.get(k, k)
    u = ev.get("unrealized_usd")
    body = (f"{ev.get('name')} 做{side} {ev.get('size') or '—'} 张，入场 {_px(ev.get('entry_px'))}，现在标记价 {_px(ev.get('mark_px'))}，"
            f"浮动盈亏 {'—' if u is None else f'${u:+.2f}'}\n"
            f"触发：{why}\n"
            f"该怎么做：{advice(ev)}\n"
            + (f"开仓理由：{rule['open_rule']}\n" if rule.get("open_rule") else "")
            + "（程序只提醒，不会替你下单）")
    pri = "max" if k in ("SL_LIKELY_HIT",) else "high" if k in ("SOFT_LOW_BROKEN", "EXIT_TIME_REACHED") else "default"
    return f"仓位看守：{ev.get('name')} {TITLES.get(k, k)}", body, pri


# ------------------------------------------------------------------ the poll (side effects live here only)
_writers: dict = {}
_polls = [0]


def _writer(settings, stream: str):
    from .rawstore import RawWriter
    key = (str(settings.data), stream)
    if key not in _writers:
        _writers[key] = RawWriter(settings.data, stream)
    return _writers[key]


def bot_coins(settings) -> set[str]:
    """bot 自己开着的仓位（live 账里有 entry 已发出、还没 exit 的合约）。"""
    try:
        from .autotrade import _led
        rows = _led(settings).all()
    except Exception:  # noqa: BLE001
        return set()
    out = set()
    for r in rows:
        if r["kind"] == "entry" and r.get("sent") and not any(x["kind"] == "exit" and x["weekend"] == r["weekend"] for x in rows):
            out.add(r["coin"])
    return out


def poll(settings, post=None, now_ms: int | None = None, push=True) -> dict | None:
    """一次完整循环：读接口 → 存原始快照和标记价 → 走规则 → 推送。没填钱包地址时什么都不做。"""
    addr = address()
    if not addr:
        return None
    raw = fetch_raw(addr, post)
    if now_ms is not None:
        raw["t_ms"] = now_ms
    snap = parse_snapshot(raw)
    w_snap, w_mark = _writer(settings, STREAM), _writer(settings, MARK_STREAM)
    w_snap.add("rest", addr, raw, snap["t_ms"] * 1_000_000)
    for coin in snap["positions"]:
        m = snap["marks"].get(coin)
        if m is not None:
            w_mark.add("rest", coin, {"coin": coin, "mark_px": m, "t_ms": snap["t_ms"]}, snap["t_ms"] * 1_000_000)
    reg, events = process(load(settings), snap, bot_coins(settings), snap["t_ms"])
    reg["last_poll"] = _iso(snap["t_ms"])
    reg["n_open"] = len(snap["positions"])
    save(settings, reg)
    _polls[0] += 1
    if _polls[0] % cfg(settings)["flush_every_polls"] == 0 or events:
        flush()
    if push:
        for ev in events:
            title, body, pri = message(ev)
            notify.push(settings, title, body, priority=pri)
    return {"snapshot": snap, "events": events}


def flush() -> None:
    for w in list(_writers.values()):
        try:
            w.flush()
        except Exception as e:  # noqa: BLE001
            log.error("flush %s failed: %s", w.stream, e)


async def run(settings):
    """调度器里的常驻任务：每 poll_sec 秒看一次。没地址时每分钟再检查一次有没有填。"""
    import asyncio
    while True:
        every = cfg(settings)["poll_sec"]
        try:
            if address():
                await asyncio.to_thread(poll, settings)
            else:
                every = max(every, 60)
        except Exception as e:  # noqa: BLE001
            log.warning("positions poll failed: %s", e)
        await asyncio.sleep(every)


def close_reduce_only(settings, coin: str):
    """占位：按规则市价平掉一个手动仓位（reduce-only）。
    **没有实现，也不要实现**——要等账户主人和老板两个人都书面同意，并且服务器上换成"只能交易、不能提币"的 API 钱包后，
    再在这里接 autotrade.Client.close()。现在的看守只提醒、不碰钱。"""
    raise NotImplementedError("仓位看守只提醒不下单：平仓功能需账户主人 + 老板双重批准，且只能用只交易的 API 钱包")


# ------------------------------------------------------------------ CLI
def main(argv: list[str]) -> int:
    from . import config
    ap = argparse.ArgumentParser(prog="python -m desk.positions", description="仓位看守：查看/设置手动仓位的规则")
    sub = ap.add_subparsers(dest="cmd")
    st = sub.add_parser("set", help="设置一个仓位的规则（没写的字段保持不变）")
    st.add_argument("coin", help="合约名，如 BRENTOIL 或 xyz:BRENTOIL")
    st.add_argument("--sl", type=float)
    st.add_argument("--soft-low", dest="soft_low", type=float)
    st.add_argument("--soft-high", dest="soft_high", type=float)
    st.add_argument("--dwell-min", dest="dwell_min", type=float)
    st.add_argument("--exit-at", dest="exit_at", help='带时区的时间，如 "2026-10-12T11:30-07:00"')
    st.add_argument("--open-rule", dest="open_rule")
    st.add_argument("--event-status", dest="event_status", choices=["ongoing", "concluded"])
    st.add_argument("--clear", action="store_true", help="先清空旧规则再设")
    sub.add_parser("list", help="列出登记簿")
    pl = sub.add_parser("poll", help="手动读一次账户（需要填了钱包地址）")
    pl.add_argument("--no-push", action="store_true")
    a = ap.parse_args(argv)
    s = config.load()
    if a.cmd == "set":
        ent = set_rule(s, a.coin, clear=a.clear, sl=a.sl, soft_low=a.soft_low, soft_high=a.soft_high, dwell_min=a.dwell_min,
                       exit_at=a.exit_at, open_rule=a.open_rule, event_status=a.event_status)
        print(json.dumps(ent, ensure_ascii=False, indent=1))
    elif a.cmd == "list":
        reg = load(s)
        print(f"最近一次读取：{reg.get('last_poll', '—')}，开着的仓位 {reg.get('n_open', 0)} 个")
        for coin, e in sorted(reg["positions"].items()):
            print(f"- {e.get('name', coin)} [{e.get('source')}] {'开着' if e.get('open') else '已平/未出现'} "
                  f"{e.get('side', '')} {e.get('size', '')} @ {e.get('entry_px', '')} 规则 {json.dumps(e.get('rule', {}), ensure_ascii=False)}"
                  + (f" 最近事件 {e['last_event']['kind']} {e['last_event']['at']}" if e.get("last_event") else ""))
    elif a.cmd == "poll":
        r = poll(s, push=not a.no_push)
        if r is None:
            print("还没填 Hyperliquid 钱包地址（HL_ACCOUNT_ADDRESS），什么都没做。")
        else:
            print(json.dumps({"positions": r["snapshot"]["positions"], "events": [e["kind"] + " " + e["coin"] for e in r["events"]]},
                             ensure_ascii=False, indent=1))
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main(sys.argv[1:]))
