"""巡检员（desk/supervisor.py）：全部用假依赖，不联网、不读 journalctl、不调大模型。"""
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from desk import config, scheduler, supervisor as sv

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)      # 周三，美股开市


@pytest.fixture
def s(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    return config.load(tmp_path)


def env(**kw):
    d = dict(timeouts_24h=lambda: 0, freshness=lambda st, now: {"hl_ctx": 0.5, "news": 5.0},
             bars=lambda st, syms, now: None, gate=lambda st: [],
             disk_free_pct=lambda st: 60.0, mem_avail_pct=lambda: 60.0)
    d.update(kw)
    return sv.Env(**d)


def by_key(fs):
    return {f["key"]: f for f in fs}


def set_pairs_live(s, days_ago, reasons=()):
    p = Path(s.data) / "state"
    p.mkdir(parents=True, exist_ok=True)
    (p / "pairs_live.json").write_text(json.dumps({"mode": "live", "ts": (NOW - timedelta(days=days_ago)).timestamp()}))
    (p / "pairs_last_decision.json").write_text(json.dumps({
        "ts_ms": int(NOW.timestamp() * 1000), "mode": "live",
        "pairs": [{"key": f"A{i}|B{i}", "entered": False, "reason": r} for i, r in enumerate(reasons)]}))


def test_live_pairs_zero_entries_warns_with_top3_reasons(s):
    reasons = ["价差 12 < 门槛 40"] * 5 + ["休市禁开"] * 3 + ["腿不活跃（io:X）"] * 2 + ["这一对已有仓位"]
    set_pairs_live(s, days_ago=5, reasons=reasons)
    f = by_key(sv.checks(s, NOW, env()))["live.pairs.no_entry"]
    assert f["level"] == "warn"
    assert "价差还没到开仓门槛（5 次）" in f["body"] and "休市禁开（3 次）" in f["body"] and "腿不活跃" in f["body"]
    assert "这一对已有仓位" not in f["body"]                     # 只列前三
    assert f["since"]


def test_live_pairs_recent_start_or_with_entry_is_quiet(s):
    set_pairs_live(s, days_ago=0.5, reasons=["休市禁开"])        # 刚切到真下单不到 48 小时
    assert "live.pairs.no_entry" not in by_key(sv.checks(s, NOW, env()))


def test_market_hours_skips_weekend():
    fri = datetime(2026, 10, 23, 23, 0, tzinfo=timezone.utc)     # 周五美东 19:00（已休市）
    mon = datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc)     # 周一美东 09:00
    assert sv.market_hours(fri, mon) == pytest.approx(15.0)      # 周日 18:00 开市 → 周一 09:00
    assert sv.market_hours(NOW - timedelta(hours=10), NOW) == pytest.approx(10)
    start = sv.market_start(mon, 48)
    assert sv.market_hours(start, mon) == pytest.approx(48, abs=0.05)
    assert (mon - start).total_seconds() / 3600 > 48 + 40         # 中间隔了一个周末


def test_soxx_eight_down_days_alerts_and_mentions_options_status(s):
    closes = [100.0] + [100 - 2 * i for i in range(1, 9)]       # 连跌 8 天，−16%
    closes = [100.0] * 15 + closes
    gate = [{"key": "config", "name": "配置打开真钱", "ok": False, "detail": ""},
            {"key": "keys", "name": "真钱账户 key", "ok": False, "detail": ""}]
    s.raw["alpaca"] = {"enabled": True}
    bars = {"SOXX": closes, "QQQ": [100.0 + i * 0.1 for i in range(30)], "SPY": [100.0 + i * 0.1 for i in range(30)]}
    f = by_key(sv.checks(s, NOW, env(bars=lambda st, syms, now: bars, gate=lambda st: gate)))["regime"]
    assert f["level"] == "alert"
    assert "SOXX" in f["body"] and "连跌 8 天" in f["body"] and "-16.0%" in f["body"]
    assert "期权线现在" in f["body"] and "2 道没过" in f["body"]
    assert "QQQ" not in f["body"]


def test_regime_metrics():
    m = sv.regime_metrics([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 100, 95])
    assert m["down_days"] == 2 and m["dd20"] == pytest.approx((95 / 110 - 1) * 100)


def test_dedup_within_6h_and_escalation(s, monkeypatch):
    pushes = []
    monkeypatch.setattr(sv.notify, "push", lambda st, title, body, priority="default": pushes.append((title, priority)) or True)
    level = {"v": "warn"}
    monkeypatch.setattr(sv, "checks", lambda st, now=None, env=None: [sv.finding(level["v"], "k1", "标题", "正文", "2026-10-21T00:00:00+00:00")])
    assert len(sv.job_hourly(s, NOW)) == 1
    assert sv.job_hourly(s, NOW + timedelta(hours=1)) == []           # 6 小时内不重推
    assert sv.job_hourly(s, NOW + timedelta(hours=5, minutes=59)) == []
    level["v"] = "alert"
    assert len(sv.job_hourly(s, NOW + timedelta(hours=2))) == 1       # 升级 → 立刻再推
    assert pushes[-1][1] == "high" and len(pushes) == 2
    assert sv.job_hourly(s, NOW + timedelta(hours=3)) == []
    assert len(sv.job_hourly(s, NOW + timedelta(hours=9))) == 1       # 过了 6 小时，仍在 → 再推一次
    assert len(pushes) == 3
    st = sv.load_state(s)
    assert st["last"]["findings"][0]["key"] == "k1"


