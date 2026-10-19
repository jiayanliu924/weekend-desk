import json
from types import SimpleNamespace

import pytest

from desk import account_stop, autotrade, pairs_exec


class S(dict):
    def __init__(self, data, lim=500.0):
        super().__init__(account={"max_loss_usd": lim})
        self.data = str(data)


@pytest.fixture
def env(tmp_path, monkeypatch):
    pushes, puts = [], []
    monkeypatch.setattr(account_stop.notify, "push", lambda *a, **k: pushes.append(a) or True)
    monkeypatch.setattr(autotrade, "put", lambda s, **kw: puts.append(("weekend", kw)))
    monkeypatch.setattr(pairs_exec, "put", lambda s, **kw: puts.append(("pairs", kw)))
    return S(tmp_path), pushes, puts


def _pnl(monkeypatch, weekend, pairs_list):
    monkeypatch.setattr(autotrade, "realized_total", lambda s: weekend)
    monkeypatch.setattr(pairs_exec, "load_state", lambda s: {"pairs": {str(i): {"closed": {"pnl_usd": v}} for i, v in enumerate(pairs_list)}})


def test_no_trip_above_limit(env, monkeypatch):
    s, pushes, puts = env
    _pnl(monkeypatch, -300.0, [-100.0, -50.0])
    c = account_stop.check(s)
    assert c["total_usd"] == -450.0 and not c["tripped"] and not pushes and not puts


def test_trip_at_limit_turns_everything_off_once(env, monkeypatch):
    s, pushes, puts = env
    _pnl(monkeypatch, -400.0, [-100.0])
    assert account_stop.check(s)["tripped"]
    assert ("weekend", {"mode": "off", "updated_by": "account_stop"}) in puts
    assert ("pairs", {"mode": "off", "updated_by": "account_stop"}) in puts
    assert len(pushes) == 1
    account_stop.check(s)
    assert len(pushes) == 1 and account_stop.tripped(s)


def test_read_error_does_not_trip(env, monkeypatch):
    s, pushes, puts = env
    monkeypatch.setattr(autotrade, "realized_total", lambda s: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(pairs_exec, "load_state", lambda s: {"pairs": {}})
    c = account_stop.check(s)
    assert not c["tripped"] and c["errors"] and not puts


def test_open_pair_without_close_not_counted(env, monkeypatch):
    s, _, _ = env
    monkeypatch.setattr(autotrade, "realized_total", lambda s: 0.0)
    monkeypatch.setattr(pairs_exec, "load_state", lambda s: {"pairs": {"a": {"closed": None}, "b": {"closed": {"pnl_usd": -3.0}}}})
    assert account_stop.components(s)["total_usd"] == -3.0


def test_entry_blocked_when_tripped(env, monkeypatch):
    s, _, _ = env
    _pnl(monkeypatch, -600.0, [])
    monkeypatch.setattr(autotrade, "get", lambda st: {"mode": "live", "capital_usd": 5000, "max_loss_usd": 20})
    w = SimpleNamespace(wid="w", name="NVDA", coin="xyz:NVDA")
    assert autotrade.entry(s, w, {"action": {"side": 1}}) is None


def test_default_limit_when_section_missing(tmp_path):
    s = SimpleNamespace(data=str(tmp_path))
    s.__getitem__ = None
    class T(dict):
        data = str(tmp_path)
    assert account_stop.limit(T()) == 20.0


def test_reset_sets_baseline_and_does_not_retrip(env, monkeypatch):
    s, pushes, _ = env
    _pnl(monkeypatch, -600.0, [])
    assert account_stop.check(s)["tripped"]
    from desk import config
    monkeypatch.setattr(config, "load", lambda: s)
    account_stop.main(["--reset"])
    c = account_stop.check(s)
    assert not c["tripped"] and c["total_usd"] == 0.0
    _pnl(monkeypatch, -1050.0, [])          # 新增亏 450：不触发
    assert not account_stop.check(s)["tripped"]
    _pnl(monkeypatch, -1100.0, [])          # 新增亏 500：触发
    assert account_stop.check(s)["tripped"]


def test_read_error_pushes_hourly(env, monkeypatch):
    s, pushes, _ = env
    monkeypatch.setattr(autotrade, "realized_total", lambda s: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(pairs_exec, "load_state", lambda s: {"pairs": {}})
    account_stop.check(s)
    account_stop.check(s)
    assert len(pushes) == 1


def test_reset_aborts_when_unreadable(env, monkeypatch):
    s, _, _ = env
    _pnl(monkeypatch, -600.0, [])
    account_stop.check(s)
    from desk import config
    monkeypatch.setattr(config, "load", lambda: s)
    monkeypatch.setattr(autotrade, "realized_total", lambda s: (_ for _ in ()).throw(OSError("x")))
    assert account_stop.main(["--reset"]) == 1
    assert account_stop.tripped(s)


def test_kill_check_tripped_without_positions_does_not_connect(env, monkeypatch):
    s, _, _ = env
    _pnl(monkeypatch, -600.0, [])
    account_stop.check(s)
    monkeypatch.setattr(autotrade, "get", lambda st: {"mode": "off", "max_loss_usd": 20})
    monkeypatch.setattr(autotrade, "_led", lambda st: SimpleNamespace(all=lambda: []))
    monkeypatch.setattr(autotrade, "client", lambda: (_ for _ in ()).throw(AssertionError("connected")))
    assert autotrade.kill_check(s) is None
