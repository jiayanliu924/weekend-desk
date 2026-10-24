"""卡 L1：options_lab — 全部用合成数据（沙箱没有数据商网络）。夹具写法沿用 tests/test_options_v2_backtest.py：合成 parquet + 合成收盘。"""
import copy
import json
import math
import os
import shutil
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from desk import options_lab as lab
from desk import options_v2_backtest as v2

ET = "America/New_York"
SPEC = v2.load_spec()


# ---------------------------------------------------------------- 合成数据
def weekdays_between(a: str, b: str) -> list[str]:
    d, end, out = date.fromisoformat(a), date.fromisoformat(b), []
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


ALL_DAYS = weekdays_between("2023-10-02", "2025-09-30")
FRIDAYS = [d for d in weekdays_between("2023-10-01", "2025-12-31") if date.fromisoformat(d).weekday() == 4]
STRIKES = [85, 90, 95, 100, 105, 110, 115]


def osi(exp, cp, strike, root="SPY"):
    return f"{root:<6}{exp.replace('-', '')[2:]}{cp}{int(round(strike * 1000)):08d}"


def build_data(root: Path, unds=("SPY",), mutate=None, q_end="2025-09-30", sigma_fn=None):
    """root/meta/closes_<U>_full.json + root/cbbo1m/<U>/<YYYY-MM>.parquet。现价 100 ± 1.5 缓慢摆动，所以 0.95 / 0.90 总落在 95 / 90 上；
    BS 报价（半价差 0.02，总价差 0.04 <= 0.05，永远过流动性线），到期日 = 每个周五、离到期 5–62 天（含 <14 天，免得出场要补数据）。
    mutate(day, row) -> row 或 None（删掉）：给"改金库段报价"类测试用。"""
    (root / "meta").mkdir(parents=True, exist_ok=True)
    sigma_fn = sigma_fn or (lambda k: 0.20 + 0.04 * math.sin(k / 13))
    for ui, u in enumerate(unds):
        closes = {d: round(100 + 1.5 * math.sin(k / 9 + ui), 4) for k, d in enumerate(ALL_DAYS)}
        (root / "meta" / f"closes_{u}_full.json").write_text(json.dumps(closes))
        by_month: dict[str, list] = {}
        for k, d in enumerate(ALL_DAYS):
            if d < "2024-01-02" or d > q_end:
                continue
            S = closes[d]
            sg = sigma_fn(k)
            ts = pd.Timestamp(f"{d} 15:59", tz=ET).tz_convert("UTC")
            o = date.fromisoformat(d).toordinal()
            for e in FRIDAYS:
                dte = date.fromisoformat(e).toordinal() - o
                if not 5 <= dte <= 62:
                    continue
                for K in STRIKES:
                    for cp in ("C", "P"):
                        p = v2.bs_price(S, K, dte / 365, 0.045, sg + 0.0006 * (dte - 30), cp)
                        row = {"ts": ts, "symbol": osi(e, cp, K, u), "bid_px": max(p - 0.02, 0.0), "ask_px": p + 0.02, "bid_sz": 1, "ask_sz": 1}
                        if mutate:
                            row = mutate(d, row)
                            if row is None:
                                continue
                        by_month.setdefault(d[:7], []).append(row)
        for m, rows in by_month.items():
            dd = root / "cbbo1m" / u
            dd.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_parquet(dd / f"{m}.parquet", index=False)


@pytest.fixture(scope="module")
def shared_opra(tmp_path_factory):
    root = tmp_path_factory.mktemp("shared") / "opra"
    build_data(root)
    return root


def make_env(tmp_path, opra: Path) -> lab.LabEnv:
    d = tmp_path / "data"
    d.mkdir()
    os.symlink(opra, d / "opra")                       # 数据共享、日志账本各测试自己一份
    return lab.LabEnv.at(d)


@pytest.fixture
def env(tmp_path, shared_opra):
    return make_env(tmp_path, shared_opra)


def put_spread(**kw):
    p = {"why": "sell rich put premium, capped (Cboe PUT idea; R1 note)", "underlyings": ["SPY"], "dte": 38,
         "legs": [{"side": "sell", "type": "put", "strike_pct": 0.95}, {"side": "buy", "type": "put", "strike_pct": 0.90}]}
    p.update(kw)
    return p


def condor(**kw):
    p = {"why": "condor on rich IV", "underlyings": ["SPY"], "dte": 38, "entry": [{"name": "iv_rv_ge", "value": 1.2}],
         "legs": [{"side": "sell", "type": "put", "strike_pct": 0.95}, {"side": "buy", "type": "put", "strike_pct": 0.90},
                  {"side": "sell", "type": "call", "strike_pct": 1.05}, {"side": "buy", "type": "call", "strike_pct": 1.10}]}
    p.update(kw)
    return p


# ---------------------------------------------------------------- 校验：接受
@pytest.mark.parametrize("name,p,structure,kind", [
    ("put spread", put_spread(), "vertical", "credit"),
    ("condor", condor(), "iron_condor", "credit"),
    ("iron fly earnings", {"why": "x", "underlyings": ["NVDA"], "dte": 20, "entry": {"earnings_eve": True},
                           "legs": [{"side": "sell", "type": "call", "strike_pct": 1.0}, {"side": "sell", "type": "put", "strike_pct": 1.0},
                                    {"side": "buy", "type": "call", "strike_pct": 1.08}, {"side": "buy", "type": "put", "strike_pct": 0.92}]}, "iron_butterfly", "credit"),
    ("long straddle", {"why": "x", "underlyings": "all", "dte": 38, "entry": [{"name": "iv_rv_le", "value": 0.8}],
                       "legs": [{"side": "buy", "type": "call", "strike_pct": 1.0}, {"side": "buy", "type": "put", "strike_pct": 1.0}]}, "straddle", "debit"),
    ("long strangle", {"why": "x", "underlyings": ["QQQ"], "dte": 45, "legs": [{"side": "buy", "type": "call", "strike_pct": 1.05}, {"side": "buy", "type": "put", "strike_pct": 0.95}]}, "strangle", "debit"),
    ("long call", {"why": "x", "underlyings": ["SPY"], "dte": 30, "entry": [{"name": "trend_R3", "value": True}], "legs": [{"side": "buy", "type": "call", "strike_pct": 1.0}]}, "single", "debit"),
    ("bull call debit spread", {"why": "x", "underlyings": ["SPY"], "dte": 30, "legs": [{"side": "buy", "type": "call", "strike_pct": 1.0}, {"side": "sell", "type": "call", "strike_pct": 1.05}]}, "vertical", "debit"),
    ("bear call credit spread", {"why": "x", "underlyings": ["SPY"], "dte": 30, "legs": [{"side": "sell", "type": "call", "strike_pct": 1.0}, {"side": "buy", "type": "call", "strike_pct": 1.05}]}, "vertical", "credit"),
])
def test_validate_accepts(name, p, structure, kind):
    norm, why = lab.validate_proposal(p)
    assert norm is not None, why
    assert why == "" and norm["structure"] == structure and norm["kind"] == kind
    assert len(norm["legs"]) <= 4 and len(norm["conditions"]) <= 3


