"""真实成交价周期权回测：选行权价、铁鹰到期盈亏（翼封顶）、手续费封顶、提前止盈、无前视、网络被拦。
全部用确定性夹具（假成交），不碰网络。夹具只用于测试，报告会标明"测试夹具"。"""
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from desk import config
from desk import options_real_backtest as rb
from desk.options_paper import bs_price

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
EXP = datetime(2024, 3, 8, 8, 0, tzinfo=UTC)         # 周五
ENTRY = EXP - timedelta(days=7)
S0 = 60_000.0
IV = 0.55


@pytest.fixture
def s(tmp_path):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    return config.load(tmp_path)


def inst(K, cp, exp=EXP):
    return f"BTC-{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}-{int(K)}-{cp}"


def trade(ts_dt, name, usd, index=S0, iv=IV * 100, amount=1.0, tid=None):
    return {"ts": rb.ms(ts_dt), "instrument": name, "price": usd / index, "index": index, "iv": iv,
            "amount": amount, "trade_id": tid or f"{name}@{rb.ms(ts_dt)}@{usd}"}


def chain_trades(at=ENTRY, spot=S0, strikes=range(48_000, 72_001, 1_000)):
    T = (EXP - at).total_seconds() / (365 * 86400)
    out = []
    for K in strikes:
        for cp in ("C", "P"):
            if (cp == "C" and K < spot - 2000) or (cp == "P" and K > spot + 2000):
                continue
            px = bs_price(spot, K, T, IV, cp)
            if px < 5:
                continue
            out.append(trade(at - timedelta(minutes=5), inst(K, cp), px, index=spot))
    return out


class Fixture:
    """确定性夹具数据源（测试专用）。"""
    is_fixture = True
    blocked = False
    block_msg = ""

    def __init__(self, windows: dict, insts: dict | None = None):
        self.windows, self.insts = windows, insts or {}

    def label(self):
        return "测试夹具"

    def window(self, a, b):
        allt = [t for ts in self.windows.values() for t in ts]
        got = [t for t in allt if a <= t["ts"] <= b]
        return got or None

    def instrument(self, name, a, b):
        return [t for t in self.insts.get(name, []) if a <= t["ts"] <= b]

    def candle_close(self, t):
        return None


# ---------------------------------------------------------------- 合约名 / 日程
def test_parse_instrument_and_expiries():
    assert rb.parse_instrument("BTC-6JAN23-16500-C") == (datetime(2023, 1, 6, 8, tzinfo=UTC), 16500.0, "C")
    assert rb.parse_instrument("BTC-29DEC23-40000-P")[2] == "P"
    assert rb.parse_instrument("BTC-PERPETUAL") is None
    xs = rb.weekly_expiries(date(2024, 3, 1), date(2024, 3, 31))
    assert [x.day for x in xs] == [1, 8, 15, 22, 29] and all(x.weekday() == 4 and x.hour == 8 for x in xs)


def test_event_weeks_cpi_fomc():
    # 2024-03-12 CPI 落在 3/8 → 3/15 那周；3/20 FOMC 在 3/15 → 3/22
    assert rb.event_kinds_between(datetime(2024, 3, 8, 8, tzinfo=UTC), datetime(2024, 3, 15, 8, tzinfo=UTC)) == ["CPI"]
    assert rb.event_kinds_between(datetime(2024, 3, 15, 8, tzinfo=UTC), datetime(2024, 3, 22, 8, tzinfo=UTC)) == ["FOMC"]
    assert rb.event_kinds_between(datetime(2024, 3, 22, 8, tzinfo=UTC), datetime(2024, 3, 29, 8, tzinfo=UTC)) == []


# ---------------------------------------------------------------- 选行权价
def test_strike_selection_from_trades():
    tr = chain_trades()
    # 另一个到期日的成交不能混进来
    tr.append(trade(ENTRY, inst(60_000, "C", EXP + timedelta(days=7)), 9_999))
    # 同一合约两笔，VWAP 按数量加权
    tr.append(trade(ENTRY - timedelta(minutes=10), inst(60_000, "C"), 3_000, amount=3.0))
    q = rb.quotes_at(tr, EXP, S0, ENTRY)
    assert all(rb.parse_instrument(v["inst"])[0] == EXP for v in q.values())
    first = bs_price(S0, 60_000, 7 / 365, IV, "C")
    assert q[(60_000, "C")]["usd"] == pytest.approx((first + 3 * 3_000) / 4)
    sel = rb.select_strikes(q, S0)
    assert sel["atm"] == 60_000
    for cp in ("C", "P"):
        for d in rb.TARGET_DELTAS:
            K = sel[(cp, d)]
            assert abs(abs(q[(K, cp)]["delta"]) - d) <= rb.DELTA_TOL * d
            assert (K > S0) if cp == "C" else (K < S0)
    assert sel[("C", 0.05)] > sel[("C", 0.10)] > sel[("C", 0.15)] > S0
    assert sel[("P", 0.05)] < sel[("P", 0.10)] < sel[("P", 0.15)] < S0


