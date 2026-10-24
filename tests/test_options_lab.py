"""卡 L1：options_lab — 全部用合成数据（沙箱没有数据商网络）。夹具写法沿用 tests/test_options_v2_backtest.py：合成 parquet + 合成收盘。"""
import copy
import json
import math
import os
import shutil
from bisect import bisect_right
from collections import Counter
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
NORMAL = __import__("statistics").NormalDist()


def dev_stub(dsr=1.0, cx=2, ov=(0.0, 0.0), aml=300.0, n=25, cl=12, by=None, incomplete=False):
    d = {"dsr": dsr, "complexity": cx, "overlap": {"2024-08-01..2024-08-16": ov[0], "2025-04-01..2025-04-30": ov[1]}, "avg_max_loss": aml,
         "n_trades": n, "n_clusters": cl, "data_incomplete": incomplete}
    if by is not None:
        d["crash_by_und"] = by
    return d


def test_score_dev():
    assert lab.score_dev(None, None)[0] == -1.0 and lab.score_dev({"invalid": "bad"}, None)[0] == -1.0
    pts, why = lab.score_dev(dev_stub(1.0), None)
    assert pts == 1.0 and "置信度 84%" in why and "z +1.00" in why and "DSR" not in why         # 没有当前最好 -> 基线 0；文字用置信度
    assert lab.score_dev(dev_stub(1.0), {"dsr": 0.4})[0] == pytest.approx(0.6)
    assert lab.score_dev(dev_stub(1.0), 0.4)[0] == pytest.approx(0.6)
    assert lab.score_dev(dev_stub(50.0), {"dsr": 0.0})[0] == 5.0                  # 封顶 +5
    assert lab.score_dev(dev_stub(-50.0), {"dsr": 0.0})[0] == -5.0                # 封顶 −5
    assert lab.score_dev(dev_stub(2.0, cx=3), None)[0] == 2.0                    # 复杂度 ≤ 3 不罚
    assert lab.score_dev(dev_stub(2.0, cx=7), None)[0] == pytest.approx(2.0 - 0.5 * 4)
    assert lab.score_dev(dev_stub(None), None)[0] == lab.NO_DSR_POINTS


def test_score_dev_sample_floor_and_incomplete():
    """#1：< 20 笔或 < 10 簇 = 0 分，原因"样本太少"（哪怕 DSR 看起来很高）；#8：数据不全 = 0 分待定。"""
    for n, cl in ((19, 12), (25, 9), (0, 0), (5, 5)):
        assert lab.score_dev(dev_stub(9.0, n=n, cl=cl), None) == (0.0, "样本太少")
    assert lab.score_dev(dev_stub(9.0, n=20, cl=10), None)[0] == 5.0              # 刚好够
    pts, why = lab.score_dev(dev_stub(9.0, incomplete=True), None)
    assert pts == 0.0 and "数据不全" in why


def test_crash_penalty_per_underlying_summed_and_capped():
    """#5：points −= min(5, 亏损 ÷ 平均最大亏损)，按标的算再相加，总罚分封顶 −5。"""
    base = lab.score_dev(dev_stub(2.0), None)[0]
    assert base == 2.0
    pen = lambda by: base - lab.score_dev(dev_stub(2.0, by=by), None)[0]           # noqa: E731
    assert pen({"SPY": {"loss": 600.0, "avg_max_loss": 300.0}}) == pytest.approx(2.0)
    assert pen({"SPY": {"loss": 600.0, "avg_max_loss": 300.0}, "QQQ": {"loss": 450.0, "avg_max_loss": 300.0}}) == pytest.approx(3.5)       # 2 + 1.5
    assert pen({"SPY": {"loss": 3000.0, "avg_max_loss": 300.0}}) == pytest.approx(5.0)                                                   # 单个标的 10 倍 -> 封顶 5
    assert pen({"SPY": {"loss": 900.0, "avg_max_loss": 300.0}, "QQQ": {"loss": 900.0, "avg_max_loss": 300.0}}) == pytest.approx(5.0)       # 3 + 3 = 6 -> 封顶 5
    assert pen({"SPY": {"loss": 0.0, "avg_max_loss": 300.0}}) == 0.0
    assert pen({"SPY": {"loss": -50.0, "avg_max_loss": 300.0}}) == 0.0                                                                    # 盈利不加分
    # 没有逐标的数据：整体 overlap 里亏损的窗口加总 ÷ 平均最大亏损；盈利窗口不抵消
    assert pen(None) == 0.0
    old = lab.score_dev(dev_stub(2.0, ov=(-600.0, 900.0), aml=300.0), None)[0]
    assert base - old == pytest.approx(2.0)


def test_dev_metrics_crash_by_underlying():
    def tr(und, ent, ext, pnl, ml=300.0, i=0):
        return {"und": und, "entry_date": ent, "exit_date": ext, "pnl": pnl, "max_loss": ml, "ret": pnl / ml, "entry_idx": i, "reason": "take_profit", "legs": "x"}
    trades = [tr("SPY", "2024-08-02", "2024-08-09", -600.0, i=1), tr("SPY", "2025-04-03", "2025-04-10", -150.0, i=30),     # SPY：−750 亏损
              tr("QQQ", "2024-08-02", "2024-08-09", -300.0, i=1), tr("QQQ", "2025-04-03", "2025-04-10", 500.0, i=30),       # QQQ：第一个窗口亏 300，第二个窗口赚 500（不抵消）
              tr("QQQ", "2024-03-01", "2024-03-08", 50.0, i=60)]
    sim = {"trades": trades, "per_und": {}, "skips": Counter(), "unresolved": 0}
    d, weekly, _ = lab._dev_metrics(sim, lab.validate_proposal(put_spread())[0])
    assert d["crash_by_und"]["SPY"] == {"loss": 750.0, "avg_max_loss": 300.0, "ratio": 2.5}
    assert d["crash_by_und"]["QQQ"]["loss"] == 300.0 and d["crash_by_und"]["QQQ"]["ratio"] == 1.0
    assert lab.score_dev(dict(d, dsr=2.0, n_trades=25, n_clusters=12), None)[0] == pytest.approx(2.0 - 3.5)
    assert all(k in d for k in ("sr", "T", "skew", "kurt")) and d["T"] == 79                    # #2：原始统计量随结果一起存


