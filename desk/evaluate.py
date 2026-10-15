"""重开结果、评估口径（备忘 7.5）与止损终止条件（第八章）。"""
from __future__ import annotations

import math
from datetime import date

from .clock import Weekend, to_ns
from .features import first_ctx_after


def outcome(settings, w: Weekend, lock: dict) -> dict:
    coin = w.coin or settings.instrument
    grace = settings["clock"]["reopen_grace_sec"]
    first = first_ctx_after(settings, coin, to_ns(w.resume) + int(grace * 1e9))
    if not first:
        return {"ok": False, "problems": ["外部价格恢复后 30 分钟内没有预言机数据"]}
    reopen = float(first[1]["oraclePx"])
    f = lock["features"]
    fri, dev = f["fri_close"], f["dev_bps"]
    y = (reopen / fri - 1) * 1e4
    pred = lock["prediction"]["pred_bps"]
    out = {"ok": True, "reopen_px": reopen, "reopen_received_at": first[0], "y_bps": y,
           "err_model_bps": abs(pred - y), "err_A_bps": abs(y), "err_B_bps": abs(dev - y),
           "dev_reverted_bps": dev - y}
    act = lock["action"]
    shadow = lock.get("shadow") or {}
    need_exit = bool(act.get("side")) or any(a.get("side") for a in shadow.values())
    ex = first_ctx_after(settings, coin, to_ns(w.exit)) if need_exit else None
    exit_px = float(ex[1].get("midPx") or ex[1]["markPx"]) if ex else None
    fees, costs = lock.get("fees") or {}, settings["costs"]
    if act["side"] and exit_px is not None:
        p = _sim_pnl(act["side"], act["notional_usd"], act["entry_px"], exit_px, f, fees, costs, y, dev)
        out.update(exit_px=exit_px, **p)
    else:
        out.update(pnl_net_bps=0.0, pnl_net_usd=0.0, pnl_net_taker_usd=0.0,
                   pnl_net_fullfee_usd=0.0, direction_hit=None)
    # 影子策略（顺势等）：同口径算模拟盈亏，但从不下真单
    sh: dict = {}
    for sname, a in shadow.items():
        if a.get("side") and exit_px is not None:
            p = _sim_pnl(a["side"], a["notional_usd"], a["entry_px"], exit_px, f, fees, costs, y, dev)
            sh[sname] = {"side": a["side"], "notional_usd": a["notional_usd"], "pnl_net_usd": p["pnl_net_usd"],
                         "pnl_net_fullfee_usd": p["pnl_net_fullfee_usd"], "pnl_net_fullfee_bps": p["pnl_net_fullfee_bps"],
                         "direction_hit": p["direction_hit"]}
        else:
            sh[sname] = {"side": a.get("side", 0), "notional_usd": a.get("notional_usd", 0.0),
                         "pnl_net_usd": 0.0, "pnl_net_fullfee_usd": 0.0, "pnl_net_fullfee_bps": 0.0, "direction_hit": None}
        if a.get("direction"):
            sh[sname]["direction"] = a["direction"]
    if sh:
        out["shadow"] = sh
    return out


def _sim_pnl(side: int, notional: float, entry_px: float, exit_px: float, f: dict, fees: dict, costs: dict,
             y: float, dev: float) -> dict:
    """一笔的扣费后模拟盈亏（挂单进场、吃单离场），含诚实的"全额手续费"口径。

    全额手续费口径：growthMode 的约九折是冷启动补贴、会消失；只有全额手续费下也赚钱才算真有边际。
    """
    gross = side * (exit_px / entry_px - 1) * 1e4
    net_maker = gross - fees.get("maker_bps", costs["maker_bps"]) - fees.get("taker_bps", costs["taker_bps"])
    taker_entry = f.get("best_ask" if side > 0 else "best_bid") or f["dec_mid"]
    net_taker = side * (exit_px / taker_entry - 1) * 1e4 - 2 * fees.get("taker_bps", costs["taker_bps"])
    net_full = gross - costs["maker_bps"] - costs["taker_bps"]
    return {"pnl_gross_bps": gross, "pnl_net_bps": net_maker, "pnl_net_usd": net_maker / 1e4 * notional,
            "pnl_net_taker_bps": net_taker, "pnl_net_taker_usd": net_taker / 1e4 * notional,
            "pnl_net_fullfee_bps": net_full, "pnl_net_fullfee_usd": net_full / 1e4 * notional,
            "direction_hit": (math.copysign(1, y - dev) == side)}


def official(rec: dict, cfg) -> bool:
    """计入正式成绩：未作废、特征完整、且在模型知识截止日之后（7.4 第三条）。"""
    lock, out = rec.get("lock", {}), rec.get("outcome", {})
    if lock.get("void"):
        return False
    if not lock.get("features", {}).get("ok") or not out.get("ok"):
        return False
    return date.fromisoformat(rec["weekend"][:10]) > date.fromisoformat(cfg["llm"]["knowledge_cutoff"])


