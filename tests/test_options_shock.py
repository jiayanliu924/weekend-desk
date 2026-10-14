"""突发冲击样本（11c）：假抽取器输出 + 合成期权链 → 分类、建样本、去重、进行中→C / 已结束→按历史、成绩单分状态、排期、网页表。不碰网络。"""
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from desk import config, extract, options, options_paper as op, options_shock as osh, relevance, scheduler
from desk.clock import to_ns
from desk.ledger import Ledger
from desk.rawstore import RawWriter
from tests.test_options_paper import _px, chain

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
T0 = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)          # 周四：工作日也跑
H = int(3600e9)
M = int(60e9)

NEWS = [
    # key, offset_ns, title, summary
    ("g1", 0, "Israel strikes Iran nuclear sites, Tehran vows retaliation",
     "Israel struck Iranian nuclear sites overnight and Tehran vowed retaliation, officials said."),
    ("g2", 20 * M, "Israel-Iran: strikes on nuclear sites, retaliation vowed - live",
     "Israel struck Iranian nuclear sites overnight and Tehran vowed retaliation, officials said."),     # 同一件事的重复
    ("r1", 2 * H, "SEC approves in-kind redemptions for spot bitcoin ETFs",
     "The SEC on Thursday approved in-kind creations and redemptions for spot bitcoin ETFs, the agency said."),
    ("n1", 3 * H, "Nvidia unveils new chip", "Nvidia unveiled a new chip at its conference."),           # 不是突发冲击
    ("h1", 4 * H, "Bybit says $1.4bn stolen in exchange hack",
     "Bybit said about $1.4 billion in ether was stolen from a cold wallet in a hack on Friday."),
    ("m1", 5 * H, "Fed holds rates steady as expected", "The Fed held rates steady as expected on Wednesday."),   # 普通 Fed 新闻，不是意外
]


def fake_llm(system, user):
    def ev(eid, ents, et, d, m, st, span):
        return {"event_id": eid, "entities": ents, "event_type": et, "direction_in_text": d, "materiality": m,
                "event_status": st, "evidence_span": span}
    if "Israel strikes Iran" in user:
        return json.dumps([ev("g1-1", ["ISRAEL", "IRAN"], "地缘", "负面", 3, "ongoing",
                              "Israel struck Iranian nuclear sites overnight and Tehran vowed retaliation, officials said.")])
    if "live" in user:
        return json.dumps([ev("g2-1", ["ISRAEL", "IRAN"], "地缘", "负面", 3, "ongoing",
                              "Israel struck Iranian nuclear sites overnight and Tehran vowed retaliation, officials said.")])
    if "SEC approves" in user:
        return json.dumps([ev("r1-1", ["SEC", "BITCOIN"], "监管", "正面", 2, "concluded",
                              "The SEC on Thursday approved in-kind creations and redemptions for spot bitcoin ETFs, the agency said.")])
    if "Nvidia unveils" in user:
        return json.dumps([ev("n1-1", ["NVDA"], "产品", "正面", 2, "concluded", "Nvidia unveiled a new chip at its conference.")])
    if "Bybit" in user:
        return json.dumps([ev("h1-1", ["BYBIT", "ETH"], "其他", "负面", 3, None,
                              "Bybit said about $1.4 billion in ether was stolen from a cold wallet in a hack on Friday.")])
    if "Fed holds" in user:
        return json.dumps([ev("m1-1", ["FED"], "宏观", "中性", 2, "concluded", "The Fed held rates steady as expected on Wednesday.")])
    return "[]"


def seed_chain(s, at: datetime, S=80000.0, iv=50.0, S_end=80200.0):
    """at 之前 2 分钟一个期权快照 + 之后 24 小时的指数价（几乎不动 → 实际远小于隐含）。"""
    wo, wi = RawWriter(s.data, "drb_opts"), RawWriter(s.data, "drb_index")
    wo.add("deribit", "BTC", {"n_total": 500, "items": chain(at, S, iv)}, to_ns(at) - int(120e9))
    px, _, _ = _px(at, S, S_end)
    for t, p in px:
        wi.add("deribit", "btc", {"index_price": p, "estimated_delivery_price": p}, t)
    wo.flush(), wi.flush()