def crafted_entry(h, weekly, points=0.0, who="w", n_trades=25, n_clusters=12, status="scored", **extra):
    """日志里一行"合格的开发段方案"：原始 SR / T / 偏度 / 峰度由 weekly 算出来存好（和 run_dev 存的一样）。"""
    st = lab.weekly_stats(list(weekly))
    e = {"who": who, "ts": "2025-09-01T00:00:00+00:00", "hash": h, "spec": {"structure": "vertical", "underlyings": ["SPY"], "dte": 38, "legs": [], "conditions": [], "exit": {"mode": "next_session_close"}},
         "why": "x", "valid": True, "reason": "", "duplicate_of": None, "backtested": True, "status": status,
         "dev": {"n_trades": n_trades, "n_clusters": n_clusters, "dsr": 99.0, "dsr_prob": 1.0, "complexity": 2, "overlap": {}, "avg_max_loss": 300.0, "avg_ret": 0.1, "total_pnl": 1.0,
                 "boot_p": 0.1, "worst_trade": {"pnl": -1.0}, "sr": st["sr"], "T": st["T"], "skew": st["skew"], "kurt": st["kurt"], "data_incomplete": False},
         "weekly": [round(float(x), 2) for x in weekly], "points": points, "points_reason": "crafted"}
    e.update(extra)
    return e


def test_baseline_recomputed_at_current_N_and_V_not_stored(env):
    """#2：best_so_far = 日志里全部合格方案在当前 N、V 下的 z 的最大值；存的旧 dsr（这里故意存成 99）不算数。"""
    rng = np.random.default_rng(3)
    wk = rng.normal(0.6, 1.0, 79)
    lab._append(env.log_path, crafted_entry("A", wk, points=4.0))
    N0 = lab.n_trials(env)
    sc0, V0 = lab.current_scores(lab.read_log(env), N0)
    st = lab.weekly_stats(list(wk))
    assert sc0["A"]["z"] == pytest.approx(lab.deflated_sharpe(st["sr"], 79, st["skew"], st["kurt"], N0, V0)["z"]) and sc0["A"]["z"] < 99
    sc1, _ = lab.current_scores(lab.read_log(env), N0 + 500)
    assert sc1["A"]["z"] < sc0["A"]["z"]                                      # N 越大门槛越高：同一个方案的 z 变低
    assert lab.best_dev(env=env)["dsr"] == pytest.approx(sc0["A"]["z"]) and lab.best_dev(env=env)["hash"] == "A"
    assert lab.top_dev(3, env)[0]["dsr"] == pytest.approx(sc0["A"]["z"])
    # 新方案的打分用 A 的**当前** z 做基线
    r = lab.run_dev(put_spread(), "alice", env)
    assert r["status"] == "scored"
    scn, Vn = lab.current_scores(lab.read_log(env), lab.n_trials(env))
    zA = scn["A"]["z"]
    assert r["points"] == lab.score_dev(r["dev"], zA)[0] and r["points"] != lab.score_dev(r["dev"], 99.0)[0]
    assert r["dev"]["var_sr"] == pytest.approx(Vn) and r["dev"]["dsr"] == pytest.approx(scn[r["hash"]]["z"])
    assert r["dev"]["dsr_prob"] == pytest.approx(NORMAL.cdf(r["dev"]["dsr"])) and f"z {zA:+.2f}" in r["points_reason"]
    # 日志里存了原始统计量
    d = lab.read_log(env)[-1]["dev"]
    assert d["T"] == 79 and d["sr"] is not None and d["skew"] is not None and d["kurt"] is not None and d["n_trials"] == lab.n_trials(env)


def test_V_floor():
    """#3：V = max(合格方案周夏普的样本方差, 1/(T−1))。"""
    assert lab._var_sr([0.30, 0.3001, 0.2999], 79) == pytest.approx(1 / 78)       # 几乎一样的夏普 -> 方差接近 0 -> 取下限
    assert lab._var_sr([0.9], 79) == pytest.approx(1 / 78) and lab._var_sr([], 79) == pytest.approx(1 / 78)
    big = [0.0, 1.0, 2.0]
    assert lab._var_sr(big, 79) == pytest.approx(1.0)                              # 方差 1 > 下限
    rng = np.random.default_rng(1)
    base = rng.normal(0.3, 1, 79)
    log = [crafted_entry(f"x{k}", base + rng.normal(0, 0.01, 79)) for k in range(3)]
    sc, V = lab.current_scores(log, 100)
    assert V == pytest.approx(1 / 78)
    # 不合格 / 近似重复以外的方案不进 V：不合格的（n_trades < 20）不影响 V
    log2 = log + [crafted_entry("tiny", rng.normal(5, 1, 79), n_trades=3)]
    assert lab.current_scores(log2, 100)[1] == V and "tiny" not in lab.current_scores(log2, 100)[0]


