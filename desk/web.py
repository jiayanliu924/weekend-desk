"""Weekend Desk 网页看板。登录后可看：概览、每日记录（读了什么/预测/结果）、周末预测、期权研究、真钱模拟、历史回测；可下载/推送 PDF。

运行：uvicorn desk.web:app --host 127.0.0.1 --port 8000（前面由 Caddy 提供 https）
账号：python -m desk adduser 名字（在服务器上输入密码；密码只以加盐哈希保存）
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import duckdb
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import config, evaluate, notify, options, pdfreport, rawstore, sim
from .clock import current_or_next_weekend, friday_of, name_of
from .ledger import Ledger

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
E = html.escape


# ------------------------------------------------------------------ users
def _users_path(settings) -> Path:
    p = Path(settings.data) / "state" / "users.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


def add_user(settings, name: str, password: str) -> None:
    if len(password) < 10:
        raise ValueError("密码至少 10 位")
    p = _users_path(settings)
    users = json.loads(p.read_text()) if p.exists() else {}
    salt = secrets.token_bytes(16)
    users[name] = {"salt": salt.hex(), "hash": _hash(password, salt)}
    p.write_text(json.dumps(users))
    os.chmod(p, 0o600)


def del_user(settings, name: str) -> bool:
    p = _users_path(settings)
    users = json.loads(p.read_text()) if p.exists() else {}
    ok = users.pop(name, None) is not None
    p.write_text(json.dumps(users))
    return ok


def check_user(settings, name: str, password: str) -> bool:
    p = _users_path(settings)
    users = json.loads(p.read_text()) if p.exists() else {}
    u = users.get(name)
    if not u:
        _hash(password, b"0" * 16)  # constant-ish time
        return False
    return hmac.compare_digest(u["hash"], _hash(password, bytes.fromhex(u["salt"])))


# ------------------------------------------------------------------ app
S = config.load()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_secret = os.environ.get("WEB_SECRET") or secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_secret, https_only=os.environ.get("WEB_INSECURE") != "1",
                   same_site="lax", max_age=14 * 86400)
_fails: dict[str, list[float]] = {}


def _user(req: Request) -> str | None:
    return req.session.get("user")


def _guard(req: Request):
    if not _user(req):
        return RedirectResponse("/login", status_code=303)
    return None


CSS = """
:root{--bg:#f7f3ee;--card:#fffdf9;--ink:#24201c;--mut:#776e64;--line:#e4dbd0;--acc:#8c3a24;--good:#2f7a4a;--bad:#b23b2e;--chip:#efe6dc}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#171513;--card:#211e1b;--ink:#ece6df;--mut:#a39a90;--line:#36312c;--acc:#e08a6a;--good:#6cc08a;--bad:#ef7d6f;--chip:#2c2824}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"PingFang SC","Noto Sans SC",sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);z-index:5}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px}
.top{display:flex;align-items:center;gap:16px;padding:12px 0;flex-wrap:wrap}
.brand{font-weight:700;color:var(--acc);font-size:17px}
nav{display:flex;gap:4px;flex-wrap:wrap}nav a{padding:6px 10px;border-radius:8px;color:var(--ink);text-decoration:none;font-size:14px}
nav a.on{background:var(--chip);color:var(--acc);font-weight:600}
.who{margin-left:auto;color:var(--mut);font-size:13px}.who a{color:var(--mut)}
h1{font-size:22px;margin:22px 0 4px}h2{font-size:17px;margin:26px 0 8px}
.sub{color:var(--mut);font-size:13.5px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.k{color:var(--mut);font-size:12.5px}.v{font-size:22px;font-weight:650;margin-top:2px}.v.s{font-size:16px}
.good{color:var(--good)}.bad{color:var(--bad)}.mut{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--chip);font-weight:600;font-size:12.5px}
.tw{overflow-x:auto}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;background:var(--chip);font-size:12px;margin-right:4px}
.note{background:var(--chip);border-radius:10px;padding:10px 14px;font-size:13.5px;margin:10px 0}
form.inline{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
label{font-size:12.5px;color:var(--mut);display:flex;flex-direction:column;gap:3px}
input,select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
button,.btn{font:inherit;padding:8px 14px;border-radius:8px;border:0;background:var(--acc);color:#fff;cursor:pointer;text-decoration:none;display:inline-block}
.btn.ghost{background:var(--chip);color:var(--ink)}
.daynav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
ul.news{list-style:none;padding:0;margin:0}ul.news li{padding:8px 0;border-bottom:1px solid var(--line)}
.ev{font-size:12.5px;color:var(--mut);margin-top:2px}
canvas{max-width:100%}
.login{max-width:360px;margin:12vh auto;padding:0 16px}
"""

TABS = [("/", "概览"), ("/day", "每日记录"), ("/weekends", "周末预测"), ("/options", "期权研究"),
        ("/money", "放真钱会怎样"), ("/history", "历史回测"), ("/guide", "这是什么")]


def page(req: Request, active: str, title: str, body: str, head: str = "") -> HTMLResponse:
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in TABS)
    return HTMLResponse(f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)} · Weekend Desk</title>
<style>{CSS}</style>{head}</head><body><header><div class="wrap top"><span class="brand">Weekend Desk</span><nav>{nav}</nav>
<span class="who">{E(_user(req) or '')} · <a href="/logout">退出</a></span></div></header>
<main class="wrap">{body}<p class="sub" style="margin:30px 0">所有盈亏均为模拟或回测，未下真单，不构成投资建议。</p></main></body></html>""")


def _n(x, nd=1, suf="", sign=False):
    if x is None:
        return "—"
    s = f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"
    return s + suf


def _cls(x):
    return "" if not x else ("good" if x > 0 else "bad")


def _pt(ns_or_iso) -> str:
    if ns_or_iso is None:
        return "—"
    if isinstance(ns_or_iso, str):
        dt = datetime.fromisoformat(ns_or_iso)
    else:
        dt = datetime.fromtimestamp(ns_or_iso / 1e9, tz=UTC)
    return dt.astimezone(PT).strftime("%m-%d %H:%M")


# ------------------------------------------------------------------ data helpers
def _day_ns(d: date) -> tuple[int, int, list[str]]:
    a = datetime(d.year, d.month, d.day, tzinfo=PT)
    b = a + timedelta(days=1)
    utc_dates = sorted({a.astimezone(UTC).date().isoformat(), (b - timedelta(seconds=1)).astimezone(UTC).date().isoformat()})
    return int(a.timestamp() * 1e9), int(b.timestamp() * 1e9), utc_dates


def _q(stream: str, dates: list[str], sql: str) -> list[tuple]:
    files = [str(f) for d in dates for f in (Path(S.data) / "raw" / stream / f"date={d}").glob("*.parquet")]
    if not files:
        return []
    con = duckdb.connect()
    return con.execute(sql.replace("SRC", f"read_parquet({files!r})")).fetchall()


def _price_series(coin: str, a: int, b: int, dates: list[str], bucket_min: int = 15):
    rows = _q("hl_ctx", dates, f"""SELECT (received_at // {int(bucket_min*60e9)}) AS k,
        avg(CAST(json_extract_string(payload,'$.data.ctx.oraclePx') AS DOUBLE)),
        avg(CAST(json_extract_string(payload,'$.data.ctx.midPx') AS DOUBLE))
        FROM SRC WHERE key='{coin}' AND received_at >= {a} AND received_at < {b} GROUP BY k ORDER BY k""")
    return [(int(k * bucket_min * 60e9), o, m) for k, o, m in rows]


def _status() -> dict:
    now = datetime.now(UTC)
    today = now.date().isoformat()
    yday = (now - timedelta(days=1)).date().isoformat()
    coins = ",".join(f"'{i['coin']}'" for i in S.instruments)
    lastall = _q("hl_ctx", [yday, today], f"""SELECT key, arg_max(received_at, received_at), arg_max(payload, received_at)
        FROM SRC WHERE key IN ({coins}) GROUP BY key""")
    last = [(r[1], r[2]) for r in lastall if r[0] == S.instrument]
    btc = _q("drb_index", [yday, today], "SELECT received_at, payload FROM SRC ORDER BY received_at DESC LIMIT 1")
    a, b, ds = _day_ns(now.astimezone(PT).date())
    nnews = _q("news", ds, f"SELECT count(*) FROM SRC WHERE received_at >= {a} AND received_at < {b}")
    st = {"xyz": None, "btc": None, "news_today": nnews[0][0] if nnews else 0,
          "llm_key": bool(os.environ.get("ANTHROPIC_API_KEY"))}
    if last:
        c = json.loads(last[0][1])["data"]["ctx"]
        st["xyz"] = {"at": last[0][0], "oracle": float(c["oraclePx"]), "mid": float(c.get("midPx") or c["markPx"])}
    st["all"] = {}
    for key, at, payload in lastall:
        c = json.loads(payload)["data"]["ctx"]
        st["all"][key] = {"at": at, "oracle": float(c["oraclePx"]), "mid": float(c.get("midPx") or c["markPx"])}
    if btc:
        st["btc"] = {"at": btc[0][0], "px": float(json.loads(btc[0][1])["index_price"])}
    return st


# ------------------------------------------------------------------ auth routes
LOGIN = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · Weekend Desk</title><style>{css}</style></head><body><div class="login"><h1>Weekend Desk</h1>
<p class="sub">周末价格预测与期权研究看板</p>{msg}<form method="post" action="/login" class="card" style="display:grid;gap:10px">
<label>用户名<input name="username" autocomplete="username" required></label>
<label>密码<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">登录</button></form></div></body></html>"""


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(req: Request):
    return HTMLResponse(LOGIN.format(css=CSS, msg=""))


@app.post("/login")
def login(req: Request, username: str = Form(...), password: str = Form(...)):
    ip = req.headers.get("x-forwarded-for", req.client.host if req.client else "?").split(",")[0].strip()
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 900]
    if len(recent) >= 5:
        return HTMLResponse(LOGIN.format(css=CSS, msg='<p class="bad">尝试次数过多，请 15 分钟后再试。</p>'), status_code=429)
    if check_user(S, username.strip(), password):
        req.session.clear()
        req.session["user"] = username.strip()
        _fails.pop(ip, None)
        return RedirectResponse("/", status_code=303)
    _fails[ip] = recent + [now]
    return HTMLResponse(LOGIN.format(css=CSS, msg='<p class="bad">用户名或密码不对。</p>'), status_code=401)


