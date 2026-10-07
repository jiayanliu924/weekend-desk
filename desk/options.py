"""第二阶段：加密期权的事件波动率研究（备忘第九章）。

问题：宏观事件（美联储议息、CPI、非农）之前，期权价格里隐含的波动，是否系统性高于或低于随后实际走出来的波动？
只做研究：不做市、不卖期权、不下单。

每个样本：
  锁定时刻  算出未来 24 小时的"隐含波动"（用 Deribit 各到期日平值期权的隐含波动，按总方差插值到 24 小时）
  窗口结束  用 Deribit BTC 指数价算"实际波动"，比较 ln(实际 / 隐含)
样本两类：事件（事件前 60 分钟锁定）和每日对照（每天 08:00 UTC 锁定，窗口内无事件的才算对照组）。
基线 A：隐含波动是公允的（预测 ln 比值 = 0）。基线 B：同类样本的历史平均比值。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from . import extract, notify, rawstore
from .clock import to_ns
from .ledger import Ledger
from .rawstore import RawWriter, now_ns

log = logging.getLogger("options")
API = "https://www.deribit.com/api/v2/public"
UTC = timezone.utc
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
NAME = re.compile(r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:d\d+)?)-([CP])$")


# ---------------------------------------------------------------- calendar
@dataclass(frozen=True)
class Sample:
    sid: str            # "EVT-CPI-2026-10" or "DAY-2026-10-07"
    kind: str           # "event" | "daily"
    event_type: str     # CPI / FOMC / NFP / none
    event_at: datetime | None
    lock: datetime
    end: datetime


def load_events(root: Path) -> dict:
    p = Path(root) / "events.toml"
    return tomllib.loads(p.read_text()) if p.exists() else {"events": [], "past_fomc": []}


def event_samples(settings) -> list[Sample]:
    o = settings["options"]
    tz = ZoneInfo(settings["clock"]["timezone"])
    out = []
    for e in load_events(settings.root)["events"]:
        at = datetime.strptime(e["at"], "%Y-%m-%d %H:%M").replace(tzinfo=tz).astimezone(UTC)
        lock = at - timedelta(minutes=o["event_lead_min"])
        out.append(Sample(f"EVT-{e['id']}", "event", e["type"], at, lock, lock + timedelta(hours=o["window_hours"])))
    return out


def daily_sample(day, settings) -> Sample:
    o = settings["options"]
    h, m = map(int, o["daily_lock_utc"].split(":"))
    lock = datetime(day.year, day.month, day.day, h, m, tzinfo=UTC)
    return Sample(f"DAY-{day.isoformat()}", "daily", "none", None, lock, lock + timedelta(hours=o["window_hours"]))


def events_in(settings, a: datetime, b: datetime) -> list[Sample]:
    return [e for e in event_samples(settings) if a <= e.event_at < b]


# ---------------------------------------------------------------- collection
class OptionsCollector:
    def __init__(self, settings):
        self.s = settings
        self.o = settings["options"]
        self.w = {n: RawWriter(settings.data, n) for n in ("drb_opts", "drb_index", "drb_dvol")}

    async def run(self):
        async with httpx.AsyncClient(timeout=30) as cli:
            await asyncio.gather(self._index_loop(cli), self._opts_loop(cli))

    async def _index_loop(self, cli):
        cur = self.o["currency"].lower()
        while True:
            try:
                r = (await cli.get(f"{API}/get_index_price", params={"index_name": f"{cur}_usd"})).json()["result"]
                self.w["drb_index"].add("deribit", cur, r)
            except Exception as e:  # noqa: BLE001
                log.warning("index: %s", e)
            await asyncio.sleep(self.o["index_every_sec"])

    async def _opts_loop(self, cli):
        cur = self.o["currency"]
        while True:
            try:
                res = (await cli.get(f"{API}/get_book_summary_by_currency",
                                     params={"currency": cur, "kind": "option"})).json()["result"]
                keep = filter_chain(res, datetime.now(UTC), self.o["max_expiry_days"], self.o["moneyness_band"])
                self.w["drb_opts"].add("deribit", cur, {"n_total": len(res), "items": keep})
                end = int(datetime.now(UTC).timestamp() * 1000)
                d = (await cli.get(f"{API}/get_volatility_index_data",
                                   params={"currency": cur, "start_timestamp": end - 600_000,
                                           "end_timestamp": end, "resolution": 60})).json()["result"]
                if d.get("data"):
                    self.w["drb_dvol"].add("deribit", cur, {"row": d["data"][-1]})
            except Exception as e:  # noqa: BLE001
                log.warning("options: %s", e)
            for w in self.w.values():
                w.flush()
            await asyncio.sleep(self.o["poll_every_sec"])


def parse_name(name: str):
    m = NAME.match(name)
    if not m:
        return None
    _, d, mon, yy, strike, cp = m.groups()
    exp = datetime(2000 + int(yy), MONTHS[mon], int(d), 8, 0, tzinfo=UTC)
    return exp, float(strike.replace("d", ".")), cp


def filter_chain(items: list[dict], now: datetime, max_days: float, band: float) -> list[dict]:
    out = []
    for it in items:
        p = parse_name(it.get("instrument_name", ""))
        if not p or it.get("mark_iv") is None or not it.get("underlying_price"):
            continue
        exp, k, cp = p
        if not (now < exp <= now + timedelta(days=max_days)):
            continue
        if abs(k / it["underlying_price"] - 1) > band:
            continue
        out.append({"n": it["instrument_name"], "iv": it["mark_iv"], "u": it["underlying_price"],
                    "b": it.get("bid_price"), "a": it.get("ask_price"), "oi": it.get("open_interest")})
    return out


# ---------------------------------------------------------------- math
def atm_term_structure(items: list[dict], now: datetime) -> list[tuple[float, float]]:
    """[(T_years, atm_iv_decimal)] per expiry, ATM = strike nearest that expiry's underlying."""
    by_exp: dict[datetime, list[tuple[float, str, float, float]]] = {}
    for it in items:
        p = parse_name(it["n"])
        if not p:
            continue
        exp, k, cp = p
        by_exp.setdefault(exp, []).append((k, cp, it["iv"], it["u"]))
    out = []
    for exp, rows in sorted(by_exp.items()):
        u = rows[0][3]
        kstar = min({r[0] for r in rows}, key=lambda k: abs(k - u))
        ivs = [r[2] for r in rows if r[0] == kstar]
        T = (exp - now).total_seconds() / (365 * 86400)
        if T > 0 and ivs:
            out.append((T, sum(ivs) / len(ivs) / 100))
    return out


