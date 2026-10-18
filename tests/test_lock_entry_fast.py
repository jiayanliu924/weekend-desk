import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from desk import jobs, scheduler


def _wk(now):
    return SimpleNamespace(wid="2026-10-16|NVDA", decision=now - timedelta(minutes=1), resume=now + timedelta(minutes=14))


def test_entry_due_window():
    now = datetime(2026, 10, 18, 23, 46, tzinfo=timezone.utc)
    wk = _wk(now)
    assert scheduler.entry_due_now(wk, now)
    assert not scheduler.entry_due_now(wk, wk.resume - timedelta(minutes=2))
    assert not scheduler.entry_due_now(wk, wk.decision - timedelta(seconds=1))
    assert not scheduler.entry_due_now(None, now)


def test_entry_runs_once_right_after_lock(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    wk = _wk(now)
    calls = []
    monkeypatch.setattr(scheduler, "run_job", lambda k, w, s: calls.append(k))
    monkeypatch.setattr(jobs, "save_state", lambda s, d: None)
    done = set()
    asyncio.run(scheduler._entry_right_after_lock("2026-10-16|NVDA:lock", wk, None, done))
    assert calls == ["2026-10-16|NVDA:live_entry"]
    assert {"2026-10-16|NVDA:lock", "2026-10-16|NVDA:live_entry"} <= done
    asyncio.run(scheduler._entry_right_after_lock("2026-10-16|NVDA:lock", wk, None, done))
    assert len(calls) == 1          # 已下过不重复
    # due_jobs 不会再排 live_entry（它查 done）
    assert "2026-10-16|NVDA:live_entry" in done


def test_entry_skipped_outside_window(monkeypatch):
    now = datetime.now(timezone.utc)
    wk = SimpleNamespace(wid="w", decision=now - timedelta(minutes=20), resume=now + timedelta(minutes=1))
    calls = []
    monkeypatch.setattr(scheduler, "run_job", lambda k, w, s: calls.append(k))
    done = set()
    asyncio.run(scheduler._entry_right_after_lock("w:lock", wk, None, done))
    assert calls == [] and "w:live_entry" not in done


def test_backfill_memo_reads_cache_once(monkeypatch, tmp_path):
    jobs._BACKFILL_MEMO.clear()
    n = []
    monkeypatch.setattr(jobs, "_v2_backfill_rows_uncached", lambda s, f, w: n.append(1) or [{"fri": "2026-10-09", "name": "NVDA"}])
    s = SimpleNamespace(data=str(tmp_path))
    w = SimpleNamespace(decision=datetime(2026, 10, 18, 23, 45, tzinfo=timezone.utc))
    a = jobs._v2_backfill_rows(s, "us_equity", w)
    a[0]["name"] = "MUTATED"
    b = jobs._v2_backfill_rows(s, "us_equity", w)
    assert len(n) == 1 and b[0]["name"] == "NVDA"
    w2 = SimpleNamespace(decision=w.decision + timedelta(days=7))
    jobs._v2_backfill_rows(s, "us_equity", w2)
    assert len(n) == 2
