"""纸面期权交易员（11a）：定价与费用封顶、选边规则、合成链结算、对冲 vs 未对冲在大/小行情下的符号。不碰网络。"""
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from desk import config, options, options_paper as op
from desk.clock import to_ns
from desk.ledger import Ledger
from desk.rawstore import RawWriter

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
PCFG = {"notional_usd": 1000.0, "fee_pct_underlying": 0.03, "fee_cap_pct_premium": 12.5, "slippage_pct": 0.5,
        "hedge_fee_pct": 0.05, "short_below": 0.9, "long_above": 1.1, "stress_sigmas": 3.0}


@pytest.fixture
def s(tmp_path):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    return config.load(tmp_path)


# ---------------------------------------------------------------- 定价 / 费用
def test_bs_put_call_parity_and_delta():
    S, K, T, sig = 80000.0, 80000.0, 1 / 365, 0.5
    c, p = op.bs_price(S, K, T, sig, "C"), op.bs_price(S, K, T, sig, "P")
    assert c - p == pytest.approx(S - K, abs=1e-6)          # r = 0 的平价关系
    assert op.bs_delta(S, K, T, sig, "C") - op.bs_delta(S, K, T, sig, "P") == pytest.approx(1.0)
    assert abs(op.straddle_delta(S, K, T, sig)) < 0.05       # 平值跨式 delta 接近 0
    assert op.bs_price(S, K, 0, sig, "C") == 0 and op.bs_price(90000, K, 0, sig, "C") == 10000
    # 平值短期：价格 ≈ 0.4·S·σ·√T
    assert c == pytest.approx(0.3989 * S * sig * math.sqrt(T), rel=0.02)


def test_fee_cap():
    S, n = 80000.0, 1000 / 80000
    # 权利金大：按 0.03% 标的收 → 0.0003 × 80000 × n = 0.30 美元
    assert op.leg_fee_usd(50.0, S, n, PCFG) == pytest.approx(0.30)
    # 权利金小（1 美元）：封顶 12.5% → 0.125
    assert op.leg_fee_usd(1.0, S, n, PCFG) == pytest.approx(0.125)
    c = op.trade_cost_usd([10.0, 10.0], S, n, PCFG)
    assert c["fee"] == pytest.approx(0.60) and c["slippage"] == pytest.approx(0.10) and c["total"] == pytest.approx(0.70)


# ---------------------------------------------------------------- 选边规则 v1
def test_side_rule():
    assert op.choose_side([], PCFG, 5)[0] == "C"
    assert op.choose_side([0.5] * 4, PCFG, 5)[0] == "C"            # 不够 5 个
    assert op.choose_side([0.8, 0.9, 0.7, 0.85, 0.8], PCFG, 5)[0] == "A"
    assert op.choose_side([1.3, 1.2, 1.1, 1.4, 1.2], PCFG, 5)[0] == "B"
    assert op.choose_side([1.0, 0.95, 1.05, 1.0, 1.0], PCFG, 5)[0] == "C"
    assert "0.81" in op.choose_side([0.8, 0.9, 0.7, 0.85, 0.8], PCFG, 5)[1]


# ---------------------------------------------------------------- 合成链 + 结算
def chain(lock: datetime, S=80000.0, iv=50.0, days=(1, 2, 3), with_mark=True):
    items = []
    for d in days:
        exp = (lock + timedelta(days=d)).replace(hour=8, minute=0)
        tag = f"{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}"
        T = (exp - lock).total_seconds() / (365 * 86400)
        for k in (78000, 80000, 82000):
            for cp in "CP":
                px = op.bs_price(S, k, T, iv / 100, cp)
                it = {"n": f"BTC-{tag}-{k}-{cp}", "iv": iv, "u": S, "b": px / S * 0.98, "a": px / S * 1.02, "oi": 1}
                if with_mark:
                    it["m"] = px / S
                items.append(it)
    return items


def test_pick_expiry_covers_window():
    lock = datetime(2026, 10, 14, 11, 30, tzinfo=UTC)          # CPI 12:30 UTC − 60 分钟
    exp, rows = op.pick_expiry(chain(lock), lock, 24)
    assert exp == datetime(2026, 10, 16, 8, tzinfo=UTC)         # 10/15 08:00 不够盖住到 10/15 11:30，取 10/16
    legs = op.atm_legs(rows)
    assert legs["K"] == 80000 and legs["C"]["n"].endswith("-80000-C")