def test_ineligible_small_sample_logged_counted_not_best_or_promotable(env):
    """#1：< 20 笔 -> 0 分、原因"样本太少"、写日志、照样记试验；不进当前最好、不能晋级。"""
    n0 = lab.n_trials(env)
    r = lab.run_dev(put_spread(entry=[{"name": "iv_pct_ge", "value": 0.9}]), "small", env)
    assert r["status"] == "ineligible" and r["points"] == 0.0 and r["points_reason"] == "样本太少" and r["dev"]["n_trades"] < 20
    assert r["dev"]["dsr"] is None and lab.n_trials(env) == n0 + 1
    line = lab.read_log(env)[-1]
    assert line["backtested"] and line["status"] == "ineligible" and "sr" in line["dev"] and line["dev"]["T"] == 79
    assert lab.best_dev(env=env) is None and lab.top_dev(5, env) == [] and lab.current_scores(lab.read_log(env), 100)[0] == {}
    txt = lab.history_for("small", 5, env)
    assert "样本太少" in txt and "0 points" in txt
    assert lab.status(env)["too_small"] == 1 and lab.status(env)["eligible"] == 0
    # 够格的方案才进最好榜
    ok = lab.run_dev(put_spread(), "ok", env)
    assert ok["status"] == "scored" and lab.best_dev(env=env)["hash"] == ok["hash"]
    # 19 笔也不行：实测一个 19 笔的方案
    r19 = lab.run_dev(put_spread(entry=[{"name": "weekday_eq", "value": 2}], cooldown_days=5), "s19", env)
    assert r19["status"] == "ineligible" and r19["dev"]["n_trades"] == 19


def test_near_duplicate_identical_trade_set(env):
    """#4：成交明细完全相同（这里 iv_rv_ge 1.5 在合成数据里恒成立 = always）-> −0.5、原因固定、照样记试验、不进最好、不能晋级。"""
    a = lab.run_dev(put_spread(), "alice", env)
    n1 = lab.n_trials(env)
    b = lab.run_dev(put_spread(entry=[{"name": "iv_rv_ge", "value": 1.5}]), "bob", env)
    assert a["hash"] != b["hash"] and b["status"] == "near_duplicate" and b["points"] == -0.5 and b["points_reason"] == "结果和已测方案几乎一样"
    assert lab.n_trials(env) == n1 + 1 and b["dev"]["dsr"] is None
    line = lab.read_log(env)[-1]
    assert line["near_duplicate_of"]["hash"] == a["hash"] and line["trade_sig"] == lab.read_log(env)[0]["trade_sig"]
    assert lab.best_dev(env=env)["hash"] == a["hash"] and [r["hash"] for r in lab.top_dev(5, env)] == [a["hash"]]
    sc, _ = lab.current_scores(lab.read_log(env), lab.n_trials(env))
    assert sc[a["hash"]]["promotable"] and not sc[b["hash"]]["promotable"]
    assert "几乎一样" in lab.history_for("bob", 3, env)


def test_near_duplicate_helper_correlation_threshold():
    rng = np.random.default_rng(2)
    base = rng.normal(0, 100, 79)
    other = crafted_entry("O", base)
    mine = base + rng.normal(0, 10, 79)                    # 相关系数 ≈ 0.995
    assert np.corrcoef(base, mine)[0, 1] > 0.95
    assert lab._near_duplicate(list(mine), "zz", [other], "N")["hash"] == "O"
    weak = base + rng.normal(0, 150, 79)                   # 相关 ≈ 0.55
    assert np.corrcoef(base, weak)[0, 1] < 0.95 and lab._near_duplicate(list(weak), "zz", [other], "N") is None
    assert lab._near_duplicate(list(mine), "zz", [other], "O") is None            # 同一个哈希（自己）不比
    flat = crafted_entry("F", np.zeros(79))
    assert lab._near_duplicate([0.0] * 79, "zz", [flat], "N") is None             # 常数序列算不出相关，不算重复
    same_sig = crafted_entry("S", rng.normal(0, 1, 79), trade_sig="abc123")
    assert lab._near_duplicate(list(rng.normal(0, 1, 79)), "abc123", [same_sig], "N")["hash"] == "S"
    # 比对对象包含不合格 / 数据不全的方案（"日志里任一方案"）
    small = crafted_entry("T", base, n_trades=3)
    assert lab._near_duplicate(list(mine), "zz", [small], "N")["hash"] == "T"


def test_earnings_cooldown_is_stripped_before_hashing():
    """#4：引擎用不上的参数（财报单的冷却天数、出场参数）在哈希前规范化掉。"""
    ea = {"why": "x", "underlyings": ["NVDA"], "dte": 20, "entry": {"earnings_eve": True},
          "legs": [{"side": "buy", "type": "call", "strike_pct": 1.0}, {"side": "buy", "type": "put", "strike_pct": 1.0}]}
    hs = set()
    for cd in (1, 2, 5):
        n, why = lab.validate_proposal(dict(ea, cooldown_days=cd))
        assert n and n["cooldown_days"] == 1
        hs.add(lab.spec_hash(n))
    assert len(hs) == 1 and lab.spec_hash(lab.validate_proposal(ea)[0]) in hs
    n_, why = lab.validate_proposal(dict(ea, cooldown_days=3))                 # 不在档上的照样拒绝
    assert n_ is None and "cooldown_days" in why
    normal = put_spread()
    assert lab.validate_proposal(dict(normal, cooldown_days=2))[0]["cooldown_days"] == 2 and lab.spec_hash(lab.validate_proposal(dict(normal, cooldown_days=2))[0]) != lab.spec_hash(lab.validate_proposal(normal)[0])


