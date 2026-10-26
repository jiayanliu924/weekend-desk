"""头条动量影子线：假抽取器输出 + 合成链上数据 → 进场、对账、去重、成绩单、网页卡片；全程不下单。"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from desk import config, extract, headline_shadow, relevance
from desk.ledger import Ledger
from desk.rawstore import RawWriter

ROOT = Path(__file__).resolve().parent.parent
T0 = int(datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc).timestamp() * 1e9)   # 周二：工作日也要跑
H = int(3600e9)
M = int(60e9)
COIN = "xyz:BRENTOIL"


def ctx(px):
    return {"channel": "activeAssetCtx", "data": {"coin": COIN, "ctx": {
        "oraclePx": str(px), "markPx": str(px), "midPx": str(px), "funding": "0.0000125",
        "openInterest": "1000", "impactPxs": [str(px - 0.01), str(px + 0.01)], "dayNtlVlm": "1"}}}


def price_at(t_ns: int) -> float:
    """100 → 一小时后 103 → 四小时后 104 → 一天后 99（先延续、后反转）。"""
    h = (t_ns - T0) / H
    if h <= 0.04:
        return 100.0
    if h <= 1:
        return 100.0 + 3.0 * h
    if h <= 4:
        return 103.0 + (h - 1) / 3
    return max(99.0, 104.0 - (h - 4) / 20 * 5)


NEWS = [
    # key, offset, title, summary
    ("a1", 0, "Houthi drone strike hits Saudi Aramco facility, output halted",
     "A Houthi drone strike hit an Aramco facility on Tuesday and output was halted, officials said. Nvidia also reported results."),
    ("a2", 30 * M, "Houthi strike on Aramco facility halts output - update",
     "A Houthi drone strike hit an Aramco facility on Tuesday and output was halted, officials said."),   # 同一件事的重复
    ("b1", 2 * H, "Iran and Israel agree ceasefire, oil slides",
     "Iran and Israel agreed a ceasefire on Tuesday, ending twelve days of strikes."),
    ("c1", 3 * H, "Minor refinery fire in Texas", "A small fire at a Texas refinery was put out within an hour."),
    ("d1", 5 * H, "Tanker attacked near Hormuz", "A tanker was attacked near the Strait of Hormuz, the UKMTO said."),
]


def fake_llm(system, user):
    assert "event_status" in system          # 抽取指令里已加 event_status 字段
    if "Houthi drone strike hits" in user:
        return json.dumps([
            {"event_id": "a1-1", "entities": ["OIL", "SAUDI"], "event_type": "地缘", "direction_in_text": "负面",
             "materiality": 3, "is_new_information": True, "event_status": "ongoing",
             "evidence_span": "A Houthi drone strike hit an Aramco facility on Tuesday and output was halted, officials said."},
            {"event_id": "a1-2", "entities": ["NVDA"], "event_type": "财报指引", "direction_in_text": "正面",
             "materiality": 3, "is_new_information": True, "evidence_span": "Nvidia also reported results."}])
    if "update" in user:
        return json.dumps([{"event_id": "a2-1", "entities": ["OIL"], "event_type": "地缘", "direction_in_text": "负面",
                            "materiality": 3, "event_status": "ongoing",
                            "evidence_span": "A Houthi drone strike hit an Aramco facility on Tuesday and output was halted, officials said."}])
    if "ceasefire" in user:
        return json.dumps([{"event_id": "b1-1", "entities": ["IRAN", "ISRAEL", "OIL"], "event_type": "地缘",
                            "direction_in_text": "正面", "materiality": 3, "event_status": "concluded",
                            "evidence_span": "Iran and Israel agreed a ceasefire on Tuesday, ending twelve days of strikes."}])
    if "refinery fire" in user:
        return json.dumps([{"event_id": "c1-1", "entities": ["OIL"], "event_type": "其他", "direction_in_text": "负面",
                            "materiality": 1, "evidence_span": "A small fire at a Texas refinery was put out within an hour."}])
    if "Tanker attacked" in user:
        return json.dumps([{"event_id": "d1-1", "entities": ["HORMUZ"], "event_type": "地缘", "direction_in_text": "不明",
                            "materiality": 2, "event_status": "bogus-value",
                            "evidence_span": "A tanker was attacked near the Strait of Hormuz, the UKMTO said."}])
    return "[]"


@pytest.fixture
def s(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    st = config.load(tmp_path)
    wc, wn, wp = (RawWriter(st.data, n) for n in ("hl_ctx", "news", "hl_params"))
    for i in range(-60, 30 * 60):           # 每分钟一个链上价，覆盖新闻前 1 小时到后 30 小时
        t = T0 + i * M
        wc.add("ws", COIN, ctx(price_at(t)), t)
    wp.add("rest", COIN, {"asset_meta": {"name": COIN, "growthMode": "enabled"}}, T0 - H)
    for key, off, title, summary in NEWS:
        wn.add("feed", key, {"feed": "test", "title": title, "summary": summary, "published_at": None}, T0 + off)
    for x in (wc, wn, wp):
        x.flush()
    extract.run_extraction(st, T0 - H, T0 + 6 * H, extractor=fake_llm, clock=lambda: T0 + 6 * H)
    return st


def test_relevance_table():
    assert relevance.is_relevant("BRENTOIL", ["OIL"]) and relevance.is_relevant("BRENTOIL", ["Saudi Arabia"])
    assert relevance.is_relevant("BRENTOIL", ["HORMUZ"]) and not relevance.is_relevant("BRENTOIL", ["NVDA", "AAPL"])
    assert relevance.relevant_instruments(["NVIDIA"]) == ["NVDA"]
    # 油价和坏消息是反的：袭击（负面）→ 多；停火（正面）→ 空；股票照常
    assert relevance.implied_side("BRENTOIL", "负面") == 1 and relevance.implied_side("BRENTOIL", "正面") == -1
    assert relevance.implied_side("NVDA", "负面") == -1
    assert relevance.implied_side("BRENTOIL", "不明", "A tanker was attacked near Hormuz") == 1
    assert relevance.implied_side("BRENTOIL", "中性", "Talks resume, ceasefire holds") == -1
    assert relevance.implied_side("BRENTOIL", "不明", "Oil prices were little changed") == 0


def test_event_status_defaults_unknown(s):
    evs = extract.load_events(s, T0 + 7 * H, T0 - H)
    by_id = {e.get("event_id"): e for e in evs}
    assert by_id["a1-1"]["event_status"] == "ongoing" and by_id["b1-1"]["event_status"] == "concluded"
    assert by_id["a1-2"]["event_status"] == "unknown" and by_id["d1-1"]["event_status"] == "unknown"   # 缺省/非法值 → unknown
    assert s["llm"]["prompt_version"] == "v0.1"      # 不升 prompt 版本


def test_entries_marks_and_dedupe(s):
    # 新闻刚到 1 分钟：还不到 delay_sec=120 秒，不记
    r = headline_shadow.run(s, clock=lambda: T0 + 1 * M)
    assert r == {"entries": 0, "marks": 0}
    # 5 分钟后：a1 的油事件进场；同一条新闻里的 NVDA 事件不算；进场价 = 收到 +120 秒的中间价
    r = headline_shadow.run(s, clock=lambda: T0 + 5 * M)
    led = Ledger(s.data, headline_shadow.LEDGER)
    ents = [x for x in led.all() if x["kind"] == headline_shadow.KIND_ENTRY]
    assert r["entries"] == 1 and len(ents) == 1
    e = ents[0]
    assert e["entry_id"] == "a1#0" and e["side"] == 1 and e["instrument"] == "BRENTOIL" and e["mode"] == "shadow"
    assert e["entry_px"] == pytest.approx(price_at(T0 + 2 * M)) and e["entry_at"] == T0 + 2 * M
    assert e["is_new"] and e["repeat_of"] is None and e["event_status"] == "ongoing"
    assert e["weekend_news"] is False and e["notional_usd"] == s["risk"]["paper_notional_usd"]
    assert e["fees"]["growth_mode"] is True
    # 6 小时后：重复报道 a2 记为重复；停火 b1 做空、已结束；c1 重要度不够不记；d1 不明倾向按关键词 → 多
    r = headline_shadow.run(s, clock=lambda: T0 + 6 * H)
    ents = {x["entry_id"]: x for x in led.all() if x["kind"] == headline_shadow.KIND_ENTRY}
    assert set(ents) == {"a1#0", "a2#0", "b1#0", "d1#0"}
    assert not ents["a2#0"]["is_new"] and ents["a2#0"]["repeat_of"] == "a1#0"
    assert ents["b1#0"]["side"] == -1 and ents["b1#0"]["event_status"] == "concluded" and ents["b1#0"]["is_new"]
    assert ents["d1#0"]["side"] == 1 and ents["d1#0"]["is_new"]
    # 此时 a1 的 +1h、+4h 已到期，+24h 未到；b1 的 +1h 到期
    marks = [x for x in led.all() if x["kind"] == headline_shadow.KIND_MARK]
    got = {(m["entry_id"], m["horizon_h"]) for m in marks}
    assert ("a1#0", 1) in got and ("a1#0", 4) in got and ("a1#0", 24) not in got and ("b1#0", 1) in got
    m1 = next(m for m in marks if m["entry_id"] == "a1#0" and m["horizon_h"] == 1)
    assert m1["ok"] and m1["mark_px"] == pytest.approx(103.0, abs=0.06)
    assert m1["ret_bps"] == pytest.approx((m1["mark_px"] / ents["a1#0"]["entry_px"] - 1) * 1e4)
    assert m1["direction_hit"] is True and m1["pnl_net_fullfee_bps"] == pytest.approx(m1["pnl_gross_bps"] - 12.0)
    assert m1["pnl_net_fullfee_usd"] == pytest.approx(m1["pnl_net_fullfee_bps"] / 1e4 * 1000)
    # 幂等：再跑一次同一时刻，什么都不多记
    n0 = len(led.all())
    headline_shadow.run(s, clock=lambda: T0 + 6 * H)
    assert len(led.all()) == n0
    # 30 小时后：全部对账完成；a1 的 +24h 反转为亏
    headline_shadow.run(s, clock=lambda: T0 + 30 * H)
    marks = {(m["entry_id"], m["horizon_h"]): m for m in led.all() if m["kind"] == headline_shadow.KIND_MARK}
    assert all((eid, h) in marks for eid in ("a1#0", "a2#0", "b1#0", "d1#0") for h in (1, 4, 24))
    assert marks[("a1#0", 24)]["direction_hit"] is False and marks[("a1#0", 24)]["pnl_net_fullfee_bps"] < 0
    # 真钱账本里一笔都没有：影子线从不下单
    assert not (Path(s.data) / "ledger" / "live.jsonl").exists()
    assert not (Path(s.data) / "ledger" / "experiments.jsonl").exists()


def test_scorecard(s):
    headline_shadow.run(s, clock=lambda: T0 + 30 * H)
    sc = headline_shadow.headline_scorecard(s)
    assert sc["n_entries"] == 4 and sc["n_sided"] == 4 and sc["n_new"] == 3 and sc["n_repeat"] == 1
    assert sc["n_long"] == 2 and sc["n_short"] == 1 and sc["n_weekend"] == 0
    assert sc["horizons_h"] == [1, 4, 24]
    h1 = sc["by_horizon"][1]
    assert h1["n"] == 3                                   # 只算"新"的三笔
    # a1 多：+1h 涨 → 命中；b1 空（T0+2h 进场 → +1h 价格仍在涨）→ 不命中；d1 多（T0+5h 进场，之后在跌）→ 不命中
    assert h1["hit_rate"] == pytest.approx(1 / 3)
    assert sc["by_status"]["ongoing"]["n"] == 1 and sc["by_status"]["concluded"]["n"] == 1 and sc["by_status"]["unknown"]["n"] == 1
    assert sc["by_status"]["ongoing"]["by_horizon"][24]["hit_rate"] == 0.0     # a1 一天后反转
    assert sc["by_status"]["concluded"]["by_horizon"][24]["hit_rate"] == 1.0   # 停火做空一天后是对的
    assert "3 条新头条" in sc["verdict"] and "不动真钱" in sc["verdict"]
    empty = headline_shadow.headline_scorecard(s, records=[])
    assert empty["n_new"] == 0 and "还没有" in empty["verdict"]


def test_scheduler_schedules_headline_job(s):
    from desk import scheduler
    now = datetime(2026, 10, 6, 12, 7, tzinfo=timezone.utc)
    done = set()
    keys = [k for k, _ in scheduler.due_jobs(now, s, done)]
    hk = [k for k in keys if k.startswith("headline:")]
    assert len(hk) == 1
    done.add(hk[0])
    assert not [k for k, _ in scheduler.due_jobs(now, s, done) if k.startswith("headline:")]
    # 旧的 slot key 会被清掉，状态文件不会无限长
    done.add("headline:1")
    scheduler.due_jobs(now, s, done)
    assert "headline:1" not in done
    assert scheduler.run_job(hk[0], None, s) == {"entries": 0, "marks": 0}   # 新闻都在 T0 之后 → 这个时刻还没到 delay


def test_web_card(s, monkeypatch):
    import importlib
    (s.root / "events.toml").write_text((ROOT / "events.toml").read_text())
    monkeypatch.setenv("DESK_ROOT", str(s.root))
    monkeypatch.setenv("DESK_DATA", str(s.data))
    monkeypatch.setenv("WEB_INSECURE", "1")
    monkeypatch.setenv("WEB_SECRET", "x" * 32)
    headline_shadow.run(s, clock=lambda: T0 + 30 * H)
    from fastapi.testclient import TestClient
    import desk.web as web
    web = importlib.reload(web)
    web.add_user(s, "kea", "correct horse battery")
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    html = c.get("/admin").text
    assert "头条动量（影子 · 只记账）" in html and "3 条新头条" in html and "进行中（1）" in html