def _lock(lock_dt, S=80000.0, iv=50.0):
    return {"sample_kind": "daily", "event_type": "none", "has_event": False, "implied_vol": iv / 100}


def _plan(s, lock_dt, S=80000.0, iv=50.0):
    led = Ledger(s.data, "options")
    lock = _lock(lock_dt, S, iv)
    return op.plan(s, led, lock, chain(lock_dt, S, iv), lock_dt), lock


def test_plan_sizes_and_max_loss(s):
    lock_dt = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    pp, _ = _plan(s, lock_dt)
    assert pp["ok"] and pp["chosen"] == "C" and pp["n_history"] == 0
    assert pp["n_contracts"] == pytest.approx(1000 / 80000)
    assert pp["strikes"]["call"] == 80000
    # 08:00 锁定、24h 窗口刚好到次日 08:00 到期 → 取 1 天到期；平值跨式 ≈ 0.8·S·σ·√T → 1000·0.8·0.5·√(1/365) ≈ 20.9 美元
    assert pp["expiry"] == (lock_dt + timedelta(days=1)).isoformat()
    assert pp["premium"] == pytest.approx(1000 * 0.7979 * 0.5 * math.sqrt(1 / 365), rel=0.03)
    assert pp["max_loss"]["B"] == pytest.approx(pp["premium"] + pp["fees_entry"]["total"])
    assert pp["max_loss"]["A"] > pp["max_loss"]["B"] and pp["max_loss"]["C"] == 0


def _px(lock_dt, S0, S_end, S_mid=None):
    a, b = to_ns(lock_dt), to_ns(lock_dt + timedelta(hours=24))
    S_mid = S0 if S_mid is None else S_mid
    pts = []
    for i in range(0, 24 * 60 + 1, 5):
        t = a + i * 60 * 10**9
        frac = i / (24 * 60)
        p = S0 + (S_mid - S0) * frac * 2 if frac <= 0.5 else S_mid + (S_end - S_mid) * (frac - 0.5) * 2
        pts.append((t, p))
    return pts, a, b


def test_settle_big_move_long_wins_short_loses(s):
    lock_dt = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    pp, lock = _plan(s, lock_dt)
    lock["paper"] = pp
    px, a, b = _px(lock_dt, 80000, 88000)                   # +10%，远超隐含（≈ 2.6%）
    out = op.settle(s, lock, px, a, b, None)                 # 没有结束快照 → 内在价值
    assert out["ok"] and out["settle_method"] == "指数内在价值"
    assert out["pnl"]["B"] > 0 and out["pnl"]["A_unhedged"] < 0
    assert out["pnl"]["A_unhedged"] + out["pnl"]["B"] == pytest.approx(-2 * out["fees"]["total"])   # 零和减两份费用
    # 单边大涨：对冲腿（持多 delta·n）赚钱，对冲后亏损小于未对冲
    assert out["pnl"]["A_hedged"] > out["pnl"]["A_unhedged"]
    assert out["pnl"]["C"] == 0 and out["chosen_pnl"] == 0


def test_settle_small_move_short_wins(s):
    lock_dt = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    pp, lock = _plan(s, lock_dt)
    lock["paper"] = pp
    px, a, b = _px(lock_dt, 80000, 80100)                   # 几乎不动
    out = op.settle(s, lock, px, a, b, None)
    assert out["pnl"]["A_unhedged"] > 0 and out["pnl"]["B"] < 0
    assert out["pnl"]["A_unhedged"] == pytest.approx(out["premium"] - out["payout"] - out["fees"]["total"])
    # 小行情里对冲只是多付手续费，对冲后略差但同号
    assert out["pnl"]["A_hedged"] > 0 and out["pnl"]["A_hedged"] <= out["pnl"]["A_unhedged"] + 1e-9


