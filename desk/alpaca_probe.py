"""卡 A1：探测 Alpaca 期权历史数据够不够回测（只读，不下单，不碰账户钱）。

对每个标的：
- 已到期合约能不能查到（status=inactive），最早到期日是哪天 → 历史能回测多远；
- 抽一张约 1 年前到期、接近平值的合约，看日线 bars 从哪天开始、有几天；
- 同一张合约有没有历史买卖报价（quotes）；没有的话回测只能用成交价 + 点差估计。
结果打印成中文表，并写 data/reports/alpaca_probe.json。

用法（服务器）：cd /opt/weekend-desk && venv/bin/python -m desk.alpaca_probe
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from . import alpaca_exec as ax
from . import config

UNDERLYINGS = ["SOXX", "SMH", "QQQ", "SPY", "NVDA", "AMD", "MU", "INTC"]
DATA = ax.DATA_URL
OCC = re.compile(r"^[A-Z]{1,5}\d{6,7}[CP]\d{8}$")   # 标准合约代码；"1SPY…" 这种是拆股/分红调整过的，跳过


def _stock_close(c: ax.Client, sym: str, day: date) -> float | None:
    """标的在 day 之前最近一个交易日的收盘价（选"当时"的平值，不是现在的）。"""
    d = c._req("GET", f"{DATA}/v2/stocks/{sym}/bars",
               params={"timeframe": "1Day", "start": (day - timedelta(days=10)).isoformat(), "end": day.isoformat(),
                       "limit": 20, "adjustment": "raw", "feed": "iex"})
    b = d.get("bars") or []
    return float(b[-1]["c"]) if b else None


def _contracts(c: ax.Client, und: str, exp_gte: date, exp_lte: date, status: str) -> list[dict]:
    out, tok = [], None
    for _ in range(10):
        p = {"underlying_symbols": und, "expiration_date_gte": exp_gte.isoformat(),
             "expiration_date_lte": exp_lte.isoformat(), "status": status, "limit": 1000}
        if tok:
            p["page_token"] = tok
        d = c._req("GET", f"{c.base}/v2/options/contracts", params=p)
        out += d.get("option_contracts") or []
        tok = d.get("next_page_token")
        if not tok:
            break
    return out


def _bars(c: ax.Client, sym: str, start: date, end: date) -> list[dict]:
    d = c._req("GET", f"{DATA}/v1beta1/options/bars",
               params={"symbols": sym, "timeframe": "1Day", "start": start.isoformat(), "end": end.isoformat(), "limit": 10000})
    return (d.get("bars") or {}).get(sym) or []


def _quotes(c: ax.Client, sym: str, day: date) -> str:
    """历史报价接口：有就返回条数，没有 / 不支持就返回原因。"""
    try:
        d = c._req("GET", f"{DATA}/v1beta1/options/quotes",
                   params={"symbols": sym, "start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat(), "limit": 5})
        q = (d.get("quotes") or {}).get(sym) or []
        return f"有（样例 {len(q)} 条）" if q else "接口通但没数据"
    except Exception as e:  # noqa: BLE001
        return f"不支持：{str(e)[:80]}"


def probe(c: ax.Client, und: str, today: date) -> dict:
    r: dict = {"underlying": und}
    try:
        try:
            spot = c.latest_price(und)
        except Exception:  # noqa: BLE001
            spot = None
        old = _contracts(c, und, date(2023, 1, 1), today - timedelta(days=1), "inactive")
        exps = sorted({x["expiration_date"] for x in old})
        r["expired_contracts"] = len(old)
        r["earliest_expiry"] = exps[0] if exps else None
        # 抽样：约一年前到期、离现价最近（没有现价就取中位行权价）的看跌期权
        tgt = (today - timedelta(days=365)).isoformat()
        cand = [x for x in old if x.get("type") == "put" and OCC.match(x["symbol"])]
        if cand:
            exp = min({x["expiration_date"] for x in cand}, key=lambda e: abs(date.fromisoformat(e) - date.fromisoformat(tgt)))
            same = [x for x in cand if x["expiration_date"] == exp]
            ks = sorted(float(x["strike_price"]) for x in same)
            # 平值按到期前 30 天当时的标的价格选（按现在的价格选，一年前那张会是深度虚值、几乎没成交）
            try:
                ref = _stock_close(c, und, date.fromisoformat(exp) - timedelta(days=30))
            except Exception:  # noqa: BLE001
                ref = None
            ref = ref or ks[len(ks) // 2]
            r["sample_spot_then"] = ref
            pick = min(same, key=lambda x: abs(float(x["strike_price"]) - ref))
            sym = pick["symbol"]
            bars = _bars(c, sym, date.fromisoformat(exp) - timedelta(days=60), date.fromisoformat(exp))
            r["sample"] = sym
            r["sample_bars"] = len(bars)
            r["sample_first_bar"] = bars[0]["t"][:10] if bars else None
            r["sample_quotes"] = _quotes(c, sym, date.fromisoformat(bars[len(bars) // 2]["t"][:10]) if bars else date.fromisoformat(exp) - timedelta(days=10))
    except Exception as e:  # noqa: BLE001
        r["error"] = str(e)[:200]
    return r


def main(argv=None) -> int:
    s = config.load()
    kid, sec = ax.creds(True)
    paper = True
    if not (kid and sec):
        kid, sec = ax.creds(False)
        paper = False
    if not (kid and sec):
        print("没找到 Alpaca key：先在网站「设置 → 美股期权执行（Alpaca）」保存纸面账户 key。", file=sys.stderr)
        return 1
    c = ax.Client(kid, sec, paper=paper)
    today = date.today()
    rows = [probe(c, u, today) for u in UNDERLYINGS]
    print(f"Alpaca 期权历史数据探测（{'纸面' if paper else '真钱'}账户 key，只读）")
    print("标的 | 已到期合约数 | 最早到期日 | 抽样合约（到期前 30 天平值）| 到期前 60 天里有成交的天数 | 第一根日线")
    for r in rows:
        if "error" in r:
            print(f"{r['underlying']} | 出错：{r['error']}")
            continue
        print(f"{r['underlying']} | {r.get('expired_contracts')} | {r.get('earliest_expiry')} | {r.get('sample', '—')} | "
              f"{r.get('sample_bars', '—')} | {r.get('sample_first_bar', '—')}")
    q = next((r.get("sample_quotes") for r in rows if r.get("sample_quotes")), "—")
    print(f"历史买卖报价接口：{q}")
    out = Path(s.data) / "reports" / "alpaca_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"date": today.isoformat(), "paper": paper, "rows": rows}, ensure_ascii=False, indent=1))
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
