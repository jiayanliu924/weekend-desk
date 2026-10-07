"""模拟一个完整周末，跑通 open → extract → lock → outcome → report，并检查备忘 7.4 的四条硬规则。"""
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from desk import config, evaluate, extract, jobs, model
from desk.clock import to_ns, weekend_for_friday
from desk.ledger import Ledger
from desk.rawstore import RawWriter

ROOT = Path(__file__).resolve().parent.parent
FRI = date(2026, 10, 2)


def ctx(coin, oracle, mid, funding="0.00000625", oi="7000"):
    return {"channel": "activeAssetCtx", "data": {"coin": coin, "ctx": {
        "oraclePx": str(oracle), "markPx": str(mid), "midPx": str(mid), "funding": funding,
        "openInterest": oi, "impactPxs": [str(mid - 1), str(mid + 1)], "dayNtlVlm": "1"}}}


def book(coin, mid):
    lv = lambda s: [{"px": str(mid + s * (i + 1)), "sz": "1.0", "n": 1} for i in range(10)]  # noqa: E731
    return {"channel": "l2Book", "data": {"coin": coin, "time": 0, "levels": [lv(-1), lv(1)]}}


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    st = config.load(tmp_path)
    return st


def seed_market(s, w, dev_bps=100.0, reopen_bps=10.0, news_after_decision=False, with_news=True):
    coin = s.instrument
    fri = 30000.0
    wc, wb, wt, wp, wn = (RawWriter(s.data, n) for n in ("hl_ctx", "hl_book", "hl_trades", "hl_params", "news"))
    t0 = to_ns(w.close)
    # Friday afternoon: oracle == external
    for i in range(60):
        t = t0 - int((60 - i) * 60e9)
        wc.add("ws", coin, ctx(coin, fri, fri), t)
        wc.add("ws", "BTC", ctx("BTC", 60000, 60000), t)
    # weekend drift to dev_bps, sampled every 10 minutes
    dec = to_ns(w.decision)
    steps = int((dec - t0) / 600e9)
    for i in range(steps):
        t = t0 + int(i * 600e9) + 1
        px = fri * (1 + dev_bps / 1e4 * min(1, i / (steps * 0.5)))
        wc.add("ws", coin, ctx(coin, fri, px), t)
        wc.add("ws", "BTC", ctx("BTC", 60000, 60300), t)
        wb.add("ws", coin, book(coin, px), t)
        wt.add("ws", coin, {"channel": "trades", "data": [{"px": str(px), "sz": "0.01", "tid": i}]}, t)
    # after resume: oracle snaps to external
    reopen = fri * (1 + reopen_bps / 1e4)
    for i in range(40):
        t = to_ns(w.resume) + int(i * 30e9) + 1
        wc.add("ws", coin, ctx(coin, reopen, reopen), t)
    wp.add("rest", coin, {"asset_meta": {"name": coin, "growthMode": "enabled"}}, t0)
    if with_news:
        wn.add("feed", "n1", {"feed": "test", "title": "Nvidia cuts revenue guidance sharply",
                              "summary": "Nvidia said Saturday it cuts revenue guidance sharply for the quarter.",
                              "published_at": None}, t0 + int(3600e9))
        wn.add("feed", "n2", {"feed": "test", "title": "Local weather is sunny", "summary": "", "published_at": None},
               t0 + int(7200e9))
    if news_after_decision:
        wn.add("feed", "late", {"feed": "test", "title": "Apple announces huge buyback", "summary": "",
                                "published_at": None}, dec + int(60e9))
    for x in (wc, wb, wt, wp, wn):
        x.flush()


def fake_llm(system, user):
    if "Nvidia cuts revenue guidance" in user:
        return json.dumps([{"event_id": "n1-1", "entities": ["NVDA"], "event_type": "财报指引",
                            "direction_in_text": "负面", "materiality": 3, "is_new_information": True,
                            "evidence_span": "Nvidia said Saturday it cuts revenue guidance sharply for the quarter."}])
    if "Apple announces" in user:
        return json.dumps([{"entities": ["AAPL"], "materiality": 3, "evidence_span": "Apple announces huge buyback"}])
    if "weather" in user:
        return json.dumps([{"entities": [], "materiality": 0, "evidence_span": "made up sentence not in text"}])
    return "[]"