# ---------------------------------------------------------------- 校验：拒绝
@pytest.mark.parametrize("label,mut,needle", [
    ("naked short put", lambda p: p.update(legs=[{"side": "sell", "type": "put", "strike_pct": 0.95}]), "naked_short"),
    ("naked short call leg in condor", lambda p: p.update(legs=[{"side": "sell", "type": "put", "strike_pct": 0.95}, {"side": "buy", "type": "put", "strike_pct": 0.9},
                                                               {"side": "sell", "type": "call", "strike_pct": 1.05}, {"side": "sell", "type": "call", "strike_pct": 1.08}]), "naked_short"),
    ("short straddle", lambda p: p.update(legs=[{"side": "sell", "type": "call", "strike_pct": 1.0}, {"side": "sell", "type": "put", "strike_pct": 1.0}]), "naked_short"),
    ("sold put, bought call", lambda p: p.update(legs=[{"side": "sell", "type": "put", "strike_pct": 0.95}, {"side": "buy", "type": "call", "strike_pct": 1.05}]), "naked_short"),
    ("5 legs", lambda p: p.update(legs=p["legs"] + [{"side": "buy", "type": "call", "strike_pct": 1.0}] * 3), "max 4"),
    ("3 legs (butterfly)", lambda p: p.update(legs=[{"side": "buy", "type": "put", "strike_pct": 0.9}, {"side": "sell", "type": "put", "strike_pct": 0.95}, {"side": "buy", "type": "put", "strike_pct": 1.0}]), "structure_not_allowed"),
    ("reverse condor", lambda p: p.update(legs=[{"side": "buy", "type": "put", "strike_pct": 0.95}, {"side": "sell", "type": "put", "strike_pct": 0.90},
                                                {"side": "buy", "type": "call", "strike_pct": 1.05}, {"side": "sell", "type": "call", "strike_pct": 1.10}]), "structure_not_allowed"),
    ("strike too low", lambda p: p.update(legs=[{"side": "buy", "type": "put", "strike_pct": 0.85}]), "outside"),
    ("strike too high", lambda p: p.update(legs=[{"side": "buy", "type": "call", "strike_pct": 1.15}]), "outside"),
    ("strike off 0.01 grid", lambda p: p.update(legs=[{"side": "buy", "type": "call", "strike_pct": 1.0237}]), "0.01 grid"),
    ("dte too short", lambda p: p.update(dte=10), "dte"),
    ("dte too long", lambda p: p.update(dte=61), "dte"),
    ("exit_dte below 7", lambda p: p.update(exit_dte=5), "exit_dte"),
    ("exit_dte too high for dte", lambda p: p.update(dte=20, exit_dte=10), "exit_dte"),
    ("4 conditions", lambda p: p.update(entry=[{"name": "iv_rv_ge", "value": 1.0}, {"name": "rv20_ge", "value": 0.1}, {"name": "weekday_eq", "value": 1}, {"name": "iv_pct_ge", "value": 0.5}]), "max 3"),
    ("condition not allowed", lambda p: p.update(entry=[{"name": "rsi_lt", "value": 30}]), "not allowed"),
    ("off-grid threshold", lambda p: p.update(entry=[{"name": "iv_rv_ge", "value": 1.25}]), "off the allowed grid"),
    ("off-grid slope", lambda p: p.update(entry=[{"name": "term_slope_ge", "value": 0.03}]), "off the allowed grid"),
    ("weekday 5", lambda p: p.update(entry=[{"name": "weekday_eq", "value": 5}]), "off the allowed grid"),
    ("bool threshold on grid cond", lambda p: p.update(entry=[{"name": "iv_rv_ge", "value": True}]), "off the allowed grid"),
    ("value on earnings_eve", lambda p: p.update(underlyings=["NVDA"], entry=[{"name": "earnings_eve", "value": 1.0}]), "no threshold"),
    ("duplicate condition", lambda p: p.update(entry=[{"name": "iv_rv_ge", "value": 1.0}, {"name": "iv_rv_ge", "value": 1.2}]), "twice"),
    ("empty range", lambda p: p.update(entry=[{"name": "iv_rv_ge", "value": 1.5}, {"name": "iv_rv_le", "value": 0.8}]), "empty range"),
    ("underlying outside universe", lambda p: p.update(underlyings=["TSLA"]), "not in the v2 universe"),
    ("empty underlyings", lambda p: p.update(underlyings=[]), "underlyings"),
    ("earnings_eve on SPY", lambda p: p.update(entry=[{"name": "earnings_eve", "value": True}]), "earnings_eve only works"),
    ("unknown field", lambda p: p.update(leverage=3), "unknown_field"),
    ("missing why", lambda p: p.pop("why"), "why"),
    ("blank why", lambda p: p.update(why="  "), "why"),
    ("tp off grid", lambda p: p.update(take_profit=0.6), "take_profit"),
    ("sl off grid", lambda p: p.update(stop_loss=5.0), "stop_loss"),
    ("max_hold off grid", lambda p: p.update(max_hold_days=30), "max_hold_days"),
    ("cooldown off grid", lambda p: p.update(cooldown_days=3), "cooldown_days"),
    ("leg with extra key", lambda p: p.update(legs=[{"side": "buy", "type": "call", "strike_pct": 1.0, "qty": 5}]), "exactly"),
    ("duplicate legs", lambda p: p.update(legs=[{"side": "buy", "type": "call", "strike_pct": 1.0}] * 2), "duplicate legs"),
    ("two same-side same-type legs", lambda p: p.update(legs=[{"side": "buy", "type": "call", "strike_pct": 1.0}, {"side": "buy", "type": "call", "strike_pct": 1.05}]), "structure_not_allowed"),
    ("not a dict", lambda p: None, "JSON object"),
])
def test_validate_rejects(label, mut, needle):
    p = put_spread()
    r = mut(p)
    norm, why = lab.validate_proposal(None if label == "not a dict" else p)
    assert norm is None and needle in why, (label, why)


