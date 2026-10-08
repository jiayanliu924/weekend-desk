"""PDF 报告：累计成绩、每周预测与结果、期权研究、真钱模拟。中文用 reportlab 自带的 STSong-Light，不依赖字体文件。"""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import evaluate, options, sim
from .clock import friday_of, name_of
from .ledger import Ledger

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
F = "STSong-Light"
H1 = ParagraphStyle("h1", fontName=F, fontSize=17, leading=22, spaceAfter=6)
H2 = ParagraphStyle("h2", fontName=F, fontSize=12.5, leading=17, spaceBefore=10, spaceAfter=4,
                    textColor=colors.HexColor("#7a2a1e"))
P = ParagraphStyle("p", fontName=F, fontSize=9.5, leading=14)
SMALL = ParagraphStyle("s", fontName=F, fontSize=8, leading=11, textColor=colors.HexColor("#666666"))


def _n(x, nd=1, suf=""):
    return "—" if x is None else f"{x:,.{nd}f}{suf}"


def _table(rows, widths):
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), F, 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#efe6dc")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9bdb0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _day_bounds(day: date):
    pt = ZoneInfo("America/Los_Angeles")
    a = datetime(day.year, day.month, day.day, tzinfo=pt)
    b = a + timedelta(days=1)
    dates = sorted({a.astimezone(timezone.utc).date().isoformat(), (b - timedelta(seconds=1)).astimezone(timezone.utc).date().isoformat()})
    return int(a.timestamp() * 1e9), int(b.timestamp() * 1e9), dates


def _q(settings, stream, dates, sql):
    files = [str(f) for d in dates for f in (Path(settings.data) / "raw" / stream / f"date={d}").glob("*.parquet")]
    if not files:
        return []
    return duckdb.connect().execute(sql.replace("SRC", f"read_parquet({files!r})")).fetchall()


def daily_section(settings, day: date) -> list:
    """昨天：读了什么、各合约价格怎么走、预测与结果。"""
    a, b, ds = _day_bounds(day)
    out = [Paragraph(f"昨天（{day:%Y-%m-%d}，{'一二三四五六日'[day.weekday()]}）发生了什么", H2)]
    news = _q(settings, "news", ds, f"SELECT payload FROM SRC WHERE received_at >= {a} AND received_at < {b}")
    evs = []
    for (p,) in _q(settings, "events", ds + [(day + timedelta(days=1)).isoformat()], "SELECT payload FROM SRC"):
        rec = json.loads(p)
        if a <= rec["news_received_at"] < b:
            evs += [dict(e, _title=rec.get("title")) for e in rec["events"] if e.get("evidence_ok")]
    strong = [e for e in evs if e.get("materiality", 0) >= 2]
    out.append(Paragraph(f"<b>读了什么：</b>{len(news)} 条新闻；大模型整理出 {len(evs)} 个事件，其中重要事件（重要度≥2）{len(strong)} 个。", P))
    for e in strong[:6]:
        out.append(Paragraph(f"· {','.join(e.get('entities') or []) or '—'} | {e.get('event_type', '')} | {e.get('direction_in_text', '')} — "
                             f"{(e.get('_title') or '')[:80]}", SMALL))
    rows = [["合约", "昨日开始", "昨日结束", "涨跌"]]
    for inst in settings.instruments:
        r = _q(settings, "hl_ctx", ds, f"""SELECT arg_min(CAST(json_extract_string(payload,'$.data.ctx.oraclePx') AS DOUBLE), received_at),
               arg_max(CAST(json_extract_string(payload,'$.data.ctx.oraclePx') AS DOUBLE), received_at)
               FROM SRC WHERE key='{inst['coin']}' AND received_at >= {a} AND received_at < {b}""")
        if r and r[0][0]:
            p0, p1 = r[0]
            rows.append([inst["name"], _n(p0, 2), _n(p1, 2), f"{(p1 / p0 - 1) * 100:+.2f}%"])
    if len(rows) > 1:
        out.append(Spacer(1, 4))
        out.append(_table(rows, [30 * mm, 36 * mm, 36 * mm, 26 * mm]))
    acts = []
    for r in Ledger(settings.data).all():
        if a <= r["written_at"] < b and r["kind"] in ("lock", "outcome"):
            nm = r.get("name") or name_of(r["weekend"], "XYZ100")
            if r["kind"] == "lock" and r["features"].get("ok"):
                f, pr, ac = r["features"], r["prediction"], r["action"]
                acts.append(f"锁定 {nm}：偏离 {f['dev_bps']:+.0f} bps，{'信息' if f.get('news_weekend') else '噪音'}周末，预测 {pr['pred_bps']:+.0f}，{ac['reason']}")
            elif r["kind"] == "outcome" and r.get("ok"):
                acts.append(f"结果 {nm}：开盘 {r['y_bps']:+.0f} bps，模型误差 {r['err_model_bps']:.0f}，模拟 ${r.get('pnl_net_usd', 0):+.2f}")
    for r in Ledger(settings.data, "options").all():
        if a <= r["written_at"] < b and r["kind"] == "outcome" and r.get("ok"):
            acts.append(f"期权 {r['weekend']}：实际/隐含 = {r['ratio']:.2f}（{'期权偏贵' if r['ratio'] < 1 else '期权偏便宜'}）")
    out.append(Paragraph("<b>预测与结果：</b>" + ("" if acts else "昨天没有锁定或出结果（周末预测在周日锁定，期权对照样本每天凌晨锁定、次日出结果）。"), P))
    for t in acts[:20]:
        out.append(Paragraph("· " + t, SMALL))
    return out


