"""仓位看守（#12）：快照解析、每种事件、只提醒一次、停留时间算法、"事件进行中"模式、整轮 poll 不下单、网页表单。"""
import json

import pytest

from desk import notify, positions
from tests.test_web import env  # noqa: F401

COIN = "xyz:BRENTOIL"
T0 = 1_760_000_000_000   # ms


def raw(positions_=(), marks=None, front=()):
    """造一份接口原样返回。positions_: [(coin, szi, entry, upnl)]"""
    marks = marks or {}
    uni = [{"name": c} for c in marks]
    ctx = [{"markPx": str(m)} for m in marks.values()]
    return {"t_ms": T0, "address": "0x" + "a" * 40,
            "states": {"": {"assetPositions": []},
                       "xyz": {"assetPositions": [{"type": "oneWay", "position": {"coin": c, "szi": str(s), "entryPx": str(e),
                                                                                "unrealizedPnl": str(u), "liquidationPx": "90.0",
                                                                                "leverage": {"type": "isolated", "value": 3}}}
                                                  for c, s, e, u in positions_]}},
            "orders": {"": [], "xyz": []},
            "front": {"": [], "xyz": list(front)},
            "ctxs": {"": [{"universe": []}, []], "xyz": [{"universe": uni}, ctx]}}


def snap(mark=None, size=10.0, side="long", entry=101.9, t_ms=T0, coin=COIN):
    """规则引擎用的统一快照。mark=None 表示仓位不在了。"""
    if mark is None:
        return {"t_ms": t_ms, "positions": {}, "marks": {}, "tpsl": {}}
    szi = size if side == "long" else -size
    return {"t_ms": t_ms, "positions": {coin: {"coin": coin, "name": "BRENTOIL", "side": side, "size": abs(szi), "entry_px": entry,
                                               "mark_px": mark, "unrealized_usd": round((mark - entry) * szi, 2)}},
            "marks": {coin: mark}, "tpsl": {}}


def reg_with(rule: dict, **kw) -> dict:
    return {"positions": {COIN: {"coin": COIN, "name": "BRENTOIL", "source": "manual", "open": True, "side": "long", "size": 10.0,
                                 "entry_px": 101.9, "rule": rule, "state": {}, **kw}}}


def kinds(evs):
    return [e["kind"] for e in evs]


MIN = 60_000


# ------------------------------------------------------------------ parser
def test_parse_snapshot_positions_marks_tpsl():
    r = raw([(COIN, "-2.5", "101.9", "-3.2"), ("xyz:NVDA", "0", "1", "0")], marks={COIN: 100.8, "xyz:NVDA": 180.0},
            front=[{"coin": COIN, "isTrigger": True, "orderType": "Stop Market", "triggerPx": "100.12"},
                   {"coin": COIN, "isTrigger": True, "orderType": "Take Profit Market", "triggerPx": "104.0"},
                   {"coin": COIN, "isTrigger": False, "orderType": "Limit", "limitPx": "99"}])
    s = positions.parse_snapshot(r)
    assert list(s["positions"]) == [COIN]                       # szi=0 的不算仓位
    p = s["positions"][COIN]
    assert p["side"] == "short" and p["size"] == 2.5 and p["entry_px"] == 101.9 and p["mark_px"] == 100.8
    assert p["unrealized_usd"] == -3.2 and p["liq_px"] == 90.0 and p["leverage"] == 3 and p["name"] == "BRENTOIL"
    assert s["marks"]["xyz:NVDA"] == 180.0
    assert s["tpsl"][COIN] == {"tp": 104.0, "sl": 100.12}
    # 接口某一段出错（dict 里带 error）也不炸
    r["front"]["xyz"] = {"error": "boom"}
    assert positions.parse_snapshot(r)["tpsl"] == {}