def test_validate_never_raises_on_garbage():
    for junk in (None, 5, "x", [], {"legs": "x"}, {"why": "w", "underlyings": 5}, {"why": "w", "underlyings": ["SPY"], "legs": [5], "dte": 30},
                 {"why": "w", "underlyings": ["SPY"], "legs": [{"side": "buy", "type": "call", "strike_pct": "a"}], "dte": 30},
                 {"why": "w", "underlyings": ["SPY"], "legs": [{"side": "buy", "type": "call", "strike_pct": float("nan")}], "dte": 30},
                 {"why": "w", "underlyings": ["SPY"], "legs": [{"side": "buy", "type": "call", "strike_pct": 1.0}], "dte": "x"},
                 {"why": "w", "underlyings": ["SPY"], "legs": [{"side": "buy", "type": "call", "strike_pct": 1.0}], "dte": 30, "entry": 5}):
        norm, why = lab.validate_proposal(junk)
        assert norm is None and why


# ---------------------------------------------------------------- 规范化与哈希
def test_hash_is_canonical_and_ignores_why_name_order():
    a = put_spread(entry=[{"name": "iv_rv_ge", "value": 1.2}, {"name": "weekday_eq", "value": 2}], underlyings=["SPY", "QQQ"])
    b = put_spread(entry={"weekday_eq": 2.0, "iv_rv_ge": 1.2}, underlyings=["QQQ", "SPY", "SPY"], why="different words", name="x",
                   legs=[{"side": "buy", "type": "put", "strike_pct": 0.9}, {"side": "sell", "type": "put", "strike_pct": 0.95}],
                   exit_dte=7, take_profit=0.5, stop_loss=2.0, max_hold_days=25, cooldown_days=1)          # 全是缺省值写出来
    na, _ = lab.validate_proposal(a)
    nb, _ = lab.validate_proposal(b)
    assert na == nb and lab.spec_hash(na) == lab.spec_hash(nb) and len(lab.spec_hash(na)) == 16
    c, _ = lab.validate_proposal(put_spread(entry=[{"name": "iv_rv_ge", "value": 1.3}, {"name": "weekday_eq", "value": 2}], underlyings=["SPY", "QQQ"]))
    assert lab.spec_hash(c) != lab.spec_hash(na)
    d, _ = lab.validate_proposal(put_spread(take_profit=0.75))
    e, _ = lab.validate_proposal(put_spread())
    assert lab.spec_hash(d) != lab.spec_hash(e)
    assert na["underlyings"] == ["QQQ", "SPY"] and na["exit"]["mode"] == "standard"
    # 财报单：出场参数被忽略（不进哈希）
    ea = {"why": "x", "underlyings": ["NVDA"], "dte": 20, "entry": {"earnings_eve": True}, "legs": [{"side": "buy", "type": "call", "strike_pct": 1.0}, {"side": "buy", "type": "put", "strike_pct": 1.0}]}
    n1, _ = lab.validate_proposal(ea)
    n2, _ = lab.validate_proposal(dict(ea, take_profit=1.0, stop_loss=0.5, max_hold_days=10, exit_dte=9))
    assert n1 == n2 and n1["exit"] == {"mode": "next_session_close"}


def test_debit_and_credit_use_their_own_grids():
    ok, why = lab.validate_proposal({"why": "x", "underlyings": ["SPY"], "dte": 30, "legs": [{"side": "buy", "type": "call", "strike_pct": 1.0}], "take_profit": 1.0, "stop_loss": 0.5})
    assert ok and ok["exit"]["take_profit"] == 1.0
    bad, why = lab.validate_proposal(put_spread(take_profit=1.0))          # 1.0 是借方档，不是信用档
    assert bad is None and "take_profit" in why


# ---------------------------------------------------------------- 试验账本
def test_trials_seeded_once_and_counts(env):
    assert not env.trials_path.exists()
    assert lab.n_trials(env) == 60
    rows = [json.loads(l) for l in env.trials_path.read_text().splitlines()]
    assert [(r["version"], r["n"]) for r in rows] == [("v1", 32), ("v2", 28)] and all(r["kind"] == "seed" for r in rows)
    lab.ensure_ledger(env)
    lab.ensure_ledger(env)
    assert len(env.trials_path.read_text().splitlines()) == 2 and lab.n_trials(env) == 60
    lab.record_trial(env, "dev", "w", "abc")
    assert lab.n_trials(env) == 61
    with open(env.trials_path, "a") as f:
        f.write("this line is corrupt\n")
    assert lab.n_trials(env) == 62                           # 坏行按 1 次试验算（宁多不少）


# ---------------------------------------------------------------- 统计：DSR
def test_deflated_sharpe_matches_hand_calculation():
    """手算：N = 10、V = 0.04（sqrt = 0.2）；Φ⁻¹(1−1/10) = 1.2815516，Φ⁻¹(1−1/(10e)) = Φ⁻¹(0.9632121) = 1.7892418；
    SR0 = 0.2 × [(1−0.5772157)×1.2815516 + 0.5772157×1.7892418] = 0.2 × (0.541820 + 1.032779) = 0.314920；
    SR = 0.5, T = 101, skew = −0.5, kurt = 4：分母² = 1 − (−0.5)(0.5) + (4−1)/4 × 0.25 = 1.4375；
    z = (0.5 − 0.314925) × sqrt(100) / sqrt(1.4375) = 0.185075 × 10 / 1.198958 = 1.54368；DSR(概率) = Φ(1.54368) = 0.93867。"""
    d = lab.deflated_sharpe(0.5, 101, -0.5, 4.0, 10, 0.04)
    assert d["sr0"] == pytest.approx(0.314920, abs=2e-6)
    assert d["z"] == pytest.approx(1.54368, abs=2e-5)
    assert d["prob"] == pytest.approx(0.938667, abs=2e-6)
    # 试验次数越多门槛越高；方差越大门槛越高
    assert lab.deflated_sharpe(0.5, 101, -0.5, 4.0, 100, 0.04)["z"] < d["z"]
    assert lab.deflated_sharpe(0.5, 101, -0.5, 4.0, 10, 0.09)["z"] < d["z"]
    assert lab.expected_max_sr(1, 0.04) == 0.0 and lab.expected_max_sr(10, 0.0) == 0.0
    # 负偏、厚尾让同样的夏普更不可信
    assert lab.deflated_sharpe(0.5, 101, -2.0, 9.0, 10, 0.04)["z"] < d["z"]


def test_weekly_stats_moments():
    x = [1.0, -2.0, 3.0, 0.0, 4.0, -1.0, 2.0, 5.0]
    st = lab.weekly_stats(x)
    a = np.array(x)
    assert st["sr"] == pytest.approx(a.mean() / a.std(ddof=1)) and st["T"] == 8
    m2 = ((a - a.mean()) ** 2).mean()
    assert st["skew"] == pytest.approx(((a - a.mean()) ** 3).mean() / m2 ** 1.5) and st["kurt"] == pytest.approx(((a - a.mean()) ** 4).mean() / m2 ** 2)
    assert lab.weekly_stats([0.0] * 10)["sr"] is None and lab.weekly_stats([1.0])["sr"] is None