def test_settle_with_end_chain_uses_marks(s):
    lock_dt = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)       # 09:00 锁定 → 次日 08:00 盖不住，取 2 天到期
    pp, lock = _plan(s, lock_dt)
    lock["paper"] = pp
    px, a, b = _px(lock_dt, 80000, 80000)
    end_dt = lock_dt + timedelta(hours=24)
    end_items = []
    for it in chain(lock_dt, 80000, 50.0):                   # 同名合约，剩余 1 天的标记价
        exp = options.parse_name(it["n"])[0]
        k, cp = options.parse_name(it["n"])[1:]
        T = (exp - end_dt).total_seconds() / (365 * 86400)
        end_items.append(dict(it, m=op.bs_price(80000, k, T, 0.5, cp) / 80000))
    out = op.settle(s, lock, px, a, b, end_items)
    assert out["settle_method"] == "窗口结束快照标记价"
    # 标的没动、波动没变：平仓付出的 = 剩余 23 小时的跨式时间价值，小于 47 小时的权利金 → 卖方赚 theta
    assert 0 < out["payout"] < out["premium"] and out["pnl"]["A_unhedged"] > 0


# ---------------------------------------------------------------- 端到端：走 options.job_lock / job_outcome
def seed(s, smp, S=80000.0, iv=50.0, S_end=80200.0):
    wo, wi = RawWriter(s.data, "drb_opts"), RawWriter(s.data, "drb_index")
    wo.add("deribit", "BTC", {"n_total": 500, "items": chain(smp.lock, S, iv)}, to_ns(smp.lock) - int(120e9))
    px, a, b = _px(smp.lock, S, S_end)
    for t, p in px:
        wi.add("deribit", "btc", {"index_price": p, "estimated_delivery_price": p}, t)
    wo.flush(), wi.flush()


def test_lock_outcome_records_paper_and_rule_kicks_in(s):
    s.raw["options"]["min_history"] = 2
    sids = []
    for d in (20, 21, 22):
        smp = options.daily_sample(datetime(2026, 10, d).date(), s)
        seed(s, smp)
        lock = options.job_lock(s, smp, extractor=lambda a, b: "[]", clock=lambda smp=smp: to_ns(smp.lock) - 1)
        assert lock["ok"] and lock["paper"]["ok"]
        out = options.job_outcome(s, smp)
        assert out["ok"] and out["paper"]["ok"]
        sids.append((smp.sid, lock["paper"]["chosen"], out["paper"]))
    # 前两个没历史 → C；第三个有 2 个对照组历史、实际波动远小于隐含 → 均值 < 0.9 → A
    assert [c for _, c, _ in sids] == ["C", "C", "A"]
    assert sids[2][2]["chosen_pnl"] == sids[2][2]["pnl"]["A_unhedged"] > 0
    sm = op.summary(s)
    assert sm["n"] == 3 and sm["n_chosen"] == 1 and sm["hit_rate"] == 1.0
    assert sm["total"]["chosen"] == pytest.approx(sids[2][2]["chosen_pnl"])
    assert "control" in sm["by_type"] and sm["by_type"]["control"]["n"] == 3
    txt = options.lock_text(options.Ledger(s.data, "options").all()[-2])
    assert "纸面期权" in txt and "选了 A 卖跨式" in txt
    otxt = options.outcome_text(options.Ledger(s.data, "options").all()[-2], options.Ledger(s.data, "options").all()[-1])
    assert "纸面期权结算" in otxt and "同组第 3 个样本" in otxt
    assert "美元" in op.summary_text(sm)


def test_currencies_and_streams(s):
    assert options.currencies(s) == ["BTC", "ETH"]
    assert options.stream_names(s, "BTC")["drb_opts"] == "drb_opts"
    assert options.stream_names(s, "ETH") == {"drb_opts": "drb_opts_eth", "drb_index": "drb_index_eth", "drb_dvol": "drb_dvol_eth"}
    assert [c.cur for c in options.collectors(s)] == ["BTC", "ETH"]
    s.raw["options"].pop("currencies")
    assert options.currencies(s) == ["BTC"]


