"""卡 G8：风控否决带原因分类（事件/价格异常/缺数据/平台），旧格式与"只讲样本少"的否决不拦真钱；资料包带标记价；审计显示。"""
from datetime import date, datetime, timezone

import pytest

from desk import agents, autotrade
from desk.clock import weekend_for_friday
from tests.test_web import env  # noqa: F401


def _lock(side=-1):
    return {"action": {"side": side, "reason": "噪音周末，反向", "entry_px": 30000.0},
            "features": {"best_bid": 29990.0, "best_ask": 30010.0, "bid_depth_usd": 1e6, "ask_depth_usd": 1e6, "dec_mid": 30000.0},
            "void": False, "void_reasons": [], "name": "XYZ100", "coin": "xyz:XYZ100"}


def _run_with_veto(monkeypatch, veto):
    monkeypatch.setattr(agents, "last_full_run",
                        lambda settings, within_h=36: {"rooms": {"risk": {"synth": {"veto": veto}}}})


def _enter(s):
    autotrade.put(s, mode="shadow", capital_usd=700)       # 演习模式：不碰交易所，记录里照样走完否决判断
    w = weekend_for_friday(date(2026, 10, 9), s, s.inst("XYZ100"))
    return autotrade.entry(s, w, _lock())


# ---- ① 旧格式（没有 code）不拦单
def test_old_format_veto_without_code_does_not_block(env, monkeypatch):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "XYZ100", "why": "财报在决策日前"}])
    assert autotrade._vetoed(s, "XYZ100") is None
    rec = _enter(s)
    assert "skipped" not in rec and rec["sent"] is False and rec["notional_usd"] > 0
    assert rec["veto_ignored"] == "财报在决策日前"


@pytest.mark.parametrize("code", ["随便", "", None, "样本不足", 5, "事件|价格异常|缺数据|平台", "事件/平台", "事件价格异常", "   。", ["事件"]])
def test_unknown_code_ignored(env, monkeypatch, code):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "XYZ100", "code": code, "why": "财报在决策日前"}])
    assert autotrade._vetoed(s, "XYZ100") is None
    assert _enter(s)["veto_ignored"] == "财报在决策日前"


# ---- ② 四类 code 拦单
@pytest.mark.parametrize("code,why", [("事件", "周一有财报"), ("价格异常", "周末出现插针"), ("缺数据", "最近一条价格是 3 天前的"),
                                      ("平台", "交易所预言机停更")])
def test_valid_code_blocks(env, monkeypatch, code, why):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "xyz100", "code": code, "why": why}])
    assert autotrade._vetoed(s, "XYZ100") == why
    rec = _enter(s)
    assert rec["skipped"] == f"风控否决：{why}" and "veto_ignored" not in rec


def test_other_contract_not_blocked(env, monkeypatch):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "NVDA", "code": "事件", "why": "财报"}])
    rec = _enter(s)
    assert "skipped" not in rec and "veto_ignored" not in rec


# ---- ③ code 合法但 why 只讲样本少 → 忽略并留痕
@pytest.mark.parametrize("why", ["实盘样本不足 30 个周末", "样本太少，还在验证期", "实盘样本还没有30个周末", "实盘样本没有积累到 30 个周末",
                                 "样本数量不够", "缺少足够样本，不足 30 个周末"])
def test_sample_only_reason_ignored(env, monkeypatch, why):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "XYZ100", "code": "平台", "why": why}])
    assert autotrade._vetoed(s, "XYZ100") is None
    rec = _enter(s)
    assert "skipped" not in rec and rec["veto_ignored"] == why


@pytest.mark.parametrize("why", ["样本少，而且周一有财报", "样本不足，且缺标记价无法排查插针", "实盘样本少，交易所流动性差", "样本不足；议息会议在决策日前",
                                 "样本少，缺少周末价格数据"])
def test_sample_plus_concrete_reason_still_blocks(env, monkeypatch, why):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "XYZ100", "code": "事件", "why": why}])
    assert autotrade._vetoed(s, "XYZ100") == why


def test_ignored_one_then_valid_one_blocks_and_both_recorded(env, monkeypatch):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "XYZ100", "why": "老格式"}, {"name": "XYZ100", "code": "价格异常", "why": "插针"}])
    rec = _enter(s)
    assert rec["skipped"] == "风控否决：插针"
    assert autotrade._veto_check(s, "XYZ100") == ("插针", "老格式")


# ---- 风控室提示里写明规则
def test_risk_prompt_has_code_and_sample_rule():
    for txt in (agents.RISK_EXTRA,):
        assert '"code"' in txt and all(c in txt for c in agents.VETO_CODES)
        assert "不是否决理由" in txt and "验证期" in txt
    assert autotrade._VETO_CODES == agents.VETO_CODES


# ---- ④ 资料包带标记价
def _ctx(mid, mark, oracle, age_ns=0):
    return (1_000_000_000_000 - age_ns, {"midPx": str(mid), "markPx": str(mark), "oraclePx": str(oracle), "funding": "0.0000125", "openInterest": "10"})