# ---------------------------------------------------------------- 平稳块自助法
def test_block_bootstrap_own_vs_arch_similar_p():
    """#7：平稳块自助（平均块长 4 周）。装了 arch 就和 arch 的结果比；自带实现对自相关序列给出比整周 iid 自助更大的 p。"""
    rng = np.random.default_rng(4)
    e = rng.normal(0, 1, 120)
    x = np.convolve(e, np.ones(5) / 5, mode="valid")[:79] + 0.12                  # 强自相关、均值略正
    p_own = lab.block_boot_p(x, impl="own", n=20000, seed=1)
    assert 0.0 < p_own < 1.0
    assert lab.block_boot_p(x, impl="own", n=20000, seed=1) == p_own                # 种子固定可复现
    p_iid = v2.boot_p(list(x), n=20000)
    assert p_own > p_iid                                                           # 块自助保留了自相关，p 更保守（更大）
    arch = pytest.importorskip("arch.bootstrap")
    assert lab._arch_bootstrap_cls() is arch.StationaryBootstrap
    p_arch = lab.block_boot_p(x, impl="arch", n=20000, seed=1)
    assert abs(p_arch - p_own) < 0.04, (p_arch, p_own)
    assert lab.block_boot_p(x) == pytest.approx(lab.block_boot_p(x, impl="arch"))   # 默认：装了 arch 就用 arch


def test_block_bootstrap_edge_cases_and_fallback(monkeypatch):
    zero = np.random.default_rng(5).normal(0, 1, 79)
    ps = [lab.block_boot_p(np.random.default_rng(s).normal(0, 1, 79), impl="own", n=2000, seed=s) for s in range(30)]
    assert 0.35 < float(np.mean(ps)) < 0.65                                         # 纯噪声：p 均匀，平均约 0.5
    assert lab.block_boot_p(np.random.default_rng(6).normal(1.5, 1, 79), impl="own", n=2000) < 0.01
    assert lab.block_boot_p([1.0], impl="own") is None and lab.block_boot_p([], impl="own") is None
    monkeypatch.setattr(lab, "_arch_bootstrap_cls", lambda: None)                  # arch 导不进来：默认走自带实现，不报错
    assert lab.block_boot_p(zero, n=2000) == lab.block_boot_p(zero, impl="own", n=2000)
    with pytest.raises(RuntimeError):
        lab.block_boot_p(zero, impl="arch", n=100)


def test_dev_boot_p_uses_stationary_block_bootstrap(monkeypatch):
    seen = []
    monkeypatch.setattr(lab, "block_boot_p", lambda x, *a, **k: seen.append(len(x)) or 0.1234)
    trades = [{"und": "SPY", "entry_date": "2024-03-01", "exit_date": "2024-03-08", "pnl": 10.0, "max_loss": 300.0, "ret": 0.03, "entry_idx": 1, "reason": "take_profit", "legs": "x"}]
    d, *_ = lab._dev_metrics({"trades": trades, "per_und": {}, "skips": Counter(), "unresolved": 0}, lab.validate_proposal(put_spread())[0])
    assert d["boot_p"] == 0.1234 and seen == [79]
    assert lab.BOOT_MEAN_BLOCK == 4


# ---------------------------------------------------------------- 开发段回测：硬锁
def test_run_dev_basic_metrics_and_log(env):
    r = lab.run_dev(put_spread(), "alice", env)
    assert r["valid"] and not r["duplicate"] and r["hash"] and r["dev"]["n_trades"] >= 20 and r["status"] == "scored"
    d = r["dev"]
    assert d["complexity"] == 2 and d["n_weeks"] == len(lab.DEV_WEEKS) == 79 and d["n_clusters"] >= 10
    line = lab.read_log(env)[0]
    assert len(line["weekly"]) == 79 and sum(line["weekly"]) == pytest.approx(d["total_pnl"], abs=0.2)
    assert set(d["overlap"]) == {"2024-08-01..2024-08-16", "2025-04-01..2025-04-30"}
    assert d["n_trials"] == 61 and lab.n_trials(env) == 61                       # 种子 60 + 本次 1
    assert d["sharpe_ann"] == pytest.approx(d["sharpe_wk"] * math.sqrt(52)) and d["sr"] == d["sharpe_wk"]
    assert d["dsr_prob"] == pytest.approx(NORMAL.cdf(d["dsr"])) and 0 < d["boot_p"] <= 1
    trials = [json.loads(l) for l in env.trials_path.read_text().splitlines()]
    assert trials[-1]["kind"] == "dev" and trials[-1]["who"] == "alice" and trials[-1]["hash"] == r["hash"] and trials[-1]["ts"]
    assert line["who"] == "alice" and line["valid"] and line["spec"]["structure"] == "vertical" and line["backtested"] and line["status"] == "scored"
    # 开发段指标和直接用引擎算的一致（引擎复用，不是复制）
    norm, _ = lab.validate_proposal(put_spread())
    sim = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
    assert d["n_trades"] == len(sim["trades"]) and d["total_pnl"] == pytest.approx(round(sum(t["pnl"] for t in sim["trades"]), 2))
    ov = v2.window_pnl(sim["trades"], "2024-08-01", "2024-08-16")["overlap_pnl"]
    assert d["overlap"]["2024-08-01..2024-08-16"] == ov
    assert d["worst_trade"]["pnl"] == min(t["pnl"] for t in sim["trades"])
    assert "DSR" not in r["points_reason"] and "置信度" in r["points_reason"]


