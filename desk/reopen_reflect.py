"""重开策略每日反思：把当天事实压成一小份 JSON，让 Sonnet 级模型用大白话写五段话；
没有 key / 超预算 / 出错 / 解析失败 → 用代码按事实直接写一份（也是大白话）。只读账本，不碰任何下单。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import agents
from .ledger import Ledger

log = logging.getLogger(__name__)
PT = ZoneInfo("America/Los_Angeles")
TARGET_FILLS = 20
MAX_TOKENS = 1500
KEYS = ("一句话结论", "今天为什么赚/亏", "机制还成不成立", "改进方向", "明天看什么")

SYSTEM = """你是一个交易复盘员，用最朴素的中文（大白话）给一个不懂金融术语的人写每日复盘。规则：
1 全程中文，不夹英文单词；币的代号和数字、$、bps 可以直接写。出现任何术语，紧跟括号用一句话解释并举例，例如：过冲（价格被推过头的部分）。
2 要诚实：证据说明机制弱，就直说"现在看不出它管用"，不许打气，不许承诺会赚钱；样本少就说样本少，不下定论。
3 只根据给你的事实写，不编数字，没有的数据就说没有。
策略背景：外部市场（比如美股）关门时，链上合约价格被规则压着；重开时，参考价会分几步追上真实价格。我们只在参考价稳定之后、链上价格比参考价多偏出超过门槛的时候挂单，接那些被迫成交的单。门槛 = 往返手续费 + 3bps，约 9bps（bps：万分之一，9bps 就是 0.09%）。每笔不超过 $500，1 倍杠杆。当日止损 $15，全账户亏到 $20 就全停。跑满 20 笔再看结果。
只输出一个 JSON，不要别的字：
{"一句话结论": "不超过40字", "今天为什么赚/亏": "不超过150字", "机制还成不成立": "不超过150字，要说清样本够不够", "改进方向": ["最多4条，每条不超过50字"], "明天看什么": "不超过80字"}"""


# ------------------------------------------------------------------ 事实
def _pt_date(ms) -> date:
    return datetime.fromtimestamp(ms / 1000, PT).date()


def build_facts(settings, day) -> dict:
    """day：加州日期（date 或 'YYYY-MM-DD'）。读 data/ledger/reopen.jsonl，返回聚合后的小字典。"""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    recs = Ledger(settings.data, "reopen").all()
    pos: dict[str, dict] = {}
    run, skipped, blocks = set(), [], 0
    overs: dict[str, float] = {}
    thr: dict[str, float] = {}
    for r in recs:
        k, ms = r.get("kind"), r.get("t_ms")
        if k == "open":
            pos[r["pos_id"]] = {"coin": r["coin"], "open_ms": ms, "fills": [], "closed": None, "exit": None}
        elif k == "fill" and r.get("pos_id") in pos:
            pos[r["pos_id"]]["fills"].append(r)
        elif k == "exit_reason" and r.get("pos_id") in pos:
            pos[r["pos_id"]]["exit"] = pos[r["pos_id"]]["exit"] or r.get("reason")
        elif k == "closed" and r.get("pos_id") in pos:
            pos[r["pos_id"]]["closed"] = r
        elif k == "decision" and ms and _pt_date(ms) == day:
            w, ev, coin = r.get("what"), r.get("event"), r.get("coin") or r.get("name")
            if w in ("t0_detected", "settled", "window_closed", "entry", "would_enter", "blocked"):
                run.add(ev)
            if w in ("skipped_selfcheck", "no_t0"):
                skipped.append({"事件": ev, "原因": r.get("why") or r.get("note") or w})
            if w in ("blocked", "blocked_retry"):
                blocks += 1
            if w == "window_closed" and r.get("max_overshoot_bps") is not None:
                overs[coin] = max(overs.get(coin, -1e9), float(r["max_overshoot_bps"]))
            if r.get("overshoot_bps") is not None:
                overs[coin] = max(overs.get(coin, -1e9), float(r["overshoot_bps"]))
            if r.get("eff_k_bps") is not None:
                thr[coin] = float(r["eff_k_bps"])
    def summarize(p):
        f = p["fills"]
        ent = [x for x in f if x.get("purpose") == "entry"]
        ex = [x for x in f if x.get("purpose") != "entry"]
        sz = sum(x["signed_sz"] for x in ent)
        c = p["closed"] or {}
        return {"coin": p["coin"], "side": "买" if sz > 0 else "卖",
                "entry": round(sum(abs(x["signed_sz"]) * x["px"] for x in ent) / max(1e-12, sum(abs(x["signed_sz"]) for x in ent)), 4) if ent else None,
                "exit": round(ex[-1]["px"], 4) if ex else None,
                "pnl": round(float(c.get("pnl_usd", 0.0)), 4), "reason": c.get("reason") or p["exit"]}
    done = [p for p in pos.values() if p["fills"] and p["closed"]]
    today = [summarize(p) for p in done if _pt_date(p["closed"]["t_ms"]) == day]
    cum = round(sum(float(p["closed"].get("pnl_usd", 0.0)) for p in done), 4)
    limit = 20.0
    try:
        from . import account_stop
        limit = float(account_stop.limit(settings))
    except Exception:  # noqa: BLE001
        pass
    return {"日期": day.isoformat(),
            "今天": {"跑了几场事件": len(run), "跳过的事件": skipped, "成交": today,
                    "各币最大过冲bps": {c: round(v, 1) for c, v in overs.items()}, "门槛bps": thr or None,
                    "价差挡单次数": blocks, "当天盈亏美元": round(sum(t["pnl"] for t in today), 4)},
            "累计": {"盈亏美元": cum, "成交笔数": len(done), "目标笔数": TARGET_FILLS},
            "全账户止损还剩美元": round(limit + min(0.0, cum), 2)}


# ------------------------------------------------------------------ 没有大模型时：代码直接写
def fallback(facts: dict) -> dict:
    t, c = facts.get("今天") or {}, facts.get("累计") or {}
    fills = t.get("成交") or []
    pnl, cum = float(t.get("当天盈亏美元") or 0), float(c.get("盈亏美元") or 0)
    n, tgt = int(c.get("成交笔数") or 0), int(c.get("目标笔数") or TARGET_FILLS)
    left = facts.get("全账户止损还剩美元")
    ovr = t.get("各币最大过冲bps") or {}
    thr = t.get("门槛bps") or {}
    k = max(thr.values()) if thr else 9.0
    near = [f"{co} 最多偏了 {v:g}bps" for co, v in ovr.items()]
    if not fills:
        head = f"今天没有成交，没赚也没亏，累计 {n} 笔，离 {tgt} 笔还差 {max(0, tgt - n)} 笔。"
        why = ("没有挂上单。" + ("过冲（价格被推过头的部分）最大的情况：" + "，".join(near) + f"，门槛约 {k:g}bps，" +
               ("没有一个超过门槛，所以按规则不出手。" if all(v <= k for v in ovr.values()) else "有超过的，但没成交，可能被价差或时间窗挡住。") if near
               else "今天没有记到可用的过冲数据。"))
    else:
        w = sum(1 for f in fills if f["pnl"] > 0)
        head = f"今天成交 {len(fills)} 笔，{'赚' if pnl >= 0 else '亏'}了 ${abs(pnl):.2f}，累计 {'赚' if cum >= 0 else '亏'} ${abs(cum):.2f}。"
        why = (f"{len(fills)} 笔里 {w} 笔赚钱。" + "；".join(f"{f['coin']} {f['side']}，进 {f['entry']}，出 {f['exit']}，{'赚' if f['pnl'] >= 0 else '亏'} ${abs(f['pnl']):.2f}" for f in fills[:6])
               + "。赚亏主要看价格有没有真的回到参考价那边，以及手续费有没有吃掉差价。")
    if n < tgt:
        mech = f"现在才 {n} 笔，不够 {tgt} 笔，样本太少，看不出这个办法到底管不管用，不能下结论，更不能当成稳赚。"
    elif cum > 0:
        mech = f"{n} 笔跑完，合计赚 ${cum:.2f}，暂时站得住，但笔数还是不多，别当成一定能一直赚。"
    else:
        mech = f"{n} 笔跑完，合计亏 ${abs(cum):.2f}，现在看不出这个办法管用，应该认真考虑停掉或重新设计。"
    imp = []
    if t.get("价差挡单次数"):
        imp.append(f"今天有 {t['价差挡单次数']} 次被价差（买价和卖价之间的空隙）挡住，查一下是不是挡得太严。")
    if near and all(v <= k for v in ovr.values()):
        imp.append(f"门槛约 {k:g}bps，今天没有一个币偏过它，先观察，不要为了出手去降门槛。")
    if fills and pnl < 0:
        imp.append("亏的单要逐笔看：是进场太晚，还是出场太慢，不要只看总数。")
    if t.get("跳过的事件"):
        imp.append(f"今天有 {len(t['跳过的事件'])} 场事件被跳过，要查清是什么原因。")
    imp = imp or ["先继续按现有规则跑，不改参数，攒够笔数再说。"]
    tom = "看明天的重开时刻：参考价走完几步之后，各币偏了多少bps，有没有超过门槛；还有挂单有没有成交。" + (f"全账户止损还剩 ${left:g}。" if left is not None else "")
    return {"一句话结论": head, "今天为什么赚/亏": why, "机制还成不成立": mech, "改进方向": imp[:4], "明天看什么": tom}


# ------------------------------------------------------------------ 主入口
def _clean(o) -> dict | None:
    if not isinstance(o, dict) or not all(k in o for k in KEYS):
        return None
    imp = o["改进方向"]
    imp = [str(x) for x in imp] if isinstance(imp, list) else [str(imp)]
    out = {k: str(o[k]) for k in KEYS if k != "改进方向"}
    out["改进方向"] = imp[:4]
    return {k: out[k] for k in KEYS}


def reflect(settings, facts: dict, llm=None) -> dict:
    """一次调用，Sonnet 级模型；没 key / 超预算 / 出错 / 解析失败 → fallback(facts)。"""
    try:
        c = agents.cfg(settings)
        if llm is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return fallback(facts)
            llm = agents.AnthropicLLM()
        sp = agents.spend(settings)
        if sp["day"] >= sp["cap_day"] or sp["month"] >= sp["cap_month"]:
            return fallback(facts)
        model = c["lead_model"]
        user = json.dumps(facts, ensure_ascii=False)
        text, tin, tout = llm(model, SYSTEM, user, max_tokens=MAX_TOKENS) if isinstance(llm, agents.AnthropicLLM) else llm(model, SYSTEM, user)
        agents._record_spend(settings, "reopen_reflect", model, tin, tout)
        return _clean(agents.parse_json(text)) or fallback(facts)
    except Exception as e:  # noqa: BLE001
        log.warning("reopen reflect: %s", e)
        return fallback(facts)
