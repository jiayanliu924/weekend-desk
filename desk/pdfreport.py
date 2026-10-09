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
H1 = ParagraphStyle("h1", wordWrap="CJK", fontName=F, fontSize=17, leading=22, spaceAfter=6)
H2 = ParagraphStyle("h2", wordWrap="CJK", fontName=F, fontSize=12.5, leading=17, spaceBefore=10, spaceAfter=4,
                    textColor=colors.HexColor("#7a2a1e"))
P = ParagraphStyle("p", wordWrap="CJK", fontName=F, fontSize=9.5, leading=14)
SMALL = ParagraphStyle("s", wordWrap="CJK", fontName=F, fontSize=8, leading=11, textColor=colors.HexColor("#666666"))


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


ROOM_PLAIN = {
    "options": "比特币期权可以理解成给价格涨跌买的「保险」。保险费里藏着市场对未来波动的预期。这个室比的是：保险卖贵了还是卖便宜了。三个成员每天各猜一个数，第二天对答案，猜得准的加分。",
    "js": "Jane Street 是全球最会赚钱的交易公司之一。这个室拿它的做事方法当尺子，检查我们有没有偷看答案、有没有自己骗自己。",
    "niche": "我们盯着 7 个周末还能交易的合约（美股指数、英伟达、特斯拉、英特尔、三星、原油）。这个室决定哪个最值得做、哪个该放弃。",
    "trading": "三个交易员各管一本模拟账，比谁赚得多：反向派押「周末被推偏的价格会弹回来」，顺势派押「偏了是有原因的」，仓位派只挑最有把握的做。周日开盘后自动算盈亏，赢的当冠军。",
    "algo": "三个算法师各自设计交易规则，交给电脑用过去的数据考试：前三分之二的数据给他们看，后三分之一当考题。考得比现在的规则好才算数，试得越多及格线越高。",
    "risk": "风控就是踩刹车的人：专找会亏大钱的情况，可以一票否决交易室的提案。",
    "audit": "审计员检查这场会本身靠不靠谱：有没有用到开会之后才知道的信息、有没有编数字。",
}

GLOSSARY = [
    ("万分点", "百分之一的百分之一。100 万分点 = 1%。例：价格偏了 70 万分点 = 偏了 0.7%。"),
    ("链上价格 / 外部价格", "链上价格是区块链交易所里这个合约的价格；外部价格是真实市场（比如纳斯达克）的价格。周末真市场关门，链上照样能买卖，两边就会差开。"),
    ("偏离", "链上价格比外部价格高或低了多少。"),
    ("回撤 / 弹回来", "周日开盘后，链上价格被拉回真实价格的现象。"),
    ("反向（fade）/ 顺势（follow）/ 不做（skip）", "反向 = 押价格会弹回来；顺势 = 押它继续往那边走；不做 = 这次不碰。"),
    ("仓位", "这次放多少钱，0 到 1：1 = 用满这个合约分到的钱，0.35 = 用三成五。"),
    ("隐含波动 / 实际波动", "隐含波动 = 期权价格里写着的「市场预计会晃多大」；实际波动 = 事后真的晃了多大。实际÷隐含小于 1，说明期权卖贵了。"),
    ("回测 / 考试段", "回测 = 拿历史数据假装当时交易，看能赚多少；考试段 = 专门留出来、设计规则时不许看的那部分历史。"),
    ("显著 / 门槛", "确认不是运气的标准。试的方案越多，越容易碰巧撞上好结果，所以门槛会自动抬高。"),
    ("冠军 / 积分", "每个岗位三个 agent 比赛，电脑按结果打分，30 天积分第一的是冠军：它的决定会进正式模拟账。"),
    ("模拟账 / 实盘", "模拟账是纸上记账，不花真钱；实盘是真下单。实盘现在锁着。"),
]


def _plain(t) -> str:
    """去掉资料编号（F12、F14–F20 之类），让正文好读。"""
    import re as _re
    t = str(t or "")
    t = _re.sub(r"[（(][^（）()]*F\d+[^（）()]*[）)]", "", t)
    t = _re.sub(r"\bF\d+(?:\s*[–\-至到~]\s*F?\d+)?", "", t)
    t = _re.sub(r"[，、]\s*(?=[。；）])", "", t)
    return t.strip()


