"""休市窗口回测：窗口时间（美股/韩股/ICE/事件，含夏令时与假日）、决策/平仓对齐、盈亏与 carry 数学、排名。不碰网络。"""
import math
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from desk import backtest_lib as bl
from desk import config
from desk import windows_backtest as wb

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc
IV = 15 * 60 * 1000


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    shutil.copy(ROOT / "events.toml", tmp_path / "events.toml")
    return config.load(tmp_path)


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def synth_candles(start: datetime, end: datetime, px_at_end) -> list[dict]:
    """15 分钟 K 线 [k, k+15m)；收盘价 = px_at_end(区间末时刻)。确定性，不用随机数。"""
    out, k = [], int(start.timestamp() * 1000) // IV * IV
    end_ms = int(end.timestamp() * 1000)
    while k < end_ms:
        p = px_at_end(datetime.fromtimestamp((k + IV) / 1000, tz=UTC))
        out.append({"t": k, "o": p, "h": p, "l": p, "c": p, "v": 1.0})
        k += IV
    return out


def wavy(t: datetime) -> float:
    """确定性"合成行情"：围绕 100 的慢波 + 以小时为周期的快波，保证 dev 有正有负、有的过门槛有的不过。"""
    h = t.timestamp() / 3600
    return 100.0 * (1 + 0.004 * math.sin(h / 7.0) + 0.0015 * math.sin(h * 1.3))


# ---------------------------------------------------------------- 美股（复用 overnight_backtest 的日历）
def test_us_windows_times_and_dst():
    ws = {(w.label, w.name): w for w in wb.us_windows(date(2026, 10, 5), date(2026, 11, 10), names=("NVDA",))}
    w = ws[("2026-10-06", "NVDA")]
    assert w.family == "美股隔夜"
    assert (w.close, w.decision, w.reopen, w.exit) == (utc(2026, 10, 6, 20), utc(2026, 10, 7, 13, 15), utc(2026, 10, 7, 13, 30), utc(2026, 10, 7, 13, 35))
    assert ws[("2026-10-09", "NVDA")].family == "美股周末"
    # 11-01 夏令时结束：11-03 收盘 16:00 EST = 21:00 UTC，决策 09:15 EST = 14:15 UTC
    w = ws[("2026-11-03", "NVDA")]
    assert w.close == utc(2026, 11, 3, 21) and w.decision == utc(2026, 11, 4, 14, 15) and w.exit == utc(2026, 11, 4, 14, 35)
    # 每个合约各一份
    assert len(wb.us_windows(date(2026, 10, 5), date(2026, 10, 7))) == 2 * len(wb.US_EQUITY)


# ---------------------------------------------------------------- 韩股
def test_krx_calendar():
    assert not wb.is_krx_trading_day(date(2026, 10, 9))          # 韩文日
    assert not wb.is_krx_trading_day(date(2026, 10, 5))          # 开天节补休
    assert not wb.is_krx_trading_day(date(2026, 9, 25))          # 中秋
    assert wb.is_krx_trading_day(date(2026, 10, 12))
    assert wb.next_krx_trading_day(date(2026, 10, 8)) == date(2026, 10, 12)
    assert wb.next_krx_trading_day(date(2026, 10, 2)) == date(2026, 10, 6)


