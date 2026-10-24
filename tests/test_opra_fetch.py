"""卡 D2：opra_fetch — 符号转换、选合约、时间窗、计划总价、超上限中止、续跑跳过。假 Alpaca + 假 Databento，不碰网络。"""
from datetime import date

import pandas as pd

from desk import opra_fetch as of


class Settings:
    def __init__(self, tmp, cap=100.0):
        self.raw = {"opra": {"max_cost_usd": cap}}
        self.data = tmp


def con(sym, exp, k):
    return {"symbol": sym, "expiration_date": exp, "strike_price": str(k)}


class FakeSrc:
    def __init__(self):
        self.cons = [con("SOXX251024P00270000", "2025-10-24", 270), con("SOXX251024C00295000", "2025-10-24", 295),
                     con("SOXX251024C00200000", "2025-10-24", 200),      # 超出 ±10%
                     con("1SOXX251024C00270000", "2025-10-24", 270),     # 调整合约，跳过
                     con("SOXX251001C00270000", "2025-10-01", 270)]      # DTE 太短（d=9/15 时 16 天 → 在内；见测试）

    def closes(self, und, s, e):
        return {"2025-09-10": 270.0, "2025-09-11": 270.0, "2025-10-01": 270.0}

    def contracts(self, und, a, b):
        return self.cons

    def close_times(self, s, e):
        return {"2025-09-11": "13:00"}


class FakeStore:
    def to_df(self, price_type="float", map_symbols=True):
        idx = pd.DatetimeIndex(["2025-09-10T19:31:00Z"], name="ts_recv")
        return pd.DataFrame({"symbol": ["SOXX  251024P00270000"], "bid_px_00": [1.0], "ask_px_00": [1.1],
                             "bid_sz_00": [5], "ask_sz_00": [6]}, index=idx)


class FakeDB:
    def __init__(self, per=1.5):
        self.per = per
        self.cost_calls = 0
        self.get_calls = 0
        self.metadata = self
        self.timeseries = self

    def get_cost(self, **kw):
        self.cost_calls += 1
        assert kw["schema"] == "cbbo-1m" and kw["stype_in"] == "raw_symbol" and kw["dataset"] == "OPRA.PILLAR"
        return self.per

    def get_range(self, **kw):
        self.get_calls += 1
        return FakeStore()


def test_osi_conversion():
    s = of.occ_to_osi("SOXX251024P00270000")
    assert s == "SOXX  251024P00270000" and len(s) == 21
    assert of.occ_to_osi("SPY251024C00500500") == "SPY   251024C00500500"
    assert of.occ_to_osi("NVDA251024C00100000") == "NVDA  251024C00100000"
    assert of.occ_to_osi("1SOXX251024C00270000") is None
    assert of.occ_expiry("SOXX251024P00270000") == date(2025, 10, 24)


def test_contract_filter_dte_and_strike():
    cons = FakeSrc().cons
    d = date(2025, 9, 10)  # 10/24 = +44 天；10/01 = +21 天
    got = of.needed_contracts(cons, d, 270.0)
    assert got == ["SOXX251001C00270000", "SOXX251024C00295000", "SOXX251024P00270000"]
    # 边界：±10% 含端点 297/243；DTE 14/60 含端点
    edge = [con("SOXX250924C00297000", "2025-09-24", 297), con("SOXX251110C00243000", "2025-11-09", 243),
            con("SOXX250923C00270000", "2025-09-23", 270), con("SOXX251111C00270000", "2025-11-10", 270)]
    assert len(of.needed_contracts(edge, d, 270.0)) == 2
    assert of.ContractIndex(cons + edge).needed(d, 270.0) == of.needed_contracts(cons + edge, d, 270.0)


def test_windows():
    a, b = of.day_window(date(2025, 9, 10))
    assert (a.hour, a.minute, b.hour, b.minute) == (15, 30, 16, 0)
    assert b.utcoffset().total_seconds() == -4 * 3600
    a, b = of.day_window(date(2025, 11, 28), "13:00")
    assert (a.hour, a.minute, b.hour) == (12, 30, 13)
    assert b.utcoffset().total_seconds() == -5 * 3600


def test_chunking_requests():
    needed = {"2025-09-10": [f"SOXX251024C{i:08d}" for i in range(4500)]}
    reqs = of.build_requests("SOXX", needed, {})
    assert [len(r["symbols"]) for r in reqs] == [2000, 2000, 500]


