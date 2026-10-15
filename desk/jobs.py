"""每个周末的固定流程（备忘 7.6 实验日志"何时填写"）：

周五收盘   open     冻结规则哈希，写入"假设与改动"
周末期间   extract  大语言模型抽取新闻事件（每 15 分钟）
决策时刻   lock     生成特征 → 预测 → 模拟动作，结果出来前锁定，哈希推送到手机
重开后     outcome  自动写入实际结果、误差、扣费后模拟盈亏
周一早上   report   周报 + 成绩单推送，等你人工复核
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import evaluate, extract, features, model, notify, options, report
from .clock import Weekend, to_ns
from .config import rules_hash
from .ledger import Ledger

log = logging.getLogger("jobs")


def _pending_note_path(settings) -> Path:
    return Path(settings.data) / "state" / "pending_note.txt"


def job_open(settings, w: Weekend) -> dict:
    led = Ledger(settings.data)
    note_p = _pending_note_path(settings)
    note = note_p.read_text().strip() if note_p.exists() else "其余不变"
    rec = led.append("open", w.wid, {
        "exp_id": f"EXP-{w.wid}", "coin": w.coin, "name": w.name, "hypothesis_change": note, "rules_hash": rules_hash(settings.root),
        "window": {"close": w.close.isoformat(), "decision": w.decision.isoformat(), "resume": w.resume.isoformat()},
    })
    return rec


def clear_pending_note(settings) -> None:
    p = _pending_note_path(settings)
    if p.exists():
        p.unlink()


def job_extract(settings, w: Weekend, extractor=None, until: datetime | None = None, clock=None) -> int:
    end = min(until or datetime.now(timezone.utc), w.decision)
    kw = {"clock": clock} if clock else {}
    return extract.run_extraction(settings, to_ns(w.news_start), to_ns(end), extractor=extractor, **kw)


def job_lock(settings, w: Weekend, push: bool = True) -> dict:
    led = Ledger(settings.data)
    wk = led.weekend(w.wid)
    if "lock" in wk:
        return wk["lock"]
    void_reasons = []
    opened = wk.get("open")
    now_hash = rules_hash(settings.root)
    if not opened:
        void_reasons.append("周五收盘时没有冻结规则（系统当时未运行）")
    elif opened["rules_hash"] != now_hash:
        void_reasons.append("周五收盘后修改过规则/代码（第八章第四条：规则只能在周一复核时修改）")
    feats = features.build(settings, w)
    history = []
    for r in led.completed():
        if evaluate.official(r, settings):
            history.append({"dev_bps": r["lock"]["features"]["dev_bps"],
                            "news_weekend": r["lock"]["features"].get("news_weekend"),
                            "y_bps": r["outcome"]["y_bps"]})
    if feats["ok"]:
        pred = model.predict(feats, history, settings)
        fees = model.fee_bps(settings, to_ns(w.decision), w.coin or None)
        v2h = v2_history(settings, led, w)
        action = model.decide_action(feats, pred, fees, settings, v2_history=v2h)
        shadow = model.shadow_actions(feats, fees, settings, regime_history(settings, led, w), v2_history=v2h)
    else:
        void_reasons += feats["problems"]
        pred, fees, action = {"pred_bps": None}, {}, {"side": 0, "notional_usd": 0, "reason": "数据不全，不出手"}
        shadow = {}
    body = {"coin": w.coin or settings.instrument, "name": w.name, "features": feats, "prediction": pred, "fees": fees, "action": action, "shadow": shadow,
            "prompt_version": settings["llm"]["prompt_version"], "llm_model": settings["llm"]["model"],
            "rules_hash": now_hash, "void": bool(void_reasons), "void_reasons": void_reasons,
            "mode": "live" if settings["live"]["enabled"] else "paper"}
    rec = led.append("lock", w.wid, body)
    if push:
        notify.push(settings, f"已锁定预测 {w.wid}", report.lock_text(rec), priority="high")
    return rec


def regime_history(settings, led: Ledger, w: Weekend) -> list[dict]:
    """这个合约、严格早于本周末的已完成周末 [{weekend, dev_bps, move_bps}]，按时间顺序（regime 影子用）。

    不要求 official：因规则改动作废的周末价格本身是对的，照样能说明"顺势还是反向赚"；但特征/结果必须完整。
    """
    from .clock import friday_of, name_of
    default = settings.instruments[0]["name"]
    me = w.name or default
    out = []
    for r in led.completed():
        if name_of(r["weekend"], default) != me or friday_of(r["weekend"]) >= w.friday.isoformat():
            continue
        mv = model.outcome_move_bps(r["lock"], r["outcome"])
        if mv is None:
            continue
        out.append({"weekend": r["weekend"], "dev_bps": r["lock"]["features"]["dev_bps"], "move_bps": mv})
    out.sort(key=lambda h: h["weekend"])
    return out


def v2_history(settings, led: Ledger, w: Weekend, backfill_rows: list[dict] | None = None) -> dict:
    """v2 停手规则用的历史（2026-10-15 改：实盘停手必须和实验室一致）。

    同一品种族、严格早于本周末的每个日历周末：
      - 账本里有这个族的**正式**记录（evaluate.official）→ 用账本（账本优先，整个周末都用账本的行）；
      - 否则 → 用回补行：和 strategy_lab 完全同一套（strategy_lab.load_candles 只读本地缓存、weekend_rows：15m 优先、退回 1h）。
    每行 {weekend, fri, name, dev_bps, move_bps, spread_bps, dislocated, wick_ratio, source}；盈亏由 model.v2_rule_net_bps
    按"如果当时按 v2 做"、全额手续费重算（回补行没有点差 → 半点差 2 bps，和实验室的 14 bps 一致）——不看主策略当时做了什么。
    data/reports/history_weekends.json 不用：它的"决策价"是重开那一刻、出场是重开后 1 小时，和实验室/实盘不是同一笔交易。
    返回 {"rows", "backfill_missing", "n_ledger_weekends", "n_backfill_weekends"}；没有任何回补行时只用账本，并在 lock 里注明。
    backfill_rows 只给测试注入用（None = 从磁盘读）。"""
    from .clock import friday_of, name_of
    default = settings.instruments[0]["name"]
    me = w.name or default
    fam = model.instrument_family((settings.inst(me) or {}).get("coin") or w.coin or me)
    cur = w.friday.isoformat()
    ledger_rows = []
    for r in led.completed():
        if friday_of(r["weekend"]) >= cur or not evaluate.official(r, settings):
            continue
        nm = name_of(r["weekend"], default)
        coin = (settings.inst(nm) or {}).get("coin") or r["lock"].get("coin") or nm
        if model.instrument_family(coin) != fam:
            continue
        mv = model.outcome_move_bps(r["lock"], r["outcome"])
        if mv is None:
            continue
        f = r["lock"]["features"]
        ledger_rows.append({"weekend": r["weekend"], "fri": friday_of(r["weekend"]), "name": nm, "dev_bps": f["dev_bps"],
                            "move_bps": mv, "spread_bps": f.get("spread_bps"), "dislocated": f.get("dislocated"),
                            "wick_ratio": f.get("wick_ratio"), "source": "ledger"})
    if backfill_rows is None:
        backfill_rows = _v2_backfill_rows(settings, fam, w)
    bf = [b for b in backfill_rows if b["fri"] < cur]
    led_fris = {r["fri"] for r in ledger_rows}
    fill = [{**b, "weekend": f"{b['fri']}|{b['name']}"} for b in bf if b["fri"] not in led_fris]
    rows = sorted(ledger_rows + fill, key=lambda h: (h["fri"], h["name"]))
    out = {"rows": rows, "backfill_missing": not bf, "n_ledger_weekends": len(led_fris),
           "n_backfill_weekends": len({b["fri"] for b in fill})}
    if not bf:
        log.warning("v2 停手：%s 族没有回补数据，只用账本 %d 个周末", fam, len(led_fris))
    return out


def _v2_backfill_rows(settings, fam: str, w: Weekend) -> list[dict]:
    """同族每个合约的回补周末行（strategy_lab 同一套：只读本地缓存，15m 优先、退回 1h），只取在本周末决策前已平仓的。"""
    from . import strategy_lab as sl
    out = []
    for inst in settings.instruments:
        if model.instrument_family(inst["coin"]) != fam:
            continue
        try:
            cbi, _ = sl.load_candles(settings, inst["coin"], fetch=False)
            rows = sl.weekend_rows(settings, inst, cbi, w.decision) if cbi else []
        except Exception as e:  # noqa: BLE001
            log.warning("v2 停手：读 %s 回补缓存失败：%s", inst["name"], e)
            continue
        out += [{"fri": r["fri"], "name": r["name"], "dev_bps": r["dev_bps"], "move_bps": r["move_bps"],
                 "spread_bps": None, "source": f"backfill_{r['gran']}"} for r in rows]
    return out


def job_outcome(settings, w: Weekend, push: bool = True) -> dict:
    led = Ledger(settings.data)
    wk = led.weekend(w.wid)
    if "outcome" in wk:
        return wk["outcome"]
    lock = wk.get("lock")
    if not lock or not lock["features"].get("ok"):
        rec = led.append("outcome", w.wid, {"ok": False, "problems": ["没有有效的锁定预测"]})
        return rec
    out = evaluate.outcome(settings, w, lock)
    out["detected_switch"] = detect_switch(settings, w)
    rec = led.append("outcome", w.wid, out)
    if push:
        notify.push(settings, f"重开结果 {w.wid}", report.outcome_text(lock, rec))
    return rec


def job_report(settings, w: Weekend) -> Path:
    """周报：覆盖这个周五的全部合约。"""
    path = report.weekly(settings, w)
    led = Ledger(settings.data)
    card = evaluate.scorecard(led.completed(), settings)
    body = report.card_text(card)
    if settings["options"]["enabled"]:
        body += "\n\n期权研究：\n" + options.card_text(options.scorecard(settings))
    notify.push(settings, f"周报 {w.friday.isoformat()}", body)
    try:
        from . import pdfreport
        fri = w.friday.isoformat()
        notify.push_file(settings, f"周报 PDF {fri}", "完整周报见附件", pdfreport.build(settings), f"weekend-desk-week-{fri}.pdf")
    except Exception as e:  # noqa: BLE001
        log.warning("weekly pdf failed: %s", e)
    return path


def detect_switch(settings, w: Weekend) -> dict | None:
    """元数据作业（7.1）：恢复前后 30 分钟内，预言机最大跳动发生在什么时候。"""
    rows = features._ctx(settings, w.coin or settings.instrument, to_ns(w.resume) - int(1800e9), to_ns(w.resume) + int(1800e9))
    if len(rows) < 2:
        return None
    best = max(zip(rows, rows[1:]), key=lambda p: abs(float(p[1][1]["oraclePx"]) - float(p[0][1]["oraclePx"])))
    (r0, c0), (r1, c1) = best
    return {"at_utc": datetime.fromtimestamp(r1 / 1e9, tz=timezone.utc).isoformat(),
            "minutes_from_resume": (r1 - to_ns(w.resume)) / 60e9,
            "jump_bps": (float(c1["oraclePx"]) / float(c0["oraclePx"]) - 1) * 1e4}


def save_state(settings, done: set) -> None:
    p = Path(settings.data) / "state" / "jobs_done.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(done)))


def load_state(settings) -> set:
    p = Path(settings.data) / "state" / "jobs_done.json"
    return set(json.loads(p.read_text())) if p.exists() else set()


def job_daily_pdf(settings, day=None) -> bool:
    """每天推一份 PDF：昨天读了什么、预测/结果、累计成绩、模拟实盘。"""
    from . import pdfreport
    from zoneinfo import ZoneInfo
    pt = ZoneInfo("America/Los_Angeles")
    day = day or (datetime.now(timezone.utc).astimezone(pt).date())
    data = pdfreport.build(settings, daily_for=day)
    return notify.push_file(settings, f"每日报告 {day:%m-%d}", "昨天读了什么、预测了什么、结果和模拟实盘，见附件",
                            data, f"weekend-desk-daily-{day.isoformat()}.pdf")
