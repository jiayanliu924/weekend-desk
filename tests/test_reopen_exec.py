"""重开真钱执行器（desk/rx.py）：全部用假交易所 + 假时钟，不联网、不真下单。
覆盖：方向 / 阈值、名义与硬上限、off 不发单、T0+120 撤单、出场 ALO / 超时 IOC / 硬时间止损、当日 / 累计亏损止损、持仓数上限、halt 标志、数据过期、事件表、配置夹紧。"""
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from desk import autotrade, config, notify
from desk import reopen_exec as rx
from desk.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
NS = rx.NS
COIN = "xyz:SP500"
NOM = 1_760_000_000 * NS          # 名义时刻（任意；detect_t0 只看相对时间）
NOM_S = 1_760_000_000


@pytest.fixture
def s(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(rx, "_halted", lambda settings: set())
    monkeypatch.setattr(autotrade, "_client_factory", None)
    st = config.load(tmp_path)
    st.raw["reopen"] = {**st.raw["reopen"], "coins": ["SP500"]}
    return st


@pytest.fixture(autouse=True)
def pushes(monkeypatch):
    got = []
    monkeypatch.setattr(notify, "push", lambda settings, title, body, priority="default": got.append((title, body, priority)) or True)
    return got


class Sim:
    """假时钟 + 假交易所。sleep 推进时钟并触发到点的剧本回调。"""

    def __init__(self, t=NOM_S - 120.0):
        self.t = t
        self.oracle, self.mid = 100.0, 100.0
        self.half = 0.0001            # 盘口半价差（比例）
        self.depth = 50_000.0
        self.orders, self.fills, self.leverage = {}, {}, []
        self.pos, self.avail, self.next_oid = 0.0, 5000.0, 1
        self.hooks, self.ctx_fail, self.cancels = [], False, []
        self.address = "0xtest"

    # 时钟
    def now(self): return self.t

    def sleep(self, d):
        self.t += d
        for h in [h for h in self.hooks if h[0] <= self.t]:
            self.hooks.remove(h)
            h[1]()

    def at(self, t, fn): self.hooks.append((t, fn))

    # 行情
    def ctxs(self, coins):
        if self.ctx_fail:
            raise ConnectionError("down")
        return {c: {"oracle": self.oracle, "mid": self.mid, "oi": 1000.0} for c in coins}

    def book(self, coin):
        return {"bid": self.mid * (1 - self.half), "ask": self.mid * (1 + self.half), "bid_depth_usd": self.depth, "ask_depth_usd": self.depth}

    def sz_decimals(self, coin): return 2
    def balance(self): return {"available": self.avail}
    def position(self, coin): return self.pos
    def set_leverage(self, coin, lev): self.leverage.append((coin, lev)); return {"status": "ok"}

    # 下单
    def alo(self, coin, is_buy, sz, px, reduce_only, cloid):
        b = self.book(coin)
        crosses = px >= b["ask"] if is_buy else px <= b["bid"]
        rec = {"coin": coin, "is_buy": is_buy, "sz": sz, "px": px, "ro": reduce_only, "tif": "Alo", "cloid": cloid,
               "hex": rx.cloid_hex(cloid), "filled": 0.0, "status": "open"}
        oid = self.next_oid
        self.next_oid += 1
        self.orders[oid] = rec
        if crosses:
            rec["status"] = "rejected"
            return {"oid": None, "error": "Post only order would have immediately matched", "filled_sz": 0.0, "avg_px": None}
        return {"oid": oid, "error": None, "filled_sz": 0.0, "avg_px": None}

    def ioc(self, coin, is_buy, sz, px, reduce_only, cloid):
        b = self.book(coin)
        price = b["ask"] if is_buy else b["bid"]
        rec = {"coin": coin, "is_buy": is_buy, "sz": sz, "px": px, "ro": reduce_only, "tif": "Ioc", "cloid": cloid,
               "hex": rx.cloid_hex(cloid), "filled": 0.0, "status": "done"}
        self.orders[self.next_oid] = rec
        self.next_oid += 1
        if (is_buy and px < price) or (not is_buy and px > price):
            return {"oid": None, "error": None, "filled_sz": 0.0, "avg_px": None}
        if reduce_only:
            red = max(0.0, -self.pos) if is_buy else max(0.0, self.pos)
            sz = min(sz, red)
        rec["filled"] = sz
        self.pos += sz if is_buy else -sz
        self.fills[rec["hex"]] = (sz, price)
        return {"oid": None, "error": None, "filled_sz": sz, "avg_px": price}

    def fill_oid(self, oid, frac=1.0):
        o = self.orders[oid]
        q = round(o["sz"] * frac, 2)
        o["filled"] = q
        o["status"] = "filled" if frac >= 1 else "open"
        self.pos += q if o["is_buy"] else -q
        self.fills[o["hex"]] = (q, o["px"])

    def cancel(self, coin, oid, cloid=None):
        self.cancels.append(oid)
        o = self.orders[oid]
        if o["status"] == "open":
            o["status"] = "canceled"

    def order_info(self, oid):
        o = self.orders[oid]
        rem = 0.0 if o["status"] == "filled" else o["sz"] - o["filled"]
        return {"status": o["status"], "orig_sz": o["sz"], "sz": rem}

    def fill_for_cloid(self, coin, hex_, since): return self.fills.get(hex_)

    def alos(self, purpose=None):
        return [(k, o) for k, o in self.orders.items() if o["tif"] == "Alo" and (purpose is None or purpose in o["cloid"])]


def seed(jitter=True):
    """T0 前 10 分钟的 ctx：每 3 秒一行，oracle 围绕 100 做 ±1 bps 的小抖动（steps 约 1–2 bps，中位数 > 0）。"""
    rows = []
    for i in range(200):
        t = NOM - (600 - 3 * i) * NS
        o = 100.0 * (1 + (1e-4 if i % 2 else 0.0)) if jitter else 100.0
        rows.append((t, o, o, 1000.0))
    return rows


def make(s, sim, names=("SP500",), dry=False, **cf):
    s.raw["reopen"] = {**s.raw["reopen"], **cf}
    ev = rx.Event("weekly", date(2025, 10, 12), NOM, list(names))
    return rx.Runner(s, ev, sim, rx.cfg(s), dry, clock=sim.now, sleep=sim.sleep, seed_fn=lambda *a: seed())


def jump(sim, o, mid, at=NOM_S + 5):
    def f():
        sim.oracle, sim.mid = o, mid
    sim.at(at, f)


def st(s): return rx.load_state(s)


def only_pos(s):
    ps = list(st(s)["pos"].values())
    assert len(ps) == 1
    return ps[0]


# ------------------------------------------------------------------ 方向 / 阈值
def test_overshoot_up_sells_at_best_ask_and_notional(s):
    rx.put(s, mode="live")
    sim = Sim()
    jump(sim, 101.0, 101.2)            # G=+1%，S=+1；mid/oracle−1 = +19.8 bps > 10
    r = make(s, sim)
    sim.at(NOM_S + 60, lambda: None)
    for _ in range(200):               # 手动推进到 T0 之后一段时间
        r.seed() if not r.samples["SP500"] else None
        if sim.t > NOM_S + 40:
            break
        r.tick()
        sim.sleep(1.0)
    ents = sim.alos("entry")
    assert len(ents) == 1
    o = ents[0][1]
    assert o["is_buy"] is False and not o["ro"] and o["tif"] == "Alo"
    assert o["px"] == autotrade.round_px(sim.book(COIN)["ask"], 2)
    assert 450 <= o["sz"] * o["px"] <= 500.0 + 1e-6
    assert sim.leverage == [(COIN, 1)]
    assert r.final["SP500"] == "entered"


def test_overshoot_down_buys_at_best_bid(s):
    rx.put(s, mode="live")
    sim = Sim()
    jump(sim, 99.0, 98.8)              # G=−1%，S=−1；S·(mid/oracle−1) = +20 bps
    r = make(s, sim)
    r.run()
    ents = sim.alos("entry")
    assert len(ents) == 1 and ents[0][1]["is_buy"] is True
    assert ents[0][1]["px"] == autotrade.round_px(98.8 * (1 - 0.0001), 2)


def test_below_threshold_no_order_and_k_configurable(s):
    rx.put(s, mode="live")
    sim = Sim()
    jump(sim, 101.0, 101.05)           # +5 bps < 10
    r = make(s, sim)
    r.run()
    assert not sim.orders and not st(s)["pos"]
    assert r.final["SP500"] == "window_closed" and 4 < r.max_ovr["SP500"] < 6
    sim2 = Sim()
    jump(sim2, 101.0, 101.05)
    r2 = make(s, sim2, k_bps=3.0)
    r2.run()
    assert len(sim2.alos("entry")) == 1


def test_notional_hard_cap_and_depth_cap(s):
    rx.put(s, mode="live")
    assert rx.cfg(s)["notional_usd"] == 500.0
    sim = Sim()
    jump(sim, 101.0, 101.2)
    make(s, sim, notional_usd=5000.0).run()
    o = sim.alos("entry")[0][1]
    assert o["sz"] * o["px"] <= rx.NOTIONAL_HARD_CAP + 1e-6
    sim = Sim()
    sim.depth = 2000.0                 # 10% = $200
    jump(sim, 101.0, 101.2)
    make(s, sim).run()
    o = [o for _, o in sim.alos("entry")][0]
    assert o["sz"] * o["px"] <= 200.0 + 1e-6


# ------------------------------------------------------------------ off = 干跑
def test_off_mode_never_places_orders_but_logs_decision(s):
    assert rx.get(s)["mode"] == "off"
    sim = Sim()
    jump(sim, 101.0, 101.2)
    r = make(s, sim, dry=True)
    r.run()
    assert not sim.orders and not sim.leverage and not st(s)["pos"]
    dec = [x for x in Ledger(s.data, "reopen").all() if x["kind"] == "decision"]
    assert any(x["what"] == "would_enter" for x in dec) and all(x["dry"] for x in dec)
    assert r.final["SP500"] == "dry_signal"


def test_mode_file_default_off_and_roundtrip(s):
    assert rx.get(s)["mode"] == "off"
    rx.put(s, mode="live")
    assert rx.get(s)["mode"] == "live"
    rx.put(s, mode="bogus")
    assert rx.get(s)["mode"] == "off"


# ------------------------------------------------------------------ 撤单 / 出场 / 时间止损
def test_unfilled_entry_cancelled_at_t0_plus_120(s):
    rx.put(s, mode="live")
    sim = Sim()
    jump(sim, 101.0, 101.2)
    r = make(s, sim)
    r.run()
    p = only_pos(s)
    assert p["closed"]["reason"] == "no_fill"
    assert sim.cancels == [1]
    # 撤单发生在 T0+120s 之后、不早于它（T0 ≈ NOM+5）
    assert p["closed"]["t_ms"] / 1000 >= NOM_S + 5 + 120 - 1.5
    assert sim.pos == 0.0


def test_not_cancelled_before_deadline(s):
    rx.put(s, mode="live")
    sim = Sim()
    jump(sim, 101.0, 101.2)
    r = make(s, sim)
    r.seed()
    while sim.t < NOM_S + 5 + 100:
        r.tick()
        sim.sleep(1.0)
    assert sim.cancels == [] and len(sim.alos("entry")) == 1


def _enter_and_fill(s, sim, fill_at, **cf):
    r = make(s, sim, **cf)
    jump(sim, 101.0, 101.2)
    sim.at(fill_at, lambda: sim.fill_oid(1))
    return r


def test_pushes_off_by_default(s, pushes):
    assert rx.cfg(s)["notify"] is False
    rx._push(s, "t", "b")
    assert pushes == []
    s.raw["reopen"]["notify"] = True
    rx._push(s, "t", "b")
    assert len(pushes) == 1


def test_exit_posted_at_oracle_and_filled_closes_with_pnl(s, pushes):
    s.raw["reopen"]["notify"] = True
    rx.put(s, mode="live")
    sim = Sim()
    r = _enter_and_fill(s, sim, NOM_S + 20)
    # 出场 ALO（买回）在 oracle 101.0：盘口 bid 约 101.19，ask 约 101.21，101.0 < ask 不吃单
    def fill_exit():
        ex = sim.alos("exit_alo")
        assert len(ex) == 1
        k, o = ex[0]
        assert o["ro"] is True and o["is_buy"] is True and o["px"] == 101.0
        sim.fill_oid(k)
    sim.at(NOM_S + 40, fill_exit)
    r.run()
    p = only_pos(s)
    assert p["closed"]["reason"] == "oracle_exit" and abs(p["net"]) < 1e-9
    sz = sim.orders[1]["sz"]
    entry_px = sim.orders[1]["px"]
    gross = sz * (entry_px - 101.0)
    fees = 3e-4 * sz * entry_px * 0 + (3 / 1e4) * sz * entry_px + (3 / 1e4) * sz * 101.0
    assert p["closed"]["pnl_usd"] == pytest.approx(gross - fees, abs=1e-3)
    titles = " ".join(t for t, _, _ in pushes)
    assert "重开成交" in titles and "重开已平仓" in titles


def test_exit_timeout_after_10min_uses_reduce_only_ioc(s):
    rx.put(s, mode="live")
    sim = Sim()
    r = _enter_and_fill(s, sim, NOM_S + 20)
    r.run()
    p = only_pos(s)
    assert p["closed"]["reason"] == "exit_timeout"
    iocs = [o for o in sim.orders.values() if o["tif"] == "Ioc"]
    assert len(iocs) == 1 and iocs[0]["ro"] is True and iocs[0]["is_buy"] is True
    assert abs(sim.pos) < 1e-9
    assert p["closed"]["t_ms"] / 1000 >= NOM_S + 20 + 600 - 2


def test_hard_time_stop(s):
    rx.put(s, mode="live")
    sim = Sim()
    r = _enter_and_fill(s, sim, NOM_S + 20, hard_stop_s=300)
    r.run()
    p = only_pos(s)
    assert p["closed"]["reason"] == "time_stop"
    assert p["closed"]["t_ms"] / 1000 < NOM_S + 5 + 300 + 5
    assert any(o["tif"] == "Ioc" and o["ro"] for o in sim.orders.values()) and abs(sim.pos) < 1e-9
    assert rx.cfg(s)["hard_stop_s"] == 300


def test_hard_stop_cannot_exceed_15_min(s):
    s.raw["reopen"]["hard_stop_s"] = 99999
    assert rx.cfg(s)["hard_stop_s"] == 900


def test_exit_alo_crossing_book_goes_straight_to_ioc(s):
    rx.put(s, mode="live")
    sim = Sim()
    r = _enter_and_fill(s, sim, NOM_S + 20)
    sim.at(NOM_S + 19, lambda: setattr(sim, "oracle", 102.0))     # oracle 冲到盘口之上 → 买回 ALO 会吃单
    sim.at(NOM_S + 19, lambda: setattr(sim, "mid", 100.5))
    r.run()
    p = only_pos(s)
    assert p["closed"]["reason"] == "oracle_crossed_book" and not sim.alos("exit_alo")


# ------------------------------------------------------------------ 亏损止损 / 持仓上限 / 标志 / 数据过期
def _closed(s, pnl, t_ms):
    led = Ledger(s.data, "reopen")
    pid = f"old-{t_ms}-{pnl}"
    led.append("open", pid, {"pos_id": pid, "coin": "xyz:XYZ100", "event": "x", "t0_ms": t_ms, "entry_deadline_ms": t_ms, "entry_cloid": "c"})
    led.append("closed", pid, {"pos_id": pid, "coin": "xyz:XYZ100", "reason": "x", "pnl_usd": pnl, "t_ms": t_ms})


def test_daily_loss_stop_blocks_entry(s):
    rx.put(s, mode="live")
    sim = Sim()
    _closed(s, -15.5, int(sim.t * 1000) - 3_600_000 * 0)        # 今天（ET）的亏损
    # 保证"今天"：把时钟固定在落在当日内的时刻
    jump(sim, 101.0, 101.2)
    r = make(s, sim)
    r.run()
    assert not sim.alos() and r.final["SP500"] == "blocked"
    dec = [x for x in Ledger(s.data, "reopen").all() if x["kind"] == "decision" and x["what"] == "blocked"]
    assert "当日亏损止损" in dec[0]["why"]


def test_daily_cap_is_15_even_if_config_larger(s):
    s.raw["reopen"]["daily_loss_stop_usd"] = 500
    s.raw["reopen"]["cum_loss_stop_usd"] = 5000
    assert rx.cfg(s)["daily_loss_stop_usd"] == 15.0 and rx.cfg(s)["cum_loss_stop_usd"] == 50.0


def test_cumulative_loss_stop_halts_and_writes_flag(s):
    rx.put(s, mode="live")
    sim = Sim()
    old = int((sim.t - 5 * 86400) * 1000)                       # 前几天的亏损：不触发当日，但累计 −51
    _closed(s, -26.0, old)
    _closed(s, -25.0, old + 1)
    jump(sim, 101.0, 101.2)
    r = make(s, sim)
    r.run()
    assert not sim.alos()
    h = rx.get(s)
    assert h["halt_entries"] is True and "累计" in h["halt_reason"] and h["mode"] == "live"


def test_open_position_loss_forces_daily_stop_exit(s):
    rx.put(s, mode="live")
    sim = Sim()
    r = _enter_and_fill(s, sim, NOM_S + 20)
    # 我们空 ~4.9 个，价格涨到 110 → 浮亏 ≈ 4.9×(110−101.2) ≈ 43 > 15
    sim.at(NOM_S + 40, lambda: (setattr(sim, "mid", 110.0), setattr(sim, "oracle", 110.0)))
    r.run()
    p = only_pos(s)
    assert p["closed"]["reason"] == "daily_loss_stop"
    assert abs(sim.pos) < 1e-9 and any(o["tif"] == "Ioc" and o["ro"] for o in sim.orders.values())


def test_max_one_position_per_coin(s):
    rx.put(s, mode="live")
    sim = Sim()
    led = Ledger(s.data, "reopen")
    led.append("open", "p1", {"pos_id": "p1", "coin": COIN, "event": "e", "t0_ms": 0, "entry_deadline_ms": 0, "entry_cloid": "c1"})
    m = rx.MCtx(s, sim, rx.cfg(s), int(sim.t * 1000), False)
    why = rx.entry_block(m, st(s), COIN, sim.t * 1000)
    assert why and "每币最多" in why
    assert rx.entry_block(m, st(s), "xyz:XYZ100", sim.t * 1000) is None


def test_max_three_concurrent(s):
    rx.put(s, mode="live")
    sim = Sim()
    led = Ledger(s.data, "reopen")
    for i, c in enumerate(["xyz:A", "xyz:B", "xyz:C"]):
        led.append("open", f"p{i}", {"pos_id": f"p{i}", "coin": c, "event": "e", "t0_ms": 0, "entry_deadline_ms": 0, "entry_cloid": f"c{i}"})
    s.raw["reopen"]["max_concurrent"] = 99
    m = rx.MCtx(s, sim, rx.cfg(s), int(sim.t * 1000), False)
    assert rx.cfg(s)["max_concurrent"] == 3
    assert "上限" in rx.entry_block(m, st(s), "xyz:D", sim.t * 1000)


def test_halt_flag_and_account_stop_block(s, monkeypatch):
    rx.put(s, mode="live", halt_entries=True, halt_reason="t")
    sim = Sim()
    jump(sim, 101.0, 101.2)
    make(s, sim).run()
    assert not sim.orders
    rx.put(s, halt_entries=False)
    from desk import account_stop
    monkeypatch.setattr(account_stop, "check", lambda settings: {"tripped": True})
    sim = Sim()
    jump(sim, 101.0, 101.2)
    make(s, sim).run()
    assert not sim.orders


def test_halted_coin_blocks(s, monkeypatch):
    rx.put(s, mode="live")
    monkeypatch.setattr(rx, "_halted", lambda settings: {COIN})
    sim = Sim()
    jump(sim, 101.0, 101.2)
    make(s, sim).run()
    assert not sim.orders


def test_stale_data_blocks_entry_and_allows_retry(s):
    rx.put(s, mode="live")
    sim = Sim()
    m = rx.MCtx(s, sim, rx.cfg(s), int(sim.t * 1000), False)
    ev = rx.Event("weekly", date(2025, 10, 12), NOM, ["SP500"])
    res = rx.try_enter(m, ev, COIN, 1, 101.0, 101.2, NOM, last_ok_ms=sim.t * 1000 - 11_000)
    assert not res["entered"] and res["retry"] and "没更新" in res["why"]
    assert not sim.orders
    res = rx.try_enter(m, ev, COIN, 1, 101.0, 101.2, NOM, last_ok_ms=sim.t * 1000 - 5_000)
    assert res["entered"]


def test_poll_failures_never_trade(s):
    rx.put(s, mode="live")
    sim = Sim()
    jump(sim, 101.0, 101.2)
    sim.ctx_fail = True
    make(s, sim).run()
    assert not sim.orders


def test_low_balance_blocks(s):
    rx.put(s, mode="live")
    sim = Sim()
    sim.avail = 3.0
    jump(sim, 101.0, 101.2)
    make(s, sim).run()
    assert not sim.orders


def test_existing_manual_position_blocks(s):
    rx.put(s, mode="live")
    sim = Sim()
    sim.pos = 2.0
    jump(sim, 101.0, 101.2)
    make(s, sim).run()
    assert not sim.orders


def test_manage_runs_even_when_mode_off(s):
    """off 时已有仓位仍会平（和 pairs_exec 一样：模式切换不会让仓位变孤儿）。"""
    rx.put(s, mode="live")
    sim = Sim()
    r = _enter_and_fill(s, sim, NOM_S + 20)
    r.seed()
    while sim.t < NOM_S + 40:
        r.tick()
        sim.sleep(1.0)
    assert abs(sim.pos) > 0
    rx.put(s, mode="off")
    sim.t = NOM_S + 5 + 901
    m = rx.MCtx(s, sim, rx.cfg(s), int(sim.t * 1000), True)
    rx.manage_all(m)
    assert abs(sim.pos) < 1e-9


# ------------------------------------------------------------------ 事件表 / 调度
def test_events_sunday_and_weekday(s):
    cf = {"coins": ["SP500", "NVDA", "BRENTOIL"]}
    sun = rx.events_on(date(2025, 10, 12), cf)          # 周日
    assert [e.kind for e in sun] == ["weekly", "weekly"]
    hhmm = [datetime.fromtimestamp(e.nominal_ns / NS, timezone.utc).astimezone(rx.ET).strftime("%H%M") for e in sun]
    assert hhmm == ["1800", "2005"] and sun[0].names == ["BRENTOIL", "SP500"] and sun[1].names == ["NVDA"]
    tue = rx.events_on(date(2025, 10, 14), cf)          # 周二：只有 18:00 的指数 / 商品
    assert [e.kind for e in tue] == ["daily_18"] and tue[0].names == ["BRENTOIL", "SP500"]
    assert rx.events_on(date(2025, 10, 17), cf) == []   # 周五没有
    assert rx.event_from_key(tue[0].key, cf).nominal_ns == tue[0].nominal_ns


def test_due_wakes_before_event_and_not_when_done(s):
    cf = rx.cfg(s)
    ev = rx.events_on(date(2025, 10, 14), cf)[0]
    nom = datetime.fromtimestamp(ev.nominal_ns / NS, timezone.utc)
    from datetime import timedelta
    done = set()
    assert rx.due(nom - timedelta(minutes=4), s, done) == [ev.key]
    assert rx.due(nom - timedelta(minutes=10), s, done) == []
    assert rx.due(nom + timedelta(minutes=5), s, done) == []
    done.add(ev.key)
    assert rx.due(nom - timedelta(minutes=1), s, done) == []


def test_scheduler_registers_reopen(s):
    pytest.importorskip("feedparser")
    from desk import scheduler
    cf = rx.cfg(s)
    ev = rx.events_on(date(2025, 10, 14), cf)[0]
    now = datetime.fromtimestamp(ev.nominal_ns / NS - 120, timezone.utc)
    keys = [k for k, _ in scheduler.due_jobs(now, s, set())]
    assert ev.key in keys


def test_review_poll_floor_and_default(s):
    assert rx.cfg(s)["poll_s"] >= 3.0
    s.raw.setdefault("reopen", {})["poll_s"] = 0.5
    assert rx.cfg(s)["poll_s"] == rx.POLL_FLOOR_S


def test_review_account_stop_includes_reopen(s):
    from desk import account_stop
    led = rx._led(s)
    led.append("open", "p1", {"pos_id": "p1", "coin": "xyz:SP500", "event": "e", "entry_cloid": "c"})
    led.append("closed", "p1", {"pos_id": "p1", "coin": "xyz:SP500", "reason": "time_stop", "pnl_usd": -12.0, "t_ms": 1})
    c = account_stop.components(s)
    assert c["reopen_usd"] == -12.0 and c["total_usd"] <= -12.0