def test_bundle_has_mark_price_fact(env, monkeypatch):  # noqa: F811
    s, _ = env
    import desk.features as feats
    now = datetime.fromtimestamp(1_000_000_000_000 / 1e9 + 600, tz=timezone.utc)
    monkeypatch.setattr(feats, "last_ctx_before", lambda settings, coin, ns, lookback_h=72: _ctx(30030.0, 30000.0, 29900.0))
    F = agents.build_bundle(s, now)
    marks = [f for f in F.items if f["topic"].startswith("标记价 ")]
    assert len(marks) == len(s.instruments)
    t = next(f["text"] for f in marks if f["topic"] == "标记价 XYZ100")
    thr = f"{s['risk']['dislocation_bps']:.0f}"
    assert "标记价" in t and "30,030.00" in t and "30,000.00" in t and "29,900.00" in t
    assert "+10.0" in t and "+43.5" in t and "+33.4" in t           # 最新价比标记价 / 比参考价 / 标记价比参考价（万分点）
    assert f"超过 {thr} 个万分点，视为可能插针" in t and "没超过" in t       # 10 < 25：没看到插针迹象
    # 偏离超过阈值时明确提示
    monkeypatch.setattr(feats, "last_ctx_before", lambda settings, coin, ns, lookback_h=72: _ctx(30300.0, 30000.0, 29900.0))
    t2 = next(f["text"] for f in agents.build_bundle(s, now).items if f["topic"] == "标记价 XYZ100")
    assert "可能是插针，要小心" in t2


def test_bundle_mark_fact_when_mark_missing(env, monkeypatch):  # noqa: F811
    s, _ = env
    import desk.features as feats
    now = datetime.fromtimestamp(1_000_000_000_000 / 1e9 + 600, tz=timezone.utc)
    monkeypatch.setattr(feats, "last_ctx_before",
                        lambda settings, coin, ns, lookback_h=72: (1_000_000_000_000, {"midPx": "100", "oraclePx": "100"}))
    t = next(f["text"] for f in agents.build_bundle(s, now).items if f["topic"] == "标记价 XYZ100")
    assert "没有标记价" in t and "无法排查插针" in t


# ---- ⑤ 审计显示
def test_audit_rules_text(env):  # noqa: F811
    s, _ = env
    facts = agents.Facts(0)
    h = agents.rules_hash(s.root)
    autotrade.put(s, mode="live")
    ck = agents.audit_checks(s, facts, [], 0, h)["rules"]
    assert "周末线真钱已开（自动下单=live）" in ck["text"] and "实盘开关关闭" not in ck["text"] and ck["ok"]
    assert "规则文件被修改" in agents.audit_checks(s, facts, [], 0, "other")["rules"]["text"]
    assert not agents.audit_checks(s, facts, [], 0, "other")["rules"]["ok"]
    autotrade.put(s, mode="shadow")
    ck2 = agents.audit_checks(s, facts, [], 0, h)["rules"]
    assert "实盘开关关闭" in ck2["text"] and "周末线真钱已开" not in ck2["text"] and ck2["ok"]


# ---- 第 2 轮
@pytest.mark.parametrize("bad", [1, {"name": "XYZ100", "code": "事件", "why": "财报"}, "XYZ100", None, True, 3.5])
def test_veto_not_a_list_is_no_veto(env, monkeypatch, bad):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, bad)
    assert autotrade._vetoed(s, "XYZ100") is None and autotrade._veto_check(s, "XYZ100") == (None, None)
    rec = _enter(s)
    assert "skipped" not in rec and "veto_ignored" not in rec


def test_malformed_run_structure_is_no_veto(env, monkeypatch):  # noqa: F811
    s, _ = env
    for r in ({"rooms": 1}, {"rooms": {"risk": None}}, {"rooms": {"risk": {"synth": "x"}}}, {"rooms": {"risk": {"synth": {}}}}, []):
        monkeypatch.setattr(agents, "last_full_run", lambda settings, within_h=36, r=r: r)
        assert autotrade._vetoed(s, "XYZ100") is None


@pytest.mark.parametrize("code", ["事件", " 事件 ", "事件。", "事件，", "事件类", "Event", "EARNINGS", "earnings call", "价格异常", "anomaly", "Wick", "price",
                                  "缺数据", "data", "missing", "平台", "Platform", "exchange", "LIQUIDITY", "平台风险。"])
def test_code_normalized_blocks(env, monkeypatch, code):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "XYZ100", "code": code, "why": "周一公布财报"}])
    assert autotrade._vetoed(s, "XYZ100") == "周一公布财报"


@pytest.mark.parametrize("code", ["事件|价格异常|缺数据|平台", "事件｜平台", "事件、平台", "事件和平台", "价格异常缺数据", "foo", "样本"])
def test_code_template_or_mixed_ignored(env, monkeypatch, code):  # noqa: F811
    s, _ = env
    _run_with_veto(monkeypatch, [{"name": "XYZ100", "code": code, "why": "周一公布财报"}])
    assert autotrade._vetoed(s, "XYZ100") is None


@pytest.mark.parametrize("word", ["业绩", "美联储", "降息", "加息", "拆股", "停牌", "宕机", "清算", "预言机", "盘口", "偏离", "新闻", "战争", "制裁"])
def test_new_reason_words_block(env, monkeypatch, word):  # noqa: F811
    s, _ = env
    why = f"样本不足30个周末，且 INTC 有{word}风险"
    _run_with_veto(monkeypatch, [{"name": "XYZ100", "code": "事件", "why": why}])
    assert autotrade._vetoed(s, "XYZ100") == why


def test_sample_plus_earnings_blocks(env, monkeypatch):  # noqa: F811
    s, _ = env
    why = "样本不足30个周末，且 INTC 将公布业绩"
    _run_with_veto(monkeypatch, [{"name": "INTC", "code": "事件", "why": why}])
    assert autotrade._vetoed(s, "INTC") == why


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan", "inf"])
@pytest.mark.parametrize("field", ["markPx", "oraclePx", "midPx"])
def test_mark_fact_nan_inf_is_missing(env, field, bad):  # noqa: F811
    s, _ = env
    c = {"midPx": "30030", "markPx": "30000", "oraclePx": "29900", field: bad}
    t = agents._mark_fact("XYZ100", c, 5, s)
    assert "没有标记价，无法排查插针" in t and "nan" not in t.lower() and "inf" not in t.lower()
