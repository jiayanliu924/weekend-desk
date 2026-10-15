"""品种扩展（#13）：分类规则 + 覆盖文件、流动性门槛、config 段生成（经 config.load / clock.weekend_for_friday 往返）、
排名与相关折算、幂等 add、周五到周日禁改、离线提示。不碰网络（fixture_raw 是写死的假数据）。"""
import json
import math
import shutil
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from desk import backtest_lib as bl
from desk import clock, config
from desk import universe as u
from tests.conftest import core7

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
IV = 15 * 60 * 1000
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)   # 周一


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    core7(tmp_path / "config.toml")   # 断言按原 7 个合约（in_config 状态、追加 stanza 的顺序）
    (tmp_path / "knowledge").mkdir()
    shutil.copy(ROOT / "knowledge" / "universe_overrides.toml", tmp_path / "knowledge" / "universe_overrides.toml")
    return config.load(tmp_path)


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def synth_candles(start: datetime, end: datetime, px_at_end) -> list[dict]:
    out, k = [], int(start.timestamp() * 1000) // IV * IV
    end_ms = int(end.timestamp() * 1000)
    while k < end_ms:
        p = px_at_end(datetime.fromtimestamp((k + IV) / 1000, tz=UTC))
        out.append({"t": k, "o": p, "h": p, "l": p, "c": p, "v": 1.0})
        k += IV
    return out


def wavy(t: datetime) -> float:
    h = t.timestamp() / 3600
    return 100.0 * (1 + 0.004 * math.sin(h / 7.0) + 0.0015 * math.sin(h * 1.3))


def synth_get_candles(cs):
    return lambda settings, coin, days: (cs, "15m", "合成")


# ---------------------------------------------------------------- 发现数据解析
def test_parse_markets_and_main_dex_excluded():
    raw = u.fixture_raw()
    ms = {m.coin: m for m in u.parse_markets(raw)}
    assert "xyz:NVDA" in ms and "km:TOYOTA" in ms
    assert "BTC" not in ms and "ETH" not in ms                       # 主 dex 不进市场表
    assert u.main_dex_count(raw) == 3                                # 4 个里 1 个已下架
    m = ms["xyz:NVDA"]
    assert m.dex == "xyz" and m.symbol == "NVDA" and m.mark_px == 180.0 and m.open_interest == 20000
    assert m.oi_usd == pytest.approx(180.0 * 20000) and m.day_ntl_vlm == 6e6
    assert m.spread_bps == pytest.approx(5.0, abs=0.01)
    assert ms["xyz:OLDCO"].is_delisted and ms["xyz:MSFT"].growth_mode == "enabled" and ms["xyz:NVDA"].growth_mode is None


def test_discover_calls_each_dex_and_main_and_records_errors():
    calls = []

    def post(payload):
        calls.append(payload)
        if payload["type"] == "perpDexs":
            return [None, {"name": "xyz"}, {"name": "bad"}]
        if payload.get("dex") == "bad":
            raise RuntimeError("boom")
        return [{"universe": []}, []]
    raw = u.discover(post)
    assert [c["type"] for c in calls] == ["perpDexs", "metaAndAssetCtxs", "metaAndAssetCtxs", "metaAndAssetCtxs"]
    assert calls[1] == {"type": "metaAndAssetCtxs", "dex": "xyz"} and "dex" not in calls[3]
    assert set(raw["dexs"]) == {"xyz"} and "bad" in raw["errors"] and raw["main"] == [{"universe": []}, []]


