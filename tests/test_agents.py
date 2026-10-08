"""Agent 团队：名单规模、降级模式、完整开会（假模型）、代码审计、费用封顶、网页。"""
import json
import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from desk import agents, pdfreport
from tests.test_web import env  # noqa: F401  (fixture)


def test_roster_shape():
    rooms = {}
    for a in agents.AGENTS:
        rooms.setdefault(a["room"], []).append(a)
    assert len(agents.AGENTS) >= 20
    for r in agents.ROOMS:
        assert len(rooms[r["key"]]) >= 3, r["key"]          # 每个室至少 3 个（要求 ≥2，重要的 3）
    assert len({a["id"] for a in agents.AGENTS}) == len(agents.AGENTS)


def fake_llm_factory(calls):
    def llm(model, system, user):
        calls.append((model, system[:40]))
        ids = re.findall(r"^(F\d+) ", user, flags=re.M)
        c = ids[1] if len(ids) > 1 else "F1"
        if "主席" in system and "白话编辑" not in system and "记录员" not in system and "成员" not in system:
            out = {"headline": "数据正常，继续模拟", "plain": "七个合约都在收数据，样本还太少。", "today": ["周日看锁定"], "confidence": 60}
        elif "白话编辑" in system:
            out = {"push": "数据正常\n样本还少\n周日锁定", "plain": "x"}
        elif "记录员" in system:
            out = {"plain": "本室认为样本不够，继续观察。", "consensus": "继续", "dissent": [{"who": "SKEPTIC", "view": "回测太乐观"}],
                   "confidence": 55, "actions": ["多收集样本"]}
            if "交易室" in system:
                out["proposal"] = [{"name": "NVDA", "lean": "fade", "size": 0.5, "why": "无新闻"}]
            if "风控室" in system:
                out["veto"] = [{"name": "TSLA", "why": "跳空风险"}]
        else:
            out = {"stance": "谨慎", "plain": "样本太少，先别下结论。", "points": [{"claim": "资料显示样本少", "cite": [c]},
                                                                           {"claim": "编一个数 98765", "cite": ["F999"]}],
                   "confidence": 50}
        return json.dumps(out, ensure_ascii=False), 1000, 200
    return llm


def test_degraded_without_key(env, monkeypatch):  # noqa: F811
    s, web = env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rec = agents.run_meeting(s, "test", push=False)
    assert rec["mode"] == "degraded" and "API key" in rec["degraded_reason"]
    assert len(rec["facts"]) > 10 and rec["checks"]["clock"]["ok"]
    assert any("XYZ100" in f["text"] for f in rec["facts"])


def test_full_meeting_and_audit(env):  # noqa: F811
    s, web = env
    calls = []
    rec = agents.run_meeting(s, "test", llm=fake_llm_factory(calls), push=False)
    assert rec["mode"] == "full", rec.get("degraded_reason")
    members = [a for a in agents.AGENTS if a["room"] not in ("chair", "editor")]
    # 20 members × 2 rounds + 6 room syntheses + chair + editor
    assert rec["calls"] == len(members) * 2 + 6 + 2 == len(calls)
    assert set(rec["rooms"]) == {r["key"] for r in agents.ROOMS}
    ck = rec["checks"]["cite"]
    assert ck["bad_ids"] and not ck["ok"]                       # F999 doesn't exist
    assert any(x.endswith(":98765") for x in ck["unsourced_numbers"])
    assert rec["rooms"]["trading"]["synth"]["proposal"][0]["name"] == "NVDA"
    assert rec["chair"]["headline"]
    assert 0 < rec["cost_usd"] < 0.5
    # spend recorded and visible
    assert agents.spend(s)["day"] > 0
    # web page shows it
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    r = c.get("/agents")
    assert r.status_code == 200
    for t in ("主席结论", "数据正常，继续模拟", "期权研究室", "风控室", "否决 TSLA", "资料包", "引用与数字", "反向押回撤"):
        assert t in r.text, t
    assert c.get(f"/agents?run={rec['run_id']}").status_code == 200
    assert c.get("/agents?run=../../etc").status_code == 200   # bad id → falls back to empty, no traversal
    # PDF includes the meeting
    pdf = pdfreport.build(s)
    assert pdf[:4] == b"%PDF"
    assert pdfreport.agents_section(s)


def test_budget_cap(env):  # noqa: F811
    s, _ = env
    s.raw["agents"] = {**s.raw.get("agents", {}), "budget_day_usd": 0.01}
    calls = []
    rec = agents.run_meeting(s, "test", llm=fake_llm_factory(calls), push=False)
    # first calls may go through, then the cap stops the meeting
    assert rec["mode"] == "partial" and "费用到顶" in rec["degraded_reason"]
    rec2 = agents.run_meeting(s, "test", llm=fake_llm_factory(calls), push=False)
    assert rec2["mode"] == "degraded"


def test_due_schedule(env):  # noqa: F811
    s, _ = env
    t = datetime(2026, 10, 8, 13, 31, tzinfo=timezone.utc)   # 06:31 PT
    assert agents.due(t, s, set()) == ["agents:2026-10-08"]
    assert agents.due(t, s, {"agents:2026-10-08"}) == []
    sun = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)  # Sun 16:00 ET, earliest decision 17:45 ET
    assert any(k.startswith("agents_wk:") for k in agents.due(sun, s, set()))


def test_parse_json():
    assert agents.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert agents.parse_json('好的：{"a": 2} 完') == {"a": 2}
    assert agents.parse_json("nonsense")["parse_error"]
