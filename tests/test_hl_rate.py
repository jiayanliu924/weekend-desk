import sys, types
import pytest
from desk import hl_rate as hr


class Clk:
    def __init__(self): self.t = 1000.0; self.slept = []
    def now(self): return self.t
    def sleep(self, s): self.slept.append(s); self.t += s


def mk():
    c = Clk()
    return hr.RateLimiter(900, clock=c.now, sleep=c.sleep), c


def test_bucket_math():
    lim, c = mk()
    for _ in range(45):
        lim.acquire(20)
    assert c.slept == [] and lim.tokens == 0
    lim.acquire(20)                      # 需等 20/15 秒
    assert c.slept and abs(c.slept[0] - 20 / 15) < 1e-6


def test_priority_borrows_but_respects_backoff():
    lim, c = mk()
    lim.tokens = 0
    lim.acquire(20, priority=True)
    assert c.slept == [] and lim.tokens == -20
    lim.note_429(5)
    lim.acquire(1, priority=True)
    assert sum(c.slept) >= 5


def test_429_retry_then_success_and_giveup():
    lim, c = mk()
    n = {"k": 0}

    def f():
        n["k"] += 1
        if n["k"] < 3:
            raise RuntimeError("429 too many")
        return "ok"
    assert lim.call(f, 1) == "ok" and n["k"] == 3 and sum(c.slept) >= 2
    n["k"] = -99
    with pytest.raises(RuntimeError):
        lim.call(f, 1)
    assert n["k"] == -96


def test_weights():
    assert hr.payload_weight("/info", {"type": "l2Book"}) == (2.0, False)
    assert hr.payload_weight("/info", {"type": "openOrders"})[0] == 20
    assert hr.payload_weight("/exchange", {"action": {"type": "cancel", "cancels": [1, 2]}}) == (2.0, True)
    assert hr.payload_weight("/exchange", {"action": {"type": "order", "orders": [{"r": False}]}}) == (1.0, False)
    assert hr.payload_weight("/exchange", {"action": {"type": "order", "orders": [{"r": True}]}})[1] is True


class FakeInfo:
    def __init__(self, *a, **k): self.calls = []
    def post(self, p, payload=None): self.calls.append(payload); return "r"


class FakeEx(FakeInfo):
    def __init__(self, *a, **k): super().__init__()


@pytest.fixture
def fake_sdk(monkeypatch):
    mods = {
        "hyperliquid": types.ModuleType("hyperliquid"),
        "hyperliquid.exchange": types.SimpleNamespace(Exchange=FakeEx),
        "hyperliquid.info": types.SimpleNamespace(Info=FakeInfo),
        "hyperliquid.utils": types.SimpleNamespace(constants=types.SimpleNamespace(MAINNET_API_URL="x")),
    }
    mods["eth_account"] = types.SimpleNamespace(Account=types.SimpleNamespace(from_key=lambda k: object()))
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)


def test_all_three_clients_route_through_limiter(fake_sdk, monkeypatch):
    from desk import autotrade, pairs_exec, reopen_exec
    seen = []
    monkeypatch.setattr(hr.LIMITER, "acquire", lambda w, priority=False: seen.append((w, priority)))
    key = "0x" + "11" * 32
    addr = "0x" + "22" * 20
    for cl in (autotrade.Client(addr, key), pairs_exec.PairsClient(addr, key, []), reopen_exec.ReopenClient(addr, key)):
        seen.clear()
        assert cl.info.post("/info", {"type": "l2Book"}) == "r"
        cl.ex.post("/exchange", {"action": {"type": "cancel", "cancels": [1]}})
        assert seen == [(2.0, False), (1.0, True)]