# ---------------------------------------------------------------- 分类
def test_classify_rules_table():
    assert u.classify("xyz:NVDA", "xyz")[0] == "us_equity"
    assert u.classify("xyz:XYZ100", "xyz")[0] == "us_index"
    assert u.classify("xyz:SP500", "xyz")[0] == "us_index"
    assert u.classify("xyz:BRENTOIL", "xyz")[0] == "metals_energy"
    assert u.classify("xyz:GOLD", "xyz")[0] == "metals_energy"
    assert u.classify("xyz:SMSN", "xyz")[0] == "kr_equity"
    assert u.classify("xyz:HYNIX", "xyz")[0] == "kr_equity"
    assert u.classify("km:TOYOTA", "km")[0] == "jp_equity"
    assert u.classify("km:TENCENT", "km")[0] == "hk_equity"
    assert u.classify("km:ASML", "km")[0] == "eu_equity"
    assert u.classify("xyz:EURUSD", "xyz")[0] == "fx"
    assert u.classify("xyz:USD-JPY", "xyz")[0] == "fx"
    assert u.classify("xyz:USDUSD", "xyz")[0] == u.UNKNOWN                   # 同一货币不是货币对
    assert u.classify("km:005930.KS", "km")[0] == "kr_equity"                 # 交易所后缀
    assert u.classify("km:7203.T", "km")[0] == "jp_equity"
    assert u.classify("km:0700.HK", "km")[0] == "hk_equity"
    assert u.classify("km:SAP.DE", "km")[0] == "eu_equity"
    key, why = u.classify("xyz:TSMC", "xyz")
    assert key == u.UNKNOWN and "待人工确认" in why
    assert u.classify("xyz:nvda", "xyz")[0] == "us_equity"                    # 大小写不敏感


def test_classify_overrides_precedence():
    ov = {"markets": {"xyz:TSMC": "us_equity", "xyz:NVDA": "kr_equity"}, "exclude": {"xyz:GOLD": "预言机不明"},
          "dex_default": {"km": "jp_equity"}}
    assert u.classify("xyz:TSMC", "xyz", ov) == ("us_equity", "覆盖文件 [markets]")
    assert u.classify("xyz:NVDA", "xyz", ov)[0] == "kr_equity"                # 覆盖优先于规则表
    key, why = u.classify("xyz:GOLD", "xyz", ov)
    assert key == u.UNKNOWN and "排除" in why and "预言机不明" in why
    assert u.classify("km:WHATEVER", "km", ov) == ("jp_equity", "覆盖文件 [dex_default] km")
    assert u.classify("xyz:WHATEVER", "xyz", ov)[0] == u.UNKNOWN              # 没有 dex 兜底的还是 unknown
    with pytest.raises(ValueError):
        u.classify("xyz:FOO", "xyz", {"markets": {"xyz:FOO": "mars_equity"}})


def test_load_overrides_from_repo_file_parses_and_every_schedule_key_is_valid(s):
    ov = u.load_overrides(s.root)
    assert set(ov) >= {"markets", "exclude", "dex_default"}
    for v in list(ov["markets"].values()) + list(ov["dex_default"].values()):
        assert v in u.SCHEDULES
    assert u.load_overrides(Path("/nonexistent")) == {}


# ---------------------------------------------------------------- 流动性门槛
def test_liquidity_gate(s):
    g = u.gate_cfg(s)
    assert g["min_day_ntl_vlm_usd"] == 200000.0 and g["max_spread_bps"] == 20.0
    ok = u.Market("x:A", "x", "A", day_ntl_vlm=5e5, oi_usd=2e5, spread_bps=8.0)
    assert u.liquidity_gate(ok, g) == (True, [])
    low = u.Market("x:B", "x", "B", day_ntl_vlm=1e5, oi_usd=5e4, spread_bps=30.0)
    passed, why = u.liquidity_gate(low, g)
    assert not passed and len(why) == 3 and "成交额" in why[0] and "持仓" in why[1] and "点差" in why[2]
    none = u.Market("x:C", "x", "C")                                           # 缺数据按不过
    assert u.liquidity_gate(none, g)[0] is False
    # 门槛可配置
    assert u.liquidity_gate(low, {**g, "min_day_ntl_vlm_usd": 1e4, "min_oi_usd": 1e4, "max_spread_bps": 50})[0] is True