def implied_vol_for(ts: list[tuple[float, float]], hours: float) -> float | None:
    """Interpolate total variance (iv²·T) linearly in T to the window length."""
    if not ts:
        return None
    Th = hours / (365 * 24)
    if Th <= ts[0][0]:
        return ts[0][1]
    for (t0, v0), (t1, v1) in zip(ts, ts[1:]):
        if t0 <= Th <= t1:
            w0, w1 = v0 * v0 * t0, v1 * v1 * t1
            wv = w0 + (w1 - w0) * (Th - t0) / (t1 - t0)
            return math.sqrt(max(wv, 0) / Th)
    return ts[-1][1]


def realized_vol(prices: list[tuple[int, float]], start_ns: int, end_ns: int) -> tuple[float | None, float]:
    """Annualized realized vol from squared log returns; returns (vol, coverage 0..1)."""
    pts = [(t, p) for t, p in prices if start_ns <= t <= end_ns and p > 0]
    if len(pts) < 10:
        return None, 0.0
    ss = sum(math.log(b / a) ** 2 for (_, a), (_, b) in zip(pts, pts[1:]))
    span = (pts[-1][0] - pts[0][0])
    coverage = span / (end_ns - start_ns)
    years = span / 1e9 / (365 * 86400)
    return (math.sqrt(ss / years) if years > 0 else None), coverage


# ---------------------------------------------------------------- data access
def chain_at(settings, t_ns: int):
    rows = rawstore.query(settings.data, "drb_opts", select="received_at, payload",
                          sql_where=f"received_at <= {t_ns} AND received_at > {t_ns - int(1800e9)}")
    return (rows[-1][0], json.loads(rows[-1][1])["items"]) if rows else (None, None)


def index_series(settings, a_ns: int, b_ns: int) -> list[tuple[int, float]]:
    rows = rawstore.query(settings.data, "drb_index", select="received_at, payload",
                          sql_where=f"received_at >= {a_ns} AND received_at <= {b_ns}")
    return [(r, float(json.loads(p)["index_price"])) for r, p in rows]