def test_dev_reads_no_quote_from_july_2025_and_closes_open_positions_at_0630(tmp_path, shared_opra, env, monkeypatch):
    """#10：run_dev 一条 7 月以后的报价都不读（连文件都不打开）；2025-06-30 仍开着的仓位用 06-30 主口径价格强制平仓。"""
    norm, _ = lab.validate_proposal(put_spread())
    opened = []
    orig = v2.read_month
    monkeypatch.setattr(v2, "read_month", lambda path: opened.append(Path(path).stem) or orig(path))
    base = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
    assert opened and max(opened) == "2025-06"                                       # 7 月及以后的月文件一个都没打开
    assert base["trades"] and all(t["entry_date"] <= lab.DEV_END and t["exit_date"] <= lab.DEV_END for t in base["trades"])
    assert max(t["entry_date"] for t in base["trades"]) >= "2025-06-01"
    forced = [t for t in base["trades"] if t["reason"] == "open_at_end"]
    assert forced and all(t["exit_date"] == lab.DEV_END for t in forced)             # 强制平仓：都在 06-30
    full = lab._simulate(norm, env, SPEC)
    assert any(t["entry_date"] >= lab.VAULT_START for t in full["trades"]) and any(t["exit_date"] > lab.DEV_END for t in full["trades"])    # 对照：不设限就会跑到 7 月
    # 7 月及以后的 parquet 全换成垃圾、报价全改坏：开发段结果逐笔不变（打开了就会抛异常）
    root2 = tmp_path / "junk" / "opra"
    shutil.copytree(shared_opra, root2, symlinks=True)
    for m in ("2025-07", "2025-08", "2025-09"):
        (root2 / "cbbo1m" / "SPY" / f"{m}.parquet").write_bytes(b"junk, opening this means the vault leaked")
    (tmp_path / "e2").mkdir()
    env2 = make_env(tmp_path / "e2", root2)
    t2 = lab._simulate(norm, env2, SPEC, lab.DEV_START, lab.DEV_END)["trades"]
    assert [(t["entry_date"], t["exit_date"], t["pnl"]) for t in t2] == [(t["entry_date"], t["exit_date"], t["pnl"]) for t in base["trades"]]
    with pytest.raises(Exception):
        lab._simulate(norm, env2, SPEC)                                                # 对照：金库口径要读它们
    r = lab.run_dev(put_spread(), "alice", env2)                                       # 完整的 run_dev 也一样
    assert r["status"] == "scored" and r["dev"]["n_trades"] == len(base["trades"])


def test_forced_close_at_0630_uses_primary_prices_or_max_loss(tmp_path, env):
    norm, _ = lab.validate_proposal(put_spread())
    base = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
    f = [t for t in base["trades"] if t["reason"] == "open_at_end" and t["entry_date"] < lab.DEV_END]
    assert f
    t0 = f[0]
    # 06-30 的报价整体抬高：强制平仓买回更贵 -> 这几笔亏得更多，其它笔不变
    def pricey(d, row):
        return dict(row, bid_px=row["bid_px"] + 0.5, ask_px=row["ask_px"] + 0.5) if d == lab.DEV_END else row
    root2 = tmp_path / "pricey" / "opra"
    build_data(root2, mutate=pricey)
    (tmp_path / "e2").mkdir()
    alt = lab._simulate(norm, make_env(tmp_path / "e2", root2), SPEC, lab.DEV_START, lab.DEV_END)["trades"]
    by = {(t["entry_date"], t["exit_date"]): t for t in alt}
    for t in f:
        assert by[(t["entry_date"], t["exit_date"])]["pnl"] != t["pnl"]
    assert [t["pnl"] for t in base["trades"] if t["reason"] != "open_at_end"] == [t["pnl"] for t in alt if t["reason"] != "open_at_end"]
    # 06-30 没有报价：按最大亏损（信用价差 pnl = −max_loss − 平仓手续费 1.3）
    root3 = tmp_path / "noquote" / "opra"
    build_data(root3, mutate=lambda d, row: None if d == lab.DEV_END else row)
    (tmp_path / "e3").mkdir()
    nq = lab._simulate(norm, make_env(tmp_path / "e3", root3), SPEC, lab.DEV_START, lab.DEV_END)["trades"]
    mx = [t for t in nq if t["reason"] == "no_more_quotes"]
    assert mx and all(t["exit_date"] <= lab.DEV_END and t["pnl"] == pytest.approx(-t["max_loss"] - 1.3, abs=0.01) for t in mx)
    assert t0["entry_date"] in {t["entry_date"] for t in mx}


def test_dev_window_ends_exactly_at_cutoff(env):
    for mh in (10, 25):
        norm, _ = lab.validate_proposal(put_spread(max_hold_days=mh))
        s = lab._simulate(norm, env, SPEC, lab.DEV_START, lab.DEV_END)
        assert max(t["exit_date"] for t in s["trades"]) <= lab.DEV_END and max(t["entry_date"] for t in s["trades"]) <= lab.DEV_END
        assert s["last_day"] == lab.DEV_END


def test_duplicate_is_not_run_not_counted_and_scored_minus_one(env):
    r1 = lab.run_dev(put_spread(), "alice", env)
    n1 = lab.n_trials(env)
    assert n1 == 61
    r2 = lab.run_dev(put_spread(why="other words", name="again"), "bob", env)         # 别人提同一个方案
    assert r2["duplicate"] and r2["points"] == -1.0 and r2["dev"] is None and r2["hash"] == r1["hash"] and r2["status"] == "duplicate"
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
    assert set(r["dev"]["crash_by_und"]) <= {"SPY", "QQQ"}


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
    assert h.count("\n") == 2 and "bob" not in h and "trades" in h and "置信度" in h and "DSR" not in h and "INVALID" in h
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
    # 金库口径 = 全样本，考试段入场确实在 7 月以后
    norm, _ = lab.validate_proposal(put_spread())
    full = lab._simulate(norm, env, SPEC)
    assert v[0]["eval"]["test"]["trades"] == len([t for t in full["trades"] if t["entry_date"] >= lab.VAULT_START])
    # 同一个哈希只考一次：再考不加试验、不重跑
    again = lab.vault_exam(r["hash"], env)
    assert again == out and lab.n_trials(env) == n_before + 1 and len(env.vault_path.read_text().splitlines()) == 1
    with pytest.raises(ValueError):
        lab.vault_exam("deadbeefdeadbeef", env)
    assert "vault: " in lab.history_for("alice", 5, env)


