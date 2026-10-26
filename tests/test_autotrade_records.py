"""卡 G1：真钱路径只加记录——交易所返回解析、nolive（静默跳过/错过窗口）、cancel 成交量、平仓轮询。下单/平仓行为本身不变。"""
import asyncio
from datetime import date, timedelta

import pytest

from desk import account_stop, autotrade, scheduler
from desk.clock import weekend_for_friday
from desk.ledger import Ledger
from tests.test_autotrade import FakeClient, _lock
from tests.test_web import env  # noqa: F401


def _resp(*statuses):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": list(statuses)}}}


class RespClient(FakeClient):
    def __init__(self, resp):
        super().__init__()
        self.resp = resp

    def post_only(self, coin, is_buy, sz, px):
        super().post_only(coin, is_buy, sz, px)
        return self.resp


@pytest.fixture
def pushes(monkeypatch):
    out = []
    monkeypatch.setattr(autotrade.notify, "push", lambda settings, title, body, priority="default": out.append((title, body)))
    return out


def _live(env, client, monkeypatch):
    s, _ = env
    monkeypatch.setattr(autotrade, "_client_factory", lambda: client)
    autotrade.put(s, mode="live", capital_usd=700, max_loss_usd=50)
    return s, weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))


# ------------------------------------------------------------------ 1. 交易所返回
def test_resp_resting(env, monkeypatch, pushes):  # noqa: F811
    s, w = _live(env, RespClient(_resp({"resting": {"oid": 123}})), monkeypatch)
    rec = autotrade.entry(s, w, _lock(side=-1))
    assert rec["sent"] is True and rec["exch_status"] == "resting" and rec["oid"] == 123 and rec["exch_error"] is None
    assert pushes[-1][0].startswith("已挂单")


def test_resp_filled(env, monkeypatch, pushes):  # noqa: F811
    s, w = _live(env, RespClient(_resp({"filled": {"totalSz": "0.0033", "avgPx": "30010.0", "oid": 5}})), monkeypatch)
    rec = autotrade.entry(s, w, _lock(side=-1))
    assert rec["sent"] is True and rec["exch_status"] == "filled" and rec["oid"] == 5 and rec["exch_error"] is None


def test_resp_error_not_sent(env, monkeypatch, pushes):  # noqa: F811
    msg = "Post only order would have immediately matched, bbo was 30010@30011"
    fc = RespClient(_resp({"error": msg}))
    s, w = _live(env, fc, monkeypatch)
    rec = autotrade.entry(s, w, _lock(side=-1))
    assert rec["sent"] is False and rec["exch_status"] == "error" and rec["exch_error"] == msg and rec["oid"] is None
    assert pushes[-1][1] == "下单被拒：" + msg and "已挂单" not in pushes[-1][0] + pushes[-1][1]
    # 没挂出去 → 后面撤单/平仓都不动
    assert autotrade.cancel_unfilled(s, w) is None and autotrade.exit_(s, w) is None and not fc.cancels and not fc.closed


@pytest.mark.parametrize("resp", [{"status": "ok"}, None, "garbage", {"response": {"data": {}}}, {"response": {"data": {"statuses": []}}},
                                  {"response": {"data": {"statuses": ["waitingForFill"]}}}, {"response": "x"}])
def test_resp_unknown_keeps_sent(env, monkeypatch, pushes, resp):  # noqa: F811
    s, w = _live(env, RespClient(resp), monkeypatch)
    rec = autotrade.entry(s, w, _lock(side=-1))
    assert rec["sent"] is True and rec["exch_status"] == "unknown" and rec["exch_error"] is None and rec["oid"] is None


def test_resp_top_level_err():
    assert autotrade.parse_order_resp({"status": "err", "response": "Insufficient margin"}) == \
        {"exch_status": "error", "exch_error": "Insufficient margin", "oid": None}


# ------------------------------------------------------------------ 2. nolive
def _nolive(s):
    return [r for r in Ledger(s.data, "live").all() if r["kind"] == "nolive"]


def test_nolive_no_lock_and_dedup(env, monkeypatch):  # noqa: F811
    s, w = _live(env, FakeClient(), monkeypatch)
    for _ in range(3):
        assert autotrade.entry(s, w, None) is None
    rows = _nolive(s)
    assert len(rows) == 1 and rows[0]["weekend"] == w.wid and rows[0]["name"] == w.name and "lock" in rows[0]["reason"] and rows[0]["ts"] > 0


