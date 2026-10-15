"""策略实验室（desk/strategy_lab.py）：走前不偷看、固定变体方向与全额手续费、按日历周末计样本、判定、端到端离线。"""
import json
import math
import random
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from desk import backtest_lib as bl
from desk import config
from desk import strategy_lab as sl

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
FEE = 14.0


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    return config.load(tmp_path)


def fridays(n, start=date(2026, 3, 6)):
    return [(start + timedelta(days=7 * i)).isoformat() for i in range(n)]


def row(fri, name="NVDA", fam="美股单票", dev=60.0, move=20.0, news=None):
    return {"fri": fri, "name": name, "family": fam, "gran": "1h", "dev_bps": dev, "move_bps": move,
            "y_bps": dev + move, "news": news}


# ---------------------------------------------------------------- 走前不偷看
def test_walk_forward_does_not_peek_at_current_weekend():
    """前 11 个周末顺势小赚（+20 bps），第 12 个周末顺势巨亏（−1000）。
    偷看本周末的实现会在第 12 周末改成反向 / 挑反向变体、躲过这笔亏损；不偷看的必须仍然顺势、吃下亏损。"""
    fr = fridays(12)
    rows = [row(f, move=20.0) for f in fr[:11]] + [row(fr[11], move=-1000.0)]
    dec = sl.decisions(rows, FEE, min_weekends=8)
    last = fr[11]
    assert dec[("regime4", last, "NVDA")] == 1          # 过去 4 周末 Σ sign·move = +80 → 顺势（偷看会是 +80−1000 < 0 → 反向）
    assert dec[("regime8", last, "NVDA")] == 1
    pick = sl.wf_choice(sl._past(rows, last), "美股单票", FEE, 8)
    assert pick.startswith("follow")                    # 偷看的话顺势全部变负、会挑 fade
    assert dec[("wf_select", last, "NVDA")] == 1
    # 而"偷看版"确实会给出不同答案（证明这个夹具能抓到偷看）
    peek = [r for r in rows if r["fri"] <= last]
    assert sl.wf_choice(peek, "美股单票", FEE, 8).startswith("fade")
    from desk.model import regime_pick
    assert regime_pick([{"dev_bps": r["dev_bps"], "move_bps": r["move_bps"]} for r in peek], 4)["pick"] == "fade"
    # 吃下的那笔：顺势 −1000 − 14
    res = sl.evaluate_rows(rows, {"costs": {"maker_bps": 3.0, "taker_bps": 9.0, "edge_buffer_bps": 2.0}}, warmup=8, half_spread=2.0)
    t = [x for x in res["trades"]["regime4"] if x["fri"] == last][0]
    assert t["side"] == 1 and t["net_bps"] == pytest.approx(-1014.0) and t["oos"]


def test_changing_future_data_never_changes_past_or_current_decisions():
    random.seed(7)
    fr = fridays(14)
    names = [("NVDA", "美股单票"), ("TSLA", "美股单票"), ("XYZ100", "美指")]
    rows = [row(f, n, fam, dev=random.gauss(0, 80), move=random.gauss(0, 40)) for f in fr for n, fam in names]
    base = sl.decisions(rows, FEE, 4)
    for j in range(len(fr)):
        cut = fr[j]
        pert = []
        for r in rows:
            r2 = dict(r)
            if r["fri"] > cut:      # 未来：dev 和 move 全改
                r2["dev_bps"], r2["move_bps"] = random.gauss(0, 200), random.gauss(0, 200)
            elif r["fri"] == cut:   # 本周末：结果（move）改掉；决策只能用 dev 和过去
                r2["move_bps"] = -r["move_bps"] * 50
            pert.append(r2)
        new = sl.decisions(pert, FEE, 4)
        for k, v in base.items():
            if k[1] <= cut:
                assert new[k] == v, (cut, k)


# ---------------------------------------------------------------- 固定变体 / 新闻 / 成本
def test_fixed_variant_sides_thresholds_and_full_fee():
    r = row("2026-09-04", dev=60.0, move=-20.0)
    assert sl.fixed_side("fade16", r) == -1 and sl.fixed_side("fade50", r) == -1 and sl.fixed_side("fade100", r) == 0
    assert sl.fixed_side("follow50", r) == 1 and sl.fixed_side("follow150", r) == 0
    rn = row("2026-09-04", dev=-120.0, move=30.0)
    assert sl.fixed_side("fade100", rn) == 1 and sl.fixed_side("follow100", rn) == -1
    cfg = {"costs": {"maker_bps": 3.0, "taker_bps": 9.0, "edge_buffer_bps": 2.0}}
    res = sl.evaluate_rows([r], cfg, warmup=0, half_spread=2.0)
    assert res["fee_bps"] == 14.0
    assert res["trades"]["fade16"][0]["net_bps"] == pytest.approx(20 - 14)     # 反向：偏离 +60 做空，价格跌 20
    assert res["trades"]["follow50"][0]["net_bps"] == pytest.approx(-20 - 14)


def test_news_variant_uses_only_rows_with_ledger_labels():
    p = []
    r_none, r_news, r_quiet = row("2026-09-04", dev=60), row("2026-09-04", "TSLA", dev=60, news=True), row("2026-09-04", "INTC", dev=60, news=False)
    assert sl.decide("news16", r_none, p, FEE) == 0
    assert sl.decide("news16", r_news, p, FEE) == 1 and sl.decide("news16", r_quiet, p, FEE) == -1
    assert sl.decide("news16", row("2026-09-04", dev=10, news=True), p, FEE) == 0   # 不过门槛


