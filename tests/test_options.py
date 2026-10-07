"""期权研究模块（备忘第九章）：隐含/实际波动计算、事件锁定 → 对账 → 意外程度、调度。"""
import json
import math
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from desk import config, options
from desk.clock import to_ns
from desk.rawstore import RawWriter

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc

# real Deribit item shape captured 2026-10-06
REAL_ITEM = {"high": None, "low": None, "last": 0.0085, "instrument_name": "BTC-27NOV26-73000-P", "bid_price": 0.009,
             "ask_price": 0.01, "open_interest": 64.4, "mark_price": 0.00950764, "interest_rate": 0.0,
             "creation_timestamp": 1791297146454, "estimated_delivery_price": 86006.54, "price_change": None,
             "volume": 0.0, "mark_iv": 41.07, "underlying_price": 86679.51, "underlying_index": "BTC-27NOV26",
             "base_currency": "BTC", "quote_currency": "BTC", "volume_usd": 0.0, "mid_price": 0.0095}


@pytest.fixture
def s(tmp_path):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    return config.load(tmp_path)


def test_parse_and_filter():
    exp, k, cp = options.parse_name("BTC-27NOV26-73000-P")
    assert exp == datetime(2026, 11, 27, 8, tzinfo=UTC) and k == 73000 and cp == "P"
    now = datetime(2026, 11, 20, tzinfo=UTC)
    near = dict(REAL_ITEM, instrument_name="BTC-27NOV26-86000-C")
    assert len(options.filter_chain([REAL_ITEM, near], now, 10, 0.10)) == 1   # 73000 is >10% OTM


def test_implied_vol_interpolation():
    ts = [(1 / 365 / 2, 0.60), (2 / 365, 0.40)]          # 12h at 60%, 48h at 40%
    iv = options.implied_vol_for(ts, 24)
    w0, w1 = 0.36 * (0.5 / 365), 0.16 * (2 / 365)
    expect = math.sqrt((w0 + (w1 - w0) * (0.5 / 1.5)) / (1 / 365))
    assert iv == pytest.approx(expect)
    assert options.implied_vol_for([(3 / 365, 0.5)], 24) == 0.5


def test_realized_vol_recovers_sigma():
    random.seed(1)
    sigma = 0.5
    dt = 60 / (365 * 86400)
    t0 = 1_800_000_000 * 10**9
    px, p = [], 80000.0
    for i in range(24 * 60 + 1):
        px.append((t0 + i * 60 * 10**9, p))
        p *= math.exp(random.gauss(0, sigma * math.sqrt(dt)))
    rv, cov = options.realized_vol(px, t0, t0 + 24 * 3600 * 10**9)
    assert cov > 0.99 and abs(rv - sigma) < 0.05


def seed(s, smp, iv_pct=50.0, sigma=0.5, news=None):
    wo, wi, wn = RawWriter(s.data, "drb_opts"), RawWriter(s.data, "drb_index"), RawWriter(s.data, "news")
    u = 80000.0
    items = []
    for d in (1, 2, 3):
        exp = (smp.lock + timedelta(days=d)).replace(hour=8, minute=0)
        tag = f"{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}"
        for k in (78000, 80000, 82000):
            for cp in "CP":
                items.append({"n": f"BTC-{tag}-{k}-{cp}", "iv": iv_pct, "u": u, "b": 0.01, "a": 0.011, "oi": 1})
    wo.add("deribit", "BTC", {"n_total": 500, "items": items}, to_ns(smp.lock) - int(120e9))
    random.seed(7)
    dt = 60 / (365 * 86400)
    p = u
    t = to_ns(smp.lock) - int(300e9)
    while t <= to_ns(smp.end) + int(300e9):
        wi.add("deribit", "btc", {"index_price": p, "estimated_delivery_price": p}, t)
        p *= math.exp(random.gauss(0, sigma * math.sqrt(dt)))
        t += int(60e9)
    for i, (title, summ) in enumerate(news or []):
        wn.add("feed", f"n{i}", {"feed": "t", "title": title, "summary": summ, "published_at": None},
               to_ns(smp.event_at) + int(1800e9) if smp.event_at else to_ns(smp.lock) - int(3600e9))
    for w in (wo, wi, wn):
        w.flush()


def fake(system, user):
    if "实际值" in system:
        return json.dumps({"actual": "0.4%", "expected": "0.3%", "surprise_direction": "高于预期", "surprise_size": 2,
                           "evidence_span": "CPI rose 0.4% versus 0.3% expected"})
    return "[]"


def test_event_lock_outcome_surprise(s):
    smp = next(e for e in options.event_samples(s) if e.sid == "EVT-CPI-2026-10")
    assert smp.lock == smp.event_at - timedelta(minutes=60)
    seed(s, smp, iv_pct=50, sigma=0.35, news=[("US CPI rose 0.4% versus 0.3% expected", "Inflation hotter.")])
    lock = options.job_lock(s, smp, extractor=fake, clock=lambda: to_ns(smp.lock) - 1)
    assert lock["ok"] and lock["implied_vol"] == pytest.approx(0.5)
    assert lock["has_event"] and lock["prediction"]["model_version"] == "fair-v0"
    assert options.job_lock(s, smp)["record_sha256"] == lock["record_sha256"]   # locked once
    out = options.job_outcome(s, smp)
    assert out["ok"] and out["ratio"] < 1 and abs(out["realized_vol"] - 0.35) < 0.06
    sur = options.job_surprise(s, smp, extractor=fake)
    assert sur["n_sources"] == 1 and sur["max_size"] == 2
    card = options.scorecard(s)
    assert card["n_event"] == 1 and "还不够" in card["verdict"]


def test_daily_control_vs_event_window(s):
    quiet = options.daily_sample(datetime(2026, 10, 20).date(), s)
    busy = options.daily_sample(datetime(2026, 10, 14).date(), s)    # CPI at 12:30 UTC falls inside
    for smp in (quiet, busy):
        seed(s, smp)
        options.job_lock(s, smp, extractor=fake, clock=lambda smp=smp: to_ns(smp.lock) - 1)
        options.job_outcome(s, smp)
    recs = {r["sid"]: r for r in options.completed(options.Ledger(s.data, "options"))}
    assert options.group_of(recs[quiet.sid]["lock"]) == "control"
    assert options.group_of(recs[busy.sid]["lock"]) == "daily_with_event"


def test_missing_data_is_flagged(s):
    smp = options.daily_sample(datetime(2026, 10, 21).date(), s)
    lock = options.job_lock(s, smp, extractor=fake)
    assert not lock["ok"] and "没有期权快照" in lock["problems"][0]


def test_due_schedule(s):
    smp = next(e for e in options.event_samples(s) if e.sid == "EVT-FOMC-2026-10")
    done, seen = set(), []
    t = smp.lock - timedelta(hours=1)
    while t < smp.end + timedelta(hours=7):
        for k, x in options.due(t, s, done):
            if x.sid == smp.sid:
                seen.append((k.rsplit(":", 1)[1], t))
            done.add(k)
        t += timedelta(minutes=1)
    names = [n for n, _ in seen]
    assert names == ["lock", "surprise", "outcome"]
    assert dict(seen)["lock"] == smp.lock