def test_nolive_already_entered(env, monkeypatch):  # noqa: F811
    fc = FakeClient()
    s, w = _live(env, fc, monkeypatch)
    autotrade.entry(s, w, _lock(side=-1))
    assert autotrade.entry(s, w, _lock()) is None and autotrade.entry(s, w, _lock()) is None
    rows = _nolive(s)
    assert len(rows) == 1 and "已有" in rows[0]["reason"] and len(fc.orders) == 1


def test_nolive_account_stop(env, monkeypatch):  # noqa: F811
    s, w = _live(env, FakeClient(), monkeypatch)
    monkeypatch.setattr(account_stop, "check", lambda st: {"tripped": True})
    assert autotrade.entry(s, w, _lock()) is None and autotrade.entry(s, w, _lock()) is None
    rows = _nolive(s)
    assert len(rows) == 1 and "止损" in rows[0]["reason"]


def test_nolive_not_written_when_off(env, monkeypatch):  # noqa: F811
    s, _ = env
    monkeypatch.setattr(autotrade, "_client_factory", lambda: FakeClient())
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))
    assert autotrade.entry(s, w, None) is None and autotrade.entry(s, w, _lock()) is None
    assert _nolive(s) == []


def test_nolive_different_reason_or_name_separate(env, monkeypatch):  # noqa: F811
    s, w = _live(env, FakeClient(), monkeypatch)
    autotrade.note_nolive(s, w.wid, "A", "r1")
    autotrade.note_nolive(s, w.wid, "A", "r1")
    autotrade.note_nolive(s, w.wid, "A", "r2")
    autotrade.note_nolive(s, w.wid, "B", "r1")
    assert len(_nolive(s)) == 3


def test_scheduler_missed_window(env, monkeypatch):  # noqa: F811
    s, w = _live(env, FakeClient(), monkeypatch)
    now = w.resume - timedelta(minutes=1)            # 发单窗口（到重开前 2 分钟）已过
    done = {f"{w.wid}:lock"}
    assert f"{w.wid}:live_missed" in [k for k, _ in scheduler.due_jobs(now, s, done)]
    assert f"{w.wid}:live_entry" not in [k for k, _ in scheduler.due_jobs(now, s, done)]
    scheduler.run_job(f"{w.wid}:live_missed", w, s)
    scheduler.run_job(f"{w.wid}:live_missed", w, s)
    rows = _nolive(s)
    assert len(rows) == 1 and rows[0]["reason"].startswith("错过发单窗口") and rows[0]["name"] == w.name
    done.add(f"{w.wid}:live_missed")
    assert f"{w.wid}:live_missed" not in [k for k, _ in scheduler.due_jobs(now, s, done)]
    # 窗口内（lock 刚做完）不产生 live_missed
    inside = w.decision + timedelta(minutes=1)
    assert f"{w.wid}:live_missed" not in [k for k, _ in scheduler.due_jobs(inside, s, {f"{w.wid}:lock"})]


def test_scheduler_missed_right_after_lock(env, monkeypatch):  # noqa: F811
    s, w = _live(env, FakeClient(), monkeypatch)
    monkeypatch.setattr(scheduler, "entry_due_now", lambda wk, now: False)
    asyncio.run(scheduler._entry_right_after_lock(f"{w.wid}:lock", w, s, set()))
    asyncio.run(scheduler._entry_right_after_lock(f"{w.wid}:lock", w, s, set()))
    rows = _nolive(s)
    assert len(rows) == 1 and rows[0]["reason"].startswith("错过发单窗口")


def test_scheduler_missed_off_writes_nothing(env, monkeypatch):  # noqa: F811
    s, _ = env
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))
    assert autotrade.note_missed_window(s, w) is None and _nolive(s) == []


# ------------------------------------------------------------------ 3. cancel 记录
def test_cancel_filled_fields(env, monkeypatch):  # noqa: F811
    fc = FakeClient()
    s, w = _live(env, fc, monkeypatch)
    autotrade.entry(s, w, _lock(side=-1))
    fc.pos = -0.0033
    fc.fill_list = [{"coin": "xyz:XYZ100", "sz": "0.0010", "px": "30010", "side": "A", "dir": "Open Short"},
                    {"coin": "xyz:XYZ100", "sz": "0.0023", "px": "30013", "side": "A", "dir": "Open Short"}]
    c = autotrade.cancel_unfilled(s, w)
    assert c["filled_sz"] == pytest.approx(0.0033)
    assert c["entry_avg_px"] == pytest.approx((0.0010 * 30010 + 0.0023 * 30013) / 0.0033)


