"""Weekend Desk 日报 PDF（周末策略 + 重开策略 + 跨平台配对）：每天一份大白话中文 PDF（加州日期）。也提供网页「每日明细」共用的行构造 build_rows。
用法：python -m desk.daily_report [--day YYYY-MM-DD]"""
from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from .pdfreport import F, H1, H2, P, SMALL, _table

PT = ZoneInfo("America/Los_Angeles")
BJ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
_ZH_WD = "一二三四五六日"

_ZH_COIN = {"BRENTOIL": "原油", "SP500": "标普500", "XYZ100": "纳指100", "SMSN": "三星", "SKHY": "SK海力士", "HYUNDAI": "现代汽车", "KR200": "韩国200指数", "SOFTBANK": "软银", "JP225": "日经225", "CAMBRICON": "寒武纪", "GIGADEV": "兆易创新", "GOLD": "黄金", "SILVER": "白银", "NATGAS": "天然气", "CL": "WTI原油", "HO": "取暖油", "PALLADIUM": "钯金", "PLATINUM": "铂金", "COPPER": "铜", "NATURALGAS": "天然气", "NG": "天然气", "WTI": "WTI原油", "BRENT": "布伦特原油", "USOIL": "WTI原油", "XAU": "黄金", "XAG": "白银", "PAXG": "黄金", "ALUMINIUM": "铝", "ALUMINUM": "铝", "URANIUM": "铀", "TSLA": "特斯拉", "NVDA": "英伟达", "HOOD": "Robinhood", "CRCL": "Circle", "MSTR": "MicroStrategy", "COIN": "Coinbase"}

_KIND_ZH = {"kr_open": "韩国开盘", "kr_pm": "韩国午后", "jp_open": "日本开盘", "jp_pm": "日本午休后", "cn_open": "A股开盘", "cn_pm": "A股午休后",
            "hk_open": "港股开盘", "hk_pm": "港股午休后", "daily_18": "商品指数15:00", "weekly": "周日美股", "idx_cash_open": "美股开盘"}
_EXIT_ZH = {"time_stop": "到时间强制平仓", "oracle_exit": "价格回到参考价平仓", "exit_timeout": "平仓挂单超时，强制平仓",
            "oracle_crossed_book": "参考价越过盘口，提前平仓", "exit": "正常平仓"}


def _reopen_row_reason(decs: list, pos: dict | None, k_default: float) -> tuple[str, str]:
    """→ (操作, 原因)，全部中文。decs = 该 (event, name) 的 decision 记录（按时间）。"""
    by = {}
    for r in decs:
        by.setdefault(r.get("what"), []).append(r)
    ent = (by.get("entry") or [None])[-1]
    ovr = ek = None
    if ent:
        ovr, ek = ent.get("overshoot_bps"), ent.get("eff_k_bps")
    if pos is not None:
        c = pos.get("closed") or {}
        if c.get("reason") == "no_fill" or not pos.get("first_fill_ms"):
            why = f"挂单后没成交（过冲 {ovr:.1f} bps 超过门槛 {ek:.1f} bps）" if ovr is not None and ek is not None else "挂单后没成交"
            return "挂单没成交，自动撤单", why
        d = pos["open"].get("dir", 0)
        ent_f = [f for f in pos["fills"] if (f["signed_sz"] > 0) == (d > 0)]
        ex_f = [f for f in pos["fills"] if (f["signed_sz"] > 0) != (d > 0)]

        def avg(fs):
            q = sum(abs(f["signed_sz"]) for f in fs)
            return (sum(abs(f["signed_sz"]) * (f.get("px") or 0) for f in fs) / q, q) if q else (None, 0)
        pe, qe = avg(ent_f)
        px_ex, _ = avg(ex_f)
        side, back = ("卖出", "买回") if d < 0 else ("买入", "卖出")
        op = f"{pe:g} {side} ${pe * qe:,.0f}"
        op += f" → {px_ex:g} {back}" if px_ex is not None else " → 还没平仓，在看着"
        why = f"过冲 {ovr:.1f} bps 超过门槛 {ek:.1f} bps；" if ovr is not None and ek is not None else "过冲超过门槛；"
        why += _EXIT_ZH.get(pos.get("exit_reason") or c.get("reason") or "", "还没平仓" if not c else "已平仓")
        return op, why
    if "skipped_selfcheck" in by:
        return "不下单", f"开盘前自检没通过：{by['skipped_selfcheck'][-1].get('why') or '原因未记录'}"
    if "no_t0" in by:
        return "不下单", "价格没跳（没有重开）"
    retry = (by.get("blocked_retry") or [None])[-1]
    blk = (by.get("blocked") or [None])[-1]
    b = blk or retry
    if b and (blk or "window_closed" in by or True):
        w = str(b.get("why") or "")
        m = re.search(r"价差 (\d+) bps", w)
        if m:
            return "不下单", f"盘口价差 {m.group(1)} bps 太宽（盘口太空，交易不了）"
        if "读不到" in w:
            return "不下单", "读不到盘口"
        if "没更新" in w:
            return "不下单", "行情数据太久没更新，不敢下单"
        return "不下单", w or "安全检查没通过"
    if "window_closed" in by:
        mo = by["window_closed"][-1].get("max_overshoot_bps")
        if "settled" not in by and "settling" in by:
            return "不下单", "参考价还在追（未稳定），一直没法判断"
        tail = [r.get("eff_k_bps") for r in decs if r.get("eff_k_bps") is not None]
        k = tail[-1] if tail else k_default
        if mo is None:
            return "不下单", "价格没被推过头"
        return "不下单", f"过冲 {mo:.1f} bps，没到门槛 {k:.1f} bps"
    if "would_enter" in by:
        return "不下单", "开关关着，只是演习，没下真单"
    return "不下单", "没有触发条件"


