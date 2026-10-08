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
        assert len(rooms[r["key"]]) >= 3, r["key"]
    assert len(agents.AGENTS) >= 25          # 每个室至少 3 个（要求 ≥2，重要的 3）
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
        elif '"specs"' in user:
            out = {"stance": "找规律", "plain": "偏离大的个股反向做。", "points": [{"claim": "回测", "cite": [c]}], "confidence": 40,
                   "specs": [{"kind": "weekend", "name": "大偏离反向", "instruments": "all", "direction": "fade",
                              "min_abs_dev_bps": 30, "max_abs_dev_bps": 1000, "size": "flat"},
                             {"kind": "options", "name": "普通日卖波动", "side": "short", "when": "control", "min_implied": 0, "max_implied": 9},
                             {"kind": "bogus"}]}
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
    # members × 2 rounds (algo judge only speaks in round 2) + 7 room syntheses + chair + editor
    assert rec["calls"] == len(members) * 2 - 1 + 7 + 2 == len(calls)
    algo = rec["rooms"]["algo"]
    bts = [b for o in algo["rounds"][0] for b in o["out"].get("backtest", [])]
    assert len(bts) == 6 and sum(1 for b in bts if b.get("error")) == 2       # bogus spec rejected
    assert all("考试段" in b["text"] for b in bts if not b.get("error"))
    assert any(f["topic"].startswith("算法回测") for f in rec["facts"])
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
    for t in ("主席结论", "数据正常，继续模拟", "期权研究室", "风控室", "否决 TSLA", "算法室", "代码回测", "资料包", "引用与数字", "反向押回撤"):
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


def test_failed_calls_mark_meeting_failed(env):  # noqa: F811
    s, _ = env

    def broken(model, system, user):
        raise TypeError("unexpected keyword argument")
    rec = agents.run_meeting(s, "test", llm=broken, push=False)
    assert rec["mode"] == "failed" and "调用失败" in rec["degraded_reason"]
    assert agents.last_full_run(s) is None


def test_sdk_call_signature_matches():
    """我们传给 SDK 的参数必须都是当前 anthropic 版本支持的（temperature 已被新版去掉）。"""
    import inspect
    import re as _re
    from pathlib import Path
    import anthropic
    ok = set(inspect.signature(anthropic.Anthropic(api_key="x").messages.create).parameters)
    root = Path(__file__).resolve().parent.parent / "desk"
    for f in ("agents.py", "extract.py"):
        for call in _re.findall(r"messages\.create\((.*?)\)\n", (root / f).read_text(), flags=_re.S):
            for kw in _re.findall(r"(\w+)=", call):
                assert kw in ok, (f, kw)


def test_algolab_backtest(env):  # noqa: F811
    s, _ = env
    from desk import algolab
    r = algolab.run_spec(s, {"kind": "weekend", "name": "x", "instruments": ["XYZ100"], "direction": "fade",
                             "min_abs_dev_bps": 0, "max_abs_dev_bps": 999}, "T", "run")
    res = r["result"]
    assert res["train"]["n"] + res["test"]["n"] == 2          # 2 of 3 history rows exceed the threshold
    assert res["trials_total"] == 1 and res["bar"] == 0.05
    r2 = algolab.run_spec(s, {"kind": "options", "name": "y", "side": "short", "when": "all"}, "T", "run")
    assert r2["result"]["trials_total"] == 2 and r2["result"]["bar"] == 0.025
    assert algolab.validate({"kind": "weekend", "direction": "sideways"})[0] is None


def test_intel_and_friday_spec(env):  # noqa: F811
    s, _ = env
    import shutil
    from pathlib import Path
    from desk import algolab
    (Path(s.root) / "knowledge").mkdir(exist_ok=True)
    shutil.copy(Path(__file__).resolve().parent.parent / "knowledge" / "jane_street.md", Path(s.root) / "knowledge")
    f = agents.build_bundle(s)
    assert sum(1 for x in f.items if x["topic"] == "Jane Street 公开情报") >= 8
    r = algolab.run_spec(s, {"kind": "options", "name": "到期日", "side": "short", "when": "friday"}, "T", "run")
    assert "result" in r          # 2026-09-01 is a Tuesday, 2026-09-16 Wednesday → zero trades, still valid
    assert r["result"]["train"]["n"] + r["result"]["test"]["n"] == 0
