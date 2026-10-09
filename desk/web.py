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

from . import agents, config, evaluate, notify, options, pdfreport, rawstore, sim
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


def make_reset_token(settings, name: str, minutes: int = 30) -> str:
    """One-time link to set a password in the browser (avoids typing blind in the console)."""
    p = Path(settings.data) / "state" / "reset_tokens.json"
    toks = json.loads(p.read_text()) if p.exists() else {}
    now = time.time()
    toks = {k: v for k, v in toks.items() if v["exp"] > now}
    tok = secrets.token_urlsafe(24)
    toks[hashlib.sha256(tok.encode()).hexdigest()] = {"user": name, "exp": now + minutes * 60}
    p.write_text(json.dumps(toks))
    os.chmod(p, 0o600)
    return tok


def use_reset_token(settings, tok: str, consume: bool) -> str | None:
    p = Path(settings.data) / "state" / "reset_tokens.json"
    if not p.exists():
        return None
    toks = json.loads(p.read_text())
    key = hashlib.sha256(tok.encode()).hexdigest()
    v = toks.get(key)
    if not v or v["exp"] < time.time():
        return None
    if consume:
        toks.pop(key)
        p.write_text(json.dumps(toks))
    return v["user"]


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
:root{--bg:#f4f6f9;--card:#ffffff;--ink:#14213d;--mut:#64708a;--line:#e2e7ef;--acc:#0e7c7b;--acc2:#e6f3f3;--good:#16834d;--bad:#c93c3c;--chip:#eef1f6;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0e1424;--card:#151d31;--ink:#e6ebf5;--mut:#93a0ba;--line:#26314b;--acc:#3cc3bf;--acc2:#16313a;--good:#4cc38a;--bad:#f07b7b;--chip:#1c2640;--warn:#e3b04b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC","Microsoft YaHei",sans-serif;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
a{color:var(--acc)}
header{position:sticky;top:0;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line);z-index:5}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
.top{display:flex;align-items:center;gap:18px;flex-wrap:wrap;min-height:56px}
.brand{font-weight:700;font-size:16px;letter-spacing:.2px;color:var(--ink);display:flex;align-items:center;gap:8px}
.brand i{width:10px;height:10px;border-radius:50%;background:var(--acc);display:inline-block}
nav{display:flex;gap:2px;flex-wrap:wrap;overflow-x:auto}
nav a{padding:17px 10px 15px;color:var(--mut);text-decoration:none;font-size:14px;border-bottom:2px solid transparent;white-space:nowrap}
nav a:hover{color:var(--ink)}
nav a.on{color:var(--ink);border-bottom-color:var(--acc);font-weight:600}
.who{margin-left:auto;color:var(--mut);font-size:13px}.who a{color:var(--mut)}
main{padding-bottom:20px}
h1{font-size:26px;line-height:1.3;margin:28px 0 6px;font-weight:700;letter-spacing:-.2px}
h2{font-size:17px;margin:34px 0 12px;font-weight:650}
h3{font-size:15px}
.sub{color:var(--mut);font-size:14px;margin:0 0 16px;max-width:72ch}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.k{color:var(--mut);font-size:13px}.v{font-size:24px;font-weight:700;margin-top:2px;letter-spacing:-.3px}.v.s{font-size:17px;font-weight:650;letter-spacing:0}
.good{color:var(--good)}.bad{color:var(--bad)}.mut{color:var(--mut)}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}
th{background:var(--chip);font-weight:600;font-size:13px;color:var(--mut)}
td.n,th.n{text-align:right}
.tw{overflow-x:auto;border-radius:12px}
.tag{display:inline-block;padding:2px 9px;border-radius:99px;background:var(--chip);font-size:12.5px;margin:0 4px 4px 0;color:var(--ink)}
.note{background:var(--acc2);border-radius:10px;padding:12px 16px;font-size:14px;margin:12px 0;border-left:3px solid var(--acc)}
form.inline{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
label{font-size:13px;color:var(--mut);display:flex;flex-direction:column;gap:4px}
input,select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input:focus,select:focus,button:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
button,.btn{font:inherit;font-weight:600;padding:9px 16px;border-radius:9px;border:0;background:var(--acc);color:#fff;cursor:pointer;text-decoration:none;display:inline-block}
button:disabled{opacity:.5;cursor:default}
.btn.ghost,button.ghost{background:var(--chip);color:var(--ink)}
.daynav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
ul.news{list-style:none;padding:0;margin:0}ul.news li{padding:10px 0;border-bottom:1px solid var(--line)}
.ev{font-size:13px;color:var(--mut);margin-top:2px}
canvas{max-width:100%}
.login{max-width:380px;margin:12vh auto;padding:0 16px}
/* overview */
.hero{display:grid;grid-template-columns:1.1fr 1fr;gap:18px;align-items:stretch;margin-top:22px}
@media (max-width:820px){.hero{grid-template-columns:1fr}}
.countdown .big{font-size:40px;font-weight:750;letter-spacing:-1px;line-height:1.1;margin:6px 0 4px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px;background:var(--good)}
.dot.off{background:var(--bad)}
.tl{position:relative;margin:26px 4px 6px;height:64px}
.tl .bar{position:absolute;left:0;right:0;top:22px;height:6px;border-radius:6px;background:var(--chip)}
.tl .seg{position:absolute;top:22px;height:6px;border-radius:6px;background:var(--acc);opacity:.35}
.tl .m{position:absolute;top:14px;transform:translateX(-50%);text-align:center;font-size:11.5px;color:var(--mut);white-space:nowrap}
.tl .m b{display:block;width:2px;height:22px;background:var(--ink);margin:0 auto 4px;opacity:.55}
.tl .now{position:absolute;top:6px;transform:translateX(-50%);font-size:11.5px;color:var(--acc);font-weight:700}
.tl .now b{display:block;width:12px;height:12px;border-radius:50%;background:var(--acc);margin:0 auto 2px;box-shadow:0 0 0 4px var(--acc2)}
.steps{list-style:none;padding:0;margin:22px 0 12px;display:grid;grid-template-columns:repeat(4,1fr);gap:10px;counter-reset:s}
.steps li{position:relative;padding-top:34px}
.steps li::before{content:"";position:absolute;top:13px;left:26px;right:-10px;height:2px;background:var(--line)}
.steps li:last-child::before{display:none}
.steps .n{position:absolute;top:0;left:0;width:26px;height:26px;border-radius:50%;background:var(--chip);color:var(--mut);font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center}
.steps b{display:block;font-size:14px}.steps .t{display:block;font-size:12.5px;color:var(--ink);opacity:.8}.steps .d{display:block;font-size:12px;color:var(--mut);line-height:1.5;margin-top:2px}
.steps li.done .n{background:var(--acc2);color:var(--acc)}.steps li.done::before{background:var(--acc);opacity:.4}
.steps li.on .n{background:var(--acc);color:#fff;box-shadow:0 0 0 4px var(--acc2)}
@media (max-width:600px){.steps{grid-template-columns:1fr 1fr;row-gap:18px}.steps li:nth-child(2)::before{display:none}}
@media (max-width:720px){.top{gap:0 12px;min-height:0;padding-top:8px}.brand{order:1}.who{order:2}nav{order:3;width:100%;flex-wrap:nowrap;margin:0 -16px;padding:0 6px}nav a{padding:10px 10px 9px}}
.strip{display:grid;grid-template-columns:repeat(3,1fr);background:var(--card);border:1px solid var(--line);border-radius:12px}
.strip>div{padding:16px 18px;border-right:1px solid var(--line)}.strip>div:last-child{border-right:0}
@media (max-width:720px){.strip{grid-template-columns:1fr}.strip>div{border-right:0;border-bottom:1px solid var(--line)}}
.plan td:first-child{font-weight:600}
.lean-fade{color:var(--acc);font-weight:600}.lean-skip{color:var(--mut)}.lean-follow{color:var(--warn);font-weight:600}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

TABS = [("/", "概览"), ("/agents", "Agent 讨论室"), ("/day", "每日记录"), ("/weekends", "周末预测"), ("/options", "期权研究"),
        ("/money", "放真钱会怎样"), ("/history", "历史回测"), ("/guide", "这是什么"), ("/qa", "测试"), ("/settings", "设置")]


def page(req: Request, active: str, title: str, body: str, head: str = "") -> HTMLResponse:
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in TABS)
    return HTMLResponse(f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)} · Weekend Desk</title>
<style>{CSS}</style>{head}</head><body><header><div class="wrap top"><span class="brand"><i></i>Weekend Desk</span><nav>{nav}</nav>
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


SETPW = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>设置密码 · Weekend Desk</title><style>{css}</style></head><body><div class="login"><h1>设置密码</h1>
<p class="sub">账号：<b>{user}</b>。这个链接只能用一次，30 分钟内有效。</p>{msg}
<form method="post" action="/setpw" class="card" style="display:grid;gap:10px"><input type="hidden" name="token" value="{tok}">
<label>新密码（至少 10 位）<input name="p1" type="password" autocomplete="new-password" required minlength="10"></label>
<label>再输一次<input name="p2" type="password" autocomplete="new-password" required minlength="10"></label>
<label style="flex-direction:row;gap:6px;align-items:center"><input type="checkbox" onclick="for(const i of document.querySelectorAll('input[type=password],input.shown')){{i.type=this.checked?'text':'password';i.classList.toggle('shown',this.checked)}}"> 显示密码</label>
<button type="submit">保存并去登录</button></form></div></body></html>"""


@app.get("/setpw", response_class=HTMLResponse)
def setpw_form(token: str = ""):
    user = use_reset_token(S, token, consume=False)
    if not user:
        return HTMLResponse(LOGIN.format(css=CSS, msg='<p class="bad">链接无效或已过期，请在服务器上重新生成。</p>'), status_code=400)
    return HTMLResponse(SETPW.format(css=CSS, user=E(user), tok=E(token), msg=""))


@app.post("/setpw", response_class=HTMLResponse)
def setpw(token: str = Form(...), p1: str = Form(...), p2: str = Form(...)):
    user = use_reset_token(S, token, consume=False)
    if not user:
        return HTMLResponse(LOGIN.format(css=CSS, msg='<p class="bad">链接无效或已过期。</p>'), status_code=400)
    if p1 != p2:
        return HTMLResponse(SETPW.format(css=CSS, user=E(user), tok=E(token), msg='<p class="bad">两次输入不一致。</p>'), status_code=400)
    if len(p1) < 10:
        return HTMLResponse(SETPW.format(css=CSS, user=E(user), tok=E(token), msg='<p class="bad">密码至少 10 位。</p>'), status_code=400)
    add_user(S, user, p1)
    use_reset_token(S, token, consume=True)
    return HTMLResponse(LOGIN.format(css=CSS, msg=f'<p class="good">已为 {E(user)} 设置密码，请登录。</p>'))


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
        f"<tr><td><b>{E(i['name'])}</b></td><td class='n'>{_n(st['all'].get(i['coin'], {}).get('oracle'), 2)}</td>"
        f"<td>{x.decision.astimezone(PT):%a %H:%M}</td>"
        f"<td class='n'>{bc.get(i['name'], {}).get('n_official', 0)}</td>"
        f"<td class='n {_cls(bc.get(i['name'], {}).get('pnl_net_usd_total'))}'>{_n(bc.get(i['name'], {}).get('pnl_net_usd_total'), 2, sign=True)}</td>"
        f"<td class='n {_cls(hist_by.get(i['name'], {}).get('pnl'))}'>{_n(hist_by.get(i['name'], {}).get('pnl'), 2, sign=True)}</td></tr>"
        for i, x in zip(S.instruments, ws))
    oc = options.scorecard(S)
    hist = sim.run(S, 2000, 2, "maker", "history")
    live = sim.run(S, 2000, 2, "maker", "live")
    xyz = st["xyz"]
    fresh = xyz and (time.time_ns() - xyz["at"]) < 300e9
    now = datetime.now(UTC)
    # —— 下一个关键时刻（倒计时）
    groups = {}
    for i, x in zip(S.instruments, ws):
        groups.setdefault((x.close, x.decision, x.resume), []).append(i["name"])
    moments = []
    for (c, d, rz), names in groups.items():
        lab = "、".join(names) if len(names) <= 3 else f"{len(names)} 个合约"
        moments += [(c, f"{lab} 真市场收盘"), (d, f"锁定 {lab} 的预测"), (rz, f"{lab} 开盘、对答案")]
    moments.sort()
    nxt_m = next(((t, l) for t, l in moments if t > now), None)
    if nxt_m:
        left = nxt_m[0] - now
        hrs = left.total_seconds() / 3600
        cd = f"{int(hrs // 24)} 天 {int(hrs % 24)} 小时" if hrs >= 24 else f"{int(hrs)} 小时 {int(left.total_seconds() % 3600 // 60)} 分"
        cd_label = f"距离 {nxt_m[1]}（{nxt_m[0].astimezone(PT):%a %H:%M}）"
    else:
        cd, cd_label = "—", ""
    # —— 周末流程（按先后顺序的四步，标出现在走到哪一步）
    non_kr = [x for x in ws if x.name != "SMSN"] or ws
    c_us = min(x.close for x in non_kr)
    d1, d2 = min(x.decision for x in ws), max(x.decision for x in ws)
    r1, r2 = min(x.resume for x in ws), max(x.resume for x in ws)
    steps = [("周五收盘", f"{c_us.astimezone(PT):%a %H:%M}", "真市场关门，链上照常交易", c_us),
             ("周末", "周五晚到周日", "链上价格没人管，容易被推偏；程序一直在读价格和新闻", c_us + timedelta(minutes=1)),
             ("锁定预测", f"{d1.astimezone(PT):%a %H:%M} / {d2.astimezone(PT):%H:%M}", "开盘前 15 分钟写下预测，之后不许改", d1),
             ("开盘对答案", f"{r1.astimezone(PT):%a %H:%M} / {r2.astimezone(PT):%H:%M}", "价格被拉回真实水平，电脑算盈亏、推送到手机", r1)]
    cur = max([i for i, st_ in enumerate(steps) if now >= st_[3]] or [-1])
    if now > r2 + timedelta(hours=1):
        cur = -1
    step_html = "".join(
        f'<li class="{"done" if i < cur else "on" if i == cur else ""}"><span class="n">{i + 1}</span>'
        f'<b>{t}</b><span class="t">{tm}</span><span class="d">{d}</span></li>'
        for i, (t, tm, d, _) in enumerate(steps))
    # —— 最近一次 agent 会议给的本周末打算
    try:
        lr = agents.last_full_run(S, within_h=72)
    except Exception:  # noqa: BLE001
        lr = None
    plan_rows, plan_head = "", ""
    if lr:
        props = [p for p in (lr.get("rooms", {}).get("trading", {}).get("synth", {}).get("proposal") or []) if isinstance(p, dict)]
        lean_txt = {"fade": ("押它弹回来", "lean-fade"), "follow": ("顺着走", "lean-follow"), "skip": ("不做", "lean-skip")}
        for p in props:
            lt, cls = lean_txt.get(str(p.get("lean")), (str(p.get("lean")), ""))
            try:
                sz = float(p.get("size") or 0)
            except (TypeError, ValueError):
                sz = 0
            plan_rows += (f'<tr><td>{E(str(p.get("name")))}</td><td class="{cls}">{lt}</td>'
                          f'<td class="n">{"—" if not sz else f"{sz * 100:.0f}%"}</td><td class="ev">{E(pdfreport._plain(p.get("why", "")))}</td></tr>')
        ch = lr.get("chair") or {}
        plan_head = (f'<p class="sub" style="margin-bottom:10px">{E(str(ch.get("headline", "")))} '
                     f'<a href="/agents">看完整讨论</a></p>')
    plan = (f'<div class="tw"><table class="plan"><tr><th>合约</th><th>打算</th><th class="n">仓位</th><th>理由</th></tr>{plan_rows}</table></div>'
            if plan_rows else '<p class="note">还没有本周末的打算。Agent 团队每天 6:30 开会，周日锁定前 2 小时再开一次。</p>')
    pushed = req.query_params.get("msg", "")
    from . import autotrade
    at_cfg, at_real = autotrade.get(S), autotrade.realized_total(S)
    body = f"""
<section class="hero">
  <div class="card countdown">
    <div class="k"><span class="dot {'' if fresh else 'off'}"></span>{'数据正常流入' if fresh else '数据可能中断'} · {len(st['all'])}/{len(S.instruments)} 个合约在线 · 今天读了 {st['news_today']} 条新闻</div>
    <div class="big">{cd}</div>
    <div class="mut">{E(cd_label)}</div>
    <ol class="steps">{step_html}</ol>
    <div class="ev">时间都是加州时间。三星（SMSN）跟韩国市场：周五凌晨收盘，首尔周一开盘时对答案。</div>
  </div>
  <div class="card">
    <div class="k">下一个期权事件</div>
    <div class="v s">{E(nxt[0].event_type + ' · ' + nxt[0].event_at.astimezone(PT).strftime('%m-%d %H:%M')) if nxt else '—'}</div>
    <div class="ev">{' · '.join(E(e.event_type) + ' ' + e.event_at.astimezone(PT).strftime('%m-%d') for e in nxt[1:])}</div>
    <div class="k" style="margin-top:14px">新闻自动读取</div>
    <div class="v s {'good' if st['llm_key'] else 'bad'}">{'已开启' if st['llm_key'] else '未开启'}</div>
    {'' if st['llm_key'] else '<div class="ev"><a href="/settings">去设置里填 API key</a></div>'}
    <div class="k" style="margin-top:14px">自动下单</div>
    <div class="v s {'good' if at_cfg['mode'] == 'live' else ''}">{ {'off': '关闭，只做模拟', 'shadow': '演习中（不真下）', 'live': '真下单中'}[at_cfg['mode']] }</div>
    <div class="ev">{'本金上限 $' + format(at_cfg['capital_usd'], '.0f') + ' · 累计真实盈亏 $' + format(at_real, '+.2f') + ' · ' if at_cfg['mode'] != 'off' else ''}<a href="/settings">设置</a></div>
  </div>
</section>

<h2>成绩</h2>
<div class="strip">
  <div><div class="k">实时模拟盈亏（$2,000 本金，2 倍）</div><div class="v {_cls(live['total_usd'])}">${_n(live['total_usd'], 2, sign=True)}</div>
       <div class="ev">{live['n']} 个合约周末，出手 {live['n_traded']} 次</div></div>
  <div><div class="k">预测准不准（误差越小越准）</div><div class="v s">模型 {_n(card.get('mean_err_model'))} · 猜周五价 {_n(card.get('mean_err_A'))} · 猜链上价 {_n(card.get('mean_err_B'))}</div>
       <div class="ev">单位万分点；模型要同时赢另外两个才算有用。已有 {card.get('n_official', 0)} 个样本，满 30 个周末下结论</div></div>
  <div><div class="k">历史回测（过去 {hist['n_weekends']} 个周末，同样本金）</div><div class="v {_cls(hist['total_usd'])}">${_n(hist['total_usd'], 2, sign=True)}</div>
       <div class="ev">胜率 {_n(hist['win_rate'] and hist['win_rate']*100, 0, '%')} · 只供参考，会偏乐观</div></div>
</div>
<p class="sub" style="margin-top:10px">{E(card.get('verdict', '还没有完成的实时周末，第一个周末结束后这里会出现成绩。'))} 期权：{E(oc['verdict'])}</p>

<h2>本周末打算（纸面练习）</h2>
{plan_head}{plan}

<h2>7 个合约</h2>
<div class="tw"><table><tr><th>合约</th><th class="n">最新价</th><th>锁定时间</th><th class="n">实时样本</th><th class="n">实时模拟</th><th class="n">历史回测</th></tr>{inst_rows}</table></div>

<h2>报告</h2>
<div class="actions"><a class="btn" href="/report.pdf">下载 PDF 报告</a>
<form method="post" action="/push-pdf" style="margin:0"><button class="ghost" type="submit">推送到手机</button></form>
<span class="ev">每天早上 7 点会自动推一份</span></div>
{('<p class="note">' + E(pushed) + '</p>') if pushed else ''}"""
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
    manual = """<div class="card" style="margin:16px 0">
<h2 style="margin-top:0">如果你自己手动跟着做（真钱），一个周末的流程</h2>
<p class="sub">实盘下单功能还锁着，程序不会替你下单。下面是你自己照着推送手动操作的步骤。先看清楚：现在还没有一个真实周末的成绩，方法有没有用还不知道。</p>
<ol class="steps" style="grid-template-columns:repeat(4,1fr)">
<li><span class="n">1</span><b>周五前</b><span class="t">把钱放进交易账户</span><span class="d">在 Hyperliquid（trade.xyz）账户里放入 USDC。只放亏光也不心疼的钱。</span></li>
<li><span class="n">2</span><b>周末</b><span class="t">什么都不用做</span><span class="d">程序读价格和新闻。周日锁定前 2 小时 agent 开会，手机收到"本周末打算"。</span></li>
<li><span class="n">3</span><b>周日 14:45 / 16:45</b><span class="t">收到锁定推送后下单</span><span class="d">按推送里每个合约的方向挂限价单，仓位按推送比例；推送写"不做"的不碰。</span></li>
<li><span class="n">4</span><b>周日 15:05 / 17:05</b><span class="t">开盘 5 分钟后平仓</span><span class="d">全部平掉，盈亏当场到账户，随时可以提走。不是周一才结算。</span></li>
</ol></div>"""
    body = f"""<h1>如果放真钱，会赚多少或亏多少</h1>{manual}
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


def _conf(c):
    try:
        v = max(0, min(100, int(c)))
    except (TypeError, ValueError):
        return ""
    return f'<div class="k">把握 {v}%</div><div class="bar"><i style="width:{v}%"></i></div>'


def _cites(cs):
    return "".join(f'<a class="cite" href="#{E(str(c))}">[{E(str(c))}]</a>' for c in (cs or []) if isinstance(c, str))


def _say(o: dict) -> str:
    a = agents.BY_ID.get(o["agent"], {"name": o["agent"]})
    out = o.get("out", {})
    pts = "".join(f"<li>{E(str(p.get('claim', '')))}{_cites(p.get('cite'))}</li>" for p in (out.get("points") or []) if isinstance(p, dict))
    extra = ""
    if out.get("rebut"):
        extra += f'<div class="ev">回应：{E(str(out["rebut"]))}{" · 改了主意" if out.get("changed") else ""}</div>'
    for pr in out.get("proposal") or []:
        if isinstance(pr, dict):
            extra += f'<span class="tag">{E(str(pr.get("name")))} {E(str(pr.get("lean")))} {E(str(pr.get("size")))}</span>'
    for bt in out.get("backtest") or []:
        if isinstance(bt, dict):
            good = "样本外显著" in bt.get("text", "")
            extra += f'<div class="note" style="margin:6px 0">代码回测：{E(bt.get("text", ""))}{" ← 过门槛" if good else ""}</div>'
    for v in out.get("veto") or []:
        if isinstance(v, dict):
            extra += f'<span class="tag bad">否决 {E(str(v.get("name")))}：{E(str(v.get("why")))}</span>'
    return (f'<div class="say"><p class="who">{E(a["name"])} <span class="mut">{E(o["agent"])}</span>'
            f'{" · 把握 " + E(str(out.get("confidence"))) + "%" if out.get("confidence") is not None else ""}</p>'
            f'<div>{E(str(out.get("plain", "")))}</div>'
            f'{"<div class=ev>立场：" + E(str(out.get("stance"))) + "</div>" if out.get("stance") else ""}'
            f'{"<ul class=ev>" + pts + "</ul>" if pts else ""}{extra}</div>')


def _arena_html() -> str:
    from . import arena
    try:
        st = arena.standings(S, agents.AGENTS)
        book = arena.governed_book(S)
        reviews = arena.state(S).get("reviews", [])[-3:]
        recent = arena.scores(S)[-25:][::-1]
    except Exception as e:  # noqa: BLE001
        return f'<p class="note">赛马数据读取失败：{E(str(e))}</p>'
    names = {k: v["name"] for k, v in st["table"].items()}
    rows = ""
    for race, label in arena.RACES.items():
        mem = sorted([(k, t) for k, t in st["table"].items() if t["room"] == race], key=lambda x: -x[1]["score30"])
        for i, (k, t) in enumerate(mem):
            badge = ("🏆 冠军" if t["champion"] else "") + (" ⏸ 暂停" if t["paused"] else "")
            last = t["last"][0] if t["last"] else None
            last_txt = f"{last['points']:+g} {last['reason']}" if last else "还没有记录"
            pnl = book["by_agent"].get(k)
            pnl_txt = f"{pnl['pnl_bps']:+.1f}（{pnl['n']} 笔）" if pnl else ""
            rows += (f'<tr><td>{label if i == 0 else ""}</td><td><b>{E(t["name"])}</b> <span class="mut">第 {t["gen"]} 代</span></td>'
                     f'<td class="{_cls(t["score30"])}">{t["score30"]:+.1f}</td><td>{t["n_outcome"]}</td><td>{badge}</td>'
                     f'<td>{pnl_txt}</td><td class="ev">{E(last_txt)}</td></tr>')
    log_rows = "".join(f'<tr><td>{datetime.fromtimestamp(x["ts"], PT):%m-%d %H:%M}</td><td>{E(names.get(x["agent"], x["agent"]))}</td>'
                       f'<td class="{_cls(x["points"])}">{x["points"]:+g}</td><td class="ev">{E(x["reason"])}</td></tr>' for x in recent)
    rev = "".join(f'<li>{datetime.fromtimestamp(r["ts"], PT):%m-%d}：{E("；".join(r["events"]) or "无人淘汰")}</li>' for r in reviews)
    empty = "<tr><td colspan=4>还没有</td></tr>"
    rev_html = f"<div class=k>周评</div><ul>{rev}</ul>" if rev else ""
    return f"""<h2>赛马排行榜（30 天积分）</h2>
<div class="tw"><table><tr><th>岗位</th><th>agent</th><th>积分</th><th>已结算</th><th>状态</th><th>影子模拟账（万分点）</th><th>最近一次奖惩</th></tr>{rows}</table></div>
<p class="sub">正式模拟账（每次决策时听冠军的）：{book["governed"]:+.1f} 万分点，{book["n"]} 笔。纪律分每场会后立即结算；期权预测约 2 天后结算；交易在周末开盘后结算；每 7 天评一次淘汰。</p>
<details><summary>最近 25 条奖惩记录</summary><div class="tw"><table><tr><th>时间</th><th>agent</th><th>分</th><th>原因</th></tr>{log_rows or empty}</table></div>
{rev_html}</details>"""


def _chips(room: str) -> str:
    return "".join('<span class="tag" title="' + E(a["stance"]) + '">' + E(a["name"]) + "</span>"
                   for a in agents.AGENTS if a["room"] == room)


@app.get("/agents", response_class=HTMLResponse)
def agents_page(req: Request, run: str | None = None):
    if (r := _guard(req)):
        return r
    st = agents.get_state(S)
    sp = agents.spend(S)
    rec = agents.load_run(S, run)
    runs = agents.list_runs(S, 12)
    head = AGENTS_JS
    status = (f'<span class="good" id="meet-stage">正在开会：{E(st.get("stage", ""))}</span>（开完会页面会自动更新，展开的内容不会被收起）' if st.get("running")
              else f"上次开会：{E(rec['run_id'][5:10] + ' ' + rec['run_id'][11:13] + ':' + rec['run_id'][13:15]) if rec else '还没开过'}")
    key_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    body = f"""<h1>Agent 讨论室</h1>
<p class="sub">18 个起步、人数会自动变化的 AI 团队。交易、期权、算法三个岗位各有 3 个 agent 赛马：代码按结果自动打分，第一名当冠军（决定进正式模拟账、模型升级、发言优先），连续垫底的被淘汰、由冠军打法改写出的新一代顶替。风控、选品种、方法、审计负责把关。纪律写死在代码里，agent 改不了；奖惩全自动，人不干预。每天 6:30（加州时间）和周末决策前 2 小时开会。</p>
<div class="grid">
<div class="card"><div class="k">状态</div><div class="v s">{status}</div></div>
<div class="card"><div class="k">今天 / 本月费用（上限）</div><div class="v s">${sp['day']:.2f} / ${sp['month']:.2f}</div><div class="k">上限 ${sp['cap_day']:.2f}/天，${sp['cap_month']:.0f}/月</div></div>
<div class="card"><div class="k">API key</div><div class="v s {'good' if key_ok else 'bad'}">{'已填' if key_ok else '未填：只能做代码审计'}</div><div class="k"><a href="/settings">去设置</a></div></div>
<div class="card"><div class="k">手动开会</div><form method="post" action="/agents/run"><button {'disabled' if st.get('running') else ''}>现在开会</button></form><div class="k">约 5–8 分钟，约 $1.2</div></div>
</div>
{('<p class="note">' + E(req.query_params.get('msg', '')) + '</p>') if req.query_params.get('msg') else ''}
{_arena_html()}
<div class="daynav" style="margin-top:12px"><span class="k">历次会议：</span>{''.join(f'<a class="tag" href="/agents?run={E(p.stem)}">{E(p.stem[5:10] + " " + p.stem[11:13] + ":" + p.stem[13:15])}</a>' for p in runs)}</div>"""
    roster_cards = "".join(
        f'<div class="card"><div class="k">{E(m["name"])}（{sum(1 for a in agents.AGENTS if a["room"] == m["key"])} 个）</div>'
        f'<div class="roster">{_chips(m["key"])}</div>'
        f'<div class="ev">{E(m["goal"])}</div></div>' for m in agents.ROOMS)
    roster_cards += '<div class="card"><div class="k">总结（2 个）</div><div class="roster"><span class="tag">主席</span><span class="tag">白话编辑</span></div><div class="ev">主席汇总各室结论和分歧；白话编辑写手机推送。</div></div>'
    if not rec:
        body += f'<h2>团队名单</h2><div class="grid">{roster_cards}</div><p class="note">还没开过会。点"现在开会"，或等明早 6:30 自动开。</p>'
        return page(req, "/agents", "Agent 讨论室", body, head)
    if rec.get("degraded_reason"):
        body += f'<p class="note"><b>{"没开会" if rec["mode"] == "degraded" else ("会议失败" if rec["mode"] == "failed" else "会议中途停止")}：</b>{E(rec["degraded_reason"])}</p>'
    ch = rec.get("chair")
    body += (f'<p style="margin:14px 0"><a class="btn" href="/agents/{E(rec["run_id"])}.pdf">导出这次会议 PDF</a> '
             f'<span class="sub">（{E(rec["run_id"])}：主席结论、每个讨论室的结论和完整发言、代码审计、赛马积分、资料包）</span></p>')
    if ch:
        body += (f'<h2>主席结论 <span class="mut" style="font-size:13px">{E(rec["run_id"])} · {E(rec.get("reason", ""))} · '
                 f'${rec.get("cost_usd", 0):.2f} · {rec.get("calls", 0)} 次发言</span></h2>'
                 f'<div class="card"><div class="big">{E(str(ch.get("headline", "")))}</div><div>{E(str(ch.get("plain", "")))}</div>{_conf(ch.get("confidence"))}'
                 + ("<div class=k>今天/本周注意</div><ul>" + "".join(f"<li>{E(str(t))}</li>" for t in ch.get("today") or []) + "</ul>" if ch.get("today") else "")
                 + ("<div class=k>各室分歧</div><ul>" + "".join(f"<li>{E(str(t))}</li>" for t in ch.get("disagree") or []) + "</ul>" if ch.get("disagree") else "")
                 + (f'<div class="note">手机推送：{E(str(rec.get("editor", {}).get("push", "")))}</div>' if rec.get("editor", {}).get("push") else "")
                 + "</div>")
    ck = rec.get("checks", {})
    names = {"clock": "时间", "cite": "引用与数字", "rules": "规则与实盘锁", "plain": "大白话"}
    body += '<h2>代码审计</h2><div class="grid">' + "".join(
        f'<div class="card"><div class="k">{names.get(k, k)}</div><div class="v s {"ok" if v.get("ok") else "no"}">{"通过" if v.get("ok") else "有问题"}</div><div class="ev">{E(str(v.get("text", "")))}</div></div>'
        for k, v in ck.items()) + "</div>"
    for m in agents.ROOMS:
        room = rec.get("rooms", {}).get(m["key"])
        members = [a for a in agents.AGENTS if a["room"] == m["key"]]
        chips = "".join(f'<span class="tag">{E(a["name"])}</span>' for a in members)
        if not room:
            continue
        sy = room.get("synth", {})
        dis = "".join(f'<li><b>{E(agents.BY_ID.get(str(d.get("who")), {"name": str(d.get("who"))})["name"])}</b>：{E(str(d.get("view", "")))}</li>'
                      for d in sy.get("dissent") or [] if isinstance(d, dict))
        acts = "".join(f"<li>{E(str(a))}</li>" for a in sy.get("actions") or [])
        prop = ""
        if sy.get("proposal"):
            prop = ('<div class="tw"><table><tr><th>合约</th><th>倾向</th><th>仓位</th><th>理由</th></tr>' + "".join(
                f'<tr><td>{E(str(p.get("name")))}</td><td>{ {"fade": "反向押回撤", "follow": "顺着偏离", "skip": "不做"}.get(str(p.get("lean")), E(str(p.get("lean")))) }</td>'
                f'<td>{E(str(p.get("size")))}</td><td>{E(str(p.get("why", "")))}</td></tr>' for p in sy["proposal"] if isinstance(p, dict)) + "</table></div>"
                '<p class="ev">纸面提案，不下单、不改变正式预测规则。</p>')
        if m["key"] == "algo":
            prop += (f'<div class="k" style="margin-top:8px">最好的方案：{E(str(sy.get("best", "—")))} · '
                     f'{"<b class=good>赢了现行规则</b>" if sy.get("beats_rule") is True else "没有赢现行规则"}</div>')
        if sy.get("veto"):
            prop += "<div class=k>否决</div><ul>" + "".join(f'<li class="bad">否决 {E(str(v.get("name")))}：{E(str(v.get("why", "")))}</li>' for v in sy["veto"] if isinstance(v, dict)) + "</ul>"
        rounds = "".join(f"<h3 style='font-size:14px;margin:12px 0 0'>第 {i + 1} 轮</h3>" + "".join(_say(o) for o in rr)
                         for i, rr in enumerate(room.get("rounds", [])))
        body += (f'<div class="card room"><h2>{E(m["name"])}</h2><div class="roster">{chips}</div>'
                 f'<div class="big">{E(str(sy.get("plain", "")))}</div>{_conf(sy.get("confidence"))}'
                 f'{"<div class=ev>共识：" + E(str(sy.get("consensus"))) + "</div>" if sy.get("consensus") else ""}'
                 f'{"<div class=k style=margin-top:8px>不同意见</div><ul>" + dis + "</ul>" if dis else ""}'
                 f'{"<div class=k>接下来</div><ul>" + acts + "</ul>" if acts else ""}{prop}'
                 f'<details><summary>看完整讨论（{len(members)} 个成员 × {len(room.get("rounds", []))} 轮）</summary>{rounds}</details></div>')
    if not rec.get("rooms"):
        body += f'<h2>团队名单</h2><div class="grid">{roster_cards}</div>'
    facts = "".join(f'<div class="fact" id="{E(f["id"])}"><b>{E(f["id"])}</b> <span class="tag">{E(f["topic"])}</span>'
                    f'{"<span class=tag>外部文本</span>" if f.get("untrusted") else ""}{E(f["text"])}</div>' for f in rec.get("facts", []))
    body += f'<h2>资料包（{len(rec.get("facts", []))} 条，全部由代码生成）</h2><div class="card">{facts}</div>'
    return page(req, "/agents", "Agent 讨论室", body, head)


AGENTS_JS = """<script>
(function(){
  // 记住哪些"看完整讨论"是展开的（每次会议单独记）
  function key(){ return 'open:' + location.pathname + location.search; }
  document.addEventListener('DOMContentLoaded', function(){
    var ds = document.querySelectorAll('details'); var saved = [];
    try { saved = JSON.parse(localStorage.getItem(key()) || '[]'); } catch(e) {}
    ds.forEach(function(d, i){ if (saved.indexOf(i) >= 0) d.open = true;
      d.addEventListener('toggle', function(){ var o = [];
        document.querySelectorAll('details').forEach(function(x, j){ if (x.open) o.push(j); });
        try { localStorage.setItem(key(), JSON.stringify(o)); } catch(e) {} }); });
    // 开会时只轮询状态，不整页刷新；开完会再刷新一次
    var el = document.getElementById('meet-stage'); if (!el) return;
    var t = setInterval(function(){
      fetch('/agents/state', {credentials: 'same-origin'}).then(function(r){ return r.json(); }).then(function(s){
        if (s.running) { el.textContent = '正在开会：' + (s.stage || ''); }
        else { clearInterval(t); location.href = '/agents'; }
      }).catch(function(){});
    }, 15000);
  });
})();
</script>"""


@app.get("/agents/state")
def agents_state(req: Request):
    if not _user(req):
        return Response("{}", status_code=401, media_type="application/json")
    st = agents.get_state(S)
    return {"running": bool(st.get("running")), "stage": st.get("stage", ""), "run": st.get("run", "")}


@app.get("/agents/{rid}.pdf")
def agents_pdf(req: Request, rid: str):
    if (r := _guard(req)):
        return r
    rec = agents.load_run(S, rid)
    if not rec:
        return Response("没有这次会议", status_code=404)
    data = pdfreport.meeting_pdf(S, rec)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="agent-meeting-{rid}.pdf"'})


_last_manual = [0.0]


@app.post("/agents/run")
def agents_run(req: Request):
    if (r := _guard(req)):
        return r
    origin = req.headers.get("origin")
    if origin and req.headers.get("host") and origin.split("//")[-1] != req.headers["host"]:
        return Response("bad origin", status_code=403)
    cool = agents.cfg(S)["manual_cooldown_min"] * 60
    if agents.get_state(S).get("running"):
        msg = "已经在开会了"
    elif time.time() - _last_manual[0] < cool:
        msg = f"刚开过，{int((cool - (time.time() - _last_manual[0])) / 60) + 1} 分钟后才能再开"
    else:
        _last_manual[0] = time.time()
        agents.set_state(S, running=True, stage="准备中")
        import threading
        threading.Thread(target=agents.run_meeting, args=(S, f"{_user(req)} 手动开会"), daemon=True).start()
        msg = "已开始开会，约 3–5 分钟。页面会自动刷新。"
    return RedirectResponse("/agents?" + urlencode({"msg": msg}), status_code=303)


# ------------------------------------------------------------------ settings: paste the Anthropic key in the browser
def _mask(k: str) -> str:
    return f"{k[:7]}…{k[-4:]}" if k and len(k) > 15 else ("（未填）" if not k else "（格式不对）")


def check_anthropic_key(key: str) -> tuple[bool, str]:
    """Try one tiny call. Returns (ok, message in plain Chinese)."""
    try:
        import anthropic
        c = anthropic.Anthropic(api_key=key)
        c.messages.create(model=S["llm"]["model"], max_tokens=1, messages=[{"role": "user", "content": "hi"}])
        return True, "测试通过：这个 key 能用"
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        if "Authentication" in name or "401" in str(e):
            return False, "这个 key 不对（Anthropic 不认）。请确认复制的是 sk-ant- 开头的整串，而不是 key 的名字。"
        if "PermissionDenied" in name or "credit" in str(e).lower() or "billing" in str(e).lower():
            return False, "key 是对的，但账户里没有余额：去 Billing 充值后再试。"
        return False, f"测试没通过：{name}: {str(e)[:160]}"


def save_env_key(name: str, value: str) -> None:
    p = Path(S.root) / ".env"
    lines = p.read_text().splitlines() if p.exists() else []
    out, done = [], False
    for ln in lines:
        if ln.startswith(f"{name}="):
            out.append(f"{name}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{name}={value}")
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    os.environ[name] = value


@app.get("/settings", response_class=HTMLResponse)
def settings_page(req: Request):
    if (r := _guard(req)):
        return r
    cur = os.environ.get("ANTHROPIC_API_KEY", "")
    msg = req.query_params.get("msg", "")
    body = f"""<h1>设置</h1>
{('<p class="note">' + E(msg) + '</p>') if msg else ''}
<div class="card"><h2 style="margin-top:0">Anthropic API key</h2>
<p>现在：<b class="{'good' if cur else 'bad'}">{E(_mask(cur))}</b>。新闻自动抽取和 Agent 开会都要用它。</p>
<ol class="sub" style="color:var(--ink)">
<li>新开一个网页，打开 <b>platform.claude.com</b>（也就是 console.anthropic.com），登录。</li>
<li>左边菜单找 <b>API keys</b>，点 <b>Create key</b>，名字随便填（比如 weekend-desk），点创建。</li>
<li>弹出来一串很长的、<b>sk-ant-</b> 开头的字符，点旁边的复制按钮。<b>它只显示这一次</b>。</li>
<li>回到这里，粘贴到下面的框里，点"保存并测试"。</li>
</ol>
<form method="post" action="/settings/key" style="display:grid;gap:10px;max-width:560px">
<label>粘贴 key（以 sk-ant- 开头）<input name="key" type="password" autocomplete="off" required placeholder="sk-ant-api03-..."></label>
<button type="submit">保存并测试</button></form>
<p class="sub">保存前会先用这个 key 试一次（花费不到 0.01 美分），不对就不保存。key 只存在服务器上，网页上只显示开头和最后 4 位。</p></div>"""
    body += _autotrade_html()
    return page(req, "/settings", "设置", body)


def _autotrade_html() -> str:
    from . import autotrade
    cfg = autotrade.get(S)
    addr, key = autotrade.creds()
    has = autotrade.valid_address(addr) and autotrade.valid_key(key)
    label = {"off": "关闭", "shadow": "演习（算好单子、推送给你看，但不真下）", "live": "真下单"}[cfg["mode"]]
    summ = autotrade.summary(S)
    rows = ""
    for wid, k in sorted(summ["weekends"].items(), reverse=True)[:20]:
        e, x = k.get("entry", {}), k.get("exit", {})
        what = e.get("skipped") or (f'{e.get("side", "")} 约 ${e.get("notional_usd", 0):.0f}' + ("（演习）" if not e.get("sent") else ""))
        pnl = x.get("pnl_usd")
        rows += (f'<tr><td>{E(wid)}</td><td>{E(what)}</td><td class="n {_cls(pnl)}">'
                 f'{"—" if pnl is None else f"${pnl:+.2f}"}</td><td class="ev">{E(x.get("error") or e.get("error") or "")}</td></tr>')
    sel = lambda m: "checked" if cfg["mode"] == m else ""  # noqa: E731
    return f"""
<div class="card" style="margin-top:18px"><h2 style="margin-top:0">自动下单（真钱）</h2>
<p>现在：<b class="{'good' if cfg['mode'] == 'live' else ''}">{label}</b> · 本金上限 ${cfg['capital_usd']:.0f} · 最多亏 ${cfg['max_loss_usd']:.0f} 自动停 ·
累计真实盈亏 <b class="{_cls(summ['total_usd'])}">${summ['total_usd']:+.2f}</b></p>
<p class="sub">打开后，每个周末程序自己完成：锁定预测时挂单（只挂单、不吃单，杠杆 1 倍）→ 开盘前 1 分钟撤掉没成交的 → 开盘 5 分钟后平仓。
每一步都推送到手机。你不用碰交易所。这个方法还没有真实成绩，只放亏了也不心疼的钱。</p>

<h3>第一步：连接你的 Hyperliquid 账户{' <span class="good">（已连接）</span>' if has else ''}</h3>
<ol class="sub" style="color:var(--ink)">
<li>在 app.hyperliquid.xyz 登录你的钱包，确认账户里有 USDC。</li>
<li>点右上角 <b>More → API</b>，名字填 weekend-desk，点 <b>Generate</b>，再点 <b>Authorize API Wallet</b>（钱包里点确认）。有效期选最长。</li>
<li>页面会显示一串 <b>私钥</b>（0x 开头、66 位），点复制。这个"API 钱包"只能下单，<b>不能把钱提走</b>。</li>
<li>把你的<b>主钱包地址</b>（0x 开头、42 位，就是登录用的那个）和刚才的私钥填到下面。</li>
</ol>
<form method="post" action="/settings/wallet" style="display:grid;gap:10px;max-width:560px">
<label>主钱包地址<input name="address" autocomplete="off" placeholder="0x..." value="{E(addr) if autotrade.valid_address(addr) else ''}"></label>
<label>API 钱包私钥{'（已保存，只在要更换时填）' if has else ''}<input name="key" type="password" autocomplete="off" placeholder="0x..."></label>
<button type="submit">保存并检查连接</button></form>

<h3>第二步：选择模式</h3>
<form method="post" action="/settings/live" style="display:grid;gap:10px;max-width:560px">
<label style="flex-direction:row;gap:8px;color:var(--ink)"><input type="radio" name="mode" value="off" {sel('off')}> 关闭</label>
<label style="flex-direction:row;gap:8px;color:var(--ink)"><input type="radio" name="mode" value="shadow" {sel('shadow')}> 演习：到点推送"本来会下什么单"，不真下（建议先演习一个周末）</label>
<label style="flex-direction:row;gap:8px;color:var(--ink)"><input type="radio" name="mode" value="live" {sel('live')}> 真下单</label>
<div class="grid"><label>本金上限（美元，平均分给 7 个合约）<input name="capital" type="number" min="20" max="5000" step="10" value="{cfg['capital_usd']:.0f}"></label>
<label>最多亏多少就自动停（美元）<input name="max_loss" type="number" min="5" max="2000" step="5" value="{cfg['max_loss_usd']:.0f}"></label></div>
<label style="flex-direction:row;gap:8px;color:var(--ink)"><input type="checkbox" name="ack" value="1"> 我知道选"真下单"会用真钱，可能亏损；这个方法还没有真实成绩</label>
<button type="submit">保存模式</button></form>

<h3>记录</h3>
<div class="tw"><table><tr><th>周末</th><th>做了什么</th><th class="n">真实盈亏</th><th>备注</th></tr>{rows or '<tr><td colspan=4 class="mut">还没有记录</td></tr>'}</table></div>
</div>"""


@app.post("/settings/wallet")
def settings_wallet(req: Request, address: str = Form(""), key: str = Form("")):
    if (r := _guard(req)):
        return r
    origin = req.headers.get("origin")
    if origin and req.headers.get("host") and origin.split("//")[-1] != req.headers["host"]:
        return Response("bad origin", status_code=403)
    from . import autotrade
    address, key = address.strip(), key.strip()
    if not autotrade.valid_address(address):
        msg = "没保存：主钱包地址应该是 0x 开头、一共 42 位。"
    elif key and not autotrade.valid_key(key):
        msg = "没保存：私钥应该是 0x 开头、后面 64 位（0-9 和 a-f）。注意不要贴成钱包地址。"
    elif not key and not autotrade.valid_key(autotrade.creds()[1]):
        msg = "没保存：还没有私钥，请把 API 钱包私钥也填上。"
    else:
        save_env_key("HL_ACCOUNT_ADDRESS", address)
        if key:
            save_env_key("HL_API_PRIVATE_KEY", key if key.startswith("0x") else "0x" + key)
        try:
            b = autotrade.client().balance()
            msg = (f"已保存并连上。主账户 ${b['main']:.2f}，美股合约账户（xyz）${b['xyz']:.2f}。"
                   + ("" if b["xyz"] or b["main"] else " 账户里还没有钱。"))
        except Exception as e:  # noqa: BLE001
            msg = f"已保存，但检查连接失败：{type(e).__name__}: {str(e)[:160]}"
        import subprocess
        subprocess.Popen(["systemctl", "restart", "weekend-desk"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return RedirectResponse("/settings?" + urlencode({"msg": msg}), status_code=303)


@app.post("/settings/live")
def settings_live(req: Request, mode: str = Form("off"), capital: float = Form(200), max_loss: float = Form(50), ack: str = Form("")):
    if (r := _guard(req)):
        return r
    origin = req.headers.get("origin")
    if origin and req.headers.get("host") and origin.split("//")[-1] != req.headers["host"]:
        return Response("bad origin", status_code=403)
    from . import autotrade
    a, k = autotrade.creds()
    capital, max_loss = max(20.0, min(5000.0, capital)), max(5.0, min(2000.0, max_loss))
    if mode not in autotrade.MODES:
        mode = "off"
    if mode == "live" and not ack:
        msg = "没保存：选真下单要先勾选「我知道会用真钱」那一项。"
    elif mode == "live" and not (autotrade.valid_address(a) and autotrade.valid_key(k)):
        msg = "没保存：真下单要先完成第一步、连接 Hyperliquid 账户。演习模式不用连也能开。"
    else:
        autotrade.put(S, mode=mode, capital_usd=capital, max_loss_usd=max_loss, updated_by=_user(req) or "")
        msg = {"off": "已关闭自动下单。", "shadow": f"已切到演习：本周末会推送本来会下的单（本金 ${capital:.0f}），不真下。",
               "live": f"已打开真下单：本金上限 ${capital:.0f}，累计亏 ${max_loss:.0f} 自动停。每一步都会推送。"}[mode]
        notify.push(S, "自动下单设置已改", f"{_user(req)}：{msg}", priority="high")
    return RedirectResponse("/settings?" + urlencode({"msg": msg}), status_code=303)


@app.post("/settings/key")
def settings_key(req: Request, key: str = Form(...)):
    if (r := _guard(req)):
        return r
    origin = req.headers.get("origin")
    if origin and req.headers.get("host") and origin.split("//")[-1] != req.headers["host"]:
        return Response("bad origin", status_code=403)
    key = key.strip()
    if not key.startswith("sk-ant-"):
        msg = "没保存：key 必须以 sk-ant- 开头。你贴的可能是 key 的名字，请复制那串很长的字符。"
    else:
        ok, msg = check_anthropic_key(key)
        if ok:
            save_env_key("ANTHROPIC_API_KEY", key)
            import subprocess
            subprocess.Popen(["systemctl", "restart", "weekend-desk"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            msg = f"已保存（{_mask(key)}）。{msg}。后台程序已重启，新闻抽取开始工作；去「Agent 讨论室」点「现在开会」就能开第一场会。"
        else:
            msg = "没保存：" + msg
    return RedirectResponse("/settings?" + urlencode({"msg": msg}), status_code=303)


@app.get("/qa", response_class=HTMLResponse)
def qa_page(req: Request, run: str | None = None):
    if (r := _guard(req)):
        return r
    from . import qa
    rec = qa.load(S, run)
    runs = qa.list_runs(S, 12)
    _last_qa = globals().setdefault("_LAST_QA", [0.0])
    msg = req.query_params.get("msg", "")
    head = ""
    body = f"""<h1>测试与安全自检</h1>
<p class="sub">四个 AI 测试员定期给整个网站做体检，只测本站自己：界面流程、普通用户走一遍每个功能、红队技术攻击（越权/泄密/注入/暴力破解）、红队社工（文案会不会被钓鱼、会不会教坏习惯）。真正的判断由代码跑真实请求得出，AI 负责解读和补测试点。不碰真钱、不改规则。</p>
<div class="actions" style="margin:12px 0">
<form method="post" action="/qa/run" style="margin:0"><button type="submit">现在体检一次</button></form>
<span class="ev">约 1 分钟{'，带 AI 点评约 $0.1' if os.environ.get('ANTHROPIC_API_KEY') else '（没填 key 时只跑代码测试）'}</span></div>
{('<p class="note">' + E(msg) + '</p>') if msg else ''}"""
    if not rec:
        return page(req, "/qa", "测试", body + '<p class="note">还没体检过。点"现在体检一次"。</p>')
    n, nf = rec["n"], rec["n_fail"]
    nhigh = sum(1 for c in rec["checks"] if not c["ok"] and c["severity"] == "高")
    body += f"""<div class="grid">
<div class="card"><div class="k">总检查项</div><div class="v">{n}</div><div class="ev">{E(rec['run_id'][5:10] + ' ' + rec['run_id'][11:13] + ':' + rec['run_id'][13:15])} · {rec.get('secs', 0)} 秒</div></div>
<div class="card"><div class="k">通过</div><div class="v good">{n - nf}</div></div>
<div class="card"><div class="k">未通过</div><div class="v {'bad' if nf else 'good'}">{nf}</div>{'<div class="ev bad">其中高危 ' + str(nhigh) + ' 个</div>' if nf else ''}</div>
<div class="card"><div class="k">导出</div><div style="margin-top:6px"><a class="btn ghost" href="/qa/{E(rec['run_id'])}.pdf">体检报告 PDF</a></div></div>
</div>"""
    rv = rec.get("review", {})
    if rv:
        body += '<h2>AI 测试员结论</h2><div class="grid">'
        for a in qa.AGENTS:
            r_ = rv.get(a["id"], {})
            fnd = r_.get("findings") or []
            body += (f'<div class="card"><div class="k">{E(a["name"])}</div><div class="v s">{E(str(r_.get("verdict", "")))}</div>'
                     + ("".join(f'<div class="ev"><b>{E(str(f.get("severity")))}</b> {E(str(f.get("what")))} → {E(str(f.get("fix")))}</div>' for f in fnd[:4]) if fnd else '<div class="ev good">没发现问题</div>')
                     + '</div>')
        body += '</div>'
    # 分区列出
    areas = {}
    for c in rec["checks"]:
        areas.setdefault(c["area"], []).append(c)
    body += '<h2>逐项结果</h2>'
    for area, cs in areas.items():
        rows = "".join(f'<tr><td>{"✅" if c["ok"] else "❌"}</td><td>{E(c["name"])}</td>'
                       f'<td class="ev">{E(c["detail"])}{(" · <b class=bad>" + E(c["severity"]) + "危</b>") if not c["ok"] and c["severity"] else ""}</td></tr>' for c in cs)
        body += f'<h3 style="margin:16px 0 6px">{E(area)}（{sum(1 for c in cs if c["ok"])}/{len(cs)}）</h3><div class="tw"><table>{rows}</table></div>'
    body += f'<div class="daynav" style="margin-top:16px"><span class="k">历次：</span>{"".join(f"<a class=tag href=/qa?run={E(p.stem)}>{E(p.stem[5:10] + chr(32) + p.stem[11:13] + chr(58) + p.stem[13:15])}</a>" for p in runs)}</div>'
    return page(req, "/qa", "测试", body, head)


_last_qa_run = [0.0]


@app.post("/qa/run")
def qa_run(req: Request):
    if (r := _guard(req)):
        return r
    origin = req.headers.get("origin")
    if origin and req.headers.get("host") and origin.split("//")[-1] != req.headers["host"]:
        return Response("bad origin", status_code=403)
    import threading
    if time.time() - _last_qa_run[0] < 60:
        msg = "刚体检过，一分钟后再来"
    else:
        _last_qa_run[0] = time.time()
        from . import qa
        threading.Thread(target=qa.run, args=(S,), daemon=True).start()
        msg = "已开始体检，约 1 分钟后刷新查看。"
    return RedirectResponse("/qa?" + urlencode({"msg": msg}), status_code=303)


@app.get("/qa/{rid}.pdf")
def qa_pdf(req: Request, rid: str):
    if (r := _guard(req)):
        return r
    from . import qa
    rec = qa.load(S, rid)
    if not rec:
        return Response("没有这次体检", status_code=404)
    data = pdfreport.qa_pdf(S, rec)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="qa-{rid}.pdf"'})


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