@app.get("/logout")
def logout(req: Request):
    req.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def overview(req: Request):
    if (r := _guard(req)):
        return r
    st = _status()
    ws = [current_or_next_weekend(datetime.now(UTC), S, i) for i in S.instruments]
    w = min(ws, key=lambda x: x.decision)
    nxt = [e for e in options.event_samples(S) if e.event_at > datetime.now(UTC)][:3]
    recs = Ledger(S.data).completed()
    card = evaluate.scorecard(recs, S)
    bc = evaluate.scorecard_by_coin(recs, S)
    hist_by = sim.run(S, 2000, 2, "maker", "history")["by_name"]
    inst_rows = "".join(
        f"<tr><td><b>{E(i['name'])}</b></td><td>{_n(st['all'].get(i['coin'], {}).get('oracle'), 2)}</td>"
        f"<td>{x.decision.astimezone(PT):%a %H:%M}</td>"
        f"<td>{bc.get(i['name'], {}).get('n_official', 0)}</td>"
        f"<td class='{_cls(bc.get(i['name'], {}).get('pnl_net_usd_total'))}'>{_n(bc.get(i['name'], {}).get('pnl_net_usd_total'), 2, sign=True)}</td>"
        f"<td class='{_cls(hist_by.get(i['name'], {}).get('pnl'))}'>{_n(hist_by.get(i['name'], {}).get('pnl'), 2, sign=True)}</td></tr>"
        for i, x in zip(S.instruments, ws))
    oc = options.scorecard(S)
    hist = sim.run(S, 2000, 2, "maker", "history")
    live = sim.run(S, 2000, 2, "maker", "live")
    xyz = st["xyz"]
    fresh = xyz and (time.time_ns() - xyz["at"]) < 300e9
    body = f"""<h1>概览</h1><p class="sub">现在是加州时间 {datetime.now(PT):%m-%d %H:%M}。程序 24 小时在服务器上跑；你不用做任何操作，看这里和手机推送就行。</p>
<div class="grid">
<div class="card"><div class="k">系统状态</div><div class="v s {'good' if fresh else 'bad'}">{'正常采集中' if fresh else '数据可能中断'}</div>
<div class="k">{len(st['all'])}/{len(S.instruments)} 个合约在收数据 · 最新 {_pt(xyz and xyz['at'])}</div></div>
<div class="card"><div class="k">今天读了多少新闻</div><div class="v">{st['news_today']}</div><div class="k">{'大模型抽取：已开启' if st['llm_key'] else '<span class=bad>大模型抽取：未填 API Key</span>'}</div></div>
<div class="card"><div class="k">下一次周末预测锁定</div><div class="v s">{w.decision.astimezone(PT):%m-%d（%a）%H:%M}</div><div class="k">加州时间，7 个合约分 2 批锁定</div></div>
<div class="card"><div class="k">下一个期权研究事件</div><div class="v s">{E(nxt[0].event_type) + ' ' + nxt[0].event_at.astimezone(PT).strftime('%m-%d %H:%M') if nxt else '—'}</div><div class="k">{' · '.join(e.event_type + ' ' + e.event_at.astimezone(PT).strftime('%m-%d') for e in nxt[1:])}</div></div>
</div>
<h2>累计成绩</h2><div class="grid">
<div class="card"><div class="k">实时样本（合约×周末）</div><div class="v">{card.get('n_official', 0)}</div><div class="k">每个周末 7 个；30 个周末后下结论</div></div>
<div class="card"><div class="k">预测误差 模型 / 猜周五价 / 猜链上价</div><div class="v s">{_n(card.get('mean_err_model'))} / {_n(card.get('mean_err_A'))} / {_n(card.get('mean_err_B'))} bps</div><div class="k">越小越准；模型要同时赢另外两个才算有用</div></div>
<div class="card"><div class="k">实时模拟盈亏（$2,000 · 2 倍）</div><div class="v {_cls(live['total_usd'])}">${_n(live['total_usd'], 2, sign=True)}</div><div class="k">{live['n']} 个周末，出手 {live['n_traded']} 次</div></div>
<div class="card"><div class="k">历史回测（过去 {hist['n_weekends']} 个周末 × {hist['n_instruments']} 个合约，同样本金）</div><div class="v {_cls(hist['total_usd'])}">${_n(hist['total_usd'], 2, sign=True)}</div><div class="k">胜率 {_n(hist['win_rate'] and hist['win_rate']*100, 0, '%')} · 仅供参考</div></div>
</div>
<div class="note">{E(card.get('verdict', '还没有完成的实时周末，第一个周末完成后这里会出现成绩。'))}<br>期权研究：{E(oc['verdict'])}</div>
<h2>7 个合约</h2><div class="tw"><table><tr><th>合约</th><th>最新价</th><th>本周锁定（加州时间）</th><th>实时正式周末</th><th>实时模拟$</th><th>历史回测$（$2,000·2 倍，平均分）</th></tr>{inst_rows}</table></div>
<h2>报告</h2><div style="display:flex;gap:10px;flex-wrap:wrap"><a class="btn" href="/report.pdf">下载 PDF 报告</a>
<form method="post" action="/push-pdf"><button class="btn ghost" type="submit">把 PDF 推送到手机</button></form></div>
{('<p class="note">' + E(req.query_params.get('msg', '')) + '</p>') if req.query_params.get('msg') else ''}"""
    return page(req, "/", "概览", body)


