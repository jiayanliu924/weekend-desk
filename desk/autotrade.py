"""自动下单（真钱）。在网站「设置 → 自动下单」里由账户主人打开；三种模式：
- off：不下单（默认）
- shadow（演习）：到点算好要下的单、推送"本来会下什么"，但不发给交易所
- live（真下单）：按下面的流程真的下单

流程（每个合约、每个周末）：
1. 锁定预测后（重开前 15 分钟）：如果规则说要出手、风控 agent 没有否决、规则没作废 → 挂"只做 maker"的限价单
   （买单挂在买一价、卖单挂在卖一价，不吃单）。仓位 = 本金上限 ÷ 合约数，且不超过盘口深度 10%，杠杆 1 倍。
   （卡 G3，[autotrade] requote_entry=true 时：发单前先重读盘口，挂当前买一/卖一，偏离回到触发线以内就不下；重开前 5 分钟没成交的挂单撤掉重挂一次。）
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
import re
import time
from pathlib import Path

from . import hl_rate, model, notify
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
        hl_rate.wrap(self.info)
        self.ex = Exchange(Account.from_key(key), constants.MAINNET_API_URL, account_address=address, perp_dexs=["", DEX])
        hl_rate.wrap(self.ex)

    def spot_usdc(self) -> float:
        """现货里的 USDC。Unified（统一账户）模式下，这笔现货 USDC 就是合约下单用的保证金
        （实测：合约账户 accountValue 读到 0，但订单能直接用这笔钱挂住成交）。"""
        try:
            st = self.info.spot_user_state(self.address)
            return sum(float(b.get("total", 0) or 0) for b in st.get("balances", []) if b.get("coin") == "USDC")
        except Exception:  # noqa: BLE001
            return 0.0

    def balance(self) -> dict:
        main = self.info.user_state(self.address)
        dex = self.info.user_state(self.address, dex=DEX)
        f = lambda s: float(s.get("marginSummary", {}).get("accountValue", 0) or 0)  # noqa: E731
        spot = self.spot_usdc()
        # Unified 账户：能用来下单的保证金 = 合约账户 + 现货 USDC
        return {"main": f(main), "xyz": f(dex), "spot": spot,
                "available": f(main) + f(dex) + spot,
                "withdrawable_xyz": float(dex.get("withdrawable", 0) or 0)}

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

    def mid(self, coin: str) -> float:
        """盘口中间价（只读；平仓前记一笔，事后复盘滑点用）。"""
        lv = self.info.l2_snapshot(coin)["levels"]
        return (float(lv[0][0]["px"]) + float(lv[1][0]["px"])) / 2

    def top(self, coin: str) -> dict:
        """当前买一/卖一（只读，卡 G3 发单前刷新盘口用）。一侧为空 → 该侧 None；返回结构不对 → 抛错（调用方当"刷新失败"处理）。"""
        lv = self.info.l2_snapshot(coin)["levels"]
        if len(lv) < 2:
            raise ValueError("l2_snapshot levels 结构不对")
        side = lambda x: float(x[0]["px"]) if x else None  # noqa: E731
        return {"bid": side(lv[0]), "ask": side(lv[1])}


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


def _live_preflight(settings, cfg, c, check_wallet: bool | None = None) -> str | None:
    """真下单前的安全检查。返回 None 表示通过，否则返回不通过的原因（会被记进账、推送给你）。

    - 交易账户余额不能低于下限（不往没钱/几乎没钱的账户里下单）。
    - 真实环境额外校验：API 私钥推导出的地址必须 ≠ 主钱包地址——相等说明你把主钱包私钥粘错进来了
      （主钱包私钥能提币，绝不能交给服务器）。演习/测试用注入的假客户端时跳过这项。
    """
    floor = cfg.get("min_balance_usd", 5.0)
    try:
        b = c.balance()
        avail = b.get("available", (b.get("xyz", 0) or 0))  # Unified：合约+现货 USDC 都算可用保证金
    except Exception:  # noqa: BLE001
        avail = None
    if avail is not None and avail < floor:
        return f"账户可用保证金 ${avail:.2f} 低于下限 ${floor:.2f}，不下单"
    if check_wallet is None:
        check_wallet = not _client_factory   # 默认：只有真实钱包才校验（pairs_exec 传显式值）
    if check_wallet:
        main, key = creds()
        try:
            from eth_account import Account
            derived = Account.from_key(key).address.lower()
        except Exception:  # noqa: BLE001
            return None
        if main and derived == main.lower():
            return "你填的私钥是主钱包的（能提币），不是只能交易的 API 钱包。请换成 Hyperliquid 生成的 API 钱包私钥。"
    return None


def kill_check(settings, c=None) -> dict | None:
    """看门狗：把"已实现 + 当前未实现"的真实盈亏加起来，超过上限就全部市价平掉并切回 off。

    原来的上限只看已平仓的单，且只在下单那一刻检查一次——一个还开着的亏损仓位它看不见。
    这个函数应在重开后、正式平仓前被调度器定期调用。
    """
    cfg = get(settings)
    from . import account_stop
    if cfg["mode"] != "live" and not account_stop.tripped(settings):   # 总止损把模式切成 off 后，开着的仓仍要看守到平掉
        return None
    led = _led(settings)
    open_coins = []
    for r in led.all():
        if r["kind"] == "entry" and r.get("sent") and not any(
                x["kind"] == "exit" and x["weekend"] == r["weekend"] for x in led.all()):
            open_coins.append((r["weekend"], r["coin"], r["name"]))
    if cfg["mode"] != "live":
        # 只因总止损触发而在看守：没仓位就不连交易所；已经紧急平过的周末不重复平、不重复推送
        done_w = set(account_stop.state(settings).get("killed_weekends", []))
        open_coins = [o for o in open_coins if o[0] not in done_w]
        if not open_coins:
            return None
    try:
        c = c or client()
        unreal = 0.0
        for _, coin, _ in open_coins:
            st = c.info.user_state(c.address, dex=DEX) if hasattr(c, "info") else {}
            for p in st.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == coin:
                    unreal += float(pos.get("unrealizedPnl", 0) or 0)
    except Exception:  # noqa: BLE001
        return None
    total = realized_total(settings) + unreal
    if total <= -cfg["max_loss_usd"] and open_coins:
        closed = []
        for _, coin, nm in open_coins:
            try:
                c.close(coin)
                closed.append(nm)
            except Exception:  # noqa: BLE001
                pass
        put(settings, mode="off")
        account_stop.mark_killed(settings, [o[0] for o in open_coins])
        notify.push(settings, "看门狗已紧急平仓并关闭自动下单",
                    f"已实现+未实现合计 ${total:.2f}，超过上限 ${cfg['max_loss_usd']:.0f}。已平：{'、'.join(closed)}", priority="max")
        return {"killed": True, "total_usd": total, "closed": closed}
    return {"killed": False, "total_usd": total}


_VETO_CODES = ("事件", "价格异常", "缺数据", "平台")        # 和 agents.VETO_CODES 一致；agents 读不到时的兜底
_VETO_REASON_WORDS = ("财报", "议息", "事件", "插针", "异常", "缺", "没有", "平台", "流动性",
                      "偏离", "盘口", "预言机", "交易所", "过旧", "FOMC", "CPI", "非农",
                      "业绩", "美联储", "降息", "加息", "拆股", "停牌", "宕机", "清算", "新闻", "战争", "制裁")
# 只讲"样本/实盘周末数量不够"的说法（先剥掉这些片段，再看有没有别的具体原因词；"没有 30 个周末"里的"没有"不算具体原因）
_SAMPLE_PHRASE = re.compile(
    r"(还?没有|还?缺少?|不足|不够|不满|尚?未满|少于)\s*(足够的?|充足的?|积累|攒够?|跑满|达到|满)?\s*(\d+\s*个?)?\s*(实盘|真钱)?\s*(样本|周末)(?!价|行情|盘|标记|报价|数据)"
    r"|(样本|周末)(数量|数)?\s*(还?没有|还?没|不足|不够|太少|偏少|较少|很少|过少|有限|还少)")


def _veto_codes() -> tuple:
    try:
        from . import agents
        return tuple(agents.VETO_CODES)
    except Exception:  # noqa: BLE001
        return _VETO_CODES


_CODE_EN = (("event", "事件"), ("earnings", "事件"), ("anomaly", "价格异常"), ("wick", "价格异常"), ("price", "价格异常"),
            ("data", "缺数据"), ("missing", "缺数据"), ("platform", "平台"), ("exchange", "平台"), ("liquidity", "平台"))
_CODE_STRIP = " \t\r\n。，．.,、；;：:"


def _norm_code(code, codes: tuple) -> str | None:
    """把模型写的 code 归一成四类之一；认不出（含"|"抄模板、同时含两个以上类名、非字符串）→ None。"""
    if not isinstance(code, str):
        return None
    c = code.strip(_CODE_STRIP)
    if not c or "|" in c or "｜" in c:
        return None
    if sum(1 for k in codes if k in c) >= 2:
        return None
    if c in codes:
        return c
    for k in codes:
        if c.startswith(k):          # "事件类" 算 事件
            return k
    low = c.lower()
    for en, k in _CODE_EN:
        if low.startswith(en) and k in codes:
            return k
    return None


def _only_sample_reason(why: str) -> bool:
    """why 只讲样本少、没有任何具体原因词 → True（模型不守规矩时的兜底）。"""
    if "样本" not in why:
        return False
    rest = _SAMPLE_PHRASE.sub(" ", why)
    return not any(k in rest for k in _VETO_REASON_WORDS)


def _veto_check(settings, name: str) -> tuple[str | None, str | None]:
    """风控 agent 最近 24 小时对该合约的否决 → (生效的否决原因, 被忽略的否决原因)。
    只认 code 属于 事件/价格异常/缺数据/平台 的否决；code 缺失（旧格式）或不在四类里 → 忽略；
    code 合法但 why 只讲样本少 → 也忽略。被忽略的不拦单，原 why 由调用方写进 entry.veto_ignored。"""
    try:
        from . import agents
        r = agents.last_full_run(settings, within_h=24)
    except Exception:  # noqa: BLE001
        return None, None
    codes = _veto_codes()
    try:
        vetoes = r["rooms"]["risk"]["synth"]["veto"] if r else None
    except (KeyError, TypeError, IndexError):
        vetoes = None
    if not isinstance(vetoes, list):      # int / dict / str / None 一律当没有否决
        return None, None
    ignored: list[str] = []
    for v in vetoes:
        if not (isinstance(v, dict) and str(v.get("name", "")).upper() == name.upper()):
            continue
        why = str(v.get("why", ""))
        code = _norm_code(v.get("code"), codes)
        if code is None:
            log.info("风控否决忽略（code 缺失或不在四类里）：%s code=%r why=%s", name, v.get("code"), why)
            ignored.append(why or "（无原因）")
            continue
        if _only_sample_reason(why):
            log.info("风控否决忽略（只讲样本少）：%s code=%s why=%s", name, code, why)
            ignored.append(why)
            continue
        return why, "；".join(ignored) or None
    return None, "；".join(ignored) or None


def _vetoed(settings, name: str) -> str | None:
    """风控 agent 最近 24 小时的有效否决（只认 code 属于四类的；旧格式/只讲样本少的忽略，返回 None）。"""
    return _veto_check(settings, name)[0]


# ------------------------------------------------------------------ 记录用的小工具（只读/只写账，不改下单行为）
EXIT_POLL_N = 3        # 平仓后最多读几次成交（单合约最长约 3 秒；多合约串行平仓，别拖慢）
EXIT_POLL_S = 1.0      # 每次间隔（秒）


def _sleep(sec: float) -> None:
    if not _client_factory:      # 测试用假客户端时不真睡（和原来 sleep(2 if ... and not _client_factory) 一致）
        time.sleep(sec)


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_order_resp(resp) -> dict:
    """解析 Hyperliquid 下单返回 → {exch_status: resting/filled/error/unknown, exch_error, oid}。结构缺失一律 unknown，不抛错。"""
    out = {"exch_status": "unknown", "exch_error": None, "oid": None}
    try:
        if not isinstance(resp, dict):
            return out
        if str(resp.get("status", "")).lower() == "err":      # 顶层就失败：response 是一段文字
            return {**out, "exch_status": "error", "exch_error": str(resp.get("response"))[:500]}
        inner = resp.get("response")
        data = inner.get("data") if isinstance(inner, dict) else None
        sts = data.get("statuses") if isinstance(data, dict) else None
        if not isinstance(sts, list) or not sts:
            return out
        for st in sts:
            if isinstance(st, dict) and st.get("error") is not None:
                return {**out, "exch_status": "error", "exch_error": str(st["error"])[:500]}
        first = sts[0]
        if isinstance(first, dict):
            for key in ("filled", "resting"):
                if isinstance(first.get(key), dict):
                    oid = first[key].get("oid")
                    return {**out, "exch_status": key, "oid": oid}
    except Exception:  # noqa: BLE001
        pass
    return out


def note_nolive(settings, weekend: str, name: str | None, reason: str) -> dict | None:
    """"没下单"的原因记一笔 kind=nolive。同一 weekend+name+reason 只写一次（调度每分钟可能重复进来）。写账失败不影响调用方。"""
    try:
        led = _led(settings)
        for r in led.all():
            if r.get("kind") == "nolive" and r.get("weekend") == weekend and r.get("name") == name and r.get("reason") == reason:
                return None
        body = {"reason": reason, "ts": time.time()}
        if name:
            body["name"] = name
        return led.append("nolive", weekend, body)
    except Exception:  # noqa: BLE001
        log.exception("nolive write failed")
        return None


def note_missed_window(settings, w) -> dict | None:
    """调度器发现：这个周末锁定已完成（或失败），但真钱发单窗口已过、live_entry 没跑过。mode off 不记。"""
    if get(settings)["mode"] == "off":
        return None
    led = _led(settings)
    if any(r["kind"] == "entry" and r["weekend"] == w.wid for r in led.all()):
        return None
    lock = Ledger(settings.data).weekend(w.wid).get("lock")
    has_lock = bool(lock)
    if has_lock and not (lock.get("action") or {}).get("side"):      # 本来就不出手（side 0/空）：不算"错过"
        return None
    return note_nolive(settings, w.wid, getattr(w, "name", None),
                       "错过发单窗口" if has_lock else "错过发单窗口（且没有锁定记录）")


def _is_close_fill(f: dict) -> bool:
    return str(f.get("dir", "")).startswith("Close") or bool(_f(f.get("closedPnl")))


def _wavg_px(fills: list[dict]) -> float | None:
    num = den = 0.0
    for f in fills:
        px, sz = _f(f.get("px")), _f(f.get("sz"))
        if px is not None and sz:
            num += px * abs(sz)
            den += abs(sz)
    return round(num / den, 6) if den else None


def _close_seen(fills: list[dict], pos: float) -> bool:
    """看到平仓成交了吗？有 sz 字段就要求平仓成交量 ≥ 平仓前仓位的 99.9%；没有 sz（只能判断有无）就有就算。"""
    cl = [f for f in fills if _is_close_fill(f)]
    if not cl:
        return False
    szs = [_f(f.get("sz")) for f in cl]
    if any(z is None for z in szs):
        return True
    return sum(abs(z) for z in szs) >= abs(pos) * 0.999


def _mid_of(c, coin: str) -> float | None:
    try:
        m = _f(c.mid(coin))
        return m if m and m > 0 else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 卡 G3：发单前刷新盘口 / 重挂
REQUOTE_AT_MIN = 5          # 重开前几分钟重挂
REQUOTE_WINDOW_MIN = 2      # 重挂 job 的触发窗口长度：[重开前 5 分钟, 重开前 3 分钟)，过窗不补（仍远在重开前 1 分钟撤单之前）
R_GONE = "发单前价格已回归，没有边际"
R_EMPTY = "盘口一侧为空"
R_STALE_NOTE = "刷新盘口失败，用锁定价"


def requote_enabled(settings) -> bool:
    """[autotrade] requote_entry。段缺失/键缺失 = False（老行为）。"""
    try:
        return bool((settings["autotrade"] or {}).get("requote_entry", False))
    except (KeyError, TypeError):
        return False


def _fresh_top(c, coin: str) -> tuple[float | None, float | None]:
    """刷新后的 (买一, 卖一)；一侧缺失/非正数 → 该侧 None。读取失败直接抛错。"""
    t = c.top(coin)
    pos = lambda x: (_f(x) if (_f(x) or 0) > 0 else None)  # noqa: E731
    return pos(t.get("bid")), pos(t.get("ask"))


def _edge_gone(settings, feats: dict, bid: float, ask: float) -> tuple[str | None, float | None]:
    """用刷新后的中间价重算相对周五收盘锚价（features.fri_close，算 dev_bps 用的同一个价）的偏离。
    |新偏离| ≤ v2 触发线（[strategy] fade_threshold_bps，和 model.decide_v2 同一个值、同一个比较）或与锁定时方向相反 → (R_GONE, 新偏离)。
    缺锚价（老记录）→ 判断不了，返回 (None, None)。"""
    fri = _f(feats.get("fri_close"))
    if not fri or fri <= 0:
        return None, None
    dev = ((bid + ask) / 2 / fri - 1) * 1e4
    dev0 = _f(feats.get("dev_bps"))
    if dev0 is None and _f(feats.get("dec_mid")):
        dev0 = (_f(feats.get("dec_mid")) / fri - 1) * 1e4
    thr = float(model.strategy_cfg(settings)["fade_threshold_bps"])
    if abs(dev) <= thr or (dev0 is not None and dev * dev0 < 0):
        return R_GONE, dev
    return None, dev


def _cancel_err(resp) -> str | None:
    """撤单返回的失败原文；确认成功才返回 None。顶层 {"status":"err","response":"文字"} → 原文；
    不是 dict / 结构不认识 / statuses 里不是全部 "success" → 一律当失败（绝不默认成功，免得旧单还在又重挂出第二张）。"""
    if not isinstance(resp, dict):
        return "撤单返回无法识别"
    if str(resp.get("status", "")).lower() == "err":
        return str(resp.get("response"))[:300]
    try:
        sts = resp["response"]["data"]["statuses"]
        if not isinstance(sts, list) or not sts:
            return "撤单返回无法识别"
        for s in sts:
            if isinstance(s, dict) and s.get("error") is not None:
                return str(s["error"])[:300]
        if all(s == "success" for s in sts) and str(resp.get("status", "")).lower() == "ok":
            return None
    except Exception:  # noqa: BLE001
        pass
    return "撤单返回无法识别"


def _lock_feats(settings, wid: str) -> dict:
    try:
        return (Ledger(settings.data).weekend(wid).get("lock") or {}).get("features", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


REQUOTE_MIN_AGE_S = 60      # entry 发出不足这么久不重挂（刚挂上，排队位置还有价值）


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rq_finished(r: dict) -> bool:
    """这条 requote 记录算"做过了"吗：stage=done / 没有 stage 字段（老记录）/ 有 skipped。只有 pending 的不算。"""
    return r.get("stage") != "pending" or bool(r.get("skipped"))


def requote_due(settings, w) -> bool:
    """调度器问：这个合约现在要不要跑重挂 job。开关开 + live 模式 + 有 sent 的 entry + 还没撤单/平仓 + 没有"做完"的重挂记录
    （只有 pending 记录 = 上次撤单后可能崩了，窗口内要重跑补挂）。"""
    try:
        if not requote_enabled(settings) or get(settings)["mode"] != "live":
            return False
        rows = _led(settings).all()
        if not any(r["kind"] == "entry" and r["weekend"] == w.wid and r.get("sent") for r in rows):
            return False
        if any(r["kind"] in ("cancel", "exit") and r["weekend"] == w.wid for r in rows):
            return False
        return not any(r["kind"] == "requote" and r["weekend"] == w.wid and _rq_finished(r) for r in rows)
    except Exception:  # noqa: BLE001
        return False


def _judge_px(settings, c, w, coin: str, is_buy: bool, rec: dict) -> tuple[float | None, str | None]:
    """刷新盘口 + 边际检查 → (新价, 不重挂的原因)。刷新失败 → (None, None) 并在 rec 里记 requote_note（调用方当"价格没变"处理）。"""
    try:
        bid, ask = _fresh_top(c, coin)
    except Exception as e:  # noqa: BLE001
        log.warning("requote refresh failed (%s): %s", rec.get("name"), e)
        rec["requote_note"] = R_STALE_NOTE
        return None, None
    if not bid or not ask:
        return None, R_EMPTY
    gone, dev_now = _edge_gone(settings, _lock_feats(settings, w.wid), bid, ask)
    rec["dev_bps_now"] = None if dev_now is None else round(dev_now, 2)
    if gone:
        return None, gone
    return (bid if is_buy else ask), None


def requote_unfilled(settings, w) -> dict | None:
    """卡 G3：重开前 5 分钟，对本周末 sent 且仍有未成交剩余的挂单：先刷新盘口并做边际检查，再决定——
      - 新价（round_px 后）和旧价一样 → 不撤不挂（保住排队位置），记 skipped="价格没变，保留原挂单"；
      - entry 发出不足 60 秒 → 不动，记 skipped="刚挂不到 60 秒"；
      - 边际消失 / 盘口一侧空 → 撤掉不重挂，记 skipped；
      - 否则：先写 stage=pending 记录 → 撤单（返回不是明确成功就当失败，不重挂）→ 按剩余量在新买一/卖一重挂 Alo → 写 stage=done 记录。
    每个周末每个合约最多 1 次（已有"做完"的 requote 记录就不再做）。pending 记录还在而没有 done = 上次撤单后进程崩了：
    窗口内重跑时，旧单已不在、盘口上没有任何本合约挂单、entry 没有任何成交 → 按剩余量补挂（同样边际检查），否则只写 done 说明。
    只在开关开 + live 模式 + 总止损没触发时做。撤单/平仓/看门狗/名义上限不受影响：新单数量 ≤ 原单剩余。"""
    if not requote_enabled(settings) or get(settings)["mode"] != "live":
        return None
    led = _led(settings)
    rows = led.all()
    ent = next((r for r in rows if r["kind"] == "entry" and r["weekend"] == w.wid), None)
    if not ent or not ent.get("sent") or ent.get("side") not in ("买", "卖"):
        return None
    if any(r["kind"] in ("cancel", "exit") and r["weekend"] == w.wid for r in rows):
        return None
    rq_rows = [r for r in rows if r["kind"] == "requote" and r["weekend"] == w.wid]
    if any(_rq_finished(r) for r in rq_rows):
        return None
    pend = rq_rows[-1] if rq_rows else None
    from . import account_stop
    if account_stop.check(settings).get("tripped"):
        return None
    coin, name, is_buy = ent["coin"], ent.get("name"), ent["side"] == "买"
    rec: dict = {"stage": "done", "coin": coin, "name": name, "side": ent["side"], "old_px": None, "new_px": None,
                 "remaining_sz": None, "exch_status": None, "exch_error": None, "oid": None, "skipped": None}

    def done(**kw) -> dict:
        rec.update(kw)
        return led.append("requote", w.wid, rec)

    try:
        c = client()
        orders = [o for o in c.open_orders(coin) if abs(_f(o.get("sz")) or 0) > 0]
        dec = c.sz_decimals(coin)
        if pend:      # ---- 上次撤单后崩了：只在"确定没有任何东西在盘口上、也没成交"时补挂
            rec.update(old_px=pend.get("old_px"), remaining_sz=pend.get("remaining_sz"), resumed=True)
            if orders:
                return done(skipped="重挂前中断，盘口上已有本合约挂单，不补挂")
            try:
                fl = _entry_fills(c, ent, is_buy)
            except Exception as e:  # noqa: BLE001
                return done(skipped="重挂前中断，读不到成交记录，不补挂", exch_error=str(e)[:300])
            if fl > 0:
                return done(skipped="重挂前中断，期间已有成交，不补挂")
            try:      # 新单可能已发出并成交、但 user_fills 还没出现：任何方向的非零仓位都不补挂；读不到也不补
                if abs(_f(c.position(coin)) or 0.0) > 0:
                    return done(skipped="已有仓位，不补挂")
            except Exception as e:  # noqa: BLE001
                return done(skipped="重挂前中断，读不到仓位，不补挂", exch_error=str(e)[:300])
            sz = round_sz(_f(pend.get("remaining_sz")) or 0.0, dec)
            rec["remaining_sz"] = sz
            if sz <= 0:
                return done(skipped="补挂量不足最小单位，不补挂")
            px_new, skip = _judge_px(settings, c, w, coin, is_buy, rec)
            if skip:
                notify.push(settings, f"已撤单未补挂：{name} {ent['side']}", skip, priority="high")
                return done(skipped=skip)
            px_new = round_px(float(px_new if px_new else pend.get("old_px")), dec)
        else:
            own = [o for o in orders if ent.get("oid") is None or o.get("oid") == ent.get("oid")]
            if not own:      # 已经全部成交（或单子已不在）：没有剩余可重挂，不写记录
                return None
            rem = sum(abs(_f(o["sz"])) for o in own)
            old_px = _f(own[0].get("limitPx")) or _f(ent.get("px"))
            rec.update(old_px=old_px, remaining_sz=rem)
            age_ms = _now_ms() - (_f(ent.get("t_ms")) or 0) if ent.get("t_ms") else None
            if age_ms is not None and age_ms < REQUOTE_MIN_AGE_S * 1000:
                return done(skipped="刚挂不到 60 秒")
            px_new, skip = _judge_px(settings, c, w, coin, is_buy, rec)
            old_r = round_px(float(old_px), dec) if old_px else None
            px_new = round_px(float(px_new), dec) if px_new else None
            if not skip and (px_new is None or px_new == old_r):      # 价格没变（含刷新失败）：撤了就白丢排队位置
                return done(skipped="价格没变，保留原挂单")
            oids = [o["oid"] for o in own]
            led.append("requote", w.wid, {**rec, "stage": "pending", "oid": oids[0], "oids": oids})      # 先留底：撤单后崩了能补挂
            for o in own:
                cerr = _cancel_err(c.cancel(coin, o["oid"]))
                if cerr:      # 撤不掉（多半刚好成交了）或返回看不懂：不重挂，免得双份
                    notify.push(settings, f"重挂中止：{name} {ent['side']}", "撤单失败：" + cerr, priority="high")
                    return done(skipped="撤单失败：" + cerr, exch_error=cerr)
            try:      # 读单到撤单之间若又成交了一部分，剩余量按成交记录扣掉（只会变小）
                if _f(ent.get("sz")):
                    rem = min(rem, max(0.0, _f(ent["sz"]) - _entry_fills(c, ent, is_buy)))
            except Exception:  # noqa: BLE001
                pass
            sz = round_sz(rem, dec)
            rec["remaining_sz"] = sz
            if sz <= 0:
                return done(skipped="撤单后剩余不足最小单位（多半已成交），不重挂")
            if skip:
                notify.push(settings, f"已撤单未重挂：{name} {ent['side']}", skip, priority="high")
                return done(skipped=skip)
        resp = c.post_only(coin, is_buy, sz, px_new)
        pr = parse_order_resp(resp)
        rec.update(new_px=px_new, exch_status=pr["exch_status"], exch_error=pr["exch_error"], oid=pr["oid"])
        if pr["exch_status"] == "error":
            notify.push(settings, f"重挂被拒：{name} {ent['side']}", f"下单被拒：{pr['exch_error']}（原单已撤）", priority="high")
        else:
            notify.push(settings, f"已重挂：{name} {ent['side']}", f"{sz} 张 {rec['old_px']} → {px_new}，开盘前没成交会自动撤单", priority="high")
        return done()
    except Exception as e:  # noqa: BLE001
        log.exception("requote failed")
        notify.push(settings, f"重挂失败：{name}", str(e)[:300], priority="high")
        return done(exch_error=str(e)[:300])


def _entry_fills(c, ent: dict, is_buy: bool) -> float:
    """本 entry 方向的开仓成交总量（读不到就抛错，由调用方决定怎么办）。"""
    fl = [f for f in c.fills(ent["coin"], ent.get("t_ms", 0) - 60_000)
          if not str(f.get("dir", "")).startswith("Close") and f.get("side") in (None, "B" if is_buy else "A")]
    return sum(abs(_f(f.get("sz")) or 0.0) for f in fl)


# ------------------------------------------------------------------ the three steps
def entry(settings, w, lock: dict | None) -> dict | None:
    cfg = get(settings)
    if cfg["mode"] == "off":
        return None
    nm0 = getattr(w, "name", None) or (lock or {}).get("name")
    if not lock:
        note_nolive(settings, w.wid, nm0, "没有锁定记录（lock 缺失），不下单")
        return None
    from . import account_stop
    if account_stop.check(settings).get("tripped"):      # 总账户止损：触发后不再开新仓
        note_nolive(settings, w.wid, nm0, "总账户止损已触发，不再开新仓")
        return None
    led = _led(settings)
    if any(r["kind"] == "entry" and r["weekend"] == w.wid for r in led.all()):
        note_nolive(settings, w.wid, nm0, "本周末已有下单记录，不重复下单")
        return None
    a = lock.get("action", {})
    name, coin = w.name or lock.get("name"), w.coin or lock.get("coin")
    base = {"mode": cfg["mode"], "coin": coin, "name": name}
    if lock.get("void"):
        return led.append("entry", w.wid, {**base, "skipped": "本周规则作废：" + "；".join(lock.get("void_reasons", []))})
    if not a.get("side"):
        return led.append("entry", w.wid, {**base, "skipped": a.get("reason", "规则说不出手")})
    if lock.get("features", {}).get("dislocated"):
        return led.append("entry", w.wid, {**base, "skipped": "中间价和标记价脱节（疑似插针），本周不下单（防被冤枉爆仓）"})
    veto, veto_ignored = _veto_check(settings, name)
    if veto_ignored:      # 被忽略的否决：不拦单，但记下原话，网页/复盘能看到
        base["veto_ignored"] = veto_ignored
    if veto:
        return led.append("entry", w.wid, {**base, "skipped": f"风控否决：{veto}"})
    lost = realized_total(settings)
    if lost <= -cfg["max_loss_usd"]:
        put(settings, mode="off")
        notify.push(settings, "自动下单已停止", f"累计真实亏损 ${-lost:.2f}，超过上限 ${cfg['max_loss_usd']:.0f}", priority="high")
        return led.append("entry", w.wid, {**base, "skipped": "累计亏损超过上限，已自动关闭"})
    risk = settings["risk"]
    # 分母 = 主策略实际会出手的合约数（v2 只做 us_equity；v1 = 全部），不是配置里的合约总数
    n = max(1, len(model.tradable_instruments(settings)))
    notional = cfg["capital_usd"] / n
    # 回撤自动缩仓：累计真实亏损越多，下得越小（竞品"资本减少时自动收缩风险"的小 bot 版）
    dd_factor = max(0.25, 1.0 + 2.0 * min(0.0, lost) / max(1.0, cfg["capital_usd"]))
    notional *= dd_factor
    feats = lock.get("features", {})
    # 联合压测：会出手的合约可能被同一个周一宏观事件同时推同向，按完全相关给整组一个首损预算，均摊到每个合约
    worst = feats.get("max_abs_dev_bps") or risk.get("worst_adverse_bps", 500.0)
    joint_cap = risk.get("joint_first_loss_frac", 0.25) * cfg["capital_usd"] / n / (worst / 1e4)
    notional = min(notional, joint_cap)
    requote = requote_enabled(settings)
    if requote:      # 卡 G3：买/卖任一侧深度缺失 = 0 深度 = 不出手（原来是"缺的一侧忽略、取另一侧"，两侧都缺时反而不设上限）
        if not (feats.get("bid_depth_usd") and feats.get("ask_depth_usd")):
            note_nolive(settings, w.wid, name, R_EMPTY)
            return None
        depth = min(feats["bid_depth_usd"], feats["ask_depth_usd"])
    else:
        depth = min([d for d in (feats.get("bid_depth_usd"), feats.get("ask_depth_usd")) if d] or [0])
    if depth:
        notional = min(notional, depth * risk["max_depth_fraction"])
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
        # 下单前安全检查
        pf = _live_preflight(settings, cfg, c)
        if pf:
            notify.push(settings, f"自动下单未通过安全检查：{name}", pf, priority="high")
            return led.append("entry", w.wid, {**plan, "sent": False, "skipped": pf})
        dec = c.sz_decimals(coin)
        rq_extra: dict = {}
        if requote:      # 卡 G3：发单前重读盘口，挂当前买一/卖一（仍是 Alo）；还有没有边际要重新判断
            try:
                bid, ask = _fresh_top(c, coin)
            except Exception as e:  # noqa: BLE001
                log.warning("requote refresh failed (%s): %s", name, e)
                rq_extra["requote_note"] = R_STALE_NOTE
            else:
                if not bid or not ask:
                    note_nolive(settings, w.wid, name, R_EMPTY)
                    return None
                gone, dev_now = _edge_gone(settings, feats, bid, ask)
                if gone:
                    note_nolive(settings, w.wid, name, gone)
                    return None
                rq_extra.update(px_lock=px_raw, dev_bps_now=None if dev_now is None else round(dev_now, 2))
                px_raw = plan["px"] = bid if is_buy else ask
        px = round_px(float(px_raw), dec)
        sz = round_sz(notional / px, dec)
        if sz <= 0:
            return led.append("entry", w.wid, {**plan, "skipped": "金额太小，凑不够最小下单单位"})
        c.set_leverage(coin, 1)
        resp = c.post_only(coin, is_buy, sz, px)
        pr = parse_order_resp(resp)
        rejected = pr["exch_status"] == "error"
        extra = {"error": "下单被拒：" + pr["exch_error"]} if rejected else {}      # daily_report 认 error 字段，显示成"挂单失败"
        rec = led.append("entry", w.wid, {**plan, "sent": not rejected, "sz": sz, "px": px, "resp": resp,
                                          "t_ms": int(time.time() * 1000), **pr, **extra, **rq_extra})
        if rejected:
            notify.push(settings, f"自动下单被拒：{name} {plan['side']}", f"下单被拒：{pr['exch_error']}", priority="high")
        else:
            verb = "已成交" if pr["exch_status"] == "filled" else "已挂单"
            notify.push(settings, f"{verb}：{name} {plan['side']}", f"{sz} 张 @ {px}（约 ${notional:.0f}），开盘前没成交会自动撤单", priority="high")
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
        filled_sz, avg_px = None, None
        try:      # 只读：从成交记录推入场成交量/均价；读不到就留 null，不影响撤单记录
            fl = c.fills(ent["coin"], ent.get("t_ms", 0) - 60_000)
            exp = {"买": "B", "卖": "A"}.get(ent.get("side"))
            fl = [f for f in fl if not str(f.get("dir", "")).startswith("Close") and (exp is None or f.get("side") in (None, exp))]
            szs = [abs(_f(f.get("sz")) or 0.0) for f in fl]
            if fl and sum(szs) > 0:
                filled_sz, avg_px = round(sum(szs), 8), _wavg_px(fl)
            elif pos:
                filled_sz = abs(pos)
            else:
                filled_sz = 0.0
        except Exception:  # noqa: BLE001
            filled_sz = abs(pos) if pos else None
        return led.append("cancel", w.wid, {"coin": ent["coin"], "cancelled": n, "position": pos,
                                            "filled_sz": filled_sz, "entry_avg_px": avg_px})
    except Exception as e:  # noqa: BLE001
        notify.push(settings, f"撤单失败：{ent['name']}", str(e)[:300], priority="high")
        return led.append("cancel", w.wid, {"coin": ent["coin"], "error": str(e)[:300]})


def _exit_failed(settings, led, ent, w, e) -> dict:
    notify.push(settings, f"平仓失败，请立刻去 Hyperliquid 手动平仓：{ent['name']}", str(e)[:300], priority="max")
    return led.append("exit", w.wid, {"coin": ent["coin"], "error": str(e)[:300]})


def _resp_fill(resp) -> tuple[float, float] | None:
    """平仓市价单返回里的 (成交量, 均价)；认不出 → None。"""
    try:
        for st in resp["response"]["data"]["statuses"]:
            fl = st.get("filled") if isinstance(st, dict) else None
            if isinstance(fl, dict):
                sz, px = _f(fl.get("totalSz")), _f(fl.get("avgPx"))
                if sz and px:
                    return abs(sz), px
    except Exception:  # noqa: BLE001
        pass
    return None


def _entry_px_guess(settings, ent, w) -> float | None:
    """估算用的入场价，回退链：撤单记录的 entry_avg_px（真实成交均价）→ 本周末本合约最新一条 stage=done 且有 new_px 的重挂记录的 new_px
    （原单被撤、成交多半发生在重挂价）→ entry 的挂单价。"""
    rows = _led(settings).all()
    for r in reversed(rows):
        if r["kind"] == "cancel" and r["weekend"] == w.wid and _f(r.get("entry_avg_px")):
            return _f(r["entry_avg_px"])
    for r in reversed(rows):
        if r["kind"] == "requote" and r["weekend"] == w.wid and r.get("stage") == "done" and _f(r.get("new_px")) \
                and r.get("coin", ent.get("coin")) == ent.get("coin"):
            return _f(r["new_px"])
    return _f(ent.get("px"))


def _resp_pnl_estimate(settings, ent, w, pos: float, resp, missing: float | None = None) -> float | None:
    """成交明细没读全时的兜底：对"没读到成交的那部分数量"（默认整个仓位）按平仓返回的 avgPx 和入场价估算毛盈亏，不含任何手续费。
    数量取 min(missing, 平仓返回的 totalSz)。"""
    rf = _resp_fill(resp)
    if not rf or not pos:
        return None
    sz, exit_px = rf
    if missing is not None:
        sz = min(sz, max(0.0, missing))
    entry_px = _entry_px_guess(settings, ent, w)
    if not entry_px:
        return None
    return (exit_px - entry_px) * sz * (1 if pos > 0 else -1)


def exit_(settings, w) -> dict | None:
    led = _led(settings)
    ent = next((r for r in led.all() if r["kind"] == "entry" and r["weekend"] == w.wid), None)
    if not ent or not ent.get("sent") or any(r["kind"] == "exit" and r["weekend"] == w.wid for r in led.all()):
        return None
    try:      # 平仓单发出去之前出任何错 = 真的没平成，要喊人手动平
        c = client()
        pos = c.position(ent["coin"])
        mid = _mid_of(c, ent["coin"]) if pos else None
        resp = c.close(ent["coin"]) if pos else None
    except Exception as e:  # noqa: BLE001
        return _exit_failed(settings, led, ent, w, e)
    since = ent.get("t_ms", 0) - 60_000
    fills: list = []
    complete, poll_err = False, None
    if pos:      # 平仓单已发出：等到看见平仓成交再算盈亏。最多读 EXIT_POLL_N 次，每次前等 EXIT_POLL_S 秒；读失败不丢已发出的平仓 resp
        for _ in range(EXIT_POLL_N):
            _sleep(EXIT_POLL_S)
            try:
                got = c.fills(ent["coin"], since)
                if _close_seen(got, pos):
                    fills, complete, poll_err = got, True, None
                    break
                fills = got
            except Exception as e:  # noqa: BLE001
                poll_err = str(e)[:200]      # 保留之前读到的成交，继续试下一次
    else:
        try:
            fills, complete = c.fills(ent["coin"], since), True      # 没仓位：没东西可等
        except Exception as e:  # noqa: BLE001
            return _exit_failed(settings, led, ent, w, e)
    source = "fills"
    try:
        pnl = sum(float(f.get("closedPnl", 0) or 0) - float(f.get("fee", 0) or 0) for f in fills)
        fee = sum(_f(f.get("fee")) or 0.0 for f in fills)
        close_px = _wavg_px([f for f in fills if _is_close_fill(f)])
        has_close = any(_is_close_fill(f) for f in fills)
    except Exception:  # noqa: BLE001
        pnl, fee, close_px, has_close = 0.0, 0.0, None, False
    if pos and not has_close:      # 一笔平仓成交都没读到：用平仓返回估算（总比记 0 强），并明确标成估算
        est = _resp_pnl_estimate(settings, ent, w, pos, resp)
        if est is not None:
            pnl, source = est - fee, "resp_estimate"      # fee = 已读到的手续费（只有入场那笔）；平仓手续费没法估，不计入
            rf = _resp_fill(resp)
            close_px = rf[1] if rf else close_px
    elif pos and has_close and not complete:      # 只读到一部分平仓成交（覆盖 < 99.9% 平仓量）：读到的按真实成交，剩余部分按平仓返回估算
        try:
            szs = [_f(f.get("sz")) for f in fills if _is_close_fill(f)]
            covered = sum(abs(z) for z in szs) if all(z is not None for z in szs) else None
        except Exception:  # noqa: BLE001
            covered = None
        if covered is not None and covered < abs(pos) * 0.999:
            est = _resp_pnl_estimate(settings, ent, w, pos, resp, missing=abs(pos) - covered)
            if est is not None:
                pnl, source = pnl + est, "partial_plus_estimate"
    body = {"coin": ent["coin"], "name": ent["name"], "position_before": pos, "resp": resp,
            "n_fills": len(fills), "pnl_usd": round(pnl, 4), "exit_avg_px": close_px,
            "fee_usd": round(fee, 6), "mid_at_exit": mid, "fills_complete": complete, "pnl_source": source}
    if poll_err:
        body["poll_error"] = poll_err
    rec = led.append("exit", w.wid, body)
    try:
        if complete:
            msg = (f"{ent['name']}：没有成交，不赚不亏" if not fills else
                   f"{ent['name']}：已平仓，这一笔真实盈亏 ${pnl:+.2f}（含手续费）。累计 ${realized_total(settings):+.2f}")
        else:
            note = {"resp_estimate": "（按平仓返回估算：已含入场手续费，平仓手续费没算进去）",
                    "partial_plus_estimate": "（已读到的成交按真实算，其余按平仓返回估算：平仓手续费只含已读到的部分）"}.get(source, "")
            msg = (f"{ent['name']}：已平仓，成交明细稍后核对。暂记盈亏 ${pnl:+.2f}{note}"
                   "（平仓成交回报没读全，盈亏可能不准，请对一下 Hyperliquid）")
        notify.push(settings, "自动下单结果", msg, priority="high")
    except Exception:  # noqa: BLE001
        log.exception("exit push failed")
    return rec


def summary(settings) -> dict:
    rows = _led(settings).all()
    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(r["weekend"], {})[r["kind"]] = r
    return {"total_usd": realized_total(settings), "weekends": by}