def test_options_page_renders_paper_card(tmp_path, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    monkeypatch.setenv("DESK_ROOT", str(tmp_path))
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_INSECURE", "1")
    monkeypatch.setenv("WEB_SECRET", "x" * 32)
    s = config.load(tmp_path)
    s.raw["options"]["min_history"] = 2
    for d in (20, 21, 22):
        smp = options.daily_sample(datetime(2026, 10, d).date(), s)
        seed(s, smp)
        options.job_lock(s, smp, extractor=lambda a, b: "[]", clock=lambda smp=smp: to_ns(smp.lock) - 1)
        options.job_outcome(s, smp)
    import desk.web as web
    web = importlib.reload(web)
    web.add_user(s, "kea", "correct horse battery")
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"}, follow_redirects=False)
    r = c.get("/options")
    assert r.status_code == 200
    assert "纸面期权交易员" in r.text and "A 卖跨式" in r.text and "control" in r.text and "最差一笔" in r.text


# ---------------------------------------------------------------- 真实报价证据门（real_chain_stats）
def _seed(led, sid, kind, ratio, a, b, real=True, ok=True, et="CPI", paper_ok=True):
    lock = {"sample_kind": kind, "event_type": et, "ok": True, "implied_vol": 0.5}
    if real:
        lock["snapshot_received_at"] = 123
    led.append("lock", sid, lock)
    led.append("outcome", sid, {"ok": ok, "ratio": ratio, "log_ratio": math.log(ratio),
                                "paper": {"ok": paper_ok, "chosen": "C", "chosen_pnl": 0.0,
                                          "pnl": {"A_unhedged": a, "A_hedged": a, "B": b, "C": 0.0}}})


def test_real_chain_stats_math(s):
    led = Ledger(s.data, "options")
    _seed(led, "DAY-1", "daily", 0.8, 3.0, -4.0, et="none")
    _seed(led, "DAY-2", "daily", 1.25, -2.0, 1.0, et="none")
    _seed(led, "EVT-1", "event", 0.5, 6.0, -7.0)
    _seed(led, "EVT-2", "event", 2.0, -9.0, 8.0)
    _seed(led, "SHOCK-1", "shock", 1.0, 1.0, -2.0, et="SHOCK:GEO")
    _seed(led, "EVT-NOCHAIN", "event", 0.1, 100.0, -100.0, real=False)     # 没用真实期权链 → 不算
    _seed(led, "EVT-BAD", "event", 0.1, 100.0, -100.0, ok=False)          # 结算失败 → 不算
    st = options.real_chain_stats(s)
    g = st["groups"]
    assert g["daily"]["n"] == 2 and g["daily"]["geo_ratio"] == pytest.approx(1.0)          # √(0.8·1.25)
    assert g["daily"]["A_net"] == pytest.approx(1.0) and g["daily"]["B_net"] == pytest.approx(-3.0)
    assert g["daily"]["hit_A"] == pytest.approx(0.5) and g["daily"]["worst"] == pytest.approx(-4.0)
    assert g["event"]["n"] == 2 and g["event"]["geo_ratio"] == pytest.approx(1.0)          # √(0.5·2)
    assert g["event"]["A_net"] == pytest.approx(-3.0) and g["event"]["B_net"] == pytest.approx(1.0)
    assert g["event"]["worst"] == pytest.approx(-9.0)
    el = g["event_like"]
    assert el["n"] == 3 and st["n_event_like"] == 3
    assert el["geo_ratio"] == pytest.approx(1.0) and el["A_net"] == pytest.approx(-2.0) and el["B_net"] == pytest.approx(-1.0)
    assert el["hit_B"] == pytest.approx(1 / 3)
    assert st["by_type"]["CPI"]["n"] == 2 and st["by_type"]["SHOCK:GEO"]["n"] == 1
    assert not st["pass"] and any("≥ 10" in r for r in st["reasons"]) and any("都不赚钱" in r for r in st["reasons"])


def test_real_chain_gate_pass_and_file(s):
    import json
    led = Ledger(s.data, "options")
    for i in range(10):
        _seed(led, f"EVT-{i}", "event" if i % 2 else "shock", 0.9, 2.0, -3.0)
    st = options.real_chain_stats(s)
    assert st["pass"] and st["groups"]["event_like"]["A_net"] == pytest.approx(20.0)
    # 最差一笔低于 −3 × max_risk_usd → 不过
    _seed(led, "EVT-TAIL", "event", 3.0, -3 * s.raw["alpaca"]["max_risk_usd"] - 1, 1000.0)
    st = options.real_chain_stats(s)
    assert not st["pass"] and any("最差一笔" in r for r in st["reasons"])
    g = options.write_real_gate(s)
    d = json.loads(options.real_gate_file(s).read_text())
    assert d["method"] == "real_chain" and d["pass"] is False and g["pass"] is False and d["n_event_like"] == 11