@app.get("/day", response_class=HTMLResponse)
def day_view(req: Request, d: str | None = None, coin: str | None = None):
    if (r := _guard(req)):
        return r
    try:
        day = date.fromisoformat(d) if d else datetime.now(PT).date()
    except ValueError:
        day = datetime.now(PT).date()
    a, b, ds = _day_ns(day)
    news = _q("news", ds, f"SELECT received_at, key, payload FROM SRC WHERE received_at >= {a} AND received_at < {b} ORDER BY received_at DESC")
    evrows = _q("events", ds + [(day + timedelta(days=1)).isoformat()], "SELECT payload FROM SRC")
    ev_by_key: dict[str, list] = {}
    for (p,) in evrows:
        rec = json.loads(p)
        if a <= rec["news_received_at"] < b:
            ev_by_key[rec["news_key"]] = rec["events"]
    inst = S.inst(coin or "") or S.instruments[0]
    series = _price_series(inst["coin"], a, b, ds)
    btc = _q("drb_index", ds, f"SELECT received_at, payload FROM SRC WHERE received_at >= {a} AND received_at < {b} ORDER BY received_at")
    led_x = [r for r in Ledger(S.data).all() if a <= r["written_at"] < b and r["kind"] in ("lock", "outcome")]
    led_o = [r for r in Ledger(S.data, "options").all() if a <= r["written_at"] < b and r["kind"] in ("lock", "outcome", "surprise")]

    def evtxt(evs):
        out = []
        for e in evs or []:
            if not e.get("evidence_ok"):
                continue
            out.append(f'<span class="tag">{E(",".join(e.get("entities") or []) or "—")}</span>{E(str(e.get("event_type", "")))} · '
                       f'{E(str(e.get("direction_in_text", "")))} · 重要度 {e.get("materiality", 0)}')
        return "<br>".join(out)

    strong = sum(1 for k in ev_by_key for e in ev_by_key[k] if e.get("evidence_ok") and e.get("materiality", 0) >= 2)
    news_html = "".join(
        f'<li><span class="mut">{_pt(r)}</span> {E(json.loads(p).get("title", ""))} '
        f'<span class="tag">{E(Path(json.loads(p).get("feed", "")).parts[1] if "//" in json.loads(p).get("feed", "") else "")}</span>'
        + (f'<div class="ev">{evtxt(ev_by_key.get(k))}</div>' if ev_by_key.get(k) else "") + "</li>"
        for r, k, p in news[:300])

    def led_html(rows, kind):
        out = []
        for r in rows:
            if kind == "x" and r["kind"] == "lock":
                f, pr, ac = r["features"], r["prediction"], r["action"]
                txt = (f"锁定 {r.get('name') or name_of(r['weekend'], 'XYZ100')}（周末 {friday_of(r['weekend'])}）：链上偏离 {_n(f.get('dev_bps'))} bps，实质新闻 {f.get('n_mat2', '—')} 条 → "
                       f"{'信息周末' if f.get('news_weekend') else '噪音周末'}；预测开盘相对周五 {_n(pr.get('pred_bps'))} bps；{ac.get('reason', '')}")
            elif kind == "x":
                txt = (f"结果 {name_of(r['weekend'], 'XYZ100')}（周末 {friday_of(r['weekend'])}）：实际 {_n(r.get('y_bps'))} bps；误差 模型 {_n(r.get('err_model_bps'))} / "
                       f"猜周五 {_n(r.get('err_A_bps'))} / 猜链上 {_n(r.get('err_B_bps'))}；模拟盈亏 ${_n(r.get('pnl_net_usd'), 2, sign=True)}")
            elif r["kind"] == "lock":
                txt = f"期权锁定 {r['weekend']}：未来 24 小时隐含波动 {options._pct(r.get('implied_vol'))}" + ("（窗口内有事件）" if r.get("has_event") else "")
            elif r["kind"] == "outcome":
                txt = f"期权结果 {r['weekend']}：实际波动 {options._pct(r.get('realized_vol'))}，实际/隐含 {_n(r.get('ratio'), 2)}" if r.get("ok") else f"期权结果 {r['weekend']}：{'；'.join(r.get('problems', []))}"
            else:
                txt = f"事件意外程度 {r['weekend']}：{r.get('n_sources', 0)} 条来源，最大意外 {r.get('max_size', '—')}"
            out.append(f"<tr><td class='mut'>{_pt(r['written_at'])}</td><td>{E(txt)}</td></tr>")
        return "".join(out)

    pts = [(datetime.fromtimestamp(t / 1e9, tz=UTC).astimezone(PT).strftime("%H:%M"), o, m) for t, o, m in series]
    chart = ""
    if pts:
        chart = f"""<div class="card"><canvas id="c" height="110"></canvas></div><script>
new Chart(document.getElementById('c'),{{type:'line',data:{{labels:{json.dumps([p[0] for p in pts])},datasets:[
{{label:'预言机价（外部参考）',data:{json.dumps([p[1] for p in pts])},borderColor:'#8c3a24',pointRadius:0,borderWidth:1.6}},
{{label:'链上中间价',data:{json.dumps([p[2] for p in pts])},borderColor:'#5b7fa6',pointRadius:0,borderWidth:1.2,borderDash:[4,3]}}]}},
options:{{plugins:{{legend:{{labels:{{boxWidth:12}}}}}},scales:{{x:{{ticks:{{maxTicksLimit:8}}}}}}}}}});</script>"""
    btc_txt = ""
    if btc:
        p0, p1 = json.loads(btc[0][1])["index_price"], json.loads(btc[-1][1])["index_price"]
        btc_txt = f"BTC 指数 {p0:,.0f} → {p1:,.0f}（{(p1 / p0 - 1) * 100:+.2f}%）"
    body = f"""<h1>每日记录 · {day:%Y-%m-%d}（{'一二三四五六日'[day.weekday()]}）</h1>
<div class="daynav"><a class="btn ghost" href="/day?d={day - timedelta(days=1)}&coin={inst['name']}">← 前一天</a>
<form class="inline" method="get" action="/day"><input type="date" name="d" value="{day}"><button class="btn ghost">跳转</button></form>
<a class="btn ghost" href="/day?d={day + timedelta(days=1)}&coin={inst['name']}">后一天 →</a></div>
<h2>1. 它读了什么</h2><div class="grid">
<div class="card"><div class="k">新闻</div><div class="v">{len(news)}</div><div class="k">其中 {len(ev_by_key)} 条被大模型整理成事件，{strong} 个重要事件（重要度 ≥2）</div></div>
<div class="card"><div class="k">{E(inst['name'])} 价格</div><div class="v s">{_n(series[0][1] if series else None)} → {_n(series[-1][1] if series else None)}</div><div class="k">{btc_txt}</div></div></div>
<h2>当天价格走势 · {E(inst['name'])}</h2><div class="daynav">{''.join(f'<a class="btn {"" if i["name"] == inst["name"] else "ghost"}" href="/day?d={day}&coin={i["name"]}">{E(i["name"])}</a>' for i in S.instruments)}</div>
{chart or '<p class="sub">这一天没有价格记录。</p>'}
<h2>2–3. 它预测了什么、结果如何</h2>
{('<div class="tw"><table><tr><th>时间</th><th>内容</th></tr>' + led_html(led_x, 'x') + led_html(led_o, 'o') + '</table></div>') if (led_x or led_o) else '<p class="sub">这一天没有锁定预测或出结果。周末预测在周日下午 2:45（纳指/标普/原油）和 4:45（英伟达/特斯拉/英特尔/三星）锁定，开盘后几分钟出结果；期权研究每天凌晨 1 点（加州时间）锁一次对照样本，事件前 1 小时锁一次事件样本。</p>'}
<h2>当天新闻（最新在上）</h2><p class="sub">灰色小字是大模型从这条新闻里抽出的事件；没有灰字的是没抽取（只在周末窗口和期权锁定前抽取）或与市场无关。</p>
<ul class="news card">{news_html or '<li class="mut">这一天没有新闻记录。</li>'}</ul>"""
    return page(req, "/day", "每日记录", body, '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>')