def test_plan_cost_sum_and_meta(tmp_path, capsys):
    db = FakeDB(per=1.5)
    rc = of.run(Settings(tmp_path), FakeSrc(), db, ["SOXX"], date(2025, 9, 1), date(2025, 10, 5), "plan")
    assert rc == 0 and db.get_calls == 0
    out = capsys.readouterr().out
    # 9 月 2 天、10 月 1 天，都 ≤3 天 → 全部精确询价 3 次，合计 4.50，x1.2 = 5.40
    assert db.cost_calls == 3 and "4.50" in out and "5.40" in out
    assert (tmp_path / "opra" / "meta" / "closes_SOXX.json").exists() and (tmp_path / "opra" / "meta" / "contracts_SOXX.tsv").exists()
    assert not (tmp_path / "opra" / "cbbo1m").exists()


def test_cap_abort_and_yes_required(tmp_path):
    db = FakeDB(per=30.0)  # 3 × 30 = 90 < 100，但 x1.2 = 108 > 100
    rc = of.run(Settings(tmp_path), FakeSrc(), db, ["SOXX"], date(2025, 9, 1), date(2025, 10, 5), "download", yes=True)
    assert rc == 2 and db.get_calls == 0
    db = FakeDB(per=1.0)
    rc = of.run(Settings(tmp_path), FakeSrc(), db, ["SOXX"], date(2025, 9, 1), date(2025, 10, 5), "download", yes=False)
    assert rc == 2 and db.get_calls == 0


def test_download_and_resume(tmp_path):
    db = FakeDB(per=1.0)
    rc = of.run(Settings(tmp_path), FakeSrc(), db, ["SOXX"], date(2025, 9, 1), date(2025, 10, 5), "download", yes=True)
    assert rc == 0
    p = tmp_path / "opra" / "cbbo1m" / "SOXX" / "2025-09.parquet"
    assert p.exists() and (tmp_path / "opra" / "cbbo1m" / "SOXX" / "2025-10.parquet").exists()
    df = pd.read_parquet(p)
    assert list(df.columns) == ["ts", "symbol", "bid_px", "ask_px", "bid_sz", "ask_sz"]
    assert df.bid_px.dtype == float and len(df) == 2        # 9/10、9/11 各一行
    first = db.get_calls
    assert first == 3
    # 再跑一次：全部跳过，不再询价也不再下载
    db2 = FakeDB(per=1.0)
    rc = of.run(Settings(tmp_path), FakeSrc(), db2, ["SOXX"], date(2025, 9, 1), date(2025, 10, 5), "download", yes=True)
    assert rc == 0 and db2.get_calls == 0 and db2.cost_calls == 0


def test_half_day_window_used(tmp_path):
    reqs = of.build_requests("SOXX", {"2025-09-11": ["SOXX251024C00270000"]}, {"2025-09-11": "13:00"})
    assert reqs[0]["start"].startswith("2025-09-11T12:30") and reqs[0]["end"].startswith("2025-09-11T13:00")


def test_sampling_scales_and_margin(tmp_path):
    reqs = []
    for i in range(1, 21):          # 同一个月 20 个交易日，每天 1 块，价格随日期涨
        reqs.append({"und": "SPY", "month": "2025-09", "day": f"2025-09-{i:02d}", "chunk": 0,
                     "symbols": [f"d{i}"], "start": "s", "end": "e"})

    class DB(FakeDB):
        def get_cost(self, **kw):
            self.cost_calls += 1
            return 1.0

    db = DB()
    est = of.estimate(db, reqs)
    assert db.cost_calls == 3 and abs(est[("SPY", "2025-09")] - 20.0) < 1e-9
    assert of.sample_days([f"{i:02d}" for i in range(1, 21)]) == ["01", "11", "20"]
    db2 = DB()
    of.estimate(db2, reqs, exact=True)
    assert db2.cost_calls == 20
    tbl, tot = of.plan_table(est, of.SAFETY)
    assert abs(tot - 24.0) < 1e-9


def test_429_backoff_and_concurrency(monkeypatch):
    class DB(FakeDB):
        def __init__(self):
            super().__init__()
            self.n = 0

        def get_cost(self, **kw):
            self.n += 1
            if self.n <= 2:
                raise RuntimeError("HTTP 429 rate limit")
            return 2.0

    sleeps = []
    r = [{"und": "A", "month": "m", "day": "d", "symbols": ["x"], "start": "s", "end": "e"}]
    of.price_plan(DB(), r, workers=99, sleep=sleeps.append)
    assert r[0]["cost"] == 2.0 and sleeps == [2, 4]
    assert of.MAX_WORKERS <= 4


def test_alpaca_source_retries_429_and_paces():
    from desk import opra_fetch as of
    calls, sleeps = [], []
    class C:
        base = "x"
        def _req(self, m, url, params=None):
            calls.append(url)
            if len(calls) < 3:
                raise RuntimeError("Alpaca GET /v2/options/contracts → 429: rate limit exceeded")
            return {"ok": 1}
    src = of.AlpacaSource(C(), sleep=sleeps.append, min_gap_s=0.0)
    assert src._get("u", {}) == {"ok": 1} and len(calls) == 3 and sleeps.count(60) == 2