def test_vault_error_records_trial_and_error_line_and_blocks_promotion(env, monkeypatch):
    """#9：金库考试出错 -> 记试验 + 错误行（vault_error），该哈希以后不再晋级；占用本 ISO 周。"""
    r = lab.run_dev(put_spread(), "alice", env)
    other = lab.run_dev(put_spread(take_profit=0.75), "bob", env)
    n0 = lab.n_trials(env)
    real = lab._simulate

    def boom(norm, env_, spec, entry_first=None, entry_last=None):
        if entry_last is None:
            raise RuntimeError("parquet exploded")
        return real(norm, env_, spec, entry_first, entry_last)
    monkeypatch.setattr(lab, "_simulate", boom)
    now = datetime(2025, 10, 6, 12, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError):
        lab.vault_exam(r["hash"], env, now=now)
    assert lab.n_trials(env) == n0 + 1                                                  # 试验照记
    trials = [json.loads(l) for l in env.trials_path.read_text().splitlines()]
    assert trials[-1]["kind"] == "vault" and trials[-1]["hash"] == r["hash"]
    v = [json.loads(l) for l in env.vault_path.read_text().splitlines()]
    assert len(v) == 1 and v[0]["vault_error"] is True and v[0]["hash"] == r["hash"] and "parquet exploded" in v[0]["error"] and v[0]["passed"] is False and v[0]["iso_week"] == "2025-W41"
    assert lab.vault_exam(r["hash"], env) == {"hash": r["hash"], "passed": False} and lab.n_trials(env) == n0 + 1     # 不重试、不再加试验
    assert "vault: ERROR" in lab.history_for("alice", 5, env) and "PASSED" not in lab.history_for("alice", 5, env)
    st = lab.status(env)
    assert st["vault_errors"] == 1 and st["vault_exams"] == 0 and st["vault_passed"] == 0
    # 晋级：本周已占用；下周起这个哈希永远跳过，轮到别的候选
    monkeypatch.setattr(lab, "_simulate", real)
    called = []
    monkeypatch.setattr(lab, "vault_exam", lambda h, env=None, now=None: called.append(h) or {"hash": h, "passed": False})
    monkeypatch.setattr(lab, "current_pbo", lambda env=None, log=None: 0.1)
    monkeypatch.setattr(lab, "current_scores", lambda log, N: ({r["hash"]: {"z": 9.0, "prob": 1.0, "promotable": True},
                                                                other["hash"]: {"z": 3.0, "prob": 0.99, "promotable": True}}, 0.1))
    assert "already promoted" in lab.weekly_promotion(env, now=now)["skipped"] and called == []
    assert lab.weekly_promotion(env, now=datetime(2025, 10, 13, 12, tzinfo=timezone.utc)) == {"hash": other["hash"], "passed": False} and called == [other["hash"]]


def test_weekly_promotion_catches_vault_error(env, monkeypatch):
    r = lab.run_dev(put_spread(), "alice", env)
    monkeypatch.setattr(lab, "current_pbo", lambda env=None, log=None: 0.1)
    monkeypatch.setattr(lab, "current_scores", lambda log, N: ({r["hash"]: {"z": 9.0, "prob": 1.0, "promotable": True}}, 0.1))
    real = lab._simulate
    monkeypatch.setattr(lab, "_simulate", lambda norm, env_, spec, entry_first=None, entry_last=None: (_ for _ in ()).throw(OSError("disk")) if entry_last is None else real(norm, env_, spec, entry_first, entry_last))
    res = lab.weekly_promotion(env, now=datetime(2025, 10, 6, tzinfo=timezone.utc))
    assert "hash" not in res and "disk" in res["error"] and res["vault_error_hash"] == r["hash"]
    assert json.loads(env.vault_path.read_text().splitlines()[0])["vault_error"] is True


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


# ---------------------------------------------------------------- 数据不全：待定与补数据后重算
def test_incomplete_run_is_pending_not_eligible_then_rescored_without_new_trial(tmp_path):
    """#8：> 5% 的笔以 no_more_quotes / stop_on_missing 收尾 -> incomplete、0 分待定、不合格；补数据后 rescore 不加试验、新行取代旧行（旧行留着）。"""
    root = tmp_path / "opra"
    build_data(root, mutate=lambda d, row: None if d >= "2024-06-03" else row)
    (tmp_path / "e").mkdir()
    env = make_env(tmp_path / "e", root)
    r = lab.run_dev(put_spread(), "alice", env)
    assert r["status"] == "incomplete" and r["points"] == 0.0 and "数据不全" in r["points_reason"] and r["dev"]["data_incomplete"] is True
    n1 = lab.n_trials(env)
    assert n1 == 61                                                                    # 回测跑了，试验计 1 次
    assert lab.best_dev(env=env) is None and lab.top_dev(5, env) == [] and lab.current_scores(lab.read_log(env), n1)[0] == {}
    pend = lab.pending_rescore(env)
    assert [p["hash"] for p in pend] == [r["hash"]] and pend[0]["who"] == "alice" and pend[0]["bad_frac"] > 0.05
    assert lab.status(env)["pending_rescore"] == 1 and "PENDING" in lab.history_for("alice", 3, env)
    # 数据还是不全：重算也不动日志、不加试验
    assert lab.rescore_pending(env) == [{"hash": r["hash"], "status": "incomplete", "points": 0.0, "points_reason": lab.REASON_INCOMPLETE}]
    assert len(lab.read_log(env)) == 1 and lab.n_trials(env) == n1
    # 补数据（重建完整数据）：rescore 重新打分
    build_data(root)
    out = lab.rescore_pending(env)
    assert len(out) == 1 and out[0]["hash"] == r["hash"] and out[0]["status"] == "scored" and out[0]["points"] != 0.0
    assert lab.n_trials(env) == n1                                                     # 不加试验
    raw = lab.read_log(env)
    assert len(raw) == 2 and raw[0]["status"] == "incomplete" and raw[1]["rescored"] is True and raw[1]["supersedes"] == [raw[0]["ts"]]    # 旧行保留，新行取代
    assert [e["status"] for e in lab._eff(raw)] == ["scored"] and lab.pending_rescore(env) == []
    assert lab.best_dev(env=env)["hash"] == r["hash"] and lab.status(env)["pending_rescore"] == 0
    assert lab.history_for("alice", 5, env).count("\n") == 0 and "PENDING" not in lab.history_for("alice", 5, env)     # 历史只显示取代后的那行
    assert lab.rescore_pending(env) == []                                              # 没有待处理的了