# ---------------------------------------------------------------- config 段生成 → config.load → clock 往返
def test_stanza_roundtrip_through_config_and_clock(s):
    insts = [u.stanza_for("xyz:HYNIX", "kr_equity"), u.stanza_for("km:TOYOTA", "jp_equity"),
             u.stanza_for("xyz:EURUSD", "fx"), u.stanza_for("xyz:AAPL", "us_equity"), u.stanza_for("km:ASML", "eu_equity")]
    added = u.append_stanzas(s.root / "config.toml", insts)
    assert added == ["HYNIX", "TOYOTA", "EURUSD", "AAPL", "ASML"]
    s2 = config.load(s.root)
    names = [i["name"] for i in s2.instruments]
    assert names[:7] == ["XYZ100", "SP500", "BRENTOIL", "NVDA", "TSLA", "INTC", "SMSN"] and names[7:] == added
    # 新段插在 [clock] 之前，其余配置没动
    assert s2["clock"]["resume"] == "18:00" and s2["universe"]["weekends"] == 12
    fri = date(2026, 10, 9)
    w = clock.weekend_for_friday(fri, s2, s2.inst("HYNIX"))
    ref = clock.weekend_for_friday(fri, s2, s2.inst("SMSN"))
    assert (w.close, w.resume, w.decision, w.exit) == (ref.close, ref.resume, ref.decision, ref.exit)
    assert w.resume == utc(2026, 10, 12, 0, 1) and w.wid == "2026-10-09|HYNIX" and w.coin == "xyz:HYNIX"
    w = clock.weekend_for_friday(fri, s2, s2.inst("TOYOTA"))                 # JST 无夏令时：15:30 = 06:30 UTC，09:00 = 00:00 UTC
    assert w.close == utc(2026, 10, 9, 6, 30) and w.resume == utc(2026, 10, 12, 0, 0) and w.decision == utc(2026, 10, 11, 23, 45)
    w = clock.weekend_for_friday(fri, s2, s2.inst("EURUSD"))                 # EDT：17:00 = 21:00 UTC
    assert w.close == utc(2026, 10, 9, 21) and w.resume == utc(2026, 10, 11, 21) and w.exit == utc(2026, 10, 11, 21, 5)
    w = clock.weekend_for_friday(fri, s2, s2.inst("AAPL"))                   # 同 NVDA：周五 20:00 EDT = 周六 00:00 UTC
    nv = clock.weekend_for_friday(fri, s2, s2.inst("NVDA"))
    assert (w.close, w.resume, w.decision, w.exit) == (nv.close, nv.resume, nv.decision, nv.exit)
    assert w.close == utc(2026, 10, 10, 0) and w.resume == utc(2026, 10, 12, 0)
    w = clock.weekend_for_friday(fri, s2, s2.inst("ASML"))                   # CEST：17:30 = 15:30 UTC，09:00 = 07:00 UTC
    assert w.close == utc(2026, 10, 9, 15, 30) and w.resume == utc(2026, 10, 12, 7)
    # 11 月：柏林冬令时 09:00 = 08:00 UTC
    assert clock.weekend_for_friday(date(2026, 11, 6), s2, s2.inst("ASML")).resume == utc(2026, 11, 9, 8)


def test_append_stanzas_is_idempotent_and_keeps_toml_valid(s):
    p = s.root / "config.toml"
    before = p.read_text()
    assert u.append_stanzas(p, [u.stanza_for("xyz:NVDA", "us_equity")]) == []     # 已存在 → 不写
    assert p.read_text() == before
    assert u.append_stanzas(p, [u.stanza_for("xyz:AAPL", "us_equity")], {"AAPL": "test"}) == ["AAPL"]
    assert u.append_stanzas(p, [u.stanza_for("xyz:AAPL", "us_equity")]) == []
    assert p.read_text().count("xyz:AAPL") == 1 and "# test" in p.read_text()
    tomllib.loads(p.read_text())
    assert u.stanza_text(u.stanza_for("xyz:AAPL", "us_equity")).startswith('[[market.instruments]]\nname = "AAPL"\ncoin = "xyz:AAPL"\ntz = "America/New_York"\n')


# ---------------------------------------------------------------- 周末回测与相关
def test_weekend_rows_pick_decision_minus_15_and_exit_plus_5(s):
    inst = u.stanza_for("xyz:AAPL", "us_equity")

    def px(t_end: datetime) -> float:
        if t_end == utc(2026, 10, 10, 0, 0):      # 周五 20:00 EDT 关门那根
            return 100.0
        if t_end == utc(2026, 10, 11, 23, 45):    # 决策：周日 19:45 EDT
            return 101.0                          # dev +100 bps
        if t_end == utc(2026, 10, 12, 0, 15):     # 平仓：20:05 之后第一根（20:00–20:15）
            return 100.2
        return 100.5
    cs = synth_candles(utc(2026, 10, 1), utc(2026, 10, 12, 12), px)
    rows = u.weekend_rows(s, inst, cs, "15m", 1, NOW)
    assert len(rows) == 1 and rows[0]["day"] == "2026-10-09" and rows[0]["name"] == "AAPL"
    assert rows[0]["dev_bps"] == pytest.approx(100.0) and rows[0]["y_bps"] == pytest.approx(20.0)
    assert rows[0]["move_from_dec_bps"] == pytest.approx((100.2 / 101.0 - 1) * 1e4)
    # 往前数 3 个周末；K 线不够的周末被跳过
    rows = u.weekend_rows(s, inst, cs, "15m", 3, NOW)
    assert [r["day"] for r in rows] == ["2026-10-02", "2026-10-09"]