def zh_coin(c: str) -> str:
    return _ZH_COIN.get(str(c).split(":")[-1], str(c).split(":")[-1])


def _usd(v: float, sign: bool = True) -> str:
    return f"{'+' if sign and v >= 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


def build_rows(settings) -> list[tuple]:
    """全部账本 → [(加州名义时刻, 轮次, 品种, 操作, 原因, 盈亏, 是否成交)]。只读账本，不联网。"""
    from . import reopen_exec as rx
    cf = rx.cfg(settings)
    recs = rx._led(settings).all()
    st = rx.load_state(settings)
    for pid, p in st["pos"].items():
        p["fills"] = [r for r in recs if r.get("kind") == "fill" and r.get("pos_id") == pid]
    decs: dict = {}
    for r in recs:
        if r.get("kind") == "decision" and r.get("event") and r.get("name"):
            decs.setdefault((r["event"], r["name"]), []).append(r)
    posm: dict = {}
    for p in st["pos"].values():
        ek, nm = p["open"].get("event"), str(p["open"].get("coin", "")).split(":")[-1]
        if ek:
            posm[(ek, nm)] = p
    rows = []
    for key in set(decs) | set(posm):
        try:
            _, kind, ds, hhmm = key[0].split(":")
            nom = datetime.strptime(f"{ds} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(PT)
        except Exception:  # noqa: BLE001
            continue
        op, why = _reopen_row_reason(decs.get(key, []), posm.get(key), float(cf.get("k_bps", 0) or 0))
        p = posm.get(key)
        pnl = float((p["closed"] or {}).get("pnl_usd", 0.0) or 0.0) if p and p.get("first_fill_ms") else 0.0
        rows.append((nom, kind, key[1], op, why, pnl, bool(p and p.get("first_fill_ms"))))
    return rows


# ------------------------------------------------------------------ 三条真钱线：统一成交记录
STRATS = (("weekend", "周末策略"), ("reopen", "重开策略"), ("pairs", "跨平台配对"))
STRAT_ZH = dict(STRATS)
_PAIR_EXIT_ZH = {"entry_failed:unwound": "开仓失败，已回补", "entry_failed:first_leg": "第一腿没成交", "no_position": "交易所已无持仓",
                 "imbalance": "两腿失衡，平仓", "entry_exception": "开仓流程异常，平仓", "wrong_way": "价差反向扩大，止损平仓",
                 "time_stop": "到时间强制平仓", "converge": "价差收回，平仓", "exit": "价差收回，平仓"}


def _ms_of(r: dict) -> float:
    return float(r.get("t_ms") or 0) or float(r.get("written_at") or 0) / 1e6


def strategy_trades(settings) -> list[dict]:
    """三条线的真实资金平仓 → [{strat, ms, coin, pnl}]，按时间排序。读失败的那条线降级为空。"""
    from .ledger import Ledger
    out: list[dict] = []
    try:
        for r in Ledger(settings.data, "reopen").all():
            if r.get("kind") == "closed" and r.get("reason") != "no_fill":
                out.append({"strat": "reopen", "ms": float(r.get("t_ms") or 0), "coin": zh_coin(r.get("coin", "")), "pnl": float(r.get("pnl_usd") or 0)})
    except Exception:  # noqa: BLE001
        pass
    try:
        for r in Ledger(settings.data, "live").all():
            p = float(r.get("pnl_usd") or 0)
            if r.get("kind") == "exit" and p != 0:
                out.append({"strat": "weekend", "ms": _ms_of(r), "coin": zh_coin(r.get("name") or r.get("coin") or ""), "pnl": p})
    except Exception:  # noqa: BLE001
        pass
    try:
        recs = Ledger(settings.data, "pairs").all()
        opens = {r.get("pair_id"): r for r in recs if r.get("kind") == "open"}
        for r in recs:
            if r.get("kind") == "closed" and r.get("reason") != "entry_failed:first_leg":
                o = opens.get(r.get("pair_id")) or {}
                out.append({"strat": "pairs", "ms": float(r.get("t_ms") or 0) or _ms_of(r),
                            "coin": zh_coin(o.get("rich_short") or o.get("pair_key") or "配对"), "pnl": float(r.get("pnl_usd") or 0)})
    except Exception:  # noqa: BLE001
        pass
    return sorted(out, key=lambda x: x["ms"])


def strat_summary(trades: list[dict]) -> dict:
    """{strat: {n, wins, win, total}} + 'all'。"""
    res = {}
    for key in [k for k, _ in STRATS] + ["all"]:
        ts = [t for t in trades if key == "all" or t["strat"] == key]
        w = sum(1 for t in ts if t["pnl"] > 0)
        res[key] = {"n": len(ts), "wins": w, "win": (w / len(ts)) if ts else None, "total": sum(t["pnl"] for t in ts)}
    return res


def _pt_day(ms: float) -> date:
    return datetime.fromtimestamp(ms / 1000, PT).date()


def weekend_rows(settings, day: date) -> list[tuple]:
    """周末策略当天明细 → [(时间, 品种, 操作, 原因, 盈亏)]，含没成交/被取消/没下单的。"""
    from .ledger import Ledger
    recs = Ledger(settings.data, "live").all()
    ents = [r for r in recs if r.get("kind") == "entry"]
    rows = []
    for e in ents:
        w = e.get("weekend")
        ex = next((r for r in recs if r.get("kind") == "exit" and r.get("weekend") == w), None)
        ca = next((r for r in recs if r.get("kind") == "cancel" and r.get("weekend") == w), None)
        ms = _ms_of(ex) if ex else _ms_of(e)
        if _pt_day(ms) != day:
            continue
        nm = zh_coin(e.get("name") or e.get("coin") or "")
        why = _zh_text(e.get("reason") or "")
        pnl = 0.0
        if e.get("skipped"):
            op, why = "不下单", _zh_text(e["skipped"])
        elif e.get("error"):
            op, why = "挂单失败", _zh_text(e["error"])
        elif not e.get("sent"):
            op, why = "演习，没下真单", why or "演习模式"
        else:
            op = f"{e.get('side', '')} ${float(e.get('notional_usd') or 0):,.0f} 限价 {e.get('px')}"
            if ex and ex.get("error"):
                op += " → 平仓出错"
            elif ex and not ex.get("n_fills"):
                op, why = "挂单没成交，自动撤单", why or "周一开盘前没成交"
            elif ex:
                pnl = float(ex.get("pnl_usd") or 0)
                op += " → 周一开盘平仓"
            elif ca:
                op += " → 已撤单"
            else:
                op += " → 还没平仓，在看着"
        rows.append((datetime.fromtimestamp(ms / 1000, PT), nm, op, why, pnl))
    return sorted(rows, key=lambda r: r[0])


def pairs_rows(settings, day: date) -> list[tuple]:
    """跨平台配对当天开平仓 → [(时间, 品种, 操作, 原因, 盈亏)]。"""
    from .ledger import Ledger
    recs = Ledger(settings.data, "pairs").all()
    opens = {r.get("pair_id"): r for r in recs if r.get("kind") == "open"}
    rows = []
    for r in recs:
        k = r.get("kind")
        if k == "open" and _pt_day(float(r.get("t_ms") or 0)) == day:
            sb = r.get("s_entry", r.get("s0"))
            rows.append((datetime.fromtimestamp(float(r["t_ms"]) / 1000, PT), zh_coin(r.get("rich_short") or ""),
                         f"开仓：卖 {zh_coin(r.get('rich_short') or '')}（贵的一边）、买 {zh_coin(r.get('cheap_long') or '')}（便宜的一边）",
                         f"两家价差 {abs(float(sb)):.0f} bps，超过门槛 {float(r.get('thr') or 0):.0f} bps" if sb is not None else "价差超过门槛", 0.0))
        elif k == "closed":
            ms = float(r.get("t_ms") or 0) or _ms_of(r)
            if _pt_day(ms) != day:
                continue
            o = opens.get(r.get("pair_id")) or {}
            sb = o.get("s_entry", o.get("s0"))
            why = _PAIR_EXIT_ZH.get(str(r.get("reason")), _zh_text(r.get("reason") or "平仓"))
            if sb is not None:
                why += f"（入场价差 {abs(float(sb)):.0f} bps）"
            rows.append((datetime.fromtimestamp(ms / 1000, PT), zh_coin(o.get("rich_short") or ""), "平仓", why, float(r.get("pnl_usd") or 0)))
    return sorted(rows, key=lambda r: r[0])


STRATEGY = ("每天固定时间，有些市场（美股、日韩、港股、A股、商品）会重新开盘，Hyperliquid 上对应的合约价格常常被瞬间推得过头。"
            "我们提前盯住参考价，只有价格被推过头、超过门槛（要盖得住手续费）时，才在盘口上挂一笔小单，押它会回到参考价。"
            "价格回来就平仓赚差价；一直不回来，到时间也强制平仓，不死扛。"
            "每笔固定小金额，每天和全账户都有止损线，亏到线就整体停手。"
            "文中的 bps 是基点：1 bps = 0.01%，比如 9 bps 就是 0.09%。过冲是指价格被推过头、比参考价多偏出去的部分。"
            "盘口太空、数据没更新、价格没跳这些情况一律不下单，宁可错过。")


def _zh_text(t) -> str:
    """反思文字里的英文残留换成中文：xyz:SMSN → 三星，overshoot → 过冲。"""
    t = re.sub(r"xyz:([A-Z0-9]+)", lambda m: zh_coin(m.group(1)), str(t or ""))
    t = re.sub(r"(?i)overshoot", "过冲", t)
    return re.sub(r"\b([A-Z]{2,}[0-9]*)\b", lambda m: _ZH_COIN.get(m.group(1), m.group(1)), t)


STRATEGY_WEEKEND = ("周末美股休市，但链上合约照常交易。如果合约价格偏离周五收盘价太多，我们就反方向挂一笔限价小单，赌周一美股开盘后价格回归。"
                    "周一开盘前没成交就撤单，成交了就在开盘后平仓，不死扛。")
STRATEGY_PAIRS = ("同一只股票在两家链上交易所的价格有时差得很大。价差超过门槛（要盖得住手续费）时，在贵的一边卖、便宜的一边买，两边金额基本相等，等价差收回来平仓赚差价。"
                  "价差反向扩大或拖太久，一律止损平仓。")


def _section_reflect(settings, day: date) -> list:
    out = []
    try:
        from . import reopen_reflect as rr  # 另一条线负责；没有就写占位
        facts = rr.build_facts(settings, day)
        r = rr.reflect(settings, facts)
        out.append(Paragraph(f"<b>一句话结论：</b>{_zh_text(r.get('一句话结论', ''))}", P))
        out.append(Paragraph(f"<b>今天为什么赚/亏：</b>{_zh_text(r.get('今天为什么赚/亏', ''))}", P))
        out.append(Paragraph(f"<b>机制还成不成立：</b>{_zh_text(r.get('机制还成不成立', ''))}", P))
        out.append(Paragraph("<b>改进方向：</b>", P))
        for x in r.get("改进方向") or []:
            out.append(Paragraph(f"· {_zh_text(x)}", P))
        out.append(Paragraph(f"<b>明天看什么：</b>{_zh_text(r.get('明天看什么', ''))}", P))
    except Exception:  # noqa: BLE001
        out.append(Paragraph("反思还没有生成（反思模块未就绪或今天没有数据），明天补上。", P))
    return out


def _bj(dt: datetime) -> str:
    b = dt.astimezone(BJ)
    return f"周{_ZH_WD[b.weekday()]} {b:%H:%M}"


def make_pdf(settings, day: date) -> Path:
    from . import account_stop, reopen_exec as rx
    cf = rx.cfg(settings)
    try:
        rows_all = build_rows(settings)
    except Exception:  # noqa: BLE001
        rows_all = []
    rows = sorted([r for r in rows_all if r[0].date() == day], key=lambda r: (r[0], r[2]))
    fills = [r for r in rows if r[6]]
    try:
        trades = strategy_trades(settings)
    except Exception:  # noqa: BLE001
        trades = []
    today_t = [t for t in trades if _pt_day(t["ms"]) == day]
    sd, sc = strat_summary(today_t), strat_summary(trades)
    try:
        lim = account_stop.limit(settings)
        t = account_stop.components(settings)["total_usd"]
        stop_s = f"重开策略的全账户止损还剩 ${max(0.0, lim + min(0.0, t)):.2f}（上限 ${lim:.0f}）"
    except Exception:  # noqa: BLE001
        stop_s = "全账户止损余额暂时读不到"
    start = datetime(day.year, day.month, day.day, tzinfo=PT)
    S = [Paragraph(f"Weekend Desk 日报　{day:%Y-%m-%d}", H1),
         Paragraph(f"加州时间 {day:%Y-%m-%d} 周{_ZH_WD[day.weekday()]}　｜　北京时间 {start.astimezone(BJ):%Y-%m-%d %H:%M} 起（约至次日同刻）　｜　全部为真实资金", SMALL),
         Paragraph("今日总览", H2)]
    data = [["策略", "今天笔数", "今天盈亏", "累计笔数", "累计胜率", "累计盈亏"]]
    for k, nm in STRATS + (("all", "合计"),):
        a, b = sd[k], sc[k]
        data.append([nm, f"{a['n']}", _usd(a["total"]), f"{b['n']}", "-" if b["win"] is None else f"{b['win'] * 100:.0f}%", _usd(b["total"])])
    S.append(_table(data, [34 * mm, 24 * mm, 28 * mm, 24 * mm, 26 * mm, 30 * mm]))
    S.append(Paragraph(stop_s + "。", SMALL))

    def detail(title, how, rws, empty):
        S.append(Paragraph(title, H1))
        S.append(Paragraph("怎么做的", H2))
        S.append(Paragraph(how, P))
        S.append(Paragraph("今天每笔明细", H2))
        if rws:
            d = [["时间(加州)", "品种", "操作", "原因", "盈亏"]]
            for r in rws:
                d.append([f"{r[0]:%H:%M}", r[1], Paragraph(r[2], SMALL), Paragraph(r[3], SMALL), _usd(r[4])])
            S.append(_table(d, [20 * mm, 24 * mm, 54 * mm, 62 * mm, 18 * mm]))
        else:
            S.append(Paragraph(empty, P))

    try:
        wr = weekend_rows(settings, day)
    except Exception:  # noqa: BLE001
        wr = []
    detail("周末策略", STRATEGY_WEEKEND, wr, "今天没有周末策略的挂单或成交。")
    detail("重开策略", STRATEGY, [(r[0], f"{_KIND_ZH.get(r[1], r[1])} {zh_coin(r[2])}", r[3], r[4], r[5]) for r in fills], "今天没有成交。")
    S.append(Paragraph("没下单的品种及原因", H2))
    skipped = [r for r in rows if not r[6]]
    if skipped:
        for r in skipped:
            S.append(Paragraph(f"· {r[0]:%H:%M} {_KIND_ZH.get(r[1], r[1])} {zh_coin(r[2])}：{_zh_text(r[4])}", P))
    else:
        S.append(Paragraph("没有。", P))
    try:
        pr = pairs_rows(settings, day)
    except Exception:  # noqa: BLE001
        pr = []
    detail("跨平台配对", STRATEGY_PAIRS, pr, "今天没有开仓或平仓。")
    S.append(Paragraph("战略反思", H1))
    S += _section_reflect(settings, day)
    S.append(Paragraph("明天的时间表（加州时间）", H2))
    try:
        nxt = datetime(day.year, day.month, day.day, tzinfo=PT) + timedelta(days=1)
        end = nxt + timedelta(days=1)
        evs = [e for e in rx.next_events(nxt.astimezone(UTC), cf, days=2)
               if nxt.timestamp() <= e.nominal_ns / 1e9 < end.timestamp()]
        if evs:
            for e in evs:
                t = datetime.fromtimestamp(e.nominal_ns / 1e9, UTC)
                S.append(Paragraph(f"· 加州 {t.astimezone(PT):%H:%M}（北京 {_bj(t)}）：{'、'.join(zh_coin(n) for n in e.names)}", P))
        else:
            S.append(Paragraph("明天没有排上的重开事件；周末策略和跨平台配对按各自规则自动运行。", P))
    except Exception:  # noqa: BLE001
        S.append(Paragraph("明天的时间表暂时读不到。", P))
    S += [Spacer(1, 10 * mm), Paragraph("Kea Liu", P)]
    out = Path(settings.data) / "reports" / "daily" / f"日报_{day:%Y-%m-%d}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(out), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                      title=f"Weekend Desk 日报 {day}").build(S)
    return out


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(prog="desk.daily_report")
    ap.add_argument("--day", help="加州日期 YYYY-MM-DD，默认昨天")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.day) if a.day else datetime.now(PT).date() - timedelta(days=1)
    print(make_pdf(config.load(), day))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
