from desk import autotrade, reopen_preflight as pf, reopen_exec as rx


class FakeInfo:
    def post(self, path, body):
        if body["type"] == "metaAndAssetCtxs":
            names = ["SP500", "XYZ100", "GOLD"]
            return [{"universe": [{"name": f"xyz:{n}"} for n in names]},
                    [{"oraclePx": "100", "midPx": "100.1", "markPx": "100", "openInterest": "5"} for _ in names]]
        if body["type"] == "userFees":
            return {"userAddRate": "0.00003"}
        raise AssertionError(body)


class Fake(rx.ReopenClient):
    def __init__(self):
        self.info, self.address, self.placed, self.cancelled = FakeInfo(), "0xabc", [], []

    def balance(self):
        return {"available": 5000.0}

    def sz_decimals(self, coin):
        return 2

    def book(self, coin):
        return {"bid": 100.0, "ask": 100.2, "bid_depth_usd": 1e5, "ask_depth_usd": 1e5}

    def set_leverage(self, coin, lev):
        return {"status": "ok"}

    def alo(self, coin, is_buy, sz, px, ro, cloid):
        self.placed.append((coin, sz, px))
        return {"oid": 7, "filled_sz": 0.0, "error": None}

    def cancel(self, coin, oid, cloid=None):
        self.cancelled.append(oid)

    def order_info(self, oid):
        return {"status": "canceled"}


def test_preflight_flags_missing_and_runs_order_test(monkeypatch, capsys):
    f = Fake()
    monkeypatch.setattr(autotrade, "_client_factory", lambda: f)
    monkeypatch.setattr(rx, "get", lambda s: {"mode": "live", "halt_entries": False})
    monkeypatch.setattr(pf.rs, "read_ctx", lambda *a: [])
    rc = pf.main(["--order-test"])
    out = capsys.readouterr().out
    assert "交易所上没有" in out and rc == 1            # 真实配置里有不在假名单里的币
    assert f.placed and f.placed[0][2] <= 80.0 and f.cancelled == [7]
    assert "撤单成功" in out


def test_checks_returns_failures_without_printing(tmp_path, monkeypatch, capsys):
    from desk import config
    s = config.load()
    f = Fake()
    out = pf.checks(s, f)
    assert capsys.readouterr().out == ""
    assert isinstance(out, list)
    info = {}
    out = pf.checks(s, f, info=info, names=["SP500", "NOSUCH"])
    assert any("交易所上没有" in x for x in out) and info["missing"] == ["NOSUCH"]
    f.balance = lambda: {"available": 10.0}
    assert any("余额" in x for x in pf.checks(s, f, names=["SP500"]))
