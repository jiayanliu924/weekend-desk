"""手机推送（ntfy.sh）。没填 NTFY_TOPIC 时只写日志。"""
from __future__ import annotations

import base64
import logging
import os

import httpx

log = logging.getLogger("notify")


def push(settings, title: str, body: str, priority: str = "default") -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    log.info("NOTIFY %s | %s", title, body.replace("\n", " / ")[:500])
    if not topic:
        return False
    try:
        enc_title = "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?="  # RFC 2047, ntfy supports it
        httpx.post(f"{settings['notify']['server'].rstrip('/')}/{topic}", content=body.encode(),
                   headers={"Title": enc_title, "Priority": priority, "Markdown": "yes"}, timeout=20)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy failed: %s", e)
        return False
