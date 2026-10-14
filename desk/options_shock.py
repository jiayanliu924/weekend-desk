"""突发冲击样本（11c，事件类别 ③）：只记账，不下单。

排期事件（CPI / FOMC / 财报）可以在事件前 60 分钟锁定；突发事件（地缘、监管 / ETF / SEC、交易所被盗、宏观意外、关税）
没有"事件前"——新闻到达时它已经发生了。所以这里问的是另一个问题：
  **冲击发生、期权市场刚刚重新定价之后，那一刻的隐含波动，和随后 24 小时真正走出来的波动谁大？**
也就是"突发之后卖恐慌 / 买延续"有没有边际，而不是"事前预测"。

做法（每 every_min 分钟扫一次，工作日也跑）：
1. 抽取器吐出的事件里，重要度 ≥ 阈值、且命中下面任一类别关键词（实体走 desk/relevance.py 的 BTC 表，词走本文件的词表）→ 候选。
2. 同一件事 dedupe_hours 内再出现（event_id 相同 / 原句或标题高度相似 / 同类别已有样本）→ 记为重复，不开新样本。
3. 首次看到 → 建 Sample(kind="shock", event_type="SHOCK:<类别>", event_at=新闻收到时间, lock=现在, end=现在+24h)，
   立刻走 options.job_lock（用当下的期权链算隐含），锁定记录带 event_status（concluded / ongoing / unknown）和新闻详情。
4. 24 小时后 options.job_outcome 照常结算（desk/options_shock.due 负责排期）。
5. 纸面交易员规则（desk/options_paper.shock_gate）：已结束 → 和排期事件一样按历史选边；进行中 / 看不出 → 一律 C。

账本：样本本身在 data/ledger/options.jsonl（和其他样本同一本）；
      data/ledger/options_shock.jsonl 记"看过哪些事件"（kind = seen：新样本 / 重复 / 没数据），保证幂等。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import rawstore, relevance
from .extract import norm
from .headline_shadow import similarity, SIM_THRESHOLD
from .ledger import Ledger
from .options import Sample, job_lock
from .rawstore import now_ns

log = logging.getLogger("options.shock")
UTC = timezone.utc
LEDGER = "options_shock"
KIND_SEEN = "seen"

DEFAULTS = {"enabled": True, "every_min": 5, "lookback_h": 24, "dedupe_hours": 24, "max_news_age_h": 6}

# 类别 → 触发词。entities 与抽取器的 entities 比（大写、去标点）；words 在 标题+原句 的小写文本里找。
# 顺序 = 优先级（一条事件只归一个类别，先命中先得）。
CATEGORIES: dict[str, dict] = {
    "HACK": {"name": "交易所被盗/暴雷",
             "entities": ["COINBASE", "BINANCE", "BYBIT", "KRAKEN", "OKX", "TETHER", "USDT", "STABLECOIN"],
             "words": ["hack", "hacked", "exploit", "breach", "stolen", "drained", "insolven", "halts withdrawals",
                       "halt withdrawals", "depeg", "被盗", "黑客", "暴雷", "脱锚"],
             "need_both": False},
    "REG": {"name": "监管/ETF/SEC",
            "entities": ["SEC", "CFTC", "ETF", "IBIT", "BITCOIN", "BTC", "CRYPTO", "CRYPTOCURRENCY"],
            "words": ["etf", "lawsuit", "sues", "charges", "approv", "reject", "ban ", "bans", "banned", "regulat",
                      "stablecoin bill", "executive order", "bitcoin reserve", "监管", "批准", "驳回", "禁止", "起诉"],
            "need_both": True},
    "TARIFF": {"name": "关税/贸易战",
               "entities": ["TARIFF", "TRADE WAR", "CHINA", "EXPORT"],
               "words": ["tariff", "tariffs", "trade war", "export control", "export ban", "关税", "贸易战", "出口管制"],
               "need_both": False},
    "GEO": {"name": "地缘冲突",
            "entities": ["IRAN", "ISRAEL", "RUSSIA", "UKRAINE", "TAIWAN", "NORTH KOREA", "MIDDLE EAST", "HORMUZ",
                         "SAUDI", "HOUTHI", "GAZA", "LEBANON", "NATO"],
            "words": ["attack", "attacks", "strike", "strikes", "missile", "missiles", "drone", "invasion", "invade",
                      "war ", "escalat", "ceasefire", "cease-fire", "nuclear", "blockade", "袭击", "空袭", "导弹", "入侵",
                      "战争", "停火", "升级", "封锁"],
            "need_both": False},
    "MACRO": {"name": "宏观意外",
              "entities": ["FED", "FOMC", "POWELL", "TREASURY", "ECB", "BOJ", "PBOC"],
              "words": ["emergency", "surprise", "unexpected", "unscheduled", "intermeeting", "inter-meeting",
                        "downgrade", "default", "shutdown", "resign", "fired", "紧急", "意外", "突然", "下调评级", "违约", "辞职"],
              "need_both": True},
}


def cfg(settings) -> dict:
    return {**DEFAULTS, **((settings["options"].get("shock") or {}))}


def _hit_entities(ents: list[str], kws: list[str]) -> bool:
    for e in ents:
        n = relevance._norm_entity(e)
        if not n:
            continue
        for k in kws:
            if n == k or (len(k) >= 3 and k in n.split()) or (len(k) >= 4 and k in n):
                return True
    return False


def categorize(ev: dict) -> str | None:
    """一条抽取事件属于哪个突发类别；不属于任何一类 → None。
    HACK / TARIFF / GEO：实体或词命中其一即可；REG / MACRO：实体和词都要命中（避免每条普通 Fed 新闻都算"宏观意外"）。
    另外整体要和 BTC 有关（relevance.BTC 表）或类别自带的实体命中。"""
    ents = [str(x) for x in (ev.get("entities") or [])]
    text = norm(f"{ev.get('title') or ''} {ev.get('evidence_span') or ''}")
    for cat, spec in CATEGORIES.items():
        eh = _hit_entities(ents, spec["entities"])
        wh = any(w in text for w in spec["words"])
        ok = (eh and wh) if spec["need_both"] else (eh or wh)
        if ok:
            return cat
    return None


def candidate_events(settings, a_ns: int, b_ns: int) -> list[dict]:
    """收到时间在 [a, b) 内、证据句在原文里、重要度过线、能归到突发类别的事件。"""
    pv = settings["llm"]["prompt_version"]
    thr = settings["model"]["materiality_threshold"]
    if not rawstore.has_stream(settings.data, "events"):
        return []
    rows = rawstore.query(settings.data, "events", select="payload", sql_where=f"source = '{pv}'")
    out = []
    for (p,) in rows:
        rec = json.loads(p)
        t = rec["news_received_at"]
        if not (a_ns <= t < b_ns):
            continue
        for idx, e in enumerate(rec.get("events", [])):
            if not e.get("evidence_ok") or int(e.get("materiality", 0)) < thr:
                continue
            ev = {**e, "entry_id": f"{rec['news_key']}#{idx}", "news_key": rec["news_key"], "news_received_at": t,
                  "extracted_at": rec.get("extracted_at"), "title": rec.get("title")}
            cat = categorize(ev)
            if cat is None:
                continue
            ev["category"] = cat
            out.append(ev)
    out.sort(key=lambda e: e["news_received_at"])
    return out


def _repeat_of(ev: dict, seen: list[dict], dedupe_ns: int) -> str | None:
    """dedupe_hours 内有没有同一件事：event_id 相同、原句/标题高度相似、或同类别已开过样本。返回更早那条的 sid。"""
    for r in seen:
        if r.get("repeat_of") or not r.get("sid"):
            continue
        gap = ev["news_received_at"] - r["news_received_at"]
        if gap < 0 or gap > dedupe_ns:
            continue
        if ev.get("event_id") and r.get("event_id") == ev["event_id"]:
            return r["sid"]
        if similarity(ev.get("evidence_span"), r.get("evidence_span")) >= SIM_THRESHOLD:
            return r["sid"]
        if similarity(ev.get("title"), r.get("title")) >= SIM_THRESHOLD:
            return r["sid"]
        if r.get("category") == ev["category"]:
            return r["sid"]
    return None


def make_sample(ev: dict, now_ns_: int, window_hours: float) -> Sample:
    now = datetime.fromtimestamp(now_ns_ / 1e9, tz=UTC)
    sid = f"SHOCK-{ev['category']}-{now:%Y%m%d-%H%M}"
    return Sample(sid, "shock", f"SHOCK:{ev['category']}", datetime.fromtimestamp(ev["news_received_at"] / 1e9, tz=UTC),
                  now, now + timedelta(hours=window_hours))


def run(settings, clock=now_ns, extractor=None) -> dict:
    """扫一遍：新突发事件 → 立刻锁定一个样本；重复的记一笔 seen。幂等。"""
    c = cfg(settings)
    if not c["enabled"] or not settings["options"]["enabled"]:
        return {"samples": 0, "repeats": 0}
    now = clock()
    led = Ledger(settings.data, LEDGER)
    seen = [r for r in led.all() if r["kind"] == KIND_SEEN]
    have = {r["entry_id"] for r in seen}
    dedupe_ns = int(c["dedupe_hours"] * 3600e9)
    max_age_ns = int(c["max_news_age_h"] * 3600e9)
    n_new = n_rep = 0
    for ev in candidate_events(settings, now - int(c["lookback_h"] * 3600e9), now):
        if ev["entry_id"] in have:
            continue
        body = {"entry_id": ev["entry_id"], "news_key": ev["news_key"], "event_id": ev.get("event_id"),
                "category": ev["category"], "news_received_at": ev["news_received_at"], "title": ev.get("title"),
                "evidence_span": ev.get("evidence_span"), "event_status": ev.get("event_status") or "unknown",
                "materiality": ev.get("materiality"), "entities": ev.get("entities") or [], "sid": None, "repeat_of": None}
        rep = _repeat_of(ev, seen, dedupe_ns)
        if rep:
            body["repeat_of"] = rep
            n_rep += 1
        elif now - ev["news_received_at"] > max_age_ns:
            # 新闻太旧（比如服务器停了几小时）：锁定时刻的隐含已经不是"刚重新定价"的隐含，不当样本
            body["problem"] = f"新闻已过 {c['max_news_age_h']} 小时，不开样本"
        else:
            smp = make_sample(ev, now, settings["options"]["window_hours"])
            extra = {"event_status": body["event_status"],
                     "shock": {k: body[k] for k in ("entry_id", "news_key", "event_id", "category", "news_received_at",
                                                    "title", "evidence_span", "materiality", "entities")}
                     | {"category_name": CATEGORIES[ev["category"]]["name"],
                        "delay_min": (now - ev["news_received_at"]) / 60e9}}
            rec = job_lock(settings, smp, extractor=extractor, clock=clock, extra=extra, run_extract=False)
            body["sid"] = smp.sid
            body["lock_ok"] = bool(rec.get("ok"))
            n_new += 1
        seen.append(led.append(KIND_SEEN, _day(ev["news_received_at"]), body))
        have.add(ev["entry_id"])
    return {"samples": n_new, "repeats": n_rep}


def _day(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns / 1e9, tz=UTC).strftime("%Y-%m-%d")


def sample_from_lock(rec: dict) -> Sample:
    return Sample(rec["weekend"], "shock", rec["event_type"],
                  datetime.fromisoformat(rec["event_at"]) if rec.get("event_at") else None,
                  datetime.fromisoformat(rec["window_start"]), datetime.fromisoformat(rec["window_end"]))


def shock_samples(settings) -> list[Sample]:
    led = Ledger(settings.data, "options")
    return [sample_from_lock(r) for r in led.all() if r["kind"] == "lock" and r.get("sample_kind") == "shock"]


def due(now: datetime, settings, done: set) -> list[tuple[str, Sample]]:
    """突发样本的结算排期（锁定是 run() 当场做的，这里只排 outcome）。key 和 options.due 同一格式。"""
    if not settings["options"]["enabled"]:
        return []
    out = []
    for s_ in shock_samples(settings):
        k = f"opt:{s_.sid}:outcome"
        if s_.end + timedelta(minutes=5) <= now < s_.end + timedelta(hours=6) and k not in done:
            out.append((k, s_))
    return out


def rows(settings) -> list[dict]:
    """网页"突发事件样本"表：每个突发样本一行（锁定了就有，结算后补结果）。最新在前。"""
    led = Ledger(settings.data, "options")
    by: dict[str, dict] = {}
    for r in led.all():
        if r["kind"] in ("lock", "outcome") and r["weekend"].startswith("SHOCK-"):
            by.setdefault(r["weekend"], {})[r["kind"]] = r
    out = []
    for sid, w in sorted(by.items(), reverse=True):
        lk, o = w.get("lock"), w.get("outcome") or {}
        if not lk:
            continue
        sh = lk.get("shock") or {}
        pp = o.get("paper") or {}
        out.append({"sid": sid, "category": lk["event_type"], "category_name": sh.get("category_name"),
                    "event_status": lk.get("event_status") or "unknown", "title": sh.get("title"),
                    "news_received_at": sh.get("news_received_at"), "delay_min": sh.get("delay_min"),
                    "lock_ok": bool(lk.get("ok")), "implied_vol": lk.get("implied_vol"), "dvol": lk.get("dvol"),
                    "realized_vol": o.get("realized_vol"), "ratio": o.get("ratio"), "abs_move_bps": o.get("abs_move_bps"),
                    "chosen": pp.get("chosen") or (lk.get("paper") or {}).get("chosen"),
                    "reason": (lk.get("paper") or {}).get("reason"), "pnl": pp.get("pnl"), "chosen_pnl": pp.get("chosen_pnl"),
                    "settled": bool(o)})
    return out
