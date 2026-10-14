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


# ---------------------------------------------------------------- 美股口径（--market us，11c）：合成日线上跑，不碰网络
def test_us_synthetic_rows_and_fees(s):
    since, until = date(2024, 1, 1), date(2024, 6, 30)
    px, iv = ob.synthetic_us_data(since, until)
    assert all(datetime.fromtimestamp(t / 1000, tz=UTC).weekday() < 5 for t in px)     # 只有工作日
    assert set(px) == set(iv)
    pcfg, ucfg = s["options"]["paper"], ob.us_cfg(s)
    assert ucfg == {"fee_per_contract_usd": 0.65, "contract_multiplier": 100, "slippage_pct": 1.0}
    rows = ob.us_event_rows(ob.event_calendar(since, until), px, iv, pcfg, ucfg, underlying="SPY")
    assert len(rows) == 16 and {r["kind"] for r in rows} == {"CPI", "FOMC", "NFP"}      # 没有周末样本
    r = next(x for x in rows if x["kind"] == "FOMC")
    assert r["iv"] == pytest.approx(0.22)                     # 锁定日（事件前一个交易日）VIX = 18 + 4
    assert r["lock"] < r["at"] and r["ratio"] == pytest.approx(r["rv"] / r["iv"])
    assert r["short"] + r["long"] == pytest.approx(-2 * r["fees"])
    assert r["n_contracts"] == pytest.approx(1000 / (r["S0"] * 100))
    # 费用 = 每张 0.65 × 2 腿 × 进出 + 1% 滑点（权利金 + 赔付）
    assert r["fees"] == pytest.approx(0.65 * 2 * 2 * r["n_contracts"] + 0.01 * (r["premium"] + r["payout"]))
    assert r["iv_over_trailing"] is not None and "ibit_options_listed" not in r
    # 费用参数可改
    st = ob.us_straddle_pnl(400.0, 404.0, 0.2, pcfg, {**ucfg, "fee_per_contract_usd": 0.0, "slippage_pct": 0.0})
    assert st["fees"] == 0 and st["short"] == pytest.approx(st["premium"] - st["payout"])
    # 两腿都归零（标的没动）时不用平仓，只有进场费
    st0 = ob.us_straddle_pnl(400.0, 400.0, 0.2, pcfg, ucfg)
    assert st0["payout"] == 0 and st0["fees"] == pytest.approx(0.65 * 2 * st0["n_contracts"] + 0.01 * st0["premium"])


def test_us_long_fixture_implied_rich(s):
    since, until = date(2021, 1, 1), date(2025, 12, 31)
    px, iv = ob.synthetic_us_data(since, until)
    rows = ob.us_event_rows(ob.event_calendar(since, until), px, iv, s["options"]["paper"], ob.us_cfg(s))
    a = ob.aggregate(rows, "short")
    assert a["n"] > 150 and 0.4 < a["geo_ratio"] < 1.0           # 合成：实际 = 隐含 × 0.85（单日估计的几何均值还会再偏低）
    assert ob.by_kind_year(rows)[("ALL", "ALL")]["n"] == a["n"]


def test_us_ibit_flag_and_report(s):
    since, until = date(2024, 9, 1), date(2025, 1, 31)
    px, iv = ob.synthetic_us_data(since, until)
    rows = ob.us_event_rows(ob.event_calendar(since, until), px, iv, s["options"]["paper"], ob.us_cfg(s), underlying="IBIT")
    assert any(not r["ibit_options_listed"] for r in rows) and any(r["ibit_options_listed"] for r in rows)
    assert all(r["ibit_options_listed"] == (date.fromisoformat(r["at"][:10]) >= ob.IBIT_OPTIONS_SINCE) for r in rows)
    md = ob.report_md("IBIT", since, until, rows, "合成", s["options"]["paper"], synthetic=True, market="us", ucfg=ob.us_cfg(s))
    assert "美股口径" in md and "BTC DVOL" in md and "2024-11-19" in md and "合成数据" in md and "不是真实期权链" in md
    md2 = ob.report_md("SPY", since, until, rows, "x", s["options"]["paper"], market="us", ucfg=ob.us_cfg(s))
    assert "VIX 近似" in md2 and "每张合约每次成交 0.65 美元" in md2 and "Robinhood" in md2 and "合成数据" not in md2


