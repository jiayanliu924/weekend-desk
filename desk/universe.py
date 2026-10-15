"""品种扩展（#13）：发现 Hyperliquid HIP-3 上所有"底层市场会关门"的永续，分类到参考市场时间表，过流动性门槛，
按周末反向逻辑粗略回测并排名；`add` 把前 N 名写成 config.toml 的 [[market.instruments]] 段。

流程（见 knowledge/universe_README.md）：
1. 发现：info 接口 `perpDexs` 列出 HIP-3 dex，再对每个 dex 调 `metaAndAssetCtxs`（含 `meta`）拿市场元数据与当前 ctx。
   主 dex（加密永续）只数个数、不进候选：7×24 没有"休市→重开"结构，周末逻辑对它不成立。
2. 分类：规则表（按 dex / 市场命名）+ 手工覆盖文件 knowledge/universe_overrides.toml → 参考市场时间表
   （美股单票 24/5、美指/商品 23/5、韩股、日股、港股、欧股、外汇、金属能源）。认不出 → 标"待人工确认"，不自动加。
3. 流动性门槛（config [universe]）：24h 名义成交额、持仓量、impactPxs 点差；growthMode 只做备注（手续费按全额口径）。
4. 回测：最近 N 个周末，同一套反向逻辑（决策 = 重开前 15 分钟，平仓 = 重开后 5 分钟，全额手续费），
   输出周末数 / 命中率 / 平均净 bps / $1000 名义累计 / 最差一笔，以及和 XYZ100 周末收益的相关系数与"有效独立样本数"。
5. CLI：python3 -m desk.universe scan [--weekends 12] [--fixture raw.json]   → data/reports/universe.md + data/universe.json
         python3 -m desk.universe add --top 10 [--force]                   → 追加到 config.toml（幂等；只允许周一到周四）

沙盒连不上 api.hyperliquid.xyz（代理 403）：scan 会检测并打印"到服务器上跑"的说明；测试用确定性 fixture。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import backtest_lib as bl
from .backtest_lib import INTERVAL_MS, price_at_or_after, price_at_or_before, trade_stats
from .clock import last_completed_weekend, weekend_for_friday

UTC = timezone.utc
INFO_URL = "https://api.hyperliquid.xyz/info"
OVERRIDES_FILE = "knowledge/universe_overrides.toml"

RUN_ON_HOST_HINT = (
    "本机连不上 api.hyperliquid.xyz（代理 403）。到能访问 Hyperliquid 的机器（bot 服务器）上跑：\n"
    "    cd ~/weekend-desk && python3 -m desk.universe scan --weekends 12\n"
    "它会把发现结果存到 data/universe_raw.json、K 线缓存到 data/backfill/；把这两处拷回来，本机可以用\n"
    "    python3 -m desk.universe scan --fixture data/universe_raw.json\n"
    "离线重算排名。"
)

# ---------------------------------------------------------------- 参考市场时间表（周末：真市场关门 → 重开）
# 字段和 config.toml [[market.instruments]] 一一对应；来源见 knowledge/universe_README.md。
SCHEDULES: dict[str, dict] = {
    "us_equity":     {"label": "美股单票 24/5", "tz": "America/New_York", "close_day": "FRI", "close": "20:00", "resume_day": "SUN", "resume": "20:00"},
    "us_index":      {"label": "美指 23/5",     "tz": "America/New_York", "close_day": "FRI", "close": "17:00", "resume_day": "SUN", "resume": "18:00"},
    "metals_energy": {"label": "金属/能源 23/5", "tz": "America/New_York", "close_day": "FRI", "close": "17:00", "resume_day": "SUN", "resume": "18:00"},
    "fx":            {"label": "外汇",           "tz": "America/New_York", "close_day": "FRI", "close": "17:00", "resume_day": "SUN", "resume": "17:00"},
    "kr_equity":     {"label": "韩股 KRX",       "tz": "Asia/Seoul",       "close_day": "FRI", "close": "20:00", "resume_day": "MON", "resume": "09:01"},
    "jp_equity":     {"label": "日股 JPX",       "tz": "Asia/Tokyo",       "close_day": "FRI", "close": "15:30", "resume_day": "MON", "resume": "09:00"},
    "hk_equity":     {"label": "港股 HKEX",      "tz": "Asia/Hong_Kong",   "close_day": "FRI", "close": "16:00", "resume_day": "MON", "resume": "09:30"},
    "eu_equity":     {"label": "欧股 Xetra",     "tz": "Europe/Berlin",    "close_day": "FRI", "close": "17:30", "resume_day": "MON", "resume": "09:00"},
}
UNKNOWN = "unknown"
CRYPTO = "crypto"

# ---------------------------------------------------------------- 分类规则表（按市场命名；去掉 dex 前缀后大写比较）
# 顺序即优先级。认不出的一律 unknown → 报告里"待人工确认"，写进 overrides 才会进候选。
_SYMBOL_RULES: list[tuple[str, frozenset[str], str]] = [
    ("美指", frozenset({"XYZ100", "SP500", "NDX", "SPX", "NAS100", "US100", "US500", "US30", "DJI", "DJIA", "DOW",
                        "RUT", "RUSSELL2000", "US2000", "VIX"}), "us_index"),
    ("金属/能源", frozenset({"BRENTOIL", "BRENT", "WTI", "WTIOIL", "CL", "OIL", "CRUDE", "CRUDEOIL", "NATGAS", "NG", "GOLD", "XAU",
                            "XAUUSD", "SILVER", "XAG", "XAGUSD", "COPPER", "HG", "PLATINUM", "XPT", "PALLADIUM", "XPD",
                            "ALUMINUM", "ALUMINIUM", "URANIUM"}), "metals_energy"),
    ("韩股", frozenset({"SMSN", "SAMSUNG", "HYNIX", "SKHYNIX", "SKHY", "HYUNDAI", "KOSPI", "KOSPI200", "KR200", "KIA", "LGES",
                        "NAVER", "KAKAO"}), "kr_equity"),
    ("日股", frozenset({"TOYOTA", "SONY", "NINTENDO", "SOFTBANK", "SFTBY", "NIKKEI", "NIKKEI225", "JP225", "N225", "TOKYOELEC",
                        "TEL", "KEYENCE", "FASTRETAIL", "MITSUBISHIUFJ", "MUFG", "HITACHI", "ADVANTEST", "RECRUIT"}), "jp_equity"),
    ("港股", frozenset({"TENCENT", "ALIBABAHK", "BABAHK", "MEITUAN", "HSI", "HANGSENG", "HK50", "XIAOMI", "BYD", "AIA", "HSBC",
                        "JD-HK", "NETEASEHK", "SMIC", "PDDHK"}), "hk_equity"),
    ("欧股", frozenset({"ASML", "SAP", "LVMH", "SIEMENS", "NOVO", "NOVONORDISK", "DAX", "DE40", "GER40", "STOXX50", "EUROSTOXX",
                        "EU50", "AIRBUS", "NESTLE", "SHELL", "TOTALENERGIES", "HERMES", "SCHNEIDER", "ALLIANZ", "BMW",
                        "MERCEDES", "VOLKSWAGEN", "VW", "RHEINMETALL"}), "eu_equity"),
    ("美股单票", frozenset({"NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "GOOG", "META", "AVGO", "TSLA", "NFLX", "INTC", "AMD", "TSM",
                            "ORCL", "CRM", "ADBE", "QCOM", "TXN", "MU", "ARM", "SMCI", "PLTR", "COIN", "MSTR", "HOOD", "UBER",
                            "LLY", "UNH", "JNJ", "PFE", "MRK", "ABBV", "JPM", "BAC", "GS", "MS", "WFC", "V", "MA", "BRK",
                            "BRKB", "BRK.B", "BRK-B", "XOM", "CVX", "COP", "WMT", "COST", "HD", "PG", "KO", "PEP", "MCD",
                            "DIS", "NKE", "BA", "CAT", "GE", "HON", "IBM", "CSCO", "ANET", "PANW", "CRWD", "SNOW", "NOW",
                            "SHOP", "SQ", "PYPL", "ABNB", "RIVN", "LCID", "GME", "AMC", "SPY", "QQQ", "IWM", "DIA", "TLT",
                            "GLD", "SLV", "USO", "OPEN", "SOFI", "RKLB", "ASTS", "IONQ", "RGTI", "QBTS", "CRCL", "CORZ",
                            "APLD", "NBIS", "OKLO", "VRT", "DELL", "HPE", "MRVL", "LRCX", "AMAT", "KLAC",
                            "CRWV", "SNDK", "CBRS"}), "us_equity"),
]
_SUFFIX_RULES: list[tuple[str, str]] = [   # 命名里带交易所后缀/前缀
    (r"(\.KS|\.KQ|-KR|_KR|^KR:|^KRX:)$|^(KR|KRX)[:_-]", "kr_equity"),
    (r"(\.T|\.JP|-JP|_JP)$|^(JP|JPX|TSE)[:_-]", "jp_equity"),
    (r"(\.HK|-HK|_HK)$|^(HK|HKEX)[:_-]", "hk_equity"),
    (r"(\.DE|\.PA|\.AS|\.MI|-EU|_EU)$|^(EU|DE|XETRA)[:_-]", "eu_equity"),
    (r"(\.US|-US|_US)$|^(US|NYSE|NASDAQ)[:_-]", "us_equity"),
]
_CCY = {"USD", "EUR", "JPY", "GBP", "CHF", "AUD", "CAD", "NZD", "CNH", "CNY", "HKD", "SGD", "KRW", "MXN", "SEK", "NOK", "ZAR", "INR", "BRL", "TRY"}
_FX_RE = re.compile(r"^([A-Z]{3})[/_-]?([A-Z]{3})$")
_SCHED_LABEL = {k: v["label"] for k, v in SCHEDULES.items()}


def symbol_of(coin: str) -> str:
    return coin.split(":", 1)[1] if ":" in coin else coin


def classify(coin: str, dex: str, overrides: dict | None = None) -> tuple[str, str]:
    """→ (schedule_key | 'unknown', 判定依据)。覆盖文件优先：[markets] 指定时间表，[exclude] 排除，[dex_default] 按 dex 兜底。"""
    ov = overrides or {}
    if coin in ov.get("exclude", {}):
        return UNKNOWN, f"覆盖文件排除：{ov['exclude'][coin]}"
    if coin in ov.get("markets", {}):
        key = ov["markets"][coin]
        if key not in SCHEDULES:
            raise ValueError(f"{OVERRIDES_FILE}: {coin} 指向未知时间表 {key!r}；可选 {sorted(SCHEDULES)}")
        return key, "覆盖文件 [markets]"
    sym = symbol_of(coin).upper()
    for label, names, key in _SYMBOL_RULES:
        if sym in names:
            return key, f"命名规则：{label}表"
    for pat, key in _SUFFIX_RULES:
        if re.search(pat, sym):
            return key, "命名规则：交易所后缀/前缀"
    m = _FX_RE.match(sym)
    if m and m.group(1) in _CCY and m.group(2) in _CCY and m.group(1) != m.group(2):
        return "fx", "命名规则：货币对"
    dd = ov.get("dex_default", {}).get(dex)
    if dd:
        if dd not in SCHEDULES:
            raise ValueError(f"{OVERRIDES_FILE}: dex_default.{dex} 指向未知时间表 {dd!r}")
        return dd, f"覆盖文件 [dex_default] {dex}"
    return UNKNOWN, "认不出：待人工确认（写进 knowledge/universe_overrides.toml 才会进候选）"


def load_overrides(root: Path) -> dict:
    p = Path(root) / OVERRIDES_FILE
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text())


# ---------------------------------------------------------------- 发现
@dataclass
class Market:
    coin: str                 # 完整名，如 xyz:NVDA
    dex: str
    symbol: str
    sz_decimals: int | None = None
    max_leverage: float | None = None
    is_delisted: bool = False
    only_isolated: bool = False
    growth_mode: str | None = None
    oracle_px: float | None = None
    mark_px: float | None = None
    mid_px: float | None = None
    open_interest: float | None = None   # 张数（基础资产）
    oi_usd: float | None = None
    day_ntl_vlm: float | None = None
    funding: float | None = None
    impact_bid: float | None = None
    impact_ask: float | None = None
    spread_bps: float | None = None
    schedule: str = UNKNOWN
    why: str = ""
    gate_ok: bool = False
    gate_reasons: list[str] = field(default_factory=list)
    status: str = ""          # candidate / in_config / review / gate_fail / delisted / excluded
    backtest: dict | None = None


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _post(payload: dict, timeout: float = 30.0):
    import httpx
    r = httpx.post(INFO_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def discover(post=_post) -> dict:
    """拉全量原始数据：perpDexs + 每个 HIP-3 dex 的 metaAndAssetCtxs + 主 dex。原样存，分类在本地做。"""
    dexs = post({"type": "perpDexs"})
    raw = {"fetched_at": datetime.now(UTC).isoformat(), "perp_dexs": dexs, "dexs": {}, "main": None, "errors": {}}
    for d in dexs or []:
        if not d or not d.get("name"):
            continue
        name = d["name"]
        try:
            raw["dexs"][name] = post({"type": "metaAndAssetCtxs", "dex": name})
        except Exception as e:  # noqa: BLE001
            raw["errors"][name] = f"{type(e).__name__}: {e}"
    try:
        raw["main"] = post({"type": "metaAndAssetCtxs"})
    except Exception as e:  # noqa: BLE001
        raw["errors"][""] = f"{type(e).__name__}: {e}"
    return raw


def parse_markets(raw: dict) -> list[Market]:
    out = []
    for dex, mc in (raw.get("dexs") or {}).items():
        if not mc or len(mc) < 2:
            continue
        meta, ctxs = mc[0], mc[1]
        for i, u in enumerate(meta.get("universe", [])):
            c = ctxs[i] if i < len(ctxs) and isinstance(ctxs[i], dict) else {}
            coin = u["name"]
            mark = _f(c.get("markPx"))
            oi = _f(c.get("openInterest"))
            bid, ask = (None, None)
            ip = c.get("impactPxs")
            if isinstance(ip, (list, tuple)) and len(ip) == 2:
                bid, ask = _f(ip[0]), _f(ip[1])
            spread = (ask / bid - 1) * 1e4 if bid and ask and bid > 0 else None
            out.append(Market(coin=coin, dex=dex, symbol=symbol_of(coin), sz_decimals=u.get("szDecimals"),
                              max_leverage=_f(u.get("maxLeverage")), is_delisted=bool(u.get("isDelisted")),
                              only_isolated=bool(u.get("onlyIsolated")), growth_mode=u.get("growthMode"),
                              oracle_px=_f(c.get("oraclePx")), mark_px=mark, mid_px=_f(c.get("midPx")), open_interest=oi,
                              oi_usd=(oi * mark) if oi is not None and mark else None, day_ntl_vlm=_f(c.get("dayNtlVlm")),
                              funding=_f(c.get("funding")), impact_bid=bid, impact_ask=ask, spread_bps=spread))
    return out


def main_dex_count(raw: dict) -> int:
    m = raw.get("main")
    if not m or not m[0]:
        return 0
    return sum(1 for u in m[0].get("universe", []) if not u.get("isDelisted"))


# ---------------------------------------------------------------- 流动性门槛
DEFAULT_GATE = {"min_day_ntl_vlm_usd": 200_000.0, "min_oi_usd": 100_000.0, "max_spread_bps": 20.0, "weekends": 12}


def gate_cfg(settings) -> dict:
    g = dict(DEFAULT_GATE)
    g.update(settings.raw.get("universe", {}) if hasattr(settings, "raw") else settings.get("universe", {}))
    return g


def liquidity_gate(m: Market, g: dict) -> tuple[bool, list[str]]:
    """三道门：24h 名义成交额、持仓量（美元）、impactPxs 点差。缺数据按不过。"""
    why = []
    if m.day_ntl_vlm is None or m.day_ntl_vlm < g["min_day_ntl_vlm_usd"]:
        why.append(f"24h 成交额 {_usd(m.day_ntl_vlm)} < {_usd(g['min_day_ntl_vlm_usd'])}")
    if m.oi_usd is None or m.oi_usd < g["min_oi_usd"]:
        why.append(f"持仓 {_usd(m.oi_usd)} < {_usd(g['min_oi_usd'])}")
    if m.spread_bps is None or m.spread_bps > g["max_spread_bps"]:
        why.append(f"impact 点差 {('—' if m.spread_bps is None else f'{m.spread_bps:.1f}')} bps > {g['max_spread_bps']:.0f}")
    return (not why), why


def _usd(x) -> str:
    if x is None:
        return "—"
    return f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.0f}k"


# ---------------------------------------------------------------- config 段生成
def stanza_for(coin: str, schedule: str, name: str | None = None) -> dict:
    s = SCHEDULES[schedule]
    return {"name": name or symbol_of(coin), "coin": coin, "tz": s["tz"], "close_day": s["close_day"], "close": s["close"],
            "resume_day": s["resume_day"], "resume": s["resume"]}


def stanza_text(inst: dict, comment: str = "") -> str:
    L = ["[[market.instruments]]" + (f"   # {comment}" if comment else "")]
    for k in ("name", "coin", "tz", "close_day", "close", "resume_day", "resume"):
        L.append(f'{k} = "{inst[k]}"')
    return "\n".join(L) + "\n"


def append_stanzas(config_path: Path, insts: list[dict], comment_by_name: dict[str, str] | None = None) -> list[str]:
    """把新段插到 [clock] 之前（没有就追加到末尾）。已存在的 name/coin 跳过 → 幂等。返回实际加入的 name。"""
    text = config_path.read_text()
    existing = tomllib.loads(text)
    have = set()
    for i in existing.get("market", {}).get("instruments", []):
        have.add(i["name"])
        have.add(i["coin"])
    new = [i for i in insts if i["name"] not in have and i["coin"] not in have]
    if not new:
        return []
    block = "\n".join(stanza_text(i, (comment_by_name or {}).get(i["name"], "")) for i in new)
    m = re.search(r"^\[clock\]", text, flags=re.M)
    if m:
        text = text[:m.start()] + block + "\n" + text[m.start():]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    tomllib.loads(text)   # 写回前先确认还是合法 TOML
    config_path.write_text(text)
    return [i["name"] for i in new]


# ---------------------------------------------------------------- 周末回测（同一套反向逻辑）
def weekend_rows(settings, inst: dict, candles: list[dict], interval: str, n_weekends: int, now: datetime,
                 diag: dict | None = None) -> list[dict]:
    """最近 n 个已完成周末：锚 = 关门前最后一根 K 线，决策 = 重开 −15min，平仓 = 重开 +5min。

    diag（可选）收集"为什么这个周末没有样本"：K 线没覆盖到 / 缺关门价 / 缺决策价 / 缺平仓价。
    周末链上成交稀，决策价最多往前找 2 小时（15m = 8 根）；关门价、平仓价仍只找 4 根。
    """
    iv = INTERVAL_MS[interval]
    by_t = {int(c["t"]): c for c in candles}
    first_t = min(by_t) if by_t else None
    dec_back = max(4, (2 * 3_600_000) // iv)
    w = last_completed_weekend(now, settings, inst)
    rows = []
    d = diag if diag is not None else {}
    for k in ("before_candles", "no_close", "no_decision", "no_exit"):
        d.setdefault(k, 0)
    for _ in range(n_weekends):
        if first_t is None or bl.ms(w.close) < first_t:
            d["before_candles"] += 1
        else:
            p_close = price_at_or_before(by_t, iv, w.close)
            p_dec = price_at_or_before(by_t, iv, w.decision, max_back=dec_back)
            p_out = price_at_or_after(by_t, iv, w.exit)
            if all((p_close, p_dec, p_out)):
                rows.append({"name": inst["name"], "day": w.friday.isoformat(), "label": w.friday.isoformat(),
                             "t_decision": w.decision.isoformat(), "close_px": p_close, "dec_px": p_dec, "reopen_px": p_out,
                             "dev_bps": (p_dec / p_close - 1) * 1e4, "y_bps": (p_out / p_close - 1) * 1e4,
                             "move_from_dec_bps": (p_out / p_dec - 1) * 1e4})
            else:
                d["no_close" if not p_close else "no_decision" if not p_dec else "no_exit"] += 1
        w = weekend_for_friday(w.friday - timedelta(days=7), settings, inst)
    rows.reverse()
    return rows


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 4 or len(xs) != len(ys):
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def correlation_with(rows: list[dict], ref_rows: list[dict], key: str = "y_bps") -> tuple[float | None, int]:
    """同一周末（按周五日期对齐）的周末收益相关系数；返回 (ρ, 重叠周末数)。"""
    ref = {r["day"]: r[key] for r in ref_rows}
    pairs = [(r[key], ref[r["day"]]) for r in rows if r["day"] in ref]
    if not pairs:
        return None, 0
    return pearson([p[0] for p in pairs], [p[1] for p in pairs]), len(pairs)


def effective_samples(n_per_name: list[int], rhos: list[float | None]) -> float:
    """k 个同周末会一起动的名字，平均两两相关 ρ̄ 时有效独立样本 ≈ Σn / (1 + (k−1)·ρ̄)。
    ρ 用各自与 XYZ100 的相关系数近似（两两相关 ≈ ρ_i·ρ_j，取均值的平方近似）；拿不到的按 0。"""
    k = len(n_per_name)
    if k == 0:
        return 0.0
    rs = [abs(r) if r is not None else 0.0 for r in rhos]
    rbar = (statistics.mean(rs)) ** 2 if k > 1 else 0.0
    return sum(n_per_name) / (1 + (k - 1) * rbar)


def _default_get_candles(settings):
    """真网络路径：节流 + 退避（backtest_lib.info_post），并且 cache_max_age_h 小时内抓过的缓存直接用。"""
    age = float(gate_cfg(settings).get("cache_max_age_h", 6.0))
    return lambda st, coin, days: bl.get_candles(st, coin, days, max_cache_age_h=age)


def _why_empty(diag: dict, n_candles: int, interval: str, cs: list[dict]) -> str:
    if not n_candles:
        return "K 线为空"
    span = (f"{datetime.fromtimestamp(int(cs[0]['t']) / 1000, tz=UTC):%m-%d}→"
            f"{datetime.fromtimestamp(int(cs[-1]['t']) / 1000, tz=UTC):%m-%d}")
    lab ={"before_candles": "早于 K 线覆盖", "no_close": "缺关门价", "no_decision": "缺决策价", "no_exit": "缺平仓价"}
    parts = [f"{lab[k]} {diag[k]}" for k in lab if diag.get(k)]
    return f"有 {n_candles} 根 {interval} K 线（{span}），但没有一个周末取得到三个价：" + "，".join(parts)


def backtest_candidate(settings, inst: dict, n_weekends: int, now: datetime, notional: float, half_spread: float,
                       ref_rows: list[dict] | None, get_candles=None) -> dict:
    get_candles = get_candles or _default_get_candles(settings)
    days = n_weekends * 7 + 10
    try:
        cs, iv, src = get_candles(settings, inst["coin"], days)
    except Exception as e:  # noqa: BLE001
        return {"n": 0, "error": f"没有 K 线：{e}", "source": "失败", "rate_limited": bl.is_rate_limited(e)}
    diag: dict = {}
    rows = weekend_rows(settings, inst, cs, iv, n_weekends, now, diag)
    st = trade_stats(rows, settings, notional, mode="fade", half_spread_bps=half_spread)
    rho, overlap = correlation_with(rows, ref_rows or [])
    out = {"n": st["n"], "trades": st["trades"], "threshold_bps": st["threshold_bps"], "source": src,
           "revert_rate_all": st.get("revert_rate_all"), "hit_rate": st.get("hit_rate"), "win_rate": st.get("win_rate"),
           "avg_net_bps": st.get("avg_net_bps"), "total_net_usd": st.get("total_net_usd"),
           "worst_bps": st["worst"]["net_bps"] if st.get("worst") else None,
           "worst_day": st["worst"]["day"] if st.get("worst") else None,
           "p50_absdev": st.get("p50"), "rho_xyz100": rho, "rho_overlap": overlap, "missing": diag,
           "weekends": [{k: r[k] for k in ("day", "dev_bps", "y_bps", "move_from_dec_bps")} for r in rows]}
    if not rows:
        out["error"] = _why_empty(diag, len(cs), iv, cs)
    return out


# ---------------------------------------------------------------- 扫描主流程
def rank_key(m: Market) -> tuple:
    b = m.backtest or {}
    avg = b.get("avg_net_bps")
    return (0 if avg is not None else 1, -(avg or 0.0), -(b.get("total_net_usd") or 0.0), -(b.get("n") or 0), m.coin)


def scan(settings, raw: dict, n_weekends: int | None = None, now: datetime | None = None, notional: float = 1000.0,
         half_spread: float | None = None, get_candles=None, overrides: dict | None = None) -> dict:
    """发现 → 分类 → 门槛 → 回测 → 排名。返回可直接 json 化的结果。"""
    now = now or datetime.now(UTC)
    g = gate_cfg(settings)
    n_weekends = n_weekends or int(g["weekends"])
    half_spread = settings["costs"].get("half_spread_bps", 2.0) if half_spread is None else half_spread
    overrides = load_overrides(settings.root) if overrides is None else overrides
    in_cfg = {i["coin"]: i for i in settings.instruments} | {i["name"]: i for i in settings.instruments}
    markets = parse_markets(raw)
    # 参考：XYZ100（或第一个合约）的周末收益，用来算相关
    ref_inst = settings.inst("XYZ100") or settings.instruments[0]
    ref_bt = backtest_candidate(settings, ref_inst, n_weekends, now, notional, half_spread, None, get_candles)
    ref_rows = [{"day": w["day"], "y_bps": w["y_bps"]} for w in ref_bt.get("weekends", [])]
    for m in markets:
        m.schedule, m.why = classify(m.coin, m.dex, overrides)
        m.gate_ok, m.gate_reasons = liquidity_gate(m, g)
        if m.is_delisted:
            m.status = "delisted"
        elif m.coin in in_cfg or m.symbol in in_cfg:
            m.status = "in_config"
        elif m.coin in overrides.get("exclude", {}):
            m.status = "excluded"
        elif m.schedule == UNKNOWN:
            m.status = "review"
        elif not m.gate_ok:
            m.status = "gate_fail"
        else:
            m.status = "candidate"
        if m.status in ("candidate", "in_config", "review"):
            inst = in_cfg.get(m.coin) or in_cfg.get(m.symbol) or (stanza_for(m.coin, m.schedule) if m.schedule != UNKNOWN else None)
            if inst:
                m.backtest = backtest_candidate(settings, inst, n_weekends, now, notional, half_spread, ref_rows, get_candles)
    # 第二轮：第一轮被限速（429）的再试一次（info_post 已经退避过；这里再等一个完整窗口）
    retry = [m for m in markets if m.backtest and m.backtest.get("rate_limited")]
    if retry:
        bl.PACER.sleep(60.0)
        for m in retry:
            inst = in_cfg.get(m.coin) or in_cfg.get(m.symbol) or stanza_for(m.coin, m.schedule)
            m.backtest = backtest_candidate(settings, inst, n_weekends, now, notional, half_spread, ref_rows, get_candles)
    cands = sorted((m for m in markets if m.status == "candidate"), key=rank_key)
    n_eff = effective_samples([(m.backtest or {}).get("n", 0) for m in cands],
                              [(m.backtest or {}).get("rho_xyz100") for m in cands])
    return {"generated_at": now.isoformat(), "fetched_at": raw.get("fetched_at"), "weekends": n_weekends, "notional_usd": notional,
            "half_spread_bps": half_spread, "gate": g, "main_dex_markets": main_dex_count(raw),
            "dexs": sorted(raw.get("dexs", {}).keys()), "errors": raw.get("errors", {}),
            "ref": {"name": ref_inst["name"], "n": ref_bt.get("n", 0), "source": ref_bt.get("source")},
            "ranking": [m.coin for m in cands], "effective_samples": n_eff,
            "markets": [asdict(m) for m in sorted(markets, key=lambda x: (x.status != "candidate", rank_key(x)))]}


# ---------------------------------------------------------------- 报告
def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def _bps(x):
    return "—" if x is None else f"{x:+.1f}"


def report_md(res: dict) -> str:
    ms = res["markets"]
    by_status: dict[str, list[dict]] = {}
    for m in ms:
        by_status.setdefault(m["status"], []).append(m)
    g = res["gate"]
    L = [f"# 品种扩展扫描（HIP-3；最近 {res['weekends']} 个周末；回补 K 线粗略回测，不计入正式成绩）", "",
         f"生成 {res['generated_at'][:16]} UTC，发现数据抓于 {(res.get('fetched_at') or '—')[:16]}。dex：{', '.join(res['dexs']) or '—'}。"
         f"主 dex（加密永续）{res['main_dex_markets']} 个市场，**全部排除**：7×24 交易，没有“真市场关门 → 链上独自定价 → 重开”的结构，"
         "周末反向逻辑对它不成立。", "",
         f"门槛：24h 成交额 ≥ {_usd(g['min_day_ntl_vlm_usd'])}，持仓 ≥ {_usd(g['min_oi_usd'])}，impact 点差 ≤ {g['max_spread_bps']:.0f} bps。"
         f"手续费全额口径（growthMode 折扣不计），每笔名义 ${res['notional_usd']:.0f}，半点差 {res['half_spread_bps']:.0f} bps。", ""]
    if res.get("errors"):
        L += ["发现阶段出错：" + "；".join(f"{k or '主 dex'}: {v}" for k, v in res["errors"].items()), ""]
    bt_need = [m for m in ms if m["status"] in ("candidate", "in_config") and m.get("backtest") is not None]
    bt_ok = [m for m in bt_need if (m["backtest"] or {}).get("n")]
    L += [f"**回测覆盖：{len(bt_ok)} / {len(bt_need)} 个（候选 + 已在 config）取到了周末样本。**"
          + ("" if len(bt_ok) == len(bt_need) else " 没取到的原因见文末“回测失败 / 样本为 0”，不是“没信号”。"), ""]
    L += [f"## 候选排名（已过分类与门槛，按每笔平均净 bps；共 {len(by_status.get('candidate', []))} 个）", "",
          "| # | 市场 | 时间表 | 判定依据 | 24h 成交额 | 持仓 | 点差 bps | growth | 周末数 | 反转率(全部) | 出手 | 命中率 | 平均净 bps | 累计 USD | 最差一笔 | ρ(XYZ100) |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, m in enumerate(by_status.get("candidate", []), 1):
        b = m.get("backtest") or {}
        worst = "—" if b.get("worst_bps") is None else f"{b['worst_bps']:+.0f} ({b.get('worst_day')})"
        rho = "—" if b.get("rho_xyz100") is None else f"{b['rho_xyz100']:+.2f} (n={b.get('rho_overlap')})"
        L.append(f"| {i} | {m['coin']} | {_SCHED_LABEL[m['schedule']]} | {m['why']} | {_usd(m['day_ntl_vlm'])} | {_usd(m['oi_usd'])} | "
                 f"{'—' if m['spread_bps'] is None else f'{m['spread_bps']:.1f}'} | {m.get('growth_mode') or '—'} | {b.get('n', 0)} | "
                 f"{_pct(b.get('revert_rate_all'))} | {b.get('trades', 0)} | {_pct(b.get('hit_rate'))} | {_bps(b.get('avg_net_bps'))} | "
                 f"{'—' if b.get('total_net_usd') is None else f'{b['total_net_usd']:+.2f}'} | {worst} | {rho} |")
    if not by_status.get("candidate"):
        L.append("| — | （没有候选） | | | | | | | | | | | | | | |")
    k = len(by_status.get("candidate", []))
    L += ["", "### 相关性提醒", "",
          f"- 同一个周末里，美股单票 / 美指 / 美国商品会被同一个周一宏观事件同向推动，它们的周末收益不是独立样本。"
          f"上表 ρ(XYZ100) 是每个候选最近 {res['weekends']} 个周末的收益（关门价 → 平仓价）与 {res['ref']['name']} 的相关系数。",
          f"- 把 {k} 个候选的周末样本直接相加是 {sum((m.get('backtest') or {}).get('n', 0) for m in by_status.get('candidate', []))} 个；"
          f"按平均相关折算后的**有效独立样本约 {res['effective_samples']:.1f} 个**（公式 Σn / (1 + (k−1)·ρ̄)，ρ̄ 用各自与 XYZ100 相关系数均值的平方近似）。"
          "正式评估（备忘 7.4）的 30 个周末门槛要按有效样本数算，不是按名字数 × 周末数。",
          "- 不同时区的名字（韩/日/港/欧）重开时刻不同，和美股周末不同时结算，相关性天然更低——分散价值高于再加一个美股单票。"]
    if by_status.get("in_config"):
        L += ["", "## 已在 config.toml 里的合约（对照）", "",
              "| 市场 | 时间表 | 24h 成交额 | 持仓 | 点差 bps | 门槛 | 周末数 | 出手 | 平均净 bps | 累计 USD | ρ(XYZ100) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for m in by_status["in_config"]:
            b = m.get("backtest") or {}
            rho = "—" if b.get("rho_xyz100") is None else f"{b['rho_xyz100']:+.2f}"
            L.append(f"| {m['coin']} | {_SCHED_LABEL.get(m['schedule'], '待确认')} | {_usd(m['day_ntl_vlm'])} | {_usd(m['oi_usd'])} | "
                     f"{'—' if m['spread_bps'] is None else f'{m['spread_bps']:.1f}'} | {'过' if m['gate_ok'] else '不过：' + '；'.join(m['gate_reasons'])} | "
                     f"{b.get('n', 0)} | {b.get('trades', 0)} | {_bps(b.get('avg_net_bps'))} | "
                     f"{'—' if b.get('total_net_usd') is None else f'{b['total_net_usd']:+.2f}'} | {rho} |")
    if by_status.get("gate_fail"):
        L += ["", "## 分类成功但流动性不过门槛（不加）", "", "| 市场 | 时间表 | 24h 成交额 | 持仓 | 点差 bps | 原因 |", "|---|---|---|---|---|---|"]
        for m in by_status["gate_fail"]:
            L.append(f"| {m['coin']} | {_SCHED_LABEL[m['schedule']]} | {_usd(m['day_ntl_vlm'])} | {_usd(m['oi_usd'])} | "
                     f"{'—' if m['spread_bps'] is None else f'{m['spread_bps']:.1f}'} | {'；'.join(m['gate_reasons'])} |")
    if by_status.get("review"):
        L += ["", "## 待人工确认（认不出参考市场，不自动加）", "",
              "把确认好的写进 `knowledge/universe_overrides.toml` 的 `[markets]`（值为时间表键：" + ", ".join(SCHEDULES) + "），再跑一次 scan。", "",
              "| 市场 | dex | 24h 成交额 | 持仓 | 点差 bps | 门槛 | 说明 |", "|---|---|---|---|---|---|---|"]
        for m in by_status["review"]:
            L.append(f"| {m['coin']} | {m['dex']} | {_usd(m['day_ntl_vlm'])} | {_usd(m['oi_usd'])} | "
                     f"{'—' if m['spread_bps'] is None else f'{m['spread_bps']:.1f}'} | {'过' if m['gate_ok'] else '不过'} | {m['why']} |")
    failed = [m for m in ms if m["status"] in ("candidate", "in_config") and m.get("backtest") is not None
              and not (m["backtest"] or {}).get("n")]
    if failed:
        L += ["", "## 回测失败 / 样本为 0（原因）", "",
              "限速（429）的已自动退避重试过；仍失败的再跑一次 scan 即可（K 线会走缓存，不重复吃权重）。", "",
              "| 市场 | 状态 | 原因 |", "|---|---|---|"]
        for m in failed:
            L.append(f"| {m['coin']} | {m['status']} | {(m['backtest'] or {}).get('error') or '—'} |")
    for st, title in (("excluded", "覆盖文件排除"), ("delisted", "已下架")):
        if by_status.get(st):
            L += ["", f"## {title}", "", ", ".join(m["coin"] for m in by_status[st])]
    L += ["", "## 口径", "",
          "- 周末窗按各时间表：锚 = 真市场关门前最后一根 K 线收盘价；决策 = 重开前 15 分钟；平仓 = 重开后 5 分钟之后第一根 K 线。",
          "- 反向：|dev| > 门槛（挂单 3 + 吃单 9 + 半点差 + edge_buffer）时按 −sign(dev) 开仓；净收益扣全额手续费；不含 carry、不含滑点。",
          "- 反转率(全部) = 所有周末里决策→平仓方向与偏离相反的比例；命中率 = 只算过门槛出手的那些。",
          "- 时间表只给周末关门/重开；假日、提前收盘不在这里处理（windows_backtest 有美股/韩股/ICE 的手写日历）。",
          "- 下一步：`python3 -m desk.universe add --top N`（只允许周一到周四改 config.toml）。"]
    return "\n".join(L)


# ---------------------------------------------------------------- 入口
def write_outputs(settings, res: dict, out_md: Path | None = None, out_json: Path | None = None) -> tuple[Path, Path]:
    out_md = out_md or Path(settings.data) / "reports" / "universe.md"
    out_json = out_json or Path(settings.data) / "universe.json"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(report_md(res))
    out_json.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return out_md, out_json


def config_change_allowed(now: datetime, settings) -> bool:
    """config.toml 只能周一到周四改（周五收盘到周日决策之间改动会让当周作废）。"""
    tz = ZoneInfo(settings["clock"]["timezone"])
    return now.astimezone(tz).weekday() <= 3


def cmd_scan(settings, a, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    if a.fixture:
        raw = json.loads(Path(a.fixture).read_text())
        print(f"[universe] 用离线发现数据 {a.fixture}（抓于 {raw.get('fetched_at')}）", file=sys.stderr)
    else:
        ok, why = bl.hyperliquid_reachable()
        if not ok:
            print(f"[universe] Hyperliquid 不可达（{why}）。", file=sys.stderr)
            print(RUN_ON_HOST_HINT, file=sys.stderr)
            cached = Path(settings.data) / "universe_raw.json"
            if not cached.exists():
                return 1
            raw = json.loads(cached.read_text())
            print(f"[universe] 退回本地 {cached}（抓于 {raw.get('fetched_at')}）", file=sys.stderr)
        else:
            raw = discover()
            p = Path(settings.data) / "universe_raw.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(raw, ensure_ascii=False))
    res = scan(settings, raw, a.weekends, now, a.notional, a.half_spread)
    md, js = write_outputs(settings, res)
    print(report_md(res))
    print(f"\n[universe] 写入 {md} 与 {js}", file=sys.stderr)
    return 0


def cmd_add(settings, a, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    js = Path(settings.data) / "universe.json"
    if not js.exists():
        print(f"[universe] 没有 {js}，先跑 python3 -m desk.universe scan", file=sys.stderr)
        return 1
    res = json.loads(js.read_text())
    if not config_change_allowed(now, settings) and not a.force:
        print("[universe] 现在是周五到周日：config.toml 只允许周一到周四改（周末改动会让当周结果作废）。--force 可强行。", file=sys.stderr)
        return 2
    by_coin = {m["coin"]: m for m in res["markets"]}
    picks = [by_coin[c] for c in res["ranking"][:a.top]]
    insts = [stanza_for(m["coin"], m["schedule"]) for m in picks]
    comments = {symbol_of(m["coin"]): f"{_SCHED_LABEL[m['schedule']]}；universe scan {res['generated_at'][:10]} 第 {i} 名" for i, m in enumerate(picks, 1)}
    added = append_stanzas(Path(settings.root) / "config.toml", insts, comments)
    skipped = [i["name"] for i in insts if i["name"] not in added]
    for n in added:
        i = next(x for x in insts if x["name"] == n)
        print(f"+ {n:10s} {i['coin']:20s} {i['tz']:18s} {i['close_day']} {i['close']} → {i['resume_day']} {i['resume']}")
    if skipped:
        print(f"已存在，跳过：{', '.join(skipped)}")
    if not picks:
        print("排名为空：没有候选可加。")
    print(f"[universe] 加入 {len(added)} 个；记得 python -m desk refreeze，并把 relevance.py 的新闻相关性表补上新名字。", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="发现 + 分类 + 门槛 + 回测 + 排名")
    sc.add_argument("--weekends", type=int, default=None, help="回测最近多少个周末（默认 config [universe].weekends）")
    sc.add_argument("--notional", type=float, default=1000.0)
    sc.add_argument("--half-spread", type=float, default=None)
    sc.add_argument("--fixture", type=str, default=None, help="离线：用保存的 universe_raw.json 代替发现接口")
    ad = sub.add_parser("add", help="把排名前 N 的候选写进 config.toml")
    ad.add_argument("--top", type=int, default=10)
    ad.add_argument("--force", action="store_true", help="周五到周日也强行改 config.toml（不建议）")
    a = ap.parse_args(argv)
    s = config.load()
    return cmd_scan(s, a) if a.cmd == "scan" else cmd_add(s, a)


# ---------------------------------------------------------------- 测试用确定性 fixture（不是真数据）
def fixture_raw() -> dict:
    """仿 Hyperliquid 返回结构的假数据：一个 xyz dex + 一个 km dex + 主 dex。数值写死，测试用。"""
    def u(name, lev=10, delisted=False, growth=None):
        d = {"name": name, "szDecimals": 2, "maxLeverage": lev, "marginTableId": 1}
        if delisted:
            d["isDelisted"] = True
        if growth:
            d["growthMode"] = growth
        return d

    def c(px, oi, vlm, spread_bps=5.0, funding=0.00001):
        half = px * spread_bps / 2e4
        return {"funding": str(funding), "openInterest": str(oi), "prevDayPx": str(px * 0.99), "dayNtlVlm": str(vlm),
                "premium": "0.0001", "oraclePx": str(px), "markPx": str(px), "midPx": str(px),
                "impactPxs": [str(px - half), str(px + half)], "dayBaseVlm": str(vlm / px)}
    xyz_u = [u("xyz:XYZ100"), u("xyz:SP500"), u("xyz:BRENTOIL"), u("xyz:NVDA"), u("xyz:TSLA"), u("xyz:INTC"), u("xyz:SMSN"),
             u("xyz:AAPL"), u("xyz:MSFT", growth="enabled"), u("xyz:GOLD"), u("xyz:HYNIX"), u("xyz:EURUSD"), u("xyz:TSMC"),
             u("xyz:AMD"), u("xyz:OLDCO", delisted=True), u("xyz:GME")]
    xyz_c = [c(25000, 100, 5e6), c(6500, 400, 4e6), c(100, 20000, 3e6), c(180, 20000, 6e6), c(400, 5000, 2e6),
             c(30, 50000, 1e6), c(80, 30000, 1e6),
             c(230, 8000, 3e6), c(500, 4000, 2.5e6), c(4000, 100, 1.5e6), c(300, 2000, 9e5), c(1.08, 2e6, 2e6),
             c(200, 5000, 2e6), c(160, 3000, 1.2e6, spread_bps=4), c(10, 10, 100), c(50, 100, 20000, spread_bps=60)]
    km_u = [u("km:TOYOTA"), u("km:SONY"), u("km:ZZZ")]
    km_c = [c(20, 50000, 1.5e6, spread_bps=8), c(20, 50000, 1.5e6, spread_bps=35), c(5, 1000, 3e5, spread_bps=10)]
    main_u = [u("BTC", 50), u("ETH", 50), u("SOL", 20), u("XRP", 20, delisted=True)]
    main_c = [c(65000, 1000, 1e9), c(3000, 10000, 5e8), c(150, 100000, 2e8), c(0.5, 0, 0)]
    return {"fetched_at": "2026-10-12T12:00:00+00:00",
            "perp_dexs": [None, {"name": "xyz", "full_name": "trade.xyz", "deployer": "0x0"}, {"name": "km", "full_name": "km", "deployer": "0x1"}],
            "dexs": {"xyz": [{"universe": xyz_u}, xyz_c], "km": [{"universe": km_u}, km_c]},
            "main": [{"universe": main_u}, main_c], "errors": {}}


if __name__ == "__main__":
    raise SystemExit(main())
