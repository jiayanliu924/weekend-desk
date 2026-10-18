"""跨 dex 配对真下单（desk/pairs_exec.py）：全部用注入的假客户端和确定性假数据，不连交易所。
覆盖：off/shadow 不发单、live 两腿开仓、一腿被拒回补、部分成交回补、平仓只用 reduce-only 且只平我们自己的量（手动仓位不动）、
各种上限拦截、当日亏损止损切回 off、cloid 前缀、杠杆封顶、异常路径不留单腿。"""
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from desk import autotrade, config
from desk import backtest_lib as bl
from desk import pairs_exec as px
from desk import pairs_lab as pl
from desk.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
IV = bl.INTERVAL_MS["5m"]
T0 = 1_760_000_000_000 // pl.DAY_MS * pl.DAY_MS
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
def candles(coin_prices):
    return [{"t": T0 + i * IV, "c": str(p), "v": "10"} for i, p in enumerate(coin_prices)]


def make_data(n_days=17, tail=(), base=120.0, allow_second=False):
    """B 恒为 base，A = base·(1+s/1e4)。n = n_days·288 + 1 根，最后一根（序号 n−1，≡ 0 mod 24）是尖峰；
    tail 是追加在后面的价差（bps），例如 (0, 0) 表示价差已收敛。返回 (data, now)：now = 最后一根收盘后 20 秒。"""
    n = n_days * 288 + 1
    sp = [80.0 if (i % 24) in (0, 1) else 0.0 for i in range(n)] + list(tail)
    pa = [base * (1 + x / 1e4) for x in sp]
    pb = [base] * len(sp)
    raw = pl.fixture_raw()
    pairs = [p for p in pl.discover_pairs(raw, {"min_day_ntl_vlm_usd": 2e5, "min_oi_usd": 1e5, "max_spread_bps": 20.0})
             if p["kind"] == "same" and p["gate_ok"]]
    cs = {A: candles(pa), B: candles(pb)}
    if allow_second:
        cs.update({A2: candles(pa), B2: candles(pb)})
    pairs = [p for p in pairs if p["a"] in cs]
    data = {"pairs": pairs, "candles": cs, "funding": {}}
    now = datetime.fromtimestamp((T0 + len(sp) * IV) / 1000 + 20, timezone.utc)
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
    fc.mids[A] *= 1.01
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
    cfg_set(s, data_timeout_s=0.1)
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
    cfg_set(s, data_timeout_s=0.05)
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