def test_no_trade_no_strike():
    """只在有成交的行权价里选；没有 put 成交 → 没有平值，平值结构跳过。"""
    tr = [t for t in chain_trades() if not t["instrument"].endswith("-P")]
    q = rb.quotes_at(tr, EXP, S0, ENTRY)
    sel = rb.select_strikes(q, S0)
    assert "atm" not in sel and ("P", 0.15) not in sel
    assert rb.build_legs("iron_fly_10", sel, q) is None and rb.build_legs("long_straddle", sel, q) is None


# ---------------------------------------------------------------- 费用 / 盈亏
def test_fee_cap():
    # 便宜期权：0.03% × 60000 = 18 美元 > 12.5% × 40 = 5 → 封顶 5
    assert rb.leg_cost(40.0, S0, 0.0) == pytest.approx(5.0)
    # 贵期权：不封顶 = 18；加 5% 滑点 = 18 + 0.05 × 2000
    assert rb.leg_cost(2000.0, S0, 0.0) == pytest.approx(18.0)
    assert rb.leg_cost(2000.0, S0, 5.0) == pytest.approx(18.0 + 100.0)


def _condor():
    return [{"side": -1, "cp": "C", "K": 64_000, "price": 600.0, "inst": "c1"},
            {"side": -1, "cp": "P", "K": 56_000, "price": 500.0, "inst": "p1"},
            {"side": 1, "cp": "C", "K": 67_000, "price": 150.0, "inst": "c2"},
            {"side": 1, "cp": "P", "K": 53_000, "price": 120.0, "inst": "p2"}]


def test_condor_expiry_pnl_wing_cap():
    legs, slip = _condor(), 5.0
    e = rb.structure_entry(legs, S0, slip, "iron_condor_15_05")
    assert e["credit"] == pytest.approx(600 + 500 - 150 - 120)
    # 区间内到期：全部权利金 − 进场费
    assert rb.expiry_pnl(legs, S0, 60_000, slip, "iron_condor_15_05") == pytest.approx(e["credit"] - e["cost_in"])
    # 暴涨/暴跌：亏损被翼封顶在 翼宽 − 权利金 − 费用，不会超过 max_risk
    for ST in (100_000, 120_000, 20_000):
        p = rb.expiry_pnl(legs, S0, ST, slip, "iron_condor_15_05")
        assert p >= -e["max_risk"] - 1e-9
    p_hi = rb.expiry_pnl(legs, S0, 100_000, slip, "iron_condor_15_05")
    p_vhi = rb.expiry_pnl(legs, S0, 120_000, slip, "iron_condor_15_05")
    gross_hi = -(3_000 - e["credit"])
    assert gross_hi - e["cost_in"] - 2 * 0.00015 * 100_000 == pytest.approx(p_hi)
    assert p_vhi < p_hi + 1e-9 and p_vhi >= -e["max_risk"]


def test_long_straddle_risk_is_debit():
    legs = [{"side": 1, "cp": "C", "K": 60_000, "price": 1_500.0, "inst": "c"},
            {"side": 1, "cp": "P", "K": 60_000, "price": 1_400.0, "inst": "p"}]
    e = rb.structure_entry(legs, S0, 5.0, "long_straddle")
    assert e["max_risk"] == pytest.approx(2_900 + e["cost_in"])
    assert rb.expiry_pnl(legs, S0, 60_000, 5.0, "long_straddle") == pytest.approx(-e["max_risk"])


# ---------------------------------------------------------------- 提前止盈 / 无前视
def _decay_trades(at_hours: dict[int, float]):
    """短腿每个时点的价格（占入场价比例）；翼不成交。"""
    out = {}
    for name, p0 in (("c1", 600.0), ("p1", 500.0)):
        out[name] = [trade(ENTRY + timedelta(hours=h, minutes=-10), name, p0 * frac) for h, frac in at_hours.items()]
    return out


def test_early_exit_triggers_at_50pct():
    legs = _condor()
    credit = 830.0
    # 短腿 +2h 跌到 80%，+5h 跌到 30% → 浮盈（翼按 0 卖回，亏 270）= 1100×0.7 − 270 = 500 ≥ 415 → +5h 平
    lt = _decay_trades({2: 0.8, 5: 0.3})
    ee = rb.early_exit(legs, lt, ENTRY, EXP, S0, 5.0, "iron_condor_15_05")
    assert ee is not None and ee["at"] == rb.ms(ENTRY + timedelta(hours=5))
    gross = 1100 * 0.7 - 270
    cost_out = rb.leg_cost(180.0, S0, 5.0) + rb.leg_cost(150.0, S0, 5.0)
    e = rb.structure_entry(legs, S0, 5.0, "iron_condor_15_05")
    assert gross >= 0.5 * credit
    assert ee["pnl"] == pytest.approx(gross - e["cost_in"] - cost_out)


