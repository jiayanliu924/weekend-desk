"""进程内共享的 Hyperliquid REST 限速器：所有线（autotrade / pairs / reopen / collector）共用一个。
令牌桶按 Info 权重计（预算 900/分钟 = IP 限额 1200 的 75%）；任何一处 429 → 全进程共同退避；
429 自动重试最多 3 次。撤单/平仓（priority=True）可提前透支桶（允许为负），但仍遵守 429 退避。"""
from __future__ import annotations

import threading
import time

BUDGET_PER_MIN = 900.0
MAX_TRIES = 3
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 30.0

INFO_WEIGHTS = {
    "metaAndAssetCtxs": 20, "clearinghouseState": 20, "userFees": 20, "openOrders": 20,
    "frontendOpenOrders": 20, "userFills": 20, "userFillsByTime": 20, "spotClearinghouseState": 20,
    "l2Book": 2, "allMids": 2, "orderStatus": 2, "meta": 20, "fundingHistory": 20,
}
DEFAULT_INFO_WEIGHT = 20
PRIORITY_ACTIONS = {"cancel", "cancelByCloid", "batchModify"}


def is_429(e: BaseException) -> bool:
    return getattr(e, "status_code", None) == 429 or "429" in str(e)[:200]


class RateLimiter:
    def __init__(self, budget_per_min=BUDGET_PER_MIN, clock=time.monotonic, sleep=time.sleep):
        self.rate = budget_per_min / 60.0
        self.cap = budget_per_min
        self.tokens = budget_per_min
        self.clock, self.sleep = clock, sleep
        self.last = clock()
        self.until = 0.0
        self.lock = threading.Lock()

    def _refill(self, now):
        self.tokens = min(self.cap, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def backoff_remaining(self) -> float:
        return max(0.0, self.until - self.clock())

    def note_429(self, secs: float):
        with self.lock:
            self.until = max(self.until, self.clock() + secs)

    def acquire(self, weight: float, priority: bool = False):
        """阻塞到可以发。priority：桶可为负（透支），但仍等 429 退避结束。"""
        while True:
            with self.lock:
                now = self.clock()
                wait = max(0.0, self.until - now)
                if wait <= 0:
                    self._refill(now)
                    if priority or self.tokens >= weight:
                        self.tokens -= weight
                        return
                    wait = (weight - self.tokens) / self.rate
            self.sleep(max(0.01, wait))

    def call(self, fn, weight: float, priority: bool = False):
        for attempt in range(MAX_TRIES):
            self.acquire(weight, priority)
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                if not is_429(e) or attempt == MAX_TRIES - 1:
                    raise
                self.note_429(min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** attempt))


LIMITER = RateLimiter()


def payload_weight(path: str, payload) -> tuple[float, bool]:
    """(权重, 是否优先)。"""
    p = payload if isinstance(payload, dict) else {}
    if "/exchange" in path:
        act = p.get("action", {}) or {}
        t = act.get("type")
        n = len(act.get("orders") or act.get("cancels") or []) or 1
        prio = t in PRIORITY_ACTIONS or (t == "order" and bool(act.get("orders")) and
                                         all(o.get("r") for o in act["orders"]))
        return float(n), prio
    return float(INFO_WEIGHTS.get(p.get("type"), DEFAULT_INFO_WEIGHT)), False


def wrap(client_obj, limiter: RateLimiter | None = None):
    """包住 SDK Info/Exchange 实例的 post（实例级，不动 SDK 类）。幂等。"""
    if client_obj is None or getattr(client_obj, "_hl_rate_wrapped", False):
        return client_obj
    orig = client_obj.post

    def post(path, payload=None, *a, **kw):
        w, prio = payload_weight(path, payload)
        lim = limiter or LIMITER
        return lim.call(lambda: orig(path, payload, *a, **kw), w, prio)

    client_obj.post = post
    client_obj._hl_rate_wrapped = True
    return client_obj
