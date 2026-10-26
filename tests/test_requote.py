"""卡 G3：发单前刷新盘口（买单挂当前买一、卖单挂当前卖一，仍是 Alo）+ 重开前 5 分钟重挂一次。
开关 [autotrade] requote_entry；关 = 与改动前完全一致。交易所全部用假客户端。"""
from datetime import date, timedelta

import pytest

from desk import autotrade, scheduler
from desk.clock import weekend_for_friday
from desk.ledger import Ledger
from tests.test_autotrade import FakeClient, _lock
from tests.test_web import env  # noqa: F401

FRI_PX = 29700.0         # 周五收盘锚价：锁定时 dec_mid 30000 → 偏离 +101 bps → v2 反向 = 卖
BUY_FRI_PX = 30300.0     # 买方向用：dec_mid 30000 → 偏离 -99 bps → 反向 = 买


def _rq_lock(side=-1, fri=None, **feat):
    lk = _lock(side=side)
    fri = fri or (FRI_PX if side < 0 else BUY_FRI_PX)
    lk["features"].update(fri_close=fri, dev_bps=(30000.0 / fri - 1) * 1e4, **feat)
    return lk


class RqClient(FakeClient):
    """假交易所：有可刷新的盘口、带 sz/limitPx 的挂单簿、会递增的 oid。"""

    def __init__(self, bid=29995.0, ask=30005.0, top_exc=None):
        super().__init__()
        self.book = {"bid": bid, "ask": ask}
        self.top_exc = top_exc
        self.top_calls = 0
        self.next_oid = 100
        self.cancel_resp = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}

    def top(self, coin):
        self.top_calls += 1
        if self.top_exc:
            raise self.top_exc
        return dict(self.book)

    def post_only(self, coin, is_buy, sz, px):
        oid, self.next_oid = self.next_oid, self.next_oid + 1
        self.orders.append((coin, is_buy, sz, px))
        self.open.append({"coin": coin, "oid": oid, "sz": str(sz), "limitPx": str(px), "side": "B" if is_buy else "A"})
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def cancel(self, coin, oid):
        self.cancels.append(oid)
        self.open = [o for o in self.open if o["oid"] != oid]
        return self.cancel_resp


@pytest.fixture
def pushes(monkeypatch):
    out = []
    monkeypatch.setattr(autotrade.notify, "push", lambda settings, title, body, priority="default": out.append((title, body)))
    return out


def _setup(env, client, monkeypatch, requote=True):  # noqa: F811
    s, _ = env
    monkeypatch.setattr(autotrade, "_client_factory", lambda: client)
    autotrade.put(s, mode="live", capital_usd=700, max_loss_usd=50)
    s.raw.setdefault("autotrade", {})["requote_entry"] = requote
    return s, weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))


def _rows(s, kind):
    return [r for r in Ledger(s.data, "live").all() if r["kind"] == kind]


def _store_lock(s, w, lock):
    Ledger(s.data).append("lock", w.wid, lock)