def run_weekend(s, w, extractor=fake_llm):
    jobs.job_open(s, w)
    jobs.job_extract(s, w, extractor=extractor, until=w.decision,
                     clock=lambda: to_ns(w.decision) - int(600e9))  # extraction finished 10 min before decision
    lock = jobs.job_lock(s, w)
    out = jobs.job_outcome(s, w)
    return lock, out


def test_full_weekend_news(s):
    w = weekend_for_friday(FRI, s)
    seed_market(s, w, dev_bps=100, reopen_bps=90)
    lock, out = run_weekend(s, w)
    f = lock["features"]
    assert f["ok"], f["problems"]
    assert abs(f["dev_bps"] - 100) < 1
    assert f["n_mat2"] == 1 and f["news_weekend"] and f["n_top10"] == 1
    assert lock["prediction"]["model_version"] == "h1-rule-v0"
    assert abs(lock["prediction"]["pred_bps"] - f["dev_bps"]) < 1e-9   # news → confirm deviation
    assert lock["fees"]["growth_mode"] is True and lock["fees"]["taker_bps"] == pytest.approx(0.9)
    assert not lock["void"]
    assert out["ok"] and abs(out["y_bps"] - 90) < 0.5
    assert out["err_B_bps"] < out["err_A_bps"]
    path = jobs.job_report(s, w)
    assert "本周实验日志" in path.read_text()


def test_quiet_weekend_fade(s):
    w = weekend_for_friday(FRI, s)
    seed_market(s, w, dev_bps=60, reopen_bps=5, with_news=False)
    lock, out = run_weekend(s, w)
    assert not lock["features"]["news_weekend"]
    assert lock["prediction"]["pred_bps"] == 0.0               # quiet → deviation erased
    assert lock["action"]["side"] == -1                         # fade: sell
    assert out["pnl_net_bps"] > 0


def test_point_in_time_and_evidence(s):
    w = weekend_for_friday(FRI, s)
    seed_market(s, w, news_after_decision=True)
    lock, _ = run_weekend(s, w)
    # the Apple item arrived after decision → must not be counted (7.4 第一条)
    assert lock["features"]["n_mat2"] == 1
    # weather event had a fabricated evidence span → evidence_ok False (7.4 第二条)
    evs = extract.load_events(s, to_ns(w.decision) + int(3600e9), to_ns(w.news_start))
    assert all(e["evidence_ok"] for e in evs)


def test_rule_change_voids_weekend(s):
    w = weekend_for_friday(FRI, s)
    seed_market(s, w)
    jobs.job_open(s, w)
    with open(s.root / "config.toml", "a") as f:
        f.write("\n# edited mid-weekend\n")
    lock = jobs.job_lock(s, w)
    assert lock["void"] and "周五收盘后修改过规则" in lock["void_reasons"][0]
    rec = {"weekend": w.wid, "lock": lock, "outcome": jobs.job_outcome(s, w)}
    assert not evaluate.official(rec, s)


def test_lock_is_written_once(s):
    w = weekend_for_friday(FRI, s)
    seed_market(s, w)
    jobs.job_open(s, w)
    a = jobs.job_lock(s, w)
    b = jobs.job_lock(s, w)
    assert a["record_sha256"] == b["record_sha256"]
    assert sum(1 for r in Ledger(s.data).all() if r["kind"] == "lock") == 1


def test_linear_model_after_min_train(s):
    hist = [{"dev_bps": d, "news_weekend": n, "y_bps": d * (0.9 if n else 0.3)}
            for d, n in [(50, False), (-40, True), (30, False), (80, True), (-20, False), (60, False),
                         (-70, True), (25, False), (45, True), (-35, False), (15, False), (90, True)]]
    p = model.predict({"dev_bps": 100, "news_weekend": False}, hist, s)
    assert p["model_version"].startswith("ols") and abs(p["pred_bps"] - 30) < 1e-6


def test_dst_switch():
    cfg = config.load(ROOT).raw
    summer = weekend_for_friday(date(2026, 10, 2), cfg)
    winter = weekend_for_friday(date(2026, 11, 6), cfg)
    assert summer.resume.hour == 22 and winter.resume.hour == 23   # 6pm ET in UTC
    assert summer.decision == summer.resume - timedelta(minutes=15)
