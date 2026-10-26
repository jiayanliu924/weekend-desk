"""重开日报 PDF：每天一份大白话中文 PDF（加州日期）。也提供网页「每日明细」共用的行构造 build_rows。
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
    st = rx.load_state(settings)
    try:
        rows_all = build_rows(settings)
    except Exception:  # noqa: BLE001
        rows_all = []
    rows = sorted([r for r in rows_all if r[0].date() == day], key=lambda r: (r[0], r[2]))
    fills = [r for r in rows if r[6]]
    tot = sum(r[5] for r in fills)
    done = [p for p in st["pos"].values() if p["closed"] and p["closed"].get("reason") != "no_fill"]
    cum = sum(p["closed"].get("pnl_usd", 0.0) or 0.0 for p in done)
    left = max(0, 20 - len(done))
    try:
        lim = account_stop.limit(settings)
        t = account_stop.components(settings)["total_usd"]
        stop_s = f"全账户止损还剩 ${max(0.0, lim + min(0.0, t)):.2f}（上限 ${lim:.0f}）"
    except Exception:  # noqa: BLE001
        stop_s = "全账户止损余额暂时读不到"
    start = datetime(day.year, day.month, day.day, tzinfo=PT)
    S = [Paragraph(f"重开日报　{day:%Y-%m-%d}", H1),
         Paragraph(f"加州时间 {day:%Y-%m-%d} 周{_ZH_WD[day.weekday()]}　｜　北京时间 {start.astimezone(BJ):%Y-%m-%d %H:%M} 起（约至次日同刻）", SMALL),
         Paragraph("今天一句话", H2),
         Paragraph(f"成交 {len(fills)} 笔，合计{'赚' if tot >= 0 else '亏'} ${abs(tot):.2f}；累计 {len(done)} 笔，累计{'赚' if cum >= 0 else '亏'} ${abs(cum):.2f}；"
                   f"离 20 笔还差 {left} 笔；{stop_s}。", P),
         Paragraph("策略是怎么做的", H2), Paragraph(STRATEGY, P),
         Paragraph("每笔明细", H2)]
    if fills:
        data = [["时间(加州)", "轮次", "品种", "操作", "原因", "盈亏"]]
        for r in fills:
            data.append([f"{r[0]:%H:%M}", _KIND_ZH.get(r[1], r[1]), zh_coin(r[2]),
                         Paragraph(r[3], SMALL), Paragraph(r[4], SMALL), _usd(r[5])])
        S.append(_table(data, [20 * mm, 22 * mm, 22 * mm, 42 * mm, 54 * mm, 18 * mm]))
    else:
        S.append(Paragraph("今天没有成交。", P))
    S.append(Paragraph("没下单的品种及原因", H2))
    skipped = [r for r in rows if not r[6]]
    if skipped:
        for r in skipped:
            S.append(Paragraph(f"· {r[0]:%H:%M} {_KIND_ZH.get(r[1], r[1])} {zh_coin(r[2])}：{r[4]}", P))
    else:
        S.append(Paragraph("没有。", P))
    S.append(Paragraph("战略反思与改进方向", H2))
    S += _section_reflect(settings, day)
    S.append(Paragraph("明天的时间表（加州时间）", H2))
    try:
        nxt = datetime(day.year, day.month, day.day, tzinfo=PT) + timedelta(days=1)
        end = nxt + timedelta(days=1)
        evs = [e for e in rx.events_on(nxt.date(), cf) if True]
        evs = [e for e in rx.next_events(nxt.astimezone(UTC), cf, days=2)
               if nxt.timestamp() <= e.nominal_ns / 1e9 < end.timestamp()]
        if evs:
            for e in evs:
                t = datetime.fromtimestamp(e.nominal_ns / 1e9, UTC)
                S.append(Paragraph(f"· 加州 {t.astimezone(PT):%H:%M}（北京 {_bj(t)}）：{'、'.join(zh_coin(n) for n in e.names)}", P))
        else:
            S.append(Paragraph("明天没有排上的重开事件。", P))
    except Exception:  # noqa: BLE001
        S.append(Paragraph("明天的时间表暂时读不到。", P))
    S += [Spacer(1, 10 * mm), Paragraph("Kea Liu", P)]
    out = Path(settings.data) / "reports" / "daily" / f"重开日报_{day:%Y-%m-%d}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(out), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                      title=f"重开日报 {day}").build(S)
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
