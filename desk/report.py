"""推送文字与周报（备忘 7.6 实验日志模板 + 7.5 评估口径）。"""
from __future__ import annotations

import html
from pathlib import Path

from . import evaluate, options
from .clock import Weekend, friday_of, name_of
from .ledger import Ledger


def _f(x, nd=1, suf=""):
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "是" if x else "否"
    if isinstance(x, (int, float)):
        return f"{x:,.{nd}f}{suf}"
    return str(x)


def _nm(rec: dict) -> str:
    return rec.get("name") or name_of(rec.get("weekend", ""), "XYZ100")


def locks_summary(recs: list[dict]) -> str:
    lines = []
    for r in recs:
        f, p, a = r["features"], r["prediction"], r["action"]
        if not f.get("ok"):
            lines.append(f"{_nm(r)}：数据不全，不出手")
            continue
        act = a["reason"].split("，")[0] if not a["side"] else f"{'买' if a['side'] > 0 else '卖'} ${a['notional_usd']:,.0f}"
        lines.append(f"{_nm(r)}：偏离 {f['dev_bps']:+.0f} bps，{'信息' if f['news_weekend'] else '噪音'}周末，"
                     f"预测 {p['pred_bps']:+.0f} → {act}" + ("（作废）" if r.get("void") else ""))
    return "\n".join(lines)


def outcomes_summary(settings, items) -> str:
    lines, tot = [], 0.0
    for w, r in items:
        nm = w.name or "XYZ100"
        if not r.get("ok"):
            lines.append(f"{nm}：{'；'.join(r.get('problems', []))}")
            continue
        pnl = r.get("pnl_net_usd", 0.0)
        tot += pnl
        lines.append(f"{nm}：开盘 {r['y_bps']:+.0f} bps，误差 模型 {r['err_model_bps']:.0f}/周五 {r['err_A_bps']:.0f}/链上 {r['err_B_bps']:.0f}，"
                     f"模拟 ${pnl:+.2f}")
    lines.append(f"本批合计 ${tot:+.2f}（每个合约 ${settings['risk']['paper_notional_usd']:,.0f} 模拟单）")
    return "\n".join(lines)


def lock_text(rec: dict) -> str:
    f, p, a = rec["features"], rec["prediction"], rec["action"]
    if rec.get("void"):
        head = "⚠️ 本周作废：" + "；".join(rec["void_reasons"])
    else:
        head = "模拟模式" if rec["mode"] == "paper" else "实盘模式"
    lines = [f"{_nm(rec)} · {head}"]
    if f.get("ok"):
        lines += [
            f"周五收盘 {_f(f['fri_close'], 1)}，链上中间价 {_f(f['dec_mid'], 1)}，偏离 {_f(f['dev_bps'])} bps",
            f"实质事件（materiality≥2）{f['n_mat2']} 个 → {'信息周末' if f['news_weekend'] else '噪音周末'}",
            f"预测重开相对周五 {_f(p['pred_bps'])} bps（{p['model_version']}）",
            f"动作：{a['reason']}" + (f"，{'买' if a['side'] > 0 else '卖'} ${_f(a['notional_usd'], 0)} @ {_f(a.get('entry_px'), 1)}" if a["side"] else ""),
        ]
        if f.get("strong_titles"):
            lines.append("事件：" + " | ".join(f["strong_titles"][:3]))
    lines.append(f"锁定哈希 {rec['record_sha256'][:16]}")
    return "\n".join(lines)


def outcome_text(lock: dict, rec: dict) -> str:
    if not rec.get("ok"):
        return "；".join(rec.get("problems", ["无结果"]))
    lines = [
        f"重开价 {_f(rec['reopen_px'], 1)}，相对周五 {_f(rec['y_bps'])} bps",
        f"误差：模型 {_f(rec['err_model_bps'])} / 基线A {_f(rec['err_A_bps'])} / 基线B {_f(rec['err_B_bps'])} bps",
    ]
    if lock["action"]["side"]:
        lines.append(f"模拟盈亏（挂单进/吃单出，扣费后）{_f(rec['pnl_net_bps'])} bps = ${_f(rec['pnl_net_usd'], 2)}；"
                     f"全吃单 ${_f(rec['pnl_net_taker_usd'], 2)}")
    return "\n".join(lines)


