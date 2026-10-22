"""卡 D2：带花费上限的 Databento 期权 1 分钟买卖报价（cbbo-1m）下载器，只拉日线期权回测真正需要的合约和分钟。

流程：
1. 免费（Alpaca）：每只标的的日收盘价（不复权）+ 全部标准期权合约（含已到期；"1XYZ…" 调整过的合约跳过）。
2. 每个交易日 d：到期日在 d+14..d+60 天、行权价在收盘价 ±10% 内的合约 = 当天需要的合约。
3. 每个交易日只拉收盘前 30 分钟（15:30–16:00 纽约时间；半天市 12:30–13:00，来自 Alpaca /v2/calendar）。
4. 先对所有请求调 metadata.get_cost 求总价（--plan 只打印）；--download 要求总价 ≤ [opra] max_cost_usd 且带 --yes。
下载按 (标的, 月) 存 data/opra/cbbo1m/<UND>/<YYYY-MM>.parquet；已有文件跳过（可续跑）；逐月处理，内存小。
同时存 data/opra/meta/：closes_<UND>.json、needed_<UND>.json（每天需要的合约）。

用法：venv/bin/python -m desk.opra_fetch --plan | --download --yes [--underlyings A B] [--start 2024-01-02 --end 2026-10-21]
key：.env 里 DATABENTO_API_KEY；Alpaca key 同 alpaca_probe。
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import alpaca_exec as ax
from . import config

DATASET = "OPRA.PILLAR"
SCHEMA = "cbbo-1m"
UNDERLYINGS = ["SOXX", "SMH", "QQQ", "SPY", "NVDA", "AMD", "MU", "INTC"]
DEFAULT_START = "2024-01-02"
DTE_MIN, DTE_MAX = 14, 60
STRIKE_BAND = 0.10
MAX_SYMBOLS = 2000
WINDOW_MIN = 30
ET = ZoneInfo("America/New_York")
OCC = re.compile(r"^([A-Z]{1,5})(\d{6})([CP])(\d{8})$")   # 标准合约；"1SPY…" 这种调整过的不匹配 → 跳过
DEFAULT_MAX_COST = 100.0


# ---------------------------------------------------------------- 符号
def occ_to_osi(sym: str) -> str | None:
    """SOXX251024P00270000 → 'SOXX  251024P00270000'（根代码左对齐补空格到 6 位 + yymmdd + C/P + 8 位行权价，共 21 位）。"""
    m = OCC.match(sym or "")
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    return f"{root:<6}{ymd}{cp}{strike}"


def occ_expiry(sym: str) -> date | None:
    m = OCC.match(sym or "")
    if not m:
        return None
    s = m.group(2)
    return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]))


# ---------------------------------------------------------------- 选合约 / 时间窗
def needed_contracts(contracts: list[dict], d: date, close: float) -> list[str]:
    """contracts: [{symbol, expiration_date, strike_price}]，返回当天需要的 OCC 代码（升序、去重）。"""
    lo_e, hi_e = d + timedelta(days=DTE_MIN), d + timedelta(days=DTE_MAX)
    lo_k, hi_k = close * (1 - STRIKE_BAND), close * (1 + STRIKE_BAND)
    out = set()
    for c in contracts:
        if not OCC.match(c["symbol"]):
            continue
        e = c["_exp"] if "_exp" in c else date.fromisoformat(c["expiration_date"])
        if e < lo_e or e > hi_e:
            continue
        if lo_k <= float(c["strike_price"]) <= hi_k:
            out.add(c["symbol"])
    return sorted(out)


class ContractIndex:
    """按到期日排序，每天只扫 DTE 窗口内的合约。"""

    def __init__(self, contracts: list[dict]):
        seen, rows = set(), []
        for c in contracts:
            s = c.get("symbol", "")
            if s in seen or not OCC.match(s):
                continue
            seen.add(s)
            rows.append((date.fromisoformat(c["expiration_date"]), s, float(c["strike_price"])))
        rows.sort()
        self.rows = rows
        self.exps = [r[0] for r in rows]

    def needed(self, d: date, close: float) -> list[str]:
        a = bisect.bisect_left(self.exps, d + timedelta(days=DTE_MIN))
        b = bisect.bisect_right(self.exps, d + timedelta(days=DTE_MAX))
        lo, hi = close * (1 - STRIKE_BAND), close * (1 + STRIKE_BAND)
        return sorted(s for _, s, k in self.rows[a:b] if lo <= k <= hi)


def day_window(d: date, close_hhmm: str = "16:00") -> tuple[datetime, datetime]:
    """收盘前 30 分钟（纽约时间，带时区）。"""
    h, m = (int(x) for x in close_hhmm.split(":")[:2])
    end = datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
    return end - timedelta(minutes=WINDOW_MIN), end


def chunks(items: list, n: int = MAX_SYMBOLS) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)]


# ---------------------------------------------------------------- Alpaca（免费）
class AlpacaSource:
    """包一层 ax.Client；测试里换成假的（同名方法）。"""

    def __init__(self, c: ax.Client, sleep=time.sleep, min_gap_s: float = 0.35):
        self.c = c
        self._sleep, self._gap, self._last = sleep, min_gap_s, 0.0

    def _get(self, url: str, params: dict):
        """Alpaca 限速 200 次/分钟：每次请求至少间隔 min_gap_s；碰到 429 等 60 秒再试，最多 5 次。"""
        for k in range(6):
            wait = self._gap - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                return self.c._req("GET", url, params=params)
            except RuntimeError as e:
                if "429" not in str(e) or k == 5:
                    raise
                self._sleep(60)
            except Exception as e:  # noqa: BLE001  网络抖动（DNS / 连接断开 / 超时）：等 15 秒再试
                if k == 5 or not any(w in type(e).__name__ for w in ("Connect", "Timeout", "Network", "Read", "Protocol")):
                    raise
                self._sleep(15)

    def closes(self, und: str, start: date, end: date) -> dict[str, float]:
        out, tok = {}, None
        for _ in range(100):
            p = {"timeframe": "1Day", "start": start.isoformat(), "end": end.isoformat(), "limit": 10000,
                 "adjustment": "raw", "feed": "iex"}
            if tok:
                p["page_token"] = tok
            d = self._get(f"{ax.DATA_URL}/v2/stocks/{und}/bars", p)
            for b in d.get("bars") or []:
                out[b["t"][:10]] = float(b["c"])
            tok = d.get("next_page_token")
            if not tok:
                break
        return out

    def contracts(self, und: str, exp_gte: date, exp_lte: date) -> list[dict]:
        """active + inactive，按月分段翻页。"""
        out = []
        cur = exp_gte
        while cur <= exp_lte:
            nxt = min((cur.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1), exp_lte)
            for status in ("active", "inactive"):
                tok = None
                for _ in range(200):
                    p = {"underlying_symbols": und, "expiration_date_gte": cur.isoformat(),
                         "expiration_date_lte": nxt.isoformat(), "status": status, "limit": 1000}
                    if tok:
                        p["page_token"] = tok
                    d = self._get(f"{self.c.base}/v2/options/contracts", p)
                    out += d.get("option_contracts") or []
                    tok = d.get("next_page_token")
                    if not tok:
                        break
            cur = nxt + timedelta(days=1)
        return out

    def close_times(self, start: date, end: date) -> dict[str, str]:
        """日期 → 收盘时间 'HH:MM'（半天市是 13:00）。拿不到就返回空，调用方按 16:00。"""
        try:
            rows = self.c._req("GET", f"{self.c.base}/v2/calendar", params={"start": start.isoformat(), "end": end.isoformat()})
        except Exception:  # noqa: BLE001
            return {}
        return {r["date"]: r["close"] for r in rows or [] if r.get("date") and r.get("close")}


# ---------------------------------------------------------------- 计划
def build_meta(src, und: str, start: date, end: date) -> dict:
    closes = src.closes(und, start, end)
    cons = src.contracts(und, start + timedelta(days=DTE_MIN), end + timedelta(days=DTE_MAX))
    idx = ContractIndex(cons)
    need = {}
    for ds in sorted(closes):
        d = date.fromisoformat(ds)
        syms = idx.needed(d, closes[ds])
        if syms:
            need[ds] = syms
    return {"closes": closes, "needed": need, "n_contracts": len(idx.rows)}


def month_of(ds: str) -> str:
    return ds[:7]


def build_requests(und: str, needed: dict[str, list[str]], close_times: dict[str, str]) -> list[dict]:
    """每个 (交易日, 符号块) 一个请求。"""
    reqs = []
    for ds in sorted(needed):
        t0, t1 = day_window(date.fromisoformat(ds), close_times.get(ds, "16:00"))
        osi = [occ_to_osi(s) for s in needed[ds]]
        for k, ch in enumerate(chunks([x for x in osi if x])):
            reqs.append({"und": und, "month": month_of(ds), "day": ds, "chunk": k, "symbols": ch,
                         "start": t0.isoformat(), "end": t1.isoformat()})
    return reqs


def cost_of(db, r: dict) -> float:
    return float(db.metadata.get_cost(dataset=DATASET, start=r["start"], end=r["end"], symbols=r["symbols"],
                                      schema=SCHEMA, stype_in="raw_symbol"))


SAMPLE_DAYS = 3
SAFETY = 1.2
MAX_WORKERS = 4


def _is_429(e: Exception) -> bool:
    return "429" in str(e) or getattr(e, "http_status", None) == 429 or getattr(e, "status_code", None) == 429


def price_plan(db, reqs: list[dict], workers: int = MAX_WORKERS, sleep=time.sleep) -> None:
    """给每个请求填 r['cost']（美元）。get_cost 免费；并发 ≤4，遇到 429 指数退避重试。"""
    def one(r):
        for attempt in range(6):
            try:
                r["cost"] = cost_of(db, r)
                return
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if not _is_429(e) and ("422" in msg or "symbology" in msg):   # 参数/符号问题：重试没用，记下来接着估
                    r["cost"], r["error"] = 0.0, msg.splitlines()[0][:160] + " | " + " ".join(msg.splitlines()[1:2])[:160]
                    return
                if attempt == 5:
                    raise
                sleep(min(2 ** (attempt + 1), 30) if _is_429(e) else 1 + attempt)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, MAX_WORKERS))) as ex:
        list(ex.map(one, reqs))


def sample_days(days: list[str], n: int = SAMPLE_DAYS) -> list[str]:
    """从排好序的交易日里取 n 个均匀间隔的（不足 n 个就全取）。"""
    if len(days) <= n:
        return list(days)
    return [days[round(i * (len(days) - 1) / (n - 1))] for i in range(n)]


def estimate(db, reqs: list[dict], exact: bool = False, workers: int = MAX_WORKERS, sleep=time.sleep) -> dict[tuple[str, str], float]:
    """返回 {(标的, 月): 预计 $}。exact：每个请求都问价；否则每个(标的,月)只精确问 3 个均匀间隔的交易日，
    按该月有数据的交易日数放大。"""
    by = defaultdict(lambda: defaultdict(list))
    for r in reqs:
        by[(r["und"], r["month"])][r["day"]].append(r)
    if exact:
        price_plan(db, reqs, workers, sleep)
        return {k: sum(r["cost"] for rs in days.values() for r in rs) for k, days in by.items()}
    picked = []
    for days in by.values():
        for d in sample_days(sorted(days)):
            picked += days[d]
    price_plan(db, picked, workers, sleep)
    bad = [r for r in picked if r.get("error")]
    if bad:
        print(f"注意：{len(bad)}/{len(picked)} 个抽样请求询价失败（按 $0 计，估价会偏低）。例：")
        for r in bad[:3]:
            print(f"  {r['und']} {r['day']} 前几个符号 {[x for x in r['symbols'][:2]]}：{r['error']}")
    out = {}
    for k, days in by.items():
        ds = sorted(days)
        samp = sample_days(ds)
        per_day = [sum(r["cost"] for r in days[d]) for d in samp]
        out[k] = sum(per_day) / len(per_day) * len(ds)
    return out


def plan_table(est: dict[tuple[str, str], float], margin: float = 1.0) -> tuple[str, float]:
    per = defaultdict(float)
    for (u, _), v in est.items():
        per[u] += v
    raw = sum(per.values())
    lines = ["标的 | 预计花费 $"]
    for u in per:
        lines.append(f"{u} | {per[u]:,.2f}")
    lines.append(f"合计 | {raw:,.2f}")
    if margin != 1.0:
        lines.append(f"合计 x{margin} 安全边际（与上限比较） | {raw * margin:,.2f}")
    return "\n".join(lines), raw * margin


def diagnose(db, und: str = "SPY", day: str = "2024-02-01", occ: str = "SPY240301P00469000") -> None:
    """排查"符号认不出"：① 我们拼的符号能否解析 ② 父代码能解析出多少个 ③ 用我们的符号询价。"""
    d1 = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    osi = occ_to_osi(occ)
    for label, kw in (("我们拼的符号 → instrument_id", dict(symbols=[osi], stype_in="raw_symbol")),
                      (f"{und}.OPT 父代码 → instrument_id", dict(symbols=[f"{und}.OPT"], stype_in="parent"))):
        try:
            res = db.symbology.resolve(dataset=DATASET, stype_out="instrument_id", start_date=day, end_date=d1, **kw)
            r = res.get("result", {}) if isinstance(res, dict) else {}
            print(f"{label}：解析到 {len(r)} 个；未解析 {res.get('not_found') if isinstance(res, dict) else '?'}；"
                  f"部分 {res.get('partial') if isinstance(res, dict) else '?'}；样例 {[repr(k) for k in list(r)[:3]]}")
        except Exception as e:  # noqa: BLE001
            print(f"{label} 出错：{str(e)[:250]}")
    t0, t1 = day_window(date.fromisoformat(day))
    for sch_kw in (dict(symbols=[osi], stype_in="raw_symbol"), dict(symbols=[f"{und}.OPT"], stype_in="parent")):
        try:
            c = db.metadata.get_cost(dataset=DATASET, start=t0.isoformat(), end=t1.isoformat(), schema=SCHEMA, **sch_kw)
            print(f"询价 {sch_kw['stype_in']} {sch_kw['symbols'][0]!r} {day} 收盘前半小时：${c}")
        except Exception as e:  # noqa: BLE001
            print(f"询价 {sch_kw['stype_in']} 出错：{str(e)[:250]}")


def max_cost(settings) -> float:
    return float((settings.raw.get("opra") or {}).get("max_cost_usd", DEFAULT_MAX_COST))


# ---------------------------------------------------------------- 下载
OUT_COLS = ["ts", "symbol", "bid_px", "ask_px", "bid_sz", "ask_sz"]


def _normalize(df):
    import pandas as pd
    df = df.reset_index()
    tcol = "ts_recv" if "ts_recv" in df.columns else ("ts_event" if "ts_event" in df.columns else df.columns[0])
    out = pd.DataFrame({"ts": df[tcol], "symbol": df["symbol"],
                        "bid_px": df["bid_px_00"].astype(float), "ask_px": df["ask_px_00"].astype(float),
                        "bid_sz": df["bid_sz_00"], "ask_sz": df["ask_sz_00"]})
    return out[OUT_COLS]


def _picked_count(reqs):
    by = defaultdict(lambda: defaultdict(list))
    for r in reqs:
        by[(r["und"], r["month"])][r["day"]].append(r)
    for days in by.values():
        for d in sample_days(sorted(days)):
            yield from days[d]


def month_path(root: Path, und: str, month: str) -> Path:
    return root / "cbbo1m" / und / f"{month}.parquet"


def fetch_month(db, reqs: list[dict]):
    import pandas as pd
    frames = []
    for r in reqs:
        for attempt in range(4):
            try:
                store = db.timeseries.get_range(dataset=DATASET, schema=SCHEMA, stype_in="raw_symbol", symbols=r["symbols"],
                                                start=r["start"], end=r["end"])
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "symbology" in msg or "could be resolved" in msg:   # 这一天这批合约都还没上市：没数据也不收钱，跳过
                    store = None
                    break
                if attempt == 3:
                    raise
                time.sleep(min(2 ** (attempt + 1), 30))
        if store is None:
            continue
        df = store.to_df(price_type="float", map_symbols=True)
        if len(df):
            frames.append(_normalize(df))
    if not frames:
        return pd.DataFrame(columns=OUT_COLS)
    return pd.concat(frames, ignore_index=True)


def download(db, reqs: list[dict], root: Path, log=print, est: dict | None = None) -> dict:
    by = defaultdict(list)
    for r in reqs:
        by[(r["und"], r["month"])].append(r)
    done = skipped = 0
    spent = 0.0
    for (und, month), rs in sorted(by.items()):
        p = month_path(root, und, month)
        if p.exists():
            skipped += 1
            continue
        df = fetch_month(db, rs)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(p)
        e = (est or {}).get((und, month), 0.0)
        spent += e
        done += 1
        with open(root / "meta" / "download_log.jsonl", "a") as f:
            f.write(json.dumps({"und": und, "month": month, "rows": len(df), "est_cost_usd": round(e, 4),
                                "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
        log(f"  {und} {month}：{len(df)} 行，预计 ${e:,.2f}")
        del df
    return {"downloaded": done, "skipped": skipped, "est_spent": spent}


# ---------------------------------------------------------------- 主流程
def run(settings, src, db, unds: list[str], start: date, end: date, mode: str, yes: bool = False,
        root: Path | None = None, out=print, workers: int = MAX_WORKERS, exact: bool = False, sleep=time.sleep) -> int:
    root = root or (settings.data / "opra")
    (root / "meta").mkdir(parents=True, exist_ok=True)
    close_times = src.close_times(start, end)
    reqs_all = []
    for u in unds:
        out(f"[{u}] 读取收盘价和合约列表（Alpaca，免费）…")
        cache = root / "meta" / f"meta_{u}_{start}.json"
        m = json.loads(cache.read_text()) if cache.exists() else None
        # 断点续跑：合约清单拉过、而且截止日离现在不超过 7 天就直接用（Alpaca 限速，重拉 QQQ/SPY 要十几分钟）
        if not m or date.fromisoformat(m.get("end", "1900-01-01")) < end - timedelta(days=7):
            m = build_meta(src, u, start, end)
            m["end"] = end.isoformat()
            cache.write_text(json.dumps(m))
        (root / "meta" / f"closes_{u}.json").write_text(json.dumps(m["closes"]))
        (root / "meta" / f"needed_{u}.json").write_text(json.dumps(m["needed"]))
        monthly = defaultdict(set)
        for ds, syms in m["needed"].items():
            monthly[month_of(ds)].update(syms)
        (root / "meta" / f"needed_monthly_{u}.json").write_text(json.dumps({k: sorted(v) for k, v in sorted(monthly.items())}))
        reqs = build_requests(u, m["needed"], close_times)
        # 已下载的月份不再计费
        reqs = [r for r in reqs if not month_path(root, u, r["month"]).exists()]
        out(f"[{u}] 交易日 {len(m['closes'])}，合约 {m['n_contracts']}，待下载请求 {len(reqs)}")
        reqs_all += reqs
    n_calls = len(reqs_all) if exact else sum(1 for _ in _picked_count(reqs_all))
    out(f"向 Databento 询价（metadata.get_cost，不扣费；{'逐个请求精确' if exact else '每标的每月抽 3 天估算'}，约 {n_calls} 次调用）…")
    est = estimate(db, reqs_all, exact, workers, sleep)
    table, total = plan_table(est, 1.0 if exact else SAFETY)
    cap = max_cost(settings)
    out(table)
    out(f"上限 ${cap:,.2f}（config.toml [opra] max_cost_usd）；已下载的月份已排除。")
    if mode == "plan":
        out("只是计划，没有下载，没有扣费。")
        return 0
    if total > cap:
        out(f"中止：预计 ${total:,.2f} 超过上限 ${cap:,.2f}。缩小标的/日期范围，或在 config.toml 调高 [opra] max_cost_usd。")
        return 2
    if not yes:
        out("中止：下载要加 --yes 确认。")
        return 2
    res = download(db, reqs_all, root, out, est)
    out(f"完成：新下载 {res['downloaded']} 个月文件，跳过已有 {res['skipped']} 个，预计花费 ${res['est_spent']:,.2f}（API 不返回实扣金额，实际以 Databento 账单为准）。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="desk.opra_fetch")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="只算价格，不下载")
    g.add_argument("--download", action="store_true", help="下载（需 --yes 且总价不超上限）")
    g.add_argument("--diagnose", action="store_true", help="看 Databento 的 OPRA 符号格式并试询价一个合约")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--exact", action="store_true", help="逐个请求精确询价（慢，调用很多）；默认抽样估算 x1.2")
    ap.add_argument("--underlyings", nargs="+", default=UNDERLYINGS)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    a = ap.parse_args(argv)
    s = config.load()
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        print("没找到 DATABENTO_API_KEY：先把 key 写进 .env", file=sys.stderr)
        return 1
    kid, sec = ax.creds(True)
    paper = True
    if not (kid and sec):
        kid, sec = ax.creds(False)
        paper = False
    if not (kid and sec):
        print("没找到 Alpaca key", file=sys.stderr)
        return 1
    import databento as dbn
    db = dbn.Historical(key)
    if a.diagnose:
        for u, d, occ in (("SPY", "2024-02-01", "SPY240301P00469000"), ("SOXX", "2025-09-15", "SOXX251024P00270000")):
            try:
                diagnose(db, u, d, occ)
            except Exception as e:  # noqa: BLE001
                print(f"{u} 诊断出错：{str(e)[:300]}")
        return 0
    src = AlpacaSource(ax.Client(kid, sec, paper=paper))
    return run(s, src, db, a.underlyings, date.fromisoformat(a.start), date.fromisoformat(a.end),
               "plan" if a.plan else "download", a.yes, exact=a.exact)


if __name__ == "__main__":
    sys.exit(main())