@app.get("/weekends", response_class=HTMLResponse)
def weekends(req: Request, coin: str | None = None):
    if (r := _guard(req)):
        return r
    led = Ledger(S.data)
    rows = []
    for x in reversed(led.completed()):
        nm = name_of(x["weekend"], "XYZ100")
        if coin and nm != coin:
            continue
        f, p, a, o = x["lock"]["features"], x["lock"]["prediction"], x["lock"]["action"], x["outcome"]
        rows.append(f"""<tr><td>{friday_of(x['weekend'])}</td><td><b>{E(nm)}</b></td><td>{_n(f.get('fri_close'))}</td><td>{_n(f.get('dev_bps'), sign=True)}</td>
<td>{f.get('n_mat2', '—')} → {'信息' if f.get('news_weekend') else '噪音'}<div class="ev">{E(' | '.join(f.get('strong_titles', [])[:2]))}</div></td>
<td>{_n(p.get('pred_bps'), sign=True)}</td><td>{_n(o.get('y_bps'), sign=True)}</td>
<td>{_n(o.get('err_model_bps'))} / {_n(o.get('err_A_bps'))} / {_n(o.get('err_B_bps'))}</td>
<td>{ {1: '买', -1: '卖', 0: '不出手'}[a.get('side', 0)] }</td><td class="{_cls(o.get('pnl_net_usd'))}">{_n(o.get('pnl_net_usd'), 2, sign=True)}</td>
<td>{'作废：' + E('；'.join(x['lock'].get('void_reasons', []))) if x['lock'].get('void') else ''}</td></tr>""")
    card = evaluate.scorecard(led.completed(), S)
    body = f"""<h1>周末预测</h1><p class="sub">每行一个周末。bps 是万分之一（100 bps = 1%）。"误差"三列：我们的模型 / 永远猜周五收盘价 / 永远猜周日链上价，越小越准。</p>
<div class="note">{E(card.get('verdict', '还没有完成的周末。'))}</div>
<div class="daynav"><a class="btn {'ghost' if coin else ''}" href="/weekends">全部</a>{''.join(f'<a class="btn {"" if coin == i["name"] else "ghost"}" href="/weekends?coin={i["name"]}">{E(i["name"])}</a>' for i in S.instruments)}</div>
<div class="tw" style="margin-top:10px"><table><tr><th>周末（周五）</th><th>合约</th><th>关门价</th><th>链上偏离</th><th>实质新闻 → 判断</th><th>预测开盘</th><th>实际开盘</th><th>误差 模型/周五/链上</th><th>动作</th><th>模拟盈亏 $</th><th>备注</th></tr>
{''.join(rows) or '<tr><td colspan=11 class="mut">第一个周末（10/9–10/11）完成后这里会出现 7 行，每个合约一行。上线前的历史见"历史回测"。</td></tr>'}</table></div>"""
    return page(req, "/weekends", "周末预测", body)


