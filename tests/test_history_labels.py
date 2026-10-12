"""knowledge/history_event_labels.json：字段齐全、枚举合法、id 唯一、每条至少一个来源、必含事件都在、README 里的统计和数据一致。"""
import json
import re
from pathlib import Path

import pytest

from desk import relevance

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "knowledge" / "history_event_labels.json"
README = ROOT / "knowledge" / "history_event_labels.README.md"

CATEGORIES = {"geopolitical_oil", "geopolitical_broad", "macro_policy", "sanctions", "opec",
              "corporate_single_name", "crypto_native", "natural_disaster", "pandemic"}
STATUSES = {"concluded", "ongoing"}
CONFIDENCE = {"high", "medium", "low"}
REQUIRED = {"event_id": str, "date_utc": str, "weekend": bool, "event_status": str, "category": str,
            "entities": list, "headline": str, "summary": str, "sources": list, "materiality": int,
            "initial_direction": dict, "reopen_move_bps": dict, "reversal_within_2d": bool,
            "confidence": str, "notes": str}
DATE_RE = re.compile(r"^20(19|2[0-6])-\d{2}-\d{2}(T\d{2}:\d{2}Z)?$")
MUST_HAVE = ["abqaiq-2019-09-14", "soleimani-2020-01-03", "opec-breakdown-price-war-2020-03-08",
             "fed-emergency-zero-2020-03-15", "russia-invades-ukraine-2022-02-24", "russia-oil-ban-talk-2022-03-06",
             "hamas-attack-2023-10-07", "iran-drone-attack-israel-2024-04-13", "iran-missile-attack-israel-2024-10-01",
             "israel-strikes-iran-2025-06-13", "us-strikes-fordow-2025-06-21", "opec-surprise-cut-2023-04-02",
             "svb-fed-backstop-2023-03-12", "yen-carry-unwind-2024-08-05", "deepseek-2025-01-27",
             "liberation-day-tariffs-2025-04-02", "houthi-riyadh-airport-2026-10-10"]


@pytest.fixture(scope="module")
def data():
    return json.loads(PATH.read_text())


def test_size_and_unique_ids(data):
    ev = data["events"]
    assert 40 <= len(ev) <= 80
    ids = [e["event_id"] for e in ev]
    assert len(ids) == len(set(ids))
    assert all(re.match(r"^[a-z0-9-]+-20\d{2}-\d{2}-\d{2}$", i) for i in ids)


def test_schema_and_enums(data):
    for e in data["events"]:
        for k, t in REQUIRED.items():
            assert k in e, (e["event_id"], k)
            assert isinstance(e[k], t), (e["event_id"], k, type(e[k]))
        assert "move_90min_bps" in e and (e["move_90min_bps"] is None or isinstance(e["move_90min_bps"], dict))
        assert DATE_RE.match(e["date_utc"]), e["event_id"]
        assert e["event_status"] in STATUSES, e["event_id"]
        assert e["category"] in CATEGORIES, e["event_id"]
        assert e["confidence"] in CONFIDENCE, e["event_id"]
        assert 1 <= e["materiality"] <= 3, e["event_id"]
        assert e["entities"] and all(x == x.upper() for x in e["entities"]), e["event_id"]
        assert all(v in ("up", "down", "flat") for v in e["initial_direction"].values()), e["event_id"]
        assert e["reopen_move_bps"] and all(isinstance(v, (int, float)) for v in e["reopen_move_bps"].values()), e["event_id"]
        if e["move_90min_bps"]:
            assert all(isinstance(v, (int, float)) for v in e["move_90min_bps"].values()), e["event_id"]
        # 至少一个来源，且是 http(s) 链接
        assert len(e["sources"]) >= 1, e["event_id"]
        assert all(s.startswith("http") for s in e["sources"]), e["event_id"]
        assert e["headline"].strip() and e["summary"].strip()


def test_must_have_events(data):
    ids = {e["event_id"] for e in data["events"]}
    missing = [m for m in MUST_HAVE if m not in ids]
    assert not missing, missing
    h = next(e for e in data["events"] if e["event_id"] == "houthi-riyadh-airport-2026-10-10")
    assert h["weekend"] and h["event_status"] == "ongoing" and h["category"] == "geopolitical_oil"
    assert "OIL" in h["entities"] and h["move_90min_bps"]["BRENT"] < 0 < h["reopen_move_bps"]["BRENT"]
    assert any("nationthailand.com" in s for s in h["sources"]) and any("cbsnews.com" in s for s in h["sources"])
    f = next(e for e in data["events"] if e["event_id"] == "us-strikes-fordow-2025-06-21")
    assert f["weekend"] and f["reversal_within_2d"] and f["move_90min_bps"]["BRENT"] > 0 > f["reopen_move_bps"]["BRENT"]
    d = next(e for e in data["events"] if e["event_id"] == "iran-drone-attack-israel-2024-04-13")
    assert d["weekend"] and d["reopen_move_bps"]["BRENT"] < 0          # 油价周一反跌


def test_oil_events_are_relevant_to_brentoil(data):
    """标签集的实体口径和 desk/relevance.py 一致：所有 geopolitical_oil / opec 事件都应命中 BRENTOIL。"""
    for e in data["events"]:
        if e["category"] in ("geopolitical_oil", "opec"):
            assert relevance.is_relevant("BRENTOIL", e["entities"]), e["event_id"]


def test_readme_tally_matches_data(data):
    """README 里的 concluded/ongoing 反转计数必须和数据一致，防止两边漂移。"""
    oil = [e for e in data["events"] if e["category"] == "geopolitical_oil"]
    txt = README.read_text()
    for st in ("concluded", "ongoing"):
        g = [e for e in oil if e["event_status"] == st]
        rev = sum(1 for e in g if e["reversal_within_2d"])
        row = next(l for l in txt.splitlines() if l.startswith(f"| **{st}"))
        cells = [c.strip().strip("*") for c in row.split("|")[1:-1]]
        assert int(cells[1]) == len(g) and int(cells[2]) == rev and int(cells[3]) == len(g) - rev, (st, cells[:4])
