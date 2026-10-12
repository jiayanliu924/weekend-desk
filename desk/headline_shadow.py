"""头条动量影子线（只记账，永远不下单）。

问题：油价相关的大新闻（袭击油田、霍尔木兹、停火……）一出来，顺着新闻方向追，到底赚不赚钱？
已结束的一次性事件（concluded）和仍在进行的事件（ongoing），哪种会反转、哪种会延续？

做法（工作日也跑，每几分钟扫一次）：
1. 抽取器吐出的事件里，重要度 ≥2 且实体命中 BRENTOIL 关键词（desk/relevance.py）的 → 候选。
2. 进场价 = 新闻"我方收到时间" + delay_sec 秒后的第一个链上中间价（给读新闻和下单留出两分钟）。
3. 方向 = 原文倾向对油价的含义：袭击/供应冲击（负面）→ 多；停火/降温（正面）→ 空；中性/不明按关键词兜底，兜不出来记 side=0 不算账。
4. +1h / +4h / +24h 各对一次账，用 evaluate._sim_pnl 的同一口径（挂单进、吃单出，另给一个全额手续费数）。
5. 同一事件 6 小时内再出现（event_id 相同或原句高度相似）→ 记为"重复"，成绩单只算"新"的那一笔。

账本：data/ledger/headline_shadow.jsonl，kind = headline_shadow（进场）/ headline_mark（对账）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher

from . import evaluate, features, model, rawstore, relevance
from .clock import current_or_next_weekend
from .extract import norm
from .ledger import Ledger
from .rawstore import now_ns

log = logging.getLogger("headline_shadow")

LEDGER = "headline_shadow"
KIND_ENTRY = "headline_shadow"
KIND_MARK = "headline_mark"
SIM_THRESHOLD = 0.6          # 原句相似度超过它 → 同一件事的重复报道
CTX_WINDOW_MIN = 10.0        # 进场/对账时刻之后多少分钟内必须有链上数据，否则这一格记为缺数据

DEFAULTS = {"enabled": True, "instrument": "BRENTOIL", "delay_sec": 120, "horizons_h": [1, 4, 24],
            "dedupe_hours": 6, "lookback_h": 48, "every_min": 5}


def cfg(settings) -> dict:
    return {**DEFAULTS, **(settings.raw.get("headline_shadow") or {})}


def similarity(a: str, b: str) -> float:
    a, b = norm(a or ""), norm(b or "")
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def candidate_events(settings, a_ns: int, b_ns: int) -> list[dict]:
    """收到时间在 [a, b) 内、证据句在原文里、重要度过线、实体命中该合约的事件。"""
    c = cfg(settings)
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
            if not relevance.is_relevant(c["instrument"], e.get("entities") or []):
                continue
            out.append({**e, "entry_id": f"{rec['news_key']}#{idx}", "news_key": rec["news_key"],
                        "news_received_at": t, "extracted_at": rec.get("extracted_at"), "title": rec.get("title")})
    out.sort(key=lambda e: e["news_received_at"])
    return out


def _is_weekend(settings, inst: dict, t_ns: int) -> bool:
    dt = datetime.fromtimestamp(t_ns / 1e9, tz=timezone.utc)
    w = current_or_next_weekend(dt, settings, inst)
    return w.close <= dt < w.resume


def _repeat_of(ev: dict, entries: list[dict], dedupe_ns: int) -> str | None:
    """6 小时内有没有同一件事的更早记录：event_id 相同，或原句/标题高度相似。"""
    for r in entries:
        gap = ev["news_received_at"] - r["news_received_at"]
        if gap < 0 or gap > dedupe_ns or r.get("instrument") != ev["instrument"]:
            continue
        if ev.get("event_id") and r.get("event_id") == ev["event_id"]:
            return r["entry_id"]
        if similarity(ev.get("evidence_span"), r.get("evidence_span")) >= SIM_THRESHOLD:
            return r["entry_id"]
        if similarity(ev.get("title"), r.get("title")) >= SIM_THRESHOLD:
            return r["entry_id"]
    return None


def run(settings, clock=now_ns) -> dict:
    """扫一遍：新候选事件记进场；到期的进场记对账。幂等——重复调用不会重复记账。"""
    c = cfg(settings)
    if not c["enabled"]:
        return {"entries": 0, "marks": 0}
    inst = settings.inst(c["instrument"])
    if not inst:
        return {"entries": 0, "marks": 0, "problem": f"配置里没有合约 {c['instrument']}"}
    coin = inst["coin"]
    now = clock()
    led = Ledger(settings.data, LEDGER)
    recs = led.all()
    entries = [r for r in recs if r["kind"] == KIND_ENTRY]
    have = {r["entry_id"] for r in entries}
    marked = {(r["entry_id"], r["horizon_h"]) for r in recs if r["kind"] == KIND_MARK}
    delay_ns = int(c["delay_sec"] * 1e9)
    dedupe_ns = int(c["dedupe_hours"] * 3600e9)
    notional = settings["risk"]["paper_notional_usd"]

    n_entry = 0
    for ev in candidate_events(settings, now - int(c["lookback_h"] * 3600e9), now - delay_ns):
        if ev["entry_id"] in have:
            continue
        t_entry = ev["news_received_at"] + delay_ns
        ctx = features.first_ctx_after(settings, coin, t_entry, horizon_min=CTX_WINDOW_MIN)
        if not ctx:
            if now > t_entry + int(CTX_WINDOW_MIN * 60e9):
                log.info("headline_shadow: no ctx within %s min after %s, skip %s", CTX_WINDOW_MIN, t_entry, ev["entry_id"])
                have.add(ev["entry_id"])
                body = _entry_body(settings, inst, ev, side=0, entry_px=None, entry_at=t_entry, ctx_at=None,
                                   repeat_of=_repeat_of({**ev, "instrument": inst["name"]}, entries, dedupe_ns),
                                   notional=notional, problem="进场时刻后 10 分钟内没有链上数据")
                entries.append(led.append(KIND_ENTRY, _day(ev["news_received_at"]), body))
                n_entry += 1
            continue
        text = f"{ev.get('title') or ''} {ev.get('evidence_span') or ''}"
        side = relevance.implied_side(inst["name"], ev.get("direction_in_text"), text)
        entry_px = float(ctx[1].get("midPx") or ctx[1]["markPx"])
        body = _entry_body(settings, inst, ev, side=side, entry_px=entry_px, entry_at=t_entry, ctx_at=ctx[0],
                           repeat_of=_repeat_of({**ev, "instrument": inst["name"]}, entries, dedupe_ns),
                           notional=notional)
        rec = led.append(KIND_ENTRY, _day(ev["news_received_at"]), body)
        entries.append(rec)
        have.add(ev["entry_id"])
        n_entry += 1

    n_mark = 0
    costs = settings["costs"]
    for r in entries:
        if not r.get("side") or r.get("entry_px") is None:
            continue
        for h in c["horizons_h"]:
            if (r["entry_id"], h) in marked:
                continue
            t_h = r["entry_at"] + int(h * 3600e9)
            if t_h > now:
                continue
            ctx = features.first_ctx_after(settings, coin, t_h, horizon_min=CTX_WINDOW_MIN)
            if not ctx:
                if now > t_h + int(CTX_WINDOW_MIN * 60e9):
                    led.append(KIND_MARK, r["weekend"], {"entry_id": r["entry_id"], "horizon_h": h, "ok": False,
                                                         "problem": "对账时刻后 10 分钟内没有链上数据"})
                    marked.add((r["entry_id"], h))
                    n_mark += 1
                continue
            mark_px = float(ctx[1].get("midPx") or ctx[1]["markPx"])
            ret_bps = (mark_px / r["entry_px"] - 1) * 1e4
            p = evaluate._sim_pnl(r["side"], r["notional_usd"], r["entry_px"], mark_px, {"dec_mid": r["entry_px"]},
                                  r.get("fees") or {}, costs, y=ret_bps, dev=0.0)
            led.append(KIND_MARK, r["weekend"], {"entry_id": r["entry_id"], "horizon_h": h, "ok": True,
                                                 "mark_px": mark_px, "mark_at": t_h, "mark_received_at": ctx[0],
                                                 "ret_bps": ret_bps, **p})
            marked.add((r["entry_id"], h))
            n_mark += 1
    return {"entries": n_entry, "marks": n_mark}


def _day(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")


def _entry_body(settings, inst: dict, ev: dict, *, side: int, entry_px, entry_at: int, ctx_at, repeat_of,
                notional: float, problem: str | None = None) -> dict:
    fees = model.fee_bps(settings, entry_at, inst["coin"]) if entry_px is not None else {}
    body = {"entry_id": ev["entry_id"], "news_key": ev["news_key"], "event_id": ev.get("event_id"),
            "instrument": inst["name"], "coin": inst["coin"], "side": side, "entry_px": entry_px,
            "entry_at": entry_at, "entry_received_at": ctx_at, "news_received_at": ev["news_received_at"],
            "delay_sec": cfg(settings)["delay_sec"], "title": ev.get("title"), "evidence_span": ev.get("evidence_span"),
            "direction_in_text": ev.get("direction_in_text"), "event_type": ev.get("event_type"),
            "materiality": ev.get("materiality"), "event_status": ev.get("event_status") or "unknown",
            "entities": ev.get("entities") or [], "is_new": repeat_of is None, "repeat_of": repeat_of,
            "weekend_news": _is_weekend(settings, inst, ev["news_received_at"]),
            "notional_usd": notional if side else 0.0, "fees": fees, "mode": "shadow"}
    if problem:
        body["problem"] = problem
    if side == 0 and not problem:
        body["problem"] = "原文倾向看不出对油价的方向，不记账"
    return body


def _hstats(marks: list[dict]) -> dict:
    ok = [m for m in marks if m.get("ok")]
    n = len(ok)
    if not n:
        return {"n": 0, "hit_rate": None, "mean_pnl_net_bps": None, "mean_pnl_fullfee_bps": None,
                "sum_pnl_fullfee_usd": 0.0}
    hits = [m["direction_hit"] for m in ok if m.get("direction_hit") is not None]
    return {"n": n, "hit_rate": (sum(1 for h in hits if h) / len(hits)) if hits else None,
            "mean_pnl_net_bps": sum(m["pnl_net_bps"] for m in ok) / n,
            "mean_pnl_fullfee_bps": sum(m["pnl_net_fullfee_bps"] for m in ok) / n,
            "sum_pnl_fullfee_usd": sum(m["pnl_net_fullfee_usd"] for m in ok)}


def headline_scorecard(settings, records: list[dict] | None = None) -> dict:
    """成绩单：样本数、各时间点的命中率与平均盈亏（全额手续费），再按 已结束/进行中 分开看。只算"新"的那一笔。"""
    c = cfg(settings)
    recs = Ledger(settings.data, LEDGER).all() if records is None else records
    entries = [r for r in recs if r["kind"] == KIND_ENTRY]
    marks = [r for r in recs if r["kind"] == KIND_MARK]
    by_entry: dict[str, dict[int, dict]] = {}
    for m in marks:
        by_entry.setdefault(m["entry_id"], {})[m["horizon_h"]] = m
    sided = [e for e in entries if e.get("side")]
    new = [e for e in sided if e.get("is_new")]
    res = {"instrument": c["instrument"], "n_entries": len(entries), "n_sided": len(sided),
           "n_new": len(new), "n_repeat": len(sided) - len(new),
           "n_weekend": sum(1 for e in new if e.get("weekend_news")),
           "n_long": sum(1 for e in new if e["side"] > 0), "n_short": sum(1 for e in new if e["side"] < 0),
           "horizons_h": list(c["horizons_h"]), "by_horizon": {}, "by_status": {}}
    for h in c["horizons_h"]:
        res["by_horizon"][h] = _hstats([by_entry.get(e["entry_id"], {}).get(h) for e in new if by_entry.get(e["entry_id"], {}).get(h)])
    for st in ("concluded", "ongoing", "unknown"):
        grp = [e for e in new if (e.get("event_status") or "unknown") == st]
        res["by_status"][st] = {"n": len(grp), "by_horizon": {
            h: _hstats([by_entry.get(e["entry_id"], {}).get(h) for e in grp if by_entry.get(e["entry_id"], {}).get(h)])
            for h in c["horizons_h"]}}
    res["verdict"] = _verdict(res)
    return res


def _verdict(res: dict) -> str:
    n = res["n_new"]
    if not n:
        return "还没有记到油价相关的重要新闻；抽取器一吐出来就会自动记账。"
    parts = []
    for h, s in res["by_horizon"].items():
        if s["n"]:
            hit = f"命中 {s['hit_rate'] * 100:.0f}%" if s["hit_rate"] is not None else ""
            parts.append(f"+{h}h：{s['n']} 笔{('，' + hit) if hit else ''}，全额手续费平均 {s['mean_pnl_fullfee_bps']:+.0f} bps")
    body = f"已记 {n} 条新头条（另 {res['n_repeat']} 条重复报道不计）。" + ("；".join(parts) + "。" if parts else "")
    con, ong = res["by_status"]["concluded"]["n"], res["by_status"]["ongoing"]["n"]
    body += f" 已结束 {con} / 进行中 {ong} / 看不出 {res['n_new'] - con - ong}。"
    if n < 20:
        body += " 不到 20 条样本，只算'值得继续看'，不动真钱。"
    else:
        h24 = res["by_horizon"].get(24) or {}
        body += ("24 小时全额手续费为正，可以考虑设计真钱版本（仍先小仓）。" if (h24.get("mean_pnl_fullfee_bps") or 0) > 0
                 else "24 小时全额手续费不赚钱，追头条不是边际，别上真钱。")
    return body