@pytest.fixture
def s(tmp_path):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    st = config.load(tmp_path)
    wn = RawWriter(st.data, "news")
    for key, off, title, summary in NEWS:
        wn.add("feed", key, {"feed": "test", "title": title, "summary": summary, "published_at": None}, to_ns(T0) + off)
    wn.flush()
    extract.run_extraction(st, to_ns(T0) - H, to_ns(T0) + 6 * H, extractor=fake_llm, clock=lambda: to_ns(T0) + 6 * H)
    return st


# ---------------------------------------------------------------- 分类
def test_categorize_and_btc_relevance():
    assert relevance.is_relevant("BTC", ["SEC"]) and relevance.is_relevant("BTC", ["spot bitcoin ETF"])
    assert relevance.is_relevant("BTC", ["IRAN"]) and not relevance.is_relevant("BTC", ["NVDA"])
    assert osh.categorize({"entities": ["ISRAEL"], "title": "Israel strikes Iran", "evidence_span": ""}) == "GEO"
    assert osh.categorize({"entities": ["SEC", "BITCOIN"], "title": "SEC approves bitcoin ETF", "evidence_span": ""}) == "REG"
    assert osh.categorize({"entities": ["BYBIT"], "title": "Bybit hack", "evidence_span": "stolen"}) == "HACK"
    assert osh.categorize({"entities": ["CHINA"], "title": "US raises tariffs on China", "evidence_span": ""}) == "TARIFF"
    assert osh.categorize({"entities": ["FED"], "title": "Fed emergency rate cut", "evidence_span": ""}) == "MACRO"
    # 普通 Fed 新闻（实体命中、没有意外词）和纯产品新闻都不算
    assert osh.categorize({"entities": ["FED"], "title": "Fed holds rates steady", "evidence_span": ""}) is None
    assert osh.categorize({"entities": ["NVDA"], "title": "Nvidia unveils chip", "evidence_span": ""}) is None
    # SEC 只有实体没有监管词也不算（need_both）
    assert osh.categorize({"entities": ["SEC"], "title": "SEC chair speaks at conference", "evidence_span": ""}) is None


def test_candidates(s):
    evs = osh.candidate_events(s, to_ns(T0) - H, to_ns(T0) + 6 * H)
    assert [(e["entry_id"], e["category"]) for e in evs] == [("g1#0", "GEO"), ("g2#0", "GEO"), ("r1#0", "REG"), ("h1#0", "HACK")]
    assert evs[0]["event_status"] == "ongoing" and evs[2]["event_status"] == "concluded"
    assert extract.validate([{"event_status": None}], "", [])[0]["event_status"] == "unknown"


