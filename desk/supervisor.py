"""巡检员（只读）：替 Kea 盯那些她一直在手工发现的问题。全部是代码判断，不用大模型（只有每日摘要最多调一次）。

绝不下单、不撤单、不改任何模式：只读账本 / 状态文件 / 行情，只会"推送提醒"。

checks(settings, now) -> [{level: info|warn|alert, key, title, body（大白话）, since}]
  1. 真钱线空转：套利 / 周末自动下单 / 美股期权，模式是真下单，但最近 48 小时（只算开市时间）一笔都没开 → warn，附前三个没开的原因
  2. 数据健康：24 小时内"抓数据超过 45s"次数（journalctl，没有就跳过）、采集数据多久没更新
  3. 亏损预算：总账户止损用掉 ≥50% warn、≥75% alert；套利单日止损同理
  4. 大盘（给期权线看）：SOXX / QQQ / SPY 连跌天数、5/10 日涨跌、离 20 日高点
  5. 磁盘 / 内存
每一项各自 try/except：某一项自己坏了 = 一条 warn，不会拖垮整个任务。

job_hourly：跑 checks，新出现 / 升级的 warn+alert 推手机；同一个 key 最多每 6 小时再推一次（升级除外）。状态在 data/state/supervisor.json。
job_daily：每天 07:05（加州）一条大白话日报；最多调一次大模型（lead_model），不可用就推代码拼的日报。
命令行：python -m desk.supervisor --check        只打印，不推送
        python -m desk.supervisor --daily --dry  只打印日报（不调大模型、不推送、不写状态）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import notify

log = logging.getLogger("supervisor")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
RANK = {"info": 0, "warn": 1, "alert": 2}
MODE_CN = {"off": "关闭", "shadow": "演习（不真下单）", "live": "真下单"}

DEFAULTS = {
    "enabled": True,
    "no_entry_market_h": 48.0,          # 真钱线连续多少小时（只算开市时间）没开仓就提醒
    "weekend_no_entry_days": 8.0,       # 周末自动下单线：多少天内没有下过单
    "timeout_warn_24h": 5, "timeout_alert_24h": 60,
    "ctx_stale_warn_min": 10.0, "ctx_stale_alert_min": 30.0, "news_stale_warn_min": 180.0,
    "decision_stale_min": 20.0,         # 套利线决策表多久没更新（开市时间、真下单模式）
    "loss_warn_frac": 0.5, "loss_alert_frac": 0.75,
    "regime_symbols": ["SOXX", "QQQ", "SPY"],
    "regime_down_days_warn": 4, "regime_down_days_alert": 5,
    "regime_change10_warn_pct": -5.0, "regime_change10_alert_pct": -8.0,
    "regime_drawdown_warn_pct": -7.0, "regime_drawdown_alert_pct": -10.0,
    "disk_free_min_pct": 15.0, "mem_avail_min_pct": 15.0,
    "repush_hours": 6.0, "max_repeats": 4,
    "daily_hour_pt": 7, "daily_minute_pt": 5,
}


def cfg(settings) -> dict:
    d = dict(DEFAULTS)
    try:
        d.update(settings.raw.get("supervisor") or {})
    except AttributeError:
        pass
    return d


def finding(level: str, key: str, title: str, body: str, since: str = "") -> dict:
    return {"level": level, "key": key, "title": title, "body": body, "since": since}


# ------------------------------------------------------------------ 开市时间（美东周五 17:00 → 周日 18:00 休市）
def _closed_windows(a: datetime, b: datetime) -> list[tuple[datetime, datetime]]:
    d = a.astimezone(ET).date() - timedelta(days=7)
    d -= timedelta(days=(d.weekday() - 4) % 7)          # 回到那一周的周五
    out = []
    while True:
        s = datetime.combine(d, dtime(17, 0), tzinfo=ET)
        e = datetime.combine(d + timedelta(days=2), dtime(18, 0), tzinfo=ET)
        if s >= b:
            break
        if e > a:
            out.append((max(s, a), min(e, b)))
        d += timedelta(days=7)
    return out


def market_hours(a: datetime, b: datetime) -> float:
    """a→b 之间有多少小时是开市的（扣掉周末休市）。"""
    if b <= a:
        return 0.0
    tot = (b - a).total_seconds()
    tot -= sum((e - s).total_seconds() for s, e in _closed_windows(a, b))
    return tot / 3600


def market_start(now: datetime, hours: float) -> datetime:
    """往回数 `hours` 个开市小时，起点在哪。"""
    lo, hi = now - timedelta(days=30), now
    for _ in range(40):
        mid = lo + (hi - lo) / 2
        if market_hours(mid, now) > hours:
            lo = mid
        else:
            hi = mid
    return hi


def _iso(ts: float | datetime | None) -> str:
    if ts is None or ts == 0:
        return ""
    if not isinstance(ts, datetime):
        ts = datetime.fromtimestamp(float(ts), UTC)
    return ts.astimezone(UTC).isoformat(timespec="seconds")


def _pt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(PT).strftime("%m-%d %H:%M")
    except ValueError:
        return "?"


# ------------------------------------------------------------------ 可注入的外部依赖（测试里全换成假的）
def _journal_timeouts() -> int | None:
    """过去 24 小时 journald 里"抓数据超过 / 上一次数据抓取还没结束"的行数。逐行流式数，不把日志整个读进内存。
    没有 journalctl / 没权限 / 有任何报错输出 / 超过 20 秒 → None（当作查不了，不当作健康）。"""
    import tempfile
    if not shutil.which("journalctl"):
        return None
    try:
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(["journalctl", "-u", "weekend-desk", "--since", "-24h", "--no-pager", "-o", "cat"],
                                    stdout=subprocess.PIPE, stderr=err, text=True, errors="replace")
            timer = threading.Timer(20.0, proc.kill)
            timer.start()
            n, denied = 0, False
            try:
                for ln in proc.stdout:
                    if "抓数据超过" in ln or "上一次数据抓取还没结束" in ln:
                        n += 1
                    elif "No journal files" in ln or "insufficient permissions" in ln:
                        denied = True
                rc = proc.wait()
            finally:
                timer.cancel()
                proc.stdout.close()
            err.seek(0)
            has_err = bool(err.read().strip())
        return None if (rc != 0 or has_err or denied) else n
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _freshness(settings, now: datetime) -> dict:
    """{流名: 最新一条是多少分钟前}；窗口内一条都没有 → None。和 `python -m desk status` 同一个口径（rawstore 里的 received_at 最大值）。"""
    from . import rawstore
    out = {}
    now_ns = int(now.timestamp() * 1e9)
    for stream, win_h in (("hl_ctx", 3), ("news", 24)):
        a = now_ns - int(win_h * 3600e9)
        rows = rawstore.query(settings.data, stream, select="max(received_at)", order="1",
                              sql_where=f"received_at >= {a}", a_ns=a, b_ns=now_ns + int(60e9))
        last = rows[0][0] if rows and rows[0][0] else None
        out[stream] = (now_ns - last) / 60e9 if last else None
    return out


def _alpaca_bars(settings, symbols: list[str], now: datetime) -> dict | None:
    """Alpaca 美股日线收盘价（只读行情端点）。没有 key → None。"""
    from . import alpaca_exec as ax
    cl = ax.client(True) or ax.client(False)
    if cl is None:
        return None
    try:
        d = cl._req("GET", f"{ax.DATA_URL}/v2/stocks/bars",
                    params={"symbols": ",".join(symbols), "timeframe": "1Day", "limit": 1000, "feed": "iex",
                            "start": (now - timedelta(days=50)).strftime("%Y-%m-%d"), "adjustment": "split"})
    finally:
        try:
            cl.http.close()
        except Exception:  # noqa: BLE001
            pass
    return {s: [float(b["c"]) for b in (d.get("bars") or {}).get(s, [])] for s in symbols}


def _gate(settings) -> list[dict]:
    from . import alpaca_exec as ax
    return ax.gate_checks(settings, check_clock=False)


def _disk_free_pct(settings) -> float:
    u = shutil.disk_usage(str(settings.data))
    return u.free / u.total * 100


def _mem_avail_pct() -> float | None:
    try:
        kv = {}
        for ln in Path("/proc/meminfo").read_text().splitlines():
            k, v = ln.split(":", 1)
            kv[k] = float(v.split()[0])
        return kv["MemAvailable"] / kv["MemTotal"] * 100
    except (OSError, KeyError, ValueError):
        return None


@dataclass
class Env:
    timeouts_24h: Callable[[], int | None] = _journal_timeouts
    freshness: Callable = _freshness
    bars: Callable = _alpaca_bars
    gate: Callable = _gate
    disk_free_pct: Callable = _disk_free_pct
    mem_avail_pct: Callable = _mem_avail_pct


# ------------------------------------------------------------------ 状态文件（去重 / 限频 / 给网站看的最近结果）
def _state_path(settings) -> Path:
    p = Path(settings.data) / "state" / "supervisor.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_state(settings) -> dict:
    d = {"sent": {}, "first_seen": {}, "daily": {}, "last": {}}
    try:
        d.update(json.loads(_state_path(settings).read_text()))
    except (OSError, ValueError):
        pass
    return d


def save_state(settings, st: dict) -> None:
    p = _state_path(settings)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, p)


# ------------------------------------------------------------------ 三条线的信息（检查和日报共用）
def _reason_key(r: str) -> str:
    r = (r or "").strip()
    if re.match(r"^价差\s*[\d.]+\s*<\s*门槛", r):
        return "价差还没到开仓门槛"
    return re.sub(r"\d+(\.\d+)?", "N", r)[:60]


def _top_reasons(raw: list[str], n: int = 3) -> list[tuple[str, int]]:
    c = Counter(_reason_key(r) for r in raw if r)
    return c.most_common(n)


def _fmt_reasons(top: list[tuple[str, int]]) -> str:
    return "；".join(f"{i}）{r}（{c} 次）" for i, (r, c) in enumerate(top, 1))


def pairs_info(settings, now: datetime, env: Env) -> dict:
    from . import pairs_exec as px
    c = cfg(settings)
    g = px.get(settings)
    mode = g["mode"]
    cut = market_start(now, float(c["no_entry_market_h"]))
    st = px.load_state(settings)
    opens_ms = sorted(float(p["open"].get("t_ms") or 0) for p in st["pairs"].values())
    recent = [t for t in opens_ms if t / 1000 >= cut.timestamp()]
    reasons: list[str] = []
    dec_age_min = None
    try:
        doc = json.loads(px.decision_path(settings).read_text())
        dec_age_min = (now.timestamp() * 1000 - float(doc.get("ts_ms") or 0)) / 60000
        reasons += [r.get("reason", "") for r in doc.get("pairs", []) if not r.get("entered")]
    except (OSError, ValueError):
        pass
    cut_ns = cut.timestamp() * 1e9
    try:
        reasons += [r.get("reason", "") for r in px._led(settings).all()
                    if r["kind"] == "skip" and float(r.get("written_at") or 0) >= cut_ns]
    except Exception as e:  # noqa: BLE001
        log.info("pairs skip rows: %s", e)
    day0 = px._day_start_ms(int(now.timestamp() * 1000))
    day_pnl = sum(p["closed"].get("pnl_usd", 0.0) or 0.0 for p in st["pairs"].values()
                  if p["closed"] and p["closed"].get("t_ms", 0) >= day0)
    return {"mode": mode, "mode_ts": float(g.get("ts") or 0), "halt": bool(g.get("halt_entries")),
            "halt_reason": g.get("halt_reason", ""), "entries": len(recent),
            "last_entry_ts": (opens_ms[-1] / 1000) if opens_ms else 0.0, "open_now": len(px.open_pairs(st)),
            "reasons": _top_reasons(reasons), "dec_age_min": dec_age_min, "day_pnl": day_pnl,
            "daily_stop": float(px.cfg(settings)["daily_loss_stop_usd"]), "cut": cut}


def weekend_info(settings, now: datetime, env: Env) -> dict:
    from . import autotrade as at
    from .ledger import Ledger
    c = cfg(settings)
    g = at.get(settings)
    rows = Ledger(settings.data, "live").all()
    cut = now - timedelta(days=float(c["weekend_no_entry_days"]))
    ent = [r for r in rows if r["kind"] == "entry"]
    recent = [r for r in ent if float(r.get("written_at") or 0) >= cut.timestamp() * 1e9]
    sent = [r for r in recent if r.get("sent")]
    reasons = [r.get("skipped") or r.get("error") or "" for r in recent if not r.get("sent")]
    last = ent[-1] if ent else None
    return {"mode": g["mode"], "mode_ts": float(g.get("ts") or 0), "recent": len(recent), "sent": len(sent),
            "reasons": _top_reasons(reasons), "total_usd": at.realized_total(settings), "last": last, "cut": cut}


def options_info(settings, now: datetime, env: Env) -> dict:
    from . import alpaca_exec as ax
    from .ledger import Ledger
    c = cfg(settings)
    ac = ax.cfg(settings)
    cut = market_start(now, float(c["no_entry_market_h"]))
    rows = Ledger(settings.data, ax.LEDGER).all()
    cut_ns = cut.timestamp() * 1e9
    win = [r for r in rows if float(r.get("written_at") or 0) >= cut_ns]
    opens = [r for r in win if r["kind"] == "open" and (r.get("sent") or r.get("mode") == "dry")]
    skips = [r for r in win if r["kind"] == "skip"]
    gate = env.gate(settings) if ac["enabled"] else []
    fails = [x for x in gate if not x["ok"] and x["key"] != "clock"]
    if not ac["enabled"]:
        route = "off"
    elif not fails:
        route = "live"
    else:
        try:
            route = "paper" if ax.has_keys(True) else "dry"
        except Exception:  # noqa: BLE001
            route = "dry"
    open_rows = ax.open_positions(settings)
    return {"enabled": bool(ac["enabled"]), "route": route, "gate_total": len(gate), "gate_fails": fails,
            "opens": len(opens), "skips": len(skips), "reasons": _top_reasons([r.get("reason", "") for r in skips]),
            "open_now": len(open_rows), "last": rows[-1] if rows else None, "cut": cut}


ROUTE_CN = {"off": "没打开", "live": "会走真钱", "paper": "走纸面账户（不动真钱）", "dry": "只演练记录（没发单）"}


# ------------------------------------------------------------------ 检查 1：真钱线空转
def check_live_lines(settings, now: datetime, env: Env) -> list[dict]:
    out = []
    c = cfg(settings)
    h = float(c["no_entry_market_h"])
    p = pairs_info(settings, now, env)
    if p["mode"] == "live":
        started = p["mode_ts"]
        long_enough = started <= 0 or started <= p["cut"].timestamp()
        if long_enough and p["entries"] == 0:
            top = p["reasons"]
            why = (f"最近没开仓的原因，按次数排前三：{_fmt_reasons(top)}。" if top
                   else "账本和决策表里都找不到没开仓的原因，可能这条线根本没在跑，请看一眼服务是否正常。")
            extra = ""
            if p["halt"]:
                extra += f" 另外这条线处于「停开新仓」状态（原因：{p['halt_reason'] or '没写'}）。"
            if p["dec_age_min"] is not None and p["dec_age_min"] > 30:
                extra += f" 决策表已经 {p['dec_age_min']:.0f} 分钟没更新，数据可能抓不到。"
            since = max(started, p["last_entry_ts"], p["cut"].timestamp() if not started else 0)
            out.append(finding("warn", "live.pairs.no_entry", f"套利线开着真下单，但 {h:.0f} 小时内一笔都没开",
                               f"套利线现在是真下单模式，最近 {h:.0f} 小时（周末休市不算）一笔新仓都没开。{why}{extra}"
                               "没开不一定是坏事（可能价差一直没到线），但值得看一眼原因是不是合理。", _iso(since)))
    w = weekend_info(settings, now, env)
    if w["mode"] == "live":
        days = float(c["weekend_no_entry_days"])
        long_enough = w["mode_ts"] <= 0 or w["mode_ts"] <= w["cut"].timestamp()
        if w["recent"] and not w["sent"]:
            out.append(finding("warn", "live.weekend.no_entry", f"周末自动下单开着真下单，但最近 {days:.0f} 天一单都没下",
                               f"最近 {days:.0f} 天有 {w['recent']} 次下单机会，全部被跳过了。原因前三：{_fmt_reasons(w['reasons'])}。"
                               "如果每个周末都是同一个原因，说明这条线可能被某个条件一直卡住。",
                               _iso(w["cut"])))
        elif not w["recent"] and long_enough:
            out.append(finding("warn", "live.weekend.no_entry", f"周末自动下单开着真下单，但最近 {days:.0f} 天连一次下单尝试都没有",
                               "账本里没有任何下单或跳过的记录，可能周末流程没跑起来。", _iso(w["cut"])))
    o = options_info(settings, now, env)
    if o["route"] == "live" and o["opens"] == 0 and o["skips"]:
        out.append(finding("warn", "live.options.no_entry", f"美股期权线能走真钱，但 {h:.0f} 小时内没下单",
                           f"期间出现了 {o['skips']} 次下单机会，都被跳过。原因前三：{_fmt_reasons(o['reasons'])}。", _iso(o["cut"])))
    return out


# ------------------------------------------------------------------ 检查 2：数据健康
def check_data_health(settings, now: datetime, env: Env) -> list[dict]:
    out, c = [], cfg(settings)
    n = env.timeouts_24h()
    if n is None:
        out.append(finding("info", "data.timeouts.unavailable", "查不了数据超时次数", "服务器上读不到系统日志，这一项跳过。"))
    elif n >= int(c["timeout_warn_24h"]):
        lvl = "alert" if n >= int(c["timeout_alert_24h"]) else "warn"
        out.append(finding(lvl, "data.timeouts", f"过去 24 小时抓数据超时 {n} 次",
                           f"套利线抓行情数据超时（等太久没回来）已经 {n} 次。超时的那几分钟套利线是瞎的：不会开新仓，"
                           "已有仓位的平仓和止损仍然照跑。如果次数还在涨，可能是网络或交易所接口慢。"))
    try:
        fr = env.freshness(settings, now)
    except Exception as e:  # noqa: BLE001
        fr = {}
        out.append(finding("warn", "data.freshness.error", "读不到数据采集的新鲜度", f"{type(e).__name__}: {str(e)[:150]}"))
    if "hl_ctx" in fr:
        m = fr["hl_ctx"]
        if m is None or m >= float(c["ctx_stale_warn_min"]):
            lvl = "alert" if m is None or m >= float(c["ctx_stale_alert_min"]) else "warn"
            out.append(finding(lvl, "data.hl_ctx.stale", "行情采集停了" if lvl == "alert" else "行情采集变慢了",
                               "交易所行情（hl_ctx）" + ("最近 3 小时一条都没收到。" if m is None else f"最新一条是 {m:.0f} 分钟前的。")
                               + "平时应该每几十秒一条。所有策略都靠这份数据，请先看采集服务。"))
    if "news" in fr:
        m = fr["news"]
        if m is None or m >= float(c["news_stale_warn_min"]):
            out.append(finding("warn", "data.news.stale", "新闻采集太久没更新",
                               "新闻" + ("最近 24 小时一条都没收到。" if m is None else f"最新一条是 {m / 60:.1f} 小时前的。")
                               + "新闻源可能挂了，事件类策略会失明。"))
    p = pairs_info(settings, now, env)
    if p["mode"] == "live" and p["dec_age_min"] is not None and p["dec_age_min"] > float(c["decision_stale_min"]):
        if market_hours(now - timedelta(minutes=p["dec_age_min"]), now) > 0.9 * p["dec_age_min"] / 60:   # 休市时本来就不更新
            out.append(finding("warn", "data.pairs_decision.stale", f"套利线的决策表 {p['dec_age_min']:.0f} 分钟没更新",
                               "套利线正常每分钟会评估一次并更新决策表。这么久没更新，通常是抓数据一直超时或者程序卡住了。"))
    return out


# ------------------------------------------------------------------ 检查 3：亏损预算
def check_loss_budget(settings, now: datetime, env: Env) -> list[dict]:
    from . import account_stop
    out, c = [], cfg(settings)
    comp = account_stop.components(settings)
    lim = account_stop.limit(settings)
    if account_stop.tripped(settings):
        out.append(finding("alert", "loss.account.tripped", "总账户止损已经触发，所有自动下单已停",
                           f"累计真实亏损已到上限 ${lim:.0f}，周末线和套利线都已切到关闭。要恢复需要你手动解除。"))
    else:
        used = -comp["total_usd"] / lim if lim else 0.0
        if used >= float(c["loss_warn_frac"]):
            lvl = "alert" if used >= float(c["loss_alert_frac"]) else "warn"
            out.append(finding(lvl, "loss.account", f"总亏损预算已用掉 {used * 100:.0f}%",
                               f"周末线加套利线累计已实现盈亏 ${comp['total_usd']:+.2f}（周末 ${comp['weekend_usd']:+.2f}，套利 ${comp['pairs_usd']:+.2f}），"
                               f"总止损线是 −${lim:.0f}，到线就会全部停手。只算已平仓的，还开着的仓位不在里面。"))
    p = pairs_info(settings, now, env)
    stop = p["daily_stop"]
    if stop and p["day_pnl"] < 0:
        used = -p["day_pnl"] / stop
        if used >= float(c["loss_warn_frac"]):
            lvl = "alert" if used >= float(c["loss_alert_frac"]) else "warn"
            out.append(finding(lvl, "loss.pairs_daily", f"套利线今天的亏损预算已用掉 {used * 100:.0f}%",
                               f"套利线今天已平仓部分亏了 ${-p['day_pnl']:.2f}，单日止损线是 ${stop:.0f}，到线会平掉所有套利仓位并关闭这条线。"
                               "（这里只算已平仓的；系统自己的止损还会把没平的仓位浮亏算进去。）"))
    return out


# ------------------------------------------------------------------ 检查 4：大盘（给期权线看）
def regime_metrics(closes: list[float]) -> dict | None:
    if len(closes) < 11:
        return None
    down = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            down += 1
        else:
            break
    last = closes[-1]
    return {"down_days": down, "chg5": (last / closes[-6] - 1) * 100, "chg10": (last / closes[-11] - 1) * 100,
            "dd20": (last / max(closes[-20:]) - 1) * 100, "last": last}


SYM_CN = {"SOXX": "半导体指数 SOXX", "QQQ": "纳指 QQQ", "SPY": "标普 SPY"}


def options_status_text(o: dict) -> str:
    if not o["enabled"]:
        return "期权线现在没打开（配置里 enabled=false），不会下单。"
    fails = o["gate_fails"]
    s = f"期权线现在：{ROUTE_CN[o['route']]}"
    if fails:
        s += f"（真钱 {o['gate_total']} 道关还有 {len(fails)} 道没过：{'、'.join(x['name'] for x in fails[:3])}）"
    return s + f"；未平仓位 {o['open_now']} 个。"


def check_regime(settings, now: datetime, env: Env) -> list[dict]:
    c = cfg(settings)
    syms = list(c["regime_symbols"])
    bars = env.bars(settings, syms, now)
    if bars is None:
        return [finding("info", "regime.nokeys", "没法看大盘走势", "没有 Alpaca 的 key，拿不到美股日线，这一项跳过。")]
    worst, lines = "info", []
    for s in syms:
        m = regime_metrics([float(x) for x in bars.get(s) or []])
        if not m:
            continue
        lvl = "info"
        if m["down_days"] >= int(c["regime_down_days_warn"]) or m["chg10"] <= float(c["regime_change10_warn_pct"]) \
                or m["dd20"] <= float(c["regime_drawdown_warn_pct"]):
            lvl = "warn"
        if m["down_days"] >= int(c["regime_down_days_alert"]) or m["chg10"] <= float(c["regime_change10_alert_pct"]) \
                or m["dd20"] <= float(c["regime_drawdown_alert_pct"]):
            lvl = "alert"
        if lvl != "info":
            lines.append(f"{SYM_CN.get(s, s)}已连跌 {m['down_days']} 天，近 5 个交易日 {m['chg5']:+.1f}%、近 10 个交易日 {m['chg10']:+.1f}%，"
                         f"比近 20 日最高点低 {abs(m['dd20']):.1f}%。")
            if RANK[lvl] > RANK[worst]:
                worst = lvl
    if worst == "info":
        return []
    o = options_info(settings, now, env)
    return [finding(worst, "regime", "大盘在明显下跌，期权线值得看一眼",
                    "".join(lines) + options_status_text(o) + "大盘连续下跌时，期权价格和事件冲击都会和平时不一样，"
                    "建议确认期权线的参数和风险上限是否还合适。（日线含今天还没收盘的那一根。）")]


# ------------------------------------------------------------------ 检查 5：磁盘 / 内存
def check_resources(settings, now: datetime, env: Env) -> list[dict]:
    out, c = [], cfg(settings)
    d = env.disk_free_pct(settings)
    if d is not None and d < float(c["disk_free_min_pct"]):
        out.append(finding("alert" if d < float(c["disk_free_min_pct"]) / 2 else "warn", "res.disk",
                           f"磁盘只剩 {d:.0f}% 空闲", "服务器磁盘快满了，满了以后数据写不进去、程序会出错。需要清理旧数据或扩容。"))
    m = env.mem_avail_pct()
    if m is not None and m < float(c["mem_avail_min_pct"]):
        out.append(finding("alert" if m < float(c["mem_avail_min_pct"]) / 2 else "warn", "res.mem",
                           f"可用内存只剩 {m:.0f}%", "服务器内存很紧（只有 2GB），再紧下去程序可能被系统强制关掉。看看是不是回测或别的程序占了内存。"))
    return out


CHECKS = [("live_lines", "真钱线是否空转", check_live_lines), ("data_health", "数据健康", check_data_health),
          ("loss_budget", "亏损预算", check_loss_budget), ("regime", "大盘走势", check_regime),
          ("resources", "磁盘和内存", check_resources)]


def checks(settings, now: datetime | None = None, env: Env | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    env = env or Env()
    out: list[dict] = []
    for name, label, fn in CHECKS:
        try:
            out += fn(settings, now, env)
        except Exception as e:  # noqa: BLE001
            log.warning("supervisor check %s failed: %s", name, e)
            out.append(finding("warn", f"check_failed.{name}", f"巡检项「{label}」自己出错了",
                               f"这一项没检查成（{type(e).__name__}: {str(e)[:150]}），其余检查照常。"))
    st = load_state(settings)
    for f in out:                     # 问题第一次被看到的时间（只读，不写状态）
        if not f["since"]:
            f["since"] = st["first_seen"].get(f["key"]) or _iso(now)
    out.sort(key=lambda f: -RANK[f["level"]])
    return out


# ------------------------------------------------------------------ 每小时：去重 + 推送
def _due(st: dict, f: dict, now_ts: float, c: dict) -> bool:
    s = st["sent"].get(f["key"])
    if not s:
        return True
    if RANK[f["level"]] > RANK[s["level"]]:
        return True
    if int(s.get("n", 1)) >= int(c["max_repeats"]):
        return False
    return now_ts - float(s["ts"]) >= float(c["repush_hours"]) * 3600


def job_hourly(settings, now: datetime | None = None, env: Env | None = None) -> list[dict]:
    c = cfg(settings)
    if not c["enabled"]:
        return []
    now = now or datetime.now(UTC)
    fs = checks(settings, now, env)
    st = load_state(settings)
    now_ts = now.timestamp()
    pushed = []
    for f in fs:
        if RANK[f["level"]] < RANK["warn"] or not _due(st, f, now_ts, c):
            continue
        prev = st["sent"].get(f["key"])
        n = int(prev.get("n", 1)) + 1 if prev else 1       # 按 key 累计，不管级别（警报↔提醒来回跳也不能绕过上限）；升级仍然立刻推
        label = "警报" if f["level"] == "alert" else "提醒"
        notify.push(settings, f"巡检{label}：{f['title']}", f"{f['body']}\n（从 {_pt(f['since'])} 加州时间起）",
                    priority="high" if f["level"] == "alert" else "default")
        st["sent"][f["key"]] = {"ts": now_ts, "level": f["level"], "n": n}
        pushed.append(f)
    active = {f["key"] for f in fs if RANK[f["level"]] >= RANK["warn"]}
    st["first_seen"] = {k: v for k, v in {**{f["key"]: f["since"] for f in fs}, **st["first_seen"]}.items() if k in active}
    keep = float(c["repush_hours"]) * 3600
    st["sent"] = {k: v for k, v in st["sent"].items() if k in active or now_ts - float(v["ts"]) < keep}
    st["last"] = {"ts": _iso(now), "findings": [f for f in fs if RANK[f["level"]] >= RANK["warn"]]}
    save_state(settings, st)
    return pushed


# ------------------------------------------------------------------ 每日摘要
def line_status(settings, now: datetime, env: Env) -> dict[str, list[str]]:
    """每条线 3 行状态（代码生成）。"""
    out = {}

    def safe(name, fn):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = [f"状态读不出来：{type(e).__name__}: {str(e)[:100]}", "", ""]

    def pairs():
        p = pairs_info(settings, now, env)
        top = p["reasons"][0][0] if p["reasons"] else "暂无记录"
        return [f"模式：{MODE_CN.get(p['mode'], p['mode'])}" + ("，停开新仓中" if p["halt"] else ""),
                f"最近 {cfg(settings)['no_entry_market_h']:.0f} 小时（开市时间）开仓 {p['entries']} 笔；现在持仓 {p['open_now']} 对",
                f"今天已平仓盈亏 ${p['day_pnl']:+.2f}（单日止损 ${p['daily_stop']:.0f}）；最常见的没开仓原因：{top}"]

    def weekend():
        w = weekend_info(settings, now, env)
        last = w["last"]
        ltxt = "还没有下单记录"
        if last:
            ltxt = f"最近一次（周末 {last['weekend']}）" + ("已挂单" if last.get("sent") else f"没下：{(last.get('skipped') or last.get('error') or '演习')[:40]}")
        return [f"模式：{MODE_CN.get(w['mode'], w['mode'])}", f"累计真实盈亏 ${w['total_usd']:+.2f}", ltxt]

    def options():
        o = options_info(settings, now, env)
        last = o["last"]
        return [f"状态：{ROUTE_CN[o['route']]}",
                f"未平仓位 {o['open_now']} 个" + (f"；真钱 {o['gate_total']} 道关还有 {len(o['gate_fails'])} 道没过" if o["gate_total"] else ""),
                f"最近一条记录：{last['kind']}（{last['weekend']}）" if last else "还没有期权下单记录"]

    safe("套利线", pairs)
    safe("周末自动下单", weekend)
    safe("美股期权线", options)
    return out


def code_digest(fs: list[dict], status: dict[str, list[str]], now: datetime) -> str:
    L = [f"每日巡检（加州时间 {now.astimezone(PT):%m-%d %H:%M}）"]
    bad = [f for f in fs if RANK[f["level"]] >= RANK["warn"]]
    if bad:
        L.append("需要注意：")
        for f in bad:
            L.append(f"· {'【警报】' if f['level'] == 'alert' else '【提醒】'}{f['title']}：{f['body']}")
    else:
        L.append("没有需要处理的问题。")
    L.append("各条线现状：")
    for name, lines in status.items():
        L.append(f"▸ {name}：" + "；".join(x for x in lines if x))
    return "\n".join(L)


DAILY_SYSTEM = ("你是交易系统的巡检员，给一位不懂金融术语的老板写每天早上的手机推送。只用下面给的事实，不许编数字，不许给买卖建议。"
                "写 6-10 行大白话：先说「有没有要处理的事」（有就按紧急程度说清楚是什么、为什么要看），再用一两句话说三条线各自现在什么状态。"
                "不要标题、不要 markdown、不要英文缩写（SOXX 这类代码可以保留）。")


def job_daily(settings, now: datetime | None = None, env: Env | None = None, llm=None, dry: bool = False) -> str | None:
    """返回推送出去（或 dry 时本来会推送）的正文。每天最多调一次大模型，且每天最多推一条。"""
    c = cfg(settings)
    if not c["enabled"]:
        return None
    now = now or datetime.now(UTC)
    env = env or Env()
    today = str(now.astimezone(PT).date())
    st = load_state(settings)
    if not dry and st["daily"].get("date") == today and st["daily"].get("pushed"):
        return None
    fs = checks(settings, now, env)
    status = line_status(settings, now, env)
    text = None
    if not dry:
        text = _llm_digest(settings, fs, status, llm, st, today)
        st = load_state(settings)          # _llm_digest 里写过计数，别用旧的覆盖
    if not text:
        text = code_digest(fs, status, now)
    if dry:
        return text
    alert = any(f["level"] == "alert" for f in fs)
    notify.push(settings, f"每日巡检 {now.astimezone(PT):%m-%d}", text, priority="high" if alert else "default")
    st["daily"] = {**st["daily"], "date": today, "pushed": True}
    save_state(settings, st)
    return text


def _llm_digest(settings, fs, status, llm, st, today) -> str | None:
    """最多一次调用 / 天（失败也算一次，不重试）。任何问题 → None（改用代码拼的日报）。"""
    from . import agents
    if st["daily"].get("date") == today and int(st["daily"].get("calls", 0)) >= 1:
        return None
    if llm is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            llm = agents.AnthropicLLM()
            llm.client = llm.client.with_options(timeout=40.0, max_retries=0)   # 调度线程不能被一次慢调用拖住
        except Exception as e:  # noqa: BLE001
            log.warning("supervisor llm init: %s", e)
            return None
    sp = agents.spend(settings)
    if sp["day"] >= sp["cap_day"] or sp["month"] >= sp["cap_month"]:
        return None
    st["daily"] = {"date": today, "calls": 1}
    save_state(settings, st)
    model = agents.cfg(settings)["lead_model"]
    user = json.dumps({"问题清单": [{k: f[k] for k in ("level", "title", "body")} for f in fs if RANK[f["level"]] >= RANK["warn"]],
                       "三条线状态": status}, ensure_ascii=False)
    try:
        text, tin, tout = (llm(model, DAILY_SYSTEM, user, max_tokens=1200) if isinstance(llm, agents.AnthropicLLM)
                           else llm(model, DAILY_SYSTEM, user))
        agents._record_spend(settings, "supervisor-daily", model, tin, tout)
    except Exception as e:  # noqa: BLE001
        log.warning("supervisor daily llm: %s", e)
        return None
    text = (text or "").strip()
    return text or None


# ------------------------------------------------------------------ 调度器入口：放后台线程，慢一点也不会卡住每分钟的下单 / 止损任务
_LOCK = threading.Lock()


def run_async(settings, kind: str) -> None:
    def _t():
        if not _LOCK.acquire(timeout=120):
            return
        try:
            (job_daily if kind == "daily" else job_hourly)(settings)
        except Exception:  # noqa: BLE001
            log.exception("supervisor %s failed", kind)
        finally:
            _LOCK.release()
    threading.Thread(target=_t, daemon=True, name=f"supervisor-{kind}").start()


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def due(now: datetime, settings, done: set) -> list[tuple[str, object]]:
    """scheduler.due_jobs 调用：每小时一个 key；每天加州 07:05 之后（4 小时内）一个 key。
    日报没推成（后台线程忙 / 出错）就每个加州整点再补一次，直到当天推成。配置写错用默认值，不抛异常。"""
    c = cfg(settings)
    if not c["enabled"]:
        return []
    out = []
    slot = int(now.timestamp() // 3600)
    if f"supervisor:{slot}" not in done:
        out.append((f"supervisor:{slot}", None))
    for k in [k for k in done if k.startswith("supervisor:") and k[11:].isdigit() and int(k[11:]) < slot - 3]:
        done.discard(k)
    pt = now.astimezone(PT)
    h, m = _int(c["daily_hour_pt"], 7), _int(c["daily_minute_pt"], 5)
    if (pt.hour, pt.minute) >= (h, m) and pt.hour < h + 4:
        key = f"supervisor_daily:{pt.date()}"
        if key not in done:
            out.append((key, None))
        else:
            try:
                ok = bool((load_state(settings)["daily"] or {}).get("pushed")) and load_state(settings)["daily"].get("date") == str(pt.date())
            except Exception:  # noqa: BLE001
                ok = True
            retry = f"{key}:{pt.hour}"
            if not ok and retry not in done:
                out.append((retry, None))
    return out


# ------------------------------------------------------------------ 命令行
def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description="巡检员（只读）")
    ap.add_argument("--check", action="store_true", help="跑一遍检查并打印，不推送")
    ap.add_argument("--daily", action="store_true", help="生成每日摘要")
    ap.add_argument("--dry", action="store_true", help="配合 --daily：只打印，不调大模型、不推送、不写状态")
    a = ap.parse_args(argv)
    s = config.load()
    if a.daily:
        if not a.dry:
            ap.error("--daily 需要加 --dry（真正的日报由调度器每天 07:05 PT 自动推）")
        print(job_daily(s, dry=True))
        return 0
    if a.check:
        fs = checks(s)
        for f in fs:
            print(f"[{f['level']:5s}] {f['key']}｜{f['title']}\n        {f['body']}\n        自 {_pt(f['since'])} PT")
        print(f"共 {len(fs)} 条" if fs else "没有发现问题")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
