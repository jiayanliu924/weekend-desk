"""卡 L1：期权研究室引擎（设计：knowledge/options_lab_loop.md，§1 / §3 / §4 是硬规则）。

agents 提方案（dict）-> 代码校验 + 查重 -> 开发段真实报价回测（desk/options_v2_backtest.py 的引擎，一行不复制）-> 代码打分 -> 研究日志；
每周最多一个方案进金库考试（代码执行，结果只回 {"hash","passed"}）。全局试验账本记下每一次回测，DSR / PBO 都按它放大门槛。

文件（都在 <data>/lab/ 下；报告在 <data>/reports/）：
  trials.jsonl  全局试验账本。首次使用时写两行种子：v1 = 32、v2 = 28（卡 A3/P2 登记的历史尝试，只有总数没有明细）；
                之后每个"新哈希"的开发段回测追加 1 行（who, hash, time），每次金库考试追加 1 行。n_trials() = 所有行的 n 之和。
  log.jsonl     研究日志：每个提案 1 行（who、时间、规范化方案、哈希、校验结果、开发段指标或原因、得分、逐周盈亏）。
  vault.jsonl   金库考试全部数字（只给人看）。lab_vault_<hash>.md 是同一份的中文报告。

========================================================================================================================
方案格式（proposal，全部字段；多余字段 = 无效）
  why           一句经济逻辑（+ 引用研究底稿条目），必填，不进哈希
  name          可选，忽略
  underlyings   v2 标的的子集（["SPY","NVDA"]，或组名 "index_like" / "all" / "earnings"）
  legs          1–4 条 {"side": buy|sell, "type": call|put, "strike_pct": 0.90–1.10（0.01 一档）}
  dte           目标到期天数 14–60；实际取 [dte−7, dte+7] ∩ [14, 60] 里最接近 dte 的到期日
  entry         ≤3 个入场条件，[{"name": ..., "value": ...}] 或 {name: value}；空 = 没持仓就开（和 v2 的 always 一样）
  exit_dte / take_profit / stop_loss / max_hold_days / cooldown_days   出场与冷却（缺省值 = v2 登记值；取值见下面的离散档）
  组合有"财报前夜"条件时出场固定为"公布后第一个交易日收盘平"，exit_dte / take_profit / stop_loss / max_hold_days 被忽略（不进哈希）。

结构（只认这六种；其它形状 = 无效）：单腿（只能买）、垂直价差、跨式、宽跨、铁鹰、铁蝶。
  "卖出的腿必须有同类型买入腿封顶"：每个类型（call / put）里 卖腿数 ≤ 买腿数，否则 = 裸卖 = 无效（最大亏损必须有限）。
  借方价差（买近平值腿、卖远腿）允许，走借方出场规则；铁鹰 / 铁蝶只认"卖内侧、买外侧"（信用）。

入场条件与阈值档（常量见下；阈值必须正好落在档上，浮点只容忍 1e-9）：
  iv_rv_ge / iv_rv_le       IV30 ÷ RV20            IV_RV_GRID
  iv_pct_ge / iv_pct_le     IV30 在过去 252 个交易日 IV30 里的分位（只用 d−1 及以前的每日 IV30，中间值算一半；过去有效值 < 60 个 = 算不出）  IV_PCT_GRID
  term_slope_ge / _le       期限斜率 = IV(≈55 天) − IV(≈30 天)（v2 的 IV30 定义推广到 55 天，两个到期日线性插值）  TERM_SLOPE_GRID
  rv20_ge / rv20_le         RV20 水平（年化，d−20 … d−1 的日对数收益）   RV20_GRID
  weekday_eq                星期几（0 = 周一 … 4 = 周五）   WEEKDAY_GRID
  earnings_eve              财报前夜（knowledge/earnings_history.json；只能配 NVDA / AMD / MU / INTC）   无阈值（值只能是 true）
  trend_R1 … trend_R4       v1 的四个趋势信号（options_rules_backtest.signal），无阈值（R1 要 IEF 日线）
  同一个条件名不能出现两次；同一族的 _ge 阈值不得大于 _le 阈值（空区间 = 无效）。
  无前视：已实现的量（RV20、均线、趋势信号）只用 d−1 及以前的收盘（趋势信号整体按 d−1 的收盘算，不用 d 当天价）；
  隐含的量（IV30、分位、期限斜率）用 d 当天 ≥15:50 ET 的最后一条报价（和 v2 一样）。

出场 / 冷却的离散档：take_profit（信用：占权利金比例；借方：占本金比例）、stop_loss（信用：权利金的倍数；借方：本金比例）、
  max_hold_days（交易日）、cooldown_days；exit_dte 取整数 ≥7 且 ≤ （入场窗口最小到期天数 − 7），保证至少持有一周。

========================================================================================================================
三段数据（§1）：开发段 2024-01-02 → 2025-06-30（agents 每轮可看，打分只用它）；金库段 2025-07-01 → 最近（agents 永远看不到）。
run_dev 的硬锁：入场只在开发段；报价只加载到"开发段最后一个交易日之后再 max_hold_days 个交易日"（财报单 1 个）——
  **为什么要多读这几天**：开发段最后几天开的仓位要平仓，平仓价必须有报价；多读的只是这些仓位的出场，入场、信号、
  逐周盈亏（按入场周归属）都不碰 2025-07-01 之后的任何东西。月文件名晚于这个截止日的 parquet 根本不打开；打开的月文件里晚于截止日的行丢掉。
  到截止日仓位仍没平：沿用 v2 引擎规则（最后一次完整报价平仓 open_at_end；一直没报价按最大亏损 no_more_quotes，保守）。

统计口径（可核对的出处）：
- Deflated Sharpe Ratio：Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and
  Non-Normality", Journal of Portfolio Management 40(5), 94–107。见 deflated_sharpe()。
- PBO（回测过拟合概率）、CSCV：Bailey, Borwein, López de Prado & Zhu (2017), "The Probability of Backtest Overfitting",
  Journal of Computational Finance 20(4), 39–69。见 pbo()。
- 周块自助 p：v2 的 boot_p（H0 均值 = 0，按 ISO 日历周整周重抽，含空周）。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from itertools import combinations
from pathlib import Path
from statistics import NormalDist

import numpy as np

from . import options_rules_backtest as v1
from . import options_v2_backtest as v2

# ---------------------------------------------------------------- 常量
DEV_START, DEV_END, VAULT_START = "2024-01-02", "2025-06-30", "2025-07-01"
assert (DEV_START, DEV_END, VAULT_START) == (v1.SAMPLE_START, v1.TRAIN_END, v1.TEST_START)
DEV_WEEKS = v2.weeks_between(DEV_START, DEV_END)          # 开发段全部 ISO 日历周（含没交易的周）
SEED_TRIALS = (("v1", 32, "卡 A3：4 条买方规则 × 8 标的"), ("v2", 28, "卡 P2：六族 28 次"))
LAB_EXTRA_SUB = "cbbo1m_extra_lab"                        # 研究室自己的补数据目录（opra_fetch --extra --extra-sub cbbo1m_extra_lab）
LAB_NEEDED_FILE = "extra_needed_lab.json"                 # meta/ 下的补数据清单（opra_fetch --needed-file extra_needed_lab.json）
EXTRA_SUBS = v2.EXTRA_SUBS + (LAB_EXTRA_SUB,)

MAX_LEGS, MAX_CONDITIONS = 4, 3
STRIKE_PCT_MIN, STRIKE_PCT_MAX = 0.90, 1.10
DTE_MIN, DTE_MAX, DTE_WINDOW = 14, 60, 7
EXIT_DTE_MIN, MIN_HOLD_CAL_DAYS = 7, 7
WHY_MAX = 600

IV_RV_GRID = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5)
IV_PCT_GRID = (0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9)
TERM_SLOPE_GRID = (-0.04, -0.02, 0.0, 0.02, 0.04)
RV20_GRID = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
WEEKDAY_GRID = (0, 1, 2, 3, 4)
IV_PCT_WINDOW, IV_PCT_MIN_OBS = 252, 60
TERM_LONG_DAYS, TERM_SHORT_DAYS = 55, 30
CONDITIONS: dict[str, tuple] = {
    "iv_rv_ge": IV_RV_GRID, "iv_rv_le": IV_RV_GRID, "iv_pct_ge": IV_PCT_GRID, "iv_pct_le": IV_PCT_GRID,
    "term_slope_ge": TERM_SLOPE_GRID, "term_slope_le": TERM_SLOPE_GRID, "rv20_ge": RV20_GRID, "rv20_le": RV20_GRID,
    "weekday_eq": WEEKDAY_GRID, "earnings_eve": (True,),
    "trend_R1": (True,), "trend_R2": (True,), "trend_R3": (True,), "trend_R4": (True,)}
COND_ORDER = ["weekday_eq", "trend_R1", "trend_R2", "trend_R3", "trend_R4", "rv20_ge", "rv20_le", "iv_rv_ge", "iv_rv_le",
              "iv_pct_ge", "iv_pct_le", "term_slope_ge", "term_slope_le"]    # 便宜的先判，一个 False 就不再算 IV

TP_CREDIT_GRID, SL_CREDIT_GRID = (0.25, 0.5, 0.75), (1.0, 1.5, 2.0, 3.0)
TP_DEBIT_GRID, SL_DEBIT_GRID = (0.25, 0.5, 1.0), (0.25, 0.35, 0.5)
MAX_HOLD_GRID, COOLDOWN_GRID = (10, 15, 20, 25), (1, 2, 5)
DEFAULTS = {"credit": {"take_profit": 0.5, "stop_loss": 2.0, "max_hold_days": 25, "cooldown_days": 1},
            "debit": {"take_profit": 0.5, "stop_loss": 0.35, "max_hold_days": 15, "cooldown_days": 5}, "exit_dte": 7}
ALLOWED_KEYS = {"name", "why", "underlyings", "legs", "dte", "entry", "exit_dte", "take_profit", "stop_loss", "max_hold_days", "cooldown_days"}

# 打分（§2.5）
DSR_CAP = 5.0
COMPLEXITY_FREE, COMPLEXITY_COST = 3, 0.5
CRASH_MULT, CRASH_PENALTY = 3.0, 2.0          # 任一暴跌窗口持仓盈亏 < −3 × 平均最大亏损 -> 一次性扣 2 分（toml/设计没给数，取 2）
INVALID_POINTS, NO_DSR_POINTS = -1.0, -2.0    # 无效/重复 −1；跑了但没有交易 / 算不出 DSR（周盈亏方差为 0）−2
PBO_BLOCKS, PBO_MAX = 8, 0.5
EULER_GAMMA = 0.5772156649015329
WEEKS_PER_YEAR = 52


# ---------------------------------------------------------------- 路径 / 环境
@dataclass(frozen=True)
class LabEnv:
    data_dir: Path
    spec_path: Path = v2.SPEC_PATH
    earnings_path: Path = v2.EARNINGS_PATH

    @classmethod
    def at(cls, data_dir, **kw) -> "LabEnv":
        return cls(Path(data_dir), **kw)

    @property
    def opra(self) -> Path:
        return self.data_dir / "opra"

    @property
    def lab(self) -> Path:
        return self.data_dir / "lab"

    @property
    def reports(self) -> Path:
        return self.data_dir / "reports"

    @property
    def trials_path(self) -> Path:
        return self.lab / "trials.jsonl"

    @property
    def log_path(self) -> Path:
        return self.lab / "log.jsonl"

    @property
    def vault_path(self) -> Path:
        return self.lab / "vault.jsonl"


def default_env() -> LabEnv:
    from . import config
    return LabEnv.at(config.load().data)


def _env(env: LabEnv | None) -> LabEnv:
    return env or default_env()


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _ts(now: datetime | None = None) -> str:
    return _now(now).isoformat(timespec="seconds")


def _jd(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    raise TypeError(type(o))


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=_jd, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(path: Path, bad: dict | None = None) -> list[dict]:
    """逐行读 jsonl；坏行默认跳过，bad 给了就用 bad 顶替（试验账本的坏行按 1 次试验算，宁多不少）。"""
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            if isinstance(d, dict):
                out.append(d)
                continue
        except ValueError:
            pass
        if bad is not None:
            out.append(dict(bad))
    return out


# ---------------------------------------------------------------- 方案校验
class _Invalid(Exception):
    pass


_UNIV: dict | None = None


def _universe() -> dict:
    global _UNIV
    if _UNIV is None:
        _UNIV = v2.load_spec()["universe"]
    return _UNIV


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _on_grid(x, grid: tuple):
    """x 正好在离散档上（容忍 1e-9）-> 返回档上的规范值，否则 None。"""
    if isinstance(x, bool) or not _is_num(x):
        return None
    for g in grid:
        if abs(x - g) < 1e-9:
            return g
    return None


def _int_like(x) -> int | None:
    if isinstance(x, bool) or not _is_num(x) or abs(x - round(x)) > 1e-9:
        return None
    return int(round(x))


def _expand_unds(u) -> list[str]:
    uni = _universe()
    items = [u] if isinstance(u, str) else u
    if not isinstance(items, list) or not items:
        raise _Invalid("underlyings: need a non-empty list of tickers (or a group name: index_like / all / earnings)")
    out: set[str] = set()
    for it in items:
        if not isinstance(it, str):
            raise _Invalid("underlyings: entries must be strings")
        if it in ("index_like", "all", "earnings"):
            out.update(uni[it])
        elif it in uni["all"]:
            out.add(it)
        else:
            raise _Invalid(f"underlyings: {it!r} is not in the v2 universe {uni['all']}")
    return sorted(out)


def _classify(legs: list[dict]) -> tuple[str, str]:
    """(structure, kind)；kind = credit / debit。形状不在六种里 -> _Invalid。legs 已校验过字段。"""
    for t in ("call", "put"):                              # 裸卖检查：每个类型卖腿数 ≤ 买腿数
        ns = sum(1 for l in legs if l["type"] == t and l["side"] == "sell")
        nb = sum(1 for l in legs if l["type"] == t and l["side"] == "buy")
        if ns > nb:
            raise _Invalid(f"naked_short: {ns} sold {t}(s) but only {nb} bought {t}(s) to cap them (every sold leg needs a bought leg of the same type)")
    n = len(legs)
    if n == 1:
        return "single", "debit"                          # 覆盖检查已保证是买腿
    if n == 2:
        a, b = legs
        if a["type"] == b["type"]:
            if a["side"] == b["side"]:
                raise _Invalid("structure_not_allowed: two same-type legs on the same side")
            if abs(a["strike_pct"] - b["strike_pct"]) < 1e-9:
                raise _Invalid("structure_not_allowed: vertical needs two different strikes")
            sold, bought = (a, b) if a["side"] == "sell" else (b, a)
            near = sold["strike_pct"] > bought["strike_pct"] if a["type"] == "put" else sold["strike_pct"] < bought["strike_pct"]
            return "vertical", ("credit" if near else "debit")      # 卖的是更靠近平值（put 更高 / call 更低）的一条 = 信用价差
        if a["side"] != b["side"]:
            raise _Invalid("structure_not_allowed: call and put on opposite sides (the sold leg would be uncovered)")
        call, put = (a, b) if a["type"] == "call" else (b, a)
        if abs(call["strike_pct"] - put["strike_pct"]) < 1e-9:
            return "straddle", "debit"
        if put["strike_pct"] < call["strike_pct"]:
            return "strangle", "debit"
        raise _Invalid("structure_not_allowed: strangle needs put strike below call strike")
    if n == 4:
        sp = [l for l in legs if l["type"] == "put" and l["side"] == "sell"]
        bp = [l for l in legs if l["type"] == "put" and l["side"] == "buy"]
        sc = [l for l in legs if l["type"] == "call" and l["side"] == "sell"]
        bc = [l for l in legs if l["type"] == "call" and l["side"] == "buy"]
        if len(sp) == len(bp) == len(sc) == len(bc) == 1:
            p_s, p_b, c_s, c_b = sp[0]["strike_pct"], bp[0]["strike_pct"], sc[0]["strike_pct"], bc[0]["strike_pct"]
            if p_b < p_s <= c_s < c_b:
                return ("iron_butterfly" if abs(p_s - c_s) < 1e-9 else "iron_condor"), "credit"
        raise _Invalid("structure_not_allowed: 4 legs must be an iron condor / iron butterfly (sell inner put+call, buy outer put below and call above)")
    raise _Invalid(f"structure_not_allowed: {n} legs (allowed: single, vertical, straddle, strangle, iron condor, iron butterfly)")


def _parse_conditions(entry) -> list[tuple[str, object]]:
    if entry is None:
        return []
    if isinstance(entry, dict):
        items = list(entry.items())
    elif isinstance(entry, list):
        items = []
        for c in entry:
            if not isinstance(c, dict) or "name" not in c or set(c) - {"name", "value"}:
                raise _Invalid("entry: each condition must be {\"name\": ..., \"value\": ...}")
            items.append((c["name"], c.get("value", True)))
    else:
        raise _Invalid("entry: must be a list of conditions (or a {name: value} dict)")
    if len(items) > MAX_CONDITIONS:
        raise _Invalid(f"entry: {len(items)} conditions, max {MAX_CONDITIONS}")
    out, seen = [], set()
    for name, val in items:
        if name not in CONDITIONS:
            raise _Invalid(f"entry: condition {name!r} is not allowed (allowed: {sorted(CONDITIONS)})")
        if name in seen:
            raise _Invalid(f"entry: condition {name!r} used twice")
        seen.add(name)
        grid = CONDITIONS[name]
        if grid == (True,):
            if val is not True:
                raise _Invalid(f"entry: {name} takes no threshold (value must be true)")
            out.append((name, True))
            continue
        g = _on_grid(val, grid)
        if g is None:
            raise _Invalid(f"entry: {name} threshold {val!r} is off the allowed grid {list(grid)}")
        out.append((name, int(g) if name == "weekday_eq" else float(g)))
    vals = dict(out)
    for fam in ("iv_rv", "iv_pct", "term_slope", "rv20"):
        if f"{fam}_ge" in vals and f"{fam}_le" in vals and vals[f"{fam}_ge"] > vals[f"{fam}_le"]:
            raise _Invalid(f"entry: {fam}_ge {vals[f'{fam}_ge']} > {fam}_le {vals[f'{fam}_le']} is an empty range")
    return sorted(out)


def _validate(p) -> dict:
    if not isinstance(p, dict):
        raise _Invalid("proposal must be a JSON object")
    extra = sorted(set(p) - ALLOWED_KEYS)
    if extra:
        raise _Invalid(f"unknown_field: {extra} (allowed: {sorted(ALLOWED_KEYS)})")
    why = p.get("why")
    if not isinstance(why, str) or not why.strip():
        raise _Invalid("why: a one-sentence economic rationale is required")
    if len(why) > WHY_MAX:
        raise _Invalid(f"why: longer than {WHY_MAX} characters")
    unds = _expand_unds(p.get("underlyings"))
    # 腿
    raw_legs = p.get("legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        raise _Invalid("legs: need a non-empty list")
    if len(raw_legs) > MAX_LEGS:
        raise _Invalid(f"legs: {len(raw_legs)} legs, max {MAX_LEGS}")
    legs = []
    for l in raw_legs:
        if not isinstance(l, dict) or set(l) != {"side", "type", "strike_pct"}:
            raise _Invalid("legs: each leg must be exactly {side, type, strike_pct}")
        if l["side"] not in ("buy", "sell") or l["type"] not in ("call", "put"):
            raise _Invalid("legs: side must be buy|sell and type call|put")
        k = l["strike_pct"]
        if not _is_num(k):
            raise _Invalid("legs: strike_pct must be a number")
        if not (STRIKE_PCT_MIN - 1e-9 <= k <= STRIKE_PCT_MAX + 1e-9):
            raise _Invalid(f"legs: strike_pct {k} outside {STRIKE_PCT_MIN}–{STRIKE_PCT_MAX}")
        if abs(k * 100 - round(k * 100)) > 1e-6:
            raise _Invalid(f"legs: strike_pct {k} must be on a 0.01 grid")
        legs.append({"side": l["side"], "type": l["type"], "strike_pct": round(float(k), 2)})
    if len({(l["type"], l["strike_pct"], l["side"]) for l in legs}) != len(legs):
        raise _Invalid("legs: duplicate legs")
    structure, kind = _classify(legs)
    legs.sort(key=lambda l: (l["type"], l["strike_pct"], l["side"]))
    # 到期
    dte = _int_like(p.get("dte"))
    if dte is None or not DTE_MIN <= dte <= DTE_MAX:
        raise _Invalid(f"dte: must be an integer {DTE_MIN}–{DTE_MAX}")
    lo = max(DTE_MIN, dte - DTE_WINDOW)
    # 条件
    conds = _parse_conditions(p.get("entry"))
    names = {c[0] for c in conds}
    earn = "earnings_eve" in names
    if earn and not set(unds) <= set(_universe()["earnings"]):
        raise _Invalid(f"entry: earnings_eve only works with {_universe()['earnings']} (the earnings calendar covers only those)")
    # 出场
    cd = p.get("cooldown_days", DEFAULTS[kind]["cooldown_days"])
    if _on_grid(cd, COOLDOWN_GRID) is None:
        raise _Invalid(f"cooldown_days: {cd!r} not in {list(COOLDOWN_GRID)}")
    cd = int(_on_grid(cd, COOLDOWN_GRID))
    if earn:
        ex = {"mode": "next_session_close"}
    else:
        ed = p.get("exit_dte", DEFAULTS["exit_dte"])
        ed = _int_like(ed)
        if ed is None or ed < EXIT_DTE_MIN:
            raise _Invalid(f"exit_dte: must be an integer >= {EXIT_DTE_MIN}")
        if ed > lo - MIN_HOLD_CAL_DAYS:
            raise _Invalid(f"exit_dte: {ed} too high for dte {dte} (max {lo - MIN_HOLD_CAL_DAYS} = lowest entry DTE {lo} minus {MIN_HOLD_CAL_DAYS} days minimum hold)")
        tpg, slg = (TP_CREDIT_GRID, SL_CREDIT_GRID) if kind == "credit" else (TP_DEBIT_GRID, SL_DEBIT_GRID)
        tp, sl = p.get("take_profit", DEFAULTS[kind]["take_profit"]), p.get("stop_loss", DEFAULTS[kind]["stop_loss"])
        mh = p.get("max_hold_days", DEFAULTS[kind]["max_hold_days"])
        if _on_grid(tp, tpg) is None:
            raise _Invalid(f"take_profit: {tp!r} not in {list(tpg)} ({kind} structure)")
        if _on_grid(sl, slg) is None:
            raise _Invalid(f"stop_loss: {sl!r} not in {list(slg)} ({kind} structure)")
        if _on_grid(mh, MAX_HOLD_GRID) is None:
            raise _Invalid(f"max_hold_days: {mh!r} not in {list(MAX_HOLD_GRID)}")
        ex = {"mode": "standard", "exit_dte": ed, "take_profit": float(_on_grid(tp, tpg)), "stop_loss": float(_on_grid(sl, slg)),
              "max_hold_days": int(_on_grid(mh, MAX_HOLD_GRID))}
    return {"v": 1, "underlyings": unds, "legs": legs, "structure": structure, "kind": kind, "dte": dte,
            "conditions": [{"name": n, "value": v} for n, v in conds], "exit": ex, "cooldown_days": cd}


def validate_proposal(p: dict) -> tuple[dict | None, str]:
    """(规范化方案, "") 或 (None, 原因)。规则见模块文档（设计 §4）。规范化方案不含 why / name；字段顺序、腿顺序、条件顺序、
    数值类型（阈值一律 float / int）都是确定的，所以同一个方案不管怎么写，spec_hash 都一样。"""
    try:
        return _validate(p), ""
    except _Invalid as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - 任何奇怪输入都只是"无效方案"
        return None, f"malformed: {type(e).__name__}: {e}"


def spec_hash(normalized: dict) -> str:
    """规范化 JSON（键排序、紧凑分隔）的 sha256 前 16 位。"""
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 试验账本
def ensure_ledger(env: LabEnv | None = None) -> None:
    """首次使用写两行种子（v1 = 32、v2 = 28）。多进程安全（文件锁）。"""
    env = _env(env)
    env.trials_path.parent.mkdir(parents=True, exist_ok=True)
    with open(env.trials_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            if not f.read().strip():
                for ver, n, note in SEED_TRIALS:
                    f.write(json.dumps({"kind": "seed", "version": ver, "n": n, "note": note}, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def n_trials(env: LabEnv | None = None) -> int:
    env = _env(env)
    ensure_ledger(env)
    return int(sum(int(r.get("n", 1)) for r in _read(env.trials_path, bad={"n": 1})))


def record_trial(env: LabEnv, kind: str, who: str, h: str, now: datetime | None = None) -> None:
    ensure_ledger(env)
    _append(env.trials_path, {"kind": kind, "n": 1, "who": who, "hash": h, "ts": _ts(now)})


# ---------------------------------------------------------------- 统计：DSR / PBO / 周盈亏
def weekly_stats(vals) -> dict:
    """周盈亏序列 -> {sr（每周，不年化）, skew, kurt（非超额）, T}。方差为 0 / 不足 2 周 -> sr None。"""
    a = np.asarray(vals, float)
    T = len(a)
    if T < 2:
        return {"sr": None, "skew": None, "kurt": None, "T": T}
    sd = a.std(ddof=1)
    m2 = float(np.mean((a - a.mean()) ** 2))
    if sd <= 1e-12 or m2 <= 1e-12:
        return {"sr": None, "skew": None, "kurt": None, "T": T}
    z = a - a.mean()
    return {"sr": float(a.mean() / sd), "skew": float(np.mean(z ** 3) / m2 ** 1.5), "kurt": float(np.mean(z ** 4) / m2 ** 2), "T": T}


def expected_max_sr(n_trials_: int, var_sr: float) -> float:
    """SR0 = sqrt(V) × [(1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))]：N 次试验里"纯运气"能达到的最大夏普的期望（BLdP 2014，式 (6)）。"""
    if n_trials_ <= 1 or var_sr <= 0:
        return 0.0
    nd = NormalDist()
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * nd.inv_cdf(1 - 1 / n_trials_) + EULER_GAMMA * nd.inv_cdf(1 - 1 / (n_trials_ * math.e)))


def deflated_sharpe(sr: float, T: int, skew: float, kurt: float, n_trials_: int, var_sr: float) -> dict:
    """Deflated Sharpe Ratio（Bailey & López de Prado 2014, JPM 40(5)）。

    sr   = 被评估策略的**每期**夏普（本模块用每周，不年化）；T = 观测期数（周数）；
    skew / kurt = 该策略周收益的偏度、峰度（非超额，正态 = 3）；
    n_trials_ = 全局试验次数 N（n_trials()）；var_sr = 全部试验的每期夏普的方差 V。
    SR0 = expected_max_sr(N, V)；
    z = (sr − SR0) · sqrt(T − 1) / sqrt(1 − skew·sr + (kurt − 1)/4 · sr²)；论文的 DSR = Φ(z) = prob。
    **本研究室的"DSR"数值取 z（设计里"DSR>0""提升封顶 ±5"只有对 z 才有意义；Φ(z) ∈ [0,1] 放不下 ±5）**：z > 0 ⇔ prob > 0.5 ⇔
    夏普超过了"N 次试验里运气能到的最大夏普"。分母 ≥ 0 恒成立（kurt ≥ skew² + 1）；退化到 0 时返回 None。"""
    sr0 = expected_max_sr(n_trials_, var_sr)
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if T < 2 or den <= 1e-12:
        return {"sr0": sr0, "z": None, "prob": None}
    z = (sr - sr0) * math.sqrt(T - 1) / math.sqrt(den)
    return {"sr0": sr0, "z": z, "prob": NormalDist().cdf(z)}


def pbo(matrix, S: int = PBO_BLOCKS) -> float | None:
    """回测过拟合概率（CSCV：Bailey, Borwein, López de Prado & Zhu 2017, J. Computational Finance 20(4)）。
    matrix = 策略 × 周 的盈亏；周按时间顺序切成 S 个等长块（周数不能被 S 整除就丢掉最早的零头；< 2S 周 = None），
    取其中 S/2 块做样本内（C(S, S/2) 种，S=8 → 70 种），其余做样本外；样本内夏普最高的策略，在样本外 N 个策略里的名次 r（平手取中位名次），
    相对名次 ω = r/(N+1)，λ = ln(ω/(1−ω))；PBO = λ < 0（样本内最优的在样本外低于中位数）的组合占比，λ = 0 算半个（纯噪声期望正好 0.5）。
    策略数 < 2 -> None。每块内的夏普 = 均值/标准差（std = 0 记 0）。"""
    M = np.asarray(matrix, float)
    if M.ndim != 2 or M.shape[0] < 2 or S % 2:
        return None
    N, T = M.shape
    if T < 2 * S:
        return None
    T2 = (T // S) * S
    M = M[:, T - T2:]
    blocks = np.array_split(np.arange(T2), S)

    def sharpe(cols):
        sub = M[:, cols]
        sd = sub.std(axis=1, ddof=1)
        return np.where(sd > 1e-12, sub.mean(axis=1) / np.where(sd > 1e-12, sd, 1.0), 0.0)
    below, n = 0.0, 0
    for comb in combinations(range(S), S // 2):
        ins = np.concatenate([blocks[b] for b in comb])
        oos = np.concatenate([blocks[b] for b in range(S) if b not in comb])
        best = int(np.argmax(sharpe(ins)))
        perf = sharpe(oos)
        r = 1.0 + float(np.sum(perf < perf[best] - 1e-12)) + 0.5 * float(np.sum(np.abs(perf - perf[best]) <= 1e-12) - 1)
        omega = r / (N + 1)
        lam = math.log(omega / (1 - omega))
        below += 1.0 if lam < -1e-12 else (0.5 if abs(lam) <= 1e-12 else 0.0)
        n += 1
    return below / n


# ---------------------------------------------------------------- 研究日志
def read_log(env: LabEnv | None = None) -> list[dict]:
    return _read(_env(env).log_path)


def _ran(e: dict) -> bool:
    """这一行是"真的跑过开发段回测"的提案（有哈希、有指标、不是重复）。"""
    return bool(e.get("backtested")) and isinstance(e.get("dev"), dict) and isinstance(e.get("weekly"), list)


def _sr_pop(log: list[dict]) -> list[float]:
    out = []
    for e in log:
        if _ran(e):
            sr = weekly_stats(e["weekly"])["sr"]
            if sr is not None:
                out.append(sr)
    return out


def _var_sr(srs: list[float], T: int) -> float:
    """全部开发段回测的每周夏普的样本方差；不足 2 个时退回 1/(T−1)（iid 正态、真实夏普 ≈ 0 时夏普估计量的渐近方差）。"""
    if len(srs) >= 2:
        return float(np.var(srs, ddof=1))
    return 1.0 / max(T - 1, 1)


def best_dev(env: LabEnv | None = None, log: list[dict] | None = None) -> dict | None:
    """日志里开发段 DSR 最高的一行的 dev 指标（当时算的）；没有 -> None。"""
    log = read_log(env) if log is None else log
    best = None
    for e in log:
        if _ran(e) and e["dev"].get("dsr") is not None and (best is None or e["dev"]["dsr"] > best["dsr"]):
            best = e["dev"]
    return best


def score_dev(result: dict | None, best_so_far: dict | float | None) -> tuple[float, str]:
    """§2.5 开发段打分（返回 (分, 原因)）：
    分 = clip(DSR − 当前最好的 DSR, ±5) − 0.5 × max(0, 复杂度 − 3) − 暴跌罚分；格式错 / 越界 / 重复 = −1。
    best_so_far = 当前最好方案的 dev 指标 dict（取 "dsr"）或数字；没有最好方案 = 以 DSR 0（刚好超过去膨胀基准）为基线。
    复杂度 = 入场条件数 + 腿数。暴跌罚分：两个暴跌窗口里任一个的持仓期间盈亏 < −3 × 平均最大亏损 -> 扣 2 分（一次）。
    跑了但没有交易 / DSR 算不出 = −2（比无效更差：无效至少是格式问题，这是"提了个不交易的方案"）。"""
    if result is None or result.get("invalid"):
        return INVALID_POINTS, f"invalid: {(result or {}).get('invalid') or 'invalid proposal'}"
    dsr = result.get("dsr")
    if dsr is None:
        return NO_DSR_POINTS, "no measurable DSR (no trades or zero weekly variance)"
    base = best_so_far.get("dsr") if isinstance(best_so_far, dict) else best_so_far
    base = 0.0 if base is None else float(base)
    imp = max(-DSR_CAP, min(DSR_CAP, dsr - base))
    cx = int(result.get("complexity", 0))
    cpen = COMPLEXITY_COST * max(0, cx - COMPLEXITY_FREE)
    why = [f"DSR {dsr:+.2f} vs best {base:+.2f} -> {imp:+.2f}"]
    if cpen:
        why.append(f"complexity {cx} -> -{cpen:.2f}")
    crash = 0.0
    aml = result.get("avg_max_loss")
    if aml:
        bad = [k for k, v in (result.get("overlap") or {}).items() if v < -CRASH_MULT * aml]
        if bad:
            crash = CRASH_PENALTY
            why.append(f"crash loss beyond {CRASH_MULT:g}x avg max loss in {', '.join(bad)} -> -{crash:.2f}")
    return round(imp - cpen - crash, 3), "; ".join(why)


# ---------------------------------------------------------------- 条件（入场信号）
class LabCtx(v2.Ctx):
    """v2.Ctx + 研究室条件要用的缓存（IV 分位、期限斜率）和趋势信号用的 IEF。"""

    def __init__(self, und, days, adj, raw, q, spec, ief=None, ief_pos=None):
        super().__init__(und, days, adj, raw, q, spec)
        self.ief, self.ief_pos = ief, ief_pos
        self._slope: dict[int, float | None] = {}
        self._pct: dict[int, float | None] = {}

    def iv_pct(self, i: int) -> float | None:
        """IV30 在 d−252 … d−1 的每日 IV30 里的分位（严格小于的占比 + 相等的一半）；有效历史 < 60 个 = None。只用过去的每日快照。"""
        if i not in self._pct:
            cur = self.iv(i)
            res = None
            if cur is not None:
                past = [x for x in (self.iv(j) for j in range(max(0, i - IV_PCT_WINDOW), i)) if x is not None]
                if len(past) >= IV_PCT_MIN_OBS:
                    res = (sum(x < cur for x in past) + 0.5 * sum(x == cur for x in past)) / len(past)
            self._pct[i] = res
        return self._pct[i]

    def term_slope(self, i: int) -> float | None:
        """IV(≈55 天) − IV(≈30 天)，都是当天 ≥15:50 快照。两边都只能用同一个单一到期日（slope 恒为 0，没有信息）-> None。"""
        if i not in self._slope:
            far, dfar = v2.iv_at(self.q, self.ords[i], self.raw[i], self.spec, TERM_LONG_DAYS)
            near, dnear = v2.iv_at(self.q, self.ords[i], self.raw[i], self.spec, TERM_SHORT_DAYS)
            ok = far is not None and near is not None and not (len(dfar) == 1 and dfar == dnear)
            self._slope[i] = (far - near) if ok else None
        return self._slope[i]

    def trend(self, rule: str, i: int) -> bool | None:
        """v1 趋势信号，整体按 d−1 的收盘算（不用 d 当天价）。R1 要 IEF；没有 -> None。"""
        if i < 1:
            return None
        if rule == "R1":
            if self.ief is None or self.ief_pos is None or self.ief_pos[i - 1] is None:
                return None
            return bool(v1.signal("R1", self.adj, i - 1, self.ief, self.ief_pos[i - 1]))
        return bool(v1.signal(rule, self.adj, i - 1))


def cond_signal(strat: dict, ctx: LabCtx, i: int) -> bool | None:
    """strat["conditions"] 全部成立 -> True；任一明确不成立 -> False；没有 False 但有算不出的 -> None。
    条件不含 earnings_eve（它由财报日历决定入场日，scan_entries 已处理）。"""
    cs = {c["name"]: c["value"] for c in strat["conditions"] if c["name"] != "earnings_eve"}
    unknown = False
    for name in COND_ORDER:
        if name not in cs:
            continue
        val = cs[name]
        if name == "weekday_eq":
            r = date.fromisoformat(ctx.days[i]).weekday() == val
        elif name.startswith("trend_"):
            r = ctx.trend(name[-2:], i)
        elif name.startswith("rv20"):
            rv = ctx.rv(i)
            r = None if rv is None else (rv >= val if name.endswith("_ge") else rv <= val)
        elif name.startswith("iv_rv"):
            iv, rv = ctx.iv(i), ctx.rv(i)
            r = None if iv is None or rv is None or rv <= 0 else (iv / rv >= val if name.endswith("_ge") else iv / rv <= val)
        elif name.startswith("iv_pct"):
            x = ctx.iv_pct(i)
            r = None if x is None else (x >= val if name.endswith("_ge") else x <= val)
        else:
            x = ctx.term_slope(i)
            r = None if x is None else (x >= val if name.endswith("_ge") else x <= val)
        if r is False:
            return False
        if r is None:
            unknown = True
    return None if unknown else True


# ---------------------------------------------------------------- 方案 -> v2 引擎的 strategy dict
def to_strategy(norm: dict) -> dict:
    h = spec_hash(norm)
    dte = norm["dte"]
    s = {"id": f"LAB_{h}", "underlyings": list(norm["underlyings"]), "legs": [dict(l) for l in norm["legs"]],
         "entry": "earnings_eve" if any(c["name"] == "earnings_eve" for c in norm["conditions"]) else "conditions",
         "conditions": [dict(c) for c in norm["conditions"]],
         "expiry": {"min_dte": max(DTE_MIN, dte - DTE_WINDOW), "max_dte": min(DTE_MAX, dte + DTE_WINDOW), "target_dte": dte},
         "cooldown_days": norm["cooldown_days"]}
    if norm["kind"] == "debit" and norm["structure"] == "vertical":
        s["net_debit_ok"] = True
    ex = norm["exit"]
    if ex["mode"] == "next_session_close":
        s["exit"] = "next_session_close"
        return s
    s.update(exit_dte=ex["exit_dte"], max_hold_days=ex["max_hold_days"])
    dc, dd = DEFAULTS["credit"], DEFAULTS["debit"]
    s.update(take_profit_frac_of_credit=dc["take_profit"], stop_loss_mult_of_credit=dc["stop_loss"],
             take_profit_frac_of_debit=dd["take_profit"], stop_loss_frac_of_debit=dd["stop_loss"])
    if norm["kind"] == "credit":
        s.update(take_profit_frac_of_credit=ex["take_profit"], stop_loss_mult_of_credit=ex["stop_loss"])
    else:
        s.update(take_profit_frac_of_debit=ex["take_profit"], stop_loss_frac_of_debit=ex["stop_loss"])
    return s


# ---------------------------------------------------------------- 回测驱动（共用：开发段 / 金库）
def _simulate(norm: dict, env: LabEnv, spec: dict, entry_first: str | None = None, entry_last: str | None = None) -> dict:
    """entry_last 给了（开发段）：报价和日线只保留到 entry_last 之后再 max_hold_days 个交易日（财报单 1 个），
    入场只到 entry_last。没给（金库）：全部数据，入场从样本起点到最新（金库成绩单靠入场日切分）。"""
    strat = to_strategy(norm)
    root = env.opra
    earn_mode = strat["entry"] == "earnings_eve"
    earn = v2.load_earnings(env.earnings_path) if earn_mode else None
    need_ief = any(c["name"] == "trend_R1" for c in norm["conditions"])
    ief_c = None
    if need_ief:
        try:
            ief_c = v1.read_closes(root, "IEF")[1]
        except FileNotFoundError:
            ief_c = None
    out = {"strat": strat, "trades": [], "per_und": {}, "skips": Counter(), "n_sig": 0, "need": {}, "unresolved": 0,
           "last_day": None, "q_last": None}
    for k, u in enumerate(norm["underlyings"]):
        try:
            raw_d, adj_d = v1.read_closes(root, u)
        except FileNotFoundError:
            out["per_und"][u] = {"status": "no_data", "trades": 0}
            continue
        days = sorted(set(adj_d) & set(raw_d))
        last_day = None
        if entry_last is not None:
            allow = 1 if earn_mode else strat["max_hold_days"]
            days = days[:bisect_right(days, entry_last) + allow]
            last_day = days[-1] if days else None
            if last_day is None:
                out["per_und"][u] = {"status": "no_data", "trades": 0}
                continue
        q = v2.load_quotes(root, u, last_day=last_day, extra_subs=EXTRA_SUBS)
        if q is None:
            out["per_und"][u] = {"status": "no_data", "trades": 0}
            continue
        ief, ipos = (None, None)
        if ief_c:
            ief, ipos = v1._align_ief(days, ief_c)
        ctx = LabCtx(u, days, [adj_d[d] for d in days], [raw_d[d] for d in days], q, spec, ief, ipos)
        if earn_mode:
            if earn is None:
                out["per_und"][u] = {"status": "no_earnings_calendar", "trades": 0}
                continue
            ev, _ = v2.earnings_entries(earn.get(u, []), days)
            if not ev:
                out["per_und"][u] = {"status": "no_earnings_events", "trades": 0}
                continue
            cands = v2.scan_entries(strat, ctx, ev, first=entry_first, last=entry_last, signal_fn=cond_signal)
        else:
            cands = v2.scan_entries(strat, ctx, None, first=entry_first, last=entry_last, signal_fn=cond_signal)
        need = v2.extra_needed_for(strat, ctx, cands)
        tr, sig, sk = v2.simulate(strat, ctx, spec, cands)
        for t in tr:
            t["entry_idx"] += 100_000 * k              # 簇按标的分别数（evaluate 里按 entry_idx 聚簇，不同标的不能并成一簇）
        out["trades"] += tr
        out["per_und"][u] = {"status": "ok", "trades": len(tr), "signal_days": len(sig)}
        out["skips"].update(sk)
        out["n_sig"] += len(sig)
        out["unresolved"] += v2.still_missing(q, need)
        for day, syms in need.items():
            out["need"].setdefault(u, {}).setdefault(day, set()).update(syms)
        out["last_day"] = max(out["last_day"] or "", days[-1]) if days else out["last_day"]
        del q, ctx
    if out["need"]:
        v1.write_extra_needed(root, out["need"], LAB_NEEDED_FILE)
    return out


def _dev_metrics(sim: dict, norm: dict, srs_before: list[float], n_trials_now: int) -> tuple[dict, list[float]]:
    trades = sim["trades"]
    wk = v2.weekly_pnl(trades, DEV_WEEKS)
    wvals = [wk[w] for w in DEV_WEEKS]
    st = weekly_stats(wvals)
    var_sr = _var_sr(srs_before + ([st["sr"]] if st["sr"] is not None else []), len(DEV_WEEKS))
    d = {"sr0": None, "z": None, "prob": None}
    if st["sr"] is not None:
        d = deflated_sharpe(st["sr"], st["T"], st["skew"], st["kurt"], n_trials_now, var_sr)
    cl = sum(len(v1.clusters([t["entry_idx"] for t in trades if t["und"] == u])) for u in {t["und"] for t in trades})
    rets = [t["ret"] for t in trades]
    reasons = Counter(t["reason"] for t in trades)
    bad = reasons.get("no_more_quotes", 0) + reasons.get("stop_on_missing", 0)
    worst = min(trades, key=lambda t: t["pnl"]) if trades else None
    return {
        "n_trades": len(trades), "n_clusters": cl,
        "avg_ret": float(np.mean(rets)) if rets else None,
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "boot_p": v2.boot_p(wvals) if trades else None, "tstat": v2.tstat(wvals) if trades else None,
        "sharpe_wk": st["sr"], "sharpe_ann": (st["sr"] * math.sqrt(WEEKS_PER_YEAR)) if st["sr"] is not None else None,
        "skew": st["skew"], "kurt": st["kurt"], "n_weeks": st["T"],
        "n_trials": n_trials_now, "var_sr": var_sr, "sr0": d["sr0"], "dsr": d["z"], "dsr_prob": d["prob"],
        "overlap": {k: v2.window_pnl(trades, a, b)["overlap_pnl"] for k, (a, b) in v2.CRASH_WINDOWS.items()},
        "worst_trade": ({"pnl": worst["pnl"], "ret": worst["ret"], "entry_date": worst["entry_date"], "und": worst["und"]} if worst else None),
        "avg_max_loss": float(np.mean([t["max_loss"] for t in trades])) if trades else None,
        "complexity": len(norm["conditions"]) + len(norm["legs"]),
        "reasons": dict(reasons), "n_no_more_quotes": reasons.get("no_more_quotes", 0), "n_stop_on_missing": reasons.get("stop_on_missing", 0),
        "data_incomplete": bool(trades and bad / len(trades) > v2.INCOMPLETE_FRAC), "unresolved_quotes": sim["unresolved"],
        "per_und": sim["per_und"], "skips": dict(sim["skips"]),
    }, [round(x, 2) for x in wvals]


def _brief(norm: dict | None) -> str:
    if not norm:
        return "?"
    legs = ",".join(f"{'S' if l['side'] == 'sell' else 'B'}{l['type'][0].upper()}{l['strike_pct']:g}" for l in norm["legs"])
    conds = " & ".join(c["name"] if c["value"] is True else f"{c['name']}={c['value']:g}" for c in norm["conditions"]) or "always"
    ex = norm["exit"]
    exs = "next-session" if ex["mode"] == "next_session_close" else f"exit{ex['exit_dte']}d tp{ex['take_profit']:g} sl{ex['stop_loss']:g} hold{ex['max_hold_days']}"
    return f"{norm['structure']} {','.join(norm['underlyings'])} dte{norm['dte']} [{legs}] if {conds}; {exs}"


# ---------------------------------------------------------------- 开发段
def run_dev(p: dict, who: str, env: LabEnv | None = None, now: datetime | None = None) -> dict:
    """校验 -> 查重 -> 开发段回测（只 2024-01-02 … 2025-06-30 入场）-> 记试验账本 -> 打分 -> 写研究日志。
    返回 {"hash","valid","reason","duplicate","points","points_reason","dev"}（dev = 开发段指标；无效 / 重复时为 None）。
    无效 -> −1；哈希和日志里已跑过的重复（不管谁提的）-> 不跑、不记试验、−1；引擎抛异常（不该发生）-> 记 error 行、不记试验、0 分。"""
    env = _env(env)
    ensure_ledger(env)
    ts = _ts(now)
    line = {"who": who, "ts": ts, "hash": None, "spec": None, "why": (p.get("why") if isinstance(p, dict) and isinstance(p.get("why"), str) else None),
            "valid": False, "reason": "", "duplicate_of": None, "backtested": False, "dev": None, "weekly": None, "points": INVALID_POINTS, "points_reason": ""}
    norm, reason = validate_proposal(p)
    if norm is None:
        line.update(reason=reason, points_reason=f"invalid: {reason}")
        _append(env.log_path, line)
        return _result(line)
    h = spec_hash(norm)
    line.update(hash=h, spec=norm, valid=True)
    log = read_log(env)
    first = next((e for e in log if e.get("hash") == h and e.get("backtested")), None)
    if first:
        line.update(duplicate_of={"who": first.get("who"), "ts": first.get("ts")}, reason="duplicate of an already-tested spec",
                    points_reason="duplicate: not run, -1")
        _append(env.log_path, line)
        return _result(line)
    spec = v2.load_spec(env.spec_path)
    try:
        sim = _simulate(norm, env, spec, entry_first=DEV_START, entry_last=DEV_END)
    except Exception as e:  # noqa: BLE001
        line.update(reason=f"engine_error: {type(e).__name__}: {e}", points=0.0, points_reason="engine error (not counted as a trial)")
        _append(env.log_path, line)
        return _result(line)
    record_trial(env, "dev", who, h, now)
    dev, weekly = _dev_metrics(sim, norm, _sr_pop(log), n_trials(env))
    best = best_dev(log=log)
    pts, why = score_dev(dev, best)
    line.update(backtested=True, dev=dev, weekly=weekly, points=pts, points_reason=why)
    _append(env.log_path, line)
    return _result(line)


def _result(line: dict) -> dict:
    return {"hash": line["hash"], "valid": line["valid"], "reason": line["reason"], "duplicate": bool(line["duplicate_of"]),
            "points": line["points"], "points_reason": line["points_reason"], "dev": line["dev"]}


def vaulted_hashes(env: LabEnv | None = None) -> dict[str, dict]:
    return {e["hash"]: e for e in _read(_env(env).vault_path) if e.get("hash")}


def history_for(who: str, n: int = 10, env: LabEnv | None = None) -> str:
    """who 最近 n 个提案的紧凑文字摘要（给提示词用）：方案、开发段数字、得分。金库只给"过 / 没过"，**永远不含金库数字**。"""
    env = _env(env)
    vault = vaulted_hashes(env)
    mine = [e for e in read_log(env) if e.get("who") == who][-max(0, n):]
    if not mine:
        return "(no proposals yet)"
    lines = []
    for e in mine:
        head = f"- {str(e.get('ts', ''))[:10]} {_brief(e.get('spec'))}: " if e.get("spec") else f"- {str(e.get('ts', ''))[:10]} (invalid proposal): "
        if not e.get("valid"):
            body = f"INVALID, {e.get('reason', '')}"
        elif e.get("duplicate_of"):
            body = "DUPLICATE of an already-tested spec, not run"
        elif not _ran(e):
            body = f"not scored ({e.get('reason', '')})"
        else:
            d = e["dev"]
            if d["n_trades"] == 0:
                body = "0 trades in dev"
            else:
                ov = list(d["overlap"].values())
                dsr = "n/a" if d["dsr"] is None else format(d["dsr"], "+.2f")
                body = (f"{d['n_trades']} trades ({d['n_clusters']} clusters), avg ret on max loss {d['avg_ret']:+.1%}, total ${d['total_pnl']:+.0f}/contract, "
                        f"weekly-block p {d['boot_p']:.2f}, DSR {dsr}, "
                        f"crash overlap ${ov[0]:+.0f}/${ov[1]:+.0f}, worst ${d['worst_trade']['pnl']:+.0f}")
            body += f"; points {e['points']:+.2f} ({e.get('points_reason', '')})"
            if d.get("data_incomplete"):
                body += "; WARNING: many trades closed without exit quotes"
        if e.get("hash") in vault:
            body += f"; vault: {'PASSED' if vault[e['hash']].get('passed') else 'FAILED'}"
        lines.append(head + body)
    return "\n".join(lines)


def current_pbo(env: LabEnv | None = None, log: list[dict] | None = None) -> float | None:
    """全部（有交易的）开发段回测的逐周盈亏矩阵的 PBO。"""
    log = read_log(env) if log is None else log
    rows = [e["weekly"] for e in log if _ran(e) and e["dev"]["n_trades"] > 0 and len(e["weekly"]) == len(DEV_WEEKS)]
    return pbo(rows) if len(rows) >= 2 else None


# ---------------------------------------------------------------- 金库
def vault_exam(h: str, env: LabEnv | None = None, now: datetime | None = None) -> dict:
    """金库考试：2025-07-01 … 最新，v2 影子档过关逻辑（v2.evaluate：考试段主口径总盈亏 > 0、考试段簇数 ≥ 10、整样本净盈亏 > 0 且开发段总盈亏 > 0、
    考试段周块自助 p < 0.10、不是"好得离谱"），再加完整性闸门（> 5% 的笔以没报价收尾 = 不过）。全局 Bonferroni（0.05 / 累计试验次数）只报告不强求。
    一个哈希只考一次（再考直接返回上次结果，不加试验）。**返回值只有 {"hash","passed"}**；全部数字写 vault.jsonl 和 reports/lab_vault_<hash>.md，给人看。"""
    env = _env(env)
    ensure_ledger(env)
    done = vaulted_hashes(env).get(h)
    if done:
        return {"hash": h, "passed": bool(done["passed"])}
    ent = next((e for e in read_log(env) if e.get("hash") == h and _ran(e)), None)
    if ent is None:
        raise ValueError(f"no dev-tested proposal with hash {h}")
    norm = ent["spec"]
    spec = v2.load_spec(env.spec_path)
    record_trial(env, "vault", ent.get("who", "?"), h, now)
    sim = _simulate(norm, env, spec)
    trades = sim["trades"]
    days_end = sim["last_day"] or VAULT_START
    weeks = v2.weeks_between(VAULT_START, days_end) if days_end >= VAULT_START else []
    ev = v2.evaluate(sim["strat"], "POOL", trades, list(range(sim["n_sig"])), dict(sim["skips"]), spec, weeks=weeks)
    reasons = Counter(t["reason"] for t in trades)
    bad = reasons.get("no_more_quotes", 0) + reasons.get("stop_on_missing", 0)
    complete = not (trades and bad / len(trades) > v2.INCOMPLETE_FRAC)
    passed = bool(ev["shadow_pass"] and complete)
    nt = n_trials(env)
    bonf = 0.05 / nt
    p = ev["test_week_boot_p"]
    rec = {"hash": h, "who": ent.get("who"), "ts": _ts(now), "iso_week": v2.week_key(_now(now).date().isoformat()), "passed": passed,
           "complete": complete, "n_trials": nt, "bonferroni": bonf, "bonferroni_ok": bool(p is not None and p < bonf),
           "spec": norm, "eval": ev, "per_und": sim["per_und"], "unresolved_quotes": sim["unresolved"]}
    _append(env.vault_path, rec)
    _write_vault_report(env, rec)
    return {"hash": h, "passed": passed}


def _write_vault_report(env: LabEnv, rec: dict) -> Path:
    ev = rec["eval"]
    te, al, tr = ev["test"], ev["all"], ev["train"]

    def f(x, spec=".2f"):
        return "n/a" if x is None else format(x, spec)
    L = [f"# 金库考试报告 {rec['hash']}（只给人看，不要回流给提方案的 agent）", "",
         f"- 提案人：{rec.get('who')}；考试时间：{rec['ts']}；结论：**{'通过（进影子记账）' if rec['passed'] else '没过'}**",
         f"- 方案：{_brief(rec['spec'])}", f"- 累计试验次数（含本次）：{rec['n_trials']}；全局 Bonferroni 线 p < {rec['bonferroni']:.5f}：{'达到' if rec['bonferroni_ok'] else '没达到'}（只报告，不强求）",
         f"- 数据完整性：{'完整' if rec['complete'] else '不完整（> 5% 的笔以没报价收尾）-> 一律不过'}；补数据缺口 {rec['unresolved_quotes']} 个（合约,日）", "",
         "## 过关各条（v2 影子档）",
         f"- 考试段主口径总盈亏 > 0：{ev['crit_test_pnl_positive']}（{f(te['total_pnl'])}，{te['trades']} 笔，胜率 {f(te['win_rate'])}）",
         f"- 考试段簇数 ≥ 10：{ev['crit_clusters_ge_min']}（{ev['traded_clusters_test']} 簇）",
         f"- 整样本净盈亏 > 0：{ev['crit_whole_net_positive']}（{f(al['total_pnl'])}）；开发段总盈亏 > 0：{ev['crit_train_total_positive']}（{f(tr['total_pnl'])}）",
         f"- 考试段周块自助 p < 0.10：{ev['crit_boot_p_lt_shadow']}（p = {f(ev['test_week_boot_p'], '.4f')}，{ev['test_weeks']} 周）",
         f"- 好得离谱（当 bug 查）：{ev['too_good']}", "",
         "## 其它", f"- 最差一笔：{ev['worst_trade']}", f"- 最大连续亏损笔数 {ev['max_consec_losses']}；最大回撤 {ev['max_drawdown']}",
         f"- 暴跌窗口持仓期间盈亏：{ {k: v['overlap_pnl'] for k, v in ev['windows'].items()} }",
         f"- 各标的状态：{rec['per_und']}", f"- 跳过原因：{ev['skips']}", f"- 出场原因：{ev['reasons']}", ""]
    p = env.reports / f"lab_vault_{rec['hash']}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(L), encoding="utf-8")
    return p


def weekly_promotion(env: LabEnv | None = None, now: datetime | None = None) -> dict:
    """每周最多 1 个方案进金库（读 vault.jsonl 的 iso_week 强制）：候选 = 还没考过的、跑过开发段的方案；
    按**现在**的试验次数和方差重算 DSR（试验越多门槛越高），留下 DSR > 0 的，全局 PBO（所有有交易的开发段回测）必须 < 0.5（算不出 = 不晋级），
    在剩下的里取日志里开发段得分最高的（同分取 DSR 高的）-> vault_exam。返回 {"hash","passed"}（晋级了）或 {"skipped": 原因}。"""
    env = _env(env)
    ensure_ledger(env)
    wk = v2.week_key(_now(now).date().isoformat())
    vault = vaulted_hashes(env)
    if any(v.get("iso_week") == wk for v in vault.values()):
        return {"skipped": f"already promoted in {wk}"}
    log = read_log(env)
    ran = [e for e in log if _ran(e)]
    srs = _sr_pop(log)
    nt = n_trials(env)
    var = _var_sr(srs, len(DEV_WEEKS))
    cands = []
    for e in ran:
        if e["hash"] in vault or e["dev"]["n_trades"] == 0:
            continue
        st = weekly_stats(e["weekly"])
        if st["sr"] is None:
            continue
        d = deflated_sharpe(st["sr"], st["T"], st["skew"], st["kurt"], nt, var)
        if d["z"] is not None and d["z"] > 0:
            cands.append((e["points"], d["z"], e["hash"]))
    if not cands:
        return {"skipped": "no unvaulted candidate with DSR > 0"}
    pb = current_pbo(log=log)
    if pb is None or pb >= PBO_MAX:
        return {"skipped": f"PBO {'n/a' if pb is None else format(pb, '.2f')} not < {PBO_MAX}"}
    cands.sort(reverse=True)
    return vault_exam(cands[0][2], env, now)


def status(env: LabEnv | None = None) -> dict:
    """给人看的概览（只含计数和过 / 没过，不含金库数字）。"""
    env = _env(env)
    log = read_log(env)
    vault = vaulted_hashes(env)
    best = best_dev(log=log)
    return {"n_trials": n_trials(env), "proposals": len(log), "invalid": sum(1 for e in log if not e.get("valid")),
            "duplicates": sum(1 for e in log if e.get("duplicate_of")), "backtested": sum(1 for e in log if _ran(e)),
            "best_dev_dsr": None if best is None else best["dsr"], "pbo": current_pbo(log=log),
            "vault_exams": len(vault), "vault_passed": sum(1 for v in vault.values() if v.get("passed")),
            "last_vault_week": max((v.get("iso_week", "") for v in vault.values()), default=None)}


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="desk.options_lab")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--validate", metavar="FILE", help="校验一个方案 json，打印规范化结果和哈希")
    g.add_argument("--dev", metavar="FILE", help="开发段回测一个方案 json（需要 --who）")
    g.add_argument("--promote", action="store_true", help="每周晋级：挑一个方案进金库考试（一周最多一个）")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--who", default="cli")
    ap.add_argument("--data", help="数据目录，默认 config 的 data")
    a = ap.parse_args(argv)
    env = LabEnv.at(a.data) if a.data else default_env()
    if a.validate:
        norm, why = validate_proposal(json.loads(Path(a.validate).read_text()))
        print(json.dumps({"ok": norm is not None, "reason": why, "hash": spec_hash(norm) if norm else None, "normalized": norm}, ensure_ascii=False, indent=2))
        return 0 if norm else 1
    if a.dev:
        print(json.dumps(run_dev(json.loads(Path(a.dev).read_text()), a.who, env), ensure_ascii=False, indent=2, default=_jd))
        return 0
    if a.promote:
        print(json.dumps(weekly_promotion(env)))
        return 0
    print(json.dumps(status(env), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