@app.get("/options", response_class=HTMLResponse)
def options_page(req: Request):
    if (r := _guard(req)):
        return r
    rows = []
    for x in reversed(options.completed(Ledger(S.data, "options"))):
        lk, o = x["lock"], x["outcome"]
        rows.append(f"""<tr><td>{E(x['sid'])}</td><td>{E(lk['event_type'])}{' · 窗口有事件' if lk.get('has_event') and lk['sample_kind'] == 'daily' else ''}</td>
<td>{options._pct(lk.get('implied_vol'))}</td><td>{options._pct(o.get('realized_vol'))}</td>
<td class="{'bad' if o.get('ok') and o['ratio'] < 1 else 'good' if o.get('ok') else ''}">{_n(o.get('ratio'), 2)}</td><td>{_n(o.get('abs_move_bps'), 0)}</td></tr>""")
    oc = options.scorecard(S)
    body = f"""<h1>期权研究</h1><p class="sub">问题：CPI、美联储议息这类事件前，期权价格里预期的波动，和实际走出来的波动谁大？<br>
实际/隐含 &lt; 1：期权定价偏贵（买期权的人吃亏）；&gt; 1：偏便宜。这部分只研究，不交易。</p>
<div class="note">{E(options.card_text(oc)).replace(chr(10), '<br>')}</div>
<div class="tw"><table><tr><th>样本</th><th>类型</th><th>隐含波动（年化）</th><th>实际波动（年化）</th><th>实际/隐含</th><th>24h 实际涨跌 bps</th></tr>
{''.join(rows) or '<tr><td colspan=6 class="mut">每天凌晨 1 点（加州时间）锁一次对照样本，24 小时后出结果；第一个事件样本是 10/14 CPI。历史粗看见"历史回测"。</td></tr>'}</table></div>"""
    return page(req, "/options", "期权研究", body)