# ---------------------------------------------------------------- 统计：PBO
def test_pbo_noise_is_about_half_and_signal_is_low():
    """单次 CSCV 的 70 个组合高度相关，纯噪声的 PBO 单次波动大（std ≈ 0.19），所以对噪声取 40 个随机种子的平均（期望 0.5）。"""
    ps = [lab.pbo(np.random.default_rng(s).normal(0, 1, size=(20, 104))) for s in range(40)]
    assert 0.42 <= float(np.mean(ps)) <= 0.58, np.mean(ps)
    assert min(ps) < 0.4 and max(ps) > 0.6                          # 单次确实散得很开（不是常数）
    for s in range(10):                                             # 一个真的更好的策略（周夏普 0.6）：样本内最优通常就是它，样本外也排前面 -> PBO 低
        sig = np.random.default_rng(100 + s).normal(0, 1, size=(10, 104))
        sig[3] += 0.6
        assert lab.pbo(sig) < 0.15
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 1, size=(20, 104))
    assert lab.pbo(noise[:1]) is None and lab.pbo(noise[:, :10]) is None           # 策略太少 / 周数 < 16
    # 零头周被丢掉（最早的）：前面再加几周垃圾，结果不变
    junk = np.concatenate([rng.normal(0, 50, size=(20, 5)), noise], axis=1)        # 109 周 -> 丢掉最早 5 周 -> 和 noise 的 104 周一样
    assert lab.pbo(junk) == pytest.approx(lab.pbo(noise))


def test_pbo_degenerate_inputs():
    z = np.zeros((4, 32))
    assert lab.pbo(z) == pytest.approx(0.5)                  # 全平手：中位名次，算半个
    assert lab.pbo(np.vstack([np.ones((1, 32)) * np.linspace(0, 1, 32), np.zeros((3, 32))])) is not None


# ---------------------------------------------------------------- 打分
def dev_stub(dsr=1.0, cx=2, ov=(0.0, 0.0), aml=300.0):
    return {"dsr": dsr, "complexity": cx, "overlap": {"2024-08-01..2024-08-16": ov[0], "2025-04-01..2025-04-30": ov[1]}, "avg_max_loss": aml}


def test_score_dev():
    assert lab.score_dev(None, None)[0] == -1.0 and lab.score_dev({"invalid": "bad"}, None)[0] == -1.0
    pts, why = lab.score_dev(dev_stub(1.0), None)
    assert pts == 1.0 and "DSR +1.00" in why                                     # 没有当前最好 -> 基线 0
    assert lab.score_dev(dev_stub(1.0), {"dsr": 0.4})[0] == pytest.approx(0.6)
    assert lab.score_dev(dev_stub(1.0), 0.4)[0] == pytest.approx(0.6)
    assert lab.score_dev(dev_stub(50.0), {"dsr": 0.0})[0] == 5.0                  # 封顶 +5
    assert lab.score_dev(dev_stub(-50.0), {"dsr": 0.0})[0] == -5.0                # 封顶 −5
    assert lab.score_dev(dev_stub(2.0, cx=3), None)[0] == 2.0                    # 复杂度 ≤ 3 不罚
    assert lab.score_dev(dev_stub(2.0, cx=7), None)[0] == pytest.approx(2.0 - 0.5 * 4)
    assert lab.score_dev(dev_stub(2.0, ov=(-901.0, 0.0), aml=300.0), None)[0] == pytest.approx(2.0 - lab.CRASH_PENALTY)    # < −3 × 300
    assert lab.score_dev(dev_stub(2.0, ov=(-900.0, -899.0), aml=300.0), None)[0] == 2.0                                     # 刚好 3 倍不罚
    assert lab.score_dev(dev_stub(None), None)[0] == lab.NO_DSR_POINTS


# ---------------------------------------------------------------- 开发段回测：硬锁
def D_END_for(allow):
    n = bisect_right(ALL_DAYS, lab.DEV_END)
    return ALL_DAYS[n + allow - 1]


def test_run_dev_basic_metrics_and_log(env):
    r = lab.run_dev(put_spread(), "alice", env)
    assert r["valid"] and not r["duplicate"] and r["hash"] and r["dev"]["n_trades"] >= 8
    d = r["dev"]
    assert d["complexity"] == 2 and d["n_weeks"] == len(lab.DEV_WEEKS) == 79 and d["n_clusters"] >= 1
    line = lab.read_log(env)[0]
    assert len(line["weekly"]) == 79 and sum(line["weekly"]) == pytest.approx(d["total_pnl"], abs=0.2)
    assert set(d["overlap"]) == {"2024-08-01..2024-08-16", "2025-04-01..2025-04-30"}
    assert d["worst_trade"]["pnl"] <= 0 or d["worst_trade"]["pnl"] == min(x for x in [d["worst_trade"]["pnl"]])
    assert d["n_trials"] == 61 and lab.n_trials(env) == 61                       # 种子 60 + 本次 1
    assert d["sharpe_ann"] == pytest.approx(d["sharpe_wk"] * math.sqrt(52))
    assert r["points"] == lab.score_dev(d, None)[0] or r["points_reason"]
    trials = [json.loads(l) for l in env.trials_path.read_text().splitlines()]
    assert trials[-1]["kind"] == "dev" and trials[-1]["who"] == "alice" and trials[-1]["hash"] == r["hash"] and trials[-1]["ts"]
    assert line["who"] == "alice" and line["valid"] and line["spec"]["structure"] == "vertical" and line["backtested"]
    # 开发段指标和直接用引擎算的一致（引擎复用，不是复制）
    norm, _ = lab.validate_proposal(put_spread())
    sim = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
    assert d["n_trades"] == len(sim["trades"]) and d["total_pnl"] == pytest.approx(round(sum(t["pnl"] for t in sim["trades"]), 2))
    ov = v2.window_pnl(sim["trades"], "2024-08-01", "2024-08-16")["overlap_pnl"]
    assert d["overlap"]["2024-08-01..2024-08-16"] == ov
    assert d["worst_trade"]["pnl"] == min(t["pnl"] for t in sim["trades"])