def test_pearson_and_effective_samples():
    assert u.pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert u.pearson([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert u.pearson([1, 2, 3], [1, 2, 3]) is None                               # 样本太少
    assert u.pearson([1, 1, 1, 1], [1, 2, 3, 4]) is None                         # 零方差
    rows = [{"day": d, "y_bps": v} for d, v in (("a", 1), ("b", 2), ("c", 3), ("d", 4), ("e", 9))]
    ref = [{"day": d, "y_bps": v} for d, v in (("a", 2), ("b", 4), ("c", 6), ("d", 8), ("z", 0))]
    rho, n = u.correlation_with(rows, ref)
    assert rho == pytest.approx(1.0) and n == 4
    assert u.correlation_with(rows, []) == (None, 0)
    # 完全相关的 3 个名字 × 12 周末 = 36 个样本，有效只有 12；完全独立 → 36；一个名字不折算
    assert u.effective_samples([12, 12, 12], [1.0, 1.0, 1.0]) == pytest.approx(12.0)
    assert u.effective_samples([12, 12, 12], [0.0, None, 0.0]) == pytest.approx(36.0)
    assert u.effective_samples([12], [0.9]) == pytest.approx(12.0)
    assert u.effective_samples([], []) == 0.0


def test_scan_statuses_ranking_and_outputs(s):
    cs = synth_candles(utc(2026, 6, 1), NOW, wavy)
    res = u.scan(s, u.fixture_raw(), 12, NOW, get_candles=synth_get_candles(cs))
    st = {m["coin"]: m["status"] for m in res["markets"]}
    assert st["xyz:NVDA"] == "in_config" and st["xyz:SMSN"] == "in_config"
    assert st["xyz:AAPL"] == "candidate" and st["xyz:HYNIX"] == "candidate" and st["km:TOYOTA"] == "candidate"
    assert st["xyz:TSMC"] == "review" and st["km:ZZZ"] == "review"                 # 认不出
    assert st["km:SONY"] == "gate_fail" and st["xyz:GME"] == "gate_fail"            # 点差 / 成交额
    assert st["xyz:OLDCO"] == "delisted"
    assert "BTC" not in st and res["main_dex_markets"] == 3
    assert set(res["ranking"]) == {"xyz:AAPL", "xyz:AMD", "xyz:MSFT", "xyz:EURUSD", "xyz:HYNIX", "km:TOYOTA", "xyz:GOLD"}
    by = {m["coin"]: m for m in res["markets"]}
    # 排名按平均净 bps 从高到低
    avgs = [by[c]["backtest"]["avg_net_bps"] for c in res["ranking"]]
    assert avgs == sorted(avgs, reverse=True)
    b = by["xyz:AAPL"]["backtest"]
    assert b["n"] == 12 and b["trades"] > 0 and b["worst_bps"] is not None and b["worst_day"]
    assert 0 <= b["hit_rate"] <= 1 and b["total_net_usd"] == pytest.approx(b["avg_net_bps"] * b["trades"] / 10)
    # 同一合成行情、同一时间表 → 和 XYZ100 相关 = 1（SP500 同表）；韩股时间表不同 → 相关 < 1
    assert by["xyz:GOLD"]["backtest"]["rho_xyz100"] == pytest.approx(1.0)
    assert by["xyz:HYNIX"]["backtest"]["rho_xyz100"] < 0.999
    assert 0 < res["effective_samples"] < 7 * 12
    # 认不出的不回测；已在 config 的回测用 config 里的时间表
    assert by["xyz:TSMC"]["backtest"] is None and by["xyz:SMSN"]["backtest"]["n"] == 12
    md, js = u.write_outputs(s, res)
    assert md == Path(s.data) / "reports" / "universe.md" and js == Path(s.data) / "universe.json"
    txt = md.read_text()
    for sec in ("## 候选排名", "### 相关性提醒", "有效独立样本", "## 已在 config.toml", "## 分类成功但流动性不过门槛", "## 待人工确认", "## 已下架", "全部排除"):
        assert sec in txt, sec
    assert "| 1 | " + res["ranking"][0] in txt and "xyz:TSMC" in txt and "km:SONY" in txt
    assert json.loads(js.read_text())["ranking"] == res["ranking"]


def test_scan_respects_overrides_and_candles_failure(s):
    cs = synth_candles(utc(2026, 6, 1), NOW, wavy)
    ov = {"markets": {"xyz:TSMC": "us_equity"}, "exclude": {"xyz:AAPL": "测试排除"}, "dex_default": {}}
    res = u.scan(s, u.fixture_raw(), 12, NOW, get_candles=synth_get_candles(cs), overrides=ov)
    st = {m["coin"]: m["status"] for m in res["markets"]}
    assert st["xyz:TSMC"] == "candidate" and st["xyz:AAPL"] == "excluded" and "xyz:AAPL" not in res["ranking"]

    def boom(settings, coin, days):
        raise RuntimeError("没有 K 线：网络失败（ProxyError 403），本地也没有缓存")
    res = u.scan(s, u.fixture_raw(), 12, NOW, get_candles=boom)
    by = {m["coin"]: m for m in res["markets"]}
    assert by["xyz:AAPL"]["status"] == "candidate" and by["xyz:AAPL"]["backtest"]["n"] == 0 and "403" in by["xyz:AAPL"]["backtest"]["error"]
    assert res["ranking"] and "没有候选" not in u.report_md(res)


# ---------------------------------------------------------------- CLI：scan 离线提示、add 幂等与周内限制
def test_cli_scan_offline_prints_host_instructions(s, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda: s)
    monkeypatch.setattr(bl, "hyperliquid_reachable", lambda: (False, "ProxyError 403"))
    assert u.main(["scan"]) == 1
    err = capsys.readouterr().err
    assert "api.hyperliquid.xyz" in err and "bot 服务器" in err and "--fixture" in err
    # 有上次服务器拷回来的 universe_raw.json 就退回去用它
    (Path(s.data) / "universe_raw.json").write_text(json.dumps(u.fixture_raw()))
    cs = synth_candles(utc(2026, 6, 1), NOW, wavy)
    monkeypatch.setattr(bl, "get_candles", synth_get_candles(cs))
    assert u.main(["scan", "--weekends", "6"]) == 0
    cap = capsys.readouterr()
    assert "退回本地" in cap.err and "最近 6 个周末" in cap.out
    assert (Path(s.data) / "reports" / "universe.md").exists()


def test_cli_scan_with_fixture_file(s, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "load", lambda: s)
    monkeypatch.setattr(bl, "hyperliquid_reachable", lambda: (False, "ProxyError 403"))
    cs = synth_candles(utc(2026, 6, 1), NOW, wavy)
    monkeypatch.setattr(bl, "get_candles", synth_get_candles(cs))
    fx = tmp_path / "raw.json"
    fx.write_text(json.dumps(u.fixture_raw()))
    assert u.main(["scan", "--fixture", str(fx)]) == 0
    assert json.loads((Path(s.data) / "universe.json").read_text())["ranking"]


def test_cli_add_top_idempotent_and_weekday_guard(s, monkeypatch, capsys):
    real_load = config.load
    monkeypatch.setattr(config, "load", lambda root=None: s if root is None else real_load(root))
    assert u.main(["add", "--top", "3"]) == 1                                     # 还没 scan
    cs = synth_candles(utc(2026, 6, 1), NOW, wavy)
    res = u.scan(s, u.fixture_raw(), 12, NOW, get_candles=synth_get_candles(cs))
    u.write_outputs(s, res)
    capsys.readouterr()
    import argparse
    a = argparse.Namespace(top=3, force=False)
    assert u.cmd_add(s, a, now=NOW) == 0                                         # 周一
    out = capsys.readouterr().out
    top3 = [u.symbol_of(c) for c in res["ranking"][:3]]
    for n in top3:
        assert f"+ {n}" in out
    s2 = config.load(s.root)
    assert [i["name"] for i in s2.instruments][7:] == top3
    for n in top3:
        i = s2.inst(n)
        sched = u.SCHEDULES[next(m["schedule"] for m in res["markets"] if m["coin"] == i["coin"])]
        assert (i["tz"], i["close_day"], i["close"], i["resume_day"], i["resume"]) == tuple(sched[k] for k in ("tz", "close_day", "close", "resume_day", "resume"))
    # 第二次：全部已存在
    assert u.cmd_add(s, a, now=NOW) == 0
    out = capsys.readouterr().out
    assert "已存在，跳过" in out and "+ " not in out
    assert [i["name"] for i in config.load(s.root).instruments][7:] == top3
    # 周五到周日拒绝（周六 08:00 UTC = 周六 04:00 美东）；--force 放行
    assert u.cmd_add(s, argparse.Namespace(top=5, force=False), now=utc(2026, 10, 17, 8)) == 2
    assert "周一到周四" in capsys.readouterr().err
    assert u.cmd_add(s, argparse.Namespace(top=5, force=True), now=utc(2026, 10, 17, 8)) == 0
    assert len(config.load(s.root).instruments) == 7 + 5
    # 周四 23:30 美东还是周四；周五 00:30 美东就不行
    assert u.config_change_allowed(utc(2026, 10, 16, 3, 30), s) and not u.config_change_allowed(utc(2026, 10, 16, 4, 30), s)


# ---------------------------------------------------------------- 2026-10-15 修复：45 个候选只有 6 个拿到回测
US45 = sorted(next(n for lab, n, k in u._SYMBOL_RULES if k == "us_equity") - {"NVDA", "TSLA", "INTC"})[:45]


def raw45() -> dict:
    """45 个都过门槛的美股单票候选 + config 里的 7 个。"""
    def ctx(px=100.0):
        return {"funding": "0.00001", "openInterest": "50000", "dayNtlVlm": "5000000", "oraclePx": str(px), "markPx": str(px),
                "midPx": str(px), "impactPxs": [str(px * 0.9998), str(px * 1.0002)]}
    names = [f"xyz:{n}" for n in US45] + ["xyz:XYZ100", "xyz:SP500", "xyz:BRENTOIL", "xyz:NVDA", "xyz:TSLA", "xyz:INTC", "xyz:SMSN"]
    return {"fetched_at": "2026-10-12T12:00:00+00:00", "perp_dexs": [None, {"name": "xyz"}],
            "dexs": {"xyz": [{"universe": [{"name": n, "szDecimals": 2, "maxLeverage": 10} for n in names]}, [ctx() for _ in names]]},
            "main": [{"universe": []}, []], "errors": {}}


class FakeHL:
    """仿 Hyperliquid info：每 60 秒（假时钟）1200 权重；candleSnapshot 20 + 每 60 根 +1，最多返回 endTime 前 5000 根；超了就 429。
    另有一个"收集器"一直占着 300 权重（同 IP 的其他程序）。"""

    def __init__(self, candles):
        self.t = 0.0
        self.log: list[tuple[float, float]] = []
        self.candles = candles
        self.n429 = 0
        self.calls = 0

    def clock(self):
        return self.t

    def sleep(self, dt):
        self.t += dt

    def used(self):
        return sum(w for t, w in self.log if self.t - t < 60) + 300

    def post(self, payload):
        self.calls += 1
        self.t += 0.2
        if self.used() + 20 > 1200:
            self.n429 += 1
            self.log.append((self.t, 20))
            raise RuntimeError("Client error '429 Too Many Requests' for url 'https://api.hyperliquid.xyz/info'")
        end = payload["req"]["endTime"]
        cs = [c for c in self.candles if c["t"] <= end][-5000:]
        self.log.append((self.t, 20 + len(cs) // 60))
        return cs


def test_scan_backtests_every_candidate_under_rate_limit(s, monkeypatch):
    cs = synth_candles(utc(2026, 8, 1), NOW, wavy)
    hl = FakeHL(cs)
    pacer = bl.Pacer(clock=hl.clock, sleep=hl.sleep)
    monkeypatch.setattr(bl, "PACER", pacer)

    def gc(settings, coin, days):
        return bl.get_candles(settings, coin, days, max_cache_age_h=6, post=hl.post, pacer=pacer)
    res = u.scan(s, raw45(), 6, NOW, get_candles=gc, overrides={})
    by = {m["coin"]: m for m in res["markets"]}
    cands = [m for m in res["markets"] if m["status"] == "candidate"]
    assert len(cands) == 45
    assert all(m["backtest"]["n"] > 0 for m in cands), [m["coin"] for m in cands if not m["backtest"]["n"]]
    assert all(by[c]["backtest"]["n"] > 0 for c in ("xyz:NVDA", "xyz:SMSN"))
    assert pacer.slept > 0                                                    # 真的等过（没有节流时 ~6 个之后全 429）
    for t0, _ in hl.log:                                                      # 服务端窗口内权重从未超限
        assert sum(w for t, w in hl.log if t0 - 60 < t <= t0) + 300 <= 1200
    assert "回测覆盖：52 / 52" in u.report_md(res)
    calls = hl.calls                                                          # 第二次扫描：6 小时内的缓存直接用
    u.scan(s, raw45(), 6, NOW, get_candles=gc, overrides={})
    assert hl.calls == calls


def test_old_behaviour_reproduced_without_pacing_and_retry_then_report_explains(s, monkeypatch):
    """不节流、不重试时复现服务器上的现象：前几个有样本，之后"没有 K 线"（429）。报告必须列出原因，不能只写 0。"""
    import functools
    cs = synth_candles(utc(2026, 8, 1), NOW, wavy)
    hl = FakeHL(cs)
    nopace = bl.Pacer(budget=1e12, clock=hl.clock, sleep=hl.sleep)
    monkeypatch.setattr(bl, "PACER", nopace)
    monkeypatch.setattr(bl, "info_post", functools.partial(bl.info_post, retries=0))

    def gc(settings, coin, days):
        return bl.get_candles(settings, coin, days, post=hl.post, pacer=nopace)
    res = u.scan(s, raw45(), 6, NOW, get_candles=gc, overrides={})
    cands = [m for m in res["markets"] if m["status"] == "candidate"]
    ok = [m for m in cands if m["backtest"]["n"]]
    assert 0 < len(ok) < 45 and hl.n429 > 0
    md = u.report_md(res)
    assert "## 回测失败 / 样本为 0（原因）" in md and "429" in md and "回测覆盖：" in md


def test_why_empty_explains_missing_prices(s):
    inst = u.stanza_for("xyz:AAPL", "us_equity")
    # 只有周五的 K 线 → 关门价有、决策价（周日）没有
    cs = [c for c in synth_candles(utc(2026, 9, 1), NOW, wavy)
          if datetime.fromtimestamp(c["t"] / 1000, tz=UTC).weekday() == 4]
    b = u.backtest_candidate(s, inst, 4, NOW, 1000.0, 2.0, None, lambda st, coin, days: (cs, "15m", "合成"))
    assert b["n"] == 0 and "缺决策价" in b["error"] and b["missing"]["no_decision"] == 4


def test_save_cache_merges_history(s):
    a = synth_candles(utc(2026, 9, 1), utc(2026, 9, 3), wavy)
    b = synth_candles(utc(2026, 9, 2), utc(2026, 9, 5), wavy)
    bl.save_cache(s, "xyz:AAPL", "15m", a)
    bl.save_cache(s, "xyz:AAPL", "15m", b)
    got, _ = bl.load_cache_iv(s, "xyz:AAPL", "15m")
    assert [c["t"] for c in got] == sorted({c["t"] for c in a + b})


def test_info_post_retries_429_but_not_proxy_403():
    calls = []

    def post403(p):
        calls.append(p)
        raise RuntimeError("ProxyError 403 Forbidden")
    pacer = bl.Pacer(clock=lambda: 0.0, sleep=lambda dt: None)
    with pytest.raises(RuntimeError, match="403"):
        bl.info_post({"type": "meta"}, post=post403, pacer=pacer)
    assert len(calls) == 1
    n = []

    def post429(p):
        n.append(p)
        if len(n) < 3:
            raise RuntimeError("429 Too Many Requests")
        return [1, 2]
    assert bl.info_post({"type": "meta"}, post=post429, pacer=pacer) == [1, 2] and len(n) == 3 and pacer.slept > 0