# ------------------------------------------------------------------ events
def test_new_external_position_registered_once():
    reg, evs = positions.process({"positions": {}}, snap(102.0))
    assert kinds(evs) == ["NEW_EXTERNAL_POSITION"]
    e = reg["positions"][COIN]
    assert e["source"] == "manual" and e["open"] and e["first_seen"] and e["entry_px"] == 101.9 and e["size"] == 10.0 and e["side"] == "long"
    reg, evs = positions.process(reg, snap(102.1, t_ms=T0 + 30_000))
    assert evs == []                                            # 第二次看到不再提醒
    # bot 自己开的仓：登记为 bot，不提醒
    reg2, evs2 = positions.process({"positions": {}}, snap(102.0), bot_coins={COIN})
    assert evs2 == [] and reg2["positions"][COIN]["source"] == "bot"


def test_soft_low_dwell_math_and_dedupe():
    rule = {"soft_low": 101.5, "dwell_min": 10, "sl": 100.12}
    reg = reg_with(rule)
    reg, evs = positions.process(reg, snap(101.4, t_ms=T0))            # 刚跌破：开始计时
    assert evs == [] and reg["positions"][COIN]["state"]["below_low_since_ms"] == T0
    reg, evs = positions.process(reg, snap(101.3, t_ms=T0 + 9 * MIN + 59_000))   # 9 分 59 秒：还不够
    assert evs == []
    reg, evs = positions.process(reg, snap(101.3, t_ms=T0 + 10 * MIN))          # 满 10 分钟：提醒
    assert kinds(evs) == ["SOFT_LOW_BROKEN"] and evs[0]["dwell_min"] == pytest.approx(10.0)
    reg, evs = positions.process(reg, snap(101.2, t_ms=T0 + 20 * MIN))          # 继续在下面：不重复
    assert evs == []
    reg, evs = positions.process(reg, snap(101.6, t_ms=T0 + 21 * MIN))          # 回到上面：计时清零
    assert evs == [] and "below_low_since_ms" not in reg["positions"][COIN]["state"]
    reg, evs = positions.process(reg, snap(101.4, t_ms=T0 + 22 * MIN))          # 再跌破：从头计时
    assert evs == []
    reg, evs = positions.process(reg, snap(101.4, t_ms=T0 + 31 * MIN))          # 新一轮满 10 分钟才再次提醒（新的状态转换）
    assert evs == []
    reg, evs = positions.process(reg, snap(101.4, t_ms=T0 + 32 * MIN))
    assert kinds(evs) == ["SOFT_LOW_BROKEN"]


def test_ongoing_doubles_dwell_and_suppresses_unless_near_sl():
    rule = {"soft_low": 101.5, "dwell_min": 10, "sl": 100.12, "event_status": "ongoing"}
    assert positions.effective_dwell_ms(rule) == 20 * MIN
    reg = reg_with(rule)
    reg, _ = positions.process(reg, snap(101.0, t_ms=T0))
    reg, evs = positions.process(reg, snap(101.0, t_ms=T0 + 10 * MIN))      # 普通模式会提醒；进行中要 20 分钟
    assert evs == []
    reg, evs = positions.process(reg, snap(101.0, t_ms=T0 + 20 * MIN))      # 够 20 分钟，但离 SL 还有 0.9%：压住不提醒
    assert evs == [] and not reg["positions"][COIN]["state"].get("low_fired")
    reg, evs = positions.process(reg, snap(100.5, t_ms=T0 + 21 * MIN))      # 离 SL 0.38%（<0.5%）：提醒
    assert kinds(evs) == ["SOFT_LOW_BROKEN"] and "翻倍" in positions.message(evs[0])[1]
    # 事件结束后恢复正常：10 分钟即可
    rule2 = {**rule, "event_status": "concluded"}
    assert positions.effective_dwell_ms(rule2) == 10 * MIN


def test_soft_high_reclaimed_only_on_transition():
    reg = reg_with({"soft_high": 102.5})
    reg, evs = positions.process(reg, snap(102.8, t_ms=T0))                 # 第一次看到就在上面：不算"收复"
    assert evs == []
    reg, evs = positions.process(reg, snap(102.2, t_ms=T0 + MIN))           # 跌下去
    assert evs == []
    reg, evs = positions.process(reg, snap(102.6, t_ms=T0 + 2 * MIN))       # 收复
    assert kinds(evs) == ["SOFT_HIGH_RECLAIMED"]
    reg, evs = positions.process(reg, snap(102.9, t_ms=T0 + 3 * MIN))       # 还在上面：不重复
    assert evs == []