def test_dev_never_enters_after_2025_06_30_and_ignores_vault_quotes(tmp_path, shared_opra, env):
    norm, _ = lab.validate_proposal(put_spread())
    base = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
    assert base["trades"] and all(t["entry_date"] <= lab.DEV_END for t in base["trades"])
    last_dev_day = max(t["entry_date"] for t in base["trades"])
    assert last_dev_day >= "2025-06-01"                                          # 样本确实一路开到开发段末尾
    # 没有"2025-07-01 之后入场"的交易：就算 7 月以后的报价非常诱人
    full = lab._simulate(norm, env, SPEC)
    assert any(t["entry_date"] >= lab.VAULT_START for t in full["trades"])        # 对照：不设限的话金库段有入场
    # 1) 改写 / 删掉所有"截止日之后"的报价（含 7 月里晚于 max_hold 的）-> 开发段结果逐笔不变
    dend = D_END_for(25)
    assert dend > lab.VAULT_START

    def wreck(d, row):
        if d > dend:
            row = dict(row)
            row["bid_px"], row["ask_px"] = 0.0, 50.0
        return row
    root2 = tmp_path / "wrecked" / "opra"
    build_data(root2, mutate=wreck)
    (tmp_path / "e2").mkdir()
    env2 = make_env(tmp_path / "e2", root2)
    t2 = lab._simulate(norm, env2, SPEC, lab.DEV_START, lab.DEV_END)["trades"]
    assert [(t["entry_date"], t["exit_date"], t["pnl"]) for t in t2] == [(t["entry_date"], t["exit_date"], t["pnl"]) for t in base["trades"]]
    # 2) 截止日所在月以后的 parquet 一个字节都不许打开（max_hold 25 个交易日 = 8 月初，所以 9 月的文件是垃圾也照常）：打开就会抛异常
    assert dend[:7] == "2025-08"
    root3 = tmp_path / "junk" / "opra"
    shutil.copytree(shared_opra, root3, symlinks=True)
    (root3 / "cbbo1m" / "SPY" / "2025-09.parquet").write_bytes(b"junk, opening this means the vault leaked")
    (tmp_path / "e3").mkdir()
    env3 = make_env(tmp_path / "e3", root3)
    t3 = lab._simulate(norm, env3, SPEC, lab.DEV_START, lab.DEV_END)["trades"]
    assert [t["pnl"] for t in t3] == [t["pnl"] for t in base["trades"]]
    with pytest.raises(Exception):
        lab._simulate(norm, env3, SPEC)                                           # 对照：金库口径要读它们，所以会炸


def test_vault_period_pnl_enters_dev_only_through_closing_trades(tmp_path, env):
    """7 月（截止日内）的报价只影响"开发段入场、7 月才平"的那几笔；其余开发段交易、逐周盈亏（按入场周）一分不动；没有 7 月入场。"""
    norm, _ = lab.validate_proposal(put_spread())
    base = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)

    def rig(d, row):
        if lab.VAULT_START <= d <= "2025-07-31":
            row = dict(row)
            row["bid_px"], row["ask_px"] = 0.01, 0.03                  # 7 月报价全部"便宜"：买回成本极低 -> 跨月仓位会大赚
        return row
    root2 = tmp_path / "rigged" / "opra"
    build_data(root2, mutate=rig)
    (tmp_path / "e2").mkdir()
    env2 = make_env(tmp_path / "e2", root2)
    alt = lab._simulate(norm, env2, SPEC, lab.DEV_START, lab.DEV_END)["trades"]
    assert len(alt) == len(base["trades"]) and all(t["entry_date"] <= lab.DEV_END for t in alt)
    changed = [(a, b) for a, b in zip(alt, base["trades"]) if a["pnl"] != b["pnl"]]
    assert changed, "7 月报价应当影响跨月仓位的平仓"
    assert all(b["exit_date"] >= lab.VAULT_START and a["exit_date"] >= lab.VAULT_START for a, b in changed)    # 只有 7 月才平的仓位受影响
    wk_a, wk_b = v2.weekly_pnl(alt, lab.DEV_WEEKS), v2.weekly_pnl(base["trades"], lab.DEV_WEEKS)
    diff_weeks = [w for w in lab.DEV_WEEKS if abs(wk_a[w] - wk_b[w]) > 1e-9]
    assert diff_weeks and max(diff_weeks) >= "2025-W24" and min(diff_weeks) >= "2025-W20"           # 只在末尾那几周（入场周）有差别
    assert set(wk_a) == set(lab.DEV_WEEKS)                                                          # 没有 7 月的周


def test_dev_window_is_max_hold_days_after_cutoff(env):
    norm, _ = lab.validate_proposal(put_spread(max_hold_days=10))
    s = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
    assert max(t["exit_date"] for t in s["trades"]) <= D_END_for(10)
    norm25, _ = lab.validate_proposal(put_spread(max_hold_days=25))
    s25 = lab._simulate(norm25, env, SPEC, lab.DEV_START, lab.DEV_END)
    assert max(t["exit_date"] for t in s25["trades"]) <= D_END_for(25)


def test_duplicate_is_not_run_not_counted_and_scored_minus_one(env):
    r1 = lab.run_dev(put_spread(), "alice", env)
    n1 = lab.n_trials(env)
    assert n1 == 61
    r2 = lab.run_dev(put_spread(why="other words", name="again"), "bob", env)         # 别人提同一个方案
    assert r2["duplicate"] and r2["points"] == -1.0 and r2["dev"] is None and r2["hash"] == r1["hash"]
    assert lab.n_trials(env) == n1
    log = lab.read_log(env)
    assert len(log) == 2 and log[1]["duplicate_of"]["who"] == "alice" and not log[1]["backtested"]
    r3 = lab.run_dev(put_spread(take_profit=0.75), "bob", env)                        # 不同参数 = 新方案
    assert not r3["duplicate"] and lab.n_trials(env) == n1 + 1


def test_invalid_proposal_is_logged_scored_minus_one_no_trial(env):
    r = lab.run_dev({"why": "x", "underlyings": ["SPY"], "dte": 38, "legs": [{"side": "sell", "type": "put", "strike_pct": 0.95}]}, "mal", env)
    assert not r["valid"] and r["points"] == -1.0 and "naked_short" in r["reason"] and r["hash"] is None
    assert lab.n_trials(env) == 60 and len(lab.read_log(env)) == 1
    assert lab.run_dev("garbage", "mal", env)["points"] == -1.0


def test_pooled_two_underlyings_cluster_counts_per_underlying(tmp_path):
    root = tmp_path / "opra"
    build_data(root, unds=("SPY", "QQQ"), q_end="2024-12-31")
    (tmp_path / "e").mkdir()
    env = make_env(tmp_path / "e", root)
    norm, _ = lab.validate_proposal(put_spread(underlyings=["SPY", "QQQ"]))
    sim = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
    unds = {t["und"] for t in sim["trades"]}
    assert unds == {"SPY", "QQQ"} and sim["per_und"]["SPY"]["status"] == sim["per_und"]["QQQ"]["status"] == "ok"
    r = lab.run_dev(put_spread(underlyings=["SPY", "QQQ"]), "p", env)
    one = lab.run_dev(put_spread(underlyings=["SPY"], take_profit=0.75), "p", env)
    assert r["dev"]["n_trades"] > one["dev"]["n_trades"]
    per = sum(len(v2.clusters([t["entry_idx"] for t in sim["trades"] if t["und"] == u])) for u in unds)
    assert r["dev"]["n_clusters"] == per