def _box(text, style=None):
    t = Table([[Paragraph(text, style or P)]], colWidths=[178 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1e9df")),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


def qa_pdf(settings, rec: dict) -> bytes:
    """网站体检报告 PDF。"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title="Weekend Desk 体检报告")
    cell = ParagraphStyle("c", parent=SMALL, textColor=colors.black)
    n, nf = rec["n"], rec["n_fail"]
    s = [Paragraph("Weekend Desk 体检报告", H1),
         Paragraph(f"{_x(rec['run_id'])} · 共 {n} 项，通过 {n - nf}，未通过 {nf} · 只测本站自己，不碰真钱", SMALL)]
    rv = rec.get("review", {})
    if rv:
        s.append(Paragraph("AI 测试员结论", H2))
        for a in rec.get("agents_meta", []):
            r_ = rv.get(a["id"], {})
            s.append(Paragraph(f"<b>{_x(a['name'])}</b>：{_x(r_.get('verdict'))}", P))
            for f in r_.get("findings") or []:
                s.append(Paragraph(f"　· [{_x(f.get('severity'))}] {_x(f.get('what'))} → {_x(f.get('fix'))}", SMALL))
    areas = {}
    for c in rec["checks"]:
        areas.setdefault(c["area"], []).append(c)
    for area, cs in areas.items():
        s.append(Paragraph(f"{_x(area)}（{sum(1 for c in cs if c['ok'])}/{len(cs)}）", H2))
        rows = [["", "检查项", "说明"]]
        for c in cs:
            rows.append(["通过" if c["ok"] else "未过", Paragraph(_x(c["name"]), cell),
                         Paragraph(_x(c["detail"]) + ("" if c["ok"] else f"（{_x(c['severity'])}危）"), cell)])
        s.append(_table(rows, [14 * mm, 70 * mm, 94 * mm]))
    doc.build(s)
    return buf.getvalue()


def meeting_pdf(settings, rec: dict) -> bytes:
    """一次 Agent 会议的大白话纪要：前面是看得懂的总结，最后附原始记录。"""
    from reportlab.platypus import PageBreak
    from . import agents, arena
    ps = rec.get("plain_summary") or agents.plain_summary(settings, rec)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Agent 会议纪要 {rec.get('run_id')}")
    BIG = ParagraphStyle("big", parent=P, fontSize=12, leading=19)
    H3 = ParagraphStyle("h3", parent=P, fontSize=11, leading=16, spaceBefore=9, spaceAfter=3, textColor=colors.HexColor("#7a2a1e"))
    names = {a["id"]: a["name"] for a in rec.get("roster", [])}
    rid = rec.get("run_id", "")
    s = [Paragraph("Agent 团队会议纪要（大白话版）", H1),
         Paragraph(f"开会时间 {rid[:10]} {rid[11:13]}:{rid[13:15]}（加州时间）· 全部是模拟研究，没有下任何真钱的单 · 不构成投资建议", SMALL),
         Spacer(1, 6)]
    if rec.get("degraded_reason"):
        s.append(_box("说明：" + _x(rec["degraded_reason"])))
    # —— 第 1 页：一页看懂
    s.append(Paragraph("一页看懂", H2))
    s.append(_box("<br/>".join("· " + _x(x) for x in ps.get("overview", [])) or "（本次没有总结）", BIG))
    if ps.get("weekend"):
        s.append(Paragraph("这个周末会发生什么", H2))
        s.append(Paragraph(_x(ps["weekend"]), P))
    if ps.get("money"):
        s.append(Paragraph("和钱有关的", H2))
        s.append(Paragraph(_x(ps["money"]), P))
    st = rec.get("standings") or {}
    if st.get("table"):
        s.append(Paragraph("谁表现好、谁表现差（赛马积分）", H2))
        s.append(Paragraph("每个岗位 3 个 agent 比赛，电脑按结果自动加减分：编数字、引用错资料当场扣分；猜对、赚钱加分。"
                           "积分最高的是冠军，它的决定算数、还能用更强的模型；连续垫底的会被换掉。", SMALL))
        rows = [["岗位", "agent", "积分", "状态"]]
        for race, label in arena.RACES.items():
            for k, t in sorted([(k, t) for k, t in st["table"].items() if t["room"] == race], key=lambda x: -x[1]["score30"]):
                rows.append([label, t["name"], f"{t['score30']:+.1f}", ("冠军" if t.get("champion") else "") + (" 暂停" if t.get("paused") else "")])
        s.append(_table(rows, [24 * mm, 70 * mm, 30 * mm, 54 * mm]))
    s.append(Paragraph("看不懂的词", H2))
    for term, exp in GLOSSARY + [(t.get("term", ""), t.get("plain", "")) for t in ps.get("terms", []) if isinstance(t, dict)]:
        if term:
            s.append(Paragraph(f"<b>{_x(term)}</b>：{_x(exp)}", P))
    # —— 每个讨论室一页
    for meta in rec.get("rooms_meta", []):
        room = rec.get("rooms", {}).get(meta["key"])
        if not room:
            continue
        sy = room.get("synth", {})
        rp = (ps.get("rooms") or {}).get(meta["key"], {})
        s.append(PageBreak())
        s.append(Paragraph(meta["name"], H1))
        s.append(_box("<b>这个室是干嘛的：</b>" + _x(ROOM_PLAIN.get(meta["key"], meta.get("goal", "")))))
        s.append(Spacer(1, 6))
        s.append(Paragraph("结论（大白话）", H3))
        s.append(Paragraph(_x(rp.get("conclusion") or _plain(sy.get("plain"))), BIG))
        if rp.get("why_care"):
            s.append(Paragraph("<b>这对你意味着什么：</b>" + _x(rp["why_care"]), P))
        conf = sy.get("confidence")
        if conf is not None:
            s.append(Paragraph(f"大家对这个结论有多大把握：{_x(conf)}%（100% = 完全确定）", SMALL))
        dis = [d for d in sy.get("dissent") or [] if isinstance(d, dict)]
        if rp.get("disagree") or dis:
            s.append(Paragraph("谁不同意、为什么", H3))
            if rp.get("disagree"):
                s.append(Paragraph(_x(rp["disagree"]), P))
            else:
                for d in dis[:4]:
                    s.append(Paragraph(f"· {_x(names.get(str(d.get('who')), d.get('who')))}：{_x(_plain(d.get('view')))}", P))
        props = [p for p in sy.get("proposal") or [] if isinstance(p, dict)]
        if props:
            s.append(Paragraph("这个周末每个合约怎么做（纸面练习，不下真钱）", H3))
            lean = {"fade": "押它弹回来", "follow": "押它继续走", "skip": "不做"}
            rows = [["合约", "做法", "放多少", "为什么"]]
            for p in props:
                rows.append([_x(p.get("name")), lean.get(str(p.get("lean")), _x(p.get("lean"))),
                             "不放" if not p.get("size") else f"{float(p.get('size') or 0) * 100:.0f}%",
                             Paragraph(_x(_plain(p.get("why"))), SMALL)])
            s.append(_table(rows, [24 * mm, 30 * mm, 18 * mm, 106 * mm]))
        vetoes = [v for v in sy.get("veto") or [] if isinstance(v, dict)]
        for v in vetoes:
            s.append(Paragraph(f"否决：{_x(v.get('name'))}——{_x(_plain(v.get('why')))}", P))
        acts = sy.get("actions") or []
        if acts:
            s.append(Paragraph("接下来要做的", H3))
            for a_ in acts[:5]:
                s.append(Paragraph("· " + _x(_plain(a_)), P))
    # —— 附录：原始记录
    s.append(PageBreak())
    s.append(Paragraph("附录：原始发言记录（给想深究的人看，可以跳过）", H1))
    s.append(Paragraph("下面是每个 agent 的原话。方括号里的 F 编号指它引用的资料条目，资料原文在最后。", SMALL))
    ck = rec.get("checks", {})
    for v in ck.values():
        s.append(Paragraph(f"代码核对：{'[通过]' if v.get('ok') else '[有问题]'} {_x(v.get('text'))}", SMALL))
    cell = ParagraphStyle("c", parent=SMALL, textColor=colors.black)
    for meta in rec.get("rooms_meta", []):
        room = rec.get("rooms", {}).get(meta["key"])
        if not room:
            continue
        s.append(Paragraph(meta["name"], H2))
        for i, rr in enumerate(room.get("rounds", [])):
            s.append(Paragraph(f"第 {i + 1} 轮", SMALL))
            for o in rr:
                out = o.get("out", {})
                s.append(Paragraph(f"<b>{_x(names.get(o['agent'], o['agent']))}</b>：{_x(out.get('plain'))}", cell))
                for pt in out.get("points") or []:
                    if isinstance(pt, dict):
                        s.append(Paragraph(f"　· {_x(pt.get('claim'))} [{_x(','.join(map(str, pt.get('cite') or [])))}]", SMALL))
                if out.get("rebut"):
                    s.append(Paragraph("　回应：" + _x(out["rebut"]), SMALL))
                for b_ in out.get("backtest") or []:
                    if isinstance(b_, dict):
                        s.append(Paragraph("　电脑回测：" + _x(b_.get("text")), SMALL))
                if out.get("forecast_ratio") is not None:
                    s.append(Paragraph(f"　期权预测：实际÷隐含 = {_x(out['forecast_ratio'])}", SMALL))
    s.append(Paragraph(f"资料原文（{len(rec.get('facts', []))} 条，全部由程序生成）", H2))
    for f in rec.get("facts", []):
        s.append(Paragraph(f"{f['id']} [{_x(f['topic'])}] {'（外部新闻标题）' if f.get('untrusted') else ''}{_x(f['text'])}", SMALL))
    doc.build(s)
    return buf.getvalue()


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