def _t_one_sided(diffs: list[float]) -> tuple[float, float]:
    n = len(diffs)
    if n < 3:
        return float("nan"), float("nan")
    m = sum(diffs) / n
    sd = math.sqrt(sum((d - m) ** 2 for d in diffs) / (n - 1))
    if sd == 0:
        return float("inf") if m > 0 else 0.0, 0.0 if m > 0 else 1.0
    t = m / (sd / math.sqrt(n))
    # normal approximation for p (n>=30 at decision time)
    p = 0.5 * math.erfc(t / math.sqrt(2))
    return t, p


def scorecard(records: list[dict], cfg) -> dict:
    off = [r for r in records if official(r, cfg)]
    n = len(off)
    res = {"n_official": n, "n_total": len(records)}
    if not n:
        return res
    em = [r["outcome"]["err_model_bps"] for r in off]
    ea = [r["outcome"]["err_A_bps"] for r in off]
    eb = [r["outcome"]["err_B_bps"] for r in off]
    res.update(mean_err_model=sum(em) / n, mean_err_A=sum(ea) / n, mean_err_B=sum(eb) / n,
               med_err_model=sorted(em)[n // 2], med_err_A=sorted(ea)[n // 2], med_err_B=sorted(eb)[n // 2])
    tA, pA = _t_one_sided([a - m for a, m in zip(ea, em)])
    tB, pB = _t_one_sided([b - m for b, m in zip(eb, em)])
    res.update(t_vs_A=tA, p_vs_A=pA, t_vs_B=tB, p_vs_B=pB)
    # 按"日历周末"聚类再检验一次：7 个合约同一个周末高度相关，当成 7 个独立样本会把显著性灌水。
    # 做法：同一个周末里的差值先取平均，再对"每个周末一个数"做检验，并报告有效样本数（去重后的周末数）。
    by_wk_A: dict[str, list] = {}
    by_wk_B: dict[str, list] = {}
    for r, a, b, m in zip(off, ea, eb, em):
        wk = r["weekend"][:10]
        by_wk_A.setdefault(wk, []).append(a - m)
        by_wk_B.setdefault(wk, []).append(b - m)
    res["n_weekends"] = len(by_wk_A)
    meansA = [sum(v) / len(v) for v in by_wk_A.values()]
    meansB = [sum(v) / len(v) for v in by_wk_B.values()]
    tAw, pAw = _t_one_sided(meansA)
    tBw, pBw = _t_one_sided(meansB)
    res.update(p_vs_A_weekend=pAw, p_vs_B_weekend=pBw)
    traded = [r for r in off if r["lock"]["action"]["side"]]
    res["n_traded"] = len(traded)
    res["hit_rate"] = (sum(1 for r in traded if r["outcome"].get("direction_hit")) / len(traded)) if traded else None
    res["pnl_net_usd_total"] = sum(r["outcome"].get("pnl_net_usd", 0) for r in off)
    res["pnl_net_taker_usd_total"] = sum(r["outcome"].get("pnl_net_taker_usd", 0) for r in off)
    res["pnl_net_fullfee_usd_total"] = sum(r["outcome"].get("pnl_net_fullfee_usd", 0) for r in off)
    # 第八章止损
    rw = cfg["risk"]["rolling_weeks"]
    rolling = sum(r["outcome"].get("pnl_net_usd", 0) for r in off[-rw:])
    res["rolling_pnl_usd"] = rolling
    res["rolling_stop"] = rolling < -cfg["risk"]["rolling_loss_limit"] * cfg["risk"]["capital_usd"]
    need = cfg["risk"]["eval_after_weekends"]
    nw = res["n_weekends"]
    if nw >= need:
        # 要同时过四关才算数：对两条基线都显著（逐条样本），按周末聚类后仍对两条基线都显著，且全额手续费下也赚钱。
        beats = (pA < 0.05 and pB < 0.05 and pAw < 0.05 and pBw < 0.05
                 and res["pnl_net_fullfee_usd_total"] > 0)
        res["verdict"] = ("H1 通过：按日历周末聚类后仍显著优于两条基线，且全额手续费下仍赚钱，可以考虑扩大" if beats else
                          "H1 终止：满 30 个周末后，按周末聚类的显著性或全额手续费盈利这几关没全过。保留数据管道，转向其他假设")
    else:
        res["verdict"] = (f"还差 {need - nw} 个日历周末才有资格判断 H1（已有 {nw} 个；7 个合约同一周末高度相关，"
                          f"按周末而不是按条数算）。之前的任何结果都只算'值得继续看'")
    return res


def shadow_scorecard(records: list[dict], cfg, strat: str = "follow") -> dict:
    """影子策略成绩（只模拟、不下真单）：累计模拟盈亏、方向命中率、够不够样本下结论。

    判断"该不该上真钱"用的是全额手续费口径：只有全额手续费下也稳定为正，才算真有边际。
    """
    off = [r for r in records if official(r, cfg)]
    traded = [(r, r["outcome"].get("shadow", {}).get(strat)) for r in off]
    traded = [(r, s) for r, s in traded if s and s.get("side")]
    n_wk = len({r["weekend"][:10] for r, _ in traded})
    pnl = sum(s.get("pnl_net_usd", 0) for _, s in traded)
    pnl_full = sum(s.get("pnl_net_fullfee_usd", 0) for _, s in traded)
    hits = [s["direction_hit"] for _, s in traded if s.get("direction_hit") is not None]
    hit_rate = (sum(1 for h in hits if h) / len(hits)) if hits else None
    need = cfg["risk"]["eval_after_weekends"]
    res = {"strategy": strat, "n_trades": len(traded), "n_weekends": n_wk,
           "pnl_net_usd_total": pnl, "pnl_net_fullfee_usd_total": pnl_full, "hit_rate": hit_rate}
    if strat != "follow":
        from .model import SHADOW_LABELS
        lab = SHADOW_LABELS.get(strat, strat)
        res["label"] = lab
        hit_txt = f"、方向命中 {hit_rate * 100:.0f}%" if hit_rate is not None else ""
        if n_wk >= need:
            res["verdict"] = (f"影子「{lab}」已有 {n_wk} 个日历周末样本，全额手续费累计 ${pnl_full:+.2f}{hit_txt}。"
                              + ("为正，可以讨论小仓上真钱。" if pnl_full > 0 else "不为正，不上真钱。"))
        else:
            res["verdict"] = (f"影子「{lab}」：{n_wk} 个日历周末、{len(traded)} 笔，全额手续费累计 ${pnl_full:+.2f}{hit_txt}；"
                              f"还差 {max(0, need - n_wk)} 个周末才有资格下结论。")
        return res
    if n_wk >= need:
        hit_txt = f"、方向命中 {hit_rate * 100:.0f}%" if hit_rate is not None else ""
        tail = ("全额手续费下为正，可考虑把顺势策略转真钱（仍建议先小仓）。" if pnl_full > 0
                else "全额手续费下不赚钱，先别上真钱，继续观察或换假设。")
        res["verdict"] = (f"影子已有 {n_wk} 个信息周末样本，累计模拟 ${pnl:+.2f}"
                          f"（全额手续费 ${pnl_full:+.2f}）{hit_txt}。" + tail)
    else:
        res["verdict"] = (f"顺势策略影子跑：已有 {n_wk} 个信息周末样本，累计模拟 ${pnl:+.2f}"
                          f"（全额手续费 ${pnl_full:+.2f}）。还需 {max(0, need - n_wk)} 个信息周末样本才有资格下结论，"
                          f"现在只算'值得继续看'，不动真钱。")
    return res


SHADOW_ORDER = ("follow", "follow50", "fade50", "regime", "v2", "fade50_stocks")


def shadow_table(records: list[dict], cfg) -> list[dict]:
    """"影子策略对照"表：主策略（实际 action）+ 每条影子，同口径（全额手续费）累计盈亏、笔数、日历周末数、命中率。

    只算正式记录（official）。影子在旧记录里没有的（10/15 之前锁定的周末没有 follow50/fade50/regime）就是 0 笔。
    """
    from .model import SHADOW_LABELS
    off = [r for r in records if official(r, cfg)]
    rows = []
    traded = [r for r in off if (r["lock"].get("action") or {}).get("side")]
    hits = [r["outcome"].get("direction_hit") for r in traded if r["outcome"].get("direction_hit") is not None]
    rows.append({"strategy": "primary", "label": "主策略（实际 action）", "n_trades": len(traded),
                 "n_weekends": len({r["weekend"][:10] for r in traded}),
                 "pnl_net_fullfee_usd_total": sum(r["outcome"].get("pnl_net_fullfee_usd", 0.0) for r in traded),
                 "hit_rate": (sum(1 for h in hits if h) / len(hits)) if hits else None})
    seen = {k for r in off for k in (r["outcome"].get("shadow") or {})}
    for k in list(SHADOW_ORDER) + sorted(seen - set(SHADOW_ORDER)):
        sc = shadow_scorecard(records, cfg, k)
        rows.append({"strategy": k, "label": SHADOW_LABELS.get(k, k), "n_trades": sc["n_trades"], "n_weekends": sc["n_weekends"],
                     "pnl_net_fullfee_usd_total": sc["pnl_net_fullfee_usd_total"], "hit_rate": sc["hit_rate"]})
    return rows


def scorecard_by_coin(records: list[dict], cfg) -> dict[str, dict]:
    """每个合约一张成绩单。"""
    from .clock import name_of
    groups: dict[str, list] = {}
    default = cfg.instruments[0]["name"]
    for r in records:
        groups.setdefault(name_of(r["weekend"], default), []).append(r)
    order = [i["name"] for i in cfg.instruments]
    return {k: scorecard(groups[k], cfg) for k in sorted(groups, key=lambda k: order.index(k) if k in order else 99)}