# ---------------------------------------------------------------- 条件
def mk_ctx(env, und="SPY", days_to="2025-06-30"):
    raw_d, adj_d = lab.v1.read_closes(env.opra, und)
    days = sorted(set(raw_d) & set(adj_d))
    days = days[:bisect_right(days, days_to)]
    q = v2.load_quotes(env.opra, und, last_day=days_to)
    return lab.LabCtx(und, days, [adj_d[d] for d in days], [raw_d[d] for d in days], q, SPEC)


def strat_with(conds, **kw):
    norm, why = lab.validate_proposal(put_spread(entry=conds, **kw))
    assert norm, why
    return lab.to_strategy(norm)


def test_cond_weekday_and_rv_and_ivrv(env):
    ctx = mk_ctx(env)
    i = ctx.days.index("2025-03-12")                      # 周三
    assert lab.cond_signal(strat_with([{"name": "weekday_eq", "value": 2}]), ctx, i) is True
    assert lab.cond_signal(strat_with([{"name": "weekday_eq", "value": 3}]), ctx, i) is False
    rv = ctx.rv(i)
    assert rv is not None
    s_hi = strat_with([{"name": "rv20_ge", "value": 0.5}])
    s_lo = strat_with([{"name": "rv20_le", "value": 0.5}])
    assert lab.cond_signal(s_hi, ctx, i) is False and lab.cond_signal(s_lo, ctx, i) is True      # 合成收盘很平，RV 远低于 50%
    iv = ctx.iv(i)
    ratio = iv / rv
    for thr in lab.IV_RV_GRID:
        assert lab.cond_signal(strat_with([{"name": "iv_rv_ge", "value": thr}]), ctx, i) is (ratio >= thr)
        assert lab.cond_signal(strat_with([{"name": "iv_rv_le", "value": thr}]), ctx, i) is (ratio <= thr)
    # 有一个明确 False 就是 False（即使另一个算不出）；没有 False 但有算不出 -> None
    j = ctx.days.index("2023-12-29")                      # 周五；报价数据 2024-01-02 才开始 -> IV 算不出
    assert ctx.iv(j) is None and ctx.rv(j) is not None
    s_iv = strat_with([{"name": "iv_rv_ge", "value": 1.0}])
    assert lab.cond_signal(s_iv, ctx, j) is None
    assert lab.cond_signal(strat_with([{"name": "iv_rv_ge", "value": 1.0}, {"name": "weekday_eq", "value": 0}]), ctx, j) is False       # 一个明确 False 压过算不出
    assert lab.cond_signal(strat_with([{"name": "iv_rv_ge", "value": 1.0}, {"name": "weekday_eq", "value": 4}]), ctx, j) is None


def test_rv_and_trend_conditions_use_only_data_before_today(env):
    ctx = mk_ctx(env)
    i = ctx.days.index("2025-03-12")
    for cond in ({"name": "rv20_ge", "value": 0.1}, {"name": "trend_R2", "value": True}, {"name": "trend_R3", "value": True}, {"name": "trend_R4", "value": True}):
        s = strat_with([cond])
        base = lab.cond_signal(s, ctx, i)
        adj2 = list(ctx.adj)
        adj2[i] *= 3
        adj2[i + 1:] = [1e9] * (len(adj2) - i - 1)
        ctx2 = lab.LabCtx("SPY", ctx.days, adj2, ctx.raw, ctx.q, SPEC)
        assert lab.cond_signal(s, ctx2, i) is base
    # 趋势信号是按 d−1 算的：改 d−1 的收盘会变
    s = strat_with([{"name": "trend_R3", "value": True}])
    up = list(ctx.adj)
    for k in range(i - 60, i):
        up[k] = 50 + (k - (i - 60)) * 1.0                    # 一路上涨
    ctx_up = lab.LabCtx("SPY", ctx.days, up, ctx.raw, ctx.q, SPEC)
    assert lab.cond_signal(s, ctx_up, i) is True
    down = list(up)
    down[i - 1] = 1.0                                        # d−1 暴跌
    assert lab.cond_signal(s, lab.LabCtx("SPY", ctx.days, down, ctx.raw, ctx.q, SPEC), i) is False
    assert lab.cond_signal(strat_with([{"name": "trend_R1", "value": True}]), ctx, i) is None      # 没有 IEF 数据 = 算不出


def test_iv_percentile_is_past_only_and_needs_history(tmp_path, env):
    ctx = mk_ctx(env)
    i = ctx.days.index("2025-03-12")
    pct = ctx.iv_pct(i)
    past = [ctx.iv(j) for j in range(i - 252, i)]
    past = [x for x in past if x is not None]
    cur = ctx.iv(i)
    assert pct == pytest.approx((sum(x < cur for x in past) + 0.5 * sum(x == cur for x in past)) / len(past)) and 0 <= pct <= 1
    s_ge, s_le = strat_with([{"name": "iv_pct_ge", "value": 0.5}]), strat_with([{"name": "iv_pct_le", "value": 0.5}])
    assert lab.cond_signal(s_ge, ctx, i) is (pct >= 0.5) and lab.cond_signal(s_le, ctx, i) is (pct <= 0.5)
    early = ctx.days.index("2024-02-01")                   # 有效历史 < 60 个交易日
    assert ctx.iv_pct(early) is None and lab.cond_signal(s_ge, ctx, early) is None
    # 无前视：d 之后的报价全部改掉，分位不变
    root2 = tmp_path / "fut" / "opra"
    cut = ctx.days[i]
    build_data(root2, mutate=lambda d, row: dict(row, bid_px=0.5, ask_px=0.52) if d > cut else row)
    (tmp_path / "e2").mkdir()
    env2 = make_env(tmp_path / "e2", root2)
    ctx2 = mk_ctx(env2)
    assert ctx2.iv_pct(i) == pct
    # d−1 的报价改了（过去的快照）就会变；d 当天的 IV 改了也会变（隐含的量用 d 当天快照）
    root3 = tmp_path / "past" / "opra"
    build_data(root3, mutate=lambda d, row: dict(row, bid_px=row["bid_px"] * 1.5, ask_px=row["ask_px"] * 1.5) if d == ctx.days[i - 1] else row)
    (tmp_path / "e3").mkdir()
    ctx3 = mk_ctx(make_env(tmp_path / "e3", root3))
    assert ctx3.iv_pct(i) != pct or ctx3.iv(i - 1) != ctx.iv(i - 1)


