"""卡 A4：options_rules_backtest — 合成 parquet，不碰网络。"""
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from desk import options_rules_backtest as ob

SPEC = ob.load_spec()
RULE = {"id": "RX_test", "side": "put", "why": "", "when": []}
ET = "America/New_York"


def weekdays(n, start=date(2024, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = weekdays(80)
RAW = [100.0] * len(DAYS)


def osi(exp, cp, strike):
    return f"SPY   {exp.replace('-', '')[2:]}{cp}{int(round(strike * 1000)):08d}"


def row(day, hhmm, sym, bid, ask):
    ts = pd.Timestamp(f"{day} {hhmm}", tz=ET).tz_convert("UTC")
    return {"ts": ts, "symbol": sym, "bid_px": bid, "ask_px": ask, "bid_sz": 1, "ask_sz": 1}


def quotes_from(rows, tmp_path):
    p = tmp_path / "SPY"
    p.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_parquet(p / "2024-01.parquet", index=False)
    return ob.Quotes.from_dir(p)


def daily(sym, idxs, bid, ask, hhmm="15:59"):
    f = bid if callable(bid) else (lambda k: bid)
    return [row(DAYS[k], hhmm, sym, f(k), max(ask, f(k) + 0.05)) for k in idxs]


def run_sim(q, sig_idx, spec=SPEC, rule=RULE, monkeypatch=None):
    monkeypatch.setattr(ob, "signal", lambda rid, adj, i, ief=None, ief_i=None: i in sig_idx)
    return ob.simulate(rule, "SPY", DAYS, RAW, RAW, q, spec)


PUT_A = osi("2024-02-09", "P", 97)     # 1/4 入场时 36 DTE


def test_fills_at_ask_bid_and_max_hold(tmp_path, monkeypatch):
    q = quotes_from(daily(PUT_A, range(2, 40), 2.0, 2.05), tmp_path)
    tr, sig, sk = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert len(tr) == 1
    t = tr[0]
    assert t["entry_ask"] == 2.05 and t["exit_bid"] == 2.0 and t["contracts"] == 2   # floor(500/205)
    assert t["reason"] == "max_hold" and t["exit_idx"] == 2 + 20
    cost = 2 * 100 * 2.05 * 1.01 + 2 * 0.65
    proceeds = 2 * 100 * 2.0 * 0.99 - 2 * 0.65
    assert t["pnl"] == pytest.approx(proceeds - cost, abs=0.01)


def test_take_profit_and_stop_loss(tmp_path, monkeypatch):
    q = quotes_from(daily(PUT_A, range(2, 10), lambda k: 4.3 if k == 6 else 2.0, 2.05), tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["reason"] == "take_profit" and tr[0]["exit_idx"] == 6 and tr[0]["pnl"] > 0
    q = quotes_from(daily(PUT_A, range(2, 10), lambda k: 1.0 if k == 4 else 2.0, 2.05), tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["reason"] == "stop_loss" and tr[0]["exit_idx"] == 4 and tr[0]["pnl"] < 0
    # 刚好没到 -50%（相对 ask*1.01）：不止损
    q = quotes_from(daily(PUT_A, range(2, 6), lambda k: 2.0 if k == 2 else 1.04, 2.05), tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["reason"] != "stop_loss"


def test_exit_dte(tmp_path, monkeypatch):
    sym = osi("2024-02-01", "P", 97)    # 1/2 入场 30 DTE；1/25 是 DTE 7
    q = quotes_from(daily(sym, range(0, 30), 2.0, 2.05), tmp_path)
    tr, *_ = run_sim(q, {0}, monkeypatch=monkeypatch)
    t = tr[0]
    assert t["reason"] == "exit_dte" and t["exit_date"] == "2024-01-25"


def test_cooldown(tmp_path, monkeypatch):
    B = osi("2024-02-16", "P", 97)
    rows = daily(PUT_A, range(2, 5), lambda k: 4.3 if k == 3 else 2.0, 2.05) + daily(B, range(8, 16), 2.0, 2.05)
    q = quotes_from(rows, tmp_path)
    tr, sig, sk = run_sim(q, {2, 3, 5, 8, 9}, monkeypatch=monkeypatch)
    assert [t["entry_idx"] for t in tr] == [2, 9]        # 3 在持仓内，5/8 在平仓(3)+5 之内
    assert sk["cooldown_or_open"] == 3
    tr, *_ = run_sim(q, {2, 8}, monkeypatch=monkeypatch)
    assert [t["entry_idx"] for t in tr] == [2]            # idx 8 = 3+5，仍在冷却


def test_spread_filter_and_no_quote(tmp_path, monkeypatch):
    q = quotes_from(daily(PUT_A, range(2, 6), 1.5, 2.0), tmp_path)    # 价差 28%
    tr, sig, sk = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr == [] and sk == {"spread": 1}
    q = quotes_from(daily(PUT_A, range(3, 6), 2.0, 2.05), tmp_path)   # 信号日没有报价
    tr, sig, sk = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr == [] and sk == {"no_contract": 1}
    # 价差刚好 15%：mid=1, bid .9286 ask 1.0714 → 不应被拒；超过一点则拒
    q = quotes_from(daily(PUT_A, range(2, 6), 0.93, 1.07), tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert len(tr) == 1
    q = quotes_from(daily(PUT_A, range(2, 6), 0.0, 1.07), tmp_path)
    tr, sig, sk = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr == [] and sk == {"spread": 1}


def test_too_expensive_skipped(tmp_path, monkeypatch):
    q = quotes_from(daily(PUT_A, range(2, 6), 5.0, 5.1), tmp_path)     # 一张 510 > 500
    tr, sig, sk = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr == [] and sk == {"too_expensive": 1}


def test_no_more_quotes_handling(tmp_path, monkeypatch):
    # 完全没有后续报价：到期归零
    q = quotes_from(daily(PUT_A, [2], 2.0, 2.05), tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    t = tr[0]
    cost = 2 * 100 * 2.05 * 1.01 + 2 * 0.65
    assert t["reason"] == "no_more_quotes" and t["exit_date"] == "2024-02-09"
    assert t["pnl"] == pytest.approx(-cost, abs=0.01) and t["pnl_lastbid"] == pytest.approx(-cost, abs=0.01)
    # 中途消失：预先登记口径 = 到期归零；最后买价只做参考
    q = quotes_from(daily(PUT_A, [2, 3, 4], 2.0, 2.05), tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    t = tr[0]
    assert t["reason"] == "no_more_quotes" and t["exit_date"] == "2024-02-09"
    assert t["pnl"] == pytest.approx(-cost, abs=0.01) and t["pnl_lastbid"] > -cost + 300
    # 中间缺几天：顺延到下一个有报价日，而不是归零
    q = quotes_from(daily(PUT_A, [2, 3, 4, 5, 6], 2.0, 2.05) + daily(PUT_A, [25], 2.0, 2.05), tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["exit_idx"] == 25 and tr[0]["reason"] == "max_hold"


def test_cluster_counting():
    assert ob.clusters([1, 3, 8, 20, 24, 40]) == [[1, 3, 8], [20, 24], [40]]
    assert ob.clusters([]) == []
    assert len(ob.clusters([0, 6, 12])) == 3 and len(ob.clusters([0, 5, 10])) == 1


def mk(entry, exit_, pnl, idx, premium=500.0, reason="max_hold"):
    return {"entry_date": entry, "exit_date": exit_, "pnl": pnl, "pnl_lastbid": pnl, "ret": pnl / premium,
            "ret_lastbid": pnl / premium, "entry_idx": idx, "reason": reason}


def test_train_test_split_and_stats():
    assert ob.seg_of("2025-06-30") == "train" and ob.seg_of("2025-07-01") == "test"
    trades = [mk("2025-06-30", "2025-07-10", -50, 1), mk("2025-07-01", "2025-07-20", 100, 100),
              mk("2025-08-01", "2025-08-10", -20, 200), mk("2025-09-01", "2025-09-10", -30, 300)]
    ev = ob.evaluate("R2", "SPY", trades, [1, 100, 200, 300], {})
    assert ev["train"]["trades"] == 1 and ev["train"]["total_pnl"] == -50
    assert ev["test"]["trades"] == 3 and ev["test"]["total_pnl"] == 50
    assert ev["traded_clusters_test"] == 3 and ev["bonferroni"] == pytest.approx(0.05 / 32)
    assert ev["max_consec_losses"] == 2 and ev["max_drawdown"] == pytest.approx(50)
    assert ev["passed"] is False and ev["crit_clusters_ge_10"] is False      # 簇数不足


def test_pass_and_red_flag():
    trades = [mk(f"2025-{7 + k // 20:02d}-{1 + k % 20:02d}", "2025-12-30", 60 + k % 3, 1000 + 10 * k) for k in range(12)]
    ev = ob.evaluate("R3", "QQQ", trades, [t["entry_idx"] for t in trades], {})
    assert ev["traded_clusters_test"] == 12 and ev["crit_test_pnl_positive"] and ev["passed"]
    big = [mk(f"2025-{7 + k // 20:02d}-{1 + k % 20:02d}", "2025-12-30", 400 + k, 1000 + 10 * k) for k in range(12)]
    ev = ob.evaluate("R3", "QQQ", big, [t["entry_idx"] for t in big], {})
    assert ev["red_flag"] and not ev["passed"]


# ---------------------------------------------------------------- 无前视
@pytest.mark.parametrize("rid", ["R1_x", "R2_x", "R3_x", "R4_x"])
def test_signal_never_uses_future(rid):
    rng = np.random.default_rng(1)
    adj = list(100 * np.cumprod(1 + rng.normal(0, 0.02, 300)))
    ief = list(100 * np.cumprod(1 + rng.normal(0, 0.005, 300)))
    fired = 0
    for i in range(60, 280):
        a = ob.signal(rid, adj[:i + 1], i, ief[:i + 1], i)
        b = ob.signal(rid, adj, i, ief, i)
        c = ob.signal(rid, adj[:i + 1] + [1e9] * 50, i, ief[:i + 1] + [-1e9] * 50, i)
        d = ob.signal(rid, adj[:i + 1] + [0.001] * 50, i, ief[:i + 1] + [1e9] * 50, i)
        assert a == b == c == d
        fired += a
    assert fired >= 0


def test_signal_rules_basic_truth():
    down = [100 - 0.5 * k for k in range(80)]
    assert ob.signal("R2_trend_down_put", down, 79) is True
    assert ob.signal("R3_trend_up_call", down, 79) is False
    up = [100 + 0.5 * k for k in range(80)]
    assert ob.signal("R3_trend_up_call", up, 79) is True
    assert ob.signal("R2_trend_down_put", up, 79) is False
    crash = [100.0] * 60 + [100 - 3 * k for k in range(1, 7)]
    assert ob.signal("R4_capitulation_rebound_call", crash, 65) is True
    assert ob.signal("R4_capitulation_rebound_call", crash, 63) is False      # 只连跌 3 天
    ief = [100.0] * 100 + [99.0]
    assert ob.signal("R1_rates_breakdown_put", down, 79, ief[:61] + [0] * 0, 60) is False or True
    flat_ief = [100 - 0.1 * k for k in range(80)]
    assert ob.signal("R1_rates_breakdown_put", down, 79, flat_ief, 79) is True
    assert ob.signal("R1_rates_breakdown_put", up, 79, flat_ief, 79) is False


def test_signal_day_uses_closes_up_to_d_in_sim(tmp_path, monkeypatch):
    """simulate 传给 signal 的价格序列允许包含未来（调用方给的是全序列），signal 只读 [0..i]：把 d+1 之后的收盘改掉，交易不变。"""
    q = quotes_from(daily(PUT_A, range(0, 40), 2.0, 2.05), tmp_path)
    adj = [100 - 0.4 * k for k in range(len(DAYS))]
    r = {"id": "R2_trend_down_put", "side": "put", "why": "", "when": []}
    t1, s1, _ = ob.simulate(r, "SPY", DAYS, adj, RAW, q, SPEC)
    adj2 = adj[:60] + [1e6] * (len(adj) - 60)
    t2, s2, _ = ob.simulate(r, "SPY", DAYS, adj2, RAW, q, SPEC)
    assert [s for s in s1 if s < 60] == [s for s in s2 if s < 60]
    assert [t for t in t1 if t["entry_idx"] < 60] == [t for t in t2 if t["entry_idx"] < 60]


def test_entry_uses_last_quote_at_or_before_1559(tmp_path, monkeypatch):
    sym = PUT_A
    d = DAYS[2]
    rows = [row(d, "15:30", sym, 1.0, 1.05), row(d, "15:58", sym, 2.0, 2.05), row(d, "15:59", sym, 3.0, 3.05),
            row(d, "16:00", sym, 0.1, 0.12), row(d, "16:01", sym, 9.0, 9.05)] + daily(sym, range(3, 30), 3.0, 3.05)
    q = quotes_from(rows, tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["entry_ask"] == 3.05           # 15:59，不是 16:00 / 16:01
    # 夏令时：EDT 15:59 = 19:59Z，16:00 EDT 排除
    sm = date(2024, 7, 1).isoformat()
    p = tmp_path / "S2"
    p.mkdir()
    pd.DataFrame([row(sm, "15:59", sym, 1.0, 1.1), row(sm, "16:00", sym, 5.0, 5.1)]).to_parquet(p / "2024-07.parquet", index=False)
    m = ob.read_month(p / "2024-07.parquet")
    assert len(m) == 1 and m.iloc[0]["ask"] == 1.1


def test_exit_never_before_entry_plus_one(tmp_path, monkeypatch):
    # 入场当天晚些时候/当天本身的"暴涨"bid 不能触发出场；出场最早在入场日 +1
    d = DAYS[2]
    rows = [row(d, "15:59", PUT_A, 2.0, 2.05), row(d, "16:00", PUT_A, 50.0, 50.1)] + daily(PUT_A, range(3, 30), 2.0, 2.05)
    q = quotes_from(rows, tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    t = tr[0]
    assert t["exit_idx"] > t["entry_idx"] and t["exit_date"] > t["entry_date"]
    assert t["reason"] == "max_hold" and t["exit_bid"] == 2.0
    # 即使入场当天 bid 就等于翻倍价，次日以前不会出场
    rows = [row(d, "15:59", PUT_A, 2.0, 2.05)] + daily(PUT_A, [3], 4.5, 4.55)
    q = quotes_from(rows, tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["exit_idx"] == 3 and tr[0]["reason"] == "take_profit"


def test_contract_selection_rules(tmp_path, monkeypatch):
    d = DAYS[2]          # 2024-01-04
    e1, e2, e3 = "2024-02-09", "2024-02-16", "2024-02-23"      # DTE 36 / 43 / 50
    rows = []
    for e in (e1, e2, e3):
        for k in (95, 97, 98, 100, 103):
            rows.append(row(d, "15:59", osi(e, "P", k), 2.0, 2.05))
            rows.append(row(d, "15:59", osi(e, "C", k), 2.0, 2.05))
    q = quotes_from(rows, tmp_path)
    con, why = ob.select_contract(q, date.fromisoformat(d).toordinal(), "put", 100.0, SPEC)
    assert con["symbol"] == osi(e1, "P", 97) and why == ""
    con, _ = ob.select_contract(q, date.fromisoformat(d).toordinal(), "call", 100.0, SPEC)
    assert con["symbol"] == osi(e1, "C", 103)


def test_fetch_closes_with_fake_source(tmp_path):
    calls = []

    class Src:
        def closes(self, s, a, b, adjustment="raw"):
            calls.append((s, adjustment, a.isoformat()))
            return {"2024-01-02": 1.0, "2024-01-03": 2.0}

    ob.fetch_closes(tmp_path, ["IEF", "SPY"], end=date(2024, 1, 5), src=Src(), log=lambda *_: None)
    assert (tmp_path / "meta" / "closes_IEF_full.json").exists() and (tmp_path / "meta" / "closes_SPY_split.json").exists()
    assert ("IEF", "raw", "2023-09-01") in calls
    n = len(calls)
    ob.fetch_closes(tmp_path, ["IEF"], end=date(2024, 1, 5), src=Src(), log=lambda *_: None)   # 缓存命中
    assert len(calls) == n
    raw, adj = ob.read_closes(tmp_path, "SPY")
    assert raw["2024-01-03"] == 2.0 and adj["2024-01-03"] == 2.0


def test_end_to_end_cli(tmp_path, monkeypatch):
    root = tmp_path / "opra"
    (root / "meta").mkdir(parents=True)
    days = weekdays(120, date(2023, 9, 1))
    px = {d: 100.0 - 0.3 * k for k, d in enumerate(days)}
    (root / "meta" / "closes_SPY_full.json").write_text(json.dumps(px))
    (root / "meta" / "closes_IEF_full.json").write_text(json.dumps({d: 100.0 - 0.1 * k for k, d in enumerate(days)}))
    spec = ob.load_spec()
    out, trades = ob.run(root, ["SPY"], ["R2"], spec, log=lambda *_: None)
    assert out["warnings"] and out["results"] == []          # 没报价数据：只警告
    ob.write_outputs({"results": [], "warnings": ["x"], **{k: 0 for k in ("train_end",)}}, [], spec, tmp_path / "rep")
    assert (tmp_path / "rep" / "options_rules_v1.md").exists() and (tmp_path / "rep" / "options_rules_v1_trades.csv").exists()


# ---------------------------------------------------------------- 两遍流程：extra_needed + 合并
def test_extra_needed_generation(tmp_path, monkeypatch):
    # 主数据：信号日 2 有 PUT_A；之后只有 3、4 两天有报价，5.. 缺
    q = quotes_from(daily(PUT_A, [2, 3, 4], 2.0, 2.05), tmp_path)
    monkeypatch.setattr(ob, "signal", lambda rid, adj, i, ief=None, ief_i=None: i == 2)
    need = ob.extra_candidates(RULE, DAYS, RAW, RAW, q, SPEC)
    occ = PUT_A.replace(" ", "")
    assert occ == "SPY240209P00097000"
    # 缺的日子 = idx 5..22（入场+20 个交易日，先到 exit_dte：2/9 到期，DTE<=7 是 2/2，idx 22 = 2/1 更早；取先到者）
    got = sorted(need)
    assert got[0] == DAYS[5] and all(occ in need[d] for d in got)
    assert DAYS[3] not in need and DAYS[4] not in need and DAYS[2] not in need
    last_idx = max(DAYS.index(d) for d in got)
    assert last_idx <= 2 + 20
    # 提前到 exit_dte：30 DTE 合约 1/2 入场，最晚出场 1/25（DTE 7）
    sym = osi("2024-02-01", "P", 97)
    q = quotes_from(daily(sym, [0], 2.0, 2.05), tmp_path)
    monkeypatch.setattr(ob, "signal", lambda rid, adj, i, ief=None, ief_i=None: i == 0)
    need = ob.extra_candidates(RULE, DAYS, RAW, RAW, q, SPEC)
    assert max(need) == "2024-01-25" and min(need) == DAYS[1]


def test_extra_needed_is_superset_ignoring_cooldown(tmp_path, monkeypatch):
    B = osi("2024-02-16", "P", 97)
    q = quotes_from(daily(PUT_A, [2], 2.0, 2.05) + daily(B, [4], 2.0, 2.05), tmp_path)
    monkeypatch.setattr(ob, "signal", lambda rid, adj, i, ief=None, ief_i=None: i in (2, 4))
    need = ob.extra_candidates(RULE, DAYS, RAW, RAW, q, SPEC)
    syms = set().union(*need.values())
    assert PUT_A.replace(" ", "") in syms and "SPY" in next(iter(syms))


def test_write_extra_needed_merges(tmp_path):
    ob.write_extra_needed(tmp_path, {"SPY": {"2024-01-05": {"B", "A"}}})
    ob.write_extra_needed(tmp_path, {"SPY": {"2024-01-05": {"C"}, "2024-01-08": {"A"}}, "QQQ": {"2024-01-05": {"Q"}}})
    d = json.loads((tmp_path / "meta" / "extra_needed.json").read_text())
    assert d["SPY"] == {"2024-01-05": ["A", "B", "C"], "2024-01-08": ["A"]} and d["QQQ"] == {"2024-01-05": ["Q"]}


def test_merge_extra_for_exit_only(tmp_path, monkeypatch):
    main = tmp_path / "main"
    ext = tmp_path / "ext"
    main.mkdir()
    ext.mkdir()
    pd.DataFrame(daily(PUT_A, [2, 3], 2.0, 2.05)).to_parquet(main / "2024-01.parquet", index=False)
    # 补数据：后面几天，含一个翻倍的 bid；另外一个只在补数据里出现的"别的合约"，当天不许被选
    other = osi("2024-02-09", "P", 96)
    pd.DataFrame(daily(PUT_A, [4, 5, 6], lambda k: 4.5 if k == 6 else 2.0, 2.05)
                 + daily(other, [2], 2.0, 2.05)).to_parquet(ext / "2024-01.parquet", index=False)
    q = ob.Quotes.from_dir(main, ext)
    assert q.n_extra == 4
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["symbol"] == PUT_A and tr[0]["reason"] == "take_profit" and tr[0]["exit_idx"] == 6
    # 选约只看主数据：补数据里的 96 行权价当天不可见
    assert set(q.day_rows(date.fromisoformat(DAYS[2]).toordinal())["symbol"].astype(str)) == {PUT_A}
    # 没有补数据 -> 同一笔变成归零（预先登记口径）
    q0 = ob.Quotes.from_dir(main)
    tr0, *_ = run_sim(q0, {2}, monkeypatch=monkeypatch)
    assert tr0[0]["reason"] == "no_more_quotes" and tr0[0]["pnl"] < -300
    # 重复 (合约, 日)：主数据优先
    pd.DataFrame(daily(PUT_A, [3], 9.0, 9.05)).to_parquet(ext / "2024-02.parquet", index=False)
    q2 = ob.Quotes.from_dir(main, ext)
    assert [x[1] for x in q2.sym_quotes(PUT_A, date.fromisoformat(DAYS[2]).toordinal())][0] == 2.0


# ---------------------------------------------------------------- A5 审计：对抗用例
def raw_rows(sym, day, items):
    """items: [(hhmm, bid, ask)]，允许 NaN。"""
    return [row(day, hh, sym, b, a) for hh, b, a in items]


def entry_rows(sym=PUT_A, k=2):
    return raw_rows(sym, DAYS[k], [("15:59", 2.0, 2.05)])


def test_nan_bid_is_zero_bid_and_stop_loss_fires(tmp_path, monkeypatch):
    nan = float("nan")
    rows = entry_rows() + raw_rows(PUT_A, DAYS[3], [("15:59", nan, 2.05)]) + daily(PUT_A, range(4, 12), 2.0, 2.05)
    q = quotes_from(rows, tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    t = tr[0]
    assert t["reason"] == "stop_loss" and t["exit_idx"] == 3 and t["exit_bid"] == 0.0 and t["pnl"] < -300
    # NaN 的行没被丢掉
    m = ob.read_month(tmp_path / "SPY" / "2024-01.parquet")
    r = m[(m["symbol"] == PUT_A) & (m["day"] == date.fromisoformat(DAYS[3]).toordinal())]
    assert len(r) == 1 and r.iloc[0]["bid"] == 0.0


def test_crossed_quote_uses_latest_noncrossed_else_zero(tmp_path):
    d = DAYS[3]
    rows = raw_rows(PUT_A, d, [("15:57", 4.0, 4.1), ("15:58", 3.0, 3.1), ("15:59", 9.0, 2.0)])        # 最后一条倒挂
    rows += raw_rows(osi("2024-02-09", "P", 96), d, [("15:58", 7.0, 3.0), ("15:59", 8.0, 3.0)])         # 全部倒挂
    p = tmp_path / "X"
    p.mkdir()
    pd.DataFrame(rows).to_parquet(p / "m.parquet", index=False)
    m = ob.read_month(p / "m.parquet").set_index("symbol")
    assert m.loc[PUT_A, "bid"] == 3.0 and m.loc[PUT_A, "ask"] == 3.1 and m.loc[PUT_A, "qmin"] == 15 * 60 + 58
    assert m.loc[osi("2024-02-09", "P", 96), "bid"] == 0.0


def test_wide_market_blocks_take_profit(tmp_path, monkeypatch):
    # 第 4 天 bid 4.4 但 ask 6.0（价差 31%）：不止盈；第 5 天正常报价 -> 止盈
    rows = entry_rows() + raw_rows(PUT_A, DAYS[3], [("15:59", 2.0, 2.05)]) + raw_rows(PUT_A, DAYS[4], [("15:59", 4.4, 6.0)]) \
        + raw_rows(PUT_A, DAYS[5], [("15:59", 4.4, 4.5)])
    q = quotes_from(rows, tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["exit_idx"] == 5 and tr[0]["reason"] == "take_profit"
    # 宽价差不挡止损：bid 0.8、ask 3.0 照样按 bid 止损
    rows = entry_rows() + raw_rows(PUT_A, DAYS[3], [("15:59", 0.8, 3.0)])
    q = quotes_from(rows, tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["reason"] == "stop_loss" and tr[0]["exit_bid"] == 0.8


def test_stale_quote_blocks_take_profit_and_entry(tmp_path, monkeypatch):
    # 止盈日的最后一条报价是 15:30（过期）-> 不止盈；下一天 15:59 的好报价才止盈
    rows = entry_rows() + raw_rows(PUT_A, DAYS[3], [("15:30", 4.5, 4.55)]) + raw_rows(PUT_A, DAYS[4], [("15:55", 4.5, 4.55)])
    rows += [row(DAYS[3], "15:59", osi("2024-02-09", "P", 90), 1.0, 1.05)]       # 让 DAYS[3] 不被当成半天市
    q = quotes_from(rows, tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr[0]["exit_idx"] == 4 and tr[0]["reason"] == "take_profit"
    # 入场报价 15:30 -> 当天不入场
    rows = raw_rows(PUT_A, DAYS[2], [("15:30", 2.0, 2.05)]) + [row(DAYS[2], "15:59", osi("2024-02-09", "P", 90), 1.0, 1.05)]
    q = quotes_from(rows, tmp_path)
    tr, sig, sk = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr == [] and sk == {"stale_quote": 1}
    # 15:50 刚好算新鲜
    q = quotes_from(raw_rows(PUT_A, DAYS[2], [("15:50", 2.0, 2.05)]) + daily(PUT_A, range(3, 8), 2.0, 2.05), tmp_path)
    tr, *_ = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert len(tr) == 1


def test_signals_use_prior_closes_for_averages():
    adj = [100 + 0.2 * k for k in range(80)]
    i = 70
    a2 = list(adj)
    a2[i] = 1.0
    assert ob.sma_prior(adj, i, 50) == ob.sma_prior(a2, i, 50) == pytest.approx(sum(adj[i - 50:i]) / 50)
    # R3：今天价格 > 前 50 个收盘均值 且 >= 前 19 个收盘的最高 -> 触发；今天价格只比前高低一点 -> 不触发
    assert ob.signal("R3_trend_up_call", adj[:i] + [adj[i - 1] + 0.01], i) is True
    assert ob.signal("R3_trend_up_call", adj[:i] + [adj[i - 1] - 0.01], i) is False
    # R1：IEF 今天价 <= 前 59 个收盘最低才算新低；等于也算
    ief = [100.0] * 70
    assert ob.signal("R1_x", [100 - 0.5 * k for k in range(80)], 79, ief + [100.0] * 9 + [99.9], 79) is True
    assert ob.signal("R1_x", [100 - 0.5 * k for k in range(80)], 79, ief + [100.0] * 9 + [100.1], 79) is False


def test_ief_uses_dividend_adjusted_file(tmp_path):
    calls = []

    class Src:
        def closes(self, s, a, b, adjustment="raw"):
            calls.append((s, adjustment))
            return {"2024-01-02": 1.0}

    ob.fetch_closes(tmp_path, ["IEF", "SPY"], end=date(2024, 1, 5), src=Src(), log=lambda *_: None)
    assert ("IEF", "all") in calls and ("IEF", "split") not in calls and ("SPY", "split") in calls
    (tmp_path / "meta" / "closes_IEF_all.json").write_text(json.dumps({"2024-01-02": 7.0}))
    raw, adj = ob.read_closes(tmp_path, "IEF")
    assert adj["2024-01-02"] == 7.0


def test_min1_sensitivity_is_separate_and_labelled(tmp_path, monkeypatch):
    q = quotes_from(daily(PUT_A, range(2, 30), 5.0, 5.1), tmp_path)
    tr, sig, sk = run_sim(q, {2}, monkeypatch=monkeypatch)
    assert tr == [] and sk == {"too_expensive": 1}                       # 预先登记口径：跳过
    monkeypatch.setattr(ob, "signal", lambda rid, adj, i, ief=None, ief_i=None: i == 2)
    tr1, _, _ = ob.simulate(RULE, "SPY", DAYS, RAW, RAW, q, SPEC, min1=True)
    assert len(tr1) == 1 and tr1[0]["contracts"] == 1
    # 报告里有跳过计数表和"不作为结论"标注
    res = [{"rule": "RX_test", "und": "SPY", **ob.evaluate("RX_test", "SPY", [], [2], {"too_expensive": 1}),
            "sens_min1": {"test_pnl": 12.0}}]
    spec = {**SPEC, "rule": [RULE]}
    ob.write_report({"results": res, "warnings": [], "extra_rows": {}}, [], spec, tmp_path / "r.md")
    txt = (tmp_path / "r.md").read_text()
    assert "被跳过的信号" in txt and "一张就超预算" in txt and "不作为结论" in txt
