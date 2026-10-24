"""卡 L1 对 desk/options_v2_backtest.py 的小改动（默认行为不变、新参数可选）的测试；夹具沿用 test_options_v2_backtest.py 的合成数据。"""
from datetime import date

import pytest

from desk import options_v2_backtest as v2
from tests.test_options_v2_backtest import (DAYS, EXP, SPEC, STRAT, always, mkctx, osi, qrows, quotes_from, spread_rows,
                                            write_q)


def test_scan_entries_date_range_and_signal_fn(tmp_path):
    q = quotes_from(spread_rows(range(0, 31)), tmp_path)
    ctx = mkctx(q)
    s = always("F1_put_spread")
    full = [c["i"] for c in v2.scan_entries(s, ctx)]
    assert full[0] == 0 and len(full) > 20
    rng = [c["i"] for c in v2.scan_entries(s, ctx, first=DAYS[3], last=DAYS[9])]
    assert rng and rng[0] == 3 and rng[-1] <= 9
    calls = []

    def sig(strat, c, i):
        calls.append(i)
        return None if i == 4 else (i % 2 == 0)
    got = v2.scan_entries(s, ctx, first=DAYS[3], last=DAYS[7], signal_fn=sig)
    assert calls == [3, 4, 5, 6, 7]                                       # last 之后的日子根本不看
    assert [(c["i"], c["why"]) for c in got if c["legs"] is None] == [(4, "no_signal_data")]
    assert [c["i"] for c in got if c["legs"] is not None] == [6]


def test_scan_entries_earnings_branch_applies_signal_fn(tmp_path):
    exp = "2024-01-26"
    rows = []
    for k in (6, 7):
        rows += qrows(osi(exp, "C", 100), [k], 3.0, 3.05) + qrows(osi(exp, "P", 100), [k], 2.9, 2.95)
    q = quotes_from(rows, tmp_path)
    ctx = mkctx(q)
    s = STRAT["F4c_earnings_straddle_control"]
    ev, _ = v2.earnings_entries([{"date": "2024-01-10", "timing": "amc", "source": "x"}], DAYS)
    assert len(v2.scan_entries(s, ctx, ev, signal_fn=lambda *a: True)) == 1
    assert v2.scan_entries(s, ctx, ev, signal_fn=lambda *a: False) == []
    assert v2.scan_entries(s, ctx, ev, signal_fn=lambda *a: None)[0]["why"] == "no_signal_data"
    assert len(v2.scan_entries(s, ctx, ev)) == 1                              # 不给 signal_fn：B2 行为


def test_load_quotes_last_day_does_not_open_later_months(tmp_path):
    root = tmp_path / "opra"
    write_q(spread_rows(range(0, 20)), root / "cbbo1m" / "SPY", "2024-01.parquet")
    (root / "cbbo1m" / "SPY" / "2024-03.parquet").write_bytes(b"not a parquet file")        # 一打开就会炸
    (root / "cbbo1m_extra_v2" / "SPY").mkdir(parents=True)
    (root / "cbbo1m_extra_v2" / "SPY" / "2024-04.parquet").write_bytes(b"junk")
    q = v2.load_quotes(root, "SPY", last_day="2024-02-15")
    assert q is not None and q.last_day <= date(2024, 2, 15).toordinal()
    cut = v2.load_quotes(root, "SPY", last_day=DAYS[9])                       # 同一个月内的截止日：晚于它的行被丢掉
    assert cut.last_day == date.fromisoformat(DAYS[9]).toordinal()
    with pytest.raises(Exception):
        v2.load_quotes(root, "SPY")                                             # 不给截止日 = 全部打开（B2 行为）


def test_iv_at_30_equals_iv30_and_term_points(tmp_path):
    o = date.fromisoformat(DAYS[2]).toordinal()
    rows = []
    for ex, sig in (("2024-02-02", 0.20), ("2024-02-09", 0.24), ("2024-03-01", 0.30), ("2024-03-08", 0.32)):
        T = (date.fromisoformat(ex).toordinal() - o) / 365
        for cp in ("C", "P"):
            p = v2.bs_price(100.0, 100.0, T, 0.045, sig, cp)
            rows += qrows(osi(ex, cp, 100), [2], p - 0.01, p + 0.01)
    q = quotes_from(rows, tmp_path)
    assert v2.iv_at(q, o, 100.0, SPEC, 30)[0] == v2.iv30(q, o, 100.0, SPEC)
    iv55, d55 = v2.iv_at(q, o, 100.0, SPEC, 55)
    assert d55 == (59, 66) or len(d55) == 1                                   # 3/1 = 57 天、3/8 = 64 天（从 1/4 起），55 落在最近的两个之外时取近的
    assert iv55 > v2.iv30(q, o, 100.0, SPEC)                                  # 远月 IV 更高


def test_expiry_window_from_strategy_and_debit_vertical(tmp_path):
    """strat['expiry'] 覆盖 toml [expiry]；net_debit_ok 的借方价差（买 P95、卖 P90）不再因 no_credit 被跳过，走借方出场规则。"""
    q = quotes_from(spread_rows(), tmp_path)
    s = dict(STRAT["F3_cheap_straddle"], entry="always", legs=[{"side": "buy", "type": "put", "strike_pct": 0.95}, {"side": "sell", "type": "put", "strike_pct": 0.90}])
    ctx = mkctx(q)
    cands = [c for c in v2.scan_entries(s, ctx) if c["i"] == 2]
    assert cands[0]["legs"]
    t, why = v2.build_trade(s, ctx, cands[0], SPEC)
    assert t is None and why == "no_credit"                                   # 默认：有卖腿 = 必须收到权利金
    s["net_debit_ok"] = True
    t, why = v2.build_trade(s, ctx, cands[0], SPEC)
    assert t is not None and t["kind"] == "debit" and t["net_ps"] < 0
    assert t["max_loss"] == pytest.approx(-t["net_ps"] * 100, abs=0.01)
    e = date.fromisoformat(EXP).toordinal()
    d0 = date.fromisoformat(DAYS[2]).toordinal()                              # 入场日离 EXP 36 天
    s2 = dict(STRAT["F1_put_spread"], expiry={"min_dte": 14, "max_dte": 20, "target_dte": 16})
    assert v2.pick_expiry([e], d0, SPEC, s2) is None
    s3 = dict(STRAT["F1_put_spread"], expiry={"min_dte": 30, "max_dte": 40, "target_dte": 36})
    assert v2.pick_expiry([e], d0, SPEC, s3) == e
    assert v2.pick_expiry([e], d0, SPEC, STRAT["F1_put_spread"]) == e         # 没有 expiry 字段：照 toml（30–45）
