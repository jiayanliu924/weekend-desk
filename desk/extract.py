"""大语言模型抽取（备忘 7.3）。唯一任务：把文本变成事件表，不让模型做任何价格判断。

7.4 第二条"只认原文"：每个事件必须附原句；原句不在原文里的，记为抽取错误，不进特征。
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone

from . import rawstore
from .rawstore import RawWriter, now_ns

log = logging.getLogger("extract")

# 备忘 7.3 系统指令 v0.1（原文照录）
SYSTEM_PROMPT_V01 = """你是一个信息抽取器。只根据下面提供的原文抽取事件，不得使用你自己掌握的任何背景知识，不得推测市场反应。如果原文没有明确信息，输出空列表。

输入：一段文本、来源名称、发布时间（UTC）、我方收到时间（UTC）
关注对象：纳斯达克 100 成分股、美国宏观政策、地缘冲突、主要芯片与 AI 公司

对每个事件输出：
{
  "event_id": "源ID-序号",
  "entities": ["NVDA"],            // 只填原文明确提到的公司或指数
  "event_type": "财报指引|监管|并购|宏观|地缘|产品|高管|其他",
  "direction_in_text": "正面|负面|中性|不明",   // 原文自身的表述倾向
  "materiality": 0-3,               // 0 无关；3 足以改变公司或指数基本面
  "is_new_information": true/false, // 原文是否声明这是首次披露
  "evidence_span": "原文中支持判断的原句，逐字复制",
  "published_at": "...", "received_at": "...",
  "prompt_version": "v0.1", "model_version": "..."
}

只输出一个 JSON 数组，不要输出任何其他文字。"""

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def clean(text: str) -> str:
    return WS.sub(" ", html.unescape(TAG.sub(" ", text or ""))).strip()


def norm(s: str) -> str:
    return WS.sub(" ", (s or "").replace("“", '"').replace("”", '"')
                  .replace("‘", "'").replace("’", "'")).strip().lower()


def iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)) and ts > 1e12:
        ts = ts / 1e9
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build_input(item: dict, received_ns: int, key: str) -> tuple[str, str]:
    text = clean(item.get("title", "")) + "\n" + clean(item.get("summary", ""))
    user = (f"源ID：{key}\n来源名称：{item.get('feed')}\n发布时间（UTC）：{iso(item.get('published_at'))}\n"
            f"我方收到时间（UTC）：{iso(received_ns)}\n原文：\n{text}")
    return text, user


def parse_events(raw_text: str) -> list[dict]:
    m = re.search(r"\[.*\]", raw_text, re.S)
    if not m:
        return []
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return []
    return [e for e in out if isinstance(e, dict)]


def validate(events: list[dict], text: str, top10: list[str]) -> list[dict]:
    t = norm(text)
    good = []
    for e in events:
        span = norm(str(e.get("evidence_span", "")))
        e["evidence_ok"] = bool(span) and span in t
        try:
            e["materiality"] = max(0, min(3, int(e.get("materiality", 0))))
        except (TypeError, ValueError):
            e["materiality"] = 0
        ents = [str(x).upper() for x in (e.get("entities") or [])]
        e["entities"] = ents
        e["touches_top10"] = any(x in top10 for x in ents)
        good.append(e)
    return good


class AnthropicExtractor:
    def __init__(self, model: str):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model

    def __call__(self, system: str, user: str) -> str:
        r = self.client.messages.create(model=self.model, max_tokens=1500, temperature=0,
                                        system=system, messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def pending(settings, start_ns: int, end_ns: int) -> list[tuple]:
    pv = settings["llm"]["prompt_version"]
    done = {k for (k,) in rawstore.query(settings.data, "events", select="key",
                                         sql_where=f"source = '{pv}'")} if rawstore.has_stream(settings.data, "events") else set()
    rows = rawstore.query(settings.data, "news", select="received_at, key, payload",
                          sql_where=f"received_at >= {start_ns} AND received_at < {end_ns}")
    return [r for r in rows if r[1] not in done]


def run_extraction(settings, start_ns: int, end_ns: int, extractor=None, clock=now_ns) -> int:
    cfg = settings
    pv, model = cfg["llm"]["prompt_version"], cfg["llm"]["model"]
    extractor = extractor or AnthropicExtractor(model)
    w = RawWriter(cfg.data, "events")
    items = pending(cfg, start_ns, end_ns)[: cfg["llm"]["max_items_per_run"]]
    n = 0
    for received_ns, key, payload in items:
        item = json.loads(payload)
        text, user = build_input(item, received_ns, key)
        try:
            out = extractor(SYSTEM_PROMPT_V01, user)
        except Exception as e:  # noqa: BLE001
            log.warning("extract failed %s: %s", key, e)
            continue
        events = validate(parse_events(out), text, cfg["market"]["top10"])
        rec = {"news_key": key, "news_received_at": received_ns,
               "published_at": item.get("published_at"), "title": item.get("title"),
               "extracted_at": clock(), "prompt_version": pv, "model_version": model,
               "events": events}
        w.add(pv, key, rec)
        n += 1
    w.flush()
    return n


def load_events(settings, decision_ns: int, start_ns: int) -> list[dict]:
    """Events usable at decision time: news received before decision AND extraction finished before decision."""
    pv = settings["llm"]["prompt_version"]
    rows = rawstore.query(settings.data, "events", select="payload",
                          sql_where=f"source = '{pv}'") if rawstore.has_stream(settings.data, "events") else []
    out = []
    for (p,) in rows:
        rec = json.loads(p)
        if not (start_ns <= rec["news_received_at"] < decision_ns and rec["extracted_at"] < decision_ns):
            continue
        for e in rec["events"]:
            if e.get("evidence_ok"):
                out.append({**e, "_received_at": rec["news_received_at"], "_title": rec.get("title")})
    return out
