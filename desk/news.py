"""新闻与文本采集（备忘 7.2）。每条新闻记下"我方收到时间"，发布时间只作参考（7.4 第一条）。"""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import time
from pathlib import Path

import feedparser
import httpx

from . import rawstore
from .rawstore import RawWriter

log = logging.getLogger("news")


def item_key(link: str, title: str) -> str:
    return hashlib.sha256(f"{link}|{title}".encode()).hexdigest()[:24]


class NewsPoller:
    def __init__(self, settings):
        self.s = settings
        self.w = RawWriter(settings.data, "news")
        self.state_path = Path(settings.data) / "state" / "feeds.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen = {k for (k,) in rawstore.query(settings.data, "news", select="key")}
        self.status = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    async def poll_once(self, cli: httpx.AsyncClient) -> int:
        new = 0
        for url in self.s["news"]["feeds"]:
            try:
                resp = await cli.get(url)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
                for e in feed.entries:
                    title = (e.get("title") or "").strip()
                    link = e.get("link") or ""
                    k = item_key(link, title)
                    if not title or k in self.seen:
                        continue
                    self.seen.add(k)
                    pub = e.get("published_parsed") or e.get("updated_parsed")
                    payload = {
                        "feed": url, "title": title, "link": link,
                        "summary": (e.get("summary") or "")[:4000],
                        "published_at": calendar.timegm(pub) if pub else None,
                    }
                    self.w.add(url, k, payload)
                    new += 1
                self.status[url] = {"ok_at": int(time.time()), "entries": len(feed.entries)}
            except Exception as ex:  # noqa: BLE001
                st = self.status.get(url, {})
                st["error_at"], st["error"] = int(time.time()), str(ex)[:200]
                self.status[url] = st
                log.warning("feed failed %s: %s", url, ex)
        self.w.flush()
        self.state_path.write_text(json.dumps(self.status, indent=1))
        return new

    async def run(self):
        headers = {"User-Agent": self.s["news"]["user_agent"]}
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as cli:
            while True:
                try:
                    n = await self.poll_once(cli)
                    if n:
                        log.info("news: %d new items", n)
                except Exception as e:  # noqa: BLE001
                    log.error("news loop: %s", e)
                await asyncio.sleep(self.s["news"]["poll_every_sec"])