def test_us_run_and_cli(s, monkeypatch, capsys):
    md, rows = ob.run(s, "QQQ", date(2024, 3, 1), date(2024, 6, 30), synthetic=True, market="us", fee_per_contract=0.1)
    assert rows and "VXN 近似" in md and "每张合约每次成交 0.10 美元" in md
    assert (Path(s.data) / "reports" / "options_backtest_US_QQQ_SYNTHETIC.md").exists()
    with pytest.raises(ValueError):
        ob.run(s, "BTC", date(2024, 3, 1), date(2024, 4, 1), synthetic=True, market="us")
    monkeypatch.setattr("desk.config.load", lambda root=None: s)
    assert ob.main(["--market", "us", "--underlying", "spy", "--since", "2024-03-01", "--until", "2024-04-30", "--synthetic"]) == 0
    assert "美股口径，VIX 近似）：SPY" in capsys.readouterr().out
    # 网络被拦、没缓存 → 退出码 2，提示去 bot 服务器跑
    def boom(*a, **k):
        raise ob.NetworkBlocked("CONNECT tunnel failed, response 403")
    monkeypatch.setattr(ob, "fetch_stooq_daily", boom)
    monkeypatch.setattr(ob, "fetch_fred_daily", boom)
    assert ob.main(["--market", "us", "--underlying", "SPY", "--since", "2024-01-01"]) == 2
    out = capsys.readouterr().out
    assert "stooq.com" in out and "--market us --underlying SPY" in out
    # 有缓存 → 用缓存
    ob.save_series(s, "us_SPY_px_1d", {0: 400.0})
    ob.save_series(s, "us_VIX_1d", {0: 18.0})
    px, iv, src = ob.get_us_data(s, "SPY", date(2024, 1, 1), date(2024, 2, 1))
    assert px == {0: 400.0} and iv == {0: 18.0} and "本地缓存" in src


def test_us_csv_parsing_and_fallback(monkeypatch):
    stooq = "Date,Open,High,Low,Close,Volume\n2024-01-02,470,472,468,471.5,1000\n2024-01-03,471,473,469,470.2,900\n"
    d = ob.parse_daily_csv(stooq, "Date", "Close")
    assert d == {ob._day_ms(date(2024, 1, 2)): 471.5, ob._day_ms(date(2024, 1, 3)): 470.2}
    fred = "observation_date,VIXCLS\n2024-01-02,13.2\n2024-01-03,.\n2024-01-04,14.1\n"
    d = ob.parse_daily_csv(fred, "observation_date", "VIXCLS")
    assert d == {ob._day_ms(date(2024, 1, 2)): 13.2, ob._day_ms(date(2024, 1, 4)): 14.1}   # "." 跳过
    with pytest.raises(RuntimeError):
        ob.parse_daily_csv("a,b\n1,2\n", "Date", "Close")
    # stooq 被拦 → 退到 FRED
    def blocked(sym):
        raise ob.NetworkBlocked("403")
    assert ob._try_sources("x", [(blocked, "spy.us"), (lambda s_: {1: 2.0}, "SP500")]) == {1: 2.0}
    with pytest.raises(ob.NetworkBlocked):
        ob._try_sources("x", [(blocked, "spy.us"), (blocked, "SP500")])
    with pytest.raises(ob.NetworkBlocked):
        ob._try_sources("x", [(blocked, "ibit.us"), (lambda s_: {}, None)])      # None 源跳过
    # DVOL 小时 → 日（21:00 UTC 那一格，×100 变"点"）
    t = ob._day_ms(date(2024, 1, 2))
    daily = ob.dvol_daily_from_hourly({t + 20 * ob.H_MS: 0.5, t + 21 * ob.H_MS: 0.55, t + 22 * ob.H_MS: 0.6})
    assert list(daily) == [t] and daily[t] == pytest.approx(55.0)