# ---------------------------------------------------------------- 晋级
def test_weekly_promotion_one_per_iso_week_and_picks_max_current_z_not_points(env, monkeypatch):
    rng = np.random.default_rng(11)
    lab.ensure_ledger(env)
    for k in range(28):
        lab._append(env.log_path, crafted_entry(f"noise{k:02d}", rng.normal(0, 1, 79)))
    lab._append(env.log_path, crafted_entry("strongA", rng.normal(2.5, 1, 79), points=1.0))          # z 最高、得分低
    lab._append(env.log_path, crafted_entry("strongB", rng.normal(2.0, 1, 79), points=3.0))          # z 次高、得分高
    calls = []

    def fake_vault(h, env=None, now=None):
        calls.append(h)
        lab._append(env.vault_path, {"hash": h, "passed": True, "iso_week": v2.week_key(now.date().isoformat())})
        lab.record_trial(env, "vault", "w", h, now)
        return {"hash": h, "passed": True}
    monkeypatch.setattr(lab, "vault_exam", fake_vault)
    assert lab.current_pbo(env) is not None and lab.current_pbo(env) <= 0.30
    wed = datetime(2025, 10, 1, 12, tzinfo=timezone.utc)                   # 2025-W40
    assert lab.weekly_promotion(env, now=wed) == {"hash": "strongA", "passed": True}           # 按当前 z，不按得分
    thu = datetime(2025, 10, 2, 12, tzinfo=timezone.utc)
    sk = lab.weekly_promotion(env, now=thu)
    assert "skipped" in sk and "2025-W40" in sk["skipped"] and calls == ["strongA"]            # 同一个 ISO 周不再来
    nxt = datetime(2025, 10, 8, 12, tzinfo=timezone.utc)                   # 2025-W41
    assert lab.weekly_promotion(env, now=nxt) == {"hash": "strongB", "passed": True}           # strongA 已考过，轮到 B
    third = datetime(2025, 10, 15, 12, tzinfo=timezone.utc)
    sk3 = lab.weekly_promotion(env, now=third)
    assert "skipped" in sk3 and "no unvaulted candidate" in sk3["skipped"] and "置信度" in sk3["skipped"] and calls == ["strongA", "strongB"]
    assert lab.status(env)["vault_exams"] == 2


def test_weekly_promotion_bar_z_1645_and_pbo_030(env, monkeypatch):
    """#6：z ≥ 1.645（dsr_prob ≥ 0.95）且 PBO ≤ 0.30。"""
    called = []
    monkeypatch.setattr(lab, "vault_exam", lambda h, env=None, now=None: called.append(h) or {"hash": h, "passed": False})
    now = datetime(2025, 10, 1, tzinfo=timezone.utc)

    def with_scores(sc):
        monkeypatch.setattr(lab, "current_scores", lambda log, N: (sc, 0.1))
    monkeypatch.setattr(lab, "current_pbo", lambda env=None, log=None: 0.0)
    with_scores({"h": {"z": 1.644, "prob": 0.9499, "promotable": True}})
    assert "no unvaulted candidate" in lab.weekly_promotion(env, now=now)["skipped"] and called == []
    with_scores({"h": {"z": 1.6449, "prob": 0.95005, "promotable": True}})                       # prob 够了但 z 还没到 1.645：不行
    assert "skipped" in lab.weekly_promotion(env, now=now) and called == []
    with_scores({"h": {"z": 1.645, "prob": 0.94, "promotable": True}})                           # z 到了但 prob < 0.95：不行
    assert "skipped" in lab.weekly_promotion(env, now=now) and called == []
    with_scores({"h": {"z": 3.0, "prob": 0.99, "promotable": False}})                            # 近似重复：不能晋级
    assert "skipped" in lab.weekly_promotion(env, now=now) and called == []
    with_scores({"h": {"z": 1.7, "prob": 0.955, "promotable": True}})
    for pb, ok in ((0.31, False), (0.3001, False), (None, False), (0.30, True)):
        monkeypatch.setattr(lab, "current_pbo", lambda env=None, log=None, pb=pb: pb)
        res = lab.weekly_promotion(env, now=now)
        if ok:
            assert res == {"hash": "h", "passed": False} and called == ["h"]
        else:
            assert "PBO" in res["skipped"] and called == []
    # 晋级后写进 vault.jsonl（这里 vault_exam 是假的，不写）：真实 z 数值的边界交给上面的 z / prob 双条件
    assert lab.Z_PROMOTE == 1.645 and lab.PROB_PROMOTE == 0.95 and lab.PBO_MAX == 0.30
    assert NORMAL.cdf(lab.Z_PROMOTE) >= 0.95