def test_early_exit_ignores_entry_window_and_future():
    legs = _condor()
    # 入场窗口里（入场前 20 分钟）的低价成交不能触发
    lt = {"c1": [trade(ENTRY - timedelta(minutes=20), "c1", 1.0)], "p1": [trade(ENTRY - timedelta(minutes=20), "p1", 1.0)]}
    assert rb.early_exit(legs, lt, ENTRY, EXP, S0, 5.0, "iron_condor_15_05") is None
    # 在 +5h 触发；加上 +5h 之后的成交（未来）不改变触发时刻和盈亏
    base = _decay_trades({2: 0.8, 5: 0.3})
    a = rb.early_exit(legs, base, ENTRY, EXP, S0, 5.0, "iron_condor_15_05")
    fut = {k: v + [trade(ENTRY + timedelta(hours=5, minutes=30), k, 0.01), trade(EXP - timedelta(hours=3), k, 9_999.0)]
           for k, v in base.items()}
    b = rb.early_exit(legs, fut, ENTRY, EXP, S0, 5.0, "iron_condor_15_05")
    assert a == b
    # 超过 2 小时的成交不用：call 只在 +50 分钟成交、put 只在 +4h50 成交 → 两条短腿从来不同时"新鲜" → 不触发
    stale = {"c1": [trade(ENTRY + timedelta(minutes=50), "c1", 60.0)],
             "p1": [trade(ENTRY + timedelta(hours=4, minutes=50), "p1", 50.0)]}
    assert rb.early_exit(legs, stale, ENTRY, EXP, S0, 5.0, "iron_condor_15_05") is None


def test_settlement_uses_only_pre_expiry_index_and_end_to_end(s):
    wins = {"entry": chain_trades()}
    # 到期前 30 分钟的成交（另一到期日）指数价 = 61,000；到期后的成交指数价 = 99,999（不能用）
    nxt = EXP + timedelta(days=7)
    wins["settle"] = [trade(EXP - timedelta(minutes=m), inst(61_000, "C", nxt), 2_000, index=61_000) for m in (5, 15, 25)]
    wins["after"] = [trade(EXP + timedelta(minutes=10), inst(61_000, "C", nxt), 2_000, index=99_999)]
    src = Fixture(wins)
    w, why = rb.load_week(src, EXP, "FRI7", None)
    assert why == "" and w.settle == pytest.approx(61_000) and w.spot == pytest.approx(S0)
    rows = rb.week_rows(w, 5.0)
    strats = {r["strategy"] for r in rows}
    assert {"iron_condor_15_05", "iron_fly_10", "long_straddle", "short_strangle_15_REF"} <= strats
    for r in rows:
        if r["strategy"] != "short_strangle_15_REF":
            assert r["r"] >= -1.0 - 1e-9
    md, all_rows = rb.run(s, src, "BTC", date(2024, 3, 1), date(2024, 3, 8))
    assert md.startswith("# ⚠ 测试夹具") and "不是真实成交" in md
    assert (Path(s.data) / "reports" / "options_real_backtest_BTC_FIXTURE.md").exists()
    assert {r["slippage_pct"] for r in all_rows} == {2.0, 5.0, 10.0}


# ---------------------------------------------------------------- 统计
def test_risk_fraction_and_ruin():
    rs = [0.1] * 20 + [-1.0]
    f = rb.risk_fraction(rs)
    assert 0.19 <= f <= 0.2 and rb.compound_dd(rs, f) <= 0.2 < rb.compound_dd(rs, f + 0.005)
    assert rb.max_drawdown_additive([1, -0.5, -0.5, 2]) == pytest.approx(1.0)
    assert rb.p_ruin([0.01] * 10, 1.0) == 0.0
    assert rb.p_ruin([-1.0], 0.6) == 1.0


# ---------------------------------------------------------------- 网络被拦
def test_blocked_network_exits_2(s, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("Proxy CONNECT tunnel failed, response 403")
    monkeypatch.setattr(rb, "_paged", boom)
    monkeypatch.setattr(config, "load", lambda *a, **k: s)
    assert rb.main(["--since", "2024-03-01", "--until", "2024-03-15"]) == 2
    out = capsys.readouterr().out
    assert "bot 服务器" in out and "不会用合成数据" in out
    assert not list((Path(s.data) / "reports").glob("options_real_backtest_*")) if (Path(s.data) / "reports").exists() else True


def test_entry_quote_uses_no_post_entry_trades():
    """review 2026-10-18：入场报价只用入场前的成交；入场后的成交（哪怕价格离谱）不改变行权价、价格、指数。"""
    base = chain_trades()
    settle = [trade(EXP - timedelta(minutes=5), inst(61_000, "C", EXP + timedelta(days=7)), 2_000, index=61_000)]
    src_a = Fixture({"entry": base, "settle": settle})
    post = [trade(ENTRY + timedelta(minutes=m), t["instrument"], 1.0, index=99_999) for m in (1, 15, 29) for t in base[:5]]
    src_b = Fixture({"entry": base, "settle": settle, "post": post})
    wa, _ = rb.load_week(src_a, EXP, "FRI7", None)
    wb, _ = rb.load_week(src_b, EXP, "FRI7", None)
    assert wa.spot == wb.spot and wa.sel == wb.sel
    assert {k: v["usd"] for k, v in wa.quotes.items()} == {k: v["usd"] for k, v in wb.quotes.items()}