def test_exit_time_reached_once():
    rule = positions.parse_rule(exit_at="2026-10-12T11:30-07:00")
    assert rule["exit_at_ms"] == 1_791_829_800_000
    reg = reg_with(rule)
    reg, evs = positions.process(reg, snap(102.0, t_ms=rule["exit_at_ms"] - 1))
    assert evs == []
    reg, evs = positions.process(reg, snap(102.0, t_ms=rule["exit_at_ms"]))
    assert kinds(evs) == ["EXIT_TIME_REACHED"] and positions.advice(evs[0]) == "到退出时刻，该走了"
    reg, evs = positions.process(reg, snap(102.0, t_ms=rule["exit_at_ms"] + MIN))
    assert evs == []
    with pytest.raises(ValueError):
        positions.parse_rule(exit_at="2026-10-12T11:30")                   # 不带时区不收


def test_sl_likely_hit_vs_position_closed():
    rule = {"sl": 100.12}
    reg = reg_with(rule)
    reg, _ = positions.process(reg, snap(100.1, t_ms=T0))                   # 最后一次标记价已经 ≤ SL
    reg, evs = positions.process(reg, snap(None, t_ms=T0 + 30_000))         # 仓位消失
    assert kinds(evs) == ["SL_LIKELY_HIT"] and evs[0]["last_mark"] == 100.1
    e = reg["positions"][COIN]
    assert not e["open"] and e["closed_at"]
    reg, evs = positions.process(reg, snap(None, t_ms=T0 + 60_000))         # 继续不在：不重复
    assert evs == []
    # 另一种：最后标记价离 SL 很远 → 手动平/TP 带走
    reg = reg_with(rule)
    reg, _ = positions.process(reg, snap(103.0, t_ms=T0))
    reg, evs = positions.process(reg, snap(None, t_ms=T0 + 30_000))
    assert kinds(evs) == ["POSITION_CLOSED"]
    # 空头：标记价 ≥ SL 才算打到
    reg = reg_with(rule, side="short")
    reg, _ = positions.process(reg, snap(100.2, side="short", t_ms=T0))
    reg, evs = positions.process(reg, snap(None, t_ms=T0 + 30_000))
    assert kinds(evs) == ["SL_LIKELY_HIT"]


def test_reopen_after_close_is_a_new_external_position():
    reg = reg_with({"sl": 100.0})
    reg, _ = positions.process(reg, snap(102.0, t_ms=T0))
    reg, evs = positions.process(reg, snap(None, t_ms=T0 + MIN))
    assert kinds(evs) == ["POSITION_CLOSED"]
    reg, evs = positions.process(reg, snap(102.0, t_ms=T0 + 2 * MIN))
    assert kinds(evs) == ["NEW_EXTERNAL_POSITION"] and reg["positions"][COIN]["rule"] == {"sl": 100.0}   # 规则保留


def test_message_is_plain_chinese():
    reg = reg_with({"soft_low": 101.5, "dwell_min": 0, "open_rule": "看空油价，等 OPEC 消息"})
    reg, evs = positions.process(reg, snap(101.0, t_ms=T0))
    title, body, pri = positions.message(evs[0])
    assert "BRENTOIL" in title and "跌破软下限" in title and pri == "high"
    for s in ("做多 10.0 张", "入场 101.90", "标记价 101.00", "浮动盈亏 $-9.00", "按规则该市价平", "看空油价", "不会替你下单"):
        assert s in body, s


