"""赛马场：每个"赛马岗"（交易、期权、算法）有 3 个 agent 互相竞争，代码按结果打分，自动奖惩，人不干预。

打分全部由代码完成，agent 不能给自己打分：
- 每场会议结束立刻结算"纪律分"（所有 agent）：引用了不存在的资料 −3/处；数字找不到出处 −1/个（最多 −5）；
  发言格式不完整 −2；完整且每个论点都有引用 +1。
- 期权岗：每天预测下一个对照样本的"实际/隐含"比值，答案出来后按比"历史平均"猜得准多少给分（±10 封顶）。
- 交易岗：对下个周末每个合约给出 fade/follow/skip + 仓位，开盘后按模拟盈亏给分（每 5 个万分点 1 分，±10 封顶）。
- 算法岗：方案交给代码做样本外回测，赢现行规则多少给多少分（±5 封顶）；过了多次尝试门槛 +5；交白卷/格式错 −1。

奖励：积分 → 排行榜；赛马岗第一名 = 冠军：冠军的决定进"正式模拟账"、模型升一级、发言在结论里优先、打法给别人学。
惩罚：扣分；连续两次周评垫底且落后冠军 10 分以上 → 淘汰，用冠军打法改写出新一代顶替（分数清零重来）；
     交易 agent 自己的模拟账 30 天亏超 15 分 → 暂停冠军资格（照样发言、照样记影子账）。
冠军本身也不行（30 天积分 ≤ 0 且已结算 ≥ 4 次）时，自动加一个挑战者（每个岗位最多 4 个）。
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import current_or_next_weekend

RACES = {"trading": "交易", "options": "期权", "algo": "算法"}
WINDOW_DAYS = 30
MAX_PER_RACE = 4


def _d(settings) -> Path:
    p = Path(settings.data) / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _append(p: Path, rec: dict):
    with p.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


# ------------------------------------------------------------------ state (generations, challengers, pauses)
def state(settings) -> dict:
    p = _d(settings) / "arena.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"agents": {}, "reviews": []}


def save_state(settings, st: dict):
    p = _d(settings) / "arena.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(p)


def roster(settings, base: list[dict]) -> list[dict]:
    """基础名单 + 每个 agent 当前这一代的立场 + 自动加入的挑战者。"""
    st = state(settings)["agents"]
    out = []
    for a in base:
        s = st.get(a["id"], {})
        out.append({**a, "gen": s.get("gen", 1), "stance": s.get("stance") or a["stance"],
                    "name": s.get("name") or a["name"], "since": s.get("since", 0)})
    for aid, s in st.items():
        if s.get("challenger") and not s.get("removed"):
            out.append({"id": aid, "room": s["room"], "name": s["name"], "stance": s["stance"],
                        "gen": s.get("gen", 1), "since": s.get("since", 0), "challenger": True})
    return out


# ------------------------------------------------------------------ scores
def add_score(settings, agent: str, kind: str, points: float, reason: str, ref: str = "", **extra):
    _append(_d(settings) / "scores.jsonl", {"ts": time.time(), "agent": agent, "kind": kind,
                                           "points": round(float(points), 2), "reason": reason, "ref": ref, **extra})


def scores(settings) -> list[dict]:
    return _read(_d(settings) / "scores.jsonl")


def standings(settings, base: list[dict], now: float | None = None) -> dict:
    """{agent_id: {score30, n_outcome, last:[...], status}} + {race: champion_id}."""
    now = now or time.time()
    ros = roster(settings, base)
    since = {a["id"]: a.get("since", 0) for a in ros}
    cut = now - WINDOW_DAYS * 86400
    st = state(settings)["agents"]
    table: dict[str, dict] = {a["id"]: {"score30": 0.0, "total": 0.0, "n_outcome": 0, "last": [], "room": a["room"],
                                        "name": a["name"], "gen": a.get("gen", 1),
                                        "paused": st.get(a["id"], {}).get("paused", False)} for a in ros}
    trade_pnl: dict[str, float] = {}
    for s in scores(settings):
        t = table.get(s["agent"])
        if t is None or s["ts"] < since.get(s["agent"], 0):
            continue
        t["total"] += s["points"]
        if s["ts"] >= cut:
            t["score30"] += s["points"]
            if s["kind"] in ("outcome", "algo"):
                t["n_outcome"] += 1
            if s["kind"] == "outcome" and t["room"] == "trading":
                trade_pnl[s["agent"]] = trade_pnl.get(s["agent"], 0) + s["points"]
        t["last"].append(s)
    for aid, t in table.items():
        t["last"] = t["last"][-6:][::-1]
        t["score30"] = round(t["score30"], 2)
        t["total"] = round(t["total"], 2)
        if t["room"] == "trading" and trade_pnl.get(aid, 0) < -15:
            t["paused"] = True
    champs = {}
    for race in RACES:
        cands = [(aid, t) for aid, t in table.items() if t["room"] == race and not t["paused"]]
        if not cands:
            continue
        prev = state(settings).get("champions", {}).get(race)
        best = max(cands, key=lambda x: (x[1]["score30"], x[0] == prev))
        champs[race] = best[0]
    for aid, t in table.items():
        t["champion"] = champs.get(t["room"]) == aid
    return {"table": table, "champions": champs}


# ------------------------------------------------------------------ immediate scoring after each meeting
def score_meeting(settings, rec: dict, checks: dict):
    rid = rec["run_id"]
    bad: dict[str, int] = {}
    for x in checks.get("cite", {}).get("bad_ids", []):
        bad[x.split(":")[0]] = bad.get(x.split(":")[0], 0) + 1
    uns: dict[str, int] = {}
    for x in checks.get("cite", {}).get("unsourced_numbers", []):
        uns[x.split(":")[0]] = uns.get(x.split(":")[0], 0) + 1
    seen = set()
    for room in rec.get("rooms", {}).values():
        for rr in room.get("rounds", []):
            for o in rr:
                aid, out = o["agent"], o.get("out", {})
                if out.get("error"):
                    continue                       # 接口出错不是 agent 的错
                pts, why = 0.0, []
                if out.get("parse_error"):
                    pts -= 2
                    why.append("发言格式不完整 −2")
                pp = [p for p in out.get("points") or [] if isinstance(p, dict)]
                if pp and all(p.get("cite") for p in pp) and not out.get("parse_error"):
                    pts += 1
                    why.append("完整且论点都有引用 +1")
                if aid not in seen:                # 引用/数字问题按人算一次（两轮合计）
                    if bad.get(aid):
                        pts -= 3 * bad[aid]
                        why.append(f"引用了不存在的资料 {bad[aid]} 处 −{3 * bad[aid]}")
                    if uns.get(aid):
                        k = min(5, uns[aid])
                        pts -= k
                        why.append(f"{uns[aid]} 个数字找不到出处 −{k}")
                    seen.add(aid)
                if why:
                    add_score(settings, aid, "discipline", pts, "；".join(why), rid)


def score_algo(settings, rec: dict):
    room = rec.get("rooms", {}).get("algo")
    if not room:
        return
    rid = rec["run_id"]
    for rr in room.get("rounds", []):
        for o in rr:
            aid, out = o["agent"], o.get("out", {})
            if out.get("error") or "backtest" not in out:
                continue
            bts = out.get("backtest") or []
            if not bts:
                add_score(settings, aid, "algo", -1, "交白卷：没有提交方案", rid)
                continue
            best, note = None, ""
            for b in bts:
                if b.get("error"):
                    add_score(settings, aid, "algo", -1, f"方案无效：{b['error']}", rid)
                    continue
                r = b["result"]
                if r["test"]["n"] < 8 or r["test"]["mean"] is None:
                    continue
                base = r["baseline_test"]["mean"] or 0.0
                ex = r["test"]["mean"] - base
                ex = ex / 5 if b["spec"]["kind"] == "weekend" else ex * 20
                if best is None or ex > best:
                    best, note = ex, b["spec"]["name"]
                if "显著好于" in r["verdict"]:
                    add_score(settings, aid, "algo", 5, f"「{b['spec']['name']}」样本外显著赢现行规则 +5", rid)
            if best is None:
                add_score(settings, aid, "algo", -0.5, "方案在考试段笔数太少，无法评估", rid)
            else:
                pts = max(-5.0, min(5.0, best))
                add_score(settings, aid, "algo", pts, f"最好方案「{note}」考试段比现行规则{'多' if pts >= 0 else '少'}赚 {abs(pts):.1f} 分", rid)


# ------------------------------------------------------------------ predictions (trading / options) and settlement
def record_predictions(settings, rec: dict, now: datetime, champions: dict, paused: set):
    p = _d(settings) / "predictions.jsonl"
    rid = rec["run_id"]
    tr = rec.get("rooms", {}).get("trading")
    if tr:
        last = tr["rounds"][-1]
        for o in last:
            for prop in o.get("out", {}).get("proposal") or []:
                if not isinstance(prop, dict):
                    continue
                inst = next((i for i in settings.instruments if i["name"] == str(prop.get("name", "")).upper()), None)
                if not inst:
                    continue
                w = current_or_next_weekend(now, settings, inst)
                if now >= w.decision:
                    continue
                lean = prop.get("lean") if prop.get("lean") in ("fade", "follow", "skip") else "skip"
                try:
                    size = max(0.0, min(1.0, float(prop.get("size", 0))))
                except (TypeError, ValueError):
                    size = 0.0
                _append(p, {"ts": now.timestamp(), "run": rid, "agent": o["agent"], "kind": "trade", "target": w.wid,
                            "decision": w.decision.isoformat(), "lean": lean, "size": size,
                            "champion": champions.get("trading") == o["agent"] and o["agent"] not in paused})
    op = rec.get("rooms", {}).get("options")
    if op:
        from . import options
        tgt = options.daily_sample((now.astimezone(timezone.utc) + timedelta(days=1)).date(), settings)
        base = _hist_ratio(settings)
        for o in op["rounds"][-1]:
            v = o.get("out", {}).get("forecast_ratio")
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if 0.05 <= v <= 5 and now < tgt.lock:
                _append(p, {"ts": now.timestamp(), "run": rid, "agent": o["agent"], "kind": "vol", "target": tgt.sid,
                            "forecast": v, "base": base, "champion": champions.get("options") == o["agent"]})


def _hist_ratio(settings) -> float:
    p = Path(settings.data) / "reports" / "history_options.json"
    try:
        c = json.loads(p.read_text()).get("control", [])
        return math.exp(sum(math.log(x["ratio"]) for x in c) / len(c)) if c else 0.85
    except (OSError, ValueError, KeyError):
        return 0.85


def settle(settings) -> int:
    """把已经出结果的预测结算成分数。可以反复调用（已结算的不会重复）。"""
    from . import algolab, options
    from .ledger import Ledger
    preds = _read(_d(settings) / "predictions.jsonl")
    done_p = _d(settings) / "settled.jsonl"
    done = {(d["agent"], d["kind"], d["target"]) for d in _read(done_p)}
    # 每个 agent 每个目标只算最后一次（决策前）的预测
    latest: dict[tuple, dict] = {}
    for x in preds:
        latest[(x["agent"], x["kind"], x["target"])] = x
    wk = {r["weekend"]: r for r in Ledger(settings.data).completed()}
    op = {r["sid"]: r for r in options.completed(Ledger(settings.data, "options"))}
    cost, _ = algolab.weekend_cost_bps(settings)
    n = 0
    for key, x in latest.items():
        if key in done:
            continue
        if x["kind"] == "trade":
            r = wk.get(x["target"])
            if not r:
                continue
            lock, out = r["lock"], r["outcome"]
            if not out.get("ok") or lock.get("void") or "dev_bps" not in lock.get("features", {}):
                _append(done_p, {"agent": x["agent"], "kind": "trade", "target": x["target"], "void": True})
                continue
            dev, y = lock["features"]["dev_bps"], out["y_bps"]
            side = 0 if x["lean"] == "skip" or x["size"] == 0 else (-1 if x["lean"] == "fade" else 1) * (1 if dev > 0 else -1)
            pnl = x["size"] * (side * (y - dev) - cost) if side else 0.0
            pts = max(-10.0, min(10.0, pnl / 5))
            name = x["target"].split("|")[-1]
            add_score(settings, x["agent"], "outcome", pts,
                      f"{name} {x['target'][:10]}：押 {x['lean']} 仓位 {x['size']:.1f}，模拟 {pnl:+.1f} 万分点",
                      x["target"], pnl_bps=pnl, champion=x.get("champion", False))
        else:
            r = op.get(x["target"])
            if not r:
                continue
            if not r["outcome"].get("ok"):
                _append(done_p, {"agent": x["agent"], "kind": "vol", "target": x["target"], "void": True})
                continue
            a = r["outcome"]["ratio"]
            err, berr = abs(math.log(x["forecast"] / a)), abs(math.log(x["base"] / a))
            pts = max(-10.0, min(10.0, 10 * (berr - err)))
            add_score(settings, x["agent"], "outcome", pts,
                      f"{x['target']}：猜 {x['forecast']:.2f}，实际 {a:.2f}（历史平均 {x['base']:.2f}）", x["target"],
                      champion=x.get("champion", False))
        _append(done_p, {"agent": x["agent"], "kind": x["kind"], "target": x["target"]})
        n += 1
    return n


def governed_book(settings) -> dict:
    """正式模拟账 = 每次决策时冠军的交易；对照 = 每个交易 agent 自己的影子账。"""
    out = {"governed": 0.0, "n": 0, "by_agent": {}}
    for s in scores(settings):
        if s.get("kind") != "outcome" or "pnl_bps" not in s:
            continue
        b = out["by_agent"].setdefault(s["agent"], {"pnl_bps": 0.0, "n": 0})
        b["pnl_bps"] += s["pnl_bps"]
        b["n"] += 1
        if s.get("champion"):
            out["governed"] += s["pnl_bps"]
            out["n"] += 1
    return out


# ------------------------------------------------------------------ weekly review: retire / replace / add challenger
MUTATE_PROMPT = """你在为一个交易研究团队设计新成员。岗位：{race}。
现任冠军的打法：{champ}
被淘汰成员原来的立场：{loser}
写一个新成员：继承冠军打法里有效的部分，但要有一个明确不同的思路（不能照抄），并且遵守纪律（只用资料包、不编数字）。
只输出 JSON：{{"name": "四到六个字的名字", "stance": "两三句话的立场与打法"}}"""


def weekly_review(settings, base: list[dict], llm=None, model: str | None = None, now: float | None = None) -> list[str]:
    """每 7 天一次：淘汰连续垫底的、必要时加挑战者。返回发生了什么（大白话）。"""
    now = now or time.time()
    st = state(settings)
    if now - st.get("last_review", 0) < 6.5 * 86400:
        return []
    S = standings(settings, base, now)
    tab, champs = S["table"], S["champions"]
    events = []
    for race, label in RACES.items():
        members = [(aid, t) for aid, t in tab.items() if t["room"] == race]
        if len(members) < 2 or race not in champs:
            continue
        champ = champs[race]
        ct = tab[champ]
        worst_id, wt = min(members, key=lambda x: x[1]["score30"])
        champ_play = _playbook(settings, champ) or next((a["stance"] for a in roster(settings, base) if a["id"] == champ), "")
        prev_last = st.get("last_worst", {}).get(race)
        st.setdefault("last_worst", {})[race] = worst_id
        if (worst_id != champ and prev_last == worst_id and wt["n_outcome"] >= 2
                and ct["score30"] - wt["score30"] >= 10):
            loser = next((a for a in roster(settings, base) if a["id"] == worst_id), None)
            new = _mutate(llm, model, label, champ_play, loser["stance"] if loser else "")
            s = st["agents"].setdefault(worst_id, {})
            s.update({"gen": s.get("gen", 1) + 1, "stance": new["stance"], "name": new["name"], "since": now})
            if loser and loser.get("challenger"):
                s["room"] = race
            events.append(f"{label}岗：{wt['name']} 连续两周垫底、落后冠军 {ct['score30'] - wt['score30']:.0f} 分，淘汰；"
                          f"第 {s['gen']} 代「{new['name']}」接替，积分清零。")
            st["last_worst"][race] = None
        if ct["score30"] <= 0 and ct["n_outcome"] >= 4 and len(members) < MAX_PER_RACE:
            new = _mutate(llm, model, label, champ_play, "（无，新增挑战者）")
            cid = f"{race.upper()}_C{len(members) + 1}"
            st["agents"][cid] = {"challenger": True, "room": race, "name": new["name"], "stance": new["stance"],
                                 "gen": 1, "since": now}
            events.append(f"{label}岗：冠军 30 天积分 {ct['score30']:.1f} 不为正，自动加入挑战者「{new['name']}」。")
    st["last_review"] = now
    st["champions"] = champs
    st.setdefault("reviews", []).append({"ts": now, "events": events})
    st["reviews"] = st["reviews"][-20:]
    save_state(settings, st)
    return events


def _playbook(settings, aid: str) -> str:
    p = _d(settings) / "playbooks.json"
    try:
        return json.loads(p.read_text()).get(aid, "")
    except (OSError, ValueError):
        return ""


def save_playbooks(settings, rec: dict):
    p = _d(settings) / "playbooks.json"
    try:
        books = json.loads(p.read_text())
    except (OSError, ValueError):
        books = {}
    for race in RACES:
        room = rec.get("rooms", {}).get(race)
        if not room:
            continue
        for o in room["rounds"][-1]:
            pb = o.get("out", {}).get("playbook")
            if isinstance(pb, str) and pb.strip():
                books[o["agent"]] = pb.strip()[:300]
    p.write_text(json.dumps(books, ensure_ascii=False, indent=1))


def _mutate(llm, model, race, champ, loser) -> dict:
    if llm is not None:
        try:
            from .agents import parse_json
            text, _, _ = llm(model, "你是团队设计师。只输出 JSON。", MUTATE_PROMPT.format(race=race, champ=champ, loser=loser))
            j = parse_json(text)
            if j.get("name") and j.get("stance"):
                return {"name": str(j["name"])[:12], "stance": str(j["stance"])[:400]}
        except Exception:  # noqa: BLE001
            pass
    return {"name": f"{race}新秀", "stance": f"学习冠军的打法：{champ[:200]}；但更保守：信号不够强就不做，仓位减半。"}


def feedback_line(settings, aid: str, S: dict) -> str:
    """开会前告诉 agent 自己的成绩和最近奖惩（及时反馈）。"""
    t = S["table"].get(aid)
    if not t:
        return ""
    race = t["room"]
    peers = sorted([(x["score30"], k) for k, x in S["table"].items() if x["room"] == race], reverse=True)
    rank = next((i + 1 for i, (_, k) in enumerate(peers) if k == aid), None)
    last = "；".join(f"{s['points']:+g}（{s['reason']}）" for s in t["last"][:3]) or "还没有记录"
    s = f"你的成绩：30 天积分 {t['score30']:+.1f}，"
    if race in RACES:
        s += f"本岗排名 {rank}/{len(peers)}{'，你是冠军：你的决定会进正式模拟账' if t['champion'] else ''}"
        if t["paused"]:
            s += "，你因模拟账亏损过多被暂停冠军资格"
        s += "。"
        champ = S["champions"].get(race)
        if champ and champ != aid:
            pb = _playbook(settings, champ)
            if pb:
                s += f"现任冠军的打法：{pb}。"
    s += f"最近奖惩：{last}。扣分最重的是编造引用和数字。"
    return s