@app.get("/money", response_class=HTMLResponse)
def money(req: Request, capital: float = 2000, lev: float = 2, mode: str = "maker", source: str = "history"):
    if (r := _guard(req)):
        return r
    mode = mode if mode in ("maker", "taker") else "maker"
    source = source if source in ("live", "history") else "history"
    res = sim.run(S, capital, lev, mode, source)
    by_day: dict[str, float] = {}
    for r_ in res["rows"]:
        by_day[r_["date"]] = r_["equity"]
    labels = list(by_day)
    eq = [round(v, 2) for v in by_day.values()]
    trs = "".join(f"<tr><td>{E(r_['date'])}</td><td>{E(r_.get('name', ''))}</td><td>{ {1: '买', -1: '卖', 0: '—'}[r_['side']] }</td><td>{_n(r_['net_bps'], 1, sign=True)}</td>"
                  f"<td class='{_cls(r_['pnl_usd'])}'>{_n(r_['pnl_usd'], 2, sign=True)}</td><td>{_n(r_['equity'], 2)}</td><td class='mut'>{E(r_['note'])}</td></tr>"
                  for r_ in reversed(res["rows"]))
    opt = res["options"]
    opt_html = ""
    if opt:
        opt_html = "<h2>附：如果每次事件前买跨式期权（估算）</h2><div class='tw'><table><tr><th>事件</th><th>以权利金计的收益</th><th>说明</th></tr>" + "".join(
            f"<tr><td>{E(o['date'])}</td><td class='{_cls(o['net_bps'])}'>{_n(o['net_bps'] / 100, 1, '%', sign=True)}</td><td class='mut'>{E(o['note'])}</td></tr>" for o in opt) + "</table></div>"
    sel = lambda a, b: "selected" if a == b else ""  # noqa: E731
    q = urlencode({"capital": capital, "lev": lev})
    body = f"""<h1>如果放真钱，会赚多少或亏多少</h1>
<p class="sub">按每个周末的真实结果，换算成你填的本金和杠杆。"历史回测"用上线前的小时 K 线，没有新闻过滤、成交价取得理想，<b>会偏乐观</b>；"实时模拟"是上线后锁定的正式记录。</p>
<form class="inline card" method="get" action="/money">
<label>本金（美元）<input type="number" name="capital" value="{capital:g}" min="100" step="100"></label>
<label>杠杆（倍）<input type="number" name="lev" value="{lev:g}" min="0.5" max="30" step="0.5"></label>
<label>成交方式<select name="mode"><option value="maker" {sel(mode, 'maker')}>挂单进、吃单出（便宜）</option><option value="taker" {sel(mode, 'taker')}>进出都吃单（贵）</option></select></label>
<label>数据<select name="source"><option value="history" {sel(source, 'history')}>历史回测（{len(sim._history_weekend_trades(S, mode))} 笔）</option><option value="live" {sel(source, 'live')}>实时模拟</option></select></label>
<button type="submit">计算</button></form>
<div class="grid" style="margin-top:12px">
<div class="card"><div class="k">累计盈亏</div><div class="v {_cls(res['total_usd'])}">${_n(res['total_usd'], 2, sign=True)}</div><div class="k">收益率 {_n(res['return_pct'], 1, '%', sign=True)}</div></div>
<div class="card"><div class="k">折合一年</div><div class="v {_cls(res['annualized_usd'])}">${_n(res['annualized_usd'], 0, sign=True)}</div><div class="k">按 52 个周末线性外推</div></div>
<div class="card"><div class="k">胜率 / 出手</div><div class="v s">{_n(res['win_rate'] and res['win_rate']*100, 0, '%')} / {res['n_traded']} 次</div><div class="k">{res['n_weekends']} 个周末 × {res['n_instruments']} 个合约</div></div>
<div class="card"><div class="k">最大回撤 / 最差一周</div><div class="v s bad">{_n(res['max_drawdown_pct'], 1, '%')} / ${_n(res['worst_usd'], 2)}</div><div class="k">最好一周 ${_n(res['best_usd'], 2, sign=True)}</div></div>
<div class="card"><div class="k">压力情形（一次）</div><div class="v s bad">${_n(res['stress_usd'], 0)}</div><div class="k">真出大事、开盘反向再走 3%：本金的 {_n(res['stress_pct'], 0, '%')}</div></div>
</div>
{'<div class="card" style="margin-top:12px"><canvas id="eq" height="100"></canvas></div>' if eq else '<p class="note">还没有数据。实时模拟要等第一个周末完成；先看历史回测。</p>'}
<script>if(document.getElementById('eq'))new Chart(document.getElementById('eq'),{{type:'line',data:{{labels:{json.dumps(labels)},datasets:[{{label:'账户金额（美元）',data:{json.dumps(eq)},borderColor:'#8c3a24',pointRadius:2,borderWidth:1.6,fill:false}}]}},options:{{plugins:{{legend:{{display:false}}}}}}}});</script>
<h2>分合约</h2><p class="sub">本金平均分给 {res['n_instruments']} 个合约：每个合约每周末下注 ${res['per_instrument_notional']:,.0f}（本金 × 杠杆 ÷ {res['n_instruments']}）。</p>
<div class="tw"><table><tr><th>合约</th><th>出手</th><th>赚钱次数</th><th>累计盈亏 $</th><th>最差一次 $</th></tr>{''.join(f"<tr><td><b>{E(k)}</b></td><td>{v['traded']}</td><td>{v['wins']}</td><td class='{_cls(v['pnl'])}'>{_n(v['pnl'], 2, sign=True)}</td><td class='bad'>{_n(v['worst'], 2)}</td></tr>" for k, v in res['by_name'].items())}</table></div>
<details><summary style="cursor:pointer;margin:18px 0 8px;font-weight:600">逐笔明细（{len(res['rows'])} 笔，点开查看）</summary><div class="tw"><table><tr><th>周末</th><th>合约</th><th>方向</th><th>扣费后 bps</th><th>盈亏 $</th><th>账户 $</th><th>说明</th></tr>{trs}</table></div></details>
{opt_html}
<div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap"><a class="btn" href="/report.pdf?{q}">按这组参数下载 PDF</a>
<form method="post" action="/push-pdf"><input type="hidden" name="capital" value="{capital:g}"><input type="hidden" name="lev" value="{lev:g}"><button class="btn ghost">推送到手机</button></form></div>"""
    return page(req, "/money", "放真钱会怎样", body, '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>')


