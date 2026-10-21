"""美股期权执行层（11d）：把期权研究的选边（A / B）变成 Alpaca 上的美股期权订单。**默认什么都不发。**

三种模式（每次下单前重新判断，写进账本）：
- live（真钱）：下面"真钱条件"全部满足才会走 https://api.alpaca.markets；少一条都不行
- paper（纸面账户）：真钱条件没满足、但有 Alpaca 纸面账户的 key → 发到 https://paper-api.alpaca.markets（假钱，真撮合）
- dry（演练）：连纸面 key 都没有 → 只把"本来会下什么"写进账本、推送，不发任何请求

真钱条件（can_go_live，全部要过）：
  1. config [alpaca] enabled = true 且 live = true
  2. 服务器上是真钱账户的 key（ALPACA_PAPER=0）
  3. data/state/approvals.json 里 kea 和 boss 都批准过，且批准时的风险上限 ≥ 现在配置的
  4. 回测门要两道都过：
     4a. 近似回测门 data/reports/options_gate.json（DVOL/VIX 近似，**只算 CPI/FOMC/NFP，不含周末**）：事件 ≥ 60 个、净盈利 > 0、
         去掉最好 3 笔仍 > 0、最差一笔 ≥ −3 × 单笔风险上限、不是合成数据、是新版（method=dvol_proxy、events_only）
     4b. 真实报价门（options.real_chain_stats，结果也写到 data/reports/options_real_gate.json）：用本机存的 Deribit 真实期权链
         结算过的事件/突发样本 ≥ 10 个、纸面卖跨式或买跨式累计净盈亏 > 0（全额手续费）、最差一笔 ≥ −3 × 单笔风险上限
     为什么要两道：2026-10-15 发现 DVOL 回测的卖方盈利几乎全是周末样本——DVOL 近似在周末系统性高估卖方（周末真实期权已按安静定价）
  5. 纸面门：已结算的事件/突发样本（不算每日对照）≥ paper_events_required 个，且按规则选边的纸面累计盈亏 ≥ 0
  6. 现在没有未平的真钱仓位（最多同时 1 个事件仓位）
  7. 今天（美东）真钱已实现亏损没到 daily_loss_stop_usd
  8. Alpaca 时钟说市场开着，且离收盘 > 10 分钟

结构映射：A（卖波动）→ 铁蝶式（卖平值 call + put，买 ±wing_pct% 的翅膀，最大亏损有上限；散户账户一般不让裸卖跨式）；
B（买波动）→ 买跨式；C → 不下单。只对事件 / 突发样本下单，每日对照样本不下。
张数 = floor(max_risk_usd / 每张最大亏损)，算出来 0 张就跳过并写明原因。

账本：data/ledger/alpaca.jsonl（kind = open / skip / close / close_pending / panic / approve）。
CLI：python -m desk.alpaca_exec status | approve --who kea|boss [--revoke] | panic-close | retry

Alpaca API 说明（2026-10 查 docs.alpaca.markets「Options Level 3 Trading」）：
  多腿单 POST /v2/orders：{"order_class": "mleg", "qty": "1", "type": "limit", "limit_price": "1.00", "time_in_force": "day",
  "legs": [{"symbol": OCC 代码, "ratio_qty": "1", "side": "buy|sell", "position_intent": "buy_to_open|sell_to_open|buy_to_close|sell_to_close"}]}。
  各腿 ratio 必须是最简比（GCD = 1）；Level 3 下"所有卖出腿必须在同一张多腿单里被覆盖"（铁蝶满足）。
  **不确定处（文档页面没写清，先在纸面账户验证）：**
    - mleg 的 limit_price 正负号：这里按 Alpaca API 参考的写法"正数 = 付钱（debit），负数 = 收钱（credit）"；
    - mleg 是否接受 type = "market"：示例只有 limit。平仓先发 market 多腿单，被拒就逐腿 DELETE /v2/positions/{symbol} 兜底
      （论坛也有人反映多腿一次平仓会失败）。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import notify
from .ledger import Ledger

log = logging.getLogger("alpaca")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
BASE_PAPER = "https://paper-api.alpaca.markets"
BASE_LIVE = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
LEDGER = "alpaca"
MODE_NAME = {"live": "真钱", "paper": "纸面账户", "dry": "演练（只记录，不发单）"}
STRUCT_NAME = {"A": "铁蝶式（卖波动，亏损有上限）", "B": "买跨式（买波动）", "C": "不动"}
WHO = ("kea", "boss")
CRYPTO_SHOCKS = {"HACK", "REG"}   # 突发类别里和比特币直接相关的 → IBIT；其他（地缘 / 关税 / 宏观意外）→ 大盘
CLOSE_BUFFER_MIN = 10
DEBATE_MAX_TRIES = 2   # 辩论遇到临时错误最多试几次（debate_error 记录，下次锁定可重试）
DEFAULTS = {"enabled": False, "live": False, "max_risk_usd": 100.0, "daily_loss_stop_usd": 100.0,
            "paper_events_required": 5, "wing_pct": 3.0, "macro_underlying": "SPY",
            "gate_min_events": 60, "gate_worst_multiple": 3.0}

# 测试注入：httpx.MockTransport；生产为 None（真网络）
TRANSPORT = None


def cfg(settings) -> dict:
    return {**DEFAULTS, **(settings.raw.get("alpaca") or {})}


# ---------------------------------------------------------------- keys
def _paper_flag() -> bool:
    return os.environ.get("ALPACA_PAPER", "1").strip() != "0"


def creds(paper: bool) -> tuple[str, str]:
    """paper=True：纸面账户的 key；paper=False：真钱账户的 key。拿不到返回 ("", "")。
    Alpaca 的纸面账户和真钱账户 key 不同：ALPACA_KEY_ID/ALPACA_SECRET 是哪种由 ALPACA_PAPER=1|0 决定；
    想同时放两套时，纸面那套可另放 ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET。"""
    kid, sec = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET", "").strip()
    if paper:
        pk, ps = os.environ.get("ALPACA_PAPER_KEY_ID", "").strip(), os.environ.get("ALPACA_PAPER_SECRET", "").strip()
        if pk and ps:
            return pk, ps
        return (kid, sec) if (_paper_flag() and kid and sec) else ("", "")
    return (kid, sec) if (not _paper_flag() and kid and sec) else ("", "")


def has_keys(paper: bool) -> bool:
    return all(creds(paper))


# ---------------------------------------------------------------- client
class Client:
    """Alpaca REST 的薄封装。paper 决定交易端点；行情统一走 data.alpaca.markets。"""

    def __init__(self, key_id: str, secret: str, paper: bool, transport=None):
        self.paper = paper
        self.base = BASE_PAPER if paper else BASE_LIVE
        t = transport if transport is not None else TRANSPORT
        kw = {"transport": t} if t is not None else {}
        self.http = httpx.Client(headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
                                 timeout=20, **kw)

    def _req(self, method: str, url: str, **kw):
        r = self.http.request(method, url, **kw)
        if r.status_code >= 300:
            raise RuntimeError(f"Alpaca {method} {url.split('.markets')[-1]} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def account(self) -> dict:
        return self._req("GET", f"{self.base}/v2/account")

    def clock(self) -> dict:
        return self._req("GET", f"{self.base}/v2/clock")

    def contracts(self, underlying: str, exp_gte: date, exp_lte: date, strike_gte: float, strike_lte: float) -> list[dict]:
        out, tok = [], None
        for _ in range(20):
            p = {"underlying_symbols": underlying, "expiration_date_gte": exp_gte.isoformat(),
                 "expiration_date_lte": exp_lte.isoformat(), "strike_price_gte": f"{strike_gte:.2f}",
                 "strike_price_lte": f"{strike_lte:.2f}", "status": "active", "limit": 1000}
            if tok:
                p["page_token"] = tok
            d = self._req("GET", f"{self.base}/v2/options/contracts", params=p)
            out += d.get("option_contracts") or []
            tok = d.get("next_page_token")
            if not tok:
                break
        return out

    def latest_price(self, symbol: str) -> float:
        d = self._req("GET", f"{DATA_URL}/v2/stocks/{symbol}/trades/latest")
        return float(d["trade"]["p"])

    def bars(self, symbol: str, start: date, end: date) -> list[dict]:
        d = self._req("GET", f"{DATA_URL}/v2/stocks/{symbol}/bars",
                      params={"timeframe": "1Day", "start": start.isoformat(), "end": end.isoformat(), "limit": 100, "adjustment": "split"})
        return d.get("bars") or []

    def snapshots(self, underlying: str, expiration: date | None = None) -> dict[str, dict]:
        out, tok = {}, None
        for _ in range(20):
            p = {"limit": 1000}
            if expiration:
                p["expiration_date"] = expiration.isoformat()
            if tok:
                p["page_token"] = tok
            d = self._req("GET", f"{DATA_URL}/v1beta1/options/snapshots/{underlying}", params=p)
            out.update(d.get("snapshots") or {})
            tok = d.get("next_page_token")
            if not tok:
                break
        return out

    def submit_order(self, payload: dict) -> dict:
        return self._req("POST", f"{self.base}/v2/orders", json=payload)

    def positions(self) -> list[dict]:
        return self._req("GET", f"{self.base}/v2/positions")

    def close_position(self, symbol: str) -> dict:
        return self._req("DELETE", f"{self.base}/v2/positions/{symbol}")


def client(paper: bool, transport=None) -> Client | None:
    kid, sec = creds(paper)
    if not (kid and sec):
        return None
    return Client(kid, sec, paper, transport)


# ---------------------------------------------------------------- 研究 → 美股标的
def map_underlying(settings, event_type: str) -> str | None:
    """BTC 研究（每日对照 / BTC）→ IBIT；宏观 CPI/FOMC/NFP → macro_underlying（SPY，可改 QQQ）；
    EARNINGS:<T> → T；SHOCK:<类别> → 加密类（交易所被盗 / 监管-ETF）IBIT，其他 macro_underlying。"""
    macro = str(cfg(settings)["macro_underlying"]).upper()
    et = (event_type or "").strip()
    up = et.upper()
    if up.startswith("EARNINGS:"):
        t = et.split(":", 1)[1].strip().upper()
        return t or None
    if up.startswith("SHOCK:"):
        return "IBIT" if up.split(":", 1)[1] in CRYPTO_SHOCKS else macro
    if up in ("CPI", "FOMC", "NFP"):
        return macro
    if up in ("BTC", "NONE", ""):
        return "IBIT"
    return None


# ---------------------------------------------------------------- 选合约
def _expiry_close(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)


def pick_expiry(contracts: list[dict], window_end: datetime) -> date | None:
    """最早一个收盘到期（美东 16:00）≥ 窗口结束的到期日。"""
    exps = sorted({date.fromisoformat(c["expiration_date"]) for c in contracts})
    for e in exps:
        if _expiry_close(e) >= window_end:
            return e
    return None


def _by_strike(contracts: list[dict], expiry: date) -> dict[float, dict[str, dict]]:
    out: dict[float, dict[str, dict]] = {}
    for c in contracts:
        if date.fromisoformat(c["expiration_date"]) != expiry or c.get("tradable") is False:
            continue
        cp = "C" if str(c["type"]).lower().startswith("c") else "P"
        out.setdefault(float(c["strike_price"]), {})[cp] = c
    return out


def select_legs(structure: str, contracts: list[dict], expiry: date, S: float, wing_pct: float) -> dict:
    """返回 {"ok", "K", "legs": [{symbol, side, position_intent, strike, cp, role}], "wings": {...}, "problems"}。
    A = 铁蝶：卖平值 call + put（同一行权价 K），买 call 翅膀 ≈ K×(1+w)、put 翅膀 ≈ K×(1−w)；B = 买平值 call + put。"""
    bs = _by_strike(contracts, expiry)
    both = [k for k, v in bs.items() if "C" in v and "P" in v]
    if not both:
        return {"ok": False, "problems": [f"{expiry} 没有同时有 call 和 put 的行权价"]}
    K = min(both, key=lambda k: (abs(k - S), k))
    leg = lambda c, side, role, cp: {"symbol": c["symbol"], "side": side,  # noqa: E731
                                    "position_intent": f"{side}_to_open", "strike": float(c["strike_price"]), "cp": cp, "role": role}
    if structure == "B":
        return {"ok": True, "K": K, "legs": [leg(bs[K]["C"], "buy", "long_call", "C"), leg(bs[K]["P"], "buy", "long_put", "P")]}
    if structure != "A":
        return {"ok": False, "problems": [f"结构 {structure} 不下单"]}
    w = wing_pct / 100
    calls = [k for k, v in bs.items() if "C" in v and k > K]
    puts = [k for k, v in bs.items() if "P" in v and k < K]
    if not calls or not puts:
        return {"ok": False, "problems": ["找不到铁蝶的翅膀（平值上方的 call / 下方的 put）"]}
    kc = min(calls, key=lambda k: (abs(k - K * (1 + w)), k))
    kp = min(puts, key=lambda k: (abs(k - K * (1 - w)), -k))
    return {"ok": True, "K": K, "wings": {"call": kc, "put": kp},
            "legs": [leg(bs[K]["C"], "sell", "short_call", "C"), leg(bs[K]["P"], "sell", "short_put", "P"),
                     leg(bs[kc]["C"], "buy", "long_call_wing", "C"), leg(bs[kp]["P"], "buy", "long_put_wing", "P")]}


def quote_mid(snap: dict | None) -> float | None:
    if not snap:
        return None
    q = snap.get("latestQuote") or {}
    b, a = q.get("bp"), q.get("ap")
    if b is not None and a and float(a) >= float(b) >= 0:      # 远虚值腿买一价常是 0：中间价 = 卖一价 / 2
        return (float(b) + float(a)) / 2
    t = snap.get("latestTrade") or {}
    return float(t["p"]) if t.get("p") else None


def price_structure(structure: str, sel: dict, snaps: dict[str, dict]) -> dict:
    """按买卖中间价算：A 的净收入（credit）和每张最大亏损 =（较宽翅膀宽度 − credit）×100；B 的净支出（debit）和每张最大亏损 = debit×100。"""
    mids, missing = {}, []
    for lg in sel["legs"]:
        m = quote_mid(snaps.get(lg["symbol"]))
        if m is None:
            missing.append(lg["symbol"])
        mids[lg["symbol"]] = m
    if missing:
        return {"ok": False, "problems": [f"没有报价：{', '.join(missing)}"], "mids": mids}
    signed = sum((mids[l["symbol"]] if l["side"] == "buy" else -mids[l["symbol"]]) for l in sel["legs"])
    if structure == "A":
        credit = -signed
        width = max(sel["wings"]["call"] - sel["K"], sel["K"] - sel["wings"]["put"])
        ml = (width - credit) * 100
        if credit <= 0 or ml <= 0:
            return {"ok": False, "problems": [f"铁蝶报价不合理（净收入 {credit:.2f}，翅膀宽 {width:.2f}）"], "mids": mids}
        return {"ok": True, "mids": mids, "net": credit, "credit": credit, "width": width, "max_loss_per_contract": ml,
                "limit_price": -round(credit, 2)}
    debit = signed
    if debit <= 0:
        return {"ok": False, "problems": [f"买跨式报价不合理（净支出 {debit:.2f}）"], "mids": mids}
    return {"ok": True, "mids": mids, "net": debit, "debit": debit, "max_loss_per_contract": debit * 100,
            "limit_price": round(debit, 2)}


def qty_for(max_risk_usd: float, max_loss_per_contract: float) -> int:
    if max_loss_per_contract <= 0:
        return 0
    return int(math.floor(max_risk_usd / max_loss_per_contract + 1e-9))


def mleg_payload(legs: list[dict], qty: int, order_type: str = "limit", limit_price: float | None = None,
                 client_order_id: str | None = None) -> dict:
    """多腿单。limit_price：正数 = 付钱（debit），负数 = 收钱（credit）——见文件头"不确定处"。"""
    p = {"order_class": "mleg", "qty": str(int(qty)), "type": order_type, "time_in_force": "day",
         "legs": [{"symbol": l["symbol"], "ratio_qty": "1", "side": l["side"], "position_intent": l["position_intent"]}
                  for l in legs]}
    if order_type == "limit":
        p["limit_price"] = f"{limit_price:.2f}"
    if client_order_id:
        p["client_order_id"] = client_order_id[:120]
    return p


def closing_legs(legs: list[dict]) -> list[dict]:
    flip = {"buy": "sell", "sell": "buy"}
    return [{**l, "side": flip[l["side"]], "position_intent": f"{flip[l['side']]}_to_close"} for l in legs]


# ---------------------------------------------------------------- 批准
def _approvals_path(settings) -> Path:
    p = Path(settings.data) / "state" / "approvals.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def approvals(settings) -> dict:
    try:
        return json.loads(_approvals_path(settings).read_text())
    except (OSError, ValueError):
        return {}


def approve(settings, who: str, revoke: bool = False) -> dict:
    if who not in WHO:
        raise ValueError(f"who 只能是 {' / '.join(WHO)}")
    d = approvals(settings)
    c = cfg(settings)
    if revoke:
        d.pop(who, None)
    else:
        d[who] = {"ts": datetime.now(UTC).isoformat(), "max_risk_usd": float(c["max_risk_usd"]),
                  "daily_loss_stop_usd": float(c["daily_loss_stop_usd"])}
    _approvals_path(settings).write_text(json.dumps(d, ensure_ascii=False, indent=1))
    Ledger(settings.data, LEDGER).append("approve", "-", {"who": who, "revoke": revoke, "record": d.get(who)})
    return d


# ---------------------------------------------------------------- 门
def gate_file(settings) -> Path:
    return Path(settings.data) / "reports" / "options_gate.json"


def _ledger_rows(settings) -> list[dict]:
    return Ledger(settings.data, LEDGER).all()


def open_positions(settings, mode: str | None = None) -> list[dict]:
    """已发出（sent=True）或演练记下、还没平的开仓记录。"""
    rows = _ledger_rows(settings)
    closed = {(r["weekend"], r.get("mode")) for r in rows if r["kind"] in ("close", "panic") and r.get("final", True)}
    out = []
    for r in rows:
        if r["kind"] == "open" and (r.get("sent") or r.get("mode") == "dry") and (r["weekend"], r.get("mode")) not in closed:
            if mode is None or r.get("mode") == mode:
                out.append(r)
    return out


def realized_today(settings, mode: str = "live", now: datetime | None = None) -> float:
    today = (now or datetime.now(UTC)).astimezone(ET).date()
    tot = 0.0
    for r in _ledger_rows(settings):
        if r["kind"] in ("close", "panic") and r.get("mode") == mode and r.get("pnl_usd") is not None:
            if datetime.fromtimestamp(r["written_at"] / 1e9, tz=UTC).astimezone(ET).date() == today:
                tot += float(r["pnl_usd"])
    return tot


def paper_track(settings) -> dict:
    from .options import completed
    led = Ledger(settings.data, "options")
    n, tot = 0, 0.0
    for r in completed(led):
        if r["lock"].get("sample_kind") not in ("event", "shock"):
            continue
        pp = r["outcome"].get("paper") or {}
        if pp.get("ok") and pp.get("chosen_pnl") is not None:
            n += 1
            tot += float(pp["chosen_pnl"])
    return {"n": n, "chosen_pnl": tot}


def _parse_ts(s: str) -> datetime:
    """Alpaca 时间戳可能带纳秒（9 位小数），fromisoformat 只吃到微秒：截掉多余的位数。"""
    if "." in s:
        head, rest = s.split(".", 1)
        i = 0
        while i < len(rest) and rest[i].isdigit():
            i += 1
        s = head + "." + rest[:min(i, 6)].ljust(6, "0") + rest[i:]
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def clock_ok(clk: dict) -> tuple[bool, str]:
    if not clk.get("is_open"):
        return False, f"美股现在没开盘（下次开盘 {clk.get('next_open', '?')}）"
    left = (_parse_ts(clk["next_close"]) - _parse_ts(clk["timestamp"])).total_seconds() / 60
    if left <= CLOSE_BUFFER_MIN:
        return False, f"离收盘只剩 {left:.0f} 分钟（要 > {CLOSE_BUFFER_MIN}）"
    return True, f"开盘中，离收盘 {left:.0f} 分钟"


def gate_checks(settings, now: datetime | None = None, transport=None, check_clock: bool = True) -> list[dict]:
    """逐条检查真钱条件。前面几条都过了才去问真钱端点的时钟（否则根本不碰真钱 URL）。"""
    c = cfg(settings)
    risk = float(c["max_risk_usd"])
    out = []

    def add(key, name, ok, detail):
        out.append({"key": key, "name": name, "ok": bool(ok), "detail": detail})

    add("config", "配置打开真钱", c["enabled"] and c["live"],
        f"enabled={str(c['enabled']).lower()}，live={str(c['live']).lower()}" + ("" if c["enabled"] and c["live"] else "（两个都要是 true）"))
    add("keys", "真钱账户 key", has_keys(False),
        "已填真钱 key（ALPACA_PAPER=0）" if has_keys(False) else
        ("现在填的是纸面账户 key（ALPACA_PAPER=1）" if all((os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET"))) else "没填 Alpaca key"))
    ap = approvals(settings)
    miss = [w for w in WHO if w not in ap]
    stale = [w for w in WHO if w in ap and (float(ap[w].get("max_risk_usd", 0)) < risk
                                           or float(ap[w].get("daily_loss_stop_usd", 0)) < float(c["daily_loss_stop_usd"]))]
    add("approvals", "两人批准（kea + boss）", not miss and not stale,
        ("缺：" + "、".join(miss) + "。" if miss else "") +
        ("批准后风险上限被调大了，要重新批准：" + "、".join(stale) + "。" if stale else "") +
        ("；".join(f"{w} 于 {ap[w]['ts'][:16]} 批准（单笔 ≤ ${ap[w]['max_risk_usd']:.0f}）" for w in WHO if w in ap) or ""))
    try:
        g = json.loads(gate_file(settings).read_text())
    except (OSError, ValueError):
        g = None
    if g is None:
        add("backtest", "近似回测门（只算事件）", False, "还没有 data/reports/options_gate.json（先跑 python3 -m desk.options_backtest --market us --underlying SPY）")
    else:
        need = int(c["gate_min_events"])
        floor = -float(c["gate_worst_multiple"]) * risk
        fails = []
        if g.get("method") != "dvol_proxy" or not g.get("events_only"):
            fails.append("是旧版回测门（可能把周末样本算进去了，周末是 DVOL 近似的假象），要重跑 options_backtest")
        if g.get("synthetic"):
            fails.append("是合成数据")
        if int(g.get("n_events", 0)) < need:
            fails.append(f"事件 {g.get('n_events', 0)} 个 < {need}")
        if not float(g.get("net_usd", 0)) > 0:
            fails.append(f"净 {float(g.get('net_usd', 0)):+.2f} 不 > 0")
        if not float(g.get("net_ex_best3_usd", 0)) > 0:
            fails.append(f"去掉最好 3 笔 {float(g.get('net_ex_best3_usd', 0)):+.2f} 不 > 0")
        if float(g.get("worst_usd", -1e18)) < floor:
            fails.append(f"最差一笔 {float(g.get('worst_usd', 0)):+.2f} < {floor:+.0f}")
        add("backtest", "近似回测门（只算事件）", not fails,
            ("；".join(fails) if fails else f"{g['n_events']} 个 CPI/FOMC/NFP 事件，净 {g['net_usd']:+.2f}，去掉最好 3 笔 {g['net_ex_best3_usd']:+.2f}，最差 {g['worst_usd']:+.2f}")
            + f"（{g.get('underlying', '?')}，{g.get('side', '?')}；周末样本不算，DVOL 近似在周末系统性高估卖方）")
    try:
        from .options import real_chain_stats
        rg = real_chain_stats(settings)
    except Exception as e:  # noqa: BLE001
        rg = {"pass": False, "reasons": [f"算不出来：{str(e)[:120]}"]}
    if rg["pass"]:
        el = rg["groups"]["event_like"]
        rdetail = (f"真实期权链结算的事件/突发样本 {el['n']} 个，实际/隐含 {el['geo_ratio']:.2f}，"
                   f"纸面卖跨式 {el['A_net']:+.2f}、买跨式 {el['B_net']:+.2f} 美元（全额手续费），最差 {el['worst']:+.2f}")
    else:
        rdetail = "；".join(rg["reasons"])
    add("real_chain", "真实报价门", rg["pass"],
        rdetail + "（近似回测只是参考，真钱前必须有真实报价的证据）")
    pt = paper_track(settings)
    need_p = int(c["paper_events_required"])
    add("paper", "纸面成绩", pt["n"] >= need_p and pt["chosen_pnl"] >= 0,
        f"已结算事件/突发样本 {pt['n']} 个（要 ≥ {need_p}），按规则选边累计 {pt['chosen_pnl']:+.2f} 美元（要 ≥ 0）")
    op = open_positions(settings, "live")
    add("one_open", "最多 1 个未平真钱仓位", not op, "没有未平的真钱仓位" if not op else f"还有未平的：{', '.join(r['weekend'] for r in op)}")
    lost = realized_today(settings, "live", now)
    stop = float(c["daily_loss_stop_usd"])
    add("daily_loss", "当日止损", lost > -stop, f"今天（美东）真钱已实现 {lost:+.2f}，止损线 −{stop:.0f}")
    if all(x["ok"] for x in out) and check_clock:
        try:
            cl = client(False, transport)
            ok, why = clock_ok(cl.clock())
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"问不到 Alpaca 时钟：{str(e)[:120]}"
        add("clock", "开盘且离收盘 > 10 分钟", ok, why)
    else:
        add("clock", "开盘且离收盘 > 10 分钟", False, "前面的条件没全过，没去问真钱端点" if check_clock else "没检查")
    return out


def can_go_live(settings, now: datetime | None = None, transport=None) -> tuple[bool, list[str]]:
    checks = gate_checks(settings, now, transport)
    return all(x["ok"] for x in checks), [f"{x['name']}：{x['detail']}" for x in checks if not x["ok"]]


def decide_mode(settings, now: datetime | None = None, transport=None) -> tuple[str, list[dict]]:
    checks = gate_checks(settings, now, transport)
    if all(x["ok"] for x in checks):
        return "live", checks
    return ("paper" if has_keys(True) else "dry"), checks


def mode_preview(settings) -> str:
    """网页用：不联网判断"现在下单会走哪个模式"（时钟那条不查）。"""
    if not cfg(settings)["enabled"]:
        return "off"
    checks = gate_checks(settings, check_clock=False)
    if all(x["ok"] for x in checks if x["key"] != "clock"):
        return "live（开盘时）"
    return "paper" if has_keys(True) else "dry"


# ---------------------------------------------------------------- 生命周期
def _fails(checks: list[dict]) -> list[str]:
    return [f"{x['name']}：{x['detail']}" for x in checks if not x["ok"]]


def _push(settings, title: str, body: str, priority: str = "default"):
    try:
        notify.push(settings, title, body, priority=priority)
    except Exception as e:  # noqa: BLE001
        log.warning("notify failed: %s", e)


def open_for_sample(settings, sample, paper_plan: dict, now: datetime | None = None, transport=None, debate_llm=None) -> dict | None:
    """锁定后调用（options.job_lock）。返回写进账本的记录；没启用 / 选 C / 每日样本 → None。"""
    c = cfg(settings)
    if not c["enabled"]:
        return None
    chosen = (paper_plan or {}).get("chosen", "C")
    if chosen not in ("A", "B") or getattr(sample, "kind", None) not in ("event", "shock"):
        return None
    led = Ledger(settings.data, LEDGER)
    sid = sample.sid
    if any(r["weekend"] == sid and r["kind"] in ("open", "skip") for r in led.all()):
        return None
    now = now or datetime.now(UTC)
    under = map_underlying(settings, sample.event_type)
    mode, checks = decide_mode(settings, now, transport)
    base = {"mode": mode, "structure": chosen, "underlying": under, "event_type": sample.event_type,
            "window_end": sample.end.isoformat(), "max_risk_usd": float(c["max_risk_usd"]), "gates": checks,
            "paper_reason": paper_plan.get("reason")}
    fails = _fails(checks)

    def skip(why: str, **extra) -> dict:
        rec = led.append("skip", sid, {**base, "reason": why, **extra})
        _push(settings, f"美股期权没下单：{sid}", f"模式：{MODE_NAME[mode]}。原因：{why}")
        return rec

    if c["live"] and mode != "live":
        _push(settings, f"美股期权：真钱条件没满足，改用{MODE_NAME[mode]}", "\n".join("✗ " + f for f in fails), priority="high")
    if not under:
        return skip(f"研究事件 {sample.event_type} 对不上美股标的")
    if open_positions(settings, mode):
        return skip(f"{MODE_NAME[mode]}里还有没平的事件仓位（最多同时 1 个）")
    if mode == "dry":
        rec = led.append("open", sid, {**base, "sent": False, "payload": None, "response": None,
                                       "note": "没有 Alpaca key，拿不到合约和报价，只记下意图"})
        _push(settings, f"美股期权演练：{sid}",
              f"模式：{MODE_NAME[mode]}。本来会在 {under} 上做 {STRUCT_NAME[chosen]}，单笔最多亏 ${c['max_risk_usd']:.0f}。\n"
              "没填 Alpaca key，没发任何单。")
        return rec
    cl = client(mode == "paper", transport)
    try:
        S = cl.latest_price(under)
        win_end = sample.end.astimezone(ET)
        band = max(float(c["wing_pct"]) * 2.5, 8.0) / 100
        cons = cl.contracts(under, win_end.date(), win_end.date() + timedelta(days=14), S * (1 - band), S * (1 + band))
        exp = pick_expiry(cons, sample.end)
        if exp is None:
            return skip(f"{under} 没有在窗口结束（{win_end:%m-%d %H:%M} 美东）之后到期的合约")
        sel = select_legs(chosen, cons, exp, S, float(c["wing_pct"]))
        if not sel["ok"]:
            return skip("；".join(sel["problems"]), S=S, expiry=exp.isoformat())
        pr = price_structure(chosen, sel, cl.snapshots(under, exp))
        if not pr["ok"]:
            return skip("；".join(pr["problems"]), S=S, expiry=exp.isoformat(), legs=sel["legs"])
        qty = qty_for(float(c["max_risk_usd"]), pr["max_loss_per_contract"])
        if qty <= 0:
            return skip(f"每张最多亏 ${pr['max_loss_per_contract']:.0f}，超过单笔风险上限 ${c['max_risk_usd']:.0f}：0 张，不下",
                        S=S, expiry=exp.isoformat(), legs=sel["legs"], pricing=pr)
    except Exception as e:  # noqa: BLE001
        return skip(f"取合约/报价出错：{str(e)[:200]}")
    # Card B：多空辩论闸门。只能否决或缩小张数；出错按 [debate] on_error（默认 skip）
    from . import debate
    dbt = None
    try:
        if debate.cfg(settings)["enabled"]:
            prop = {"sid": sid, "underlying": under, "structure": chosen, "structure_name": STRUCT_NAME[chosen],
                    "event_type": sample.event_type, "window_end": sample.end.isoformat(), "expiry": exp.isoformat(),
                    "qty": qty, "max_loss_usd": pr["max_loss_per_contract"] * qty}
            ctx = debate.build_context(settings, prop, cl, now)
            dbt = debate.run_gate(settings, ctx, prop, llm=debate_llm, now=now)
    except Exception as e:  # noqa: BLE001
        dbt = {"ok": False, "veto": True, "id": None, "direction": "unclear", "confidence": 0.0, "size_mult": 0.0,
               "error": f"辩论闸门异常：{type(e).__name__}: {str(e)[:150]}", "error_kind": "transient", "cost_usd": 0.0}
    dsum = None
    if dbt is not None:
        dsum = debate.brief(dbt)
        if dbt["veto"]:
            why = dbt.get("error") or "；".join(dbt.get("veto_reasons") or []) or "风控否决"
            reason = (f"多空辩论没通过（{'出错，按规则不下单' if dbt.get('error') else '被否决'}）：{why}。辩论编号 {dbt.get('id')}")
            extra = dict(S=S, expiry=exp.isoformat(), legs=sel["legs"], pricing=pr, debate=dsum)
            kind = dbt.get("error_kind")
            if dbt.get("error") and kind == "budget":
                _push(settings, f"美股期权辩论：AI 预算用完，{sid} 没下单", why, priority="high")
            if dbt.get("error") and kind in ("transient", "deadline", "budget"):
                tries = 1 + sum(1 for r in led.all() if r["weekend"] == sid and r["kind"] == "debate_error")
                if tries < DEBATE_MAX_TRIES and now < sample.end:
                    rec = led.append("debate_error", sid, {**base, "reason": reason, "attempt": tries, **extra})
                    _push(settings, f"美股期权没下单（辩论出错，可重试）：{sid}", f"模式：{MODE_NAME[mode]}。{reason}", priority="high")
                    return rec
            return skip(reason, **extra)
        new_qty = min(qty, math.floor(qty * min(1.0, max(0.0, float(dbt["size_mult"])))))
        if new_qty <= 0:
            return skip(f"多空辩论建议缩仓到 {dbt['size_mult']:.2f}，{qty} 张缩完是 0 张，不下。辩论编号 {dbt.get('id')}",
                        S=S, expiry=exp.isoformat(), legs=sel["legs"], pricing=pr, debate=dsum)
        qty = new_qty
        # 辩论花了时间：重新取报价、重算限价和每张最大亏损（只许张数不增）
        try:
            pr = price_structure(chosen, sel, cl.snapshots(under, exp))
            if not pr["ok"]:
                return skip("辩论后重新取报价失败：" + "；".join(pr["problems"]), S=S, expiry=exp.isoformat(), legs=sel["legs"], debate=dsum)
            qty = min(qty, qty_for(float(c["max_risk_usd"]), pr["max_loss_per_contract"]))
            if qty <= 0:
                return skip("辩论后重新报价，每张最大亏损超过单笔风险上限：0 张，不下", S=S, expiry=exp.isoformat(),
                            legs=sel["legs"], pricing=pr, debate=dsum)
        except Exception as e:  # noqa: BLE001
            return skip(f"辩论后重新取报价出错：{str(e)[:200]}", debate=dsum)
    legs = [{**l, "entry_mid": pr["mids"][l["symbol"]]} for l in sel["legs"]]
    payload = mleg_payload(legs, qty, "limit", pr["limit_price"], client_order_id=f"wd-{sid}-open")
    body = {**base, "S": S, "expiry": exp.isoformat(), "K": sel["K"], "wings": sel.get("wings"), "legs": legs, "qty": qty,
            "pricing": {k: v for k, v in pr.items() if k != "mids"}, "max_loss_usd": pr["max_loss_per_contract"] * qty,
            "payload": payload, "endpoint": cl.base}
    if dsum is not None:
        body["debate"] = dsum
    try:
        resp = cl.submit_order(payload)
        rec = led.append("open", sid, {**body, "sent": True, "response": resp})
    except Exception as e:  # noqa: BLE001
        rec = led.append("open", sid, {**body, "sent": False, "response": None, "error": str(e)[:300]})
        _push(settings, f"美股期权下单失败（{MODE_NAME[mode]}）：{sid}", str(e)[:300], priority="high")
        return rec
    netw = f"收 ${pr['net']:.2f}/股" if chosen == "A" else f"付 ${pr['net']:.2f}/股"
    _push(settings, f"美股期权已下单（{MODE_NAME[mode]}）：{sid}",
          f"模式：{MODE_NAME[mode]}。{under} {STRUCT_NAME[chosen]} {qty} 张，到期 {exp}，平值 {sel['K']:g}"
          + (f"，翅膀 {sel['wings']['put']:g}/{sel['wings']['call']:g}" if chosen == "A" else "")
          + f"。限价 {netw}，最多亏 ${pr['max_loss_per_contract'] * qty:.0f}。窗口结束自动平仓。"
          + (f"\n多空辩论 {dsum['id']}：风控方向 {dsum['direction']}，把握 {dsum['confidence']:.2f}，仓位系数 {dsum['size_mult']:.2f}。" if dsum else "")
          + ("" if mode == "live" else "\n没走真钱的原因：" + "；".join(fails[:3])),
          priority="high" if mode == "live" else "default")
    return rec


def _latest_open(settings, sid: str) -> dict | None:
    return next((r for r in reversed(open_positions(settings)) if r["weekend"] == sid), None)


def _est_pnl(legs: list[dict], qty: int, exit_mids: dict[str, float | None]) -> float | None:
    tot = 0.0
    for l in legs:
        m, e = exit_mids.get(l["symbol"]), l.get("entry_mid")
        if m is None or e is None:
            return None
        tot += (m - e) * (1 if l["side"] == "buy" else -1)
    return tot * 100 * qty


def _intrinsic(l: dict, S: float) -> float:
    return max(S - l["strike"], 0.0) if l["cp"] == "C" else max(l["strike"] - S, 0.0)


def close_for_sample(settings, sample, now: datetime | None = None, transport=None) -> dict | None:
    """结果出来时调用（options.job_outcome）：用反向多腿市价单平掉所有腿；已到期就只记录；没开盘 → 记 close_pending 等下次重试。"""
    sid = sample.sid if hasattr(sample, "sid") else str(sample)
    op = _latest_open(settings, sid)
    if not op:
        return None
    return _close(settings, op, now or datetime.now(UTC), transport, reason="窗口结束")


def _close(settings, op: dict, now: datetime, transport, reason: str) -> dict:
    led = Ledger(settings.data, LEDGER)
    sid, mode = op["weekend"], op["mode"]
    base = {"mode": mode, "structure": op.get("structure"), "underlying": op.get("underlying"), "reason": reason, "final": True}
    if mode == "dry":
        rec = led.append("close", sid, {**base, "status": "dry", "pnl_usd": None, "payload": None, "response": None})
        _push(settings, f"美股期权演练平仓：{sid}", f"模式：{MODE_NAME[mode]}。本来会在这时平掉 {op.get('underlying')} 的仓位，没发单。")
        return rec
    legs, qty = op["legs"], int(op["qty"])
    exp = date.fromisoformat(op["expiry"])
    cl = client(mode == "paper", transport)
    if cl is None:
        rec = led.append("close_pending", sid, {**base, "final": False, "error": "key 不见了，没法平仓"})
        _push(settings, f"请立刻手动平仓（{MODE_NAME[mode]}）：{sid}", "服务器上的 Alpaca key 不见了，程序平不了仓。", priority="max")
        return rec
    if now >= _expiry_close(exp):
        pnl = None
        try:
            S = cl.latest_price(op["underlying"])
            pnl = _est_pnl(legs, qty, {l["symbol"]: _intrinsic(l, S) for l in legs})
        except Exception:  # noqa: BLE001
            S = None
        rec = led.append("close", sid, {**base, "status": "expired", "S_end": S, "pnl_usd": pnl, "payload": None, "response": None})
        _push(settings, f"美股期权已到期（{MODE_NAME[mode]}）：{sid}",
              f"合约 {exp} 已到期，没有再下平仓单（由券商按到期规则处理）。估算盈亏 {'—' if pnl is None else f'${pnl:+.2f}'}。")
        return rec
    try:
        ok, why = clock_ok(cl.clock())
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"问不到时钟：{str(e)[:120]}"
    if not ok:
        rec = led.append("close_pending", sid, {**base, "final": False, "why": why})
        _push(settings, f"美股期权等开盘再平（{MODE_NAME[mode]}）：{sid}", f"{why}。开盘后程序每 15 分钟重试一次；到期前一定会平。", priority="high")
        return rec
    exit_mids: dict[str, float | None] = {}
    try:
        snaps = cl.snapshots(op["underlying"], exp)
        exit_mids = {l["symbol"]: quote_mid(snaps.get(l["symbol"])) for l in legs}
    except Exception as e:  # noqa: BLE001
        log.warning("close snapshots failed: %s", e)
    payload = mleg_payload(closing_legs(legs), qty, "market", client_order_id=f"wd-{sid}-close-{int(time.time())}")
    resp, err, fallback = None, None, []
    try:
        resp = cl.submit_order(payload)
    except Exception as e:  # noqa: BLE001
        err = str(e)[:300]
        for l in legs:          # 兜底：逐腿平
            try:
                fallback.append({"symbol": l["symbol"], "response": cl.close_position(l["symbol"])})
            except Exception as e2:  # noqa: BLE001
                fallback.append({"symbol": l["symbol"], "error": str(e2)[:200]})
    failed = err is not None and any("error" in f for f in fallback)
    pnl = None if failed else _est_pnl(legs, qty, exit_mids)
    rec = led.append("close" if not failed else "close_pending", sid,
                     {**base, "final": not failed, "status": "closed" if not failed else "failed", "payload": payload,
                      "response": resp, "error": err, "fallback": fallback, "exit_mids": exit_mids, "pnl_usd": pnl,
                      "endpoint": cl.base})
    if failed:
        _push(settings, f"平仓失败，请立刻去 Alpaca 手动平仓（{MODE_NAME[mode]}）：{sid}", (err or "")[:300], priority="max")
    else:
        _push(settings, f"美股期权已平仓（{MODE_NAME[mode]}）：{sid}",
              f"模式：{MODE_NAME[mode]}。{op.get('underlying')} {STRUCT_NAME.get(op.get('structure'), '')} {qty} 张已平"
              + ("（多腿单被拒，已逐腿平）" if err else "") + f"。按中间价估算盈亏 {'—' if pnl is None else f'${pnl:+.2f}'}（以券商成交为准）。",
              priority="high" if mode == "live" else "default")
    return rec


def retry_pending(settings, now: datetime | None = None, transport=None) -> list[dict]:
    """调度器每 15 分钟调一次：等开盘的（close_pending），以及窗口结束 30 分钟后还没平的（比如结果任务出错没跑到）→ 再试一次平仓。"""
    now = now or datetime.now(UTC)
    pend = {r["weekend"] for r in _ledger_rows(settings) if r["kind"] == "close_pending"}
    out = []
    for op in open_positions(settings):
        overdue = bool(op.get("window_end")) and now >= datetime.fromisoformat(op["window_end"]) + timedelta(minutes=30)
        if (op["weekend"] in pend or overdue) and op.get("mode") != "dry":
            out.append(_close(settings, op, now, transport, reason="重试平仓"))
    return out


def panic_close(settings, transport=None) -> list[dict]:
    """紧急：账本里所有没平的仓位逐腿平掉（DELETE /v2/positions/{symbol}）。"""
    led = Ledger(settings.data, LEDGER)
    out = []
    for op in open_positions(settings):
        if op.get("mode") == "dry":
            out.append(led.append("panic", op["weekend"], {"mode": "dry", "final": True, "pnl_usd": None, "results": []}))
            continue
        cl = client(op["mode"] == "paper", transport)
        res = []
        for l in op.get("legs") or []:
            try:
                res.append({"symbol": l["symbol"], "response": cl.close_position(l["symbol"]) if cl else None,
                            **({} if cl else {"error": "没有 key"})})
            except Exception as e:  # noqa: BLE001
                res.append({"symbol": l["symbol"], "error": str(e)[:200]})
        bad = [r for r in res if "error" in r]
        out.append(led.append("panic", op["weekend"], {"mode": op["mode"], "final": not bad, "pnl_usd": None, "results": res}))
        _push(settings, f"紧急平仓（{MODE_NAME[op['mode']]}）：{op['weekend']}",
              "全部腿已发平仓。" if not bad else "有腿没平掉，请立刻去 Alpaca 手动处理：" + ", ".join(r["symbol"] for r in bad),
              priority="max")
    return out


# ---------------------------------------------------------------- 网页 / CLI
def recent_rows(settings, n: int = 20) -> list[dict]:
    return [r for r in _ledger_rows(settings) if r["kind"] != "approve"][-n:][::-1]


def status_text(settings) -> str:
    c = cfg(settings)
    checks = gate_checks(settings, check_clock=False)
    L = [f"Alpaca 执行：enabled={c['enabled']} live={c['live']}；纸面 key {'有' if has_keys(True) else '无'}，真钱 key {'有' if has_keys(False) else '无'}",
         f"现在下单会走：{mode_preview(settings)}", "真钱条件："]
    L += [f"  {'✓' if x['ok'] else '✗'} {x['name']}：{x['detail']}" for x in checks]
    ops = open_positions(settings)
    L.append("未平仓位：" + (", ".join(f"{r['weekend']}（{MODE_NAME[r['mode']]}）" for r in ops) or "无"))
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    from . import config
    ap = argparse.ArgumentParser(prog="python -m desk.alpaca_exec", description="美股期权执行（Alpaca）")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="真钱条件逐条 ✓/✗、未平仓位")
    a = sub.add_parser("approve", help="批准真钱（kea 和 boss 各跑一次）")
    a.add_argument("--who", required=True, choices=WHO)
    a.add_argument("--revoke", action="store_true", help="撤销批准")
    sub.add_parser("panic-close", help="紧急：平掉账本里所有没平的仓位")
    sub.add_parser("retry", help="重试还没平的仓位")
    args = ap.parse_args(argv)
    s = config.load()
    if args.cmd == "approve":
        d = approve(s, args.who, args.revoke)
        print(("已撤销 " if args.revoke else "已批准 ") + args.who + "。现在：" + (", ".join(f"{k} @ {v['ts'][:19]}" for k, v in d.items()) or "没人批准"))
        return 0
    if args.cmd == "panic-close":
        res = panic_close(s)
        print(f"处理了 {len(res)} 个仓位。" + " ".join(f"{r['weekend']}:{'ok' if r.get('final') else '有失败'}" for r in res))
        return 0
    if args.cmd == "retry":
        print(f"重试了 {len(retry_pending(s))} 个")
        return 0
    print(status_text(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
