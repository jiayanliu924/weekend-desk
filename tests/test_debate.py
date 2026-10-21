"""Card B 多空辩论闸门：否决 / 缩仓 / 不放大 / 丢弃无引用观点 / 出错 skip / 关闭直通 / 接进 open_for_sample。全部假 LLM，不碰网络。"""
import json
import shutil
from pathlib import Path

import pytest

from desk import alpaca_exec as ax, config, debate
from tests.test_alpaca_exec import FakeAlpaca, NOW, RISK, ROOT, paper_keys, smp

CTX = {"asof": "x", "facts": [{"id": f"F{i}", "topic": "t", "text": "x", "at": None, "untrusted": False} for i in (1, 2, 3)]}
PROP = {"sid": "EVT-1", "underlying": "SPY", "structure": "A", "qty": 4, "max_loss_usd": 400}


class FakeLLM:
    def __init__(self, risk=None, bull=None, bear=None, boom=False, junk=False):
        self.risk = risk or {"direction": "with", "confidence": 0.7, "size_mult": 1.0, "veto": False, "why": "ok"}
        self.bull = bull or {"stance": "多", "points": [{"claim": "a", "fact_ids": ["F1"]}], "confidence": 0.6}
        self.bear = bear or {"stance": "空", "points": [{"claim": "b", "fact_ids": ["F2"]}], "confidence": 0.5}
        self.boom, self.junk, self.calls = boom, junk, []

    def __call__(self, model, system, user, max_tokens=900):
        self.calls.append(model)
        if self.boom:
            raise TimeoutError("slow")
        if self.junk:
            return "not json at all", 10, 10
        d = self.risk if "风控官" in system else self.bull if "多头" in system else self.bear
        return json.dumps(d, ensure_ascii=False), 1000, 300


@pytest.fixture
def s(tmp_path):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    st = config.load(tmp_path)
    st.raw["alpaca"] = {**st.raw["alpaca"], "enabled": True}
    return st


def gate(s, llm, **kw):
    return debate.run_gate(s, CTX, PROP, llm=llm, now=NOW)


def test_pass_and_saved(s):
    r = gate(s, FakeLLM())
    assert r["ok"] and not r["veto"] and r["size_mult"] == 1.0 and r["cost_usd"] > 0
    assert r["bull"]["points"][0]["fact_ids"] == ["F1"]
    f = Path(s.data, "debates", r["id"] + ".json")
    assert f.exists() and "context" in json.loads(f.read_text())


def test_models_default_to_agents(s):
    llm = FakeLLM()
    gate(s, llm)
    assert llm.calls == ["claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001", "claude-sonnet-5-5"]


def test_veto_flag_against_and_tiny_size(s):
    assert gate(s, FakeLLM(risk={"direction": "with", "confidence": 0.9, "size_mult": 1, "veto": True}))["veto"]
    assert gate(s, FakeLLM(risk={"direction": "against", "confidence": 0.6, "size_mult": 1, "veto": False}))["veto"]
    assert not gate(s, FakeLLM(risk={"direction": "against", "confidence": 0.59, "size_mult": 1, "veto": False}))["veto"]
    assert gate(s, FakeLLM(risk={"direction": "with", "confidence": 0.9, "size_mult": 0.2, "veto": False}))["veto"]


def test_shrink(s):
    r = gate(s, FakeLLM(risk={"direction": "with", "confidence": 0.5, "size_mult": 0.5, "veto": False}))
    assert not r["veto"] and r["size_mult"] == 0.5


@pytest.mark.parametrize("bad", [{"veto": "true"}, {"veto": 1}, {"veto": None}, {"size_mult": 50}, {"size_mult": 1.7},
                                 {"size_mult": -0.1}, {"size_mult": "garbage"}, {"confidence": 3}, {"confidence": "high"},
                                 {"direction": "maybe"}, {"size_mult": True}])
def test_malformed_risk_is_parse_failure_not_open(s, bad):
    r = gate(s, FakeLLM(risk={"direction": "with", "confidence": 0.5, "size_mult": 1.0, "veto": False, **bad}))
    assert r["veto"] and not r["ok"] and r["error"] and r["error_kind"] == "parse" and r["size_mult"] == 0.0


def test_never_increase(s):
    r = gate(s, FakeLLM(risk={"direction": "with", "confidence": 0.5, "size_mult": 1, "veto": False}))
    assert r["size_mult"] == 1.0


def test_drops_uncited_points(s):
    bull = {"stance": "多", "confidence": 0.5, "points": [{"claim": "a", "fact_ids": []}, {"claim": "b", "fact_ids": ["F99"]},
                                                         {"claim": "c", "fact_ids": ["F3"]}]}
    r = gate(s, FakeLLM(bull=bull))
    assert r["reasons_dropped"] == 2 and [p["claim"] for p in r["bull"]["points"]] == ["c"]


@pytest.mark.parametrize("kw", [{"boom": True}, {"junk": True}])
def test_error_skips(s, kw):
    r = gate(s, FakeLLM(**kw))
    assert r["veto"] and not r["ok"] and r["error"]
    assert Path(s.data, "debates", r["id"] + ".json").exists()


def test_error_pass_config(s):
    s.raw["debate"] = {"on_error": "pass"}
    r = gate(s, FakeLLM(boom=True))
    assert r["ok"] and not r["veto"] and r["size_mult"] == 1.0 and r["error"]