@app.get("/history", response_class=HTMLResponse)
def history(req: Request):
    if (r := _guard(req)):
        return r
    pw = Path(S.data) / "reports" / "history_weekends.json"
    po = Path(S.data) / "reports" / "history_options.json"
    import statistics as stt
    wk = json.loads(pw.read_text()) if pw.exists() else []
    op = json.loads(po.read_text()) if po.exists() else {"events": [], "control": []}
    wrows = "".join(f"<tr><td>{x['weekend']}</td><td>{E(x.get('name', 'XYZ100'))}</td><td>{_n(x['dev_bps'], 0, sign=True)}</td><td>{_n(x['y_bps'], 0, sign=True)}</td>"
                    f"<td>{_n(x['err_A'], 0)}</td><td>{_n(x['err_B'], 0)}</td><td>{'是' if (x['dev_bps'] > 0) == (x['y_bps'] > 0) else '否'}</td></tr>"
                    for x in sorted(wk, key=lambda x: (x['weekend'], x.get('name', '')), reverse=True))
    names = sorted({x.get('name', 'XYZ100') for x in wk}, key=lambda n: [i['name'] for i in S.instruments].index(n) if S.inst(n) else 99)
    per = ""
    for nm in names:
        xs = [x for x in wk if x.get('name', 'XYZ100') == nm]
        per += (f"<tr><td><b>{E(nm)}</b></td><td>{len(xs)}</td><td>{_n(stt.median([x['err_A'] for x in xs]), 0)}</td>"
                f"<td>{_n(stt.median([x['err_B'] for x in xs]), 0)}</td><td>{sum(1 for x in xs if (x['dev_bps'] > 0) == (x['y_bps'] > 0))}/{len(xs)}</td></tr>")
    ctl = [c["ratio"] for c in op["control"]]
    body = f"""<h1>历史回测（上线前，仅供参考）</h1><p class="sub">用回补的小时 K 线算的，不计入正式成绩。正式成绩只算上线后实时锁定的记录（研究备忘 7.2：事后找回来的历史只能用于粗略探索）。</p>
<h2>周末合约：过去约 {len({x['weekend'] for x in wk})} 个周末 × {len(names)} 个合约</h2>
<div class="tw"><table><tr><th>合约</th><th>周末数</th><th>猜关门价误差（中位 bps）</th><th>猜链上价误差（中位 bps）</th><th>链上方向=开盘方向</th></tr>{per}</table></div>

<h2>逐周明细</h2><div class="tw" style="margin-top:12px"><table><tr><th>周末</th><th>合约</th><th>重开前链上偏离 bps</th><th>开盘后 bps</th><th>猜周五误差</th><th>猜链上误差</th><th>方向一致</th></tr>{wrows or '<tr><td colspan=7 class=mut>还没有回补数据。在服务器上运行 desk backfill。</td></tr>'}</table></div>
<h2>期权：实际/隐含 波动</h2>
<div class="grid"><div class="card"><div class="k">普通日（{len(ctl)} 天）平均</div><div class="v">{_n(stt.geometric_mean(ctl) if ctl else None, 2)}</div><div class="k">&lt;1：期权大多数时候偏贵</div></div>
{''.join(f'<div class="card"><div class="k">议息日 {E(e["date"])}</div><div class="v">{_n(e["ratio"], 2)}</div></div>' for e in op['events'])}</div>
<p class="sub">口径：隐含波动用 Deribit 30 天波动率指数近似，会把单个事件摊薄，所以只能看方向。</p>"""
    return page(req, "/history", "历史回测", body)