def test_weekly_promotion_gates_candidates(env, monkeypatch):
    """合格 / 数据完整 / 近似重复 / 样本太少的方案都进不了候选；候选的 dsr_prob 随结果存好。"""
    rng = np.random.default_rng(5)
    called = []
    monkeypatch.setattr(lab, "vault_exam", lambda h, env=None, now=None: called.append(h) or {"hash": h, "passed": False})
    monkeypatch.setattr(lab, "current_pbo", lambda env=None, log=None: 0.1)
    now = datetime(2025, 10, 1, tzinfo=timezone.utc)
    assert "skipped" in lab.weekly_promotion(env, now=now)                  # 日志是空的
    lab._append(env.log_path, crafted_entry("tiny", rng.normal(3, 1, 79), n_trades=19))
    lab._append(env.log_path, crafted_entry("fewclusters", rng.normal(3, 1, 79), n_clusters=9))
    lab._append(env.log_path, crafted_entry("pending", rng.normal(3, 1, 79), status="incomplete"))
    lab._append(env.log_path, crafted_entry("dup", rng.normal(3, 1, 79), status="near_duplicate"))
    lab._append(env.log_path, crafted_entry("flat", np.zeros(79)))
    assert "no unvaulted candidate" in lab.weekly_promotion(env, now=now)["skipped"] and called == []
    lab._append(env.log_path, crafted_entry("good", rng.normal(3, 1, 79)))
    assert lab.weekly_promotion(env, now=now) == {"hash": "good", "passed": False} and called == ["good"]


def test_status_shape(env):
    lab.run_dev(put_spread(), "a", env)
    lab.run_dev({"why": "x"}, "a", env)
    st = lab.status(env)
    assert st["n_trials"] == 61 and st["proposals"] == 2 and st["invalid"] == 1 and st["backtested"] == 1 and st["vault_exams"] == 0
    assert st["eligible"] == 1 and st["pending_rescore"] == 0 and st["best_confidence"] > 0.9 and "pbo" in st and "best_dev_dsr" in st


# ---------------------------------------------------------------- CLI：同一把锁 / 精简环境变量 / 内存上限
def _write(tmp_path, obj, name="p.json"):
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return str(p)


def test_cli_validate_status_dev_promote_rescore(tmp_path, capsys, env):
    p = _write(tmp_path, put_spread())
    assert lab.main(["--validate", p], env=env, limits=False) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["hash"] == lab.spec_hash(out["normalized"])
    p2 = _write(tmp_path, {"why": "x", "underlyings": ["SPY"], "dte": 30, "legs": [{"side": "sell", "type": "call", "strike_pct": 1.0}]}, "bad.json")
    assert lab.main(["--validate", p2], env=env, limits=False) == 1
    assert "naked_short" in json.loads(capsys.readouterr().out)["reason"]
    assert lab.main(["--status"], env=env, limits=False) == 0
    assert json.loads(capsys.readouterr().out)["n_trials"] == 60
    assert lab.main(["--dev", p, "--who", "cli"], env=env, limits=False) == 0
    assert json.loads(capsys.readouterr().out)["dev"]["n_trades"] > 0
    assert lab.main(["--promote"], env=env, limits=False) == 0
    assert "skipped" in json.loads(capsys.readouterr().out)
    assert lab.main(["--rescore"], env=env, limits=False) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_takes_the_round_lock_shared_with_lab_room(tmp_path, capsys, env):
    from desk import lab_room
    p = _write(tmp_path, put_spread())
    with lab_room.round_lock(env) as got:
        assert got
        for argv in (["--dev", p, "--who", "cli"], ["--promote"], ["--rescore"]):
            assert lab.main(argv, env=env, limits=False) == 0
            assert json.loads(capsys.readouterr().out) == {"skipped": "another round is running"}
        assert lab.read_log(env) == [] and lab.n_trials(env) == 60                   # 什么都没写
        assert lab.main(["--status"], env=env, limits=False) == 0                    # 只读的不加锁
        capsys.readouterr()
    assert lab.main(["--dev", p, "--who", "cli"], env=env, limits=False) == 0        # 锁放开后可以跑
    assert len(lab.read_log(env)) == 1
    capsys.readouterr()
    with lab_room.round_lock(env) as got:                                              # CLI 跑完把锁放了
        assert got


def test_cli_applies_limits_and_reexecs_with_lean_env(tmp_path, capsys, env, monkeypatch):
    from desk import lab_room
    p = _write(tmp_path, put_spread())
    monkeypatch.setenv("DESK_DATA", str(env.data_dir))
    applied = []
    monkeypatch.setattr(lab_room, "apply_limits", lambda mb, niceness=19: applied.append(mb))
    for k in lab_room.LEAN_ENV:
        monkeypatch.setenv(k, lab_room.LEAN_ENV[k])
    monkeypatch.delenv(lab.LEAN_REEXEC_FLAG, raising=False)
    assert lab.main(["--dev", p, "--who", "cli"], env=env, limits=True) == 0          # 精简环境变量齐全：不重启，直接加限制后跑
    capsys.readouterr()
    assert applied == [lab_room.DEFAULTS["mem_limit_mb"]]
    assert lab.main(["--status"], env=env, limits=True) == 0 and len(applied) == 1     # 只读命令不加限制
    capsys.readouterr()
    # 缺精简环境变量 + 真正的命令行入口（env=None）：带上环境变量重启自己一次（execve 被替换成记录）
    monkeypatch.delenv("ARROW_DEFAULT_MEMORY_POOL")
    seen = {}

    class Stop(Exception):
        pass

    def fake_execve(path, argv, envp):
        seen.update(argv=argv, envp=envp)
        raise Stop()
    monkeypatch.setattr(os, "execve", fake_execve)
    with pytest.raises(Stop):
        lab.main(["--promote"], limits=True)
    assert seen["argv"][1:4] == ["-m", "desk.options_lab", "--promote"] and seen["envp"]["ARROW_DEFAULT_MEMORY_POOL"] == "system" and seen["envp"][lab.LEAN_REEXEC_FLAG] == "1"
    monkeypatch.setenv(lab.LEAN_REEXEC_FLAG, "1")                                      # 已经重启过：不再重启（不会死循环）
    monkeypatch.setattr(lab, "default_env", lambda: env)
    assert lab.main(["--promote"], limits=True) == 0
    assert len(applied) == 2
