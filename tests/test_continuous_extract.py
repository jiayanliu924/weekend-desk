"""工作日持续抽取：调度出现、只抽最近窗口、不重复抽。"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from desk import config, rawstore, scheduler
from desk.rawstore import RawWriter

ROOT = Path(__file__).resolve().parent.parent


def _settings(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    if (ROOT / "events.toml").exists():
        shutil.copy(ROOT / "events.toml", tmp_path / "events.toml")
    return config.load(tmp_path)


def test_job_scheduled_on_weekday(tmp_path):
    s = _settings(tmp_path)
    now = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)   # Wednesday
    keys = [k for k, _ in scheduler.due_jobs(now, s, set())]
    assert any(k.startswith("xtract:") for k in keys)


def test_disabled_when_zero(tmp_path):
    s = _settings(tmp_path)
    s.raw["llm"]["continuous_every_min"] = 0
    now = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
    assert not any(k.startswith("xtract:") for k, _ in scheduler.due_jobs(now, s, set()))


def test_extracts_recent_only_and_once(tmp_path):
    s = _settings(tmp_path)
    now_ns = int(datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc).timestamp() * 1e9)
    w = RawWriter(s.data, "news")
    w.add("feed", "old", {"feed": "t", "title": "Old tanker attack near Hormuz", "summary": "", "published_at": None},
          now_ns - int(5 * 3600e9))
    w.add("feed", "new", {"feed": "t", "title": "New tanker attack near Hormuz", "summary": "", "published_at": None},
          now_ns - int(10 * 60e9))
    w.flush()
    calls = []

    def fake(system, user):
        calls.append(user)
        return json.dumps([])

    assert scheduler.continuous_extract(s, extractor=fake, now_ns=now_ns) == 1
    assert len(calls) == 1 and "New tanker" in calls[0]
    # second run: already extracted → nothing new
    assert scheduler.continuous_extract(s, extractor=fake, now_ns=now_ns) == 0
    keys = {k for (k,) in rawstore.query(s.data, "events", select="key")}
    assert keys == {"new"}