def dvol_at(settings, t_ns: int) -> float | None:
    rows = rawstore.query(settings.data, "drb_dvol", select="payload",
                          sql_where=f"received_at <= {t_ns} AND received_at > {t_ns - int(1800e9)}")
    return float(json.loads(rows[-1][0])["row"][4]) / 100 if rows else None


def macro_events_before(settings, t_ns: int, hours: float = 24) -> int:
    evs = extract.load_events(settings, t_ns, t_ns - int(hours * 3600e9))
    return sum(1 for e in evs if e["materiality"] >= 2 and e.get("event_type") in ("宏观", "监管", "地缘"))


# ---------------------------------------------------------------- model
def _history(led: Ledger, settings, group: str) -> list[float]:
    out = []
    for r in completed(led):
        if official(r) and group_of(r["lock"]) == group:
            out.append(r["outcome"]["log_ratio"])
    return out


def group_of(lock: dict) -> str:
    if lock["sample_kind"] == "event":
        return "event"
    return "daily_with_event" if lock.get("has_event") else "control"


def predict(hist: list[float], n_macro: int, rows_for_ols: list[tuple[float, int]], cfg) -> dict:
    o = cfg["options"]
    base_b = sum(hist) / len(hist) if hist else 0.0
    if len(rows_for_ols) >= 30:
        X = np.array([[1.0, n] for _, n in rows_for_ols])
        y = np.array([v for v, _ in rows_for_ols])
        c, *_ = np.linalg.lstsq(X, y, rcond=None)
        return {"pred": float(c[0] + c[1] * n_macro), "model_version": f"ols-macro-n{len(rows_for_ols)}", "baseline_B": base_b}
    if len(hist) >= o["min_history"]:
        return {"pred": base_b, "model_version": f"mean-n{len(hist)}", "baseline_B": base_b}
    return {"pred": 0.0, "model_version": "fair-v0", "baseline_B": base_b}


