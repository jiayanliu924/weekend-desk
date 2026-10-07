"""采集解析（用 2026-10-06 实际收到的 Hyperliquid 消息格式）、调度时间线、回补统计。"""
import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from desk import backfill, config, rawstore, scheduler
from desk.clock import weekend_for_friday
from desk.collector import Collector
from desk.features import book_before, last_ctx_before

ROOT = Path(__file__).resolve().parent.parent

REAL_CTX = '{"channel":"activeAssetCtx","data":{"coin":"xyz:XYZ100","ctx":{"funding":"0.00000625","openInterest":"7049.6874","prevDayPx":"31090.0","dayNtlVlm":"204936975.5174998045","premium":"-0.0001119928","oraclePx":"31252.0","markPx":"31249.0","midPx":"31248.5","impactPxs":["31248.0","31249.0"],"dayBaseVlm":"6563.3756"}}}'
REAL_BOOK = json.dumps({"channel": "l2Book", "data": {"coin": "xyz:XYZ100", "time": 1791323260565, "levels": [
    [{"px": "31248.0", "sz": "3.0342", "n": 3}, {"px": "31247.0", "sz": "1.1425", "n": 2}],
    [{"px": "31249.0", "sz": "2.0", "n": 2}, {"px": "31250.0", "sz": "1.0", "n": 1}]]}})
REAL_TRADES = '{"channel":"trades","data":[{"coin":"xyz:XYZ100","side":"A","px":"31248.0","sz":"0.0009","time":1791323083301,"hash":"0x39","tid":270336010212158,"users":["0x5a","0xdb"]}]}'


def settings(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    return config.load(tmp_path)


def test_collector_parses_real_messages(tmp_path):
    s = settings(tmp_path)
    c = Collector(s)
    for m in (REAL_CTX, REAL_BOOK, REAL_TRADES, '{"channel":"subscriptionResponse","data":{}}', "not json"):
        c._on_msg(m)
    c.flush()
    t = rawstore.now_ns() + 1
    r, ctx = last_ctx_before(s, "xyz:XYZ100", t)
    assert ctx["oraclePx"] == "31252.0" and ctx["midPx"] == "31248.5"
    b = book_before(s, "xyz:XYZ100", t)
    assert b["best_bid"] == 31248.0 and b["best_ask"] == 31249.0 and b["spread_bps"] < 0.5
    assert rawstore.verify(s.data, "hl_ctx")["hash_mismatch"] == 0


def test_throttle(tmp_path):
    s = settings(tmp_path)
    c = Collector(s)
    for _ in range(50):
        c._on_msg(REAL_CTX)
        c._on_msg(REAL_BOOK)
    c.flush()
    assert len(rawstore.query(s.data, "hl_ctx")) == 1
    assert len(rawstore.query(s.data, "hl_book")) == 1


def test_scheduler_timeline(tmp_path):
    s = settings(tmp_path)
    w = weekend_for_friday(date(2026, 10, 9), s)
    done = set()
    seen = []
    t = w.close - timedelta(hours=2)
    while t < w.resume + timedelta(days=1):
        for key, _ in scheduler.due_jobs(t, s, done):
            done.add(key)
            seen.append((key, t))
        t += timedelta(minutes=1)
    names = [k.split(":", 1)[1] for k, _ in seen if k.startswith(w.wid)]
    order = [n for n in names if not n.startswith("extract")]
    assert order == ["open", "lock", "outcome", "report"]
    assert "extract_final" in names and sum(1 for n in names if n.startswith("extract")) > 150
    when = dict((k.split(":", 1)[1], tt) for k, tt in seen if k.startswith(w.wid))
    assert when["lock"] == w.decision and when["outcome"] >= w.exit


def test_backfill_weekend_table(tmp_path):
    s = settings(tmp_path)
    w = weekend_for_friday(date(2026, 10, 2), s)
    H = 3600 * 1000
    ms = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731
    candles = []
    t = ms(w.close) - 10 * H
    while t < ms(w.resume) + 5 * H:
        px = 100.0 if t < ms(w.close) else (101.0 if t < ms(w.resume) else 100.5)
        candles.append({"t": t, "o": px, "h": px, "l": px, "c": px, "v": 1})
        t += H
    rows = backfill.weekend_table(s, candles)
    assert len(rows) == 1 and round(rows[0]["dev_bps"]) == 100 and round(rows[0]["y_bps"]) == 50
    assert "基线" in backfill.baseline_report(s, rows)