def test_kr_windows_times_no_dst_and_holiday():
    ws = {w.label: w for w in wb.kr_windows(date(2026, 9, 14), date(2026, 10, 14))}
    # 周三 10-07 → 周四 10-08 隔夜：15:30 KST = 06:30 UTC；09:00 KST = 00:00 UTC；决策 08:45 = 前一日 23:45 UTC；平仓 09:05
    w = ws["2026-10-07"]
    assert w.family == "韩股隔夜" and w.name == "SMSN"
    assert (w.close, w.decision, w.reopen, w.exit) == (utc(2026, 10, 7, 6, 30), utc(2026, 10, 7, 23, 45), utc(2026, 10, 8, 0, 0), utc(2026, 10, 8, 0, 5))
    # 周四 10-08 → 韩文日 + 周末 → 周一 10-12：假日窗
    w = ws["2026-10-08"]
    assert w.family == "韩股假日" and w.reopen == utc(2026, 10, 12, 0, 0)
    assert "2026-10-09" not in ws and "2026-10-10" not in ws
    # 普通周五 → 周一：周末窗
    assert ws["2026-09-18"].family == "韩股周末" and ws["2026-09-18"].reopen == utc(2026, 9, 21, 0, 0)
    # 首尔无夏令时：1 月和 7 月的 UTC 时刻一样
    jan = {w.label: w for w in wb.kr_windows(date(2026, 1, 5), date(2026, 1, 8))}["2026-01-06"]
    jul = {w.label: w for w in wb.kr_windows(date(2026, 7, 6), date(2026, 7, 9))}["2026-07-07"]
    assert jan.close.time() == jul.close.time() == utc(2026, 1, 1, 6, 30).time()
    assert jan.decision.time() == jul.decision.time() == utc(2026, 1, 1, 23, 45).time()


# ---------------------------------------------------------------- ICE Brent
def test_ice_windows_break_weekend_dst_and_closure():
    ws = {w.label: w for w in wb.ice_windows(date(2026, 10, 5), date(2026, 11, 15))}
    w = ws["2026-10-06"]                                           # 周二，EDT
    assert w.family == "Brent 日间休市" and w.name == "BRENTOIL"
    assert (w.close, w.decision, w.reopen, w.exit) == (utc(2026, 10, 6, 22), utc(2026, 10, 6, 23, 45), utc(2026, 10, 7, 0, 0), utc(2026, 10, 7, 0, 5))
    w = ws["2026-10-09"]                                           # 周五 → 周日 18:00 ET
    assert w.family == "Brent 周末" and w.close == utc(2026, 10, 9, 22) and w.reopen == utc(2026, 10, 11, 22)
    assert w.decision == utc(2026, 10, 11, 21, 45) and w.exit == utc(2026, 10, 11, 22, 5)
    assert "2026-10-10" not in ws and "2026-10-11" not in ws       # 周六、周日没有窗口
    w = ws["2026-11-10"]                                           # EST：18:00 = 23:00 UTC，20:00 = 01:00 UTC
    assert w.close == utc(2026, 11, 10, 23) and w.reopen == utc(2026, 11, 11, 1) and w.exit == utc(2026, 11, 11, 1, 5)
    # 耶稣受难日 2026-04-03：周四 04-02 的日间休市窗（次日休市）和周五周末窗都不生成
    gf = {w.label for w in wb.ice_windows(date(2026, 3, 30), date(2026, 4, 10))}
    assert "2026-04-01" in gf and "2026-04-02" not in gf and "2026-04-03" not in gf and "2026-04-06" in gf


# ---------------------------------------------------------------- 宏观事件
def test_event_windows_from_events_toml(s):
    now = utc(2026, 10, 12, 12)
    evs = wb.load_event_times(s)
    ids = {e[0] for e in evs}
    assert "CPI-2026-10" in ids and "FOMC-2026-09-16" in ids
    ws = wb.event_windows(s, date(2026, 1, 1), date(2026, 12, 31), names=("NVDA", "SMSN"), now=now)
    by = {(w.label, w.name): w for w in ws}
    # 2026-09-16 14:00 EDT = 18:00 UTC：锚 14:00，决策 17:00，重开 18:00，平仓 18:30
    w = by[("FOMC-2026-09-16", "NVDA")]
    assert w.family == "事件 FOMC"
    assert (w.close, w.decision, w.reopen, w.exit) == (utc(2026, 9, 16, 14), utc(2026, 9, 16, 17), utc(2026, 9, 16, 18), utc(2026, 9, 16, 18, 30))
    assert w.hold_hours == pytest.approx(1.5)
    # 1 月 FOMC 在 EST：14:00 = 19:00 UTC
    assert by[("FOMC-2026-01-28", "SMSN")].reopen == utc(2026, 1, 28, 19)
    # 未来事件（10-14 CPI）不出现
    assert ("CPI-2026-10", "NVDA") not in by
    # 所有合约都有事件窗
    assert {w.name for w in wb.event_windows(s, date(2026, 1, 1), date(2026, 12, 31), now=now)} == set(wb.ALL_INSTRUMENTS)