def test_cancel_filled_fallbacks(env, monkeypatch):  # noqa: F811
    fc = FakeClient()
    s, w = _live(env, fc, monkeypatch)
    autotrade.entry(s, w, _lock(side=-1))
    fc.pos = -0.002                                   # 没有成交记录，但持仓在 → 用持仓量，均价取不到
    c = autotrade.cancel_unfilled(s, w)
    assert c["filled_sz"] == pytest.approx(0.002) and c["entry_avg_px"] is None
    # 完全没成交
    fc2 = FakeClient()
    s2, w2 = _live(env, fc2, monkeypatch)
    w2 = weekend_for_friday(date(2026, 10, 16), s2, s2.inst("XYZ100"))
    autotrade.entry(s2, w2, _lock(side=-1))
    c2 = autotrade.cancel_unfilled(s2, w2)
    assert c2["filled_sz"] == 0.0 and c2["entry_avg_px"] is None


def test_cancel_fills_unavailable_gives_null(env, monkeypatch):  # noqa: F811
    class NoFills(FakeClient):
        def fills(self, coin, since):
            raise RuntimeError("boom")
    fc = NoFills()
    s, w = _live(env, fc, monkeypatch)
    autotrade.entry(s, w, _lock(side=-1))
    c = autotrade.cancel_unfilled(s, w)
    assert c["cancelled"] == 1 and c["filled_sz"] is None and c["entry_avg_px"] is None and "error" not in c


# ------------------------------------------------------------------ 4. 平仓轮询
class LaggyFills(FakeClient):
    """前 lag 次读成交返回空（模拟成交回报延迟），之后才有平仓成交。"""

    def __init__(self, lag, mid=30020.0):
        super().__init__()
        self.lag, self.reads, self._mid = lag, 0, mid

    def mid(self, coin):
        return self._mid

    def fills(self, coin, since):
        self.reads += 1
        return [] if self.reads <= self.lag else self.fill_list


CLOSE_FILLS = [{"coin": "xyz:XYZ100", "sz": "0.0033", "px": "30000", "fee": "0.01", "closedPnl": "0", "dir": "Open Short"},
               {"coin": "xyz:XYZ100", "sz": "0.0033", "px": "29990", "fee": "0.03", "closedPnl": "0.20", "dir": "Close Short"}]


def _open_and_pos(env, fc, monkeypatch):
    s, w = _live(env, fc, monkeypatch)
    autotrade.entry(s, w, _lock(side=-1))
    fc.pos = -0.0033
    fc.fill_list = CLOSE_FILLS
    return s, w


def test_exit_polls_until_close_fill(env, monkeypatch):  # noqa: F811
    fc = LaggyFills(lag=2)
    sleeps = []
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: sleeps.append(sec))
    s, w = _open_and_pos(env, fc, monkeypatch)
    x = autotrade.exit_(s, w)
    assert fc.reads == 3 and sleeps == [1.0, 1.0, 1.0]
    assert x["fills_complete"] is True and x["n_fills"] == 2
    assert x["pnl_usd"] == pytest.approx(0.16)                       # 盈亏口径不变：Σ(closedPnl − fee)
    assert x["exit_avg_px"] == pytest.approx(29990) and x["fee_usd"] == pytest.approx(0.04) and x["mid_at_exit"] == 30020.0


def test_exit_gives_up_after_3_polls(env, monkeypatch, pushes):  # noqa: F811
    fc = LaggyFills(lag=99)
    sleeps = []
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: sleeps.append(sec))
    s, w = _open_and_pos(env, fc, monkeypatch)
    x = autotrade.exit_(s, w)
    assert fc.reads == 3 and sleeps == [1.0] * 3 and x["fills_complete"] is False
    assert x["pnl_usd"] == 0 and x["exit_avg_px"] is None and x["fee_usd"] == 0
    assert "没读全" in pushes[-1][1]


class FlakyFills(FakeClient):
    """第 1 次读成交只有开仓（没平仓成交），之后全抛错；close 返回带 avgPx/totalSz。"""

    def __init__(self, first, close_resp=None):
        super().__init__()
        self.first, self.reads, self.close_resp = first, 0, close_resp

    def close(self, coin):
        super().close(coin)
        return self.close_resp or {"status": "ok"}

    def fills(self, coin, since):
        self.reads += 1
        if self.reads == 1:
            return self.first
        raise RuntimeError("fills 超时")