def test_alpaca_source_other_errors_raise():
    from desk import opra_fetch as of
    import pytest
    class C:
        base = "x"
        def _req(self, m, url, params=None):
            raise RuntimeError("Alpaca → 403: forbidden")
    with pytest.raises(RuntimeError):
        of.AlpacaSource(C(), sleep=lambda x: None, min_gap_s=0.0)._get("u", {})


def test_fetch_month_skips_unresolvable_request():
    from desk import opra_fetch as of
    class TS:
        def get_range(self, **kw):
            raise RuntimeError("422 symbology_invalid_request\nNone of the symbols could be resolved")
    class DB:
        timeseries = TS()
    df = of.fetch_month(DB(), [{"symbols": ["SPY   240301P00469000"], "start": "a", "end": "b"}])
    assert len(df) == 0


def test_alpaca_source_retries_network_errors():
    from desk import opra_fetch as of
    class ConnectError(Exception):
        pass
    n, sleeps = [], []
    class C:
        base = "x"
        def _req(self, m, url, params=None):
            n.append(1)
            if len(n) < 3:
                raise ConnectError("Temporary failure in name resolution")
            return {"ok": 1}
    assert of.AlpacaSource(C(), sleep=sleeps.append, min_gap_s=0.0)._get("u", {}) == {"ok": 1} and sleeps.count(15) == 2


def test_extra_mode_plan_cap_download_resume(tmp_path):
    import json
    root = tmp_path / "opra"
    (root / "meta").mkdir(parents=True)
    st = Settings(tmp_path)
    assert of.run_extra(st, FakeSrc(), FakeDB(), ["SOXX"], "plan") == 1          # 没有 extra_needed.json
    need = {"SOXX": {"2025-09-10": ["SOXX251024P00270000"], "2025-09-11": ["SOXX251024P00270000", "SOXX251024C00295000"],
                     "2025-10-01": ["SOXX251024P00270000"]}}
    (root / "meta" / "extra_needed.json").write_text(json.dumps(need))
    db = FakeDB(per=1.0)
    assert of.run_extra(st, FakeSrc(), db, ["SOXX"], "plan") == 0
    assert db.cost_calls == 3 and db.get_calls == 0                              # 逐请求精确询价：3 个(标的,日)请求
    assert of.run_extra(st, FakeSrc(), FakeDB(per=1.0), ["SOXX"], "download") == 2   # 没 --yes
    assert of.run_extra(st, FakeSrc(), FakeDB(per=60.0), ["SOXX"], "download", yes=True) == 2   # 超上限 100
    db = FakeDB(per=1.0)
    assert of.run_extra(st, FakeSrc(), db, ["SOXX"], "download", yes=True) == 0
    assert (root / "cbbo1m_extra" / "SOXX" / "2025-09.parquet").exists() and (root / "cbbo1m_extra" / "SOXX" / "2025-10.parquet").exists()
    assert not (root / "cbbo1m").exists()
    n = db.get_calls
    assert of.run_extra(st, FakeSrc(), db, ["SOXX"], "download", yes=True) == 0   # 续跑：已有月份不再请求
    assert db.get_calls == n


def test_extra_mode_needed_file_and_sub_keep_v1_and_v2_apart(tmp_path):
    """卡 B2：v2 回测的清单 extra_needed_v2.json 下到 cbbo1m_extra_v2/，不读不写 v1 的 extra_needed.json / cbbo1m_extra/
    （同一个 sub 下已有的月文件会被跳过，所以 v2 必须用自己的目录）。"""
    import json
    root = tmp_path / "opra"
    (root / "meta").mkdir(parents=True)
    st = Settings(tmp_path)
    v1_need = {"SOXX": {"2025-09-10": ["SOXX251024P00270000"]}}
    v2_need = {"SOXX": {"2025-09-11": ["SOXX251024P00270000", "SOXX251024C00295000"]}}
    (root / "meta" / "extra_needed.json").write_text(json.dumps(v1_need))
    # v2 清单还不存在 -> 退出码 1，并点名文件
    msgs = []
    assert of.run_extra(st, FakeSrc(), FakeDB(), ["SOXX"], "plan", needed_file="extra_needed_v2.json", out=msgs.append) == 1
    assert any("extra_needed_v2.json" in m for m in msgs)
    (root / "meta" / "extra_needed_v2.json").write_text(json.dumps(v2_need))
    # v1 已经把 cbbo1m_extra/SOXX/2025-09 下过了：v2 用自己的目录，照样会下
    (root / "cbbo1m_extra" / "SOXX").mkdir(parents=True)
    (root / "cbbo1m_extra" / "SOXX" / "2025-09.parquet").write_bytes(b"v1")
    db = FakeDB(per=1.0)
    assert of.run_extra(st, FakeSrc(), db, ["SOXX"], "plan", needed_file="extra_needed_v2.json", sub="cbbo1m_extra_v2") == 0
    assert db.cost_calls == 1                                                    # 只有 v2 清单里的 1 个(标的,日)请求
    assert of.run_extra(st, FakeSrc(), db, ["SOXX"], "download", yes=True, needed_file="extra_needed_v2.json", sub="cbbo1m_extra_v2") == 0
    assert (root / "cbbo1m_extra_v2" / "SOXX" / "2025-09.parquet").exists()
    assert (root / "cbbo1m_extra" / "SOXX" / "2025-09.parquet").read_bytes() == b"v1"      # v1 的文件没动
    # 默认参数仍是 v1 的行为：读 extra_needed.json，v1 的月文件已存在 -> 没有待下载请求
    db2 = FakeDB(per=1.0)
    assert of.run_extra(st, FakeSrc(), db2, ["SOXX"], "download", yes=True) == 0 and db2.get_calls == 0