def test_windows_for_routes_by_instrument(s):
    now = utc(2026, 10, 12, 12)
    fams = lambda n: {w.family.split(" ")[0] for w in wb.windows_for(s, n, date(2026, 9, 1), date(2026, 10, 11), 15, 5, now)}  # noqa: E731
    assert fams("NVDA") >= {"美股隔夜", "美股周末", "事件"} and "韩股隔夜" not in fams("NVDA") and "Brent" not in fams("NVDA")
    assert fams("SMSN") >= {"韩股隔夜", "韩股周末", "事件"} and "美股隔夜" not in fams("SMSN")
    assert fams("BRENTOIL") >= {"Brent", "事件"} and "美股隔夜" not in fams("BRENTOIL")


# ---------------------------------------------------------------- 取价对齐
def test_window_rows_pick_the_right_candles_and_funding():
    w = wb.CWindow("韩股隔夜", "SMSN", "2026-10-07", utc(2026, 10, 7, 6, 30), utc(2026, 10, 7, 23, 45), utc(2026, 10, 8, 0, 0), utc(2026, 10, 8, 0, 5))

    def px(t_end: datetime) -> float:
        if t_end == utc(2026, 10, 7, 6, 30):
            return 100.0        # 锚：06:15–06:30 那根
        if t_end == utc(2026, 10, 7, 23, 45):
            return 99.0         # 决策：23:30–23:45 那根 → dev −100 bps
        if t_end == utc(2026, 10, 8, 0, 15):
            return 99.8         # 平仓：00:05 之后第一根（00:00–00:15）
        return 100.7            # 其他都是干扰值
    cs = synth_candles(utc(2026, 10, 7, 0), utc(2026, 10, 8, 6), px)
    funding = [{"t": int(utc(2026, 10, 7, h).timestamp() * 1000), "rate": 0.0001} for h in range(24)] + \
              [{"t": int(utc(2026, 10, 8, 0).timestamp() * 1000), "rate": 0.0003}]
    rows = wb.window_rows(cs, [w], "15m", funding)
    assert len(rows) == 1
    r = rows[0]
    assert (r["close_px"], r["dec_px"], r["reopen_px"]) == (100.0, 99.0, 99.8)
    assert r["dev_bps"] == pytest.approx(-100.0) and r["y_bps"] == pytest.approx(-20.0)
    assert r["move_from_dec_bps"] == pytest.approx((99.8 / 99.0 - 1) * 1e4)
    # (23:45, 00:05] 内只结算了 00:00 那一次 → 0.0003
    assert r["funding_sum"] == pytest.approx(0.0003)
    assert r["hold_hours"] == pytest.approx(20 / 60)
    # 没有费率数据 → None（统计时按 0）
    assert wb.window_rows(cs, [w], "15m", None)[0]["funding_sum"] is None