# ---------------------------------------------------------------- 建样本 / 去重 / 规则
def test_run_creates_samples_dedupes_and_gates(s):
    t_scan1 = T0 + timedelta(minutes=5)
    seed_chain(s, t_scan1)
    r = osh.run(s, clock=lambda: to_ns(t_scan1), extractor=fake_llm)
    assert r == {"samples": 1, "repeats": 0}
    led = Ledger(s.data, "options")
    locks = [x for x in led.all() if x["kind"] == "lock"]
    assert len(locks) == 1
    lk = locks[0]
    assert lk["sample_kind"] == "shock" and lk["event_type"] == "SHOCK:GEO" and lk["event_status"] == "ongoing"
    assert lk["weekend"] == f"SHOCK-GEO-{t_scan1:%Y%m%d-%H%M}"
    assert lk["window_start"] == t_scan1.isoformat() and lk["event_at"] == T0.isoformat()
    assert lk["shock"]["entry_id"] == "g1#0" and lk["shock"]["category_name"] == "地缘冲突" and lk["shock"]["delay_min"] == pytest.approx(5)
    assert lk["ok"] and lk["implied_vol"] == pytest.approx(0.5)
    # 进行中 → 纸面一律 C，哪怕有历史
    assert lk["paper"]["ok"] and lk["paper"]["chosen"] == "C" and "进行中" in lk["paper"]["reason"]
    assert lk["paper"]["event_status"] == "ongoing"
    assert options.group_of(lk) == "shock"                       # 不混进对照组
    # 幂等：同一时刻再跑，不多记
    n0 = len(led.all())
    osh.run(s, clock=lambda: to_ns(t_scan1), extractor=fake_llm)
    assert len(led.all()) == n0
    # 6 小时后：g2 是 g1 的重复（相似原句 + 同类别 24h 内）；r1 已结束 → 建 REG 样本；h1 被盗 → HACK 样本（状态缺省 unknown → C）
    t_scan2 = T0 + timedelta(hours=6)
    seed_chain(s, t_scan2)
    r = osh.run(s, clock=lambda: to_ns(t_scan2), extractor=fake_llm)
    assert r == {"samples": 2, "repeats": 1}
    seen = {x["entry_id"]: x for x in Ledger(s.data, osh.LEDGER).all()}
    assert set(seen) == {"g1#0", "g2#0", "r1#0", "h1#0"}
    assert seen["g2#0"]["repeat_of"] == seen["g1#0"]["sid"] and seen["g2#0"]["sid"] is None
    locks = {x["weekend"]: x for x in led.all() if x["kind"] == "lock"}
    reg = next(v for k, v in locks.items() if k.startswith("SHOCK-REG"))
    hack = next(v for k, v in locks.items() if k.startswith("SHOCK-HACK"))
    assert reg["event_status"] == "concluded" and hack["event_status"] == "unknown"
    assert reg["paper"]["chosen"] == "C" and "同组历史只有 0 个" in reg["paper"]["reason"]   # 已结束但没历史 → C（正常规则）
    assert hack["paper"]["chosen"] == "C" and "看不出" in hack["paper"]["reason"]
    # 排期：24 小时 + 5 分钟后结算 GEO 样本
    now = t_scan1 + timedelta(hours=24, minutes=6)
    dues = osh.due(now, s, set())
    assert [k for k, _ in dues] == [f"opt:{lk['weekend']}:outcome"]
    smp = dues[0][1]
    assert smp.kind == "shock" and smp.lock == t_scan1 and smp.end == t_scan1 + timedelta(hours=24)
    out = options.job_outcome(s, smp)
    assert out["ok"] and out["ratio"] < 1 and out["paper"]["ok"] and out["paper"]["chosen_pnl"] == 0
    assert out["paper"]["pnl"]["A_unhedged"] > 0                 # 事后看卖跨式会赚，但规则不让做
    assert not osh.due(now, s, {f"opt:{lk['weekend']}:outcome"})


def test_concluded_follows_history_rule(s):
    """已结束的突发事件：同类别历史 ≥ min_history 且均值 实际/隐含 < 0.9 → A 卖跨式。"""
    s.raw["options"]["min_history"] = 2
    led = Ledger(s.data, "options")
    # 先手工塞两个 SHOCK:REG 的已完成样本（实际远小于隐含）
    for i in range(2):
        led.append("lock", f"SHOCK-REG-2026100{i + 1}-0900", {"sample_kind": "shock", "event_type": "SHOCK:REG", "ok": True,
                                                                "event_status": "concluded", "implied_vol": 0.5})
        led.append("outcome", f"SHOCK-REG-2026100{i + 1}-0900", {"ok": True, "ratio": 0.6, "log_ratio": -0.51,
                                                                   "realized_vol": 0.3})
    t = T0 + timedelta(hours=6)
    seed_chain(s, t)
    osh.run(s, clock=lambda: to_ns(t), extractor=fake_llm)
    locks = {x["weekend"]: x for x in led.all() if x["kind"] == "lock"}
    reg = next(v for k, v in locks.items() if k.startswith("SHOCK-REG-20261015"))
    geo = next(v for k, v in locks.items() if k.startswith("SHOCK-GEO"))
    assert reg["paper"]["chosen"] == "A" and reg["paper"]["n_history"] == 2
    assert geo["paper"]["chosen"] == "C"                         # 进行中：有没有历史都不动
    # 结算后：选边盈亏 = A
    smp = osh.sample_from_lock(reg)
    out = options.job_outcome(s, smp)
    assert out["paper"]["chosen"] == "A" and out["paper"]["chosen_pnl"] == out["paper"]["pnl"]["A_unhedged"] > 0
    assert out["paper"]["event_status"] == "concluded"
    txt = options.lock_text(reg)
    assert "突发 SHOCK:REG（已结束）" in txt and "选了 A 卖跨式" in txt