def test_estimate_extra_is_programmatic_and_matches_run_extra(tmp_path):
    """卡 L2：研究室不解析 --plan 的文字，而是读 estimate_extra 的数字；它和 run_extra 用同一套请求、同样的逐请求询价，且不下载。"""
    import json
    root = tmp_path / "opra"
    (root / "meta").mkdir(parents=True)
    st = Settings(tmp_path)
    assert of.estimate_extra(st, FakeSrc(), FakeDB(), needed_file="extra_needed_lab.json", sub="cbbo1m_extra_lab") == \
        {"has_file": False, "n_requests": 0, "total_usd": 0.0, "underlyings": []}
    need = {"SOXX": {"2025-09-10": ["SOXX251024P00270000"], "2025-09-11": ["SOXX251024P00270000", "SOXX251024C00295000"],
                     "2025-10-01": ["SOXX251024P00270000"]}, "SPY": {}}
    (root / "meta" / "extra_needed_lab.json").write_text(json.dumps(need))
    db = FakeDB(per=1.5)
    est = of.estimate_extra(st, FakeSrc(), db, needed_file="extra_needed_lab.json", sub="cbbo1m_extra_lab")
    assert est == {"has_file": True, "n_requests": 3, "total_usd": 4.5, "underlyings": ["SOXX"]}     # SPY 清单为空：不算
    assert db.cost_calls == 3 and db.get_calls == 0 and not (root / "cbbo1m_extra_lab").exists()      # 只询价，没下载
    # 已有的月文件不再算（和 run_extra 的续跑一致）
    (root / "cbbo1m_extra_lab" / "SOXX").mkdir(parents=True)
    (root / "cbbo1m_extra_lab" / "SOXX" / "2025-09.parquet").write_bytes(b"x")
    est2 = of.estimate_extra(st, FakeSrc(), FakeDB(per=1.5), ["SOXX"], needed_file="extra_needed_lab.json", sub="cbbo1m_extra_lab")
    assert est2["n_requests"] == 1 and est2["total_usd"] == 1.5
    # 全都下过了 -> 0 个请求、0 美元、一次询价都不发
    (root / "cbbo1m_extra_lab" / "SOXX" / "2025-10.parquet").write_bytes(b"x")
    db3 = FakeDB()
    assert of.estimate_extra(st, FakeSrc(), db3, needed_file="extra_needed_lab.json", sub="cbbo1m_extra_lab")["n_requests"] == 0 and db3.cost_calls == 0


def test_index_cache_roundtrip_gives_same_requests(tmp_path):
    root = tmp_path / "opra"
    (root / "meta").mkdir(parents=True)
    src = FakeSrc()
    s0, e0 = date(2025, 9, 1), date(2025, 10, 5)
    c1, i1 = of._load_index(src, root, "SOXX", s0, e0, print)
    c2, i2 = of._load_index(src, root, "SOXX", s0, e0, print)      # 第二次走缓存
    assert c1 == c2 and i1.rows == i2.rows
    days = sorted(c1)
    assert of._reqs_for_days("SOXX", days, c1, i1, {}) == of._reqs_for_days("SOXX", days, c2, i2, {})


def test_month_days_clips_to_end():
    m = of._month_days({"2026-10-20": 1.0, "2026-10-21": 1.0, "2026-09-30": 1.0}, date(2026, 9, 1), date(2026, 10, 20))
    assert m == {"2026-09": ["2026-09-30"], "2026-10": ["2026-10-20"]}
