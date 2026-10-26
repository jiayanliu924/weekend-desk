"""重开每日反思：fallback、build_facts、全中文（无网络）。"""
import json
import re
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from desk import config, reopen_reflect as rr
from desk.ledger import Ledger
from tests.test_alpaca_exec import ROOT

PT = ZoneInfo("America/Los_Angeles")


def ms(h):
    return int(datetime(2026, 10, 26, h, 0, tzinfo=PT).timestamp() * 1000)


@pytest.fixture
def s(tmp_path):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    return config.load(tmp_path)


def sample(s):
    L = Ledger(s.data, "reopen")
    L.append("decision", "e1", {"event": "e1", "name": "SPY", "coin": "xyz:SPY", "what": "window_closed", "max_overshoot_bps": 7.2, "t_ms": ms(9)})
    L.append("decision", "e2", {"event": "e2", "name": "x", "coin": None, "what": "skipped_selfcheck", "why": "测试", "t_ms": ms(10)})
    L.append("decision", "e1", {"event": "e1", "name": "QQQ", "coin": "xyz:QQQ", "what": "entry", "overshoot_bps": 12.0, "eff_k_bps": 9.0, "t_ms": ms(11)})
    L.append("open", "p1", {"pos_id": "p1", "coin": "xyz:QQQ", "t_ms": ms(11)})
    L.append("fill", "p1", {"pos_id": "p1", "signed_sz": 1.0, "px": 100.0, "purpose": "entry", "t_ms": ms(11)})
    L.append("fill", "p1", {"pos_id": "p1", "signed_sz": -1.0, "px": 100.2, "purpose": "exit_ioc", "t_ms": ms(12)})
    L.append("closed", "p1", {"pos_id": "p1", "coin": "xyz:QQQ", "reason": "converged", "pnl_usd": 0.15, "t_ms": ms(12)})


def test_build_facts(s):
    sample(s)
    f = rr.build_facts(s, "2026-10-26")
    assert f["今天"]["跑了几场事件"] == 1 and len(f["今天"]["跳过的事件"]) == 1
    assert f["今天"]["成交"][0]["pnl"] == 0.15 and f["今天"]["成交"][0]["side"] == "买"
    assert f["今天"]["各币最大过冲bps"]["xyz:QQQ"] == 12.0
    assert f["累计"]["成交笔数"] == 1 and f["累计"]["目标笔数"] == 20
    assert rr.build_facts(s, "2026-10-25")["今天"]["成交"] == []


def _plain(out):
    assert list(out) == list(rr.KEYS) and len(out["改进方向"]) <= 4
    txt = " ".join([out[k] for k in rr.KEYS if k != "改进方向"] + out["改进方向"])
    txt = re.sub(r"xyz:[A-Z]+|[A-Z]{2,5}\b|bps", "", txt)
    assert not re.search(r"[A-Za-z]", txt), txt


def test_fallback_with_and_without_fills(s):
    sample(s)
    _plain(rr.reflect(s, rr.build_facts(s, "2026-10-26")))      # 无 key → fallback
    _plain(rr.fallback(rr.build_facts(s, "2026-10-25")))


def test_llm_ok_and_budget(s, monkeypatch):
    good = {k: "好" for k in rr.KEYS}
    good["改进方向"] = ["a"] * 6
    llm = lambda m, sy, u: (json.dumps(good, ensure_ascii=False), 1000, 500)  # noqa: E731
    f = rr.build_facts(s, "2026-10-26")
    assert len(rr.reflect(s, f, llm=llm)["改进方向"]) == 4
    monkeypatch.setitem(s.raw.setdefault("agents", {}), "budget_day_usd", 0.0)
    called = []
    out = rr.reflect(s, f, llm=lambda *a: called.append(1))
    assert not called and out["一句话结论"]
    s.raw["agents"]["budget_day_usd"] = 4.0
    assert rr.reflect(s, f, llm=lambda *a: ("坏", 1, 1))["机制还成不成立"]