# ------------------------------------------------------------------ registry / rules
def test_set_rule_resolves_name_and_keeps_others(env):  # noqa: F811
    s, _ = env
    e = positions.set_rule(s, "BRENTOIL", sl=100.12, soft_low=101.5, dwell_min=10)
    assert e["coin"] == COIN and e["rule"] == {"sl": 100.12, "soft_low": 101.5, "dwell_min": 10.0}
    e = positions.set_rule(s, "xyz:BRENTOIL", soft_high="102.5", event_status="ongoing")
    assert e["rule"]["sl"] == 100.12 and e["rule"]["soft_high"] == 102.5 and e["rule"]["event_status"] == "ongoing"
    e = positions.set_rule(s, "BRENTOIL", clear=True, sl=99)
    assert e["rule"] == {"sl": 99.0}
    with pytest.raises(ValueError):
        positions.set_rule(s, "BRENTOIL", event_status="maybe")
    assert json.loads((s.data / "state" / "positions.json").read_text())["positions"][COIN]["rule"] == {"sl": 99.0}


def test_poll_end_to_end_no_orders(env, monkeypatch):  # noqa: F811
    s, _ = env
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0x" + "a" * 40)
    calls, pushed = [], []
    monkeypatch.setattr(notify, "push", lambda st, t, b, priority="default": pushed.append((t, b, priority)) or True)

    def fake_post(body):
        calls.append(body)
        assert body["type"] in ("clearinghouseState", "openOrders", "frontendOpenOrders", "metaAndAssetCtxs")   # 只读接口
        r = raw([(COIN, "10", "101.9", "-9.0")], marks={COIN: 101.0})
        return {"clearinghouseState": r["states"], "openOrders": r["orders"], "frontendOpenOrders": r["front"],
                "metaAndAssetCtxs": r["ctxs"]}[body["type"]][body.get("dex", "")]

    positions.set_rule(s, "BRENTOIL", soft_low=101.5, dwell_min=0)
    out = positions.poll(s, post=fake_post, now_ms=T0)
    assert kinds(out["events"]) == ["NEW_EXTERNAL_POSITION", "SOFT_LOW_BROKEN"]
    assert [p[0] for p in pushed] == ["仓位看守：BRENTOIL 发现新的手动仓位", "仓位看守：BRENTOIL 跌破软下限"]
    assert all(c.get("user", "0x" + "a" * 40) == "0x" + "a" * 40 for c in calls) and len(calls) == 8
    reg = positions.load(s)
    assert reg["n_open"] == 1 and reg["positions"][COIN]["open"] and reg["positions"][COIN]["last_event"]["kind"] == "SOFT_LOW_BROKEN"
    # 原始快照和 30 秒标记价都落了盘
    from desk import rawstore
    positions.flush()
    assert rawstore.query(s.data, positions.STREAM, select="count(*)", order="1")[0][0] == 1
    rows = rawstore.query(s.data, positions.MARK_STREAM, select="key, payload")
    assert rows[0][0] == COIN and json.loads(rows[0][1])["mark_px"] == 101.0
    # 没地址：什么都不做
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS")
    assert positions.poll(s, post=fake_post) is None
    with pytest.raises(NotImplementedError):
        positions.close_reduce_only(s, COIN)


def test_web_card_and_form(env, monkeypatch):  # noqa: F811
    s, web = env
    from fastapi.testclient import TestClient
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS", raising=False)
    assert "还没填钱包地址" in c.get("/admin").text
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0x" + "a" * 40)
    r = c.post("/settings/position", data={"coin": COIN, "sl": "100.12", "soft_low": "101.5", "soft_high": "102.5", "dwell_min": "10",
                                            "exit_at": "2026-10-12T11:30-07:00", "open_rule": "看空油价", "event_status": "ongoing"})
    assert "已保存 BRENTOIL 的规则" in r.text and "事件进行中" in r.text
    assert positions.load(s)["positions"][COIN]["rule"]["exit_at_ms"] == 1_791_829_800_000
    assert "没保存" in c.post("/settings/position", data={"coin": COIN, "exit_at": "明天"}).text
    reg, _ = positions.process(positions.load(s), snap(101.0))
    positions.save(s, reg)
    home = c.get("/admin").text
    assert "仓位看守（包括你手动开的仓）" in home and "软下限 101.5" in home and "看空油价" in home
    assert "仓位规则（仓位看守）" in c.get("/settings").text
