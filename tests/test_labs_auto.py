"""labs_auto：三条线归一化、排名、过门槛、缺一条线时的容错、网页、调度。全部用假输出，不联网。"""
import json
import random
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from desk import labs_auto as la
from desk import options_real_backtest as ob
from desk import pairs_lab as pl
from tests.test_web import env  # noqa: F401  (fixture)

WEEK = pl.WEEK_MS


def rec(key, ratio, n=30, pos=True, ruin=0.01, lev=2.0, ret=0.01):
    return la.make_record(key, "x", n, ratio * 0.01, 0.01, 0.1, lev, ret, ruin, None, pos, "n")


def test_make_record_units_and_gate():
    r = la.make_record("pairs", "A / B", 30, 0.015, 0.01, 0.12, 1.5, 0.0225, 0.02, 5000.0, True, "")
    assert r["mean_wk_pct"] == 1.5 and r["sd_wk_pct"] == 1.0 and abs(r["ratio"] - 1.5) < 1e-9
    assert r["max_dd_pct"] == 12.0 and r["ret_at_lev_pct"] == 2.25 and r["p_ruin_pct"] == 2.0
    assert r["passes"] is True and r["note_optimistic"] is True and r["line"] == "跨平台同股套利"


def test_passes_gate_each_condition():
    base = dict(n=20, oos_mean_pos=True, p_ruin=0.049)
    def mk(**kw):
        a = {**base, **kw}
        return la.make_record("options", "v", a["n"], 0.01, 0.02, 0.1, 1, 0.01, a["p_ruin"], None, a["oos_mean_pos"], "")["passes"]
    assert mk() is True
    assert mk(n=19) is False                 # 样本不够
    assert mk(oos_mean_pos=False) is False   # 样本外为负
    assert mk(p_ruin=0.05) is False          # 腰斩概率不 < 5%
    assert mk(p_ruin=None) is False          # 算不出 = 不过


def test_rank_by_ratio_none_last():
    a, b, c = rec("pairs", 0.5), rec("strategy", 2.0), rec("options", 1.0)
    c["ratio"] = None
    out = la.rank([a, c, b])
    assert [r["key"] for r in out] == ["strategy", "pairs", "options"]
    assert [r["rank"] for r in out] == [1, 2, 3]


def test_rank_failing_never_above_passing():
    hi_fail = rec("options", 9.0, n=5)          # 样本不够 → 不过
    lo_pass = rec("pairs", 0.1)
    out = la.rank([hi_fail, lo_pass])
    assert [r["key"] for r in out] == ["pairs", "options"]
    msg = la.message(out, {})
    assert msg.index("过门槛的：") < msg.index("没过的：") and msg.index("第1名 跨平台") < msg.index("第2名 期权")


def test_passes_requires_positive_leverage_and_return():
    f = lambda lev, ret: la.make_record("pairs", "v", 30, 0.01, 0.02, 0.1, lev, ret, 0.01, None, True, "")["passes"]  # noqa: E731
    assert f(1.0, 0.01) is True
    assert f(0.0, 0.01) is False and f(None, 0.01) is False
    assert f(1.0, 0.0) is False and f(1.0, -0.01) is False and f(1.0, None) is False


def test_normalize_strategy():
    st = {"n_weekends": 25, "mean": 0.004, "sd": 0.01, "max_dd": 0.08, "lev": 2.5, "ret_at_lev": 0.01, "p_ruin": 0.02}
    r = la.normalize_strategy({"stats": {"select": st}})
    assert r["n_weeks"] == 25 and r["mean_wk_pct"] == 0.4 and r["leverage"] == 2.5 and r["passes"] is True
    neg = la.normalize_strategy({"stats": {"select": {**st, "mean": -0.001, "lev": 0.0, "p_ruin": 0.0}}})
    assert neg["passes"] is False and neg["leverage"] == 0.0 and neg["ret_at_lev_pct"] == 0.0


def test_normalize_strategy_missing():
    for bad in (None, {}, {"stats": {"select": {"n_weekends": 0}}}):
        try:
            la.normalize_strategy(bad)
        except RuntimeError:
            continue
        raise AssertionError("should raise")