def test_info_findings_never_pushed(s, monkeypatch):
    pushes = []
    monkeypatch.setattr(sv.notify, "push", lambda *a, **k: pushes.append(a) or True)
    monkeypatch.setattr(sv, "checks", lambda st, now=None, env=None: [sv.finding("info", "i", "t", "b", "")])
    assert sv.job_hourly(s, NOW) == [] and not pushes


def test_failing_check_becomes_one_finding_and_others_run(s, monkeypatch):
    def boom(st, now, e):
        raise RuntimeError("坏了")
    monkeypatch.setattr(sv, "CHECKS", [("live_lines", "真钱线是否空转", boom), ("resources", "磁盘和内存", sv.check_resources)])
    fs = sv.checks(s, NOW, env(disk_free_pct=lambda st: 5.0))
    k = by_key(fs)
    assert k["check_failed.live_lines"]["level"] == "warn" and "坏了" in k["check_failed.live_lines"]["body"]
    assert "res.disk" in k                                              # 后面的检查照常
    pushes = []
    monkeypatch.setattr(sv.notify, "push", lambda *a, **kw: pushes.append(a) or True)
    assert len(sv.job_hourly(s, NOW, env(disk_free_pct=lambda st: 5.0))) == 2    # job 不崩，一共 2 条


def test_data_health_timeouts_and_stale_collector(s):
    k = by_key(sv.checks(s, NOW, env(timeouts_24h=lambda: 12, freshness=lambda st, now: {"hl_ctx": 45.0, "news": 5.0})))
    assert k["data.timeouts"]["level"] == "warn" and "12 次" in k["data.timeouts"]["title"]
    assert k["data.hl_ctx.stale"]["level"] == "alert"
    k = by_key(sv.checks(s, NOW, env(timeouts_24h=lambda: None)))        # 没有 journalctl → 只给 info
    assert k["data.timeouts.unavailable"]["level"] == "info"


def test_loss_budget_levels(s, monkeypatch):
    from desk import account_stop
    s.raw["account"] = {"max_loss_usd": 20.0}
    monkeypatch.setattr(account_stop, "components", lambda st: {"weekend_usd": -4.0, "pairs_usd": -8.0, "total_usd": -12.0, "errors": []})
    assert by_key(sv.checks(s, NOW, env()))["loss.account"]["level"] == "warn"            # 60%
    monkeypatch.setattr(account_stop, "components", lambda st: {"weekend_usd": -4.0, "pairs_usd": -12.0, "total_usd": -16.0, "errors": []})
    assert by_key(sv.checks(s, NOW, env()))["loss.account"]["level"] == "alert"           # 80%


def test_daily_llm_called_once_and_pushed_once(s, monkeypatch):
    pushes, calls = [], []
    monkeypatch.setattr(sv.notify, "push", lambda st, title, body, priority="default": pushes.append((title, body)) or True)

    def llm(model, system, user):
        calls.append(model)
        return "今天一切正常。", 100, 50
    txt = sv.job_daily(s, NOW, env(), llm=llm)
    assert txt == "今天一切正常。" and calls == ["claude-sonnet-5-5"] and len(pushes) == 1
    assert sv.job_daily(s, NOW + timedelta(hours=1), env(), llm=llm) is None      # 同一天不再调 / 不再推
    assert len(calls) == 1 and len(pushes) == 1


def test_daily_llm_failure_falls_back_to_code_digest(s, monkeypatch):
    pushes, calls = [], []
    monkeypatch.setattr(sv.notify, "push", lambda st, title, body, priority="default": pushes.append(body) or True)

    def llm(model, system, user):
        calls.append(1)
        raise RuntimeError("API 挂了")
    set_pairs_live(s, days_ago=5, reasons=["休市禁开"])
    txt = sv.job_daily(s, NOW, env(), llm=llm)
    assert "每日巡检" in txt and "套利线" in txt and "各条线现状" in txt and pushes == [txt]
    assert len(calls) == 1                                                        # 失败也只算这一次
    sv.job_daily(s, NOW + timedelta(hours=2), env(), llm=llm)
    assert len(calls) == 1