def test_term_slope_sign_and_degenerate(tmp_path):
    """合成：近月 IV 低、远月 IV 高 -> 斜率 > 0；反过来 < 0；只有一个到期日 -> 算不出。"""
    def q_for(sig_by_dte, day="2024-03-04"):
        root = tmp_path / f"q{abs(hash(tuple(sig_by_dte.items())))}"
        o = date.fromisoformat(day).toordinal()
        rows = []
        ts = pd.Timestamp(f"{day} 15:59", tz=ET).tz_convert("UTC")
        for dte, sg in sig_by_dte.items():
            e = date.fromordinal(o + dte).isoformat()
            for cp in ("C", "P"):
                p = v2.bs_price(100.0, 100.0, dte / 365, 0.045, sg, cp)
                rows.append({"ts": ts, "symbol": osi(e, cp, 100), "bid_px": p - 0.01, "ask_px": p + 0.01, "bid_sz": 1, "ask_sz": 1})
        (root / "SPY").mkdir(parents=True)
        pd.DataFrame(rows).to_parquet(root / "SPY" / "2024-03.parquet", index=False)
        return v2.Quotes.from_dir(root / "SPY")
    day = "2024-03-04"
    days = weekdays_between("2024-01-02", day)
    px = [100.0] * len(days)
    for sig_map, sign in (({28: 0.20, 35: 0.22, 49: 0.28, 56: 0.30}, +1), ({28: 0.30, 35: 0.28, 49: 0.22, 56: 0.20}, -1)):
        ctx = lab.LabCtx("SPY", days, px, px, q_for(sig_map), SPEC)
        sl = ctx.term_slope(len(days) - 1)
        assert sl is not None and sl * sign > 0.03
        up = strat_with([{"name": "term_slope_ge", "value": 0.02}])
        assert lab.cond_signal(up, ctx, len(days) - 1) is (sign > 0)
    one = lab.LabCtx("SPY", days, px, px, q_for({35: 0.25}), SPEC)
    assert one.term_slope(len(days) - 1) is None
    assert lab.cond_signal(strat_with([{"name": "term_slope_ge", "value": 0.0}]), one, len(days) - 1) is None


def test_run_dev_with_conditions_trades_less_than_always(env):
    always_r = lab.run_dev(put_spread(), "c", env)
    wd = lab.run_dev(put_spread(entry=[{"name": "weekday_eq", "value": 0}]), "c", env)
    assert wd["dev"]["n_trades"] < always_r["dev"]["n_trades"] and wd["dev"]["complexity"] == 3
    assert all(date.fromisoformat(t["entry_date"]).weekday() == 0 for t in lab._simulate(lab.validate_proposal(put_spread(entry=[{"name": "weekday_eq", "value": 0}]))[0], env, SPEC, lab.DEV_START, lab.DEV_END)["trades"])


# ---------------------------------------------------------------- 研究日志、历史摘要、金库
def test_history_for_dev_numbers_only_and_vault_flag_only(env):
    lab.run_dev(put_spread(), "alice", env)
    lab.run_dev(put_spread(take_profit=0.75), "alice", env)
    lab.run_dev(put_spread(take_profit=0.25), "bob", env)
    lab.run_dev({"why": "x"}, "alice", env)
    h = lab.history_for("alice", 10, env)
    assert h.count("\n") == 2 and "bob" not in h and "trades" in h and "DSR" in h and "INVALID" in h
    assert "bob" not in lab.history_for("bob", 1, env) and lab.history_for("nobody", 5, env) == "(no proposals yet)"
    assert lab.history_for("alice", 1, env).count("\n") == 0
    # 金库数字绝不出现：往 vault.jsonl 塞哨兵数字，摘要里不得出现；只出现"过 / 没过"
    log = lab.read_log(env)
    h1 = [e for e in log if e["who"] == "alice" and e["valid"]][0]["hash"]
    lab._append(env.vault_path, {"hash": h1, "who": "alice", "passed": False, "iso_week": "2025-W40",
                                 "eval": {"test": {"total_pnl": 987654.32, "trades": 31337}, "test_week_boot_p": 0.031415}, "secret": 424242.42})
    txt = lab.history_for("alice", 10, env)
    for s in ("987654", "31337", "0.031415", "424242", "secret"):
        assert s not in txt
    assert "vault: FAILED" in txt and "vault: PASSED" not in txt
    lab._append(env.vault_path, {"hash": [e for e in log if e["who"] == "alice" and e["valid"]][1]["hash"], "passed": True, "iso_week": "2025-W41"})
    assert "vault: PASSED" in lab.history_for("alice", 10, env)


def test_vault_exam_end_to_end(env):
    r = lab.run_dev(put_spread(), "alice", env)
    n_before = lab.n_trials(env)
    out = lab.vault_exam(r["hash"], env, now=datetime(2025, 10, 6, 12, tzinfo=timezone.utc))
    assert set(out) == {"hash", "passed"} and out["hash"] == r["hash"] and isinstance(out["passed"], bool)
    assert lab.n_trials(env) == n_before + 1
    v = [json.loads(l) for l in env.vault_path.read_text().splitlines()]
    assert len(v) == 1 and v[0]["hash"] == r["hash"] and v[0]["passed"] == out["passed"] and v[0]["iso_week"] == "2025-W41"
    assert v[0]["eval"]["test"]["trades"] >= 1 and v[0]["n_trials"] == n_before + 1 and v[0]["bonferroni"] == pytest.approx(0.05 / (n_before + 1))
    rep = env.reports / f"lab_vault_{r['hash']}.md"
    assert rep.exists() and r["hash"] in rep.read_text() and "金库" in rep.read_text()
    # 金库口径 = 全样本，考试段入场确实在 7 月以后；开发段那部分和开发段回测的成交一致
    norm, _ = lab.validate_proposal(put_spread())
    full = lab._simulate(norm, env, SPEC)
    assert v[0]["eval"]["test"]["trades"] == len([t for t in full["trades"] if t["entry_date"] >= lab.VAULT_START])
    # 同一个哈希只考一次：再考不加试验、不重跑
    again = lab.vault_exam(r["hash"], env)
    assert again == out and lab.n_trials(env) == n_before + 1 and len(env.vault_path.read_text().splitlines()) == 1
    with pytest.raises(ValueError):
        lab.vault_exam("deadbeefdeadbeef", env)
    assert "vault: " in lab.history_for("alice", 5, env)