def test_scorecard_splits_by_status(s):
    s.raw["options"]["min_history"] = 99
    t1 = T0 + timedelta(minutes=5)
    seed_chain(s, t1)
    osh.run(s, clock=lambda: to_ns(t1), extractor=fake_llm)
    t2 = T0 + timedelta(hours=6)
    seed_chain(s, t2)
    osh.run(s, clock=lambda: to_ns(t2), extractor=fake_llm)
    for smp in osh.shock_samples(s):
        options.job_outcome(s, smp)
    oc = options.scorecard(s)
    assert oc["n_event"] == 0 and oc["n_control"] == 0            # 突发样本既不是事件也不是对照
    sh = oc["shock"]
    assert sh["n"] == 3
    assert {k: v["n"] for k, v in sh["by_status"].items()} == {"concluded": 1, "ongoing": 1, "unknown": 1}
    assert all(v["ratio"] < 1 for v in sh["by_status"].values())
    assert set(sh["by_category"]) == {"SHOCK:GEO", "SHOCK:REG", "SHOCK:HACK"}
    assert "突发冲击 3 个" in options.card_text(oc) and "进行中 1 个" in options.card_text(oc)
    ps = op.summary(s)
    assert ps["n"] == 3 and set(ps["shock_by_status"]) == {"concluded", "ongoing", "unknown"}
    assert ps["shock_by_status"]["ongoing"]["chosen"] == 0 and ps["shock_by_status"]["ongoing"]["A_unhedged"] > 0
    assert set(ps["by_type"]) == {"SHOCK:GEO", "SHOCK:REG", "SHOCK:HACK"}
    rows = osh.rows(s)
    assert len(rows) == 3 and all(r["settled"] for r in rows)
    assert rows[-1]["event_status"] == "ongoing" and rows[-1]["chosen"] == "C"


def test_old_news_not_sampled_and_disabled(s):
    t = T0 + timedelta(hours=12)            # 所有新闻都在 8–12 小时前：超过 max_news_age_h=6，不开样本，但都记 seen
    seed_chain(s, t)
    r = osh.run(s, clock=lambda: to_ns(t), extractor=fake_llm)
    seen = {x["entry_id"]: x for x in Ledger(s.data, osh.LEDGER).all()}
    assert set(seen) == {"g1#0", "g2#0", "r1#0", "h1#0"}
    assert all("problem" in x and x["sid"] is None for x in seen.values())
    assert r == {"samples": 0, "repeats": 0}
    assert not [x for x in Ledger(s.data, "options").all() if x["kind"] == "lock"]
    s.raw["options"]["shock"]["enabled"] = False
    assert osh.run(s, clock=lambda: to_ns(t), extractor=fake_llm) == {"samples": 0, "repeats": 0}