def card_text(c: dict) -> str:
    if not c.get("n_official"):
        return "还没有正式周末成绩。"
    return "\n".join([
        f"正式周末 {c['n_official']} 个（出手 {c['n_traded']} 次，方向命中率 {_f(c['hit_rate'] and c['hit_rate'] * 100, 0, '%')}）",
        f"平均重开误差：模型 {_f(c['mean_err_model'])} / A {_f(c['mean_err_A'])} / B {_f(c['mean_err_B'])} bps",
        f"累计模拟盈亏（扣费）${_f(c['pnl_net_usd_total'], 2)}；最近 8 周 ${_f(c['rolling_pnl_usd'], 2)}"
        + ("  ⚠️ 触发止损：暂停实盘，回到纯记录" if c["rolling_stop"] else ""),
        c["verdict"],
    ])


ROWS = [
    ("实验编号 / 周末", lambda o, l, r: f"{o.get('exp_id', '—')} / {l['weekend'] if l else ''}"),
    ("假设与改动", lambda o, l, r: o.get("hypothesis_change", "—")),
    ("决策时刻快照", lambda o, l, r: (f"链上偏离 {_f(l['features']['dev_bps'])} bps，实质事件 {l['features']['n_mat2']} 个，"
                                     f"BTC 周末 {_f(l['features'].get('btc_wknd_ret_bps'))} bps，周末成交 ${_f(l['features'].get('wknd_volume_usd'), 0)}")
     if l and l["features"].get("ok") else "—"),
    ("预测与动作", lambda o, l, r: (f"预测 {_f(l['prediction']['pred_bps'])} bps；{l['action']['reason']}" if l and l["features"].get("ok") else "—")),
    ("实际结果", lambda o, l, r: (f"重开 {_f(r['y_bps'])} bps，偏离回落 {_f(r['dev_reverted_bps'])} bps，扣费后 ${_f(r.get('pnl_net_usd'), 2)}"
                               if r and r.get("ok") else "—")),
    ("复盘", lambda o, l, r: None),
]


def weekly(settings, w: Weekend) -> Path:
    led = Ledger(settings.data)
    recs = led.completed()
    fri = w.friday.isoformat()
    card = evaluate.scorecard(recs, settings)
    md = [f"# Weekend Desk 周报 · 周末 {fri}", "",
          f"合约 {', '.join(i['name'] for i in settings.instruments)} · 模式 {'模拟' if not settings['live']['enabled'] else '实盘'}", "",
          "## 本周各合约（备忘 7.6 实验日志）", "",
          "| 合约 | 链上偏离 | 实质新闻 | 预测 | 实际 | 误差 模型/周五/链上 | 动作 | 扣费后$ | 备注 |", "|---|---|---|---|---|---|---|---|---|"]
    for x in recs:
        if friday_of(x["weekend"]) != fri:
            continue
        f, p, a, oc = x["lock"]["features"], x["lock"]["prediction"], x["lock"]["action"], x["outcome"]
        md.append("| {} | {} | {} | {} | {} | {} / {} / {} | {} | {} | {} |".format(
            name_of(x["weekend"], "XYZ100"), _f(f.get("dev_bps")), f.get("n_mat2", "—"), _f(p.get("pred_bps")), _f(oc.get("y_bps")),
            _f(oc.get("err_model_bps")), _f(oc.get("err_A_bps")), _f(oc.get("err_B_bps")),
            {1: "买", -1: "卖", 0: "—"}[a.get("side", 0)], _f(oc.get("pnl_net_usd"), 2),
            "作废：" + "；".join(x["lock"].get("void_reasons", [])) if x["lock"].get("void") else ""))
    notes = [r.get("note", "") for r in led.all() if r["kind"] == "review" and friday_of(r["weekend"]) == fri]
    md += ["", "复盘：" + ("；".join(notes) if notes else "（周一人工复核后用 `desk review` 填写）"), "",
           "## 累计成绩（7.5 评估口径）", "", card_text(card).replace("\n", "  \n"), "", "### 分合约", ""]
    for nm, c in evaluate.scorecard_by_coin(recs, settings).items():
        md.append(f"- {nm}：{c.get('n_official', 0)} 个正式周末，累计模拟 ${_f(c.get('pnl_net_usd_total'), 2)}")
    if settings["options"]["enabled"]:
        oc_ = options.scorecard(settings)
        md += ["", "## 期权事件波动率研究（备忘第九章，只做研究）", "", options.card_text(oc_).replace("\n", "  \n")]
    out_dir = Path(settings.data) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly-{fri}.md"
    path.write_text("\n".join(md))
    return path