@app.get("/guide", response_class=HTMLResponse)
def guide(req: Request):
    if (r := _guard(req)):
        return r
    body = """<h1>这是什么</h1>
<div class="card"><h2 style="margin-top:0">1. 它在读什么</h2><p>Hyperliquid（trade.xyz）上 7 个合约每秒的价格、挂单、成交：纳指 XYZ100、标普 SP500、布伦特原油、英伟达、特斯拉、英特尔、三星；以及美联储、CNBC、MarketWatch、Google 新闻、美国证监会的新闻。大模型把每条新闻整理成一行：哪家公司、利好利空、重要程度 0–3。</p>
<h2>2. 它预测什么</h2><p>真市场周末关门，但这些合约周末照样能买卖，价格会被少数大单推来推去。每个合约在真市场重开前 15 分钟（纳指/标普/原油：周日下午 2:45；英伟达/特斯拉/英特尔/三星：下午 4:45，加州时间），程序预测：开盘、合约被拉回真实价格时，价格会落在哪。没有重要新闻 → 判断"噪音周末"，预测会弹回周五价；有重要新闻 → 判断"信息周末"，预测周末的涨跌是真的。</p>
<h2>3. 结果是什么</h2><p>开盘后自动对答案：我们的预测、"永远猜周五价"、"永远猜周日链上价"三者谁更准；再记一笔模拟账，扣掉手续费后赚了还是亏了。</p>
<h2>4. 怎么赚钱</h2><p>在价格被"推过头"时反着买，开盘被拉回来时卖掉，赚那一截差价——本质是周末替着急的人接盘、扛两天风险。真出大事的周末不接（这就是读新闻的用处）。按历史粗算，股票类合约平均每周末每个合约赚约 0.05%–0.1%，金额很小，还不能确定不是运气。具体看"放真钱会怎样"。</p>
<h2>期权研究</h2><p>CPI、美联储开会前，记下期权市场预期 24 小时比特币会波动多大，事后对比实际波动。只研究，不交易。</p></div>"""
    return page(req, "/guide", "这是什么", body)


@app.get("/report.pdf")
def report_pdf(req: Request, capital: float = 2000, lev: float = 2):
    if (r := _guard(req)):
        return r
    data = pdfreport.build(S, capital, lev)
    fn = f"weekend-desk-{datetime.now(PT):%Y%m%d}.pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.post("/push-pdf")
def push_pdf(req: Request, capital: float = Form(2000), lev: float = Form(2)):
    if (r := _guard(req)):
        return r
    origin = req.headers.get("origin")
    if origin and req.headers.get("host") and origin.split("//")[-1] != req.headers["host"]:
        return Response("bad origin", status_code=403)
    data = pdfreport.build(S, capital, lev)
    ok = notify.push_file(S, "Weekend Desk 报告", f"{_user(req)} 从网页推送", data, f"weekend-desk-{datetime.now(PT):%Y%m%d}.pdf")
    msg = "已推送到手机（ntfy）" if ok else "推送失败：服务器上没有设置推送频道"
    return RedirectResponse("/?" + urlencode({"msg": msg}), status_code=303)