def test_regime_default_follow_and_switch_to_fade():
    fr = fridays(8)
    hist = [row(f, dev=50, move=-30) for f in fr[:6]]      # 过去 6 个周末反向赚
    r = row(fr[6], dev=80)
    assert sl.decide("regime6", r, hist, FEE) == -1
    assert sl.decide("regime6", r, hist[:5], FEE) == 1     # 不足 6 个 → 默认顺势
    assert sl.decide("regime4", r, [row(f, "TSLA", dev=50, move=-30) for f in fr[:6]], FEE) == 1   # 别的合约的历史不算


# ---------------------------------------------------------------- 统计：按日历周末计、集中度判定
def test_stats_counts_calendar_weekends_and_concentration_verdict():
    tr = []
    for i, f in enumerate(fridays(6)):
        for n in ("A", "B"):
            net = 300.0 if i == 0 else (100.0 if i == 1 else -10.0)
            tr.append({"fri": f, "name": n, "net_bps": net, "gross_bps": net + 14, "net_usd": net / 10})
    st = sl.stats(tr, 1000)
    assert st["n"] == 12 and st["n_weekends"] == 6
    assert st["total_usd"] == pytest.approx((600 + 200 - 80) / 10)
    assert st["ex_top2_usd"] == pytest.approx(-8.0)
    assert sl.verdict(st) == "样本外为正，但靠 1–2 个周末"
    assert st["max_dd_usd"] == pytest.approx(-8.0)
    good = [{"fri": f, "name": "A", "net_bps": 10.0, "gross_bps": 24.0, "net_usd": 1.0} for f in fridays(6)]
    assert sl.verdict(sl.stats(good, 1000)).startswith("通过")
    assert sl.verdict(sl.stats(good[:3], 1000)).startswith("样本太少")
    assert sl.verdict(sl.stats([], 1000)) == "没有出手"


def test_oos_window_starts_after_warmup():
    fr = fridays(10)
    rows = [row(f) for f in fr]
    res = sl.evaluate_rows(rows, {"costs": {"maker_bps": 3.0, "taker_bps": 9.0, "edge_buffer_bps": 2.0}}, warmup=8)
    assert res["oos_from"] == fr[8]
    assert res["oos"][("全部", "fade16")]["n_weekends"] == 2 and res["ins"][("全部", "fade16")]["n_weekends"] == 10


# ---------------------------------------------------------------- 端到端：1h + 15m 缓存合并、账本新闻、旧口径复算
def _write_cache(s, coin, iv, start, end, fn):
    ivm = bl.INTERVAL_MS[iv]
    k = int(start.timestamp() * 1000) // ivm * ivm
    cs = []
    while k < end.timestamp() * 1000:
        p = fn(k)
        cs.append({"t": k, "o": p, "h": p, "l": p, "c": p, "v": 1.0})
        k += ivm
    bl.save_cache(s, coin, iv, cs, merge=False)


def test_run_offline_merges_1h_and_15m_and_writes_report(s):
    now = datetime(2026, 10, 14, tzinfo=UTC)
    fn = lambda k: 100.0 * (1 + 0.003 * math.sin(k / 3.6e6 / 5))   # noqa: E731
    for inst in s.instruments:
        _write_cache(s, inst["coin"], "1h", datetime(2026, 5, 1, tzinfo=UTC), now, fn)
        _write_cache(s, inst["coin"], "15m", datetime(2026, 8, 23, tzinfo=UTC), now, fn)
    from desk.ledger import Ledger
    Ledger(s.data).append("lock", "2026-10-02|NVDA", {"features": {"ok": True, "news_weekend": True, "dev_bps": 10}})
    md = sl.run(s, fetch=False, now=now)
    out = Path(s.data) / "reports" / "strategy_lab.md"
    assert out.exists() and out.read_text() == md
    for sec in ("## 结论（中文）", "## v2 和对照", "v2（单票反向>50 + 4 周末停手）", "## 样本外（头条）", "## 样本内", "### 美指", "### 韩股", "## 走前挑选每个周末选了什么",
                "## 分时段", "旧口径复算", "1h 粒度（较早的周末）误差", "新闻标签（账本 lock 的 news_weekend）：1 行有标签"):
        assert sec in md, sec
    rows, _, _ = sl.build_rows(s, now, None, False)
    g = {r["fri"]: r["gran"] for r in rows if r["name"] == "NVDA"}
    assert g["2026-05-01"] == "1h" and g["2026-10-02"] == "15m"                # 有 15m 的周末用 15m
    assert [r["news"] for r in rows if r["name"] == "NVDA" and r["fri"] == "2026-10-02"] == [True]
    assert "2026-09-04" not in g                                                # 劳工节周一 → 美股单票跳过
    head_pipes = None
    for line in md.splitlines():                                                # 每张表的行和表头列数一致
        if not line.startswith("|"):
            head_pipes = None
            continue
        if head_pipes is None:
            head_pipes = line.count("|")
        assert line.count("|") == head_pipes, line


def test_run_with_no_data_says_so(s):
    md = sl.run(s, fetch=False, now=datetime(2026, 10, 14, tzinfo=UTC))
    assert "没有任何周末样本" in md and "desk.strategy_lab" in md
