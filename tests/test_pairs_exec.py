"""跨 dex 配对真下单（desk/pairs_exec.py）：全部用注入的假客户端和确定性假数据，不连交易所。
覆盖：off/shadow 不发单、live 两腿开仓、一腿被拒回补、部分成交回补、平仓只用 reduce-only 且只平我们自己的量（手动仓位不动）、
各种上限拦截、当日亏损止损切回 off、cloid 前缀、杠杆封顶、异常路径不留单腿。"""
import json
import logging
import shutil
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest

from desk import autotrade, config
from desk import backtest_lib as bl
from desk import pairs_exec as px
from desk import pairs_lab as pl
from desk.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
IV = bl.INTERVAL_MS["5m"]
# 整天对齐，再 +3 天：now 落在纽约周二晚上，避开默认的休市禁开窗口（周五 19:30 → 周日 20:35）
T0 = 1_760_000_000_000 // pl.DAY_MS * pl.DAY_MS + 3 * pl.DAY_MS
A, B = "io:SNDK", "xyz:SNDK"      # A（io）在 pairs_lab 里排第一：s>0 = A 贵 = 空 A 多 B
A2, B2 = "io:NBIS", "xyz:NBIS"


@pytest.fixture
def s(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    st = config.load(tmp_path)
    # 默认只做 SNDK 这一对，免得测试之间互相干扰；个别测试自己放开
    st.raw["pairs"] = {**st.raw.get("pairs", {}), "allow": [f"{A}|{B}"]}
    return st


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    sleeps = []
    monkeypatch.setattr(px, "_sleep", lambda x: sleeps.append(x))
    px._FUT.clear()
    px._CLIENT.clear()
    px._DEXSET.clear()
    return sleeps


def set_mode(s, mode):
    px.put(s, mode=mode)


def cfg_set(s, **kw):
    s.raw["pairs"] = {**s.raw["pairs"], **kw}


# ---------------------------------------------------------------- 假数据：每 24 根出现 2 根 +80 bps 的价差，最后一根正好是尖峰
def candles(coin_prices, shift=0):
    return [{"t": T0 + shift + i * IV, "c": str(p), "v": "10"} for i, p in enumerate(coin_prices)]


def make_data(n_days=17, tail=(), base=120.0, allow_second=False, day_shift=0):
    """B 恒为 base，A = base·(1+s/1e4)。n = n_days·288 + 1 根，最后一根（序号 n−1，≡ 0 mod 24）是尖峰；
    tail 是追加在后面的价差（bps），例如 (0, 0) 表示价差已收敛。返回 (data, now)：now = 最后一根收盘后 20 秒。"""
    n = n_days * 288 + 1
    sp = [80.0 if (i % 24) in (0, 1) else 0.0 for i in range(n)] + list(tail)
    # 两腿共同的小幅价格噪声（不改价差）：免得 B 腿"收盘价一动不动"被腿不活跃守卫误伤
    bs = [base * (1 + 1e-4 * ((i % 7) - 3)) for i in range(len(sp))]
    pa = [b_ * (1 + x / 1e4) for b_, x in zip(bs, sp)]
    pb = bs
    raw = pl.fixture_raw()
    pairs = [p for p in pl.discover_pairs(raw, {"min_day_ntl_vlm_usd": 2e5, "min_oi_usd": 1e5, "max_spread_bps": 20.0})
             if p["kind"] == "same" and p["gate_ok"]]
    sh = day_shift * pl.DAY_MS
    cs = {A: candles(pa, sh), B: candles(pb, sh)}
    if allow_second:
        cs.update({A2: candles(pa, sh), B2: candles(pb, sh)})
    pairs = [{**p, "strict": True} for p in pairs if p["a"] in cs]      # 夹具里的配对都过严门槛
    data = {"pairs": pairs, "candles": cs, "funding": {}}
    now = datetime.fromtimestamp((T0 + sh + len(sp) * IV) / 1000 + 20, timezone.utc)
    return data, now


class FakeClient:
    """假交易所：盘口、IOC 限价撮合（尊重限价和 reduce-only）、持仓净额。close / market_close 一旦被调用就是 bug。"""

    def __init__(self, mids=None, depth=5000.0):
        self.mids = dict(mids or {A: 120.0 * 1.008, B: 120.0, A2: 120.0 * 1.008, B2: 120.0})
        self.depth = {c: depth for c in self.mids}
        self.pos = {}
        self.orders = []          # 发给"交易所"的每张单
        self.leverage = {}
        self.reject = set()        # 这些 coin 的 IOC 被拒
        self.partial = {}          # coin -> 成交比例
        self.raise_on = set()      # 这些 coin 下单直接抛异常（超时），但"其实没成交"
        self.raise_filled = set()  # 抛异常，但其实成交了
        self.avail = 350.0
        self.wide = set()          # 这些 coin 的盘口买卖价差极宽（中间价不可信）
        self.marks = {}            # coin -> mark_px（没设置 → mark_px 返回 None）
        self.funding = []          # funding_rows 返回的行
        self.cloid_fills = {}      # cloid_hex -> (sz, px)：fill_for_cloid 查得到的真实成交
        self.lev_resp = None
        self.lev_calls = []
        self.balance_raises = False

    def balance(self):
        if self.balance_raises:
            raise ConnectionError("balance down")
        return {"main": 0.0, "xyz": 0.0, "spot": self.avail, "available": self.avail, "withdrawable_xyz": 0.0}

    def sz_decimals(self, coin):
        return 2

    def position(self, coin):
        return self.pos.get(coin, 0.0)

    def book(self, coin):
        m = self.mids[coin]
        w = 0.3 if coin in self.wide else 0.0005
        bid, ask = m * (1 - w), m * (1 + w)
        return {"bid": bid, "ask": ask, "bid_depth_usd": self.depth[coin], "ask_depth_usd": self.depth[coin]}

    def set_leverage(self, coin, lev):
        self.lev_calls.append((coin, lev))
        self.leverage[coin] = lev
        return self.lev_resp

    def mark_px(self, coin):
        return self.marks.get(coin)

    def funding_rows(self, since_ms):
        return list(self.funding)

    def fill_for_cloid(self, coin, cloid_hex_, since_ms):
        return self.cloid_fills.get(cloid_hex_)

    def _match(self, coin, is_buy, sz, px_, reduce_only):
        b = self.book(coin)
        price = b["ask"] if is_buy else b["bid"]
        if (is_buy and px_ < price) or (not is_buy and px_ > price):
            return 0.0, price           # 限价不够 → IOC 不成交
        pos = self.pos.get(coin, 0.0)
        if reduce_only:
            reducible = max(0.0, -pos) if is_buy else max(0.0, pos)
            if reducible <= 0:
                return None, price      # 会加仓 → 交易所拒单
            sz = min(sz, reducible)
        sz *= self.partial.get(coin, 1.0)
        sz = int(sz * 100 + 1e-9) / 100
        self.pos[coin] = pos + (sz if is_buy else -sz)
        return sz, price

    def ioc(self, coin, is_buy, sz, px_, reduce_only, cloid):
        self.orders.append({"coin": coin, "is_buy": is_buy, "sz": sz, "px": px_, "reduce_only": reduce_only, "cloid": cloid})
        if coin in self.raise_on:
            raise TimeoutError("timeout")
        if coin in self.raise_filled:
            self._match(coin, is_buy, sz, px_, reduce_only)
            raise TimeoutError("timeout after fill")
        if coin in self.reject:
            return {"filled_sz": 0.0, "avg_px": None, "error": "rejected"}
        f, price = self._match(coin, is_buy, sz, px_, reduce_only)
        if f is None:
            return {"filled_sz": 0.0, "avg_px": None, "error": "Reduce only order would increase position"}
        return {"filled_sz": f, "avg_px": price if f else None, "error": None}

    def close(self, *a, **k):
        raise AssertionError("market_close / close 绝不能被调用")

    market_close = close


def live_entry(s, fc, **kw):
    set_mode(s, "live")
    data, now = make_data(**kw)
    return px.step(s, now, client=fc, data=data), data, now


def pairs_state(s):
    return px.load_state(s)["pairs"]


# ---------------------------------------------------------------- off / shadow
def test_default_mode_is_off_and_separate_file(s):
    assert px.get(s)["mode"] == "off"
    px.put(s, mode="shadow")
    assert (Path(s.data) / "state" / "pairs_live.json").exists()
    assert autotrade.get(s)["mode"] == "off"      # autotrade 的 live.json 不受影响


def test_off_sends_nothing(s):
    fc = FakeClient()
    data, now = make_data()
    out = px.step(s, now, client=fc, data=data)
    assert out["entries"] == [] and fc.orders == [] and fc.leverage == {}
    assert Ledger(s.data, "pairs").all() == []


def test_shadow_sends_nothing_but_logs_plan_once(s):
    fc = FakeClient()
    set_mode(s, "shadow")
    data, now = make_data()
    out = px.step(s, now, client=fc, data=data)
    assert len(out["plans"]) == 1
    p = out["plans"][0]
    assert p["rich_short"] == A and p["cheap_long"] == B and p["sent"] is False
    assert fc.orders == [] and fc.leverage == {} and fc.pos == {}
    assert [r["kind"] for r in Ledger(s.data, "pairs").all()] == ["plan"]
    assert px.step(s, now, client=fc, data=data)["plans"] == []      # 同一根 K 线不重复推送
    assert fc.orders == []


# ---------------------------------------------------------------- live 开仓
def test_live_entry_places_both_legs_short_rich_long_cheap(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    assert out["entries"] and out["entries"][0]["opened"] and out["entries"][0]["balanced"]
    by_coin = {o["coin"]: o for o in fc.orders}
    assert set(by_coin) == {A, B}
    assert by_coin[A]["is_buy"] is False and by_coin[B]["is_buy"] is True        # 空贵的(A)、多便宜的(B)
    assert not by_coin[A]["reduce_only"] and not by_coin[B]["reduce_only"]
    # IOC 的限价在对手价外侧，且不超过滑点上限
    bkA, bkB = fc.book(A), fc.book(B)
    assert by_coin[A]["px"] <= bkA["bid"] and by_coin[A]["px"] >= bkA["bid"] * (1 - 0.0031)
    assert by_coin[B]["px"] >= bkB["ask"] and by_coin[B]["px"] <= bkB["ask"] * (1 + 0.0031)
    # 名义 ≈ 100，数量按 szDecimals=2 向下取整，等名义（差 ≤ 容忍度）
    nA, nB = -fc.pos[A] * bkA["bid"], fc.pos[B] * bkB["ask"]
    assert 90 < nA <= 100 and 90 < nB <= 100 and abs(nA - nB) <= 5
    assert round(fc.pos[A], 2) == fc.pos[A] and round(fc.pos[B], 2) == fc.pos[B]
    assert fc.leverage == {A: 2, B: 2}
    p = next(iter(pairs_state(s).values()))
    assert p["net"][A] == pytest.approx(fc.pos[A]) and p["net"][B] == pytest.approx(fc.pos[B])


def test_cloid_prefix_on_every_order_and_in_ledger(s):
    fc = FakeClient()
    live_entry(s, fc)
    assert fc.orders and all(o["cloid"].startswith("pairs-") for o in fc.orders)
    led = Ledger(s.data, "pairs").all()
    sent = [r for r in led if r["kind"] == "order"]
    fills = [r for r in led if r["kind"] == "fill"]
    assert [r["cloid"] for r in sent] == [o["cloid"] for o in fc.orders]
    assert all(r["cloid"].startswith("pairs-") and r["cloid_hex"].startswith("0x") and len(r["cloid_hex"]) == 34 for r in sent)
    assert {r["cloid"] for r in fills} == {r["cloid"] for r in sent}
    assert len({o["cloid"] for o in fc.orders}) == len(fc.orders)


def test_leverage_capped_at_2_even_if_config_says_more(s):
    cfg_set(s, leverage=10)
    fc = FakeClient()
    live_entry(s, fc)
    assert fc.leverage == {A: 2, B: 2}


# ---------------------------------------------------------------- 原子性
def test_second_leg_rejected_unwinds_first_leg_reduce_only(s):
    fc = FakeClient()
    fc.reject = {B}
    out, _, _ = live_entry(s, fc)
    assert out["entries"][0]["unwound"] is True
    assert abs(fc.pos.get(A, 0.0)) < 1e-9 and abs(fc.pos.get(B, 0.0)) < 1e-9      # 没有单腿
    legs = [o for o in fc.orders if o["coin"] == A]
    assert legs[0]["reduce_only"] is False and legs[0]["is_buy"] is False
    assert legs[-1]["reduce_only"] is True and legs[-1]["is_buy"] is True         # 买回来平空腿
    assert all(o["cloid"].startswith("pairs-") for o in fc.orders)
    p = next(iter(pairs_state(s).values()))
    assert p["closed"]["reason"] == "entry_failed:unwound"
    assert any(r["kind"] == "alert" for r in Ledger(s.data, "pairs").all())


def test_first_leg_rejected_second_never_sent(s):
    fc = FakeClient()
    fc.reject = {A}
    out, _, _ = live_entry(s, fc)
    assert [o["coin"] for o in fc.orders] == [A]
    assert fc.pos == {}
    assert next(iter(pairs_state(s).values()))["closed"]["reason"] == "entry_failed:first_leg"


def test_partial_fill_second_leg_unwinds_excess_of_first(s):
    fc = FakeClient()
    fc.partial = {B: 0.5}
    out, _, _ = live_entry(s, fc)
    assert out["entries"][0]["balanced"] is True
    bk = fc.book(A)
    nA, nB = -fc.pos[A] * bk["bid"], fc.pos[B] * fc.book(B)["ask"]
    assert abs(nA - nB) <= 5.0 + 1e-6                       # 净名义敞口落在容忍度内
    assert fc.pos[A] < 0 < fc.pos[B]                          # 仍然是一空一多
    excess = [o for o in fc.orders if o["coin"] == A and o["reduce_only"]]
    assert excess and all(o["is_buy"] for o in excess)         # 回补多出来的空腿 = 买回，reduce-only
    assert -fc.pos[A] < 0.82                                  # 比原来的 ~0.82 小


def test_partial_first_leg_sizes_second_leg_to_actual_fill(s):
    fc = FakeClient()
    fc.partial = {A: 0.3}
    live_entry(s, fc)
    first = [o for o in fc.orders if not o["reduce_only"]]
    assert first[0]["coin"] == A and first[1]["coin"] == B
    assert first[1]["sz"] < first[0]["sz"]                    # 第二腿按第一腿实际成交定量，不按原计划
    bk = fc.book(A)
    assert abs(-fc.pos[A] * bk["bid"] - fc.pos[B] * fc.book(B)["ask"]) <= 5.0


def test_exception_after_fill_without_price_is_unknown_then_adopted_by_reconcile(s):
    fc = FakeClient()
    fc.raise_filled = {B}                       # 第二腿其实成交了但回包超时，且查不到真实成交价
    out, data, now = live_entry(s, fc)
    assert out["entries"][0].get("error") == "unknown_order_state"
    fills = [r for r in Ledger(s.data, "pairs").all() if r["kind"] == "fill" and r["coin"] == B]
    assert fills[-1].get("unknown") is True and fills[-1].get("px") is None      # 绝不把限价当成交价记账
    assert px.get(s)["halt_entries"] is True
    out2 = px.step(s, datetime.fromtimestamp(now.timestamp() + 60, timezone.utc), client=fc, data=data)
    p = next(iter(pairs_state(s).values()))
    assert not p["unknown"] and p["net"][B] == pytest.approx(fc.pos[B]) and p["closed"] is None     # 按交易所观察到的数量认领
    assert any(r["kind"] == "fill" and r.get("resolves") and r["px_est"] for r in Ledger(s.data, "pairs").all())
    assert out2["entries"] == []                # 仍然停开新仓，直到手动清除


def test_exception_without_fill_is_unknown_not_confirmed_nofill_then_single_leg_unwound(s):
    fc = FakeClient()
    fc.raise_on = {B}                           # 第二腿回包超时，持仓没变、查不到成交
    out, data, now = live_entry(s, fc)
    assert out["entries"][0].get("error") == "unknown_order_state"       # 不再当成"确认没成交"
    assert px.get(s)["halt_entries"] is True and fc.pos[A] < 0
    px.step(s, later(now, 60), client=fc, data=data)                      # 对账：持仓没变 → 认领为 0；单腿 → 立即回补
    assert abs(fc.pos[A]) < 1e-9 and next(iter(pairs_state(s).values()))["closed"] is not None


def test_unknown_order_state_halts_entries_and_never_blind_reverses(s):
    class Blind(FakeClient):
        down = False

        def position(self, coin):
            if coin == B and self.down:
                raise ConnectionError("down")
            return super().position(coin)

        def ioc(self, coin, *a, **k):
            self.down = self.down or coin == B      # 发第二腿的同时，B 的持仓查询开始失败
            return super().ioc(coin, *a, **k)
    fc = Blind()
    fc.raise_on = {B}
    out, _, _ = live_entry(s, fc)
    assert out["entries"][0].get("error") == "unknown_order_state"
    assert px.get(s)["halt_entries"] is True
    assert not any(o["reduce_only"] for o in fc.orders)       # 不盲目反向下单
    # 之后不再开新仓
    data, now = make_data()
    assert px.step(s, now, client=fc, data=data)["entries"] == []


# ---------------------------------------------------------------- 平仓
def test_exit_is_reduce_only_our_size_only_manual_untouched(s):
    fc = FakeClient()
    live_entry(s, fc)
    ours = {c: q for c, q in next(iter(pairs_state(s).values()))["net"].items()}
    fc.pos[A] += -5.0                            # 开仓之后又手动加了与我们同向的仓位
    fc.pos[B] += 3.0
    assert ours[A] < 0 < ours[B]
    n_before = len(fc.orders)
    data, now = make_data(tail=(0.0, 0.0))      # 价差收敛
    now = datetime.fromtimestamp(now.timestamp() + 2 * IV / 1000, timezone.utc)
    out = px.step(s, now, client=fc, data=data)
    assert out["exits"] and out["exits"][0]["closed"] and out["exits"][0]["reason"].startswith("exit_")
    exits = fc.orders[n_before:]
    assert exits and all(o["reduce_only"] for o in exits)
    assert all(o["cloid"].startswith("pairs-") for o in exits)
    by = {o["coin"]: o for o in exits}
    assert by[A]["is_buy"] is True and by[B]["is_buy"] is False
    assert by[A]["sz"] == pytest.approx(-ours[A]) and by[B]["sz"] == pytest.approx(ours[B])     # 只平我们自己的量
    assert fc.pos[A] == pytest.approx(-5.0) and fc.pos[B] == pytest.approx(3.0)                  # 手动仓位原封不动
    assert next(iter(pairs_state(s).values()))["closed"] is not None


def test_exit_with_opposing_manual_position_refuses_instead_of_flipping(s):
    fc = FakeClient()
    live_entry(s, fc)
    ours = next(iter(pairs_state(s).values()))["net"]
    fc.pos[A] = ours[A] + 10.0                  # 手动多仓把净仓位翻成多：买回我们的空腿会加仓
    n_before = len(fc.orders)
    data, now = make_data(tail=(0.0, 0.0))
    now = datetime.fromtimestamp(now.timestamp() + 2 * IV / 1000, timezone.utc)
    out = px.step(s, now, client=fc, data=data)
    assert not out["exits"][0]["closed"]
    assert not any(o["coin"] == A for o in fc.orders[n_before:])    # 没发那张会加仓的单
    assert fc.pos[A] == pytest.approx(ours[A] + 10.0)
    assert any(r["kind"] == "alert" for r in Ledger(s.data, "pairs").all())


def test_max_hold_forces_exit(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    later = datetime.fromtimestamp(now.timestamp() + 25 * 3600, timezone.utc)
    out = px.step(s, later, client=fc, data=data)
    assert out["exits"][0]["reason"] == "max_hold" and out["exits"][0]["closed"]
    assert abs(fc.pos[A]) < 1e-9 and abs(fc.pos[B]) < 1e-9


def test_per_pair_loss_forces_exit(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.mids[A] *= 1.06                           # 空腿亏
    out = px.step(s, datetime.fromtimestamp(now.timestamp() + 60, timezone.utc), client=fc, data=data)
    assert out["exits"] and out["exits"][0]["reason"] == "per_pair_loss"
    assert abs(fc.pos[A]) < 1e-9


def test_daily_loss_stop_flattens_and_turns_mode_off(s):
    cfg_set(s, daily_loss_stop_usd=3.0, max_pair_loss_usd=100.0)
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.mids[A] *= 1.06
    out = px.step(s, datetime.fromtimestamp(now.timestamp() + 60, timezone.utc), client=fc, data=data)
    assert out.get("daily_stop") is True
    assert px.get(s)["mode"] == "off"
    assert abs(fc.pos[A]) < 1e-9 and abs(fc.pos[B]) < 1e-9
    n = len(fc.orders)
    assert px.step(s, now, client=fc, data=data)["entries"] == [] and len(fc.orders) == n    # off 之后什么都不发


# ---------------------------------------------------------------- 上限
def test_depth_cap_notional_at_most_10pct_of_top_of_book(s):
    fc = FakeClient(depth=400.0)                 # 10% = $40
    live_entry(s, fc)
    n = -fc.pos[A] * fc.book(A)["bid"]
    assert 0 < n <= 40.0


def test_depth_too_thin_blocks_entry(s):
    fc = FakeClient(depth=50.0)                  # 10% = $5 < 最小下单 $10
    out, _, _ = live_entry(s, fc)
    assert fc.orders == [] and out["entries"] == []


def test_max_leg_notional_cap(s):
    cfg_set(s, max_leg_notional_usd=30.0)
    fc = FakeClient()
    live_entry(s, fc)
    assert -fc.pos[A] * fc.book(A)["bid"] <= 30.0


def test_max_total_gross_cap(s):
    cfg_set(s, max_total_gross_usd=60.0)         # 两腿合计 ≤ 60 → 每腿 ≤ 30
    fc = FakeClient()
    live_entry(s, fc)
    gross = -fc.pos[A] * fc.book(A)["bid"] + fc.pos[B] * fc.book(B)["ask"]
    assert 0 < gross <= 60.0


def test_max_concurrent_blocks_second_pair(s):
    cfg_set(s, allow=[], max_concurrent=1)
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data(allow_second=True)
    out = px.step(s, now, client=fc, data=data)
    assert len(out["entries"]) == 1
    assert {o["coin"] for o in fc.orders} in ({A, B}, {A2, B2})
    assert any("上限" in r for _, r in out["skips"])
    # 并发放开到 2，但总敞口上限 $200 已被第一对用掉 → 仍然开不了第二对
    cfg_set(s, max_concurrent=2, max_total_gross_usd=200.0)
    n = len(fc.orders)
    out = px.step(s, datetime.fromtimestamp(now.timestamp() + 1, timezone.utc), client=fc, data=data)
    assert out["entries"] == [] and len(fc.orders) == n
    # 总敞口放宽到 $400 → 第二对可以开
    cfg_set(s, max_total_gross_usd=400.0)
    out = px.step(s, datetime.fromtimestamp(now.timestamp() + 2, timezone.utc), client=fc, data=data)
    assert len(out["entries"]) == 1
    assert {o["coin"] for o in fc.orders} == {A, B, A2, B2}


def test_balance_floor_blocks_entry_via_shared_preflight(s):
    fc = FakeClient()
    fc.avail = 1.0                               # 低于 min_balance_usd=5
    out, _, _ = live_entry(s, fc)
    assert fc.orders == [] and out["entries"] == []


def test_no_second_entry_on_same_bar_or_while_open(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    n = len(fc.orders)
    px.step(s, datetime.fromtimestamp(now.timestamp() + 60, timezone.utc), client=fc, data=data)
    assert len(fc.orders) == n


# ---------------------------------------------------------------- 信号 = pairs_lab（不偷看、只用已收盘的 K 线）
def test_signal_ignores_unclosed_bar_and_future_data(s):
    data, now = make_data()
    cf = px.cfg(s)
    now_ms = int(now.timestamp() * 1000)
    a = px.signal(data["pairs"][0], data, cf, now_ms)
    assert a["ok"] and a["spread"] == pytest.approx(80.0) and a["variant"].startswith("+")
    # 往后面塞一根"还没收盘"的 K 线：结果不变
    data2 = {**data, "candles": {c: v + [{"t": T0 + len(v) * IV, "c": "999", "v": "10"}] for c, v in data["candles"].items()}}
    b = px.signal(data["pairs"][0], data2, cf, now_ms)
    assert (b["bar_t"], b["spread"], b["thr"], b["variant"]) == (a["bar_t"], a["spread"], a["thr"], a["variant"])
    # 信号阈值 = pairs_lab 的成本 + 变体 buffer
    la, lb = (data["pairs"][0]["legs"][c] for c in (A, B))
    cost = pl.round_trip_cost(9.0, la["spread_bps"] / 2, lb["spread_bps"] / 2)
    assert a["thr"] == pytest.approx(cost + int(a["variant"].split("/")[0][1:]))


def test_stale_data_gives_no_signal(s):
    data, now = make_data()
    late = datetime.fromtimestamp(now.timestamp() + 3600, timezone.utc)
    assert px.signal(data["pairs"][0], data, px.cfg(s), int(late.timestamp() * 1000))["ok"] is False


def test_stale_data_never_trades(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    px.step(s, datetime.fromtimestamp(now.timestamp() + 3600, timezone.utc), client=fc, data=data)
    assert fc.orders == []


# ---------------------------------------------------------------- 回归：autotrade 行为不变；调度器接线
def test_autotrade_preflight_default_behavior_unchanged(s, monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(autotrade, "_client_factory", lambda: fc)
    assert autotrade._live_preflight(s, {"min_balance_usd": 5.0}, fc) is None
    fc.avail = 1.0
    assert "低于下限" in autotrade._live_preflight(s, {"min_balance_usd": 5.0}, fc)


def test_scheduler_only_schedules_pairs_when_mode_not_off(s):
    from desk import scheduler
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    assert not [k for k, _ in scheduler.due_jobs(now, s, set()) if k.startswith("pairs:")]
    px.put(s, mode="shadow")
    assert [k for k, _ in scheduler.due_jobs(now, s, set()) if k.startswith("pairs:")]


# ---------------------------------------------------------------- 异常路径 / 真实客户端的回包解析
def test_unexpected_exception_mid_entry_flattens_from_ledger(s):
    class Boom(FakeClient):
        def sz_decimals(self, coin):
            raise RuntimeError("meta down")
    fc = Boom()
    out, _, _ = live_entry(s, fc)
    assert out["entries"][0]["error"] == "entry_exception"
    assert fc.orders == [] and fc.pos == {}
    assert px.get(s)["halt_entries"] is True
    assert not px.open_pairs(px.load_state(s))


def test_leverage_failure_happens_before_any_order(s):
    class Boom(FakeClient):
        def set_leverage(self, coin, lev):
            raise RuntimeError("leverage rejected")
    fc = Boom()
    out, _, _ = live_entry(s, fc)
    assert fc.orders == [] and fc.pos == {}
    assert not px.open_pairs(px.load_state(s))


def test_pairs_client_parses_ioc_response_and_hashes_cloid():
    class Ex:
        def __init__(self, resp):
            self.resp, self.kw = resp, None

        def order(self, name, is_buy, sz, px_, ot, reduce_only=False, cloid=None):
            self.kw = {"name": name, "is_buy": is_buy, "sz": sz, "px": px_, "ot": ot, "reduce_only": reduce_only, "cloid": cloid}
            return self.resp
    c = object.__new__(px.PairsClient)
    c.ex = Ex({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "0.5", "avgPx": "120.1", "oid": 1}}]}}})
    r = c.ioc("io:SNDK", False, 0.5, 119.0, True, "pairs-SNDK-1-1")
    assert r["filled_sz"] == 0.5 and r["avg_px"] == 120.1 and r["error"] is None
    assert c.ex.kw["ot"] == {"limit": {"tif": "Ioc"}} and c.ex.kw["reduce_only"] is True
    assert c.ex.kw["cloid"].to_raw() == px.cloid_hex("pairs-SNDK-1-1")
    c.ex.resp = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Order could not immediately match"}]}}}
    r = c.ioc("io:SNDK", True, 0.5, 121.0, False, "pairs-SNDK-1-2")
    assert r["filled_sz"] == 0.0 and "immediately" in r["error"]


# ================================================================ 第 1 轮评审修复（每条一个测试）
def ms(dt):
    return int(dt.timestamp() * 1000)


def later(now, seconds):
    return datetime.fromtimestamp(now.timestamp() + seconds, timezone.utc)


def pushes(monkeypatch):
    got = []
    monkeypatch.setattr(px.notify, "push", lambda settings, title, body, priority="default": got.append((title, body)) or True)
    return got


def seed_single_leg(s, fc, now):
    """手工写账本：只有 A 腿成交（模拟两腿之间崩溃）。"""
    led = Ledger(s.data, "pairs")
    pid = "io_SNDK_xyz_SNDK-1-1"
    led.append("open", pid, {"pair_id": pid, "pair_key": f"{A}|{B}", "a": A, "b": B, "sg": 1, "s0": 80.0, "thr": 40.0, "variant": "+5/conv0",
                             "exit_mode": "conv0", "t_ms": ms(now) - 60_000, "bar_t": 1, "notional_target": 100.0,
                             "rich_short": A, "cheap_long": B})
    led.append("order", pid, {"pair_id": pid, "coin": A, "cloid": "pairs-x-1", "cloid_hex": px.cloid_hex("pairs-x-1"), "is_buy": False,
                              "sz": 0.82, "px": 120.5, "reduce_only": False, "purpose": "entry", "pos_before": 0.0})
    led.append("fill", pid, {"pair_id": pid, "coin": A, "cloid": "pairs-x-1", "signed_sz": -0.82, "px": 120.9, "fee_usd": 0.0})
    fc.pos[A] = -0.82
    return pid


# 1. 对账：结果不明的配对绝不自动收尾；限频告警；认领交易所数量；交易所已无仓位 → 了结记亏
def test_reconcile_unknown_pair_never_closed_as_no_position_and_alerts_are_rate_limited(s):
    class Blind(FakeClient):
        down = False

        def position(self, coin):
            if coin == B and self.down:
                raise ConnectionError("down")
            return super().position(coin)

        def ioc(self, coin, *a, **k):
            self.down = self.down or coin == B
            return super().ioc(coin, *a, **k)
    fc = Blind()
    fc.raise_on = {B}
    out, data, now = live_entry(s, fc)
    pid = next(iter(pairs_state(s)))
    for k in (1, 2, 3):
        px.step(s, later(now, 60 * k + 600), client=fc, data=data)       # 都超过 5 分钟：旧逻辑会按 no_position 收尾
    assert pairs_state(s)[pid]["closed"] is None and pairs_state(s)[pid]["unknown"]

    def n_alerts():
        return sum(1 for r in Ledger(s.data, "pairs").all() if r["kind"] == "alert" and "结果不明" in r["title"])
    assert n_alerts() == 1                                                # 1 小时内只告警一次
    px.step(s, later(now, 4400), client=fc, data=data)
    assert n_alerts() == 2
    assert px.get(s)["halt_entries"] is True


def test_reconcile_exchange_flat_for_n_minutes_settles_books_loss_alerts_once(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    pid = next(iter(pairs_state(s)))
    fc.pos = {}                                   # 被强平 / 手动平掉了
    fc.mids[A] *= 1.004                           # 价差 80 → 约 120 bps，还没到反向止损的 2 倍线
    px.step(s, later(now, 60), client=fc, data=data)
    assert pairs_state(s)[pid]["closed"] is None  # 还没到 N 分钟：先观察
    px.step(s, later(now, 60 * 7), client=fc, data=data)
    p = pairs_state(s)[pid]
    assert p["closed"]["reason"] == "no_position" and p["closed"]["pnl_usd"] < 0
    n_settle = lambda: sum(1 for r in Ledger(s.data, "pairs").all() if r["kind"] == "alert" and "已了结" in r["title"])   # noqa: E731
    assert n_settle() == 2                        # 两条腿各一次
    px.step(s, later(now, 60 * 9), client=fc, data=data)
    assert n_settle() == 2
    assert len(fc.orders) == 2                    # 交易所没有仓位，除了开仓那两张什么都没发


# 2. 单腿检测（含两腿之间崩溃）
def test_single_leg_pair_is_unwound_immediately(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    pid = seed_single_leg(s, fc, now)
    out = px.step(s, now, client=fc, data=data)
    assert out["exits"][0]["reason"] == "imbalance" and out["exits"][0]["closed"]
    assert abs(fc.pos[A]) < 1e-9 and fc.orders[0]["reduce_only"] and fc.orders[0]["is_buy"]
    assert pairs_state(s)[pid]["closed"] is not None


# 3. 客户端 dex = 发现的 ∪ 账本里已有仓位；先管仓位再抓数据；抓数据失败仍然平仓
def test_client_dexs_from_open_pairs_and_exits_managed_when_data_fails(s, monkeypatch):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    seen = []
    monkeypatch.setattr(px, "_make_client", lambda settings, dexs: seen.append(sorted(dexs)) or fc)

    def boom(*a, **k):
        raise RuntimeError("data down")
    monkeypatch.setattr(px, "load_data_limited", boom)
    out = px.step(s, later(now, 25 * 3600), data=None)
    assert seen and seen[0] == ["io", "xyz"]
    assert out["exits"] and out["exits"][0]["reason"] == "max_hold" and "数据不可用" in out["error"]
    assert abs(fc.pos[A]) < 1e-9 and abs(fc.pos[B]) < 1e-9


# 4. 估值：盘口太宽不用中间价；退回最近成交价（stale）；有 mark 就用 mark
def test_wide_book_mid_ignored_falls_back_to_last_fill_then_uses_mark(s, monkeypatch):
    got = pushes(monkeypatch)
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.wide = {A}
    fc.mids[A] *= 1.2                              # 要是用了这个中间价，空腿亏 20% → 触发止损
    out = px.step(s, later(now, 60), client=fc, data=data)
    assert out["exits"] == []
    assert any("最近成交价" in t for t, _ in got)  # stale 告警
    fc.marks[A] = fc.mids[A]                       # 交易所 mark 可用 → 用 mark，真实亏损显现
    out = px.step(s, later(now, 120), client=fc, data=data)
    assert out["exits"] and out["exits"][0]["reason"] == "per_pair_loss"


# 5. 杠杆
def test_leverage_error_response_aborts_entry(s):
    fc = FakeClient()
    fc.lev_resp = {"status": "err", "response": "nope"}
    out, _, _ = live_entry(s, fc)
    assert fc.orders == [] and out["entries"] == [] and not px.open_pairs(px.load_state(s))


def test_entry_refused_when_exchange_already_has_position_on_coin(s):
    fc = FakeClient()
    fc.pos = {B: 3.0}                              # 手动仓位
    out, _, _ = live_entry(s, fc)
    assert fc.orders == [] and fc.lev_calls == [] and out["entries"] == []


def test_one_live_pair_per_coin_even_if_exchange_shows_flat(s):
    fc = FakeClient()
    cfg_set(s, max_concurrent=2, max_total_gross_usd=400.0, cooldown_min=0)
    out, data, now = live_entry(s, fc)
    assert len(out["entries"]) == 1
    fc.pos[A] = fc.pos[B] = 0.0                    # 交易所上看起来是 0，账本里该币仍被占用
    n = len(fc.orders)
    px.step(s, later(now, 400), client=fc, data=data)
    assert [o for o in fc.orders[n:] if not o["reduce_only"]] == []


# 6. pair_id 不撞
def test_pair_id_contains_pair_key_and_counter(s):
    cfg_set(s, allow=[], max_concurrent=2, max_total_gross_usd=400.0)
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data(allow_second=True)
    px.step(s, now, client=fc, data=data)
    ids = sorted(pairs_state(s))
    assert len(ids) == 2 and len(set(ids)) == 2
    assert any(i.startswith("io_NBIS_xyz_NBIS-") for i in ids) and any(i.startswith("io_SNDK_xyz_SNDK-") for i in ids)
    assert sorted(i.rsplit("-", 1)[1] for i in ids) == ["1", "2"]


# 7. 异常路径的成交价
def test_exception_path_uses_actual_fill_px_when_queryable(s):
    fc = FakeClient()
    fc.raise_filled = {B}
    fc.fill_for_cloid = lambda coin, h, t: (abs(fc.pos.get(coin, 0.0)), 120.77) if coin == B else None
    out, _, _ = live_entry(s, fc)
    assert out["entries"][0]["balanced"] is True
    p = next(iter(pairs_state(s).values()))
    assert p["last_px"][B] == 120.77 and not p["unknown"]
    assert px.get(s)["halt_entries"] is False


# 8. off / shadow + 有未平仓位：照常管理；切模式推送告警
def test_off_mode_with_open_pairs_still_manages_exits_and_put_alerts(s, monkeypatch):
    got = pushes(monkeypatch)
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    px.put(s, mode="off")
    assert any("还有 1 对没平" in t for t, _ in got)
    n = len(fc.orders)
    out = px.step(s, later(now, 25 * 3600), client=fc, data=data)
    assert out["exits"] and out["exits"][0]["reason"] == "max_hold" and out["entries"] == []
    assert abs(fc.pos[A]) < 1e-9 and abs(fc.pos[B]) < 1e-9
    assert all(o["reduce_only"] for o in fc.orders[n:])


def test_scheduler_keeps_job_while_pairs_open_even_if_mode_off(s):
    from desk import scheduler
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    px.put(s, mode="off")
    t = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    assert [k for k, _ in scheduler.due_jobs(t, s, set()) if k.startswith("pairs:")]


# 9. 止损 / 超时出场后锁定
def test_stop_out_writes_24h_lock(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.mids[A] *= 1.06
    px.step(s, later(now, 60), client=fc, data=data)
    lk = px.load_state(s)["locks"][f"{A}|{B}"]
    assert lk["reason"] == "per_pair_loss" and lk["until_ms"] - lk["t_ms"] == 24 * 3600_000


def test_lock_blocks_reentry_until_spread_below_threshold(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    key = f"{A}|{B}"
    Ledger(s.data, "pairs").append("lock", key, {"pair_key": key, "reason": "max_hold", "t_ms": ms(now), "until_ms": ms(now) + 10 * 3600_000})
    px.step(s, now, client=fc, data=data)
    assert fc.orders == []                          # 尖峰信号还在，但被锁挡住
    data3, now3 = make_data(tail=(0.0,))            # 价差回到门槛以内 → 解除
    px.step(s, now3, client=fc, data=data3)
    assert key not in px.load_state(s)["locks"]
    assert fc.orders == []


# 10. flock / 抓数据超时
def test_step_is_serialized_by_flock(s):
    import fcntl
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    with open(Path(s.data) / "state" / "pairs_step.lock", "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        out = px.step(s, now, client=fc, data=data)
    assert out.get("locked") is True and fc.orders == []
    assert px.step(s, now, client=fc, data=data)["entries"]       # 锁放开后正常


def test_data_timeout_does_not_block_exits(s, monkeypatch):
    import time as _t
    px._FUT.clear()
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    monkeypatch.setattr(px, "load_data", lambda *a, **k: _t.sleep(1.0))
    cfg_set(s, data_timeout_s=0.1, data_timeout_cold_s=0.1)
    t0 = _t.time()
    out = px.step(s, later(now, 25 * 3600), client=fc, data=None)
    assert _t.time() - t0 < 0.8 and "数据不可用" in out["error"]
    assert out["exits"] and out["exits"][0]["reason"] == "max_hold"
    _t.sleep(1.0)
    px._FUT.clear()


# 11. 资金费计入盈亏和止损
def test_funding_paid_counts_toward_pair_loss(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.funding = [{"coin": A, "usdc": -6.0, "szi": fc.pos[A], "time": ms(now) + 1000}]
    out = px.step(s, later(now, 60), client=fc, data=data)
    assert out["exits"] and out["exits"][0]["reason"] == "per_pair_loss"
    assert next(iter(pairs_state(s).values()))["closed"]["pnl_usd"] < -4.0


def test_funding_of_manual_position_is_not_charged_to_us(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    big = fc.pos[A] * 100                             # 交易所持仓是我们的 100 倍（几乎全是手动仓位）
    fc.funding = [{"coin": A, "usdc": -60.0, "szi": big, "time": ms(now) + 1000}]
    out = px.step(s, later(now, 60), client=fc, data=data)
    assert out["exits"] == []                         # 我们那一份只分摊到约 −0.6，远小于止损
    assert next(iter(pairs_state(s).values()))["closed"] is None


# 12. 杂项
def test_balance_failure_skips_entry(s):
    fc = FakeClient()
    fc.balance_raises = True
    out, _, _ = live_entry(s, fc)
    assert fc.orders == [] and out["entries"] == []


def test_min_notional_checked_after_flooring_with_margin(s):
    cfg_set(s, max_leg_notional_usd=10.2)             # 取整到 0.08 张 × 120 = 9.6 < 10.5
    fc = FakeClient()
    out, _, _ = live_entry(s, fc)
    assert fc.orders == [] and out["entries"] == []


def test_halt_realerts_daily_and_clear_halt_cli(s, monkeypatch):
    got = pushes(monkeypatch)
    px.put(s, halt_entries=True, halt_alert_ts=0, halt_reason="test")
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    px.step(s, now, client=fc, data=data)
    assert sum("停开新仓" in t for t, _ in got) == 1
    px.step(s, later(now, 60), client=fc, data=data)
    assert sum("停开新仓" in t for t, _ in got) == 1
    px.step(s, later(now, 25 * 3600), client=fc, data=data)
    assert sum("停开新仓" in t for t, _ in got) == 2
    assert fc.orders == []
    assert px.main(["--clear-halt"]) == 0
    assert px.get(s)["halt_entries"] is False


# ================================================================ 第 2 轮评审修复
# 1. live 模式当日亏损止损无条件在开仓之前
def test_daily_stop_runs_in_live_mode_even_with_no_open_pairs(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    led = Ledger(s.data, "pairs")
    led.append("open", "old-1", {"pair_id": "old-1", "pair_key": f"{A}|{B}", "a": A, "b": B, "sg": 1, "s0": 80.0, "thr": 40.0, "variant": "+5/conv0",
                                 "exit_mode": "conv0", "t_ms": ms(now) - 3600_000, "bar_t": 1, "notional_target": 100.0, "rich_short": A, "cheap_long": B})
    led.append("closed", "old-1", {"pair_id": "old-1", "reason": "per_pair_loss", "pnl_usd": -9.0, "t_ms": ms(now) - 1000})
    out = px.step(s, now, client=fc, data=data)
    assert out.get("daily_stop") is True and px.get(s)["mode"] == "off"
    assert fc.orders == [] and out["entries"] == []


# 2. 异常路径：有界重查
def test_send_exception_repolls_bounded_and_picks_up_late_fill(s, _fast):
    fc = FakeClient()
    fc.raise_filled = {B}
    calls = []

    def late(coin, h, t):
        calls.append(1)
        return (abs(fc.pos.get(coin, 0.0)), 120.5) if len(calls) >= 2 and coin == B else None
    fc.fill_for_cloid = late
    # 持仓变了会提前跳出重查；这里让持仓读数保持不变来逼它重查：把 position 对 B 固定为 0
    real_pos = fc.position
    fc.position = lambda coin: 0.0 if (coin == B and len(calls) < 2) else real_pos(coin)
    out, _, _ = live_entry(s, fc)
    assert len(calls) == 2 and _fast == [2.0]                          # 第二次重查拿到真实成交，中间等了 2s
    p = next(iter(pairs_state(s).values()))
    assert p["last_px"][B] == 120.5 and not p["unknown"] and px.get(s)["halt_entries"] is False


def test_send_exception_nothing_found_after_bounded_window_is_unknown(s, _fast):
    fc = FakeClient()
    fc.raise_on = {B}
    out, _, _ = live_entry(s, fc)
    assert _fast == [2.0, 2.0]                                          # 3 次尝试 = 2 次等待，有界
    assert out["entries"][0]["error"] == "unknown_order_state" and px.get(s)["halt_entries"] is True


def test_reconcile_alerts_and_halts_on_exposure_beyond_ledger_after_recent_order(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.pos[B] += 7.0                                                     # 账本之外多出来的仓位
    px.step(s, later(now, 60), client=fc, data=data)
    assert px.get(s)["halt_entries"] is True
    assert any(r["kind"] == "alert" and "超出账本" in r["title"] for r in Ledger(s.data, "pairs").all())
    px.step(s, later(now, 120), client=fc, data=data)
    assert sum(1 for r in Ledger(s.data, "pairs").all() if r["kind"] == "alert" and "超出账本" in r["title"]) == 1    # 限频


# 3. 超时后才完成的抓数据结果会被复用
def test_late_finished_load_is_reused_instead_of_erroring(s, monkeypatch):
    import threading
    data, now = make_data()
    gate = threading.Event()

    def slow(*a, **k):
        gate.wait(2.0)
        return data
    monkeypatch.setattr(px, "load_data", slow)
    cfg_set(s, data_timeout_s=0.05, data_timeout_cold_s=0.05)
    cf = px.cfg(s)
    with pytest.raises(TimeoutError):
        px.load_data_limited(s, ms(now), cf)
    with pytest.raises(TimeoutError):                                    # 还没完成：不重复提交
        px.load_data_limited(s, ms(now), cf)
    gate.set()
    px._FUT["f"].result(timeout=2.0)
    assert px.load_data_limited(s, ms(now), cf) is data                 # 完成了：直接用，不再报错 / 重提交


# 4. 客户端缓存：一次建好，shadow 同一个 dex 集合
def test_pairs_client_built_once_for_same_dexs_and_shadow_uses_discovered(s, monkeypatch):
    built = []

    class FakePC(FakeClient):
        def __init__(self, a, k, dexs):
            super().__init__()
            built.append(sorted(dexs))
    monkeypatch.setattr(autotrade, "_client_factory", None)
    monkeypatch.setattr(autotrade, "creds", lambda: ("0x" + "1" * 40, "2" * 64))
    monkeypatch.setattr(px, "PairsClient", FakePC)
    set_mode(s, "shadow")
    data, now = make_data()
    for k in range(3):
        px.step(s, later(now, 60 * k), data=data)
    assert built == [["io", "xyz"]]


# 5. 一对出错不影响其他对和当日止损
def test_one_pair_error_does_not_skip_others_or_daily_stop(s, monkeypatch):
    cfg_set(s, allow=[], max_concurrent=2, max_total_gross_usd=400.0)
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data(allow_second=True)
    px.step(s, now, client=fc, data=data)
    assert len(pairs_state(s)) == 2
    real = px._reconcile

    def flaky(ctx, pid, out):
        if "SNDK" in pid:
            raise RuntimeError("boom")
        return real(ctx, pid, out)
    monkeypatch.setattr(px, "_reconcile", flaky)
    out = px.step(s, later(now, 25 * 3600), client=fc, data=data)
    assert [e["pair_id"] for e in out["exits"]] and all("NBIS" in e["pair_id"] for e in out["exits"])
    assert "day_pnl_usd" in out                                          # _daily_stop 仍然执行了
    assert any(r["kind"] == "alert" and "管理出错" in r["title"] for r in Ledger(s.data, "pairs").all())


# 6. 当日止损告警限频
def test_daily_stop_alert_rate_limited_while_unresolved(s, monkeypatch):
    got = pushes(monkeypatch)
    cfg_set(s, daily_loss_stop_usd=3.0, max_pair_loss_usd=100.0)
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.mids[A] *= 1.06
    fc.reject = {A, B}                                                   # 平不掉
    for k in (1, 2, 3):
        px.step(s, later(now, 60 * k), client=fc, data=data)
    n = lambda: sum("当日亏损止损触发" in t for t, _ in got)   # noqa: E731
    assert n() == 1 and px.get(s)["mode"] == "live"
    px.step(s, later(now, 3700), client=fc, data=data)
    assert n() == 2


def test_cli_mode_switch(tmp_path, monkeypatch):
    from desk import pairs_exec as pe, config
    s = config.load()
    monkeypatch.setattr(s, "data", str(tmp_path), raising=False) if hasattr(s, "data") else None
    monkeypatch.setattr(config, "load", lambda: s)
    monkeypatch.setattr(pe.notify, "push", lambda *a, **k: True)
    assert pe.main(["--mode", "shadow"]) == 0
    assert pe.get(s)["mode"] == "shadow"
    assert pe.main(["--mode", "off"]) == 0
    assert pe.get(s)["mode"] == "off"


# ================================================================ 开仓守卫 / 反向止损 / 决策透明度
NY = ZoneInfo("America/New_York")


def ny_ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=NY).timestamp() * 1000)


def utc_ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


def decisions(s):
    return json.loads((Path(s.data) / "state" / "pairs_last_decision.json").read_text())


def row(s, key=f"{A}|{B}"):
    return next(r for r in decisions(s)["pairs"] if r["key"] == key)


def cut(data, coin, n, skip_last=0, **upd):
    """复制一份 data，把 coin 倒数第 skip_last+1 起的 n 根 K 线字段改掉，例如 v=0 或 c=120.0。
    零成交的 K 线会被 pairs_lab.align 丢掉，所以零成交测试要留住最后一根，免得先被"数据断档"拦下。"""
    cs = {c: [dict(x) for x in v] for c, v in data["candles"].items()}
    for x in (cs[coin][-n - skip_last:len(cs[coin]) - skip_last]):
        x.update({k: str(v) for k, v in upd.items()})
    return {**data, "candles": cs}


# ---- 1. 休市禁开
def test_blackout_window_boundaries_and_dst(s):
    cf = px.cfg(s)
    assert cf["blackout_start"] == "FRI 19:30" and cf["blackout_end"] == "SUN 20:35" and cf["blackout_tz"] == "America/New_York"
    for args, want in [((2026, 10, 30, 19, 29), False), ((2026, 10, 30, 19, 30), True), ((2026, 10, 31, 12, 0), True),
                       ((2026, 11, 1, 20, 34), True), ((2026, 11, 1, 20, 35), False), ((2026, 11, 2, 9, 0), False),
                       ((2026, 10, 28, 12, 0), False), ((2026, 10, 25, 20, 35), False), ((2026, 10, 25, 20, 34), True)]:
        assert px.in_blackout(cf, ny_ms(*args)) is want, args
    # 夏令时 2026-11-01 回拨：周日 20:35 EST = 周一 01:35Z（若误用固定 UTC-4 会差一小时）
    assert px.in_blackout(cf, utc_ms(2026, 11, 2, 1, 34)) is True and px.in_blackout(cf, utc_ms(2026, 11, 2, 1, 35)) is False
    # 夏令时期间的周五 19:30 EDT = 23:30Z
    assert px.in_blackout(cf, utc_ms(2026, 10, 30, 23, 29)) is False and px.in_blackout(cf, utc_ms(2026, 10, 30, 23, 30)) is True
    assert px.in_blackout({**cf, "blackout_start": ""}, ny_ms(2026, 10, 31, 12, 0)) is False
    assert px.in_blackout({**cf, "blackout_tz": "Nowhere/Land"}, ny_ms(2026, 10, 28, 12, 0)) is True      # 时区读不到 → 保守禁开


def test_blackout_blocks_new_entry_but_weekday_enters(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data(day_shift=4)             # 纽约周五晚 20:05 → 禁开
    assert px.in_blackout(px.cfg(s), ms(now))
    out = px.step(s, now, client=fc, data=data)
    assert out["entries"] == [] and fc.orders == [] and fc.leverage == {}
    assert row(s)["reason"] == "休市禁开" and row(s)["entered"] is False
    data2, now2 = make_data(day_shift=0)           # 纽约周二晚上 → 正常开
    assert px.step(s, now2, client=fc, data=data2)["entries"][0]["opened"] is True


def test_blackout_does_not_block_exits(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    n = len(fc.orders)
    cfg_set(s, blackout_start="MON 00:00", blackout_end="SUN 23:59")       # 整周禁开
    assert px.in_blackout(px.cfg(s), ms(later(now, 25 * 3600)))
    out = px.step(s, later(now, 25 * 3600), client=fc, data=data)
    assert out["exits"] and out["exits"][0]["reason"] == "max_hold" and out["entries"] == []
    assert abs(fc.pos[A]) < 1e-9 and abs(fc.pos[B]) < 1e-9 and all(o["reduce_only"] for o in fc.orders[n:])


def test_blackout_applies_to_shadow(s):
    fc = FakeClient()
    set_mode(s, "shadow")
    data, now = make_data(day_shift=4)
    out = px.step(s, now, client=fc, data=data)
    assert out["plans"] == [] and row(s)["reason"] == "休市禁开" and decisions(s)["mode"] == "shadow"


# ---- 2. 反向止损
def test_entry_spread_recorded_with_sign(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    o = next(iter(pairs_state(s).values()))["open"]
    assert o["s_entry"] == pytest.approx(80.0, abs=1.0) and o["s_entry"] > 0 and o["rich_short"] == A


def test_wrong_way_unit_sign_and_caps(s):
    cf = px.cfg(s)
    assert cf["wrong_way_mult"] == 2.0 and cf["max_abs_spread_bps"] == 300.0
    o = {"s_entry": -80.0, "s0": -80.0}
    assert px.wrong_way(o, -160.0, cf) and px.wrong_way(o, -200.0, cf)
    assert not px.wrong_way(o, -159.0, cf)
    assert not px.wrong_way(o, +200.0, cf)                    # 反号且 < 300：这是在收敛，不算反向
    assert px.wrong_way(o, +300.0, cf) and px.wrong_way(o, -300.0, cf)       # 绝对价差上限不分方向
    assert not px.wrong_way(o, None, cf)


def test_wrong_way_exit_reduce_only_and_lock(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    n = len(fc.orders)
    fc.mids[A] = 120.0 * 1.017                             # 价差 80 → 170 bps ≥ 2×80
    out = px.step(s, later(now, 60), client=fc, data=data)
    assert out["exits"] and out["exits"][0]["reason"] == "wrong_way" and out["exits"][0]["closed"]
    assert abs(fc.pos[A]) < 1e-9 and abs(fc.pos[B]) < 1e-9
    assert fc.orders[n:] and all(o["reduce_only"] for o in fc.orders[n:])
    p = next(iter(pairs_state(s).values()))
    assert p["closed"]["reason"] == "wrong_way"
    lk = px.load_state(s)["locks"][f"{A}|{B}"]
    assert lk["reason"] == "wrong_way" and lk["until_ms"] - lk["t_ms"] == 24 * 3600_000


def test_wrong_way_not_triggered_by_smaller_or_opposite_move(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.mids[A] = 120.0 * 1.012                             # 120 bps < 160
    assert px.step(s, later(now, 60), client=fc, data=data)["exits"] == []
    fc.mids[A] = 120.0 * 0.99                              # 反向（价差 −100）：不是 wrong_way
    out = px.step(s, later(now, 120), client=fc, data=data)
    assert not [e for e in out["exits"] if e["reason"] == "wrong_way"]


def test_max_abs_spread_closes_even_when_ratio_not_hit(s):
    cfg_set(s, wrong_way_mult=10.0, max_abs_spread_bps=150.0)
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.mids[A] = 120.0 * 1.016                             # 160 bps ≥ 150，但 < 10×80
    out = px.step(s, later(now, 60), client=fc, data=data)
    assert out["exits"][0]["reason"] == "wrong_way" and abs(fc.pos[A]) < 1e-9


def test_wrong_way_ignores_untrustworthy_wide_book(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    fc.wide = {A}
    fc.mids[A] *= 1.2                                      # 盘口太宽的中间价不可信，不能据此止损
    out = px.step(s, later(now, 60), client=fc, data=data)
    assert not [e for e in out["exits"] if e["reason"] == "wrong_way"]


# ---- 3. 腿不活跃
def test_stale_leg_zero_volume_blocks_entry(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    out = px.step(s, now, client=fc, data=cut(data, B, 3, skip_last=1, v=0))
    assert out["entries"] == [] and fc.orders == []
    assert "腿不活跃" in row(s)["reason"] and B in row(s)["reason"]


def test_stale_leg_two_zero_bars_still_enters(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    out = px.step(s, now, client=fc, data=cut(data, B, 2, skip_last=1, v=0))
    assert out["entries"] and out["entries"][0]["opened"]


def test_stale_leg_missing_bars_count_as_zero(s):
    data, now = make_data()
    cs = {c: list(v) for c, v in data["candles"].items()}
    cs[B] = cs[B][:-3] + cs[B][-1:]                        # 缺 2 根（API 不返回无成交的 K 线）：2 < 3
    assert px.stale_leg(data["pairs"][0], {**data, "candles": cs}, px.cfg(s), ms(now)) is None
    cs[B] = cs[B][:-3] + cs[B][-1:]                        # 再缺 1 根 → 3
    assert "零成交" in px.stale_leg(data["pairs"][0], {**data, "candles": cs}, px.cfg(s), ms(now))


def test_stale_leg_flat_close_while_other_moves(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    flat = cut(data, B, 6, c=120.0)                        # B 六根收盘一动不动，A 最后一根跳了 80 bps
    assert "没动" in px.stale_leg(flat["pairs"][0], flat, px.cfg(s), ms(now))
    out = px.step(s, now, client=fc, data=flat)
    assert out["entries"] == [] and fc.orders == [] and "腿不活跃" in row(s)["reason"]
    both = cut(flat, A, 6, c=120.0)                        # 另一条腿也没动（< 10 bps）→ 不算
    assert px.stale_leg(both["pairs"][0], both, px.cfg(s), ms(now)) is None


# ---- 4. 下架 / 暂停
def test_halted_coins_from_raw_meta():
    raw = {"dexs": {"xyz": [{"universe": [{"name": "xyz:A", "isDelisted": True}, {"name": "xyz:B", "halted": True},
                                         {"name": "xyz:C"}, {"name": "xyz:D", "isHalted": False}]}, []]},
           "main": [{"universe": [{"name": "BTC"}, {"name": "XRP", "isDelisted": True}]}, []]}
    assert px.halted_coins(raw) == {"xyz:A", "xyz:B", "XRP"}
    assert px.halted_coins(pl.fixture_raw()) == set()      # 字段缺失 → 空集，检查自动跳过
    assert px.halted_coins(None) == set() and px.halted_coins({"dexs": {"x": None}, "main": None}) == set()


def test_halted_coin_blocks_entry(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    out = px.step(s, now, client=fc, data={**data, "halted": {B}})
    assert out["entries"] == [] and fc.orders == []
    assert B in row(s)["reason"] and "下架" in row(s)["reason"]


def test_halted_coin_closes_open_pair_reduce_only(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    n = len(fc.orders)
    out = px.step(s, later(now, 60), client=fc, data={**data, "halted": {A}})
    assert out["exits"] and out["exits"][0]["reason"] == "halted" and out["exits"][0]["closed"]
    assert abs(fc.pos[A]) < 1e-9 and abs(fc.pos[B]) < 1e-9 and all(o["reduce_only"] for o in fc.orders[n:])
    assert next(iter(pairs_state(s).values()))["closed"]["reason"] == "halted"


def test_halted_close_works_even_if_pair_dropped_from_discovery(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    out = px.step(s, later(now, 60), client=fc, data={**data, "pairs": [], "halted": {B}})      # 下架币被 discover_pairs 剔除
    assert out["exits"] and out["exits"][0]["reason"] == "halted"
    assert abs(fc.pos[A]) < 1e-9 and abs(fc.pos[B]) < 1e-9


# ---- 5. 决策透明度
def test_decision_file_entered_then_open(s, caplog):
    fc = FakeClient()
    caplog.set_level(logging.INFO, logger="pairs_exec")
    out, data, now = live_entry(s, fc)
    d = decisions(s)
    assert d["mode"] == "live" and d["ts"] and len(d["pairs"]) == 1
    r = d["pairs"][0]
    assert r["key"] == f"{A}|{B}" and r["entered"] is True and r["reason"] == "已开仓"
    assert r["spread_bps"] == pytest.approx(80.0, abs=0.2) and r["thr_bps"] > 0 and r["variant"]
    msgs = [m.getMessage() for m in caplog.records if m.name == "pairs_exec"]
    assert any("检查 1 对" in m and "开仓 1 笔" in m and "/" in m for m in msgs)
    px.step(s, later(now, 60), client=fc, data=data)
    assert row(s)["reason"] == "已开仓" and row(s)["entered"] is False          # 已有仓位


def test_decision_file_below_threshold_lock_and_no_variant(s, monkeypatch):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data(tail=(10.0,))                    # 价差 10 bps，远低于门槛
    px.step(s, now, client=fc, data=data)
    r = row(s)
    assert r["spread_bps"] == pytest.approx(10.0, abs=0.2) and r["reason"].startswith("价差 10 < 门槛 ") and not r["entered"]
    data2, now2 = make_data()                              # 锁定中
    key = f"{A}|{B}"
    Ledger(s.data, "pairs").append("lock", key, {"pair_key": key, "reason": "wrong_way", "t_ms": ms(now2), "until_ms": ms(now2) + 3600_000})
    px.step(s, now2, client=fc, data=data2)
    assert row(s)["reason"] == "止损锁定中"
    real = px.signal                                       # 走前判断今天不做
    monkeypatch.setattr(px, "signal", lambda *a, **k: {**real(*a, **k), "ok": False, "no_variant": True, "why": "x", "thr": None, "variant": None})
    px.step(s, later(now2, 30), client=fc, data=data2)
    r = row(s)
    assert r["reason"] == "走前判断今天不做" and r["variant"] is None and r["thr_bps"] is None
    assert fc.orders == []


def test_status_prints_plain_chinese_decision_table(s, monkeypatch, capsys):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data(day_shift=4)
    px.step(s, now, client=fc, data=data)
    monkeypatch.setattr(config, "load", lambda *a, **k: s)
    assert px.main(["--status"]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if f"{A}|{B}" in ln and "门槛" in ln)
    assert "价差" in line and "休市禁开" in line and "没开" in line
    assert "最近一次决策" in out


def test_status_without_decision_file(s, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda *a, **k: s)
    px.main(["--status"])
    assert "还没有决策记录" in capsys.readouterr().out


# ---- 评审第 1 轮
def test_wrong_way_ignores_stale_signal_bar_right_after_entry(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    o = next(iter(pairs_state(s).values()))["open"]
    # 入场用的是实时价差更大的那个（同号）；信号 K 线收盘早于开仓 → 不参与反向止损
    assert abs(o["s_entry"]) >= abs(o["s0"]) - 1e-9 and o["s_entry"] * o["s0"] > 0
    cfg_set(s, wrong_way_mult=1.0)                     # 若旧的 K 线价差 80 参与比较（80 ≥ 1×80），会立刻误触发
    fc.mids[A] = 120.0 * 1.007                         # 实时价差降到 70：没有反向
    out = px.step(s, later(now, 5), client=fc, data=data)
    assert [e for e in out["exits"] if e["reason"] == "wrong_way"] == []


def test_wrong_way_uses_bar_closed_after_entry(s):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    data2, now2 = make_data(tail=(300.0,))             # 开仓之后又收了一根 300 bps 的 K 线
    px.step(s, now2, client=fc, data=data2)
    assert next(iter(pairs_state(s).values()))["closed"]["reason"] == "wrong_way"


def test_entry_blocked_when_live_spread_absurd(s):
    fc = FakeClient()
    fc.mids[A] = 120.0 * 1.031                         # 实时价差约 310 bps ≥ 300，K 线信号仍是 80
    out, data, now = live_entry(s, fc)
    assert out["entries"] == [] and fc.orders == [] and fc.leverage == {}
    assert row(s)["reason"] == "价差异常大，像是报价坏了"


def test_halt_branch_rows_do_not_call_signal(s, monkeypatch):
    fc = FakeClient()
    set_mode(s, "live")
    px.put(s, halt_entries=True, halt_reason="t")
    data, now = make_data()
    monkeypatch.setattr(px, "signal", lambda *a, **k: (_ for _ in ()).throw(AssertionError("signal called")))
    px.step(s, now, client=fc, data=data)
    r = row(s)
    assert "停开新仓" in r["reason"] and r["spread_bps"] is None


def test_decision_write_failure_never_aborts_step(s, monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(px, "decision_path", lambda st: (_ for _ in ()).throw(RuntimeError("boom")))
    out, data, now = live_entry(s, fc)
    assert out["entries"] and out["entries"][0]["opened"]


def test_decision_lines_tz_fallback(s, monkeypatch):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    px.step(s, now, client=fc, data=data)
    monkeypatch.setattr(px, "ZoneInfo", lambda n: (_ for _ in ()).throw(KeyError(n)))
    assert "UTC" in px.decision_lines(s)[0]


# ---------------------------------------------------------------- 配对专用门槛 + 同时持有上限
def test_pairs_gate_overrides_universe_and_respects_floor(s):
    g = px.pairs_gate(s, px.cfg(s))
    assert g["min_day_ntl_vlm_usd"] == 20_000.0 and g["min_oi_usd"] == 20_000.0 and g["max_spread_bps"] == 60.0
    cfg_set(s, gate_min_day_ntl_vlm_usd=1.0, gate_min_oi_usd=0.0, gate_max_spread_bps=500.0)
    g = px.pairs_gate(s, px.cfg(s))
    assert g == {**g, **px.GATE_FLOOR}                      # 放得再松也停在硬底线
    s.raw["pairs"] = {k: v for k, v in s.raw["pairs"].items() if not k.startswith("gate_")}
    g = px.pairs_gate(s, px.cfg(s))
    from desk import universe as uv
    assert g["min_day_ntl_vlm_usd"] == uv.gate_cfg(s)["min_day_ntl_vlm_usd"]   # 没配就和周末线一样


def test_pairs_gate_does_not_touch_weekend_universe(s):
    from desk import universe as uv
    assert uv.gate_cfg(s)["min_day_ntl_vlm_usd"] == 200_000.0 and uv.gate_cfg(s)["max_spread_bps"] == 20.0


def test_looser_gate_admits_thin_pair():
    raw = pl.fixture_raw()
    strict = {"min_day_ntl_vlm_usd": 2e5, "min_oi_usd": 1e5, "max_spread_bps": 20.0}
    loose = {**strict, **px.GATE_FLOOR}
    n_strict = sum(p["kind"] == "same" and p["gate_ok"] for p in pl.discover_pairs(raw, strict))
    n_loose = sum(p["kind"] == "same" and p["gate_ok"] for p in pl.discover_pairs(raw, loose))
    assert n_loose >= n_strict


def test_max_concurrent_hard_cap(s):
    cfg_set(s, max_concurrent=50)
    assert px.cfg(s)["max_concurrent"] == px.CONCURRENT_HARD_CAP == 3


# ---------------------------------------------------------------- 卡 E：扩容后的 6 条修复
def _raw_ctx(s, monkeypatch, strict_keys=None):
    """load_data 用的假环境：fixture_raw、假 K 线抓取（记录每次请求）。返回 (calls, now_ms)。"""
    raw = pl.fixture_raw()
    monkeypatch.setattr(px.uv, "discover", lambda *a, **k: raw)
    monkeypatch.setattr(bl, "get_funding", lambda settings, coin, days: ([], None))
    px._DATA.clear()
    calls = []
    now_ms = T0 + 20 * pl.DAY_MS

    def fake(coin, interval, days, post=None, pacer=None, start_ms=None):
        calls.append((coin, start_ms))
        lo = start_ms if start_ms is not None else now_ms - days * pl.DAY_MS
        return [{"t": t, "c": "100", "v": "1"} for t in range(lo // IV * IV, now_ms, IV)]
    monkeypatch.setattr(bl, "fetch_candles_paged", fake)
    return calls, now_ms


def test_E1_open_pair_legs_kept_in_data_even_if_gate_drops_it(s, monkeypatch):
    fc = FakeClient()
    out, data, now = live_entry(s, fc)
    calls, now_ms = _raw_ctx(s, monkeypatch)
    cfg_set(s, gate_min_day_ntl_vlm_usd=1e12, gate_min_oi_usd=1e12)      # 门槛高到没有配对能过（硬底线只管下限）
    d = px.load_data(s, now_ms, px.cfg(s))
    held = [p for p in d["pairs"] if px._pair_key(p) == f"{A}|{B}"]
    assert held and held[0]["held_only"] and A in d["candles"] and B in d["candles"]
    assert px._candidates(d) == []                                       # 但不再当开仓候选
    # 且 _spread_exits 能用到它的 K 线价差（pk 里有这一对）
    assert f"{A}|{B}" in {px._pair_key(p) for p in d["pairs"]}


def test_E2_incremental_candle_fetch_and_cold_start_timeout(s, monkeypatch):
    calls, now_ms = _raw_ctx(s, monkeypatch)
    cf = px.cfg(s)
    d1 = px.load_data(s, now_ms, cf)
    n_full = len(calls)
    assert n_full == 2 and all(st is None for _, st in calls)            # 冷启动：全量
    d2 = px.load_data(s, now_ms + IV, cf)                                # 下一根 K 线：只增量
    inc = calls[n_full:]
    assert len(inc) == 2 and all(st is not None and st >= now_ms - 5 * IV for _, st in inc)
    assert abs(len(d2["candles"][A]) - len(d1["candles"][A])) <= 1
    assert all(int(c["t"]) >= now_ms + IV - cf["lookback_days"] * pl.DAY_MS for c in d2["candles"][A])   # 裁到回看窗口
    ts = [int(c["t"]) for c in d2["candles"][A]]
    assert ts == sorted(set(ts))
    # 超过 6 小时 → 全量重抓
    for c in d2["candles_full_t"]:
        d2["candles_full_t"][c] -= 7 * 3600
    px.load_data(s, now_ms + 2 * IV, cf)
    assert sum(st is None for _, st in calls) == 4
    assert cf["data_timeout_cold_s"] >= 300 > cf["data_timeout_s"]


def test_E3_loose_pair_never_orders_live_but_shadows_to_separate_ledger(s):
    fc = FakeClient()
    set_mode(s, "live")
    data, now = make_data()
    data["pairs"] = [{**p, "strict": False} for p in data["pairs"]]
    cfg_set(s, loose_live_allow=[])        # 生产配置已签字放开（"*"）；这里验证闸本身
    out = px.step(s, now, client=fc, data=data)
    assert fc.orders == [] and out["entries"] == [] and not pairs_state(s)
    assert len(out["loose_plans"]) == 1
    recs = Ledger(s.data, px.LOOSE_LEDGER).all()
    assert recs and all(r["kind"] == "plan" and r["sent"] is False for r in recs)
    assert not any(r["kind"] == "plan" for r in px._led(s).all())        # 主账本（真钱）里没有
    # 白名单里的 key 才真下
    cfg_set(s, loose_live_allow=[f"{A}|{B}"])
    out = px.step(s, later(now, 60), client=fc, data=data)
    assert len(out["entries"]) == 1 and fc.orders
    # 缺 strict 标记 = 不放行；而且 _open_pair 本身也拦
    cfg_set(s, loose_live_allow=[])
    ctx = px.Ctx(s, fc, px.cfg(s), int(now.timestamp() * 1000), "live")
    assert "影子" in px._open_pair(ctx, {"a": A, "b": B}, {"spread": 80.0}, px.load_state(s))["skipped"]


def test_E4_config_max_concurrent_is_two():
    import tomllib
    c = tomllib.loads((ROOT / "config.toml").read_text())["pairs"]
    assert c["max_concurrent"] == 2 and c["loose_live_allow"] == ["*"]     # 2026-10-21 Kea 签字
    assert c["max_concurrent"] * c["max_pair_loss_usd"] <= c["daily_loss_stop_usd"]


def test_E5_big_lot_pair_not_opened_and_imbalance_close_locks(s):
    fc = FakeClient(mids={A: 120.0 * 1.008 * 100, B: 120.0 * 100, A2: 1, B2: 1})    # 一手 0.01 × 12000 = $120 > 名义 × 5%
    set_mode(s, "live")
    data, now = make_data()
    fc.depth = {c: 1e9 for c in fc.mids}
    out = px.step(s, now, client=fc, data=data)
    assert fc.orders == [] and any("一手" in r for _, r in out["skips"])
    # imbalance 出场后锁 lock_h
    fc2 = FakeClient()
    out, data, now = live_entry(s, fc2)
    pid = next(iter(pairs_state(s)))
    fc2.pos[A] = fc2.pos[A] * 0.5     # 账本不动；用 close_req 模拟 imbalance
    px._led(s).append("close_req", pid, {"pair_id": pid, "reason": "imbalance", "t_ms": int(now.timestamp() * 1000)})
    px.step(s, later(now, 60), client=fc2, data=data)
    assert px.load_state(s)["locks"][f"{A}|{B}"]["reason"] == "imbalance"


def test_E6_lab_uses_pairs_gate(s):
    cfg_set(s, gate_min_day_ntl_vlm_usd=20000.0)
    assert pl.pairs_gate(s) == px.pairs_gate(s, px.cfg(s))
    md, res = pl.run(s, pl.fixture_raw(), "fixture", days=60, interval="5m", warmup=5, fetch=False)
    n_loose = sum(p["gate_ok"] for p in pl.discover_pairs(pl.fixture_raw(), pl.pairs_gate(s)))
    assert sum(p["gate_ok"] for p in res["pairs"]) == n_loose


def test_E2b_due_full_refetch_and_funding_are_staggered(s, monkeypatch):
    """冷启动后所有腿同时到期：每次抓数据最多 FULL_REFETCH_PER_LOAD 条腿全量重抓、FUNDING_REFRESH_PER_LOAD 条腿刷资金费率。"""
    import time as _t
    raw = pl.fixture_raw()
    monkeypatch.setattr(px.uv, "discover", lambda: raw)
    cfg_set(s, allow=[])
    full, fund = [], []
    def fake_fetch(coin, iv, days, start_ms=None, **k):
        if start_ms is None:
            full.append(coin)
        return [{"t": T0, "c": "1", "v": "1"}]
    monkeypatch.setattr(px.bl, "fetch_candles_paged", fake_fetch)
    monkeypatch.setattr(px.bl, "get_funding", lambda st, coin, d: (fund.append(coin) or [], "x"))
    cf = px.cfg(s)
    px._DATA.clear()
    d0 = px.load_data(s, T0 + 10 * IV, cf)
    legs = {c for p in d0["pairs"] for c in (p["a"], p["b"])}
    assert len(legs) > px.FULL_REFETCH_PER_LOAD and set(full) == legs     # 冷启动：全部全量
    # 让全部腿的全量重抓和资金费率都到期
    d0["candles_full_t"] = {c: 0 for c in legs}
    d0["funding_t"] = {c: (0, []) for c in legs}
    full.clear(); fund.clear()
    d1 = px.load_data(s, T0 + 11 * IV, cf)
    assert len(full) == min(len(legs), px.FULL_REFETCH_PER_LOAD)
    assert len(fund) == min(len(legs), px.FUNDING_REFRESH_PER_LOAD)
    stale = [c for c, v in d1["funding_t"].items() if v[0] == 0]
    assert len(stale) == len(legs) - len(fund)                            # 没刷新的保留旧时间，下次照样到期


def test_loose_live_allow_star_makes_all_thin_pairs_live():
    thin = {"a": "io:X", "b": "xyz:X", "strict": False}
    assert not px._live_eligible(thin, {"loose_live_allow": []})
    assert px._live_eligible(thin, {"loose_live_allow": ["*"]})
    assert px._live_eligible(thin, {"loose_live_allow": ["io:X|xyz:X"]})
    assert not px._live_eligible({"a": "io:X", "b": "xyz:X"}, {"loose_live_allow": []})   # 没标 strict 仍不放