def fake_pairs_res(n_weeks=3):
    rng = random.Random(1)
    t0 = 1_700_000_000_000
    trades = [{"t_exit": t0 + w * WEEK + 1000, "net_usd": rng.uniform(1, 8)} for w in range(n_weeks)]
    st = {"n": len(trades), "weeks": n_weeks, "week_mean_usd": sum(t["net_usd"] for t in trades) / n_weeks,
          "avg_net_bps": 5.0, "max_dd_usd": -3.0, "leverage": 4.0, "week_ret_at_lev": 0.012,
          "p_minus50_52w": 0.0, "capacity_usd": 20000.0}
    good = {"a": "xyz:SNDK", "b": "io:SNDK", "result": {"ok": True, "oos_from": t0, "t_last": t0 + n_weeks * WEEK,
                                                        "taker": st, "trades_taker": trades, "legging": {"avg_net_bps": 2.0}}}
    bad = {"a": "x", "b": "y", "result": {"ok": False, "why": "no data"}}
    return {"pairs": [bad, good]}


def test_pairs_needs_legging_positive_and_notes_selection_bias():
    res = fake_pairs_res(25)
    assert la.normalize_pairs(res)["passes"] is True
    assert "选择偏差" in la.normalize_pairs(res)["note"]
    for lg in ({"avg_net_bps": -1.0}, {}, None):
        res = fake_pairs_res(25)
        res["pairs"][1]["result"]["legging"] = lg
        assert la.normalize_pairs(res)["passes"] is False


def test_normalize_pairs_uses_full_fee_and_percent_units():
    r = la.normalize_pairs(fake_pairs_res(), notional=1000.0)
    assert r["variant"] == "xyz:SNDK / io:SNDK" and r["n_weeks"] == 3
    assert r["max_dd_pct"] == 0.3                       # 3 / 1000 = 0.3%
    assert r["leverage"] == 4.0 and r["ret_at_lev_pct"] == 1.2 and r["capacity_usd"] == 20000.0
    assert r["mean_wk_pct"] > 0 and r["sd_wk_pct"] > 0
    assert r["passes"] is False                          # 只有 3 周
    try:
        la.normalize_pairs({"pairs": [{"a": "x", "b": "y", "result": {"ok": False}}]})
    except RuntimeError:
        pass
    else:
        raise AssertionError


def fake_option_rows(n=40, edge=0.05):
    rng = random.Random(3)
    rows = []
    for i in range(n):
        base = {"entry_kind": "FRI7", "expiry": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "slippage_pct": ob.DEFAULT_SLIP,
                "variant": "hold"}
        r = edge + rng.uniform(-0.3, 0.3)
        rows.append({**base, "strategy": "iron_condor_15_05", "r": r, "pnl": r, "premium": 1, "max_risk": 1, "spot": 100})
        rows.append({**base, "strategy": "short_strangle_15_REF", "r": 5.0 + rng.random(), "pnl": 1, "premium": 1,
                     "max_risk": 1, "spot": 100})   # 参考线，必须被排除
        rows.append({**base, "slippage_pct": 10.0, "strategy": "iron_fly_10", "r": 9.0, "pnl": 1, "premium": 1,
                     "max_risk": 1, "spot": 100})   # 非默认滑点，必须被排除
    return rows


def test_normalize_options_excludes_ref_and_other_slippage():
    r = la.normalize_options(fake_option_rows())
    assert "铁鹰" in r["variant"] and r["n_weeks"] == 40
    assert r["mean_wk_pct"] > 0 and 0 < r["leverage"] <= 1.0 and r["note_optimistic"] is True
    assert r["ret_at_lev_pct"] is not None and abs(r["ret_at_lev_pct"] - r["leverage"] * r["mean_wk_pct"]) < 1e-9
    assert r["oos_mean_positive"] is True
    assert r["unit"] == "risk_budget" and "最大亏损预算" in r["note"]
    msg = la.message(la.rank([r]), {})
    assert "占每周最大亏损预算，不是占本金" in msg
    assert la.normalize_pairs(fake_pairs_res())["unit"] == "capital"
    try:
        la.normalize_options([])
    except RuntimeError:
        pass
    else:
        raise AssertionError