OPEN_ONLY = [CLOSE_FILLS[0]]
CLOSE_RESP = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "0.0033", "avgPx": "29990.0", "oid": 9}}]}}}


def test_exit_poll_exception_keeps_resp_and_estimates_pnl(env, monkeypatch, pushes):  # noqa: F811
    fc = FlakyFills(OPEN_ONLY, CLOSE_RESP)
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: None)
    s, w = _open_and_pos(env, fc, monkeypatch)
    x = autotrade.exit_(s, w)
    assert fc.reads == 3 and fc.closed == ["xyz:XYZ100"]
    assert x["kind"] == "exit" and "error" not in x and x["resp"] == CLOSE_RESP
    assert x["fills_complete"] is False and x["pnl_source"] == "resp_estimate" and x["poll_error"] == "fills 超时"
    # 空头 0.0033 张：入场价 30010（挂单价）→ 平仓均价 29990：(30010-29990)*0.0033 = +0.066，再减已读到的手续费 0.01
    assert x["pnl_usd"] == pytest.approx(0.066 - 0.01) and x["exit_avg_px"] == 29990.0
    assert autotrade.realized_total(s) == pytest.approx(x["pnl_usd"])           # realized_total 读得到这笔
    assert pushes[-1][0] == "自动下单结果" and "已平仓，成交明细稍后核对" in pushes[-1][1]
    assert not any("平仓失败" in a for a, _ in pushes)


def test_exit_second_poll_raises_uses_first_poll_fills(env, monkeypatch, pushes):  # noqa: F811
    partial = [CLOSE_FILLS[0], {**CLOSE_FILLS[1], "sz": "0.0010", "closedPnl": "0.05", "fee": "0.01"}]   # 平了一部分
    fc = FlakyFills(partial, CLOSE_RESP)
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: None)
    s, w = _open_and_pos(env, fc, monkeypatch)
    x = autotrade.exit_(s, w)
    # 读到 0.0010 的真实成交：(0−0.01)+(0.05−0.01)；没读到的 0.0023 用平仓返回 avgPx 29990 对入场价 30010（空单）估算 +0.046
    assert x["pnl_usd"] == pytest.approx(0.03 + 0.0023 * 20) and x["pnl_source"] == "partial_plus_estimate"
    assert x["fills_complete"] is False and x["n_fills"] == 2 and "error" not in x
    assert "已含入场手续费" not in pushes[-1][1] and "其余按平仓返回估算" in pushes[-1][1] and "未含手续费" not in pushes[-1][1]


class PartialFills(FakeClient):
    """一直只读到一部分平仓成交（没有异常）。"""

    def __init__(self, resp):
        super().__init__()
        self.resp = resp

    def close(self, coin):
        super().close(coin)
        return self.resp


def test_exit_partial_close_fills_plus_estimate(env, monkeypatch, pushes):  # noqa: F811
    fc = PartialFills(CLOSE_RESP)
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: None)
    s, w = _open_and_pos(env, fc, monkeypatch)
    fc.fill_list = [CLOSE_FILLS[0], {**CLOSE_FILLS[1], "sz": "0.0020", "closedPnl": "0.12", "fee": "0.02"}]
    x = autotrade.exit_(s, w)
    assert x["fills_complete"] is False and x["pnl_source"] == "partial_plus_estimate" and "poll_error" not in x
    assert x["pnl_usd"] == pytest.approx((0 - 0.01) + (0.12 - 0.02) + 0.0013 * 20)          # 剩 0.0013 按估算
    assert autotrade.realized_total(s) == pytest.approx(x["pnl_usd"])


def test_exit_partial_without_usable_resp_keeps_real_fills_only(env, monkeypatch, pushes):  # noqa: F811
    fc = PartialFills({"status": "ok"})
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: None)
    s, w = _open_and_pos(env, fc, monkeypatch)
    fc.fill_list = [CLOSE_FILLS[0], {**CLOSE_FILLS[1], "sz": "0.0020", "closedPnl": "0.12", "fee": "0.02"}]
    x = autotrade.exit_(s, w)
    assert x["pnl_source"] == "fills" and x["fills_complete"] is False and x["pnl_usd"] == pytest.approx(0.09)


