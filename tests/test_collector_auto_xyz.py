from desk import config
from desk.collector import Collector


def _u(name, delisted=False):
    return {"name": name, "szDecimals": 2, "maxLeverage": 10, **({"isDelisted": True} if delisted else {})}


def _c(vlm):
    return {"markPx": "10", "openInterest": "100", "dayNtlVlm": str(vlm), "funding": "0"}


def test_pick_auto_coins_filter_cap_and_existing(tmp_path):
    s = config.load()
    existing = s.instruments[0]["coin"]
    names = [existing, "xyz:NVDA", "xyz:SILVER", "xyz:GOLD", "xyz:OLDX", "xyz:LOWV", "xyz:ZZZUNKNOWN"]
    vlms = [9e9, 5e6, 4e6, 3e6, 9e6, 10, 9e6]
    uni = [_u(n, delisted=(n == "xyz:OLDX")) for n in names]
    payload = [{"universe": uni}, [_c(v) for v in vlms]]
    col = Collector(s)
    col.auto_xyz_min_vlm, col.auto_xyz_max = 50000, 40
    got = col.pick_auto_coins(payload)
    assert existing not in got and "xyz:OLDX" not in got and "xyz:LOWV" not in got
    assert "xyz:ZZZUNKNOWN" not in got
    assert got == ["xyz:SILVER", "xyz:GOLD"]      # volume-desc
    col.auto_xyz_max = 1
    assert col.pick_auto_coins(payload) == ["xyz:SILVER"]
    assert col.coins[0] == existing and set(s_coins := col.coins) >= {i["coin"] for i in s.instruments}