def _x(t) -> str:
    return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def agents_section(settings) -> list:
    from . import agents
    r = agents.last_full_run(settings, within_h=36)
    if not r:
        return []
    out = [Paragraph("Agent 团队会议（25 个角色）", H2)]
    ch = r.get("chair", {})
    out.append(Paragraph(f"<b>{_x(ch.get('headline'))}</b>", P))
    if ch.get("plain"):
        out.append(Paragraph(_x(ch["plain"]), P))
    for t in (ch.get("today") or [])[:5]:
        out.append(Paragraph("· " + _x(t), SMALL))
    rows = [["讨论室", "结论（大白话）", "把握", "分歧"]]
    cell = ParagraphStyle("c", parent=SMALL, textColor=colors.black)
    for meta in r.get("rooms_meta", []):
        room = r.get("rooms", {}).get(meta["key"])
        if not room:
            continue
        sy = room.get("synth", {})
        rows.append([meta["name"], Paragraph(_x(sy.get("plain")), cell), f"{sy.get('confidence', '—')}%",
                     str(len(sy.get("dissent") or []))])
    out.append(_table(rows, [30 * mm, 112 * mm, 16 * mm, 14 * mm]))
    ck = r.get("checks", {})
    out.append(Paragraph("代码审计：" + "；".join(f"{'[通过]' if v.get('ok') else '[有问题]'} {_x(v.get('text'))}" for v in ck.values())
                         + f"。本次费用 ${r.get('cost_usd', 0):.2f}，{r.get('calls', 0)} 次调用。", SMALL))
    return out


