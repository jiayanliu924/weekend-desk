"""主进程：采集一直跑，周末流程按时间点自动触发。服务器重启后会从状态文件接着跑。"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from . import jobs, notify, options, rawstore
from .clock import current_or_next_weekend, last_completed_weekend
from .collector import Collector
from .news import NewsPoller

log = logging.getLogger("scheduler")
STARTED_AT = time.time()


def due_jobs(now: datetime, settings, done: set) -> list[tuple[str, object]]:
    """Return (job_key, weekend) pairs that should run now."""
    out = []
    w = current_or_next_weekend(now, settings)
    prev = last_completed_weekend(now, settings)
    for wk in {w.wid: w, prev.wid: prev}.values():
        k = lambda name: f"{wk.wid}:{name}"  # noqa: E731
        if wk.close + timedelta(minutes=2) <= now < wk.decision and k("open") not in done:
            out.append((k("open"), wk))
        if wk.news_start <= now < wk.decision - timedelta(minutes=5):
            slot = int((now - wk.news_start).total_seconds() // 900)
            if k(f"extract{slot}") not in done:
                out.append((k(f"extract{slot}"), wk))
        if wk.decision - timedelta(minutes=5) <= now < wk.decision and k("extract_final") not in done:
            out.append((k("extract_final"), wk))
        if wk.decision <= now < wk.resume and k("lock") not in done:
            out.append((k("lock"), wk))
        if wk.exit + timedelta(minutes=2) <= now < wk.exit + timedelta(hours=6) and k("outcome") not in done:
            out.append((k("outcome"), wk))
        if wk.resume + timedelta(hours=14) <= now < wk.resume + timedelta(days=3) and k("report") not in done:
            out.append((k("report"), wk))
    day = now.strftime("%Y-%m-%d")
    if now.hour == 0 and now.minute >= 10 and f"integrity:{day}" not in done:
        out.append((f"integrity:{day}", None))
    return out


def run_job(key: str, wk, settings) -> None:
    name = key.split(":", 1)[1]
    if name == "open":
        jobs.job_open(settings, wk)
    elif name.startswith("extract"):
        jobs.job_extract(settings, wk)
    elif name == "lock":
        jobs.job_lock(settings, wk)
    elif name == "outcome":
        jobs.job_outcome(settings, wk)
    elif name == "report":
        jobs.job_report(settings, wk)
    elif key.startswith("integrity"):
        integrity(settings)


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


async def job_loop(settings):
    done = jobs.load_state(settings)
    while True:
        now = datetime.now(timezone.utc)
        todo = [(k, w, run_job) for k, w in due_jobs(now, settings, done)]
        todo += [(k, smp, options.run) for k, smp in options.due(now, settings, done)]
        for key, wk, fn in todo:
            try:
                log.info("run job %s", key)
                await asyncio.to_thread(fn, key, wk, settings)
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed: %s", key, e)
                notify.push(settings, "任务出错", f"{key}: {e}", priority="high")
            done.add(key)
            jobs.save_state(settings, done)
        await asyncio.sleep(20)


async def main(settings):
    col = Collector(settings)
    news = NewsPoller(settings)
    notify.push(settings, "Weekend Desk 已启动", f"品种 {settings.instrument}，模式 {'实盘' if settings['live']['enabled'] else '模拟'}")
    tasks = [col.run_ws(), col.run_params(), col.run_flush(), news.run(), job_loop(settings)]
    if settings["options"]["enabled"]:
        tasks.append(options.OptionsCollector(settings).run())
    try:
        await asyncio.gather(*tasks)
    finally:
        col.flush()
