"""11b DVOL 事件波动率回测：日历、合成数据上的逐事件计算、汇总与门槛扫描、网络被拦时的行为。不碰网络。"""
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from desk import config
from desk import options_backtest as ob

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


@pytest.fixture
def s(tmp_path):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    return config.load(tmp_path)


def test_calendar_times_and_counts():
    evs = ob.event_calendar(date(2024, 1, 1), date(2024, 12, 31))
    by = {}
    for e in evs:
        by.setdefault(e.kind, []).append(e)
    assert len(by["FOMC"]) == 8 and len(by["CPI"]) == 12 and len(by["NFP"]) == 12 and len(by["WEEKEND"]) == 52
    # 2024-06-12 CPI 08:30 ET（EDT）= 12:30 UTC，锁定 11:30；2024-12-18 FOMC 14:00 ET（EST）= 19:00 UTC
    cpi = next(e for e in by["CPI"] if e.at.date() == date(2024, 6, 12))
    assert cpi.at == datetime(2024, 6, 12, 12, 30, tzinfo=UTC) and cpi.lock == datetime(2024, 6, 12, 11, 30, tzinfo=UTC)
    fomc = next(e for e in by["FOMC"] if e.at.date() == date(2024, 12, 18))
    assert fomc.at == datetime(2024, 12, 18, 19, 0, tzinfo=UTC)
    assert all(e.at.weekday() == 5 and e.at.hour == 0 for e in by["WEEKEND"])
    # 2025 停摆：10 月 CPI/非农取消，改期日在表里
    e25 = ob.event_calendar(date(2025, 10, 1), date(2025, 12, 31), weekends=False)
    assert sorted(e.at.date() for e in e25 if e.kind == "NFP") == [date(2025, 11, 20), date(2025, 12, 16)]
    assert sorted(e.at.date() for e in e25 if e.kind == "CPI") == [date(2025, 10, 24), date(2025, 12, 18)]
    assert evs == sorted(evs, key=lambda e: e.at)
    # 线上 events.toml 的 2026-10-14 CPI 也在表里
    assert any(e.at.date() == date(2026, 10, 14) for e in ob.event_calendar(date(2026, 10, 1), date(2026, 10, 31)))


def test_synthetic_is_deterministic_and_hourly():
    d1, p1 = ob.synthetic_data(date(2024, 1, 1), date(2024, 2, 1))
    d2, p2 = ob.synthetic_data(date(2024, 1, 1), date(2024, 2, 1))
    assert d1 == d2 and p1 == p2
    ts = sorted(p1)
    assert all(b - a == ob.H_MS for a, b in zip(ts, ts[1:]))
    assert set(d1) == set(p1)


def test_event_rows_on_fixture(s):
    pcfg = s["options"]["paper"]
    since, until = date(2024, 1, 1), date(2024, 6, 30)
    dvol, px = ob.synthetic_data(since, until)
    rows = ob.event_rows(ob.event_calendar(since, until), dvol, px, pcfg)
    assert len(rows) == 42                      # 4 FOMC + 6 CPI + 6 NFP + 26 周六
    r = next(x for x in rows if x["kind"] == "FOMC")
    assert r["iv"] == pytest.approx(0.65)       # 事件前 24 小时 DVOL 抬高了 0.10
    assert r["ratio"] == pytest.approx(r["rv"] / r["iv"]) and r["short"] + r["long"] == pytest.approx(-2 * r["fees"])
    assert r["premium"] > 0 and r["fees"] > 0 and r["iv_over_trailing"] is not None
    # 合成数据里实际 = 隐含 × 0.85：几何均值应落在 0.85 附近，卖方整体赚钱
    a = ob.aggregate(rows, "short")
    assert 0.7 < a["geo_ratio"] < 1.0 and a["net"] > 0 and a["worst"] < 0 < a["best"]
    assert a["net_ex_best3"] == pytest.approx(a["net"] - sum(sorted((x["short"] for x in rows), reverse=True)[:3]))
    assert ob.aggregate([], "short") == {"n": 0}


def test_straddle_pnl_signs(s):
    pcfg = s["options"]["paper"]
    small = ob.straddle_pnl(50000, 50050, 0.5, 24, pcfg)
    big = ob.straddle_pnl(50000, 55000, 0.5, 24, pcfg)
    assert small["short"] > 0 > small["long"]
    assert big["long"] > 0 > big["short"]
    assert big["payout"] == pytest.approx(1000 / 50000 * 5000)


def test_sweep_and_report(s):
    since, until = date(2024, 1, 1), date(2024, 6, 30)
    dvol, px = ob.synthetic_data(since, until)
    rows = ob.event_rows(ob.event_calendar(since, until), dvol, px, s["options"]["paper"])
    sw = ob.threshold_sweep(rows)
    assert [t["thr"] for t in sw] == list(ob.THRESHOLDS)
    ns = [t["short"].get("n", 0) for t in sw]
    assert ns == sorted(ns, reverse=True)       # 门槛越高、卖的笔数越少
    md = ob.report_md("BTC", since, until, rows, "合成", s["options"]["paper"], synthetic=True)
    assert "合成数据" in md and "| FOMC | 2024 " in md and "| ALL | ALL " in md and "逐事件" in md
    md2 = ob.report_md("BTC", since, until, [], "x", s["options"]["paper"])
    assert "没有一个事件有完整数据" in md2 and "合成数据" not in md2


def test_run_synthetic_writes_labelled_report(s):
    md, rows = ob.run(s, "BTC", date(2024, 3, 1), date(2024, 4, 30), synthetic=True)
    assert rows and "合成数据" in md
    assert (Path(s.data) / "reports" / "options_backtest_BTC_SYNTHETIC.md").exists()


def test_blocked_network_without_cache_raises(s, monkeypatch):
    def boom(*a, **k):
        raise ob.NetworkBlocked("403 Forbidden")
    monkeypatch.setattr(ob, "fetch_dvol", boom)
    monkeypatch.setattr(ob, "fetch_candles", boom)
    with pytest.raises(ob.NetworkBlocked):
        ob.get_data(s, "BTC", date(2024, 1, 1), date(2024, 2, 1))
    # 有缓存 → 用缓存并说明网络不可达
    ob.save_series(s, "BTC_dvol_1h", {0: 0.5})
    ob.save_series(s, "BTC_perp_1h", {0: 40000.0})
    dvol, px, src = ob.get_data(s, "BTC", date(2024, 1, 1), date(2024, 2, 1))
    assert dvol == {0: 0.5} and px == {0: 40000.0} and "本地缓存" in src


def test_cli_blocked_exits_2(s, monkeypatch, capsys):
    monkeypatch.setattr("desk.config.load", lambda root=None: s)
    def boom(*a, **k):
        raise ob.NetworkBlocked("CONNECT tunnel failed, response 403")
    monkeypatch.setattr(ob, "fetch_dvol", boom)
    assert ob.main(["--currency", "BTC", "--since", "2024-01-01"]) == 2
    assert "bot 服务器" in capsys.readouterr().out