# ---------------------------------------------------------------- 盈亏数学
def test_trade_stats_fade_vs_follow_with_half_spread_and_carry(s):
    mk = lambda lbl, dev, move, fs=None: {"name": "X", "family": "F", "label": lbl, "day": lbl, "t_decision": lbl,  # noqa: E731
                                          "dev_bps": dev, "y_bps": dev + move, "move_from_dec_bps": move, "funding_sum": fs}
    rows = [mk("d1", +50, -30, 0.0005),   # 反向做空赚 30；空头收 5 bps carry
            mk("d2", -40, +10, 0.0005),   # 反向做多赚 10；多头付 5 bps
            mk("d3", +10, -5),            # 不过门槛
            mk("d4", +100, +40, None)]    # 反向做空亏 40；无费率
    fade = bl.trade_stats(rows, s, 1000.0, mode="fade", half_spread_bps=2.0, carry_key="funding_sum")
    assert fade["fee_bps"] == 14 and fade["threshold_bps"] == 16
    assert fade["n"] == 4 and fade["trades"] == 3
    assert fade["hit_rate"] == pytest.approx(2 / 3)
    assert fade["win_rate"] == pytest.approx(1 / 3)                       # 30-14>0, 10-14<0, -40-14<0
    assert fade["total_net_bps"] == pytest.approx(16 - 4 - 54)
    assert fade["total_net_usd"] == pytest.approx(-42 / 1e4 * 1000)
    carries = {p["day"]: p["carry_bps"] for p in fade["pnl"]}
    assert carries == pytest.approx({"d1": +5.0, "d2": -5.0, "d4": 0.0})
    assert fade["total_net_carry_usd"] == pytest.approx((-42 + 5 - 5) / 1e4 * 1000)
    assert fade["worst"]["day"] == "d4" and fade["worst"]["net_bps"] == pytest.approx(-54)
    # 顺向是镜像：毛收益取反，手续费一样
    fol = bl.trade_stats(rows, s, 1000.0, mode="follow", half_spread_bps=2.0, carry_key="funding_sum")
    assert fol["trades"] == 3 and fol["total_net_bps"] == pytest.approx(-44 - 24 + 26)
    assert fol["hit_rate"] == pytest.approx(1 / 3)
    assert {p["day"]: p["side"] for p in fol["pnl"]} == {"d1": 1, "d2": -1, "d4": 1}
    assert {p["day"]: p["carry_bps"] for p in fol["pnl"]} == pytest.approx({"d1": -5.0, "d2": +5.0, "d4": 0.0})
    # 门槛抬到 60 → 只剩 d4
    assert bl.trade_stats(rows, s, threshold_bps=60, mode="fade")["trades"] == 1
    # 空样本
    assert bl.trade_stats([], s)["trades"] == 0


def test_carry_helpers():
    rates = [{"t": int(utc(2026, 10, 7, h).timestamp() * 1000), "rate": 0.0001 * (h + 1)} for h in range(6)]
    # (01:30, 04:00] → 02:00, 03:00, 04:00 三次：0.0003 + 0.0004 + 0.0005
    assert bl.funding_sum(rates, utc(2026, 10, 7, 1, 30), utc(2026, 10, 7, 4)) == pytest.approx(0.0012)
    assert bl.funding_sum([], utc(2026, 10, 7, 1), utc(2026, 10, 7, 4)) is None
    assert bl.carry_bps(-1, 0.0012) == pytest.approx(+12.0)    # 空头收
    assert bl.carry_bps(+1, 0.0012) == pytest.approx(-12.0)    # 多头付
    assert bl.carry_bps(+1, None) == 0.0 and bl.carry_bps(0, 0.1) == 0.0


# ---------------------------------------------------------------- 排名
def test_rank_table_orders_by_avg_net_then_total(s):
    mk = lambda fam, n, lbl, dev, move: {"name": n, "family": fam, "label": lbl, "day": lbl, "t_decision": lbl,  # noqa: E731
                                         "dev_bps": dev, "y_bps": dev + move, "move_from_dec_bps": move, "funding_sum": None}
    rows = [mk("A", "X", "1", 100, -60), mk("A", "X", "2", -100, +60),      # 反向每笔 +46，顺向 −74
            mk("B", "Y", "1", 100, +30), mk("B", "Y", "2", 100, +30),       # 顺向每笔 +16，反向 −44
            mk("C", "Z", "1", 5, 1)]                                        # 不过门槛，不出现
    ranked = wb.rank_table(rows, s, 1000.0, 2.0)
    key = [(x["family"], x["name"], x["mode"]) for x in ranked]
    assert key == [("A", "X", "fade"), ("B", "Y", "follow"), ("B", "Y", "fade"), ("A", "X", "follow")]
    assert ranked[0]["avg_net_bps"] == pytest.approx(46) and ranked[0]["total_net_usd"] == pytest.approx(9.2)
    assert ranked[1]["avg_net_bps"] == pytest.approx(16) and ranked[1]["trades"] == 2
    assert ranked[0]["win_rate"] == 1.0 and ranked[0]["hit_rate"] == 1.0
    fams = wb.family_table(rows, s, 1000.0, 2.0)
    assert [x["family"] for x in fams] == ["A", "A", "B", "B", "C", "C"]
    assert fams[4]["st"]["trades"] == 0


