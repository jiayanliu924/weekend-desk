"""假设 #23 隔夜回测：用合成 K 线证明窗口时间（收盘/决策/重开）、取价和偏离计算是对的。不碰网络。"""
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from desk import config
from desk import backtest_lib as bl
from desk import overnight_backtest as ob

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
IV = 15 * 60 * 1000


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    return config.load(tmp_path)


def et(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)


def synth_candles(start: datetime, end: datetime, px_at_end) -> list[dict]:
    """15 分钟 K 线，[k, k+15m)；收盘价 = px_at_end(区间末时刻)。"""
    out, k = [], int(start.timestamp() * 1000) // IV * IV
    end_ms = int(end.timestamp() * 1000)
    while k < end_ms:
        p = px_at_end(datetime.fromtimestamp((k + IV) / 1000, tz=UTC))
        out.append({"t": k, "o": p, "h": p, "l": p, "c": p, "v": 1.0})
        k += IV
    return out


# ---------------------------------------------------------------- 日历与窗口时间
def test_calendar():
    assert ob.is_trading_day(date(2026, 10, 12))            # 哥伦布日 NYSE 照常开
    assert not ob.is_trading_day(date(2026, 11, 26))        # 感恩节
    assert not ob.is_trading_day(date(2026, 10, 10))        # 周六
    assert ob.next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    assert ob.session_close(date(2026, 11, 27)).hour == 13  # 感恩节次日提前收盘
    assert ob.session_close(date(2026, 10, 6)).hour == 16


def test_window_kinds_and_times_summer():
    ws = ob.overnight_windows(date(2026, 10, 5), date(2026, 10, 13))
    by = {w.day: w for w in ws}
    assert [w.kind for w in ws] == ["overnight"] * 4 + ["weekend", "overnight"]   # 周一..周四隔夜、周五周末、周一隔夜
    w = by[date(2026, 10, 6)]
    assert w.next_day == date(2026, 10, 7)
    # EDT = UTC-4：16:00 ET = 20:00 UTC；09:15 ET = 13:15 UTC；09:30 = 13:30；平仓 09:35；04:00 = 08:00 UTC
    assert w.close == datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
    assert w.decision == datetime(2026, 10, 7, 13, 15, tzinfo=UTC)
    assert w.reopen == datetime(2026, 10, 7, 13, 30, tzinfo=UTC)
    assert w.exit == datetime(2026, 10, 7, 13, 35, tzinfo=UTC)
    assert w.early_decision == datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    assert by[date(2026, 10, 9)].next_day == date(2026, 10, 12)
    # 最后一个交易日没有 D' 在范围内 → 不出窗口
    assert date(2026, 10, 13) not in by


def test_window_times_dst_switch_and_holiday():
    # 2026-11-01 夏令时结束：周五 10-30 收盘还是 EDT（20:00 UTC），周一 11-02 开盘已是 EST（09:30 = 14:30 UTC）
    ws = {w.day: w for w in ob.overnight_windows(date(2026, 10, 30), date(2026, 11, 30))}
    w = ws[date(2026, 10, 30)]
    assert w.kind == "weekend" and w.close == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    assert w.decision == datetime(2026, 11, 2, 14, 15, tzinfo=UTC) and w.reopen == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    w = ws[date(2026, 11, 3)]
    assert w.kind == "overnight" and w.close == datetime(2026, 11, 3, 21, 0, tzinfo=UTC)   # EST 16:00 = 21:00 UTC
    # 感恩节：周三 11-25 → 周五 11-27，跨假日；11-27 提前收盘 13:00 → 11-27 → 11-30 是周末窗口，收盘 18:00 UTC
    assert ws[date(2026, 11, 25)].kind == "holiday" and ws[date(2026, 11, 25)].next_day == date(2026, 11, 27)
    assert date(2026, 11, 26) not in ws
    w = ws[date(2026, 11, 27)]
    assert w.kind == "weekend" and w.close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


# ---------------------------------------------------------------- 取价与偏离
def test_price_lookup_and_dev():
    D, N = date(2026, 10, 6), date(2026, 10, 7)

    def px(t_end: datetime) -> float:
        t = t_end.astimezone(ET)
        if t.date() == D and t.time() <= datetime(2000, 1, 1, 16, 0).time():
            return 100.0                            # 收盘前一直 100
        if t.date() == N and t.hour == 4 and t.minute == 0:
            return 100.5                            # 04:00 ET 那根收盘价
        if t.date() == N and (t.hour, t.minute) == (9, 15):
            return 101.0                            # 决策那根（09:00–09:15）收盘价
        if t.date() == N and (t.hour, t.minute) == (9, 45):
            return 100.2                            # 重开后第一根（09:30–09:45）收盘价
        return 100.9                                # 其他时刻放个干扰值，证明取价没取错根
    cs = synth_candles(et(D, 9, 0), et(N, 12, 0), px)
    wins = [w for w in ob.overnight_windows(D, N) if w.day == D]
    rows = ob.window_rows(cs, wins, "15m", "NVDA")
    assert len(rows) == 1
    r = rows[0]
    assert r["close_px"] == 100.0 and r["dec_px"] == 101.0 and r["reopen_px"] == 100.2 and r["early_px"] == 100.5
    assert r["dev_bps"] == pytest.approx(100.0)
    assert r["dev_early_bps"] == pytest.approx(50.0)
    assert r["y_bps"] == pytest.approx(20.0)
    assert r["move_from_dec_bps"] == pytest.approx((100.2 / 101.0 - 1) * 1e4)


