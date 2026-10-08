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


def _enc(s: str) -> str:
    return "=?UTF-8?B?" + base64.b64encode(s.encode()).decode() + "?="


def push_file(settings, title: str, message: str, data: bytes, filename: str) -> bool:
    """Send a file (e.g. the PDF report) to the phone as an ntfy attachment."""
    topic = os.environ.get("NTFY_TOPIC")
    log.info("NOTIFY FILE %s (%d bytes)", filename, len(data))
    if not topic:
        return False
    try:
        r = httpx.put(f"{settings['notify']['server'].rstrip('/')}/{topic}", content=data, timeout=60,
                      headers={"Title": _enc(title), "Message": _enc(message), "Filename": filename})
        return r.status_code < 300
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy file failed: %s", e)
        return False
