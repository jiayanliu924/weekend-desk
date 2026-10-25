from datetime import date

from desk import reopen_study as rs

NS = rs.NS
NOM = rs.nominal_ns(date(2026, 10, 11), "SP500")  # 22:00 UTC (EDT)


def make_ctx(t0, jump=1.0):
    """1s 样本：T0 前 oracle=100，T0 起 oracle=101(+1%)，mid 先过冲到 101.5 再回到 ref=101。"""
    ctx = []
    for s in range(-700, 3700):
        t = t0 + s * NS
        orc = 100.0 if s < 0 else 100.0 * (1 + jump / 100)
        mid = orc if s < 0 else (101.5 if s < 30 else 101.0)
        ctx.append((t, orc, mid, 1000.0))
    return ctx


def test_detect_t0():
    t0 = NOM + 120 * NS
    ctx = make_ctx(t0)
    got, ok = rs.detect_t0(ctx, NOM)
    assert ok and got == t0
    flat = [(NOM + s * NS, 100.0, 100.0, 1.0) for s in range(-100, 100)]
    got, ok = rs.detect_t0(flat, NOM)
    assert not ok and got == NOM + NS


def test_detect_t0_ewma_rejected():
    # 逐步爬升：每步 4bps，前 10 分钟中位步长也是 4bps -> 不算跳变
    ctx = [(NOM + s * NS, 100.0 * (1 + 4e-4) ** (s + 300), 100.0, 1.0) for s in range(-300, 300)]
    got, ok = rs.detect_t0(ctx, NOM)
    assert not ok


def test_pnl_sign_and_fees(monkeypatch):
    t0 = NOM + 120 * NS
    ctx = make_ctx(t0)
    book = [(t0 - 10 * NS, 101.4, 101.5)]
    # S>0：卖在 ask=101.5；有成交 px 101.6 > 101.5 -> 入场成交；随后 px 100.9 < ref(101) -> maker 退出
    trades = [(t0 + 2 * NS, 101.6, 1.0, "B"), (t0 + 50 * NS, 100.9, 1.0, "A")]
    rows = rs.analyze_event("SP500", "2026-10-11", t0, True, ctx, book, trades)
    r = next(x for x in rows if x["k"] == 10)
    assert r["filled"] == 1
    gross = -(101.0 / 101.5 - 1) * 1e4
    assert r["gross_bps"] > 0
    assert abs(r["net_bps_main"] - (gross - 6)) < 1e-6
    assert abs(r["net_usd_main"] - (gross - 6) / 1e4 * 5000) < 1e-9
    # 无穿价成交 -> 不成交；退出走 taker：mid(fill+600)=101 -> 费用 3+9
    r2 = rs.analyze_event("SP500", "w", t0, True, ctx, book, [(t0 + 2 * NS, 101.6, 1.0, "B")])
    x = next(x for x in r2 if x["k"] == 10)
    assert x["filled"] == 1 and abs(x["net_bps_main"] - (gross - 12)) < 1e-6
    r3 = rs.analyze_event("SP500", "w", t0, True, ctx, book, [])
    assert all(x["filled"] == 0 for x in r3)


def _row(w, g=0.02, os_=30, liq=1, net=10.0, filled=1, ctrl=-5.0, k=10):
    return dict(coin="A", weekend=w, G=g, max_overshoot_bps=os_, liq_proxy=liq, k=k, filled=filled,
                net_bps_main=net if filled else "", ctrl_b_net_bps=ctrl)


def test_verdict():
    good = [_row("w1", net=10), _row("w2", net=8), _row("w3", net=9)]
    assert rs.verdict(good)[0] == "WORTH_SHADOW"
    assert rs.verdict([_row("w1", net=-1), _row("w2", net=-2)])[0] == "REJECT"
    assert rs.verdict([_row("w1", os_=5), _row("w2", os_=5)])[0] == "REJECT"
    assert rs.verdict([_row("w1", filled=0)] * 5 + [_row("w2")])[0] == "REJECT"  # 成交率 <20%
    # 去掉最佳单笔后为负 -> 不是 WORTH_SHADOW
    assert rs.verdict([_row("w1", net=20), _row("w2", net=-5), _row("w3", net=1)])[0] == "INCONCLUSIVE"
    assert rs.verdict([])[0] == "INCONCLUSIVE"
