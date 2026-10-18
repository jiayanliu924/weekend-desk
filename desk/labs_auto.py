"""三条策略线自动跑一遍、统一口径、按"单位风险收益"排名，写 data/labs/latest.json 并推手机。

服务器每周一自动跑（scheduler），update.sh 更新后也跑一次；不需要人手敲命令。
本模块只调用各 lab 现成的 run()，不改它们的算法；这里只做"统一口径 + 排名 + 门槛"。

统一口径（所有 *_pct 字段都是"占本金的百分数"，例如 1.5 = 1.5%）：
- mean_wk_pct / sd_wk_pct / max_dd_pct：1 倍（不加杠杆）下的周均、周标准差、最大回撤
- ratio：周均 ÷ 周标准差（夏普式，没有年化）
- leverage：历史最大回撤 ≤ 20% 的最大杠杆（各 lab 自己算的；期权线里"杠杆"= 每周把多少比例的本金当最大亏损）
- ret_at_lev_pct：在该杠杆下的周均收益
- p_ruin_pct：52 周内净值跌到一半（腰斩）的概率（lab 自己的 bootstrap）
- passes：样本 ≥ 20 周 且 样本外全额费用下周均 > 0 且 腰斩概率 < 5%
- note_optimistic：杠杆是在同一段历史里挑的 → 偏乐观（目前三条线都是 true）
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("labs_auto")
UTC = timezone.utc
MIN_SAMPLES = 20
MAX_RUIN = 0.05
LINES = {"pairs": "跨平台同股套利", "strategy": "周末套利升级版", "options": "期权周卖组合"}
OPT_EXCLUDE = ("long_straddle", "short_strangle_15_REF")   # 只比较"卖出、有限风险"的结构

COLUMNS = [
    ("线", "这条赚钱思路的名字"),
    ("样本周数", "回测里一共有多少个星期的结果，越多越可信；不到 20 周不算数"),
    ("每周平均 %", "不加杠杆时，平均每周赚本金的百分之几"),
    ("每周波动 %", "每周收益上下摆动的幅度（标准差），越小越稳"),
    ("性价比", "每周平均 ÷ 每周波动，越大说明承担同样风险赚得越多；排名就按它"),
    ("最大回撤 %", "不加杠杆时，历史上从最高点往下最多亏过本金的百分之几"),
    ("杠杆（回撤≤20%）", "把历史最大回撤压在 20% 以内，最多能放大几倍（在同一段历史里挑的，偏乐观）"),
    ("该杠杆下每周 %", "用上面这个杠杆时，平均每周赚本金的百分之几"),
    ("52 周腰斩概率 %", "按历史收益随机重排一年，本金中途跌掉一半的可能性"),
    ("容量 $", "大约能放多少钱进去而不明显吃掉利润（没算出来就是 —）"),
    ("过门槛", "同时满足：样本≥20 周、样本外按全额手续费仍赚钱、腰斩概率<5%"),
]


def _num(x):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _pct(x):
    x = _num(x)
    return None if x is None else x * 100.0


def make_record(line: str, variant: str, n: int, mean, sd, max_dd, lev, ret_at_lev, p_ruin, capacity,
                oos_mean_pos: bool, note: str) -> dict:
    """输入的收益都是"本金的小数"（0.015 = 1.5%）；输出 *_pct 是百分数。passes 在这里统一判断。"""
    mean, sd, p_ruin = _num(mean), _num(sd), _num(p_ruin)
    ratio = mean / sd if (mean is not None and sd) else None
    passes = bool(n >= MIN_SAMPLES and oos_mean_pos and p_ruin is not None and p_ruin < MAX_RUIN)
    return {"line": LINES[line], "key": line, "variant": variant, "n_weeks": int(n),
            "mean_wk_pct": _pct(mean), "sd_wk_pct": _pct(sd), "ratio": ratio, "max_dd_pct": _pct(max_dd),
            "leverage": _num(lev), "ret_at_lev_pct": _pct(ret_at_lev), "p_ruin_pct": _pct(p_ruin),
            "capacity_usd": _num(capacity), "passes": passes, "oos_mean_positive": bool(oos_mean_pos),
            "note_optimistic": True, "note": note}


# ---------------------------------------------------------------- 各线归一化
def normalize_pairs(res: dict, notional: float = 1000.0) -> dict:
    from . import pairs_lab as pl
    best = None
    for p in res.get("pairs", []):
        r = p.get("result") or {}
        st = r.get("taker")      # taker = 全额费用口径
        if not r.get("ok") or not st or not st.get("n"):
            continue
        trades = r.get("trades_taker")
        sd = None
        if trades is not None:
            wk = pl.weekly(trades, r["oos_from"], r["t_last"])
            if len(wk) > 1:
                sd = statistics.stdev(wk) / notional
        mean = st["week_mean_usd"] / notional if st.get("week_mean_usd") is not None else None
        sh = (mean / sd) if (mean is not None and sd) else _num(st.get("week_sharpe"))
        key = sh if sh is not None else float("-inf")
        if best is None or key > best[0]:
            best = (key, p, st, mean, sd)
    if best is None:
        raise RuntimeError("没有一对能在全额费用下算出结果")
    _, p, st, mean, sd = best
    lev = st.get("leverage") or 0.0
    note = ("样本外（走前选参数）、吃单全额费用；杠杆在同一段历史里挑，偏乐观；"
            f"15 分钟 K 线只有约 {st.get('weeks', 0)} 周样本")
    return make_record("pairs", f"{p['a']} / {p['b']}", st.get("weeks", 0), mean, sd,
                       abs(st["max_dd_usd"]) / notional if st.get("max_dd_usd") is not None else None,
                       lev, (st.get("week_ret_at_lev") if lev else 0.0), st.get("p_minus50_52w"), st.get("capacity_usd"),
                       (mean or 0) > 0 and (st.get("avg_net_bps") or 0) > 0, note)


def normalize_strategy(r3: dict | None) -> dict:
    if not r3 or not r3.get("stats"):
        raise RuntimeError("没有 v3 结果（可能没有周末数据）")
    st = r3["stats"]["select"]
    if not st.get("n_weekends"):
        raise RuntimeError("没有样本外周末")
    pos = st["mean"] > 0
    note = "走前挑选的组合、全额成本（含半个点差）；杠杆在同一段样本外里挑，偏乐观"
    return make_record("strategy", "⑤ 走前挑选（12 组合）", st["n_weekends"], st["mean"], st["sd"], st["max_dd"],
                       st["lev"] if pos else 0.0, st["ret_at_lev"] if pos else 0.0, st["p_ruin"] if pos else None,
                       None, pos, note)


def normalize_options(rows: list[dict]) -> dict:
    from . import options_real_backtest as ob
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["strategy"] in OPT_EXCLUDE or r["slippage_pct"] != ob.DEFAULT_SLIP:
            continue
        groups.setdefault((r["entry_kind"], r["variant"], r["strategy"]), []).append(r)
    best = None
    for g, grp in groups.items():
        rs = [r["r"] for r in sorted(grp, key=lambda r: r["expiry"])]
        if len(rs) < 2:
            continue
        sd = statistics.stdev(rs)
        ratio = statistics.fmean(rs) / sd if sd > 0 else float("-inf")
        if best is None or ratio > best[0]:
            best = (ratio, g, rs)
    if best is None:
        raise RuntimeError("没有可用的期权样本")
    _, g, rs = best
    mean, sd = statistics.fmean(rs), statistics.stdev(rs)
    f = ob.risk_fraction(rs) if mean > 0 else 0.0
    half = rs[len(rs) // 2:]                      # 时间上靠后的一半 = 近似样本外
    ruin = _num(ob.p_ruin(rs, f)) if (f > 0 and len(rs) >= 10) else None
    note = ("真实 Deribit 成交价、含手续费和 5% 滑点；结构是固定规则没拟合参数，但最优结构是从多个里挑的，"
            "杠杆在同一段历史里挑，偏乐观；\"样本外\"用的是时间上靠后的一半周数")
    kind, var, strat = g
    return make_record("options", f"{ob.NAMES.get(strat, strat)} · {kind} · {var}", len(rs), mean, sd,
                       ob.compound_dd(rs, 1.0), f, f * mean, ruin, None,
                       statistics.fmean(half) > 0, note)


# ---------------------------------------------------------------- 各线运行（每条一个 runner，便于测试替换）
def run_pairs(settings, fetch: bool) -> dict:
    from . import pairs_lab as pl
    raw, src, fetch2 = pl.load_raw(settings, None, fetch)
    if raw is None:
        raise RuntimeError("没有 Hyperliquid 发现数据（data/universe_raw.json 不存在且连不上）")
    _, res = pl.run(settings, raw, src, days=60, interval="15m", fetch=fetch2)
    return normalize_pairs(res)


def run_strategy(settings, fetch: bool) -> dict:
    from . import backtest_lib as bl
    from . import strategy_lab as sl
    if fetch:
        ok, _ = bl.hyperliquid_reachable()
        fetch = bool(ok)
    cap: dict = {}
    sl.run(settings, fetch=fetch, capture=cap)
    return normalize_strategy(cap.get("r3"))


def run_options(settings, fetch: bool) -> dict:
    from . import options_real_backtest as ob
    src = ob.DeribitHistory(settings, "BTC", offline=not fetch)
    until = (datetime.now(UTC) - timedelta(hours=9)).date()
    try:
        _, rows = ob.run(settings, src, "BTC", date(2023, 1, 1), until)
    except ob.NetworkBlocked as e:
        raise RuntimeError(f"连不上 deribit 历史库且没有缓存：{str(e)[:80]}") from e
    return normalize_options(rows)


RUNNERS = {"pairs": run_pairs, "strategy": run_strategy, "options": run_options}


# ---------------------------------------------------------------- 排名、输出
def rank(records: list[dict]) -> list[dict]:
    ordered = sorted(records, key=lambda r: (r["ratio"] is None, -(r["ratio"] or 0.0)))
    for i, r in enumerate(ordered, 1):
        r["rank"] = i
    return ordered


def message(ranked: list[dict], errors: dict[str, str]) -> str:
    rows = []
    for r in ranked:
        ret = r["ret_at_lev_pct"] if r["ret_at_lev_pct"] is not None else 0.0
        lev = r["leverage"] or 0.0
        ruin = f"{r['p_ruin_pct']:.1f}%" if r["p_ruin_pct"] is not None else "算不出"
        rows.append(f"第{r['rank']}名 {r['line']}：每周 {ret:+.1f}%（在最大回撤 20% 以内的杠杆 {lev:.1f} 倍），"
                    f"52 周腰斩概率 {ruin}，{'过' if r['passes'] else '没过'}门槛")
    for k, e in errors.items():
        rows.append(f"{LINES.get(k, k)}：这次没跑出来（{e[:40]}）")
    rows.append("杠杆是在历史里挑的，实际会更差；没过门槛的别放真钱。")
    return "\n".join(rows)


def run_all(settings, fetch: bool = True, runners: dict | None = None, push: bool = True) -> dict:
    runners = runners or RUNNERS
    records, errors = [], {}
    for key, fn in runners.items():
        try:
            records.append(fn(settings, fetch))
        except Exception as e:  # noqa: BLE001 - 一条线挂了不能拖累其它线
            log.exception("lab %s failed", key)
            errors[key] = f"{type(e).__name__}: {e}"[:300]
    ranked = rank(records)
    now = datetime.now(UTC)
    out = {"generated_at": now.isoformat(), "fetch": bool(fetch), "ranking": ranked, "errors": errors}
    d = Path(settings.data) / "labs"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(out, ensure_ascii=False, indent=1)
    (d / "latest.json").write_text(txt)
    (d / f"{now:%Y-%m-%d}.json").write_text(txt)
    if push:
        from . import notify
        try:
            notify.push(settings, "赚钱排行（每周自动跑）", message(ranked, errors))
        except Exception as e:  # noqa: BLE001
            log.warning("labs push failed: %s", e)
    return out


def load_latest(settings) -> dict | None:
    try:
        return json.loads((Path(settings.data) / "labs" / "latest.json").read_text())
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description="自动跑三条策略线并排名")
    ap.add_argument("--offline", action="store_true", help="不连网络，只用本地缓存")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    out = run_all(config.load(), fetch=not a.offline)
    print(message(out["ranking"], out["errors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