def test_missing_candles_are_bridged_but_not_too_far():
    D, N = date(2026, 10, 6), date(2026, 10, 7)
    cs = synth_candles(et(D, 9, 0), et(N, 12, 0), lambda t: 100.0)
    by_t = {c["t"]: c for c in cs}
    k_close = int(et(D, 15, 45).timestamp() * 1000)
    del by_t[k_close]                                   # 收盘那根没有 → 往前找一根
    by_t[k_close - IV]["c"] = 99.0
    assert ob.price_at_or_before(by_t, IV, et(D, 16, 0)) == 99.0
    for i in range(4):                                  # 连续 4 根都没有 → None
        by_t.pop(k_close - i * IV, None)
    assert ob.price_at_or_before(by_t, IV, et(D, 16, 0)) is None
    # 重开：09:35 之后第一根收盘 = 09:30 那根（收盘于 09:45）
    k_open = int(et(N, 9, 30).timestamp() * 1000)
    by_t[k_open]["c"] = 103.0
    assert ob.price_at_or_after(by_t, IV, et(N, 9, 35)) == 103.0


# ---------------------------------------------------------------- 反向交易统计
def test_fade_stats_numbers(s):
    mk = lambda day, dev, move: {"name": "X", "day": day, "t_decision": day, "dev_bps": dev, "y_bps": dev + move,  # noqa: E731
                                 "move_from_dec_bps": move}
    rows = [mk("d1", +50, -30),    # 反向做空，赚 30 → 净 18
            mk("d2", -40, +10),    # 反向做多，赚 10 → 净 -2
            mk("d3", +10, -5),     # 不到门槛，不出手
            mk("d4", +100, +40)]   # 反向做空，亏 40 → 净 -52
    st = ob.fade_stats(rows, s, notional_usd=2000.0)
    assert st["threshold_bps"] == 14 and st["fee_bps"] == 12
    assert st["n"] == 4 and st["trades"] == 3
    assert st["frac_over"] == pytest.approx(0.75)
    assert st["revert_rate"] == pytest.approx(2 / 3) and st["win_rate"] == pytest.approx(1 / 3)
    assert st["total_net_bps"] == pytest.approx(18 - 2 - 52)
    assert st["total_net_usd"] == pytest.approx(-36 / 1e4 * 2000)
    assert st["worst"]["day"] == "d4" and st["worst"]["net_bps"] == pytest.approx(-52)
    # 权益 3.6 → 3.2 → -7.2；峰值 3.6 → 最大回撤 -10.8
    assert st["max_drawdown_usd"] == pytest.approx(-10.8)
    assert st["revert_rate_all"] == pytest.approx(0.75)
    # 门槛抬到 60 → 只剩 d4
    assert ob.fade_stats(rows, s, threshold_bps=60)["trades"] == 1


# ---------------------------------------------------------------- 端到端 + 缓存回退
def test_run_end_to_end_with_synth(s, monkeypatch):
    start, end = et(date(2026, 9, 28), 0), et(date(2026, 10, 10), 0)
    cs = synth_candles(start, end, lambda t: 100.0 + (t.minute == 15) * 0.3)   # 每根 xx:15 收盘的 K 线 +30 bps
    monkeypatch.setattr(ob, "get_candles", lambda settings, coin, days: (cs, "15m", "合成"))
    md = ob.run(s, names=("NVDA", "SP500"))
    out = Path(s.data) / "reports" / "overnight_backtest.md"
    assert out.exists() and "假设 #23" in md and "| NVDA | 隔夜 09:15 |" in md
    assert "周末(线上)" in md and "## 结论" in md


def test_fetch_failure_falls_back_to_cache(s, monkeypatch):
    cs = synth_candles(et(date(2026, 10, 5), 0), et(date(2026, 10, 8), 0), lambda t: 100.0)
    ob.save_cache(s, "xyz:NVDA", "5m", cs)

    def boom(coin, interval, days):
        raise RuntimeError("ProxyError 403 Forbidden")
    monkeypatch.setattr(bl, "fetch_candles_paged", boom)
    got, iv, src = ob.get_candles(s, "xyz:NVDA", 30)
    assert iv == "5m" and len(got) == len(cs) and "本地缓存" in src and "403" in src
    with pytest.raises(RuntimeError, match="没有 K 线"):
        ob.get_candles(s, "xyz:TSLA", 30)
