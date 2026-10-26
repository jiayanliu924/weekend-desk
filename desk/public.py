"""面向投资人（LP）的公开首页。只读本地账本与配置，不联网；任何读取失败都降级为空。"""
from __future__ import annotations

from datetime import datetime
from html import escape as E
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

from .daily_report import STRATS, STRAT_ZH, strat_summary, strategy_trades, zh_coin
from .ledger import Ledger

PT = ZoneInfo("America/Los_Angeles")
router = APIRouter()


def _settings():
    from . import web
    return web.S


def real_trades(settings) -> list[dict]:
    """三条线的真实资金平仓（含 strat 字段），按时间排序。"""
    return strategy_trades(settings)


def stats(trades: list[dict], now: datetime | None = None) -> dict | None:
    if not trades:
        return None
    now = now or datetime.now(PT)
    first = datetime.fromtimestamp(trades[0]["ms"] / 1000, PT)
    cum = peak = dd = 0.0
    for t in trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"start": first.date(), "days": max(1, (now.date() - first.date()).days + 1), "n": len(trades),
            "win": sum(1 for t in trades if t["pnl"] > 0) / len(trades), "total": cum, "dd": dd}


def _usd(v: float, sign: bool = True) -> str:
    return f"{'+' if sign and v >= 0 else '-' if v < 0 else ''}${abs(v):,.2f}"


def report_names(settings, n: int = 30) -> list[str]:
    try:
        d = Path(settings.data) / "reports" / "daily"
        return [p.name for p in sorted(d.glob("*.pdf"), reverse=True)[:n]] if d.is_dir() else []
    except Exception:  # noqa: BLE001
        return []


CSS = """
:root{--bg:#fff;--ink:#1a2233;--mut:#667085;--line:#e6e9ef;--acc:#0e7c7b;--soft:#f5f8f8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Noto Sans SC","Microsoft YaHei",sans-serif;font-variant-numeric:tabular-nums}
.w{max-width:760px;margin:0 auto;padding:0 20px}
a{color:var(--acc)}
.hero{padding:64px 0 36px}
.hero h1{margin:0 0 14px;font-size:38px;letter-spacing:.3px;color:var(--acc)}
.hero p{margin:0;font-size:18px;color:var(--ink)}
section{padding:28px 0;border-top:1px solid var(--line)}
h2{margin:0 0 14px;font-size:20px}
.note{color:var(--mut);font-size:14px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:14px 0}
.k{background:var(--soft);border-radius:10px;padding:14px 16px}
.k span{display:block;color:var(--mut);font-size:13px}.k b{font-size:22px}
table{border-collapse:collapse;width:100%;font-size:15px}
td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}
.reps{display:flex;flex-wrap:wrap;gap:8px 14px}
footer{padding:28px 0 48px;border-top:1px solid var(--line);color:var(--mut);font-size:13px}
@media(max-width:560px){.grid{grid-template-columns:1fr 1fr}.hero{padding-top:40px}.hero h1{font-size:30px}}
"""


