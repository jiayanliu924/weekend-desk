"""卡 D1：估算从 Databento 拉美股期权历史买卖报价要花多少钱（只调 metadata.get_cost，不下载、不扣费）。

key 放在服务器 .env：DATABENTO_API_KEY=db-...
用法：cd /opt/weekend-desk && venv/bin/python -m desk.databento_cost
"""
from __future__ import annotations

import os
import sys

from . import config

DATASET = "OPRA.PILLAR"
UNDERLYINGS = ["SOXX", "SMH", "QQQ", "SPY", "NVDA", "AMD", "MU", "INTC"]
START, END = "2024-01-02", "2026-10-20"
# 优先 1 分钟买卖报价；对照看日线成交和逐笔报价有多贵
SCHEMAS = ["cbbo-1m", "cbbo-1s", "tcbbo", "ohlcv-1d", "definition"]


def main(argv=None) -> int:
    config.load()
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        print("没找到 DATABENTO_API_KEY：先把 key 写进 /opt/weekend-desk/.env", file=sys.stderr)
        return 1
    import databento as db
    c = db.Historical(key)
    have = set(c.metadata.list_schemas(DATASET))
    print(f"Databento {DATASET} 可用数据类型：{', '.join(sorted(have))}")
    print(f"时间段 {START} → {END}，标的 {', '.join(UNDERLYINGS)}（全部合约）\n")
    print("数据类型 | 8 只合计 $ | 分标的 $")
    for sch in SCHEMAS:
        if sch not in have:
            print(f"{sch} | 不提供")
            continue
        per = {}
        for u in UNDERLYINGS:
            try:
                per[u] = c.metadata.get_cost(dataset=DATASET, start=START, end=END, symbols=[f"{u}.OPT"],
                                             stype_in="parent", schema=sch)
            except Exception as e:  # noqa: BLE001
                per[u] = None
                print(f"  {sch} {u} 出错：{str(e)[:120]}")
        tot = sum(v for v in per.values() if v is not None)
        print(f"{sch} | {tot:,.2f} | " + "，".join(f"{u} {v:,.2f}" if v is not None else f"{u} ?" for u, v in per.items()))
    print("\n只是估价，没有下载任何数据，也没有扣费。新账户有 $125 免费额度。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
