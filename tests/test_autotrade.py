"""自动下单：关闭/演习/真下单、撤单、平仓算盈亏、亏损上限、风控否决、网页设置。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from desk import autotrade
from desk.clock import weekend_for_friday
from tests.test_web import env  # noqa: F401


class FakeClient:
    def __init__(self):
        self.orders, self.cancels, self.closed, self.pos = [], [], [], 0.0
        self.open = []
        self.fill_list = []

    def balance(self):
        return {"main": 100.0, "xyz": 250.0, "withdrawable_xyz": 250.0}

    def sz_decimals(self, coin):
        return 4

    def position(self, coin):
        return self.pos

    def set_leverage(self, coin, lev):
        return {"ok": True}

    def post_only(self, coin, is_buy, sz, px):
        self.orders.append((coin, is_buy, sz, px))
        self.open.append({"coin": coin, "oid": 7})
        return {"status": "ok"}

    def open_orders(self, coin):
        return [o for o in self.open if o["coin"] == coin]

    def cancel(self, coin, oid):
        self.cancels.append(oid)
        self.open = []

    def close(self, coin):
        self.closed.append(coin)
        self.pos = 0
        return {"status": "ok"}

    def fills(self, coin, since):
        return self.fill_list


@pytest.fixture
def fake(monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(autotrade, "_client_factory", lambda: fc)
    return fc


def _lock(side=-1, void=False):
    return {"action": {"side": side, "reason": "噪音周末，反向", "entry_px": 30000.0},
            "features": {"best_bid": 29990.0, "best_ask": 30010.0, "bid_depth_usd": 1e6, "ask_depth_usd": 1e6, "dec_mid": 30000.0},
            "void": void, "void_reasons": ["x"] if void else [], "name": "XYZ100", "coin": "xyz:XYZ100"}


def test_off_does_nothing(env, fake):  # noqa: F811
    s, _ = env
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))
    assert autotrade.entry(s, w, _lock()) is None and not fake.orders


def test_shadow_records_but_does_not_send(env, fake):  # noqa: F811
    s, _ = env
    autotrade.put(s, mode="shadow", capital_usd=700)
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))
    rec = autotrade.entry(s, w, _lock())
    assert rec["sent"] is False and rec["notional_usd"] == 100 and not fake.orders


def test_live_full_cycle(env, fake):  # noqa: F811
    s, _ = env
    autotrade.put(s, mode="live", capital_usd=700, max_loss_usd=50)
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))
    rec = autotrade.entry(s, w, _lock(side=-1))
    coin, is_buy, sz, px = fake.orders[0]
    assert coin == "xyz:XYZ100" and is_buy is False and px == 30010 and sz == pytest.approx(0.0033)   # $100 / 30010
    assert autotrade.entry(s, w, _lock()) is None            # only once per weekend
    fake.pos = -0.0033
    c = autotrade.cancel_unfilled(s, w)
    assert c["cancelled"] == 1
    fake.fill_list = [{"coin": "xyz:XYZ100", "closedPnl": "0", "fee": "0.01"}, {"coin": "xyz:XYZ100", "closedPnl": "0.20", "fee": "0.03"}]
    x = autotrade.exit_(s, w)
    assert fake.closed == ["xyz:XYZ100"] and x["pnl_usd"] == pytest.approx(0.16)
    assert autotrade.realized_total(s) == pytest.approx(0.16)


def test_skips_void_and_loss_limit(env, fake):  # noqa: F811
    s, _ = env
    autotrade.put(s, mode="live", capital_usd=700, max_loss_usd=5)
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))
    assert "作废" in autotrade.entry(s, w, _lock(void=True))["skipped"]
    autotrade._led(s).append("exit", "2026-10-02|NVDA", {"pnl_usd": -6.0})
    w2 = weekend_for_friday(date(2026, 10, 9), s, s.inst("NVDA"))
    assert "上限" in autotrade.entry(s, w2, {**_lock(), "name": "NVDA", "coin": "xyz:NVDA"})["skipped"]
    assert autotrade.get(s)["mode"] == "off" and not fake.orders


def test_rounding():
    assert autotrade.round_px(30012.345, 4) == 30012
    assert autotrade.round_px(101.537, 2) == 101.54
    assert autotrade.round_sz(0.0033333, 4) == 0.0033


def test_settings_pages(env, fake, monkeypatch):  # noqa: F811
    s, web = env
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS", raising=False)
    monkeypatch.delenv("HL_API_PRIVATE_KEY", raising=False)
    (s.root / ".env").write_text("X=1\n")
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    assert "自动下单（真钱）" in c.get("/settings").text
    assert "先完成第一步" in c.post("/settings/live", data={"mode": "shadow", "capital": 200, "max_loss": 50}).text
    assert "42 位" in c.post("/settings/wallet", data={"address": "0x123", "key": ""}).text
    r = c.post("/settings/wallet", data={"address": "0x" + "a" * 40, "key": "0x" + "b" * 64})
    assert "已保存并连上" in r.text and "$250.00" in r.text
    assert "bbbbbbbb" not in c.get("/settings").text
    assert "勾选" in c.post("/settings/live", data={"mode": "live", "capital": 300, "max_loss": 40}).text
    r = c.post("/settings/live", data={"mode": "live", "capital": 300, "max_loss": 40, "ack": "1"})
    assert "已打开真下单" in r.text and autotrade.get(s)["mode"] == "live"