# ---------------------------------------------------------------- jobs
def job_lock(settings, smp: Sample, extractor=None, clock=None) -> dict:
    led = Ledger(settings.data, "options")
    existing = [r for r in led.all() if r["weekend"] == smp.sid and r["kind"] == "lock"]
    if existing:
        return existing[0]
    t = to_ns(smp.lock)
    # make sure the past day's news has been turned into an event table before locking (point-in-time)
    try:
        kw = {"clock": clock} if clock else {}
        extract.run_extraction(settings, t - int(24 * 3600e9), t, extractor=extractor, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("extraction before options lock failed: %s", e)
    snap_at, items = chain_at(settings, t)
    problems = []
    iv = None
    if not items:
        problems.append("锁定前 30 分钟内没有期权快照")
    else:
        iv = implied_vol_for(atm_term_structure(items, smp.lock), settings["options"]["window_hours"])
        if iv is None:
            problems.append("算不出平值隐含波动")
    inside = events_in(settings, smp.lock, smp.end)
    n_macro = macro_events_before(settings, t)
    lock = {"sample_kind": smp.kind, "event_type": smp.event_type,
            "event_at": smp.event_at.isoformat() if smp.event_at else None,
            "window_start": smp.lock.isoformat(), "window_end": smp.end.isoformat(),
            "implied_vol": iv, "dvol": dvol_at(settings, t), "snapshot_received_at": snap_at,
            "has_event": bool(inside), "events_in_window": [e.sid for e in inside], "n_macro_events_24h": n_macro,
            "ok": not problems, "problems": problems, "prompt_version": settings["llm"]["prompt_version"]}
    if lock["ok"]:
        hist = _history(led, settings, group_of(lock))
        rows = [(r["outcome"]["log_ratio"], r["lock"].get("n_macro_events_24h", 0))
                for r in completed(led) if official(r)]
        lock["prediction"] = predict(hist, n_macro, rows, settings)
        if iv:
            lock["implied_move_bps"] = iv * math.sqrt(settings["options"]["window_hours"] / (365 * 24)) * 1e4
    rec = led.append("lock", smp.sid, lock)
    if smp.kind == "event":
        notify.push(settings, f"期权研究：已锁定 {smp.sid}", lock_text(rec), priority="high")
    return rec


def job_outcome(settings, smp: Sample) -> dict:
    led = Ledger(settings.data, "options")
    w = {r["kind"]: r for r in led.all() if r["weekend"] == smp.sid}
    if "outcome" in w:
        return w["outcome"]
    lock = w.get("lock")
    if not lock or not lock.get("ok"):
        return led.append("outcome", smp.sid, {"ok": False, "problems": ["没有有效的锁定"]})
    a, b = to_ns(smp.lock), to_ns(smp.end)
    px = index_series(settings, a - int(120e9), b + int(120e9))
    rv, cov = realized_vol(px, a, b)
    if rv is None or cov < 0.8:
        return led.append("outcome", smp.sid, {"ok": False, "problems": [f"窗口内指数数据覆盖率 {cov:.0%}，不足 80%"]})
    iv = lock["implied_vol"]
    lr = math.log(rv / iv)
    p = lock["prediction"]
    first = min(px, key=lambda x: abs(x[0] - a))[1]
    last = min(px, key=lambda x: abs(x[0] - b))[1]
    out = {"ok": True, "realized_vol": rv, "coverage": cov, "log_ratio": lr, "ratio": rv / iv,
           "abs_move_bps": abs(math.log(last / first)) * 1e4,
           "err_model": abs(p["pred"] - lr), "err_A": abs(lr), "err_B": abs(p["baseline_B"] - lr)}
    rec = led.append("outcome", smp.sid, out)
    if smp.kind == "event":
        notify.push(settings, f"期权研究：结果 {smp.sid}", outcome_text(lock, rec))
    return rec


SURPRISE_PROMPT = """你是一个信息抽取器。只根据下面的原文判断，不得使用你自己的背景知识。
原文是否明确写出了这次 {etype} 公布的实际值（或决定）与市场预期？
只输出一个 JSON 对象：
{{"actual": "原文写的实际值/决定，没有则 null", "expected": "原文写的预期，没有则 null",
  "surprise_direction": "高于预期|低于预期|符合预期|不明", "surprise_size": 0-3,
  "evidence_span": "原文中支持判断的原句，逐字复制"}}"""

KEYWORDS = {"CPI": r"\bCPI\b|inflation|consumer price", "FOMC": r"\bFed\b|FOMC|rate decision|Powell|interest rate",
            "NFP": r"payroll|jobs report|nonfarm|unemployment rate"}


def job_surprise(settings, smp: Sample, extractor=None) -> dict:
    """事件后 N 小时内的新闻里，抽出"实际 vs 预期"的意外程度（复盘用，不参与事前预测）。"""
    led = Ledger(settings.data, "options")
    a = to_ns(smp.event_at)
    b = a + int(settings["options"]["surprise_hours"] * 3600e9)
    pat = re.compile(KEYWORDS.get(smp.event_type, smp.event_type), re.I)
    rows = rawstore.query(settings.data, "news", select="received_at, key, payload",
                          sql_where=f"received_at >= {a} AND received_at < {b}")
    extractor = extractor or extract.AnthropicExtractor(settings["llm"]["model"])
    found = []
    for r, k, p in rows[:40]:
        item = json.loads(p)
        text = extract.clean(item.get("title", "")) + "\n" + extract.clean(item.get("summary", ""))
        if not pat.search(text):
            continue
        try:
            raw = extractor(SURPRISE_PROMPT.format(etype=smp.event_type), "原文：\n" + text)
            m = re.search(r"\{.*\}", raw, re.S)
            j = json.loads(m.group(0)) if m else {}
        except Exception as e:  # noqa: BLE001
            log.warning("surprise extract: %s", e)
            continue
        span = extract.norm(str(j.get("evidence_span", "")))
        j["evidence_ok"] = bool(span) and span in extract.norm(text)
        if j["evidence_ok"] and j.get("surprise_direction") not in (None, "不明"):
            found.append({**j, "title": item.get("title"), "received_at": r})
    sizes = [int(x.get("surprise_size", 0) or 0) for x in found]
    return led.append("surprise", smp.sid, {"n_sources": len(found), "max_size": max(sizes, default=None),
                                            "items": found[:6]})


# ---------------------------------------------------------------- ledger helpers / scoring
def completed(led: Ledger) -> list[dict]:
    by: dict[str, dict] = {}
    for r in led.all():
        by.setdefault(r["weekend"], {})[r["kind"]] = r
    return [{"sid": k, **v} for k, v in sorted(by.items()) if "lock" in v and "outcome" in v]


def official(r: dict) -> bool:
    return bool(r["lock"].get("ok") and r["outcome"].get("ok"))


def _welch(a: list[float], b: list[float]):
    if len(a) < 3 or len(b) < 3:
        return None, None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return None, None
    t = (ma - mb) / se
    return t, math.erfc(abs(t) / math.sqrt(2))  # two-sided, normal approx


def scorecard(settings) -> dict:
    led = Ledger(settings.data, "options")
    recs = [r for r in completed(led) if official(r)]
    ev = [r["outcome"]["log_ratio"] for r in recs if r["lock"]["sample_kind"] == "event"]
    ctl = [r["outcome"]["log_ratio"] for r in recs if group_of(r["lock"]) == "control"]
    res = {"n_event": len(ev), "n_control": len(ctl)}
    gm = lambda xs: math.exp(sum(xs) / len(xs)) if xs else None  # noqa: E731
    res["event_ratio"], res["control_ratio"] = gm(ev), gm(ctl)
    t, p = _welch(ev, ctl)
    res["t_event_vs_control"], res["p_event_vs_control"] = t, p
    em = [r for r in recs if r["lock"]["sample_kind"] == "event"]
    if em:
        res["event_err"] = {k: sum(r["outcome"][f"err_{k}"] for r in em) / len(em) for k in ("model", "A", "B")}
    need = settings["options"]["eval_after_events"]
    if len(ev) >= need and p is not None:
        if p < 0.05:
            res["verdict"] = f"事件日隐含波动相对实际系统性偏{'低' if t > 0 else '高'}（显著）"
        else:
            res["verdict"] = "事件日与普通日没有显著差异：隐含波动对事件的定价大体公允"
    else:
        res["verdict"] = f"事件样本 {len(ev)}/{need}，还不够下结论"
    return res


def _pct(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def lock_text(rec: dict) -> str:
    if not rec.get("ok"):
        return "数据不全：" + "；".join(rec["problems"])
    p = rec["prediction"]
    return (f"{rec['event_type']} 事件前锁定：未来 24 小时隐含波动 {_pct(rec['implied_vol'])}（年化），"
            f"约合 ±{rec.get('implied_move_bps', 0):.0f} bps。\n预测 实际/隐含 = {math.exp(p['pred']):.2f}（{p['model_version']}）。\n"
            f"锁定哈希 {rec['record_sha256'][:16]}")


def outcome_text(lock: dict, rec: dict) -> str:
    if not rec.get("ok"):
        return "；".join(rec["problems"])
    verdict = "期权定价偏贵（实际波动小于隐含）" if rec["ratio"] < 1 else "期权定价偏便宜（实际波动大于隐含）"
    return (f"实际波动 {_pct(rec['realized_vol'])} vs 隐含 {_pct(lock['implied_vol'])}，比值 {rec['ratio']:.2f}：{verdict}。\n"
            f"24 小时实际涨跌 {rec['abs_move_bps']:.0f} bps。")


def card_text(c: dict) -> str:
    f = lambda x: "—" if x is None else f"{x:.2f}"  # noqa: E731
    lines = [f"事件样本 {c['n_event']} 个，平均 实际/隐含 = {f(c['event_ratio'])}；"
             f"无事件对照日 {c['n_control']} 个，平均 {f(c['control_ratio'])}。"]
    if c.get("event_err"):
        e = c["event_err"]
        lines.append(f"事件样本预测误差（ln 比值）：模型 {e['model']:.3f} / 基线A {e['A']:.3f} / 基线B {e['B']:.3f}")
    lines.append(c["verdict"])
    return "\n".join(lines)


def due(now: datetime, settings, done: set) -> list[tuple[str, Sample]]:
    if not settings["options"]["enabled"]:
        return []
    out = []
    smps = [daily_sample((now - timedelta(days=d)).date(), settings) for d in (0, 1, 2)] + event_samples(settings)
    for s_ in smps:
        k = lambda n: f"opt:{s_.sid}:{n}"  # noqa: E731
        if s_.lock <= now < s_.lock + timedelta(minutes=50) and k("lock") not in done:
            out.append((k("lock"), s_))
        if s_.end + timedelta(minutes=5) <= now < s_.end + timedelta(hours=6) and k("outcome") not in done:
            out.append((k("outcome"), s_))
        if (s_.kind == "event" and s_.event_at + timedelta(hours=settings["options"]["surprise_hours"]) <= now
                < s_.event_at + timedelta(hours=12) and k("surprise") not in done):
            out.append((k("surprise"), s_))
    return out


def run(key: str, smp: Sample, settings) -> None:
    name = key.rsplit(":", 1)[1]
    {"lock": job_lock, "outcome": job_outcome, "surprise": job_surprise}[name](settings, smp)