def build(settings, capital: float = 2000.0, leverage: float = 2.0, daily_for: date | None = None) -> bytes:
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime.now(timezone.utc).astimezone(tz)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title="Weekend Desk 报告")
    s = []
    s.append(Paragraph("Weekend Desk " + ("每日报告" if daily_for else "报告"), H1))
    s.append(Paragraph(f"生成时间 {now:%Y-%m-%d %H:%M} 加州时间 · 合约 {', '.join(i['name'] for i in settings.instruments)} · "
                       f"{'模拟模式（未下真单）' if not settings['live']['enabled'] else '实盘模式'}", SMALL))

    s.append(Paragraph("一句话说明", H2))
    s.append(Paragraph("每个周末，程序读 7 个合约（纳指、标普、布伦特原油、英伟达、特斯拉、英特尔、三星）的周末价格和周末新闻，"
                       "在各自真市场重开前 15 分钟锁定预测：链上被推过头的价格开盘后会不会被拉回来。"
                       "开盘后自动对答案、记模拟账。另有期权研究：大事件前期权预期的波动和实际波动谁大。", P))

    if daily_for:
        s += daily_section(settings, daily_for - timedelta(days=1))
    s += agents_section(settings)

    led = Ledger(settings.data)
    card = evaluate.scorecard(led.completed(), settings)
    s.append(Paragraph("累计成绩（实时模拟，全部合约）", H2))
    if card.get("n_official"):
        s.append(_table([
            ["正式周末", "出手次数", "方向命中率", "平均误差 模型/A/B（bps）", "累计模拟盈亏"],
            [card["n_official"], card["n_traded"], _n(card["hit_rate"] and card["hit_rate"] * 100, 0, "%"),
             f"{_n(card['mean_err_model'])} / {_n(card['mean_err_A'])} / {_n(card['mean_err_B'])}",
             f"${_n(card['pnl_net_usd_total'], 2)}"]], [24 * mm, 22 * mm, 26 * mm, 56 * mm, 32 * mm]))
        s.append(Paragraph(card["verdict"], SMALL))
    else:
        s.append(Paragraph("还没有完成的实时周末。第一个周末完成后这里会出现成绩。", P))

    bc = evaluate.scorecard_by_coin(led.completed(), settings)
    if bc:
        brows = [["合约", "正式周末", "出手", "模型误差", "累计模拟$"]]
        for nm, c in bc.items():
            brows.append([nm, c.get("n_official", 0), c.get("n_traded", "—"), _n(c.get("mean_err_model")), _n(c.get("pnl_net_usd_total"), 2)])
        s.append(Spacer(1, 4))
        s.append(_table(brows, [30 * mm, 26 * mm, 20 * mm, 28 * mm, 30 * mm]))

    rows = [["周末", "合约", "链上偏离", "新闻判断", "预测", "实际", "动作", "扣费后$"]]
    for r in led.completed()[-35:]:
        f, p, a, o = r["lock"]["features"], r["lock"]["prediction"], r["lock"]["action"], r["outcome"]
        rows.append([friday_of(r["weekend"]), name_of(r["weekend"], "XYZ100"), _n(f.get("dev_bps")), "信息周末" if f.get("news_weekend") else "噪音周末",
                     _n(p.get("pred_bps")), _n(o.get("y_bps")), {1: "买", -1: "卖", 0: "不出手"}[a.get("side", 0)],
                     _n(o.get("pnl_net_usd"), 2) + (" 作废" if r["lock"].get("void") else "")])
    if len(rows) > 1:
        s.append(Paragraph("每周预测与结果（单位 bps，1 bps = 0.01%）", H2))
        s.append(_table(rows, [22 * mm, 20 * mm, 19 * mm, 21 * mm, 17 * mm, 17 * mm, 17 * mm, 25 * mm]))

    for src, title in (("live", "如果放真钱：实时模拟"), ("history", "如果放真钱：历史回测（上线前 K 线，仅供参考）")):
        r = sim.run(settings, capital, leverage, "maker", src)
        s.append(Paragraph(f"{title}（本金 ${capital:,.0f}，{leverage:g} 倍杠杆，平均分给 {len(settings.instruments)} 个合约，挂单进/吃单出）", H2))
        if not r["n"]:
            s.append(Paragraph("暂无数据。", P))
            continue
        s.append(_table([
            ["周末数", "出手", "累计盈亏", "收益率", "胜率", "最大回撤", "最差一笔"],
            [r["n_weekends"], r["n_traded"], f"${_n(r['total_usd'], 2)}", _n(r["return_pct"], 1, "%"),
             _n(r["win_rate"] and r["win_rate"] * 100, 0, "%"), _n(r["max_drawdown_pct"], 1, "%"), f"${_n(r['worst_usd'], 2)}"]],
            [18 * mm, 16 * mm, 28 * mm, 22 * mm, 18 * mm, 24 * mm, 28 * mm]))
        byr = [["合约", "出手", "胜", "盈亏$", "最差一周$"]] + [
            [nm, b["traded"], b["wins"], _n(b["pnl"], 2), _n(b["worst"], 2)] for nm, b in r["by_name"].items()]
        s.append(Spacer(1, 3))
        s.append(_table(byr, [30 * mm, 18 * mm, 16 * mm, 30 * mm, 30 * mm]))
        s.append(Paragraph(f"压力情形：所有合约同一个周末都押错、开盘反向再走 3%，一次会亏约 ${-r['stress_usd']:,.0f}"
                           f"（本金的 {-r['stress_pct']:.0f}%）。过去没出现过，但不能排除。", SMALL))

    oc = options.scorecard(settings)
    s.append(Paragraph("期权事件波动率研究（只研究，不交易）", H2))
    s.append(Paragraph(options.card_text(oc).replace("\n", "<br/>"), P))
    orows = [["样本", "类型", "隐含波动", "实际波动", "实际/隐含"]]
    for x in options.completed(Ledger(settings.data, "options"))[-12:]:
        lk, o = x["lock"], x["outcome"]
        orows.append([x["sid"], lk["event_type"], options._pct(lk.get("implied_vol")), options._pct(o.get("realized_vol")),
                      f"{o['ratio']:.2f}" if o.get("ok") else "—"])
    if len(orows) > 1:
        s.append(_table(orows, [46 * mm, 20 * mm, 26 * mm, 26 * mm, 24 * mm]))

    s.append(Spacer(1, 8))
    s.append(Paragraph("说明：所有盈亏均为模拟或回测，未扣除资金费率与滑点以外的成本，不构成投资建议。"
                       "按研究备忘，30 个正式周末之前的任何结果都只算'值得继续看'。", SMALL))
    doc.build(s)
    return buf.getvalue()