def test_run_all_isolates_failures_and_writes_files(env, monkeypatch):  # noqa: F811
    s, _ = env
    pushed = []
    from desk import notify
    monkeypatch.setattr(notify, "push", lambda settings, title, body, priority="default": pushed.append((title, body)) or True)

    def boom(settings, fetch):
        raise RuntimeError("deribit 不可达")
    runners = {"pairs": lambda st, f: rec("pairs", 1.0), "strategy": boom, "options": lambda st, f: rec("options", 2.0, n=5)}
    out = la.run_all(s, fetch=False, runners=runners)
    assert [r["key"] for r in out["ranking"]] == ["pairs", "options"]
    assert "strategy" in out["errors"] and "deribit" in out["errors"]["strategy"]
    from pathlib import Path
    d = Path(s.data) / "labs"
    latest = json.loads((d / "latest.json").read_text())
    assert latest["errors"].keys() == {"strategy"} and latest["generated_at"]
    assert len(list(d.glob("20*.json"))) == 1
    title, body = pushed[0]
    assert "第1名 跨平台同股套利" in body and "第2名 期权周卖组合" in body and "过门槛的：" in body and "没过的：" in body
    assert "周末套利升级版：这次没跑出来" in body


def test_all_labs_fail_still_writes(env, monkeypatch):  # noqa: F811
    s, _ = env
    def boom(settings, fetch):
        raise ValueError("x")
    out = la.run_all(s, fetch=False, runners={"pairs": boom, "options": boom}, push=False)
    assert out["ranking"] == [] and set(out["errors"]) == {"pairs", "options"}
    assert la.load_latest(s)["ranking"] == []


def test_labs_page(env):  # noqa: F811
    s, web = env
    c = TestClient(web.app)
    assert c.get("/labs", follow_redirects=False).status_code in (302, 303)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    r = c.get("/labs")
    assert r.status_code == 200 and "赚钱排行" in r.text and "还没跑过" in r.text
    la.run_all(s, fetch=False, runners={"pairs": lambda st, f: rec("pairs", 1.0), "options": lambda st, f: (_ for _ in ()).throw(RuntimeError("坏了"))},
               push=False)
    r = c.get("/labs")
    assert r.status_code == 200
    assert "第1名 跨平台同股套利" in r.text and "每一列是什么意思" in r.text and "坏了" in r.text and "href=\"/labs\"" in r.text


def test_scheduler_weekly_job(env, monkeypatch):  # noqa: F811
    s, _ = env
    from desk import scheduler
    mon = datetime(2026, 10, 19, 13, 5, tzinfo=timezone.utc)   # 周一
    keys = [k for k, _ in scheduler.due_jobs(mon, s, set())]
    assert "labs:2026-10-19" in keys
    assert "labs:2026-10-19" not in [k for k, _ in scheduler.due_jobs(mon, s, {"labs:2026-10-19"})]
    for t in (datetime(2026, 10, 19, 12, 59, tzinfo=timezone.utc), datetime(2026, 10, 20, 13, 5, tzinfo=timezone.utc)):
        assert not [k for k, _ in scheduler.due_jobs(t, s, set()) if k.startswith("labs:")]
    started = []
    monkeypatch.setattr(scheduler, "start_labs_auto", lambda st: started.append(1))
    scheduler.run_job("labs:2026-10-19", None, s)
    assert started == [1]


def test_lock_second_run_exits(env):  # noqa: F811
    s, _ = env
    from pathlib import Path
    d = Path(s.data) / "labs"
    d.mkdir(parents=True, exist_ok=True)
    seen = []

    def inner(settings, fetch):
        out2 = la.run_all(settings, fetch=False, runners={"pairs": lambda st, f: seen.append("second") or rec("pairs", 1.0)}, push=False)
        seen.append(out2)
        return rec("pairs", 1.0)
    out = la.run_all(s, fetch=False, runners={"pairs": inner}, push=False)
    assert seen == [{"skipped": True, "ranking": [], "errors": {}}]    # 第二次没跑 runner
    assert len(out["ranking"]) == 1 and not (d / "run.lock").exists()   # 锁已释放


def test_stale_lock_is_replaced(env):  # noqa: F811
    import os
    from pathlib import Path
    s, _ = env
    d = Path(s.data) / "labs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.lock").write_text("1")
    old = os.stat(d / "run.lock").st_mtime - la.LOCK_STALE_S - 10
    os.utime(d / "run.lock", (old, old))
    out = la.run_all(s, fetch=False, runners={"pairs": lambda st, f: rec("pairs", 1.0)}, push=False)
    assert not out.get("skipped") and len(out["ranking"]) == 1


def test_labs_page_tolerates_missing_fields(env):  # noqa: F811
    from pathlib import Path
    s, web = env
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    d = Path(s.data) / "labs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(json.dumps({"generated_at": "2026-10-19T13:00:00", "ranking": [{"rank": 1, "line": "旧格式"}], "errors": {}}))
    r = c.get("/labs")
    assert r.status_code == 200 and "旧格式" in r.text