def test_vault_incomplete_data_never_passes(env, tmp_path):
    """没报价收尾的笔 > 5% -> 整个方案不过（v2 完整性闸门）。把 2024-06-03 之后的报价全删：最后一笔没有出场报价（no_more_quotes）、笔数又少（占比 > 5%）。"""
    root = tmp_path / "thin" / "opra"
    build_data(root, mutate=lambda d, row: None if d >= "2024-06-03" else row)
    (tmp_path / "e").mkdir()
    env2 = make_env(tmp_path / "e", root)
    r = lab.run_dev(put_spread(), "alice", env2)
    out = lab.vault_exam(r["hash"], env2)
    rec = json.loads(env2.vault_path.read_text().splitlines()[0])
    assert rec["eval"]["n_no_more_quotes"] >= 1 and rec["complete"] is False and out["passed"] is False


def crafted_entry(h, weekly, points, who="w", n_trades=20):
    return {"who": who, "ts": "2025-09-01T00:00:00+00:00", "hash": h, "spec": {"structure": "vertical", "underlyings": ["SPY"], "dte": 38, "legs": [], "conditions": [], "exit": {"mode": "next_session_close"}},
            "why": "x", "valid": True, "reason": "", "duplicate_of": None, "backtested": True,
            "dev": {"n_trades": n_trades, "n_clusters": 10, "dsr": 1.0, "complexity": 2, "overlap": {}, "avg_max_loss": 300.0, "avg_ret": 0.1, "total_pnl": 1.0,
                    "boot_p": 0.1, "worst_trade": {"pnl": -1.0}},
            "weekly": [round(float(x), 2) for x in weekly], "points": points, "points_reason": "crafted"}


def test_weekly_promotion_one_per_iso_week(env, monkeypatch):
    rng = np.random.default_rng(11)
    lab.ensure_ledger(env)
    for k in range(28):
        lab._append(env.log_path, crafted_entry(f"noise{k:02d}", rng.normal(0, 1, 79), 0.0))
    lab._append(env.log_path, crafted_entry("strongA", rng.normal(2, 1, 79), 3.0))
    lab._append(env.log_path, crafted_entry("strongB", rng.normal(2, 1, 79), 2.0))
    calls = []

    def fake_vault(h, env=None, now=None):
        calls.append(h)
        lab._append(env.vault_path, {"hash": h, "passed": True, "iso_week": v2.week_key(now.date().isoformat())})
        lab.record_trial(env, "vault", "w", h, now)
        return {"hash": h, "passed": True}
    monkeypatch.setattr(lab, "vault_exam", fake_vault)
    assert lab.current_pbo(env) is not None and lab.current_pbo(env) < 0.5
    wed = datetime(2025, 10, 1, 12, tzinfo=timezone.utc)                   # 2025-W40
    assert lab.weekly_promotion(env, now=wed) == {"hash": "strongA", "passed": True}           # 得分最高、DSR > 0、PBO < 0.5
    thu = datetime(2025, 10, 2, 12, tzinfo=timezone.utc)
    sk = lab.weekly_promotion(env, now=thu)
    assert "skipped" in sk and "2025-W40" in sk["skipped"] and calls == ["strongA"]            # 同一个 ISO 周不再来
    nxt = datetime(2025, 10, 8, 12, tzinfo=timezone.utc)                   # 2025-W41
    assert lab.weekly_promotion(env, now=nxt) == {"hash": "strongB", "passed": True}           # strongA 已考过，轮到 B
    third = datetime(2025, 10, 15, 12, tzinfo=timezone.utc)
    sk3 = lab.weekly_promotion(env, now=third)
    assert "skipped" in sk3 and calls == ["strongA", "strongB"]            # 噪声方案 DSR < 0：没人可晋级
    assert lab.status(env)["vault_exams"] == 2


def test_weekly_promotion_gates_dsr_and_pbo(env, monkeypatch):
    rng = np.random.default_rng(5)
    called = []
    monkeypatch.setattr(lab, "vault_exam", lambda h, env=None, now=None: called.append(h) or {"hash": h, "passed": False})
    now = datetime(2025, 10, 1, tzinfo=timezone.utc)
    assert "skipped" in lab.weekly_promotion(env, now=now)                  # 日志是空的
    for k in range(20):                                                     # 全是噪声：DSR > 0 的不存在
        lab._append(env.log_path, crafted_entry(f"n{k}", rng.normal(0, 1, 79), 1.0))
    assert "no unvaulted candidate" in lab.weekly_promotion(env, now=now)["skipped"]
    lab._append(env.log_path, crafted_entry("good", rng.normal(2, 1, 79), 0.2))                  # DSR > 0，但分数不是最高
    lab._append(env.log_path, crafted_entry("zerotrades", np.zeros(79), 9.0, n_trades=0))      # 分数最高但没交易：不能晋级
    lab._append(env.log_path, crafted_entry("flat", np.zeros(79), 9.0))                          # 周盈亏方差 0：DSR 算不出
    monkeypatch.setattr(lab, "current_pbo", lambda env=None, log=None: 0.5)
    assert "PBO" in lab.weekly_promotion(env, now=now)["skipped"] and called == []             # PBO 必须 < 0.5（= 0.5 不行）
    monkeypatch.setattr(lab, "current_pbo", lambda env=None, log=None: None)
    assert "PBO n/a" in lab.weekly_promotion(env, now=now)["skipped"] and called == []        # 算不出 PBO = 不晋级
    monkeypatch.setattr(lab, "current_pbo", lambda env=None, log=None: 0.49)
    assert lab.weekly_promotion(env, now=now) == {"hash": "good", "passed": False} and called == ["good"]


def test_status_shape(env):
    lab.run_dev(put_spread(), "a", env)
    lab.run_dev({"why": "x"}, "a", env)
    st = lab.status(env)
    assert st["n_trials"] == 61 and st["proposals"] == 2 and st["invalid"] == 1 and st["backtested"] == 1 and st["vault_exams"] == 0
    assert "pbo" in st and "best_dev_dsr" in st


def test_cli_validate_and_status(tmp_path, capsys, env):
    p = tmp_path / "p.json"
    p.write_text(json.dumps(put_spread()))
    assert lab.main(["--validate", str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["hash"] == lab.spec_hash(out["normalized"])
    p.write_text(json.dumps({"why": "x", "underlyings": ["SPY"], "dte": 30, "legs": [{"side": "sell", "type": "call", "strike_pct": 1.0}]}))
    assert lab.main(["--validate", str(p)]) == 1
    assert "naked_short" in json.loads(capsys.readouterr().out)["reason"]
    assert lab.main(["--status", "--data", str(env.data_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["n_trials"] == 60
    p.write_text(json.dumps(put_spread()))
    assert lab.main(["--dev", str(p), "--who", "cli", "--data", str(env.data_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["dev"]["n_trades"] > 0
    assert lab.main(["--promote", "--data", str(env.data_dir)]) == 0
    assert "skipped" in json.loads(capsys.readouterr().out)