# ---------------------------------------------------------------- 端到端（合成数据）
def test_run_end_to_end_with_synth(s, monkeypatch):
    now = utc(2026, 10, 12, 12)
    cs = synth_candles(utc(2026, 9, 1), now, wavy)
    funding = [{"t": int(utc(2026, 9, 1).timestamp() * 1000) + h * 3_600_000, "rate": 0.00005} for h in range(41 * 24)]
    monkeypatch.setattr(bl, "get_candles", lambda settings, coin, days: (cs, "15m", "合成"))
    monkeypatch.setattr(bl, "get_funding", lambda settings, coin, days: (funding, "合成"))
    md = wb.run(s, date(2026, 9, 8), 1000.0, 2.0, now=now)
    out = Path(s.data) / "reports" / "windows_backtest.md"
    assert out.exists()
    for sec in ("## 结论（中文）", "## 窗口排名", "## 按窗口类型汇总", "## 门槛扫描", "## 口径说明"):
        assert sec in md
    for fam in ("美股隔夜", "美股周末", "韩股隔夜", "韩股周末", "韩股假日", "Brent 日间休市", "Brent 周末", "事件 FOMC"):
        assert fam in md, fam
    assert "| 美股隔夜 | 反向 |" in md and "| 美股隔夜 | 顺向 |" in md
    assert "SMSN" in md and "BRENTOIL" in md and "XYZ100" in md
    # --since 之前的窗口不进来
    assert "2026-09-04" not in md


def test_cli_since_parses_and_runs_offline(s, monkeypatch, capsys):
    cs = synth_candles(utc(2026, 9, 20), utc(2026, 10, 12, 12), wavy)
    monkeypatch.setattr(bl, "get_candles", lambda settings, coin, days: (cs, "15m", "合成"))
    monkeypatch.setattr(bl, "get_funding", lambda settings, coin, days: ([], "无"))
    monkeypatch.setattr(bl, "hyperliquid_reachable", lambda: (False, "ProxyError 403"))
    monkeypatch.setattr(config, "load", lambda: s)
    assert wb.main(["--since", "2026-09-28", "--notional", "500"]) == 0
    cap = capsys.readouterr()
    assert "api.hyperliquid.xyz" in cap.err and "bot 服务器" in cap.err
    assert "2026-09-28 起" in cap.out and "$500" in cap.out


# ---------------------------------------------------------------- 没网时的回退
def test_funding_fallbacks(s, monkeypatch):
    def boom(coin, start_ms, end_ms=None):
        raise RuntimeError("ProxyError 403 Forbidden")
    monkeypatch.setattr(bl, "fetch_funding_history", boom)
    rows, src = bl.get_funding(s, "xyz:NVDA", 30)
    assert rows == [] and "carry 按 0 计" in src
    bl.save_funding_cache(s, "xyz:NVDA", [{"t": 1, "rate": 0.0001}])
    rows, src = bl.get_funding(s, "xyz:NVDA", 30)
    assert rows == [{"t": 1, "rate": 0.0001}] and "本地缓存" in src and "403" in src


def test_all_sources_failed_report_prints_host_instructions(s, monkeypatch):
    def boom(settings, coin, days):
        raise RuntimeError("没有 K 线：网络失败（ProxyError 403），本地也没有缓存")
    monkeypatch.setattr(bl, "get_candles", boom)
    monkeypatch.setattr(bl, "get_funding", lambda settings, coin, days: ([], "无"))
    md = wb.run(s, date(2026, 9, 1), now=utc(2026, 10, 12))
    assert "全是空表" in md and "python3 -m desk.windows_backtest --since" in md and "没有任何窗口样本" in md
