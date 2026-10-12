"""新闻 ↔ 合约 相关性表：哪些实体/关键词算"跟这个合约有关"，以及原文倾向对该合约意味着做多还是做空。

两处用到：
1. 头条动量影子线（desk/headline_shadow.py）：抽取器吐出的事件里实体命中 BRENTOIL 的关键词 → 记一笔影子单。
2. 之后的"按合约算新闻相关度"特征（任务 #3）：每个合约只数跟自己有关的重要事件，而不是全市场一锅端。

设计成 合约名 → 关键词列表，关键词与抽取器输出的 entities（已大写）逐个比对；词表只放"原文里会直接出现的名字"。
"""
from __future__ import annotations

import re

# 合约名（config.toml [[market.instruments]].name）→ 实体关键词（大写）。
# 实体匹配规则：事件 entities 里任一项，去掉非字母数字后，等于关键词或包含关键词（如 "SAUDI ARABIA" 命中 "SAUDI"）。
COIN_KEYWORDS: dict[str, list[str]] = {
    "BRENTOIL": ["OIL", "BRENT", "CRUDE", "WTI", "OPEC", "SAUDI", "IRAN", "HORMUZ", "HOUTHI", "ARAMCO",
                 "ISRAEL", "RED SEA", "YEMEN", "IRAQ", "KUWAIT", "UAE", "QATAR", "RUSSIA", "VENEZUELA",
                 "TANKER", "PIPELINE", "REFINERY", "ENERGY"],
    "XYZ100": ["NASDAQ", "NDX", "QQQ", "US STOCKS", "S&P", "FED", "FOMC", "TARIFF", "TREASURY"],
    "SP500": ["S&P", "SPX", "SP500", "US STOCKS", "FED", "FOMC", "TARIFF", "TREASURY", "NASDAQ"],
    "NVDA": ["NVDA", "NVIDIA"],
    "TSLA": ["TSLA", "TESLA"],
    "INTC": ["INTC", "INTEL"],
    "SMSN": ["SMSN", "SAMSUNG", "KOSPI", "KOREA"],
}

# 原文倾向 → 该合约的方向。油价和"坏消息"是反的：袭击/封锁/制裁（原文"负面"）→ 供应冲击 → 油价涨 → 做多。
# 股票/指数则是常规：正面 → 做多。
_DIRECTION_SIGN: dict[str, dict[str, int]] = {
    "BRENTOIL": {"负面": +1, "正面": -1},
}
_DEFAULT_SIGN = {"正面": +1, "负面": -1}

# 原文倾向为 中性/不明 时的关键词兜底（只对油）：供应冲击词 → 多；降温词 → 空。
OIL_BULL_WORDS = ["attack", "strike", "strikes", "missile", "drone", "explosion", "blast", "blockade", "closure",
                  "close the strait", "halt", "halts", "suspend", "disrupt", "sanction", "sanctions", "seize",
                  "tanker hit", "output cut", "cuts output", "production cut", "war", "escalat", "retaliat",
                  "袭击", "空袭", "导弹", "无人机", "封锁", "制裁", "减产", "中断", "升级", "报复"]
OIL_BEAR_WORDS = ["ceasefire", "cease-fire", "truce", "de-escalat", "deescalat", "peace talks", "peace deal",
                  "agreement", "resume", "resumes", "restart", "reopen", "output hike", "raise output",
                  "raises output", "increase production", "boost production", "lift sanctions", "ease sanctions",
                  "停火", "休战", "降温", "和谈", "协议", "恢复", "增产", "解除制裁"]

_NONALNUM = re.compile(r"[^A-Z0-9&]+")


def _norm_entity(x: str) -> str:
    return _NONALNUM.sub(" ", str(x).upper()).strip()


def is_relevant(instrument: str, entities: list[str]) -> bool:
    """事件实体里有没有命中该合约的关键词。"""
    kws = COIN_KEYWORDS.get(instrument, [instrument.upper()])
    ents = [_norm_entity(e) for e in (entities or [])]
    for e in ents:
        if not e:
            continue
        for k in kws:
            if e == k or (len(k) >= 3 and k in e.split()) or (len(k) >= 4 and k in e):
                return True
    return False


def relevant_instruments(entities: list[str], instruments: list[str] | None = None) -> list[str]:
    """给一条事件，返回它跟哪些合约有关。"""
    names = instruments or list(COIN_KEYWORDS)
    return [n for n in names if is_relevant(n, entities)]


def implied_side(instrument: str, direction_in_text: str | None, text: str = "") -> int:
    """原文倾向 → 做多(+1)/做空(-1)/说不清(0)。中性/不明时对油价用关键词兜底。"""
    sign = _DIRECTION_SIGN.get(instrument, _DEFAULT_SIGN)
    d = (direction_in_text or "").strip()
    if d in sign:
        return sign[d]
    if instrument == "BRENTOIL" and text:
        t = text.lower()
        bull = sum(1 for w in OIL_BULL_WORDS if w in t)
        bear = sum(1 for w in OIL_BEAR_WORDS if w in t)
        if bull > bear:
            return +1
        if bear > bull:
            return -1
    return 0
