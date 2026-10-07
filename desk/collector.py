"""Hyperliquid 实时流采集（备忘 7.2：订单簿、成交、预言机、资金费）+ 参数快照（第八章）。"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import websockets

from .rawstore import RawWriter, now_ns

log = logging.getLogger("collector")
WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"


class Collector:
    def __init__(self, settings):
        s = settings
        self.s = s
        self.coin = s.instrument
        self.coins = [self.coin] + list(s["market"]["peripheral"])
        c = s["collect"]
        self.l2_every = c["l2_every_sec"]
        self.ctx_every = c["ctx_every_sec"]
        self.w = {name: RawWriter(s.data, name) for name in
                  ("hl_book", "hl_trades", "hl_ctx", "hl_params", "hl_conn")}
        self._last_l2 = 0.0
        self._last_ctx: dict[str, float] = {}
        self.last_msg_at = 0.0

    # ---------- websocket ----------
    async def run_ws(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=None, max_size=2**23) as ws:
                    self.w["hl_conn"].add("ws", "connect", {"url": WS_URL})
                    subs = [{"type": "l2Book", "coin": self.coin}, {"type": "trades", "coin": self.coin}]
                    subs += [{"type": "activeAssetCtx", "coin": c} for c in self.coins]
                    for sub in subs:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                    backoff = 1
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for msg in ws:
                            self._on_msg(msg)
                    finally:
                        pinger.cancel()
            except Exception as e:  # noqa: BLE001
                log.warning("ws error: %s", e)
                self.w["hl_conn"].add("ws", "disconnect", {"error": str(e)[:300]})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _ping(self, ws):
        while True:
            await asyncio.sleep(30)
            await ws.send(json.dumps({"method": "ping"}))

    def _on_msg(self, msg: str):
        r = now_ns()
        self.last_msg_at = time.time()
        try:
            j = json.loads(msg)
        except ValueError:
            return
        ch = j.get("channel")
        t = time.time()
        if ch == "l2Book":
            if t - self._last_l2 >= self.l2_every:
                self._last_l2 = t
                self.w["hl_book"].add("ws", j["data"].get("coin", ""), msg, r)
        elif ch == "trades":
            self.w["hl_trades"].add("ws", self.coin, msg, r)
        elif ch == "activeAssetCtx":
            coin = j["data"].get("coin", "")
            if t - self._last_ctx.get(coin, 0) >= self.ctx_every:
                self._last_ctx[coin] = t
                self.w["hl_ctx"].add("ws", coin, msg, r)

    # ---------- REST parameter snapshots ----------
    async def run_params(self):
        every = self.s["collect"]["params_every_sec"]
        dex = self.coin.split(":")[0] if ":" in self.coin else ""
        async with httpx.AsyncClient(timeout=30) as cli:
            while True:
                try:
                    meta = (await cli.post(INFO_URL, json={"type": "metaAndAssetCtxs", "dex": dex})).json()
                    uni, ctx = meta[0]["universe"], meta[1]
                    i = next(k for k, u in enumerate(uni) if u["name"] == self.coin)
                    dexs = (await cli.post(INFO_URL, json={"type": "perpDexs"})).json()
                    d = next((x for x in dexs if x and x.get("name") == dex), {})
                    snap = {
                        "asset_meta": uni[i], "asset_ctx": ctx[i],
                        "dex": {k: d.get(k) for k in ("name", "deployer", "oracleUpdater", "feeRecipient")},
                        "funding_multiplier": dict(d.get("assetToFundingMultiplier") or []).get(self.coin),
                        "funding_rate": dict(d.get("assetToFundingInterestRate") or []).get(self.coin),
                        "oi_cap": dict(d.get("assetToStreamingOiCap") or []).get(self.coin),
                    }
                    self.w["hl_params"].add("rest", self.coin, snap)
                except Exception as e:  # noqa: BLE001
                    log.warning("params error: %s", e)
                await asyncio.sleep(every)

    async def run_flush(self):
        every = self.s["collect"]["flush_every_sec"]
        while True:
            await asyncio.sleep(every)
            self.flush()

    def flush(self):
        for w in self.w.values():
            try:
                w.flush()
            except Exception as e:  # noqa: BLE001
                log.error("flush %s failed: %s", w.stream, e)
