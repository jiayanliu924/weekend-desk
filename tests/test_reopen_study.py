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


def _utc(ns):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ns / NS, timezone.utc)


def test_dst_nominal_times():
    # 2026-10-29 EDT: 18:00 ET = 22:00Z, 09:30 ET = 13:30Z；2026-11-02 EST: 23:00Z / 14:30Z
    a = _utc(rs.nominal_ns(date(2026, 10, 29), "SP500", kind="daily_18"))
    b = _utc(rs.nominal_ns(date(2026, 11, 2), "SP500", kind="daily_18"))
    assert (a.hour, a.minute) == (22, 0) and (b.hour, b.minute) == (23, 0)
    c = _utc(rs.nominal_ns(date(2026, 10, 29), "SP500", kind="idx_cash_open"))
    d = _utc(rs.nominal_ns(date(2026, 11, 2), "XYZ100", kind="idx_cash_open"))
    assert (c.hour, c.minute) == (13, 30) and (d.hour, d.minute) == (14, 30)


def test_gen_events():
    ev = rs.gen_events("all", date(2026, 10, 25), date(2026, 11, 1))  # Sun..Sun
    kinds = {k: [(d, n) for kk, d, n in ev if kk == k] for k in rs.KINDS}
    assert {d for d, _ in kinds["daily_18"]} == {date(2026, 10, d) for d in (26, 27, 28, 29)}
    assert {d.weekday() for d, _ in kinds["idx_cash_open"]} == {0, 1, 2, 3, 4}
    assert {d for d, _ in kinds["weekly"]} == {date(2026, 10, 25), date(2026, 11, 1)}
    assert all(k == "weekly" for k, _, _ in rs.gen_events("weekly", date(2026, 10, 25), date(2026, 11, 1)))


def test_verdict_group_by_week():
    # 周一、周二同一 ISO 周：合计为正 -> 1 周全正；按 weekend(日期)分组则 1/2 为负，不触发 >half
    rows = [dict(_row("2026-10-26", net=-3), date="2026-10-26", kind="daily_18"),
            dict(_row("2026-10-27", net=10), date="2026-10-27", kind="daily_18")]
    assert rs.verdict(rows, group="week")[0] == "WORTH_SHADOW" or rs.verdict(rows, group="week")[0] == "INCONCLUSIVE"
    assert "为负" not in rs.verdict(rows, group="week")[1]
    # 三周中两周为负 -> REJECT 且文案为"周"
    neg = [dict(_row("2026-10-12", net=-5), date="2026-10-12"), dict(_row("2026-10-19", net=-5), date="2026-10-19"),
           dict(_row("2026-10-26", net=30), date="2026-10-26")]
    v, why = rs.verdict(neg, group="week")
    assert v == "REJECT" and "2/3 个周净值为负" in why
    # 同一周内两天各为负仍算 1 周
    same = [dict(_row("2026-10-26", net=-1), date="2026-10-26"), dict(_row("2026-10-27", net=-1), date="2026-10-27"),
            dict(_row("2026-10-12", net=50), date="2026-10-12")]
    assert "为负" not in rs.verdict(same, group="week")[1]


def test_entry_no_lookahead():
    # oracle 先停在 100.2（T0），到 T0+100s 才跳到 101(=ref)。mid=100.25：相对当时 oracle 只 +5bps，
    # 相对未来 ref 是 -74bps。旧代码按 ref 判 S<0 过冲就"买"进场（用了未来信息）；正确：k=10 不触发。
    t0 = NOM + 120 * NS
    ctx = []
    for s in range(-700, 3700):
        orc = 100.0 if s < 0 else (100.2 if s < 100 else 101.0)
        mid = 100.0 if s < 0 else 100.25
        ctx.append((t0 + s * NS, orc, mid, 1000.0))
    book = [(t0 - 10 * NS, 100.2, 100.3)]
    trades = [(t0 + 2 * NS, 100.1, 1.0, "A"), (t0 + 3 * NS, 100.4, 1.0, "B")]
    rows = rs.analyze_event("SP500", "2026-10-11", t0, True, ctx, book, trades)
    assert all(r["filled"] == 0 for r in rows if r["k"] >= 10)


def test_ctrl_not_collapsed_across_kinds():
    # 同币同日 daily_18 和 idx_cash_open 的对照(b)应分别计入；旧 key (coin, weekend) 会互相覆盖
    rows = [dict(_row("2026-10-26", ctrl=8.0), kind="daily_18", date="2026-10-26"),
            dict(_row("2026-10-26", ctrl=8.0), kind="idx_cash_open", date="2026-10-26")]
    v, why = rs.verdict(rows, group="week")
    assert v == "INCONCLUSIVE" and "对照(b) 16.0" in why


def test_asia_nominal_utc():
    # 2026-10-28 (Wed)：KST/JST=UTC+9，HKT/CST=UTC+8
    hm = lambda k: (lambda t: (t.hour, t.minute))(_utc(rs.nominal_ns(date(2026, 10, 28), "X", kind=k)))
    assert hm("kr_open") == (0, 1) and hm("kr_pm") == (7, 5)
    assert hm("jp_open") == (0, 0) and hm("jp_pm") == (3, 30)
    assert hm("hk_open") == (1, 30) and hm("hk_pm") == (5, 0)
    assert hm("cn_open") == (1, 30) and hm("cn_pm") == (5, 0)


def test_asia_gen_events_weekdays_and_empty():
    ev = rs.gen_events("asia", date(2026, 10, 24), date(2026, 11, 1), {"kr": ["SMSN"], "jp": [], "hk": [], "cn": []})
    assert {k for k, _, _ in ev} == {"kr_open", "kr_pm"} and {d.weekday() for _, d, _ in ev} == {0, 1, 2, 3, 4}
    assert rs.gen_events("asia", date(2026, 10, 26), date(2026, 10, 30), {m: [] for m in rs.ASIA}) == []
    assert all(k in rs.KINDS for k in rs.ASIA_KINDS) and len(rs.ASIA_KINDS) == 8
