"""特征表（备忘 7.3 末段）：每个周末一行。7.4 第一条：只用"收到时间"早于决策时刻的数据。"""
from __future__ import annotations

import json
import math

from . import mark, rawstore
from .clock import Weekend, to_ns
from .extract import load_events


def _ctx(settings, coin: str, a_ns: int, b_ns: int) -> list[tuple[int, dict]]:
    rows = rawstore.query(settings.data, "hl_ctx", select="received_at, payload",
                          sql_where=f"key = '{coin}' AND received_at >= {a_ns} AND received_at < {b_ns}")
    return [(r, json.loads(p)["data"]["ctx"]) for r, p in rows]


def last_ctx_before(settings, coin: str, t_ns: int, lookback_h: float = 72) -> tuple[int, dict] | None:
    rows = _ctx(settings, coin, t_ns - int(lookback_h * 3600e9), t_ns)
    return rows[-1] if rows else None


def first_ctx_after(settings, coin: str, t_ns: int, horizon_min: float = 30) -> tuple[int, dict] | None:
    rows = _ctx(settings, coin, t_ns, t_ns + int(horizon_min * 60e9))
    return rows[0] if rows else None


def book_before(settings, coin: str, t_ns: int) -> dict | None:
    rows = rawstore.query(settings.data, "hl_book", select="received_at, payload",
                          sql_where=f"key = '{coin}' AND received_at >= {t_ns - int(3600e9)} AND received_at < {t_ns}")
    if not rows:
        return None
    levels = json.loads(rows[-1][1])["data"]["levels"]
    bids, asks = levels[0], levels[1]
    if not bids or not asks:
        return None
    bb, ba = float(bids[0]["px"]), float(asks[0]["px"])
    mid = (bb + ba) / 2
    return {
        "best_bid": bb, "best_ask": ba, "mid": mid, "spread_bps": (ba - bb) / mid * 1e4,
        "bid_depth_usd": sum(float(l["px"]) * float(l["sz"]) for l in bids),
        "ask_depth_usd": sum(float(l["px"]) * float(l["sz"]) for l in asks),
        "book_received_at": rows[-1][0],
    }


def trade_notional(settings, a_ns: int, b_ns: int, coin: str | None = None) -> tuple[float, int]:
    where = f"received_at >= {a_ns} AND received_at < {b_ns}" + (f" AND key = '{coin}'" if coin else "")
    rows = rawstore.query(settings.data, "hl_trades", select="payload", sql_where=where)
    seen, total = set(), 0.0
    for (p,) in rows:
        for t in json.loads(p).get("data", []):
            if t.get("tid") in seen:
                continue
            seen.add(t.get("tid"))
            total += float(t["px"]) * float(t["sz"])
    return total, len(seen)


def build(settings, w: Weekend) -> dict:
    coin = w.coin or settings.instrument
    close_ns, dec_ns, start_ns = to_ns(w.close), to_ns(w.decision), to_ns(w.news_start)
    f: dict = {"weekend": w.wid, "coin": coin, "name": w.name or coin.split(":")[-1], "feature_version": settings["model"]["feature_version"],
               "decision_utc": w.decision.isoformat(), "ok": False, "problems": []}

    fri = last_ctx_before(settings, coin, close_ns)
    dec = last_ctx_before(settings, coin, dec_ns, lookback_h=1)
    if not fri:
        f["problems"].append("缺周五收盘前的预言机价")
    if not dec:
        f["problems"].append("缺决策时刻前 1 小时内的链上数据")
    if f["problems"]:
        return f

    fri_px = float(fri[1]["oraclePx"])
    dec_mid = float(dec[1].get("midPx") or dec[1]["markPx"])
    dec_mark = float(dec[1]["markPx"])
    f.update(fri_close=fri_px, fri_close_received_at=fri[0], dec_mid=dec_mid, dec_oracle=float(dec[1]["oraclePx"]),
             dec_mark=dec_mark, dec_received_at=dec[0])
    f["dev_bps"] = (dec_mid / fri_px - 1) * 1e4
    # 稳健偏离 + 插针检测（借鉴竞品 Meridian Mark 的"去插针"思路；见 desk/mark.py）
    dislocation = settings["risk"].get("dislocation_bps", 25.0)
    ds = mark.deviation_set(fri_px, dec_mid, dec_mark, f["dec_oracle"], dislocation)
    f.update(dev_mid_bps=ds["dev_mid_bps"], dev_mark_bps=ds["dev_mark_bps"],
             fair_dev_bps=ds["fair_dev_bps"], mid_mark_gap_bps=ds["mid_mark_gap_bps"],
             dislocated=ds["dislocated"])
    # 持仓时长（决策 → 重开），资金费率 carry 要用
    f["hold_hours"] = max(0.0, (to_ns(w.resume) - dec_ns) / 3600e9)

    # weekend path: deviation persistence, funding, gaps
    path = _ctx(settings, coin, close_ns, dec_ns)
    if path:
        devs = [(r, float(c.get("midPx") or c["markPx"]) / fri_px - 1) for r, c in path]
        far = sum(1 for _, d in devs if abs(d) * 1e4 > 10)
        f["dev_persist_frac"] = far / len(devs)
        f["max_abs_dev_bps"] = max(abs(d) for _, d in devs) * 1e4
        f["wick_ratio"] = mark.wick_ratio([d * 1e4 for _, d in devs])
        f["funding_avg"] = sum(float(c["funding"]) for _, c in path) / len(path)
        f["funding_annualized_pct"] = mark.annualized_funding_pct(f["funding_avg"])
        f["funding_abs_max"] = max(abs(float(c["funding"])) for _, c in path)
        oi0, oi1 = float(path[0][1]["openInterest"]), float(path[-1][1]["openInterest"])
        f["oi_change_pct"] = (oi1 / oi0 - 1) * 100 if oi0 else 0.0
        gaps = [(b - a) / 60e9 for (a, _), (b, _) in zip(path, path[1:])]
        f["max_gap_min"] = max(gaps) if gaps else 0.0
        if f["max_gap_min"] > 15:
            f["problems"].append(f"周末数据最长断档 {f['max_gap_min']:.0f} 分钟")
    vol, ntr = trade_notional(settings, close_ns, dec_ns, coin)
    f["wknd_volume_usd"], f["wknd_trades"] = vol, ntr

    btc0 = last_ctx_before(settings, "BTC", close_ns)
    btc1 = last_ctx_before(settings, "BTC", dec_ns, lookback_h=1)
    f["btc_wknd_ret_bps"] = ((float(btc1[1]["markPx"]) / float(btc0[1]["markPx"]) - 1) * 1e4) if btc0 and btc1 else None

    book = book_before(settings, coin, dec_ns)
    if book:
        f.update(book)

    # event features (7.3)
    thr = settings["model"]["materiality_threshold"]
    evs = load_events(settings, dec_ns, start_ns)
    strong = [e for e in evs if e["materiality"] >= thr]
    f["n_events"] = len(evs)
    f["n_mat2"] = len(strong)
    f["n_top10"] = sum(1 for e in evs if e.get("touches_top10") and e["materiality"] >= 1)
    f["max_materiality"] = max((e["materiality"] for e in evs), default=0)
    if strong:
        latest = max(e["_received_at"] for e in strong)
        f["hrs_since_strongest"] = (dec_ns - latest) / 3600e9
        f["strong_titles"] = sorted({e["_title"] for e in strong})[:8]
    else:
        f["hrs_since_strongest"] = None
        f["strong_titles"] = []
    f["news_weekend"] = f["n_mat2"] > 0
    f["ok"] = True
    return f


def clean_num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x