def render(settings, now: datetime | None = None) -> str:
    try:
        trades = real_trades(settings)
        st = stats(trades, now)
    except Exception:  # noqa: BLE001
        trades, st = [], None
    if st:
        k = [("开始日期", f"{st['start']:%Y-%m-%d}"), ("运行天数", f"{st['days']} 天"), ("真实成交笔数", f"{st['n']} 笔"),
             ("胜率", f"{st['win'] * 100:.0f}%"), ("累计盈亏", _usd(st["total"])), ("最大回撤", _usd(-st["dd"]) if st["dd"] else "$0.00")]
        cards = "".join(f'<div class="k"><span>{a}</span><b>{E(b)}</b></div>' for a, b in k)
        rows = "".join(f"<tr><td>{datetime.fromtimestamp(t['ms'] / 1000, PT):%Y-%m-%d}</td><td>{E(STRAT_ZH.get(t['strat'], ''))}</td><td>{E(t['coin'])}</td><td>{E(_usd(t['pnl']))}</td></tr>"
                       for t in reversed(trades[-10:]))
        sm = strat_summary(trades)
        srows = "".join(f"<tr><td>{nm}</td><td>{sm[k]['n']}</td><td>{'-' if sm[k]['win'] is None else format(sm[k]['win'] * 100, '.0f') + '%'}</td><td>{E(_usd(sm[k]['total']))}</td></tr>"
                        for k, nm in STRATS + (("all", "合计"),))
        perf = (f'<div class="grid">{cards}</div><p class="note">最大回撤按平仓顺序的累计盈亏计算。</p>'
                f'<h3 style="font-size:16px;margin:18px 0 6px">分策略业绩</h3><table><tr><th>策略</th><th>成交笔数</th><th>胜率</th><th>累计盈亏</th></tr>{srows}</table>'
                f'<h3 style="font-size:16px;margin:18px 0 6px">最近 10 笔</h3><table><tr><th>日期</th><th>策略</th><th>品种</th><th>盈亏</th></tr>{rows}</table>')
    else:
        perf = "<p>运行中，暂无成交</p>"
    try:
        from . import reopen_exec as rx
        c = rx.cfg(settings)
        risk = (f"<ul><li>每笔上限：${c['notional_usd']:,.0f}</li><li>当日止损：${c['daily_loss_stop_usd']:,.0f}</li>"
                f"<li>全账户止损：${c['cum_loss_stop_usd']:,.0f}</li><li>杠杆：1 倍</li><li>只挂限价单</li></ul>")
    except Exception:  # noqa: BLE001
        risk = "<ul><li>杠杆：1 倍</li><li>只挂限价单</li></ul>"
    names = report_names(settings)
    reps = ('<div class="reps">' + "".join(f'<a href="/reports/{E(quote(n))}">{E(n[:-4])}</a>' for n in names) + "</div>") if names else '<p class="note">暂无日报</p>'
    try:
        mail = str(((settings.raw.get("site") or {}).get("contact_email")) or "").strip()
    except Exception:  # noqa: BLE001
        mail = ""
    contact = f'<section><h2>联系</h2><p><a href="mailto:{E(mail)}">{E(mail)}</a></p></section>' if mail else ""
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weekend Desk</title><style>{CSS}</style></head><body><div class="w">
<div class="hero"><h1>Weekend Desk</h1><p>用规则驱动的程序化策略，在外部市场重新开盘、链上合约被迫对齐的时刻，接住被迫成交的单子。</p></div>
<section><h2>我们怎么赚钱</h2>
<h3 style="font-size:16px;margin:10px 0 4px">周末策略</h3><p>周末美股休市，链上合约照常交易。如果合约价格偏离周五收盘价太多，我们就反方向挂一笔限价单，赌周一开盘后价格回归。</p>
<h3 style="font-size:16px;margin:10px 0 4px">重开策略</h3><p>股指、原油、黄金等市场收盘期间，链上的永续合约仍在交易；外部市场重新开盘时，合约价格会被迫向真实价格靠拢，期间常有人被迫成交。
我们提前盯住参考价，只有价格偏离超过能覆盖手续费的门槛时，才在盘口挂一笔限价小单。价格回归即平仓；到时间未回归也强制平仓。每一步都由预先写好的规则执行，不凭主观判断。</p>
<h3 style="font-size:16px;margin:10px 0 4px">跨平台配对</h3><p>同一只股票在两家链上交易所的价格差太大时，一边买、一边卖，等价差收回来再平仓。</p></section>
<section><h2>真实业绩</h2><p class="note">全部为真实资金成交，不含任何回测或模拟。</p>{perf}</section>
<section><h2>风控</h2>{risk}</section>
<section><h2>每日报告</h2>{reps}</section>
{contact}
<footer><p>过往业绩不代表未来收益。本页面仅供信息参考，不构成投资建议或要约。</p><p><a href="/admin" style="color:inherit">管理后台</a></p></footer>
</div></body></html>"""


@router.get("/", response_class=HTMLResponse)
def home():
    try:
        return HTMLResponse(render(_settings()))
    except Exception:  # noqa: BLE001
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Weekend Desk</title><h1>Weekend Desk</h1><p>运行中，暂无成交</p>")


@router.get("/reports/{name}")
def report_dl(name: str):
    base = (Path(_settings().data) / "reports" / "daily").resolve()
    try:
        f = (base / name).resolve()
        ok = f.parent == base and f.suffix == ".pdf" and f.is_file() and "/" not in name and "\\" not in name
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        return Response("没有这份日报", status_code=404)
    return Response(f.read_bytes(), media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(f.name)})
