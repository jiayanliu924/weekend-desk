"""卡 A4：预先登记的美股买方期权触发规则（knowledge/options_triggers_v1.toml）的真实报价回测。

规则、选约、成交、出场、样本切分、尝试次数、过门槛标准全部来自 toml，本文件不改任何数字。

数据：desk/opra_fetch.py 下载的 data/opra/cbbo1m/<UND>/<YYYY-MM>.parquet（只有收盘前 30 分钟、近平值 14–60 DTE），
      data/opra/meta/closes_<SYM>_full.json（本文件的 fetch_closes 拉：IEF + 8 个标的，从 2023-09-01，原始不复权收盘，IEX）。

口径（都写进报告）：
- 信号第 d 天只用 d 及之前的收盘；d 的"收盘"用 IEX 日线收盘近似（略晚于 15:59，差约一分钟，toml 允许）。
  均线/涨跌幅/新高用拆股调整收盘（closes_<SYM>_split.json，没有就退回不复权；NVDA 2024-06 十比一拆股，不复权会让信号乱掉）；
  选行权价用 d 的不复权收盘。
- 入场：d 当天 15:59（含）之前的最后一条报价，买 ask；出场：出场日同一口径的 bid。每张 $0.65 每边 + 权利金 1% 滑点每边。
  价差 > 15% 中间价（含 bid=0）当天不出手；选约只在"当天有报价"的合约里选；价差不过关不换合约。
- 出场每天只在 15:59 报价检查一次：期权 bid 相对入场成本（ask x 1.01，不含手续费）涨 >= +100% 止盈、跌 <= -50% 止损；
  持有满 20 个交易日、或离到期 <= 7 天无条件卖。出场日没报价 -> 顺延到该合约的下一个有报价日。入场当天不检查（出场最早是入场日 +1）。
- 一个 (规则, 标的) 同时只持一笔；平仓后 5 个交易日内不再开（平仓日 + 5 之后才能开）。
- 两遍流程：主数据只含 DTE 14–60 且在收盘价 +-10% 内的合约，合约掉出范围后没有报价。第一遍跑完写 meta/extra_needed.json
  （每个可能入场的合约、从入场日+1 到计划最晚出场日、主数据里没有报价的日子）；`python -m desk.opra_fetch --extra` 去 Databento 补，
  存到 cbbo1m_extra/；第二遍把主+补合并。补数据只用于出场，不参与选约（否则会偏向"后来被买到"的合约）。
- 补完之后仍然"再也没有报价"的仓位，按预先登记口径：到期归零（reason = no_more_quotes，pnl 就是归零结果，报告单独计数）；
  pnl_lastbid 是按最后一条 bid 平仓的参考值，不用于判定。
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT_DIR / "knowledge" / "options_triggers_v1.toml"
FULL_START = "2023-09-01"
SAMPLE_START = "2024-01-02"
TRAIN_END = "2025-06-30"
TEST_START = "2025-07-01"
ENTRY_MINUTE = 15 * 60 + 59          # 15:59 ET（含）之前的最后一条
FRESH_MIN = 15 * 60 + 50             # 入场报价 / 止盈报价不得早于 15:50 ET（否则当天不入场 / 不止盈）
FRESH_MIN_HALFDAY = 12 * 60 + 50
FEE = 0.65
SLIP = 0.01
CLUSTER_GAP = 5
N_TRIALS = 32
BONF = 0.05 / N_TRIALS
MIN_CLUSTERS = 10
RED_FLAG_RET = 0.5
BOOT_N = 10000
ET = "America/New_York"
RATES_SYMBOL = "IEF"

REASON_ZH = {"take_profit": "翻倍止盈", "stop_loss": "亏半止损", "max_hold": "持满20个交易日", "exit_dte": "临近到期",
             "no_more_quotes": "之后一直没有报价，按预先登记口径到期归零", "open_at_end": "到数据末尾还没平，按最后买价算"}


# ---------------------------------------------------------------- 规格
def load_spec(path: Path = SPEC_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------- 收盘价（下载 + 缓存）
def fetch_closes(root: Path, syms: list[str], end: date | None = None, src=None, log=print,
                 start: str = FULL_START) -> None:
    """拉 IEF + 标的的日线收盘（原始 + 拆股调整），缓存到 meta/closes_<SYM>_full.json / closes_<SYM>_split.json。
    只读 Alpaca 行情端点，复用 opra_fetch.AlpacaSource（自带限速/429 重试）。7 天内更新过的缓存不重拉。"""
    from . import alpaca_exec as ax
    from . import opra_fetch as of
    end = end or date.today()
    if src is None:
        kid, sec = ax.creds(True)
        paper = True
        if not (kid and sec):
            kid, sec = ax.creds(False)
            paper = False
        if not (kid and sec):
            raise RuntimeError("没找到 Alpaca key")
        src = of.AlpacaSource(ax.Client(kid, sec, paper=paper))
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    for s in syms:
        # 利率代理 IEF 用含分红调整的收盘（adjustment="all"），除息日的下跌不会被当成 60 日新低；
        # 股票/ETF 标的信号用拆股调整收盘（NVDA 十比一）。数据选择，写进报告。
        kinds = (("raw", "full"), ("all", "all")) if s == RATES_SYMBOL else (("raw", "full"), ("split", "split"))
        for adj, name in kinds:
            p = meta / f"closes_{s}_{name}.json"
            if p.exists():
                try:
                    last = max(json.loads(p.read_text()))
                    if date.fromisoformat(last) >= end - timedelta(days=7):
                        continue
                except Exception:  # noqa: BLE001
                    pass
            c = src.closes(s, date.fromisoformat(start), end, adjustment=adj)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(c))
            tmp.replace(p)
            log(f"  {s} {adj}: {len(c)} 天")


def read_closes(root: Path, sym: str) -> tuple[dict, dict]:
    """返回 (raw, adj)；adj = 拆股调整（文件不存在则 = raw，同一个对象）。"""
    m = root / "meta"
    raw = json.loads((m / f"closes_{sym}_full.json").read_text())
    adj = raw
    for nm in ("split", "all"):
        ps = m / f"closes_{sym}_{nm}.json"
        if ps.exists():
            adj = json.loads(ps.read_text())
            break
    return raw, adj


def split_warnings(sym: str, raw: dict, adj: dict) -> list[str]:
    if adj is not raw:
        return []
    ks = sorted(raw)
    return [f"{sym} {b} 单日 {raw[b] / raw[a]:.2f} 倍，疑似拆股且没有调整收盘（信号可能失真）"
            for a, b in zip(ks, ks[1:]) if raw[a] > 0 and not 0.55 < raw[b] / raw[a] < 1.8]


# ---------------------------------------------------------------- 信号（只用 [0..i] 的数据）
def sma_prior(x: list[float], i: int, n: int) -> float | None:
    """第 i 天之前 n 个收盘（d-n .. d-1，不含 d）的均值。"""
    if i < n:
        return None
    return float(sum(x[i - n:i])) / n


def signal(rule_id: str, adj: list[float], i: int, ief: list[float] | None = None, ief_i: int | None = None) -> bool:
    """第 i 个交易日是否触发。toml：均线/涨跌幅用"之前的收盘"，当前价用 d 的收盘当 15:59 价的代理：
    SMA50、10 日均线斜率、20/10 日涨跌幅基准、20 日新高、IEF 60 日低点、连跌天数全部由 d-1 及以前的收盘算，
    再和"今天的价格" adj[i] / ief[ief_i] 比较。只读 adj[0..i]（和 ief[0..ief_i]），后面的值不影响结果。"""
    c = adj[i]
    if rule_id.startswith("R1"):
        if ief is None or ief_i is None or ief_i < 59:
            return False
        s50 = sma_prior(adj, i, 50)
        # 新低 = 今天的价格 <= 前 59 个收盘的最低（相当于 60 日窗口含今天）
        return s50 is not None and ief[ief_i] <= min(ief[ief_i - 59:ief_i]) and c < s50
    s50 = sma_prior(adj, i, 50)
    s50p = sma_prior(adj, i - 10, 50) if i >= 10 else None       # 10 个交易日前的那条均线（同样只用当时之前的收盘）
    if rule_id.startswith("R2"):
        if s50 is None or s50p is None or i < 20:
            return False
        return c < s50 and s50 < s50p and c / adj[i - 20] - 1 <= -0.05
    if rule_id.startswith("R3"):
        if s50 is None or s50p is None or i < 19:
            return False
        return c > s50 and s50 > s50p and c >= max(adj[i - 19:i])
    if rule_id.startswith("R4"):
        if i < 10:
            return False
        chain = [c] + [adj[i - k] for k in range(1, 6)]           # 今天价 + 前 5 个收盘 -> 连跌 5 天
        down5 = all(chain[k] < chain[k + 1] for k in range(5))
        return down5 and c / adj[i - 10] - 1 <= -0.08
    raise ValueError(rule_id)


# ---------------------------------------------------------------- 报价
def parse_osi(sym: str) -> tuple[int, str, float]:
    """'SPY   240301P00469000' -> (到期日序号, 'P', 469.0)"""
    d = date(2000 + int(sym[6:8]), int(sym[8:10]), int(sym[10:12]))
    return d.toordinal(), sym[12], int(sym[13:21]) / 1000.0


def read_month(path: Path) -> pd.DataFrame:
    """一个 parquet -> 每个 (交易日, 合约) 取 15:59 前最后一条报价（带分钟数 qmin）。只读需要的列。
    保守处理：bid 为 NaN（单边市场）-> bid=0，不丢行（止损会触发）；ask 为 NaN/<=0 -> ask=NaN（不能买）；
    倒挂报价（bid>ask）-> 取窗口内最新的非倒挂报价；整个窗口都倒挂 -> bid=0。"""
    cols = ["day", "symbol", "bid", "ask", "qmin"]
    df = pd.read_parquet(path, columns=["ts", "symbol", "bid_px", "ask_px"])
    if df.empty:
        return pd.DataFrame({"day": pd.Series(dtype="int32"), "symbol": pd.Series(dtype=str),
                             "bid": pd.Series(dtype=float), "ask": pd.Series(dtype=float), "qmin": pd.Series(dtype="int32")})
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(ET)
    mod = (ts.dt.hour * 60 + ts.dt.minute).values
    local = ts.dt.tz_localize(None)
    day = ((local.dt.normalize() - pd.Timestamp("1970-01-01")).dt.days + 719163).astype("int32")   # date.toordinal
    ask = df["ask_px"].astype(float)
    out = pd.DataFrame({"ts": local.values, "day": day.values, "symbol": df["symbol"].values,
                        "bid": df["bid_px"].astype(float).fillna(0.0).clip(lower=0).values,
                        "ask": ask.where(ask > 0).values, "qmin": mod.astype("int32")})
    out = out[mod <= ENTRY_MINUTE].sort_values("ts", kind="stable")
    crossed = out["ask"].notna() & (out["bid"] > out["ask"])
    good = out[~crossed].drop_duplicates(["day", "symbol"], keep="last")
    last_any = out.drop_duplicates(["day", "symbol"], keep="last")
    only = last_any.merge(good[["day", "symbol"]], on=["day", "symbol"], how="left", indicator=True)
    only = only[only["_merge"] == "left_only"].drop(columns="_merge")
    only["bid"] = 0.0
    return pd.concat([good, only], ignore_index=True)[cols]


class Quotes:
    """某标的全部 15:59 报价（每合约每天一条）。by_day / by_sym 是位置索引，查询不扫全表。"""

    def __init__(self, df: pd.DataFrame, extra: pd.DataFrame | None = None):
        """df = 主数据（选约 + 出场都用）；extra = 补数据（只用于出场，同一 (合约, 日) 以主数据为准）。"""
        self.df = self._prep(df)
        self.by_day = self.df.groupby("day").indices
        self.by_main = self.df.groupby("symbol", observed=True).indices
        if extra is not None and len(extra):
            both = pd.concat([df, extra], ignore_index=True).drop_duplicates(["day", "symbol"], keep="first")
            self.all = self._prep(both)
            self.by_sym = self.all.groupby("symbol", observed=True).indices
        else:
            self.all, self.by_sym = self.df, self.by_main
        self.last_day = int(self.all["day"].max()) if len(self.all) else 0
        self._dmax = self.df.groupby("day")["qmin"].max().to_dict() if len(self.df) else {}
        self.n_extra = len(self.all) - len(self.df)

    @staticmethod
    def _prep(df: pd.DataFrame) -> pd.DataFrame:
        df = df.sort_values(["symbol", "day"], kind="stable").reset_index(drop=True)
        info = {s: parse_osi(s) for s in df["symbol"].unique()}
        df["symbol"] = df["symbol"].astype("category")
        cats = list(df["symbol"].cat.categories)
        codes = df["symbol"].cat.codes.values
        df["exp"] = np.array([info[c][0] for c in cats], dtype="int32")[codes] if cats else np.array([], dtype="int32")
        df["cp"] = np.array([info[c][1] for c in cats], dtype=object)[codes] if cats else []
        df["strike"] = np.array([info[c][2] for c in cats], dtype=float)[codes] if cats else []
        return df

    @staticmethod
    def _load(d: Path) -> pd.DataFrame | None:
        parts = []
        if d is not None and d.exists():
            for p in sorted(d.glob("*.parquet")):
                m = read_month(p)
                if len(m):
                    parts.append(m)
                del m
        return pd.concat(parts, ignore_index=True) if parts else None

    @classmethod
    def from_dir(cls, d: Path, extra_dir: Path | None = None) -> "Quotes":
        main = cls._load(d)
        if main is None:
            main = pd.DataFrame({"day": pd.Series(dtype="int32"), "symbol": pd.Series(dtype=str),
                                 "bid": pd.Series(dtype=float), "ask": pd.Series(dtype=float),
                                 "qmin": pd.Series(dtype="int32")})
        return cls(main, cls._load(extra_dir) if extra_dir else None)

    def stale_cut(self, day: int) -> int:
        """报价必须不早于这个分钟数（15:50 ET）才算新鲜；半天市（当天最晚报价 <= 13:00）用 12:50。"""
        return FRESH_MIN_HALFDAY if self._dmax.get(day, 10**6) <= 13 * 60 else FRESH_MIN

    def has_main(self, sym: str, day: int) -> bool:
        idx = self.by_main.get(sym)
        if idx is None:
            return False
        days = self.df["day"].values[idx]
        a = int(np.searchsorted(days, day))
        return a < len(days) and int(days[a]) == day

    def day_rows(self, day: int) -> pd.DataFrame:
        idx = self.by_day.get(day)
        return self.df.iloc[idx] if idx is not None else self.df.iloc[0:0]

    def sym_quotes(self, sym: str, after_day: int):
        """合约 sym 在 after_day 之后（不含）的 (day, bid, ask, qmin)，按日期升序。"""
        idx = self.by_sym.get(sym)
        if idx is None:
            return
        days = self.all["day"].values[idx]
        a = int(np.searchsorted(days, after_day, side="right"))
        for j in idx[a:]:
            r = self.all.iloc[j]
            yield int(r["day"]), float(r["bid"]), float(r["ask"]), int(r["qmin"])


# ---------------------------------------------------------------- 选约
def select_contract(q: Quotes, day: int, side: str, spot: float, spec: dict) -> tuple[dict | None, str]:
    c = spec["contract"]
    rows = q.day_rows(day)
    rows = rows[rows["cp"] == ("P" if side == "put" else "C")]
    dte = rows["exp"] - day
    rows = rows[(dte >= c["min_dte"]) & (dte <= c["max_dte"])]
    if rows.empty:
        return None, "no_contract"
    exps = sorted(rows["exp"].unique(), key=lambda e: (abs((e - day) - c["target_dte"]), e))
    rows = rows[rows["exp"] == exps[0]]
    pct_ = c["put_strike_pct"] if side == "put" else c["call_strike_pct"]
    tgt = spot * pct_
    k = sorted(rows["strike"].unique(), key=lambda s: (abs(s - tgt), s))[0]
    r = rows[rows["strike"] == k].iloc[0]
    bid, ask = float(r["bid"]), float(r["ask"])
    if not math.isfinite(ask) or ask <= 0:
        return None, "no_ask"
    if int(r["qmin"]) < q.stale_cut(day):
        return None, "stale_quote"
    mid = (bid + ask) / 2
    if mid <= 0 or (ask - bid) / mid > c["max_spread_pct"]:
        return None, "spread"
    return {"symbol": str(r["symbol"]), "exp": int(r["exp"]), "strike": float(k), "bid": bid, "ask": ask}, ""


# ---------------------------------------------------------------- 出场
def find_exit(q: Quotes, con: dict, entry_day: int, entry_idx: int, day_idx: dict[int, int], spec: dict) -> dict:
    """entry_day 之后（严格）逐个有报价日检查；返回 {day, bid, reason}。"""
    e = spec["exit"]
    entry_px = con["ask"] * (1 + SLIP)
    last = None
    max_spread = spec["contract"]["max_spread_pct"]
    for qd, bid, ask, qmin in q.sym_quotes(con["symbol"], entry_day):
        k = day_idx.get(qd)
        if k is None or k <= entry_idx:
            continue
        last = (qd, bid)
        ret = bid / entry_px - 1
        # 止盈只认"好报价"：价差 <= max_spread_pct 且报价不早于 15:50；否则当天不止盈（止损/到期照常按 bid 卖）
        mid = (bid + ask) / 2 if math.isfinite(ask) else 0.0
        good = mid > 0 and (ask - bid) / mid <= max_spread and qmin >= q.stale_cut(qd)
        if ret >= e["take_profit"] and good:
            return {"day": qd, "bid": bid, "reason": "take_profit"}
        if ret <= e["stop_loss"]:
            return {"day": qd, "bid": bid, "reason": "stop_loss"}
        if k - entry_idx >= e["max_hold_days"]:
            return {"day": qd, "bid": bid, "reason": "max_hold"}
        if con["exp"] - qd <= e["exit_dte"]:
            return {"day": qd, "bid": bid, "reason": "exit_dte"}
    if last is None:
        return {"day": con["exp"], "bid": 0.0, "reason": "no_more_quotes", "never": True, "alt_bid": None, "alt_day": con["exp"]}
    if last[0] == max(day_idx):      # 数据到头了仓位还开着：按最后买价记，不算归零
        return {"day": last[0], "bid": last[1], "reason": "open_at_end"}
    # 预先登记口径：之后再没有报价 -> 到期归零；最后买价只做参考
    return {"day": con["exp"], "bid": 0.0, "reason": "no_more_quotes", "never": True, "alt_bid": last[1], "alt_day": last[0]}


def make_trade(rule: dict, und: str, con: dict, entry_day: int, ex: dict, spec: dict, min1: bool = False) -> dict | None:
    n = int(math.floor(spec["contract"]["budget_usd"] / (con["ask"] * 100)))
    if min1:
        n = max(n, 1)      # 仅敏感性参考：预算不够也买 1 张（不作为结论）
    if n <= 0:
        return None
    cost = n * 100 * con["ask"] * (1 + SLIP) + n * FEE
    if ex.get("never"):
        proceeds = 0.0
    else:
        proceeds = n * 100 * ex["bid"] * (1 - SLIP) - n * FEE
    premium = n * 100 * con["ask"]
    pnl = proceeds - cost
    ab = ex.get("alt_bid")
    alt = pnl if ex["reason"] != "no_more_quotes" else (
        pnl if ab is None else n * 100 * ab * (1 - SLIP) - n * FEE - cost)
    return {"rule": rule["id"], "und": und, "symbol": con["symbol"], "side": rule["side"],
            "entry_date": date.fromordinal(entry_day).isoformat(), "exit_date": date.fromordinal(ex["day"]).isoformat(),
            "contracts": n, "entry_bid": con["bid"], "entry_ask": con["ask"], "exit_bid": ex["bid"],
            "premium": round(premium, 2), "cost": round(cost, 2), "proceeds": round(proceeds, 2),
            "pnl": round(pnl, 2), "ret": pnl / premium, "pnl_lastbid": round(alt, 2), "ret_lastbid": alt / premium,
            "reason": ex["reason"], "strike": con["strike"], "expiry": date.fromordinal(con["exp"]).isoformat()}


def simulate(rule: dict, und: str, days: list[str], adj: list[float], raw: list[float], q: Quotes, spec: dict,
             ief: list[float] | None = None, ief_pos: list[int | None] | None = None,
             start: str = SAMPLE_START, min1: bool = False) -> tuple[list[dict], list[int], dict]:
    """days 升序的交易日；adj/raw 与 days 对齐；ief_pos[i] = days[i] 在 ief 序列里的位置。
    返回 (trades, 信号日序号列表, 跳过原因计数)。trades 里带 entry_idx / exit_idx（交易日序号）。"""
    cd = spec["exit"]["cooldown_days"]
    ords = [date.fromisoformat(d).toordinal() for d in days]
    day_idx = {o: k for k, o in enumerate(ords)}
    sig_days, trades, skips = [], [], {}
    blocked = -1
    for i, d in enumerate(days):
        if d < start:
            continue
        if not signal(rule["id"], adj, i, ief, ief_pos[i] if ief_pos else None):
            continue
        sig_days.append(i)
        if i <= blocked:
            skips["cooldown_or_open"] = skips.get("cooldown_or_open", 0) + 1
            continue
        con, why = select_contract(q, ords[i], rule["side"], raw[i], spec)
        if con is None:
            skips[why] = skips.get(why, 0) + 1
            continue
        ex = find_exit(q, con, ords[i], i, day_idx, spec)
        t = make_trade(rule, und, con, ords[i], ex, spec, min1)
        if t is None:
            skips["too_expensive"] = skips.get("too_expensive", 0) + 1
            continue
        k = day_idx.get(ex["day"])
        if k is None:
            k = bisect.bisect_left(ords, ex["day"])
        t["entry_idx"], t["exit_idx"] = i, k
        trades.append(t)
        blocked = k + cd
    return trades, sig_days, skips


def extra_candidates(rule: dict, days: list[str], adj: list[float], raw: list[float], q: Quotes, spec: dict,
                     ief=None, ief_pos=None, start: str = SAMPLE_START) -> dict[str, set]:
    """第一遍产物：每个信号日（不管冷却/持仓，取超集，一遍补齐）能选出的合约，从入场日+1 到计划最晚出场日
    （入场+max_hold_days 个交易日，或 DTE<=exit_dte 的第一个交易日，先到者）里，主数据没有报价的日子。
    返回 {日期: {OCC 代码}}。"""
    e = spec["exit"]
    ords = [date.fromisoformat(d).toordinal() for d in days]
    out: dict[str, set] = {}
    for i, d in enumerate(days):
        if d < start or not signal(rule["id"], adj, i, ief, ief_pos[i] if ief_pos else None):
            continue
        con, _ = select_contract(q, ords[i], rule["side"], raw[i], spec)
        if con is None:
            continue
        occ = con["symbol"].replace(" ", "")
        for k in range(i + 1, min(i + e["max_hold_days"], len(days) - 1) + 1):
            if not q.has_main(con["symbol"], ords[k]):
                out.setdefault(days[k], set()).add(occ)
            if con["exp"] - ords[k] <= e["exit_dte"]:
                break
    return out


def write_extra_needed(root: Path, new: dict[str, dict[str, set]], fname: str = "extra_needed.json") -> Path:
    """meta/<fname>（默认 extra_needed.json）= {标的: {日期: [OCC…]}}；和已有文件按标的/日期取并集（子集重跑不丢旧的）。
    fname 参数给 v2 回测用（extra_needed_v2.json），默认值保持 v1 行为不变。"""
    p = root / "meta" / fname
    cur = json.loads(p.read_text()) if p.exists() else {}
    for u, dd in new.items():
        m = {k: set(v) for k, v in cur.get(u, {}).items()}
        for day, syms in dd.items():
            m.setdefault(day, set()).update(syms)
        cur[u] = {k: sorted(v) for k, v in sorted(m.items())}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur))
    tmp.replace(p)
    return p


# ---------------------------------------------------------------- 统计
def clusters(idx: list[int], gap: int = CLUSTER_GAP) -> list[list[int]]:
    out: list[list[int]] = []
    for v in sorted(idx):
        if out and v - out[-1][-1] <= gap:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def boot_p(x: list[float], n: int = BOOT_N, seed: int = 0) -> float | None:
    """单边 p：H0 均值=0（簇盈亏去均值后有放回重抽），看抽到均值 >= 实际均值的比例。"""
    if len(x) < 2:
        return None
    a = np.asarray(x, float)
    rng = np.random.default_rng(seed)
    obs = a.mean()
    m = rng.choice(a - obs, size=(n, len(a)), replace=True).mean(axis=1)
    return float((np.sum(m >= obs) + 1) / (n + 1))


def tstat(x: list[float]) -> float | None:
    if len(x) < 2:
        return None
    a = np.asarray(x, float)
    sd = a.std(ddof=1)
    return float(a.mean() / (sd / math.sqrt(len(a)))) if sd > 0 else None


def seg_of(entry_date: str) -> str:
    return "train" if entry_date <= TRAIN_END else "test"


def basic(trades: list[dict], alt: bool = False) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": None, "total_pnl": 0.0, "avg_pnl": None, "avg_ret": None}
    p = [t["pnl_lastbid" if alt else "pnl"] for t in trades]
    r = [t["ret_lastbid" if alt else "ret"] for t in trades]
    return {"trades": len(p), "win_rate": sum(x > 0 for x in p) / len(p), "total_pnl": round(sum(p), 2),
            "avg_pnl": round(sum(p) / len(p), 2), "avg_ret": float(np.mean(r))}


def streak_dd(trades: list[dict]) -> tuple[int, float]:
    ts = sorted(trades, key=lambda t: (t["exit_date"], t["entry_date"]))
    mx = cur = 0
    cum = peak = dd = 0.0
    for t in ts:
        cur = cur + 1 if t["pnl"] <= 0 else 0
        mx = max(mx, cur)
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return mx, round(dd, 2)


def evaluate(rule_id: str, und: str, trades: list[dict], sig_idx: list[int], skips: dict) -> dict:
    tr = [t for t in trades if seg_of(t["entry_date"]) == "train"]
    te = [t for t in trades if seg_of(t["entry_date"]) == "test"]
    mx, dd = streak_dd(trades)
    cl_te = clusters([t["entry_idx"] for t in te])
    cl_tr = clusters([t["entry_idx"] for t in tr])
    pnl_by_idx = {t["entry_idx"]: t["pnl"] for t in te}
    cpnl = [sum(pnl_by_idx[i] for i in c) for c in cl_te]
    p, ts = boot_p(cpnl), tstat(cpnl)
    b_te, b_tr = basic(te), basic(tr)
    red = b_te["avg_ret"] is not None and b_te["avg_ret"] > RED_FLAG_RET
    c1 = b_te["total_pnl"] > 0
    c2 = len(cl_te) >= MIN_CLUSTERS
    c3 = p is not None and (p < BONF or (p < 0.1 and b_tr["total_pnl"] > 0))
    reasons: dict = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {"rule": rule_id, "und": und, "all": basic(trades), "train": b_tr, "test": b_te,
            "all_lastbid": basic(trades, True), "test_lastbid": basic(te, True),
            "max_consec_losses": mx, "max_drawdown": dd,
            "signal_days": len(sig_idx), "signal_clusters": len(clusters(sig_idx)),
            "traded_clusters_train": len(cl_tr), "traded_clusters_test": len(cl_te),
            "test_cluster_tstat": ts, "test_cluster_boot_p": p, "bonferroni": BONF,
            "crit_test_pnl_positive": c1, "crit_clusters_ge_10": c2, "crit_significant": c3,
            "red_flag": red, "passed": bool(c1 and c2 and c3 and not red),
            "skips": skips, "reasons": reasons, "n_no_more_quotes": reasons.get("no_more_quotes", 0)}


# ---------------------------------------------------------------- 驱动
def _align_ief(days: list[str], ief_c: dict) -> tuple[list[float], list[int | None]]:
    ik = sorted(ief_c)
    pos = {d: j for j, d in enumerate(ik)}
    return [ief_c[d] for d in ik], [pos.get(d) for d in days]


def run(root: Path, unds: list[str], rule_ids: list[str] | None, spec: dict, log=print) -> tuple[dict, list[dict]]:
    rules = [r for r in spec["rule"] if not rule_ids or r["id"] in rule_ids or r["id"].split("_")[0] in rule_ids]
    ief_c = None
    if any(r["id"].startswith("R1") for r in rules):
        ief_c = read_closes(root, spec["universe"]["rates_proxy"])[1]
    results, trades_all, warnings = [], [], []
    extra_rows: dict[str, int] = {}
    for u in unds:
        log(f"[{u}] ...")
        raw_d, adj_d = read_closes(root, u)
        warnings += split_warnings(u, raw_d, adj_d)
        days = sorted(set(adj_d) & set(raw_d))
        adj, raw = [adj_d[d] for d in days], [raw_d[d] for d in days]
        qdir = root / "cbbo1m" / u
        if not qdir.exists() or not any(qdir.glob("*.parquet")):
            warnings.append(f"{u}: 没有报价数据")
            continue
        q = Quotes.from_dir(qdir, root / "cbbo1m_extra" / u)
        ief, ipos = _align_ief(days, ief_c) if ief_c else (None, None)
        need: dict[str, set] = {}
        for r in rules:
            for day, syms in extra_candidates(r, days, adj, raw, q, spec, ief, ipos).items():
                need.setdefault(day, set()).update(syms)
            tr, sig, sk = simulate(r, u, days, adj, raw, q, spec, ief, ipos)
            ev = evaluate(r["id"], u, tr, sig, sk)
            if sk.get("too_expensive"):
                tr1, _, _ = simulate(r, u, days, adj, raw, q, spec, ief, ipos, min1=True)
            else:
                tr1 = tr
            ev["sens_min1"] = {"note": "仅参考，不作为结论", "trades": len(tr1), "total_pnl": basic(tr1)["total_pnl"],
                               "test_pnl": basic([t for t in tr1 if seg_of(t["entry_date"]) == "test"])["total_pnl"]}
            results.append(ev)
            trades_all += [{k: v for k, v in t.items() if k not in ("entry_idx", "exit_idx")} for t in tr]
        write_extra_needed(root, {u: need})
        extra_rows[u] = q.n_extra
        del q
    n_zero = sum(x["n_no_more_quotes"] for x in results)
    if n_zero:
        warnings.append(f"{n_zero} 笔仓位出场前再无报价，按预先登记口径到期归零；若还没跑过 `python -m desk.opra_fetch --extra --download --yes`，先补数据再看结论")
    return {"spec_version": "v1", "sample": [SAMPLE_START, "latest"], "train_end": TRAIN_END, "test_start": TEST_START,
            "n_trials": N_TRIALS, "bonferroni": BONF, "extra_rows": extra_rows, "results": results, "warnings": warnings}, trades_all


# ---------------------------------------------------------------- 报告（中文）
def usd(x) -> str:
    return "-" if x is None else f"{x:+,.0f}"


def pct(x) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def fnum(x, f: str) -> str:
    return "-" if x is None else format(x, f)


def ex_line(t: dict | None) -> str:
    if not t:
        return "无"
    return (f"{t['und']} {t['entry_date']} 买入 {t['symbol'].strip()}，{t['exit_date']} 卖出（{REASON_ZH.get(t['reason'], t['reason'])}），"
            f"{t['contracts']} 张，盈亏 {t['pnl']:+,.0f} 美元（{t['ret'] * 100:+.0f}%）")


def write_report(out: dict, trades: list[dict], spec: dict, path: Path) -> None:
    res = out["results"]
    L = ["# 期权触发规则 v1 回测报告（真实历史买卖报价）", "",
         f"样本 {SAMPLE_START} → 最近一个交易日；训练段到 {TRAIN_END}，**考试段 {TEST_START} → 现在**。只买不卖，最大亏损 = 付出的权利金。",
         f"每笔花约 {spec['contract']['budget_usd']} 美元；买按卖价、卖按买价，另加每张 0.65 美元手续费和 1% 滑点（买卖各一次）。",
         f"一共试了 {N_TRIALS} 次（4 规则 x 8 标的），显著性按 32 次校正：单次 p 要小于 {BONF:.4f}。", "",
         "## 先读这几条（口径和局限）", "",
         "- 信号：均线、均线斜率、涨跌幅基准、20 日新高、IEF 60 日低点、连跌天数都用 d 之前的收盘算，再和『今天的价格』比较；今天的价格用当天 IEX 收盘近似 15:59 的价（差约 1 分钟）。入场用 15:59 前最后一条报价。",
         "- IEF 用含分红调整的收盘（避免除息日下跌被当成新低）；标的用拆股调整收盘。",
         "- 报价质量：单边市场（没有买价）按 bid=0，止损会触发；倒挂报价取窗口内最新的正常报价，没有就 bid=0；入场报价和止盈报价必须不早于 15:50 且止盈时价差不超过 15%，否则当天不入场/不止盈；止损、到期、持满天数照常按买价卖。",
         "- 主数据只含到期 14–60 天、价格在收盘价上下 10% 内的合约；持仓期间合约掉出范围后的报价，另用 `opra_fetch --extra` 补下载并合并（只用于平仓，不用于选合约）。"
         f"补进来的报价行数：{out.get('extra_rows', {})}。",
         "- 补完以后仍然一直没有报价的仓位，按预先登记口径『到期归零』（表里『无后续报价』列单独计数）；『保守口径』列相反，是按最后一次买价平仓的参考值，不用于判定。",
         "- 『信号簇』：同一标的 5 个交易日内重复触发算一簇；检验按簇算，不按笔数。", ""]
    for w in out["warnings"]:
        L.append(f"- 警告：{w}")
    if out["warnings"]:
        L.append("")
    for r in spec["rule"]:
        rr = [x for x in res if x["rule"] == r["id"]]
        if not rr:
            continue
        rt = [t for t in trades if t["rule"] == r["id"]]
        L += [f"## {r['id']}（{'买看跌 put' if r['side'] == 'put' else '买看涨 call'}）", "",
              f"**是什么**：{r['why']}", f"**触发条件**：{'；'.join(r['when'])}。", ""]
        tot = sum(x["all"]["trades"] for x in rr)
        sigs = sum(x["signal_days"] for x in rr)
        te = sum(x["test"]["total_pnl"] for x in rr)
        te_s = sum(x["test_lastbid"]["total_pnl"] for x in rr)
        ten = sum(x["test"]["trades"] for x in rr)
        L.append(f"**一共触发** {sigs} 个信号日，实际买入 {tot} 笔（其余因没报价、价差太大或在持仓/冷却期被跳过）。"
                 f"**考试段**共 {ten} 笔，合计盈亏 {te:+,.0f} 美元（若按最后买价平仓则 {te_s:+,.0f}）。")
        b = max(rt, key=lambda t: t["pnl"]) if rt else None
        w = min(rt, key=lambda t: t["pnl"]) if rt else None
        L += [f"- 最大一笔赚：{ex_line(b)}", f"- 最大一笔亏：{ex_line(w)}", "",
              "| 标的 | 买入笔数 | 胜率 | 总盈亏$ | 每笔均盈亏$ | 每笔均收益(占权利金) | 最长连亏 | 最大回撤$ | 训练段$ | 考试段$ | 考试段(按最后买价)$ | 信号簇 | 考试段簇 | t值 | p值(单边) | 无后续报价 | 最少买1张的考试段$（仅参考，不作为结论） | 结论 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for x in rr:
            a = x["all"]
            verdict = "红旗：好得离谱，先查bug" if x["red_flag"] else ("过门槛" if x["passed"] else "没过")
            L.append(f"| {x['und']} | {a['trades']} | {pct(a['win_rate'])} | {usd(a['total_pnl'])} | {usd(a['avg_pnl'])} | {pct(a['avg_ret'])} | "
                     f"{x['max_consec_losses']} | {x['max_drawdown']:,.0f} | {usd(x['train']['total_pnl'])} | {usd(x['test']['total_pnl'])} | "
                     f"{usd(x['test_lastbid']['total_pnl'])} | {x['signal_clusters']} | {x['traded_clusters_test']} | "
                     f"{fnum(x['test_cluster_tstat'], '.2f')} | {fnum(x['test_cluster_boot_p'], '.3f')} | {x['n_no_more_quotes']} | {usd(x.get('sens_min1', {}).get('test_pnl'))} | {verdict} |")
        L.append("")
    passed = [x for x in res if x["passed"]]
    SK = [("no_contract", "当天没合适报价/合约"), ("stale_quote", "报价早于15:50"), ("no_ask", "没有卖价"), ("spread", "价差太大"),
          ("too_expensive", "一张就超预算"), ("cooldown_or_open", "持仓/冷却期")]
    L += ["## 被跳过的信号（每个规则x标的，按原因计数）", "",
          "一张都买不起（超出每笔预算）的信号按预先登记口径直接跳过，没有偷偷改成买一张；这类跳过多的组合，结论只代表买得起的那部分行情。", "",
          "| 规则 | 标的 | 信号日 | 实际买入 | " + " | ".join(n for _, n in SK) + " |", "|---|---|---|---|" + "---|" * len(SK)]
    for x in res:
        L.append(f"| {x['rule']} | {x['und']} | {x['signal_days']} | {x['all']['trades']} | " + " | ".join(str(x['skips'].get(k, 0)) for k, _ in SK) + " |")
    L.append("")
    L += ["## 总结", "",
          f"{len(res)} 个（规则, 标的）组合里过门槛的有 {len(passed)} 个：" + ("、".join(f"{x['rule']}/{x['und']}" for x in passed) if passed else "无") + "。",
          "过门槛 = 考试段扣全部成本后总盈亏 > 0；考试段至少 10 个信号簇；p < 0.05/32，或 p < 0.1 且训练段也赚钱；且没触发『好得离谱』红旗。"
          "没过门槛的规则不进影子记账。"]
    flags = [x for x in res if x["red_flag"]]
    if flags:
        L.append("红旗（考试段每笔平均收益 > 权利金 50%，先当 bug 查）：" + "、".join(f"{x['rule']}/{x['und']}" for x in flags))
    path.write_text("\n".join(L) + "\n")


def write_outputs(out: dict, trades: list[dict], spec: dict, rdir: Path) -> None:
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "options_rules_v1.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    write_report(out, trades, spec, rdir / "options_rules_v1.md")
    cols = ["rule", "und", "symbol", "side", "entry_date", "exit_date", "contracts", "entry_bid", "entry_ask", "exit_bid", "strike", "expiry",
            "premium", "cost", "proceeds", "pnl", "ret", "pnl_lastbid", "ret_lastbid", "reason"]
    with open(rdir / "options_rules_v1_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="desk.options_rules_backtest")
    ap.add_argument("--underlyings", nargs="+")
    ap.add_argument("--rules", nargs="+", help="R1 R2 ... 或完整 id")
    ap.add_argument("--fetch-closes", action="store_true", help="只拉 IEF + 标的日线收盘（Alpaca 只读）后退出")
    ap.add_argument("--root", help="默认 <data>/opra")
    ap.add_argument("--out", help="报告目录，默认 <data>/reports")
    a = ap.parse_args(argv)
    from . import config
    s = config.load()
    root = Path(a.root) if a.root else s.data / "opra"
    rdir = Path(a.out) if a.out else s.data / "reports"
    spec = load_spec()
    unds = a.underlyings or spec["universe"]["underlyings"]
    if a.fetch_closes:
        fetch_closes(root, unds + [spec["universe"]["rates_proxy"]])
        return 0
    out, trades = run(root, unds, a.rules, spec)
    write_outputs(out, trades, spec, rdir)
    print(f"完成：{len(out['results'])} 个组合，{len(trades)} 笔 -> {rdir}/options_rules_v1.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