def test_earnings_samples_and_events_in(s):
    evs = options.event_samples(s)
    ern = [e for e in evs if e.is_earnings]
    assert [e.event_type for e in ern] == ["EARNINGS:SMSN", "EARNINGS:TSLA", "EARNINGS:SMSN", "EARNINGS:INTC", "EARNINGS:NVDA"]
    tsla = next(e for e in ern if e.sid == "EVT-ERN-TSLA-2026Q3")
    assert tsla.event_at == datetime(2026, 10, 21, 20, 5, tzinfo=UTC) and tsla.lock == tsla.event_at - timedelta(minutes=60)
    assert tsla.coin == "xyz:TSLA" and tsla.confirmed
    assert not next(e for e in ern if e.sid == "EVT-ERN-NVDA-FY27Q3").confirmed
    assert evs == sorted(evs, key=lambda e: e.event_at)
    # 财报不改变每日对照组的口径：10/21 的每日样本窗口里只有 TSLA 财报 → 仍算对照
    assert options.events_in(s, tsla.lock - timedelta(hours=1), tsla.end) == []
    # 纸面分组键 = EARNINGS:<ticker>
    assert op.paper_group({"sample_kind": "event", "event_type": "EARNINGS:TSLA"}) == "EARNINGS:TSLA"
    # due() 也会排财报的锁定
    done = set()
    keys = [k for k, _ in options.due(tsla.lock + timedelta(minutes=1), s, done)]
    assert f"opt:{tsla.sid}:lock" in keys


def test_earnings_lock_records_coin_and_confirmed(s):
    smp = next(e for e in options.event_samples(s) if e.sid == "EVT-ERN-NVDA-FY27Q3")
    seed_chain(s, smp.lock)
    lock = options.job_lock(s, smp, extractor=lambda a, b: "[]", clock=lambda: to_ns(smp.lock) - 1)
    assert lock["ok"] and lock["event_type"] == "EARNINGS:NVDA" and lock["coin"] == "xyz:NVDA" and lock["date_confirmed"] is False
    assert lock["paper"]["group"] == "EARNINGS:NVDA"
    out = options.job_outcome(s, smp)
    assert out["ok"]
    oc = options.scorecard(s)
    assert oc["n_event"] == 1 and oc["n_earnings"] == 1 and "财报 1 个" in options.card_text(oc)
    assert options.surprise_pattern("EARNINGS:NVDA").search("Nvidia beat estimates")


def test_scheduler_schedules_shock_scan(s):
    now = datetime(2026, 10, 15, 14, 7, tzinfo=UTC)
    done = set()
    keys = [k for k, _ in scheduler.due_jobs(now, s, done)]
    sk = [k for k in keys if k.startswith("optshock:")]
    assert len(sk) == 1
    done.add(sk[0])
    assert not [k for k, _ in scheduler.due_jobs(now, s, done) if k.startswith("optshock:")]
    done.add("optshock:1")
    scheduler.due_jobs(now, s, done)
    assert "optshock:1" not in done
    # 跑一次（真实时钟，fixture 里的新闻不在它的回看窗口内）：只要能跑通、返回计数
    r = scheduler.run_job(sk[0], None, s)
    assert set(r) == {"samples", "repeats"}
    # 没有期权快照时：样本锁定失败但仍记录（lock_ok False），不会抛异常
    t = T0 + timedelta(minutes=5)
    r = osh.run(s, clock=lambda: to_ns(t), extractor=fake_llm)
    assert r["samples"] == 1
    seen = Ledger(s.data, osh.LEDGER).all()
    assert seen[0]["lock_ok"] is False
    lk = [x for x in Ledger(s.data, "options").all() if x["kind"] == "lock"][0]
    assert not lk["ok"] and lk["event_status"] == "ongoing" and lk["shock"]["category"] == "GEO"


def test_web_shock_table(s, monkeypatch):
    import importlib
    monkeypatch.setenv("DESK_ROOT", str(s.root))
    monkeypatch.setenv("DESK_DATA", str(s.data))
    monkeypatch.setenv("WEB_INSECURE", "1")
    monkeypatch.setenv("WEB_SECRET", "x" * 32)
    t = T0 + timedelta(hours=6)
    seed_chain(s, t)
    osh.run(s, clock=lambda: to_ns(t), extractor=fake_llm)
    for smp in osh.shock_samples(s):
        options.job_outcome(s, smp)
    from fastapi.testclient import TestClient
    import desk.web as web
    web = importlib.reload(web)
    web.add_user(s, "kea", "correct horse battery")
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    html = c.get("/options").text
    assert "突发事件样本" in html and "监管/ETF/SEC" in html and "交易所被盗/暴雷" in html and "SEC approves in-kind" in html
