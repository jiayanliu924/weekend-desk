"""主进程：采集一直跑，周末流程按时间点自动触发。服务器重启后会从状态文件接着跑。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import agents, jobs, notify, options, options_shock, positions, rawstore, report
from .clock import current_or_next_weekend, last_completed_weekend
from .collector import Collector
from .news import NewsPoller

log = logging.getLogger("scheduler")
PT = ZoneInfo("America/Los_Angeles")
STARTED_AT = time.time()


def due_jobs(now: datetime, settings, done: set) -> list[tuple[str, object]]:
    """Return (job_key, weekend) pairs that should run now — per instrument, plus shared per-Friday jobs."""
    out = []
    by_friday: dict[str, list] = {}
    for inst in settings.instruments:
        w = current_or_next_weekend(now, settings, inst)
        prev = last_completed_weekend(now, settings, inst)
        for wk in {w.wid: w, prev.wid: prev}.values():
            by_friday.setdefault(wk.friday.isoformat(), []).append(wk)
            k = lambda name: f"{wk.wid}:{name}"  # noqa: E731
            if wk.close + timedelta(minutes=2) <= now < wk.decision and k("open") not in done:
                out.append((k("open"), wk))
            if wk.decision <= now < wk.resume and k("lock") not in done:
                out.append((k("lock"), wk))
            if wk.exit + timedelta(minutes=2) <= now < wk.exit + timedelta(hours=6) and k("outcome") not in done:
                out.append((k("outcome"), wk))
            # 自动下单（模式 off 时这些任务什么都不做）
            if k("lock") in done and wk.decision <= now < wk.resume - timedelta(minutes=2) and k("live_entry") not in done:
                out.append((k("live_entry"), wk))
            if wk.resume - timedelta(minutes=1) <= now < wk.exit and k("live_cancel") not in done:
                out.append((k("live_cancel"), wk))
            # 看门狗：重开到正式平仓之间，每分钟看一次"已实现+未实现"是否破上限（防重开瞬间跳空）
            if wk.resume <= now < wk.exit:
                slot = int((now - wk.resume).total_seconds() // 60)
                if k(f"live_guard{slot}") not in done:
                    out.append((k(f"live_guard{slot}"), wk))
            if wk.exit <= now < wk.exit + timedelta(hours=6) and k("live_exit") not in done:
                out.append((k("live_exit"), wk))
    for fri, wks in by_friday.items():
        # news extraction is shared across instruments: window from earliest news start to latest decision
        wks = list({w.wid: w for w in wks}.values())
        start = min(w.news_start for w in wks)
        decisions = sorted({w.decision for w in wks})
        span = replace(wks[0], news_start=start, decision=decisions[-1])
        if start <= now < decisions[-1]:
            slot = int((now - start).total_seconds() // 900)
            if f"{fri}:extract{slot}" not in done:
                out.append((f"{fri}:extract{slot}", span))
        for d in decisions:  # make sure extraction runs right before each decision time
            key = f"{fri}:extract_final_{d:%H%M}"
            if d - timedelta(minutes=5) <= now < d and key not in done:
                out.append((key, replace(span, decision=d)))
        last_resume = max(w.resume for w in wks)
        if last_resume + timedelta(hours=14) <= now < last_resume + timedelta(days=3) and f"{fri}:report" not in done:
            out.append((f"{fri}:report", wks[0]))
    day = now.strftime("%Y-%m-%d")
    if now.hour == 0 and now.minute >= 10 and f"integrity:{day}" not in done:
        out.append((f"integrity:{day}", None))
    pt = now.astimezone(PT)
    if pt.weekday() == 0 and pt.hour == 5 and f"qa:{pt.date()}" not in done:   # 周一早 5 点自动体检
        out.append((f"qa:{pt.date()}", None))
    nu = now.astimezone(timezone.utc)
    if nu.weekday() == 0 and 13 <= nu.hour < 17 and f"labs:{nu.date()}" not in done:   # 周一 13:00 UTC 自动跑三条策略线排名
        out.append((f"labs:{nu.date()}", None))
    h = settings["notify"].get("daily_pdf_hour_pt", 7)
    if h <= pt.hour < h + 4 and f"dailypdf:{pt.date()}" not in done:
        out.append((f"dailypdf:{pt.date()}", None))
    # 工作日持续抽取新闻 → 事件（周末窗口外也要有事件，头条动量和期权突发样本才看得到新闻）
    every_x = int(settings["llm"].get("continuous_every_min", 10) or 0)
    if every_x > 0:
        slot = int(now.timestamp() // (every_x * 60))
        if f"xtract:{slot}" not in done:
            out.append((f"xtract:{slot}", None))
        for k in [k for k in done if k.startswith("xtract:") and int(k[7:]) < slot - 3]:
            done.discard(k)
    # 头条动量影子线：工作日也跑，每 every_min 分钟扫一次（只记账，不下单）
    hs = settings.raw.get("headline_shadow") or {}
    if hs.get("enabled", True):
        every = max(1, int(hs.get("every_min", 5)))
        slot = int(now.timestamp() // (every * 60))
        if f"headline:{slot}" not in done:
            out.append((f"headline:{slot}", None))
        # 状态文件别被每 5 分钟一个的 key 撑爆：只留最近几个
        for k in [k for k in done if k.startswith("headline:") and int(k[9:]) < slot - 3]:
            done.discard(k)
    # 期权研究的突发冲击样本（11c）：每 every_min 分钟扫一次抽取结果，首次看到就锁定（只记账）
    sh = settings["options"].get("shock") or {}
    if settings["options"]["enabled"] and sh.get("enabled", True):
        every = max(1, int(sh.get("every_min", 5)))
        slot = int(now.timestamp() // (every * 60))
        if f"optshock:{slot}" not in done:
            out.append((f"optshock:{slot}", None))
        for k in [k for k in done if k.startswith("optshock:") and int(k[9:]) < slot - 3]:
            done.discard(k)
    # 11d：Alpaca 美股期权仓位——等开盘 / 过了窗口还没平的，每 15 分钟重试平仓
    if (settings.raw.get("alpaca") or {}).get("enabled"):
        slot = int(now.timestamp() // (15 * 60))
        if f"alpacaretry:{slot}" not in done:
            out.append((f"alpacaretry:{slot}", None))
        for k in [k for k in done if k.startswith("alpacaretry:") and int(k[12:]) < slot - 3]:
            done.discard(k)
    # 总账户止损线：每分钟核对一次两条线的真实盈亏合计
    aslot = int(now.timestamp() // 60)
    if f"acctstop:{aslot}" not in done:
        out.append((f"acctstop:{aslot}", None))
    for k in [k for k in done if k.startswith("acctstop:") and int(k[9:]) < aslot - 3]:
        done.discard(k)
    # 跨 dex 配对自动下单：只有模式不是 off 才每分钟评估一次（off 时这里什么都不加）
    from . import pairs_exec
    if pairs_exec.get(settings)["mode"] != "off" or pairs_exec.has_open(settings):   # off 也要继续管理没平的仓位
        slot = int(now.timestamp() // 60)
        if f"pairs:{slot}" not in done:
            out.append((f"pairs:{slot}", None))
        for k in [k for k in done if k.startswith("pairs:") and int(k[6:]) < slot - 3]:
            done.discard(k)
    return out


def run_job(key: str, wk, settings):
    if key.startswith("xtract:"):
        return continuous_extract(settings)
    if key.startswith("labs:"):
        return start_labs_auto(settings)
    if key.startswith("headline:"):
        from . import headline_shadow
        return headline_shadow.run(settings)
    if key.startswith("optshock:"):
        from . import options_shock
        return options_shock.run(settings)
    if key.startswith("acctstop:"):
        from . import account_stop
        return account_stop.check(settings)
    if key.startswith("pairs:"):
        from . import pairs_exec
        return pairs_exec.step(settings, datetime.now(timezone.utc))
    if key.startswith("alpacaretry:"):
        from . import alpaca_exec
        return alpaca_exec.retry_pending(settings)
    name = key.rsplit(":", 1)[1]
    if name == "open":
        return jobs.job_open(settings, wk)
    if name.startswith("extract"):
        return jobs.job_extract(settings, wk)
    if name == "lock":
        return jobs.job_lock(settings, wk, push=False)
    if name == "outcome":
        return jobs.job_outcome(settings, wk, push=False)
    if name.startswith("live_"):
        from . import autotrade
        from .ledger import Ledger
        if name.startswith("live_guard"):
            return autotrade.kill_check(settings)
        if name == "live_entry":
            return autotrade.entry(settings, wk, Ledger(settings.data).weekend(wk.wid).get("lock"))
        if name == "live_cancel":
            return autotrade.cancel_unfilled(settings, wk)
        return autotrade.exit_(settings, wk)
    if name == "report":
        jobs.clear_pending_note(settings)
        return jobs.job_report(settings, wk)
    if key.startswith("integrity"):
        return integrity(settings)
    if key.startswith("dailypdf"):
        return jobs.job_daily_pdf(settings)
    if key.startswith("qa:"):
        from . import qa
        return qa.run(settings)


LABS_MEM = "350M"   # 1GB 服务器：回测单独一个进程，超内存只杀它自己，不拖垮网站和下单


def labs_cmd(python: str | None = None) -> list[str]:
    """独立进程 + 内存上限 + 低优先级。有 systemd-run 就用 cgroup 硬上限，否则退回 nice。"""
    import shutil
    import sys
    py = python or sys.executable
    base = ["nice", "-n", "19", py, "-m", "desk.labs_auto"]
    if shutil.which("systemd-run"):
        return ["systemd-run", "--scope", "--quiet", "-p", f"MemoryMax={LABS_MEM}", "-p", "MemorySwapMax=0",
                "-p", "CPUQuota=50%", *base]
    return base


def start_labs_auto(settings):
    """赚钱排行：另起一个受限进程跑（以前在主进程线程里跑，内存吃满会连带卡死网站和下单程序）。"""
    import subprocess
    from pathlib import Path
    logf = Path(settings.data) / "labs_auto.log"
    try:
        with open(logf, "ab") as fh:
            return subprocess.Popen(labs_cmd(), stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
                                    cwd=str(Path(__file__).resolve().parent.parent))
    except Exception as e:  # noqa: BLE001
        log.exception("labs_auto launch failed: %s", e)
        notify.push(settings, "赚钱排行没启动", str(e)[:300])
        return None


def continuous_extract(settings, extractor=None, now_ns: int | None = None) -> int:
    """工作日/任意时刻：把最近 continuous_lookback_min 分钟收到、还没抽取过的新闻整理成事件。

    已抽取过的新闻（同 prompt_version）会被 extract.pending 跳过，所以和周末窗口的抽取不会重复花钱。
    """
    from . import extract
    look = int(settings["llm"].get("continuous_lookback_min", 90))
    end = now_ns or time.time_ns()
    return extract.run_extraction(settings, end - look * 60 * 1_000_000_000, end, extractor=extractor)


def integrity(settings) -> None:
    """每日自动核对（7.2）：内容哈希 + 过去 24 小时有没有数据断档。"""
    since = rawstore.now_ns() - int(86400e9)
    msgs = []
    for stream in ("hl_ctx", "hl_book", "hl_trades", "news", "drb_index", "drb_opts"):
        rows = rawstore.query(settings.data, stream, select="received_at, payload, sha256",
                              sql_where=f"received_at >= {since}")
        bad = sum(1 for _, p, s in rows if rawstore.sha(p) != s)
        gaps = [(b[0] - a[0]) / 60e9 for a, b in zip(rows, rows[1:])]
        mg = max(gaps) if gaps else None
        msgs.append(f"{stream}: {len(rows)} 条, 哈希不符 {bad}, 最长断档 {mg:.0f} 分钟" if mg is not None
                    else f"{stream}: {len(rows)} 条")
        if stream == "hl_ctx" and time.time() - STARTED_AT > 7200 and (not rows or (mg or 0) > 15 or bad):
            notify.push(settings, "数据检查异常", "\n".join(msgs), priority="high")
    log.info("integrity: %s", " | ".join(msgs))


def _safe_meeting(settings, reason):
    try:
        agents.run_meeting(settings, reason)
    except Exception as e:  # noqa: BLE001
        log.exception("agent meeting failed: %s", e)
        notify.push(settings, "Agent 会议出错", str(e)[:300])


def entry_due_now(wk, now: datetime) -> bool:
    """和 due_jobs 里 live_entry 的窗口一模一样：决策时刻 ≤ now < 重开前 2 分钟。"""
    return wk is not None and wk.decision <= now < wk.resume - timedelta(minutes=2)


async def _entry_right_after_lock(lock_key: str, wk, settings, done: set) -> None:
    ek = lock_key[: -len(":lock")] + ":live_entry"
    if ek in done or not entry_due_now(wk, datetime.now(timezone.utc)):
        return
    done.add(lock_key)          # live_entry 的前提（和 due_jobs 一致）：先记 lock 已完成，再下单
    try:
        log.info("run job %s (right after lock)", ek)
        await asyncio.to_thread(run_job, ek, wk, settings)
    except Exception as e:  # noqa: BLE001
        log.exception("job %s failed: %s", ek, e)
        notify.push(settings, "任务出错", f"{ek}: {e}", priority="high")
    done.add(ek)
    jobs.save_state(settings, done)


async def job_loop(settings):
    done = jobs.load_state(settings)
    while True:
        now = datetime.now(timezone.utc)
        todo = [(k, w, run_job) for k, w in due_jobs(now, settings, done)]
        # options jobs return records too, but are pushed by the options module itself
        todo += [(k, smp, options.run) for k, smp in options.due(now, settings, done)]
        todo += [(k, smp, options.run) for k, smp in options_shock.due(now, settings, done)]
        locks, outs = [], []
        for key, wk, fn in todo:
            try:
                log.info("run job %s", key)
                rec = await asyncio.to_thread(fn, key, wk, settings)
                if key.endswith(":lock") and not key.startswith("opt:") and isinstance(rec, dict):
                    locks.append(rec)
                    # 2026-10-18：锁完这一个合约就立刻下它的单，不等全部合约锁完（以前要等下一轮循环，锁得慢就错过窗口）
                    await _entry_right_after_lock(key, wk, settings, done)
                elif key.endswith(":outcome") and isinstance(rec, dict) and not key.startswith("opt:"):
                    outs.append((wk, rec))
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed: %s", key, e)
                notify.push(settings, "任务出错", f"{key}: {e}", priority="high")
            done.add(key)
            jobs.save_state(settings, done)
        # agent meetings take a few minutes: run in the background so collection/locks never wait
        for key in agents.due(now, settings, done):
            log.info("start agent meeting %s", key)
            reason = "周末决策前加开" if key.startswith("agents_wk") else "每日例会"
            asyncio.get_running_loop().run_in_executor(None, _safe_meeting, settings, reason)
            done.add(key)
            jobs.save_state(settings, done)
        # one combined push per batch instead of one per instrument
        if locks:
            notify.push(settings, f"已锁定 {len(locks)} 个合约的预测", report.locks_summary(locks), priority="high")
        if outs:
            notify.push(settings, f"{len(outs)} 个合约开盘结果", report.outcomes_summary(settings, outs))
        if todo:
            try:  # 及时奖惩：开盘结果/期权结果一出来就给赛马 agent 结算
                from . import arena
                arena.settle(settings)
            except Exception as e:  # noqa: BLE001
                log.warning("arena settle: %s", e)
        await asyncio.sleep(20)


async def main(settings):
    col = Collector(settings)
    news = NewsPoller(settings)
    notify.push(settings, "Weekend Desk 已启动", f"合约 {', '.join(i['name'] for i in settings.instruments)}，模式 {'实盘' if settings['live']['enabled'] else '模拟'}")
    tasks = [col.run_ws(), col.run_params(), col.run_flush(), news.run(), job_loop(settings),
             positions.run(settings)]   # 仓位看守：和看门狗一样常驻；没填钱包地址时空转
    if settings["options"]["enabled"]:
        tasks += [c.run() for c in options.collectors(settings)]
    try:
        await asyncio.gather(*tasks)
    finally:
        col.flush()
        positions.flush()