def test_exit_full_close_fills_not_estimated(env, monkeypatch, pushes):  # noqa: F811
    fc = PartialFills(CLOSE_RESP)
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: None)
    s, w = _open_and_pos(env, fc, monkeypatch)
    x = autotrade.exit_(s, w)
    assert x["pnl_source"] == "fills" and x["fills_complete"] is True and x["pnl_usd"] == pytest.approx(0.16)


def test_exit_resp_estimate_message_is_honest_about_fees(env, monkeypatch, pushes):  # noqa: F811
    fc = FlakyFills(OPEN_ONLY, CLOSE_RESP)
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: None)
    s, w = _open_and_pos(env, fc, monkeypatch)
    autotrade.exit_(s, w)
    m = pushes[-1][1]
    assert "已含入场手续费" in m and "平仓手续费没算进去" in m and "未含手续费" not in m


def test_exit_poll_exception_and_unparseable_resp_still_writes_exit(env, monkeypatch, pushes):  # noqa: F811
    fc = FlakyFills([], None)
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: None)
    s, w = _open_and_pos(env, fc, monkeypatch)
    x = autotrade.exit_(s, w)
    assert x["pnl_usd"] == 0 and x["fills_complete"] is False and "error" not in x and x["position_before"] == -0.0033
    assert "平仓失败" not in pushes[-1][0]


def test_exit_close_order_failure_is_still_a_failure(env, monkeypatch, pushes):  # noqa: F811
    fc = FakeClient()
    s, w = _open_and_pos(env, fc, monkeypatch)
    monkeypatch.setattr(fc, "close", lambda coin: (_ for _ in ()).throw(RuntimeError("拒绝")))
    x = autotrade.exit_(s, w)
    assert x["error"] == "拒绝" and pushes[-1][0].startswith("平仓失败")


def test_exit_no_position_does_not_poll(env, monkeypatch):  # noqa: F811
    fc = LaggyFills(lag=0)
    sleeps = []
    monkeypatch.setattr(autotrade, "_sleep", lambda sec: sleeps.append(sec))
    s, w = _live(env, fc, monkeypatch)
    autotrade.entry(s, w, _lock(side=-1))
    fc.fill_list = []
    x = autotrade.exit_(s, w)
    assert fc.reads == 1 and sleeps == [] and fc.closed == []
    assert x["fills_complete"] is True and x["n_fills"] == 0 and x["mid_at_exit"] is None and x["pnl_usd"] == 0


def test_exit_mid_unavailable_is_null(env, monkeypatch):  # noqa: F811
    fc = _open_and_pos(env, FakeClient(), monkeypatch)       # FakeClient 没有 mid()
    s, w = fc
    x = autotrade.exit_(s, w)
    assert x["mid_at_exit"] is None and x["fills_complete"] is True and x["pnl_usd"] == pytest.approx(0.16)


def test_close_seen_partial_fill_keeps_waiting():
    half = [{"sz": "0.0016", "dir": "Close Short", "closedPnl": "0.1"}]
    assert not autotrade._close_seen(half, -0.0033)
    assert autotrade._close_seen(half + [{"sz": "0.0017", "dir": "Close Short", "closedPnl": "0.1"}], -0.0033)


# ------------------------------------------------------------------ 5. 老记录兼容
def test_old_records_without_new_fields(env, monkeypatch):  # noqa: F811
    fc = FakeClient()
    s, w = _live(env, fc, monkeypatch)
    led = Ledger(s.data, "live")
    led.append("entry", w.wid, {"mode": "live", "coin": "xyz:XYZ100", "name": "XYZ100", "side": "卖", "sent": True,
                                "sz": 0.0033, "px": 30010, "resp": {"status": "ok"}, "t_ms": 1})
    led.append("exit", "2026-10-02|NVDA", {"coin": "xyz:NVDA", "name": "NVDA", "n_fills": 1, "pnl_usd": -1.5})
    fc.pos = -0.0033
    fc.fill_list = CLOSE_FILLS
    assert autotrade.cancel_unfilled(s, w)["kind"] == "cancel"
    assert autotrade.exit_(s, w)["pnl_usd"] == pytest.approx(0.16)
    assert autotrade.realized_total(s) == pytest.approx(0.16 - 1.5)
    assert autotrade.summary(s)["weekends"][w.wid]["entry"].get("exch_status") is None
