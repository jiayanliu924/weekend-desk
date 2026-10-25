"""资金费套利 90 天回看（只读公开接口，不需要私钥）。

做法：每周一选"过去 7 天平均资金费最高且为正"的 top_n 个币，做空永续 + 买等额现货（不赌涨跌），持有一周收资金费。
成本：每次换仓按 4 笔吃单算（开空永续、买现货、平空、卖现货），全额手续费。
两个口径：
  - hedgeable：只用 Hyperliquid 上有现货可对冲的币（BTC/ETH/SOL/HYPE）——真能做的版本
  - all：所有主盘永续（上限参考，现货要去别处买，不一定做得到）
考试（事先定死）：hedgeable 口径年化扣费后 > 8% 且 90 天里没有一周是亏的。

用法：python -m desk.funding_scan [--days 90] [--top 3] [--capital 5000]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

API = "https://api.hyperliquid.xyz/info"
HEDGEABLE = ["BTC", "ETH", "SOL", "HYPE"]
PERP_TAKER = 0.00045   # 永续吃单 4.5bps（全额，不用打折）
SPOT_TAKER = 0.0007    # 现货吃单 7bps（全额）


def post(body: dict):
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"API failed: {body}")


def funding_history(coin: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    out, t = [], start_ms
    while t < end_ms:
        rows = post({"type": "fundingHistory", "coin": coin, "startTime": t, "endTime": end_ms})
        if not rows:
            break
        out += [(int(r["time"]), float(r["fundingRate"])) for r in rows]
        nt = int(rows[-1]["time"]) + 1
        if nt <= t:
            break
        t = nt
        time.sleep(0.15)
    return out


def liquid_coins(min_oi_usd: float = 5e6) -> list[str]:
    meta, ctx = post({"type": "metaAndAssetCtxs"})
    coins = []
    for a, c in zip(meta["universe"], ctx):
        if a.get("isDelisted"):
            continue
        oi = float(c.get("openInterest") or 0) * float(c.get("markPx") or 0)
        if oi >= min_oi_usd:
            coins.append(a["name"])
    return coins


def simulate(hist: dict[str, list[tuple[int, float]]], start_ms: int, end_ms: int, top: int, capital: float,
             fee_per_rotation: float) -> list[dict]:
    week = 7 * 24 * 3600 * 1000
    weeks, t, prev = [], start_ms + week, set()   # 第一周只用来算"过去 7 天"
    while t + week <= end_ms:
        trail = {c: [r for ts, r in h if t - week <= ts < t] for c, h in hist.items()}
        avg = {c: sum(v) / len(v) for c, v in trail.items() if len(v) >= 100}
        pick = [c for c, a in sorted(avg.items(), key=lambda kv: -kv[1]) if a > 0][:top]
        per = capital / max(1, len(pick)) if pick else 0.0
        earned = sum(per * sum(r for ts, r in hist[c] if t <= ts < t + week) for c in pick)  # 空永续收正资金费
        changed = len(set(pick) ^ prev)                                                        # 只有换掉的币要付手续费
        fees = changed * (capital / max(1, top)) * fee_per_rotation / 2
        weeks.append({"week": time.strftime("%m-%d", time.gmtime(t / 1000)), "picks": pick,
                      "funding": round(earned, 2), "fees": round(fees, 2), "net": round(earned - fees, 2)})
        prev, t = set(pick), t + week
    return weeks


def report(name: str, weeks: list[dict], capital: float, days: int) -> bool:
    net = sum(w["net"] for w in weeks)
    yrs = len(weeks) * 7 / 365
    ann = net / capital / yrs if yrs else 0
    losing = sum(w["net"] < 0 for w in weeks)
    print(f"\n== {name} ==  {len(weeks)} 周 | 净赚 ${net:.2f} | 年化 {ann:.1%} | 亏损周 {losing}")
    for w in weeks:
        print(f"  {w['week']}  {','.join(w['picks']) or '-':24s} 资金费 {w['funding']:>8.2f}  手续费 {w['fees']:>6.2f}  净 {w['net']:>8.2f}")
    return ann > 0.08 and losing == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--capital", type=float, default=5000.0)
    a = ap.parse_args(argv)
    end = int(time.time() * 1000)
    start = end - a.days * 86400 * 1000
    coins = sorted(set(liquid_coins()) | set(HEDGEABLE))
    print(f"拉 {len(coins)} 个币的资金费历史（{a.days} 天）…")
    hist = {}
    for c in coins:
        try:
            hist[c] = funding_history(c, start, end)
        except Exception as e:
            print(f"  {c}: {e}")
    rot = 2 * (PERP_TAKER + SPOT_TAKER)   # 开+平，两条腿
    ok = report("hedgeable（BTC/ETH/SOL/HYPE，真能做）", simulate({c: hist[c] for c in HEDGEABLE if c in hist}, start, end, a.top, a.capital, rot), a.capital, a.days)
    report("all（上限参考）", simulate(hist, start, end, a.top, a.capital, rot), a.capital, a.days)
    print("\n考试（hedgeable 年化 > 8% 且无亏损周）：", "过" if ok else "不过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