# ------------------------------------------------------------------ 1. 开关关 = 老行为
def test_switch_off_is_old_behaviour(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient()
    s, w = _setup(env, fc, monkeypatch, requote=False)
    rec = autotrade.entry(s, w, _rq_lock(side=-1))
    assert fc.top_calls == 0                                         # 根本不读盘口
    assert fc.orders == [("xyz:XYZ100", False, rec["sz"], 30010.0)]  # 卖单挂锁定快照的卖一
    assert rec["sent"] is True and "requote_note" not in rec and "px_lock" not in rec and "dev_bps_now" not in rec
    # 重挂 job 也不动
    assert autotrade.requote_due(s, w) is False and autotrade.requote_unfilled(s, w) is None
    assert _rows(s, "requote") == [] and fc.cancels == []


def test_switch_off_keeps_old_depth_rule(env, monkeypatch, pushes):  # noqa: F811
    """关：一侧深度缺失仍按老写法（取另一侧）照常下单。"""
    fc = RqClient()
    s, w = _setup(env, fc, monkeypatch, requote=False)
    rec = autotrade.entry(s, w, _rq_lock(side=-1, bid_depth_usd=None))
    assert rec["sent"] is True and len(fc.orders) == 1


def test_switch_missing_section_means_off(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient()
    s, w = _setup(env, fc, monkeypatch)
    del s.raw["autotrade"]
    assert autotrade.requote_enabled(s) is False
    autotrade.entry(s, w, _rq_lock(side=-1))
    assert fc.top_calls == 0 and fc.orders[0][3] == 30010.0


def test_config_default_is_on():
    from desk import config
    assert config.load().raw["autotrade"]["requote_entry"] is True


# ------------------------------------------------------------------ 2. 发单前刷新
def test_refresh_sell_uses_current_ask(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient(bid=29995.0, ask=30005.0)
    s, w = _setup(env, fc, monkeypatch)
    rec = autotrade.entry(s, w, _rq_lock(side=-1))
    assert fc.top_calls == 1
    assert fc.orders[0][1] is False and fc.orders[0][3] == 30005.0           # 卖单挂当前卖一（不是锁定的 30010）
    assert rec["px"] == 30005.0 and rec["px_lock"] == 30010.0 and rec["sent"] is True
    assert rec["dev_bps_now"] == pytest.approx((30000.0 / FRI_PX - 1) * 1e4, abs=0.01)
    assert "requote_note" not in rec


def test_refresh_buy_uses_current_bid(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient(bid=29996.0, ask=30004.0)
    s, w = _setup(env, fc, monkeypatch)
    rec = autotrade.entry(s, w, _rq_lock(side=1))
    assert fc.orders[0][1] is True and fc.orders[0][3] == 29996.0            # 买单挂当前买一（锁定的是 29990）
    assert rec["px_lock"] == 29990.0


def test_edge_gone_after_refresh_writes_nolive(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient(bid=29720.0, ask=29730.0)        # 中间价 29725 → 偏离 +8 bps ≤ 50
    s, w = _setup(env, fc, monkeypatch)
    assert autotrade.entry(s, w, _rq_lock(side=-1)) is None
    assert autotrade.entry(s, w, _rq_lock(side=-1)) is None                  # 重复进来只写一次
    assert fc.orders == [] and fc.cancels == [] and _rows(s, "entry") == []
    rows = _rows(s, "nolive")
    assert len(rows) == 1 and rows[0]["reason"] == "发单前价格已回归，没有边际" and rows[0]["name"] == "XYZ100"


def test_edge_gone_when_exactly_at_threshold(env, monkeypatch, pushes):  # noqa: F811
    """和 model.decide_v2 同一个比较：|偏离| ≤ 触发线 = 不出手。"""
    mid = FRI_PX * (1 + 50 / 1e4)
    fc = RqClient(bid=mid - 1, ask=mid + 1)
    s, w = _setup(env, fc, monkeypatch)
    assert autotrade.entry(s, w, _rq_lock(side=-1)) is None
    assert _rows(s, "nolive")[0]["reason"] == "发单前价格已回归，没有边际"


def test_edge_gone_sign_flip(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient(bid=29495.0, ask=29505.0)        # 偏离 -67 bps：绝对值够大，但方向和锁定时（+）相反
    s, w = _setup(env, fc, monkeypatch)
    assert autotrade.entry(s, w, _rq_lock(side=-1)) is None
    assert fc.orders == [] and _rows(s, "nolive")[0]["reason"] == "发单前价格已回归，没有边际"


def test_threshold_read_from_config_not_hardcoded(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient()                                  # 偏离 +101 bps
    s, w = _setup(env, fc, monkeypatch)
    s.raw["strategy"]["fade_threshold_bps"] = 150
    assert autotrade.entry(s, w, _rq_lock(side=-1)) is None
    assert _rows(s, "nolive")[0]["reason"] == "发单前价格已回归，没有边际"
    s.raw["strategy"]["fade_threshold_bps"] = 50
    assert autotrade.entry(s, w, _rq_lock(side=-1))["sent"] is True


@pytest.mark.parametrize("book", [{"bid": None, "ask": 30005.0}, {"bid": 29995.0, "ask": None}, {"bid": None, "ask": None},
                                  {"bid": 0.0, "ask": 30005.0}])
def test_empty_side_writes_nolive(env, monkeypatch, pushes, book):  # noqa: F811
    fc = RqClient()
    fc.book = book
    s, w = _setup(env, fc, monkeypatch)
    assert autotrade.entry(s, w, _rq_lock(side=-1)) is None
    assert fc.orders == [] and _rows(s, "entry") == []
    rows = _rows(s, "nolive")
    assert len(rows) == 1 and rows[0]["reason"] == "盘口一侧为空"


def test_refresh_exception_falls_back_to_lock_px(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient(top_exc=RuntimeError("l2 超时"))
    s, w = _setup(env, fc, monkeypatch)
    rec = autotrade.entry(s, w, _rq_lock(side=-1))
    assert fc.orders[0][3] == 30010.0 and rec["sent"] is True                # 照旧用锁定价发单
    assert rec["requote_note"] == "刷新盘口失败，用锁定价" and "px_lock" not in rec
    assert _rows(s, "nolive") == []


def test_lock_depth_one_side_missing_is_zero_depth(env, monkeypatch, pushes):  # noqa: F811
    for feat in ({"bid_depth_usd": None}, {"ask_depth_usd": 0}, {"bid_depth_usd": None, "ask_depth_usd": None}):
        fc = RqClient()
        s, w = _setup(env, fc, monkeypatch)
        assert autotrade.entry(s, w, _rq_lock(side=-1, **feat)) is None
        assert fc.orders == [] and _rows(s, "entry") == []
        assert [r["reason"] for r in _rows(s, "nolive")] == ["盘口一侧为空"]
        Ledger(s.data, "live").path.unlink()


def test_shadow_mode_never_touches_exchange(env, monkeypatch, pushes):  # noqa: F811
    fc = RqClient()
    s, w = _setup(env, fc, monkeypatch)
    autotrade.put(s, mode="shadow")
    rec = autotrade.entry(s, w, _rq_lock(side=-1))
    assert rec["sent"] is False and fc.top_calls == 0 and fc.orders == []


# ------------------------------------------------------------------ 3. 重挂
def _placed(env, monkeypatch, **kw):  # noqa: F811
    """已经挂出一张卖单（oid 100）并存好 lock，返回 (s, w, fc, entry)。"""
    fc = RqClient(**kw.pop("client", {}))
    s, w = _setup(env, fc, monkeypatch, **kw)
    lk = _rq_lock(side=-1)
    _store_lock(s, w, lk)
    ent = autotrade.entry(s, w, lk)
    assert ent["sent"] is True and ent["oid"] == 100
    return s, w, fc, ent


def test_requote_happy_path_once_only(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.book = {"bid": 29998.0, "ask": 30002.0}                                # 之后盘口动了，偏离仍 > 50
    rec = autotrade.requote_unfilled(s, w)
    assert fc.cancels == [100]
    assert len(fc.orders) == 2 and fc.orders[1] == ("xyz:XYZ100", False, ent["sz"], 30002.0)
    assert rec["kind"] == "requote" and rec["weekend"] == w.wid and rec["name"] == "XYZ100"
    assert rec["old_px"] == 30005.0 and rec["new_px"] == 30002.0 and rec["remaining_sz"] == ent["sz"]
    assert rec["exch_status"] == "resting" and rec["exch_error"] is None and rec["skipped"] is None and rec["oid"] == 101
    # 同一个周末同一个合约不会第二次重挂
    assert autotrade.requote_unfilled(s, w) is None and autotrade.requote_unfilled(s, w) is None
    assert len(fc.orders) == 2 and fc.cancels == [100] and len(_rows(s, "requote")) == 1
    assert autotrade.requote_due(s, w) is False
    # 重开前 1 分钟的撤单照旧：撤掉新单，记账
    c = autotrade.cancel_unfilled(s, w)
    assert fc.cancels == [100, 101] and c["cancelled"] == 1


def test_requote_remaining_is_open_size_after_partial_fill(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.open[0]["sz"] = "0.0010"                                              # 已成交一部分，只剩 0.0010
    rec = autotrade.requote_unfilled(s, w)
    assert rec["remaining_sz"] == 0.001 and fc.orders[1][2] == 0.001


def test_requote_remaining_shrinks_if_filled_between_read_and_cancel(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.fill_list = [{"sz": str(ent["sz"] - 0.0005), "side": "A", "dir": "Open Short", "px": "30005"}]   # 成交记录显示只剩 0.0005
    rec = autotrade.requote_unfilled(s, w)
    assert rec["remaining_sz"] == pytest.approx(0.0005) and fc.orders[1][2] == pytest.approx(0.0005)


def test_requote_nothing_resting_no_record(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.open = []                                                              # 已全部成交
    assert autotrade.requote_unfilled(s, w) is None
    assert _rows(s, "requote") == [] and fc.cancels == [] and len(fc.orders) == 1


def test_requote_edge_gone_cancels_but_does_not_repost(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.book = {"bid": 29720.0, "ask": 29730.0}
    rec = autotrade.requote_unfilled(s, w)
    assert fc.cancels == [100] and len(fc.orders) == 1                       # 撤了，没重挂
    assert rec["skipped"] == "发单前价格已回归，没有边际" and rec["new_px"] is None and rec["exch_status"] is None
    assert rec["old_px"] == 30005.0 and rec["remaining_sz"] == ent["sz"]
    assert autotrade.requote_unfilled(s, w) is None and len(_rows(s, "requote")) == 1


def test_requote_edge_gone_on_sign_flip(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.book = {"bid": 29495.0, "ask": 29505.0}
    assert autotrade.requote_unfilled(s, w)["skipped"] == "发单前价格已回归，没有边际"


def test_requote_empty_side_skips(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.book = {"bid": None, "ask": 30002.0}
    rec = autotrade.requote_unfilled(s, w)
    assert rec["skipped"] == "盘口一侧为空" and len(fc.orders) == 1 and fc.cancels == [100]


def test_requote_refresh_exception_reposts_at_old_px(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.top_exc = RuntimeError("l2 超时")
    rec = autotrade.requote_unfilled(s, w)
    assert rec["requote_note"] == "刷新盘口失败，用锁定价" and rec["new_px"] == 30005.0 and rec["skipped"] is None
    assert len(fc.orders) == 2 and fc.orders[1][3] == 30005.0


def test_requote_cancel_error_does_not_double_up(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.cancel_resp = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed, already canceled, or filled."}]}}}
    rec = autotrade.requote_unfilled(s, w)
    assert len(fc.orders) == 1 and rec["skipped"].startswith("撤单失败") and "already canceled" in rec["exch_error"]


def test_requote_rejected_repost_recorded(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    err = "Post only order would have immediately matched"
    monkeypatch.setattr(fc, "post_only", lambda *a: {"status": "ok", "response": {"data": {"statuses": [{"error": err}]}}})
    rec = autotrade.requote_unfilled(s, w)
    assert rec["exch_status"] == "error" and rec["exch_error"] == err and rec["new_px"] is not None
    assert pushes[-1][0].startswith("重挂被拒")


def test_requote_exception_is_recorded_and_counts_as_the_one_try(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    monkeypatch.setattr(fc, "open_orders", lambda coin: (_ for _ in ()).throw(RuntimeError("boom")))
    rec = autotrade.requote_unfilled(s, w)
    assert rec["exch_error"] == "boom"
    assert autotrade.requote_unfilled(s, w) is None


def test_requote_only_when_live_switch_on_and_sent(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    autotrade.put(s, mode="off")
    assert autotrade.requote_unfilled(s, w) is None and autotrade.requote_due(s, w) is False
    autotrade.put(s, mode="live")
    s.raw["autotrade"]["requote_entry"] = False
    assert autotrade.requote_unfilled(s, w) is None and fc.cancels == []
    s.raw["autotrade"]["requote_entry"] = True
    other = weekend_for_friday(date(2026, 10, 9), s, s.inst("NVDA"))        # 没有 entry 的合约
    assert autotrade.requote_unfilled(s, other) is None and autotrade.requote_due(s, other) is False
    assert autotrade.requote_unfilled(s, w) is not None


def test_requote_skipped_after_cancel_or_exit(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    autotrade.cancel_unfilled(s, w)
    assert autotrade.requote_unfilled(s, w) is None and autotrade.requote_due(s, w) is False
    assert len(fc.orders) == 1


def test_other_contracts_orders_untouched(env, monkeypatch, pushes):  # noqa: F811
    """只动自己 entry 的那张单（按 oid），同币种上别的线的挂单不碰。"""
    s, w, fc, ent = _placed(env, monkeypatch)
    fc.open.append({"coin": "xyz:XYZ100", "oid": 555, "sz": "1.0", "limitPx": "1", "side": "B"})
    rec = autotrade.requote_unfilled(s, w)
    assert fc.cancels == [100] and rec["remaining_sz"] == ent["sz"]


# ------------------------------------------------------------------ 4. 时间线
def _first_minute(s, w, name, done=None, span=(-20, 8)):
    """从重开前 20 分钟到重开后 8 分钟每 20 秒扫一遍，返回 name 第一次/最后一次出现相对重开的分钟数。"""
    hits = []
    for sec in range(span[0] * 60, span[1] * 60, 20):
        now = w.resume + timedelta(seconds=sec)
        if f"{w.wid}:{name}" in [k for k, _ in scheduler.due_jobs(now, s, set(done or ()))]:
            hits.append(sec / 60)
    return (min(hits), max(hits)) if hits else None


def test_timeline_requote_window_before_cancel(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    done = {f"{w.wid}:lock", f"{w.wid}:live_entry"}
    rq = _first_minute(s, w, "live_requote", done)
    cn = _first_minute(s, w, "live_cancel", done)
    assert rq == (-5.0, pytest.approx(-3.0 - 1 / 3))                          # 重开前 5 分钟起，约 2 分钟窗口（-3 分钟前停）
    assert cn[0] == -1.0
    assert rq[1] < cn[0]                                                      # 全部在撤单之前
    # 窗口外不出现：重开前 6 分钟、3 分钟、1 分钟
    for m in (6, 3, 1, 0):
        assert f"{w.wid}:live_requote" not in [k for k, _ in scheduler.due_jobs(w.resume - timedelta(minutes=m), s, done)]
    # 已跑过（done）不再出
    assert _first_minute(s, w, "live_requote", done | {f"{w.wid}:live_requote"}) is None


def test_timeline_each_contract_uses_own_resume(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)                                # XYZ100：周日 18:00 ET
    nv = weekend_for_friday(date(2026, 10, 9), s, s.inst("NVDA"))            # NVDA：周日 20:00 ET
    Ledger(s.data, "live").append("entry", nv.wid, {"sent": True, "coin": "xyz:NVDA", "name": "NVDA", "side": "卖"})
    assert nv.resume - w.resume == timedelta(hours=2)
    done = {f"{w.wid}:lock", f"{nv.wid}:lock"}
    ks = lambda now: {k for k, _ in scheduler.due_jobs(now, s, done)}  # noqa: E731
    t = w.resume - timedelta(minutes=4)
    assert f"{w.wid}:live_requote" in ks(t) and f"{nv.wid}:live_requote" not in ks(t)
    t = nv.resume - timedelta(minutes=4)
    assert f"{nv.wid}:live_requote" in ks(t) and f"{w.wid}:live_requote" not in ks(t)


def test_timeline_no_job_without_entry_or_live_or_switch(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    t = w.resume - timedelta(minutes=4)
    key = f"{w.wid}:live_requote"
    ks = lambda: [k for k, _ in scheduler.due_jobs(t, s, set())]  # noqa: E731
    assert key in ks()
    s.raw["autotrade"]["requote_entry"] = False
    assert key not in ks()
    s.raw["autotrade"]["requote_entry"] = True
    autotrade.put(s, mode="shadow")
    assert key not in ks()
    autotrade.put(s, mode="live")
    assert key in ks()
    Ledger(s.data, "live").append("requote", w.wid, {"skipped": "x"})
    assert key not in ks()


def test_run_job_dispatches_requote_not_exit(env, monkeypatch, pushes):  # noqa: F811
    s, w, fc, ent = _placed(env, monkeypatch)
    rec = scheduler.run_job(f"{w.wid}:live_requote", w, s)
    assert rec["kind"] == "requote" and fc.closed == [] and _rows(s, "exit") == []
    assert len(fc.orders) == 2


def test_full_weekend_sequence(env, monkeypatch, pushes):  # noqa: F811
    """lock→entry→requote→cancel→exit 按时间顺序各跑一次，requote 在 cancel 前。"""
    s, w, fc, ent = _placed(env, monkeypatch)
    done = {f"{w.wid}:lock", f"{w.wid}:live_entry"}
    order = []
    for sec in range(-15 * 60, 7 * 60, 20):
        now = w.resume + timedelta(seconds=sec)
        for k, wk in scheduler.due_jobs(now, s, done):
            if k.startswith(w.wid) and k.split(":")[-1] in ("live_requote", "live_cancel", "live_exit"):
                scheduler.run_job(k, wk, s)
                done.add(k)
                order.append(k.split(":")[-1])
    assert order == ["live_requote", "live_cancel", "live_exit"]
    assert [r["kind"] for r in Ledger(s.data, "live").all() if r["kind"] in ("requote", "cancel", "exit")] == ["requote", "cancel", "exit"]
