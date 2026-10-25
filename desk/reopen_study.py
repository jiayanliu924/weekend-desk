"""只读研究：周末重开时的 forced-flow（过冲后回归 ref）。不下单、不联网，只读已有 parquet。

用法：python -m desk.reopen_study [--weekends 2026-10-11,2026-10-18,2026-10-25] [--out data/reports/reopen_study]
内存：每次只读 一个币 × 一个窗口（名义重开 ±70 分钟）。
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from bisect import bisect_right
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, features, rawstore

STOCKS = ["NVDA", "TSLA", "INTC", "HOOD", "PLTR", "MSFT", "MU", "AMZN", "CRWV", "SNDK", "CRCL", "META",
          "GOOGL", "AAPL", "MSTR", "AMD", "CBRS"]
INDEXES = ["SP500", "XYZ100", "BRENTOIL"]
NOMINAL = {**{c: time(20, 5) for c in STOCKS}, **{c: time(18, 0) for c in INDEXES}}  # 周日 ET；SMSN 排除
KS = (5, 10, 20)
NOTIONAL = 5000.0
CTRL_TOL_BPS = 10.0  # 对照(b)"<= 0 + small"的容差（合计 bps）
NS = 10**9
WIN = 70 * 60 * NS


# ---------------- readers（测试里 monkeypatch 这三个） ----------------
def read_ctx(settings, coin, a_ns, b_ns):
    """[(received_at_ns, oracle, mid, oi)]，按时间排序。mid 取 midPx，缺省 markPx（同 features.build）。"""
    out = []
    for r, c in features._ctx(settings, coin, a_ns, b_ns):
        try:
            out.append((r, float(c["oraclePx"]), float(c.get("midPx") or c["markPx"]), float(c["openInterest"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def read_book(settings, coin, a_ns, b_ns):
    """[(received_at_ns, best_bid, best_ask)]。"""
    rows = rawstore.query(settings.data, "hl_book", select="received_at, payload",
                          sql_where=f"key = '{coin}' AND received_at >= {a_ns} AND received_at < {b_ns}",
                          a_ns=a_ns, b_ns=b_ns)
    out = []
    for r, p in rows:
        try:
            lv = json.loads(p)["data"]["levels"]
            if lv[0] and lv[1]:
                out.append((r, float(lv[0][0]["px"]), float(lv[1][0]["px"])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def read_trades(settings, coin, a_ns, b_ns):
    """[(received_at_ns, px, sz, side)]，按 tid 去重；side 'B' = 买方吃单。"""
    rows = rawstore.query(settings.data, "hl_trades", select="received_at, payload",
                          sql_where=f"key = '{coin}' AND received_at >= {a_ns} AND received_at < {b_ns}",
                          a_ns=a_ns, b_ns=b_ns)
    seen, out = set(), []
    for r, p in rows:
        for t in json.loads(p).get("data", []):
            tid = t.get("tid")
            if tid is not None:
                if tid in seen:
                    continue
                seen.add(tid)
            out.append((r, float(t["px"]), float(t["sz"]), t.get("side")))
    return out


# ---------------- 工具 ----------------
def nominal_ns(sunday: date, name: str, tz: str = "America/New_York") -> int:
    dt = datetime.combine(sunday, NOMINAL[name], ZoneInfo(tz))
    return int(dt.astimezone(timezone.utc).timestamp()) * NS


def _sign(x):
    return (x > 0) - (x < 0)


def detect_t0(ctx, nom_ns):
    """返回 (T0_ns, inferred_ok)。ctx: [(ns, oracle, mid, oi)]。"""
    ts = [c[0] for c in ctx]
    steps = []  # (ns, step_bps)
    for i in range(1, len(ctx)):
        steps.append((ts[i], abs(ctx[i][1] / ctx[i - 1][1] - 1) * 1e4 if ctx[i - 1][1] else 0.0))
    for ns, st in steps:
        if not (nom_ns - 600 * NS <= ns <= nom_ns + 600 * NS) or st <= 3:
            continue
        prior = [s for n, s in steps if ns - 600 * NS <= n < ns]
        if len(prior) < 5:  # 没有足够的前 10 分钟样本就无法判断是否 EWMA 式爬升
            continue
        med = statistics.median(prior)
        if st >= 1.5 * med:
            return ns, True
    after = [t for t in ts if t > nom_ns]
    return (after[0], False) if after else (None, False)


class Series:
    """1 秒前向填充视图。"""
    def __init__(self, ctx, t0):
        self.ts = [c[0] for c in ctx]
        self.ctx = ctx
        self.t0 = t0

    def at(self, sec, field):  # field: 1 oracle, 2 mid, 3 oi
        i = bisect_right(self.ts, self.t0 + int(sec * NS)) - 1
        return self.ctx[i][field] if i >= 0 else None


def _best_book(book_ts, book, t_ns):
    i = bisect_right(book_ts, t_ns) - 1
    return book[i] if i >= 0 else None


def trade_cross(trades, after_ns, until_ns, px, sell_side):
    """after_ns < received_at < until_ns 内第一笔严格穿过 px 的成交 -> (ns, px) 或 None。
    sell_side=True: 我们是卖单，要 trade px > px；否则 trade px < px。"""
    for r, p, _, _ in trades:
        if r <= after_ns:
            continue
        if r >= until_ns:
            break
        if (p > px) if sell_side else (p < px):
            return r
    return None


def trade_cross_exit(trades, after_ns, until_ns, px, buying_back):
    """退出方向：buying_back（之前是卖）要 trade px < ref；否则 trade px > ref。"""
    for r, p, _, _ in trades:
        if r <= after_ns:
            continue
        if r > until_ns:
            break
        if (p < px) if buying_back else (p > px):
            return r
    return None


def liq_count(ser, trades, S, t0, nsec=3600):
    pre = {}
    for r, p, sz, side in trades:
        if t0 - 600 * NS <= r < t0:
            pre[(r - t0) // NS] = pre.get((r - t0) // NS, 0.0) + p * sz
    vals = [v for v in pre.values() if v > 0]
    med = statistics.median(vals) if vals else 0.0  # 只在有成交的秒上取中位数（全秒中位数几乎恒为 0）
    want = "B" if S > 0 else "A"
    flow = {}
    for r, p, sz, side in trades:
        if side == want and t0 <= r < t0 + (nsec + 1) * NS:
            flow[(r - t0) // NS] = flow.get((r - t0) // NS, 0.0) + p * sz
    n = 0
    for s in range(1, nsec + 1):
        o0, o1 = ser.at(s - 1, 3), ser.at(s, 3)
        if o0 and o1 is not None and (o1 / o0 - 1) < -0.005 and flow.get(s, 0.0) > 5 * med:
            n += 1
    return n


def analyze_event(coin, weekend, t0, inferred, ctx, book, trades):
    """返回 [row per k]；无法计算则 []。"""
    ser = Series(ctx, t0)
    ref = ser.at(120, 1)
    pre_or = ser.at(-5, 1)
    pre_mid = ser.at(-5, 2)
    if not ref or not pre_or or not pre_mid:
        return []
    G = ref / pre_or - 1
    S = _sign(G)
    if S == 0:
        return []
    M0 = pre_mid / ref - 1
    ovr = [ser.at(s, 2) / ref - 1 if ser.at(s, 2) else None for s in range(0, 3601)]
    mo = max((S * o for o in ovr[:121] if o is not None), default=0.0) * 1e4
    liq = liq_count(ser, trades, S, t0)
    btimes = [b[0] for b in book]
    T = lambda s: t0 + int(s * NS)
    # 对照 (b)
    ctrl_b = None
    if S * M0 > 0 and ser.at(5, 2) and ser.at(600, 2):
        ctrl_b = -S * (ser.at(600, 2) / ser.at(5, 2) - 1) * 1e4 - 18
    rows = []
    for k in KS:
        row = dict(coin=coin, weekend=weekend, T0=datetime.fromtimestamp(t0 / NS, timezone.utc).isoformat(),
                   t0_inferred=int(inferred), G=G, M0=M0, max_overshoot_bps=mo, liq_proxy=liq, k=k, filled=0,
                   gross_bps="", net_bps_main="", net_bps_ctrl60="", net_usd_main="",
                   ctrl_b_net_bps="" if ctrl_b is None else ctrl_b)
        t_in = next((s for s in range(0, 121) if ovr[s] is not None and S * ovr[s] * 1e4 > k), None)
        b = _best_book(btimes, book, T(t_in)) if t_in is not None else None
        if b:
            px = b[2] if S > 0 else b[1]   # S>0 卖在 best ask；S<0 买在 best bid
            fill = trade_cross(trades, T(t_in), T(120), px, sell_side=S > 0)
            if fill is not None:
                row["filled"] = 1
                direction = -S
                t_exit_ctrl = fill + 60 * NS if fill >= T(60) else T(60)
                xf = trade_cross_exit(trades, fill, fill + 600 * NS, ref, buying_back=S > 0)
                if xf is not None:
                    exit_px, maker = ref, True
                else:
                    i = bisect_right(ser.ts, fill + 600 * NS) - 1
                    exit_px, maker = ser.ctx[i][2], False
                i = bisect_right(ser.ts, t_exit_ctrl) - 1
                ctrl_px = ser.ctx[i][2]
                gross = direction * (exit_px / px - 1) * 1e4
                gross_c = direction * (ctrl_px / px - 1) * 1e4
                net = gross - 3 - (3 if maker else 9)
                row.update(gross_bps=gross, net_bps_main=net, net_bps_ctrl60=gross_c - 3 - 9,
                           net_usd_main=net / 1e4 * NOTIONAL)
        rows.append(row)
    return rows


# ---------------- 汇总 / 判定 ----------------
def _f(x):
    return x != ""


def verdict(rows):
    """rows: CSV 行 dict 列表。返回 (verdict, 决定条件)。"""
    k10 = [r for r in rows if r["k"] == 10]
    if not k10:
        return "INCONCLUSIVE", "无事件"
    big = [r for r in k10 if abs(r["G"]) > 0.01]
    med_os = statistics.median([r["max_overshoot_bps"] for r in big]) if big else None
    liq_share = sum(1 for r in big if r["liq_proxy"] > 0) / len(big) if big else None
    fills = [r for r in k10 if r["filled"]]
    nets = [r["net_bps_main"] for r in fills]
    tot = sum(nets)
    fill_rate = len(fills) / len(k10)
    by_w = {}
    for r in fills:
        by_w[r["weekend"]] = by_w.get(r["weekend"], 0.0) + r["net_bps_main"]
    neg_w = sum(1 for v in by_w.values() if v < 0)
    why = []
    if med_os is not None and med_os < 15:
        why.append(f"|G|>1% 中位过冲 {med_os:.1f}bps < 15")
    if liq_share is not None and liq_share < 0.5:
        why.append(f"|G|>1% 中 liq_proxy>0 占比 {liq_share:.0%} < 50%")
    if tot <= 0:
        why.append(f"k=10 主退出净和 {tot:.1f}bps <= 0")
    if by_w and neg_w > len(by_w) / 2:
        why.append(f"{neg_w}/{len(by_w)} 个周末净值为负")
    if fill_rate < 0.2:
        why.append(f"成交率 {fill_rate:.0%} < 20%")
    if why:
        return "REJECT", "; ".join(why)
    ctrl = {(r["coin"], r["weekend"]): r["ctrl_b_net_bps"] for r in k10 if _f(r["ctrl_b_net_bps"])}
    ctrl_sum = sum(ctrl.values())
    drop_best = tot - max(nets)
    if (med_os is not None and med_os >= 20 and liq_share is not None and liq_share >= 0.5 and tot > 0
            and drop_best >= 0 and ctrl_sum <= CTRL_TOL_BPS):
        return "WORTH_SHADOW", (f"过冲中位 {med_os:.1f}>=20, liq 占比 {liq_share:.0%}>=50%, 净和 {tot:.1f}>0, "
                                f"去掉最佳单笔 {drop_best:.1f}>=0, 对照(b)合计 {ctrl_sum:.1f}<={CTRL_TOL_BPS}")
    return "INCONCLUSIVE", (f"未触发 REJECT 也未满足全部 WORTH_SHADOW 条件 (过冲中位 {med_os}, liq {liq_share}, "
                            f"净和 {tot:.1f}, 去最佳 {drop_best:.1f}, 对照(b) {ctrl_sum:.1f})")


FIELDS = ["coin", "weekend", "T0", "t0_inferred", "k", "G", "M0", "max_overshoot_bps", "liq_proxy", "filled",
          "gross_bps", "net_bps_main", "net_bps_ctrl60", "net_usd_main", "ctrl_b_net_bps"]


def summarize(rows):
    k10 = [r for r in rows if r["k"] == 10]
    print(f"事件数 {len(k10)}，k=10 成交 {sum(r['filled'] for r in k10)}")
    for lab, lo, hi in (("|G|<0.5%", 0, 0.005), ("0.5-2%", 0.005, 0.02), (">2%", 0.02, 9)):
        b = [r for r in k10 if lo <= abs(r["G"]) < hi]
        f = [r for r in b if r["filled"]]
        print(f"  {lab}: 事件 {len(b)}, 成交 {len(f)}, 净和 {sum(r['net_bps_main'] for r in f):.1f}bps, "
              f"中位过冲 {statistics.median([r['max_overshoot_bps'] for r in b]) if b else float('nan'):.1f}bps")
    by_w = {}
    for r in k10:
        if r["filled"]:
            by_w[r["weekend"]] = by_w.get(r["weekend"], 0.0) + r["net_bps_main"]
    for w, v in sorted(by_w.items()):
        print(f"  周末 {w}: 净和 {v:.1f}bps (${v / 1e4 * NOTIONAL:.1f})")
    print(f"成交率(k=10): {sum(r['filled'] for r in k10) / len(k10):.0%}" if k10 else "成交率: n/a")
    v, why = verdict(rows)
    print(f"判定: {v}\n决定条件: {why}")


def run(settings, weekends, out_dir):
    names = STOCKS + INDEXES
    rows = []
    for wk in weekends:
        sunday = date.fromisoformat(wk)
        for name in names:
            inst = settings.inst(name)
            coin = inst["coin"] if inst else f"xyz:{name}"
            nom = nominal_ns(sunday, name)
            ctx = read_ctx(settings, coin, nom - WIN, nom + WIN)
            if not ctx:
                print(f"跳过 {wk} {name}: 窗口内无 hl_ctx 数据")
                continue
            t0, ok = detect_t0(ctx, nom)
            if t0 is None:
                print(f"跳过 {wk} {name}: 名义时间后无样本")
                continue
            ctx = [c for c in ctx if c[0] <= t0 + 3601 * NS]
            book = read_book(settings, coin, t0 - 120 * NS, t0 + 121 * NS)
            trades = read_trades(settings, coin, t0 - 600 * NS, t0 + 3601 * NS)
            r = analyze_event(name, wk, t0, ok, ctx, book, trades)
            if not r:
                print(f"跳过 {wk} {name}: ref/G 无法计算或 G==0")
                continue
            rows += r
            del ctx, book, trades
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "reopen_study.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"CSV -> {out / 'reopen_study.csv'}")
    summarize(rows)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekends", default="2026-10-11,2026-10-18,2026-10-25")
    ap.add_argument("--out", default="data/reports/reopen_study")
    a = ap.parse_args(argv)
    s = config.load()
    run(s, [x for x in a.weekends.split(",") if x], a.out)


if __name__ == "__main__":
    main()