def test_daily_without_key_uses_code_digest_and_dry_has_no_side_effects(s, monkeypatch):
    pushes = []
    monkeypatch.setattr(sv.notify, "push", lambda *a, **k: pushes.append(a) or True)
    assert "没有需要处理的问题" in sv.job_daily(s, NOW, env(), dry=True)
    assert not pushes and not (Path(s.data) / "state" / "supervisor.json").exists()
    assert "每日巡检" in sv.job_daily(s, NOW, env())                              # 没 ANTHROPIC_API_KEY → 代码日报
    assert len(pushes) == 1


def test_scheduler_registers_jobs(s):
    keys = [k for k, _ in scheduler.due_jobs(datetime(2026, 10, 21, 14, 10, tzinfo=timezone.utc), s, set())]
    assert any(k.startswith("supervisor:") for k in keys)
    assert any(k.startswith("supervisor_daily:") for k in keys)                   # 14:10 UTC = 07:10 PT
    keys = [k for k, _ in scheduler.due_jobs(datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc), s, set())]
    assert not any(k.startswith("supervisor_daily:") for k in keys)
    s.raw["supervisor"] = {"enabled": False}
    assert not [k for k, _ in scheduler.due_jobs(NOW, s, set()) if k.startswith("supervisor")]


def test_read_only_never_touches_modes(s):
    from desk import autotrade, pairs_exec
    set_pairs_live(s, days_ago=5, reasons=["休市禁开"])
    before = (pairs_exec.get(s), autotrade.get(s))
    sv.checks(s, NOW, env())
    assert (pairs_exec.get(s), autotrade.get(s)) == before


def test_bad_supervisor_config_does_not_break_due_jobs(s):
    s.raw["supervisor"] = {"enabled": True, "daily_hour_pt": "07:05", "daily_minute_pt": None}
    keys = [k for k, _ in scheduler.due_jobs(datetime(2026, 10, 21, 14, 10, tzinfo=timezone.utc), s, set())]
    assert any(k.startswith("supervisor:") for k in keys) and any(k.startswith("pairs") or k.startswith("acctstop:") for k in keys)

    def boom(*a, **k):
        raise RuntimeError("x")
    import desk.supervisor as m
    orig = m.due
    m.due = boom
    try:
        keys = [k for k, _ in scheduler.due_jobs(NOW, s, set())]
    finally:
        m.due = orig
    assert any(k.startswith("acctstop:") for k in keys)


def test_daily_retried_hourly_until_pushed(s):
    t = datetime(2026, 10, 21, 14, 10, tzinfo=timezone.utc)
    done = {"supervisor_daily:2026-10-21"}                       # 第一次被标记完成，但没推成（线程忙）
    ks = [k for k, _ in sv.due(t, s, done)]
    assert "supervisor_daily:2026-10-21:7" in ks
    st = sv.load_state(s)
    st["daily"] = {"date": "2026-10-21", "pushed": True}
    sv.save_state(s, st)
    assert not [k for k, _ in sv.due(t, s, done) if k.startswith("supervisor_daily")]


def test_journal_streaming_and_unavailable(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setattr(sv.shutil, "which", lambda x: "/bin/journalctl")
    real = subprocess.Popen

    def fake(script):
        def _p(cmd, **kw):
            return real(["bash", "-c", script], **kw)
        return _p
    monkeypatch.setattr(sv.subprocess, "Popen", fake("echo ok; echo 'pairs data: 抓数据超过 45s'; echo 'pairs data: 抓数据超过 45s'"))
    assert sv._journal_timeouts() == 2
    monkeypatch.setattr(sv.subprocess, "Popen", fake("echo 'Not seeing messages' >&2"))
    assert sv._journal_timeouts() is None                         # stderr 有内容 → 当作查不了
    monkeypatch.setattr(sv.subprocess, "Popen", fake("echo '-- No journal files were opened due to insufficient permissions. --'"))
    assert sv._journal_timeouts() is None
    monkeypatch.setattr(sv.subprocess, "Popen", fake("exit 1"))
    assert sv._journal_timeouts() is None


def test_flapping_level_still_capped(s, monkeypatch):
    pushes = []
    monkeypatch.setattr(sv.notify, "push", lambda *a, **k: pushes.append(a) or True)
    lv = {"v": "warn"}
    monkeypatch.setattr(sv, "checks", lambda st, now=None, env=None: [sv.finding(lv["v"], "k", "t", "b", "")])
    for i in range(12):                                            # 提醒 / 警报来回跳，间隔 7 小时
        lv["v"] = "alert" if i % 2 else "warn"
        sv.job_hourly(s, NOW + timedelta(hours=7 * i))
    assert len(pushes) <= 5      # 升级会立刻推，但每个 key 总次数有上限（max_repeats=4，升级最多再多一次）


def test_ledger_tolerates_half_written_last_line(s):
    from desk.ledger import Ledger
    led = Ledger(s.data, "x")
    led.append("a", "w", {})
    with open(led.path, "a") as f:
        f.write('{"kind": "b", "wee')
    assert [r["kind"] for r in led.all()] == ["a"]