def test_cost_cap(s):
    s.raw["debate"] = {"max_cost_usd": 0.0005}
    r = gate(s, FakeLLM())
    assert r["veto"] and "上限" in r["error"]


def test_disabled_passthrough(s):
    s.raw["debate"] = {"enabled": False}
    llm = FakeLLM()
    r = gate(s, llm)
    assert r["ok"] and not r["veto"] and r["size_mult"] == 1.0 and llm.calls == [] and r["id"] is None


def test_build_context_ids(s):
    class C:
        def latest_price(self, u): return 580.0
        def bars(self, u, a, b): return [{"c": 500.0 + i} for i in range(30)]
    ctx = debate.build_context(s, {"underlying": "SPY", "qty": 1, "max_loss_usd": 100}, C(), NOW)
    ids = [f["id"] for f in ctx["facts"]]
    assert ids == [f"F{i + 1}" for i in range(len(ids))] and any("5日" in f["topic"] for f in ctx["facts"]) and any("20日" in f["topic"] for f in ctx["facts"])


# ---------------------------------------------------------------- 接进 open_for_sample
def run_open(s, monkeypatch, llm, risk=RISK, sid="EVT-CPI-2026-10"):
    paper_keys(s, monkeypatch, risk)
    s.raw["debate"] = {"enabled": True}
    fk = FakeAlpaca()
    rec = ax.open_for_sample(s, smp(sid=sid), {"chosen": "A"}, now=NOW, transport=fk.transport, debate_llm=llm)
    return rec, fk


def test_integration_veto_sends_no_order(s, monkeypatch):
    rec, fk = run_open(s, monkeypatch, FakeLLM(risk={"direction": "against", "confidence": 0.9, "size_mult": 1, "veto": True}))
    assert rec["kind"] == "skip" and "辩论编号" in rec["reason"] and rec["debate"]["id"]
    assert not fk.orders()


def test_integration_error_sends_no_order(s, monkeypatch):
    rec, fk = run_open(s, monkeypatch, FakeLLM(boom=True))
    assert rec["kind"] == "debate_error" and not fk.orders()


def test_integration_shrink_and_never_more(s, monkeypatch):
    base, fk0 = run_open(s, monkeypatch, FakeLLM(risk={"direction": "with", "confidence": 0.5, "size_mult": 1.0, "veto": False}), risk=3000.0)
    q0 = base["qty"]
    assert base["kind"] == "open" and q0 >= 2 and base["debate"]["size_mult"] == 1.0
    Path(s.data, "ledger", "alpaca.jsonl").unlink()
    rec, fk = run_open(s, monkeypatch, FakeLLM(risk={"direction": "with", "confidence": 0.5, "size_mult": 0.5, "veto": False}), risk=3000.0, sid="EVT-CPI-2026-11")
    assert rec["kind"] == "open" and rec["qty"] == q0 // 2 and int(fk.orders()[0][2]["qty"]) == rec["qty"]


class SlowLLM(FakeLLM):
    def __call__(self, *a, **k):
        import time
        time.sleep(3)
        return super().__call__(*a, **k)


def test_deadline_hits_fast(s):
    import time
    s.raw["debate"] = {"deadline_s": 0.3}
    t0 = time.monotonic()
    r = gate(s, SlowLLM())
    assert time.monotonic() - t0 < 1.5
    assert r["veto"] and r["error_kind"] == "deadline"


def test_integration_transient_error_retries_then_final(s, monkeypatch):
    rec, fk = run_open(s, monkeypatch, FakeLLM(boom=True))
    assert rec["kind"] == "debate_error" and rec["attempt"] == 1 and not fk.orders()
    # 同一个 sid 再锁一次：能重试，这次成功就下单
    fk2 = FakeAlpaca()
    rec2 = ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk2.transport, debate_llm=FakeLLM())
    assert rec2["kind"] == "open" and fk2.orders()


def test_integration_second_transient_error_is_final(s, monkeypatch):
    run_open(s, monkeypatch, FakeLLM(boom=True))
    rec2 = ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=FakeAlpaca().transport, debate_llm=FakeLLM(boom=True))
    assert rec2["kind"] == "skip"
    assert ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=FakeAlpaca().transport, debate_llm=FakeLLM()) is None


def test_integration_parse_error_is_final_skip(s, monkeypatch):
    rec, fk = run_open(s, monkeypatch, FakeLLM(risk={"direction": "with", "confidence": 0.5, "size_mult": 9, "veto": False}))
    assert rec["kind"] == "skip" and not fk.orders()


def test_integration_budget_alerts(s, monkeypatch):
    pushed = []
    monkeypatch.setattr(ax.notify, "push", lambda st, title, body, priority="default": pushed.append((title, priority)))
    s.raw["agents"] = {"budget_day_usd": 0.0}
    rec, fk = run_open(s, monkeypatch, FakeLLM())
    assert rec["kind"] == "debate_error" and not fk.orders()
    assert any("预算" in t and p == "high" for t, p in pushed)


def test_requote_after_debate(s, monkeypatch):
    rec, fk = run_open(s, monkeypatch, FakeLLM())
    snaps = [c for c in fk.calls if "/options/snapshots/" in c[1]]
    assert rec["kind"] == "open" and len(snaps) == 2
