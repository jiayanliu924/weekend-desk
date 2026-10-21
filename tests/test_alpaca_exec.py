"""美股期权执行层（11d）：标的映射、选合约、张数、铁蝶腿、多腿单格式、每条门单独不过时绝不碰真钱下单、全过才走真钱、
当日止损、平仓生命周期、批准 CLI、没 key 时演练。全部用假 transport，不碰网络。"""
import json
import math
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from desk import alpaca_exec as ax, config
from desk.ledger import Ledger
from desk.options import Sample

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
NOW = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)        # 周四 11:00 美东，开盘中
WIN_END = datetime(2026, 10, 16, 11, 30, tzinfo=UTC)   # 周五 07:30 美东
EXP_OK = "2026-10-16"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("ALPACA_KEY_ID", "ALPACA_SECRET", "ALPACA_PAPER", "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET"):
        monkeypatch.setenv(k, "x")          # 先记下原值，测试结束时恢复（save_env_key 会直接写 os.environ）
        monkeypatch.delenv(k)
    monkeypatch.setattr(ax, "TRANSPORT", None)


@pytest.fixture
def s(tmp_path):
    for f in ("config.toml", "events.toml"):
        shutil.copy(ROOT / f, tmp_path / f)
    st = config.load(tmp_path)
    st.raw["alpaca"] = {**st.raw["alpaca"], "enabled": True}
    st.raw["debate"] = {**st.raw.get("debate", {}), "enabled": False}   # 辩论闸门另有 tests/test_debate.py
    return st


# ---------------------------------------------------------------- 假 Alpaca
def chain(S=580.0, exps=("2026-10-15", EXP_OK, "2026-10-17"), step=1.0, span=30):
    out = []
    for e in exps:
        k = S - span
        while k <= S + span:
            for cp in ("call", "put"):
                sym = f"SPY{e[2:4]}{e[5:7]}{e[8:10]}{cp[0].upper()}{int(round(k * 1000)):08d}"
                out.append({"symbol": sym, "expiration_date": e, "strike_price": f"{k:.2f}", "type": cp, "tradable": True})
            k += step
    return out


def quotes(contracts, S=580.0, atm=3.0):
    """平值 ~3 美元，离平值每远 1% 减一些；买卖价差 0.10。"""
    snaps = {}
    for c in contracts:
        K = float(c["strike_price"])
        intr = max(S - K, 0) if c["type"] == "call" else max(K - S, 0)
        tv = max(atm - abs(K - S) * 0.15, 0.05)
        m = intr + tv
        snaps[c["symbol"]] = {"latestQuote": {"bp": round(m - 0.05, 2), "ap": round(m + 0.05, 2)}}
    return snaps


class FakeAlpaca:
    def __init__(self, S=580.0, is_open=True, mins_to_close=300, order_fail=False, contracts=None, now=NOW):
        self.S, self.is_open, self.mins, self.order_fail = S, is_open, mins_to_close, order_fail
        self.contracts = contracts if contracts is not None else chain(S)
        self.snaps = quotes(self.contracts, S)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.now = now

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, url, body))
        path = req.url.path
        if path == "/v2/clock":
            ts = self.now.astimezone(ax.ET)
            return httpx.Response(200, json={"timestamp": ts.isoformat()[:19] + ".123456789" + ts.isoformat()[19:],
                                             "is_open": self.is_open, "next_open": "2026-10-16T09:30:00-04:00",
                                             "next_close": (ts + timedelta(minutes=self.mins)).isoformat()})
        if path.startswith("/v2/stocks/"):
            return httpx.Response(200, json={"trade": {"p": self.S}})
        if path == "/v2/options/contracts":
            return httpx.Response(200, json={"option_contracts": self.contracts, "next_page_token": None})
        if path.startswith("/v1beta1/options/snapshots/"):
            e = req.url.params.get("expiration_date")
            snaps = {c["symbol"]: self.snaps[c["symbol"]] for c in self.contracts if not e or c["expiration_date"] == e}
            return httpx.Response(200, json={"snapshots": snaps, "next_page_token": None})
        if path == "/v2/orders" and req.method == "POST":
            if self.order_fail:
                return httpx.Response(422, json={"message": "mleg market not supported"})
            return httpx.Response(200, json={"id": f"ord-{len(self.calls)}", "status": "accepted", **body})
        if path.startswith("/v2/positions/") and req.method == "DELETE":
            return httpx.Response(200, json={"id": "close-" + path.rsplit("/", 1)[1], "status": "accepted"})
        if path == "/v2/positions":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "not faked: " + path})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def orders(self, base=None):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/v2/orders") and (base is None or c[1].startswith(base))]

    def live_hits(self):
        return [c for c in self.calls if c[1].startswith(ax.BASE_LIVE)]


def smp(kind="event", et="CPI", sid="EVT-CPI-2026-10"):
    return Sample(sid, kind, et, NOW - timedelta(hours=1), NOW - timedelta(hours=1), WIN_END)


# ---------------------------------------------------------------- 让所有真钱门都通过
RISK = 1500.0   # SPY 平值铁蝶（±3% 翅膀）每张最多亏 ≈ $1,190，默认 $100 买不起 1 张——测试里放大


def paper_keys(s, monkeypatch, risk=RISK):
    monkeypatch.setenv("ALPACA_KEY_ID", "PKPAPER000000000000000")
    monkeypatch.setenv("ALPACA_SECRET", "x")
    s.raw["alpaca"]["max_risk_usd"] = risk


def make_all_pass(s, monkeypatch):
    s.raw["alpaca"].update({"enabled": True, "live": True, "max_risk_usd": RISK})
    monkeypatch.setenv("ALPACA_KEY_ID", "AKLIVE0000000000000000")
    monkeypatch.setenv("ALPACA_SECRET", "secretlive")
    monkeypatch.setenv("ALPACA_PAPER", "0")
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "PKPAPER000000000000000")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "secretpaper")
    ax.approve(s, "kea")
    ax.approve(s, "boss")
    write_gate(s)
    seed_paper(s, 5, 10.0)
    seed_real(s, 10)


def write_gate(s, **over):
    g = {"method": "dvol_proxy", "events_only": True, "n_events": 80, "net_usd": 120.0, "net_ex_best3_usd": 40.0,
         "worst_usd": -150.0, "pass": True, "synthetic": False, "underlying": "SPY", "side": "short"} | over
    p = ax.gate_file(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(g))


def seed_paper(s, n, pnl_each, kind="event"):
    led = Ledger(s.data, "options")
    for i in range(n):
        sid = f"EVT-T-{kind}-{i}"
        led.append("lock", sid, {"sample_kind": kind, "event_type": "CPI", "ok": True})
        led.append("outcome", sid, {"ok": True, "ratio": 1.0, "paper": {"ok": True, "chosen": "A", "chosen_pnl": pnl_each}})


def seed_real(s, n, a_pnl=5.0, b_pnl=-8.0, kind="event", ratio=0.9, start=0):
    """用真实期权链结算过的样本（锁定有快照）。chosen = C，所以不影响"纸面成绩"那条门的累计。"""
    led = Ledger(s.data, "options")
    for i in range(start, start + n):
        sid = f"EVT-REAL-{kind}-{i}"
        led.append("lock", sid, {"sample_kind": kind, "event_type": "CPI" if kind == "event" else ("none" if kind == "daily" else "SHOCK:GEO"),
                                 "ok": True, "snapshot_received_at": 1, "implied_vol": 0.5})
        led.append("outcome", sid, {"ok": True, "ratio": ratio, "log_ratio": math.log(ratio),
                                    "paper": {"ok": True, "chosen": "C", "chosen_pnl": 0.0,
                                              "pnl": {"A_unhedged": a_pnl, "A_hedged": a_pnl, "B": b_pnl, "C": 0.0}}})


# ---------------------------------------------------------------- 映射 / 选合约 / 张数
def test_ticker_mapping(s):
    assert ax.map_underlying(s, "CPI") == "SPY"
    assert ax.map_underlying(s, "FOMC") == "SPY"
    assert ax.map_underlying(s, "NFP") == "SPY"
    assert ax.map_underlying(s, "EARNINGS:nvda") == "NVDA"
    assert ax.map_underlying(s, "SHOCK:HACK") == "IBIT"
    assert ax.map_underlying(s, "SHOCK:REG") == "IBIT"
    assert ax.map_underlying(s, "SHOCK:GEO") == "SPY"
    assert ax.map_underlying(s, "SHOCK:TARIFF") == "SPY"
    assert ax.map_underlying(s, "none") == "IBIT"
    assert ax.map_underlying(s, "BTC") == "IBIT"
    s.raw["alpaca"]["macro_underlying"] = "QQQ"
    assert ax.map_underlying(s, "CPI") == "QQQ"
    assert ax.map_underlying(s, "SHOCK:MACRO") == "QQQ"
    assert ax.map_underlying(s, "WEIRD") is None


def test_expiry_and_atm_selection():
    cons = chain(580.0)
    # 10-15 16:00 美东 < 窗口结束（10-16 07:30 美东）→ 选 10-16
    assert ax.pick_expiry(cons, WIN_END) == date(2026, 10, 16)
    # 窗口结束在 10-16 17:00 美东之后 → 10-17
    assert ax.pick_expiry(cons, datetime(2026, 10, 16, 21, 0, tzinfo=UTC)) == date(2026, 10, 17)
    assert ax.pick_expiry(cons, datetime(2026, 10, 20, tzinfo=UTC)) is None
    sel = ax.select_legs("B", cons, date(2026, 10, 16), 580.4, 3.0)
    assert sel["ok"] and sel["K"] == 580.0
    assert [(l["side"], l["cp"], l["position_intent"]) for l in sel["legs"]] == [("buy", "C", "buy_to_open"), ("buy", "P", "buy_to_open")]
    sel = ax.select_legs("B", cons, date(2026, 10, 16), 580.6, 3.0)
    assert sel["K"] == 581.0


def test_iron_butterfly_legs():
    cons = chain(580.0)
    sel = ax.select_legs("A", cons, date(2026, 10, 16), 580.0, 3.0)
    assert sel["ok"] and sel["K"] == 580.0
    assert sel["wings"] == {"call": 597.0, "put": 563.0}       # 580 × (1 ± 3%) = 597.4 / 562.6 → 最近的整数行权价
    roles = {l["role"]: l for l in sel["legs"]}
    assert roles["short_call"]["side"] == "sell" and roles["short_call"]["strike"] == 580 and roles["short_call"]["position_intent"] == "sell_to_open"
    assert roles["short_put"]["side"] == "sell" and roles["short_put"]["strike"] == 580
    assert roles["long_call_wing"]["side"] == "buy" and roles["long_call_wing"]["strike"] == 597 and roles["long_call_wing"]["cp"] == "C"
    assert roles["long_put_wing"]["side"] == "buy" and roles["long_put_wing"]["strike"] == 563 and roles["long_put_wing"]["cp"] == "P"
    pr = ax.price_structure("A", sel, quotes(cons))
    assert pr["ok"] and pr["credit"] > 0 and pr["limit_price"] < 0      # 收钱 → 负数限价
    assert pr["max_loss_per_contract"] == pytest.approx((17.0 - pr["credit"]) * 100)
    pb = ax.price_structure("B", ax.select_legs("B", cons, date(2026, 10, 16), 580.0, 3.0), quotes(cons))
    assert pb["debit"] == pytest.approx(6.0) and pb["max_loss_per_contract"] == pytest.approx(600.0) and pb["limit_price"] > 0


def test_qty_from_risk():
    assert ax.qty_for(100, 30) == 3
    assert ax.qty_for(100, 100) == 1
    assert ax.qty_for(100, 600) == 0
    assert ax.qty_for(100, 0) == 0


def test_mleg_payload_shape():
    legs = [{"symbol": "SPY261016C00580000", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": "SPY261016P00580000", "side": "sell", "position_intent": "sell_to_open"}]
    p = ax.mleg_payload(legs, 2, "limit", -5.3, client_order_id="wd-x")
    assert p == {"order_class": "mleg", "qty": "2", "type": "limit", "time_in_force": "day", "limit_price": "-5.30",
                 "client_order_id": "wd-x",
                 "legs": [{"symbol": "SPY261016C00580000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                          {"symbol": "SPY261016P00580000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}]}
    m = ax.mleg_payload(ax.closing_legs(legs), 2, "market")
    assert "limit_price" not in m and m["type"] == "market"
    assert [(l["side"], l["position_intent"]) for l in m["legs"]] == [("buy", "buy_to_close")] * 2


# ---------------------------------------------------------------- 门
def test_all_gates_pass_goes_live(s, monkeypatch):
    make_all_pass(s, monkeypatch)
    fk = FakeAlpaca()
    ok, reasons = ax.can_go_live(s, NOW, fk.transport)
    assert ok, reasons
    rec = ax.open_for_sample(s, smp(), {"chosen": "A", "reason": "x"}, now=NOW, transport=fk.transport)
    assert rec["kind"] == "open" and rec["mode"] == "live" and rec["sent"]
    assert len(fk.orders(ax.BASE_LIVE)) == 1 and not fk.orders(ax.BASE_PAPER)
    body = fk.orders()[0][2]
    assert body["order_class"] == "mleg" and len(body["legs"]) == 4 and body["type"] == "limit"
    assert int(body["qty"]) == rec["qty"] >= 1 and rec["max_loss_usd"] <= RISK
    assert all(g["ok"] for g in rec["gates"])
    assert Path(s.data, "ledger", "alpaca.jsonl").exists()


GATE_BREAKERS = {
    "config_live_false": lambda s, mp: s.raw["alpaca"].update({"live": False}),
    "config_disabled_paper_keys": lambda s, mp: mp.setenv("ALPACA_PAPER", "1"),
    "missing_boss": lambda s, mp: ax.approve(s, "boss", revoke=True),
    "missing_kea": lambda s, mp: ax.approve(s, "kea", revoke=True),
    "approval_stale": lambda s, mp: s.raw["alpaca"].update({"max_risk_usd": RISK + 1}),
    "no_gate_file": lambda s, mp: ax.gate_file(s).unlink(),
    "gate_few_events": lambda s, mp: write_gate(s, n_events=59),
    "gate_net_neg": lambda s, mp: write_gate(s, net_usd=-1.0),
    "gate_ex_best3": lambda s, mp: write_gate(s, net_ex_best3_usd=-0.01),
    "gate_worst": lambda s, mp: write_gate(s, worst_usd=-3 * RISK - 1),
    "gate_synthetic": lambda s, mp: write_gate(s, synthetic=True),
    "gate_old_format_with_weekends": lambda s, mp: write_gate(s, method=None, events_only=None),
    "real_few": lambda s, mp: (Ledger(s.data, "options").path.unlink(), seed_paper(s, 5, 10.0), seed_real(s, 9)),
    "real_daily_only": lambda s, mp: (Ledger(s.data, "options").path.unlink(), seed_paper(s, 5, 10.0), seed_real(s, 30, kind="daily")),
    "real_losing": lambda s, mp: (Ledger(s.data, "options").path.unlink(), seed_paper(s, 5, 10.0), seed_real(s, 12, a_pnl=-2.0, b_pnl=-1.0)),
    "real_worst": lambda s, mp: seed_real(s, 1, a_pnl=-3 * RISK - 1, start=99),
    "paper_few": lambda s, mp: (Ledger(s.data, "options").path.unlink(), seed_paper(s, 4, 10.0)),
    "paper_daily_only": lambda s, mp: (Ledger(s.data, "options").path.unlink(), seed_paper(s, 9, 10.0, kind="daily")),
    "paper_negative": lambda s, mp: seed_paper(s, 1, -51.0),
    "open_position": lambda s, mp: Ledger(s.data, "alpaca").append("open", "EVT-OLD", {"mode": "live", "sent": True, "legs": []}),
    "daily_loss": lambda s, mp: Ledger(s.data, "alpaca").append("close", "EVT-OLD", {"mode": "live", "pnl_usd": -100.0, "final": True}),
    "daily_loss_lower_stop": lambda s, mp: (s.raw["alpaca"].update({"daily_loss_stop_usd": 50.0}),
                                            Ledger(s.data, "alpaca").append("close", "EVT-OLD", {"mode": "live", "pnl_usd": -50.0, "final": True})),
}


@pytest.mark.parametrize("name", sorted(GATE_BREAKERS))
def test_each_gate_failing_never_posts_live(s, monkeypatch, name):
    make_all_pass(s, monkeypatch)
    GATE_BREAKERS[name](s, monkeypatch)
    fk = FakeAlpaca()
    # now=None：当日止损按真实的"今天"算（账本 written_at 是真实时间）
    ok, reasons = ax.can_go_live(s, None, fk.transport)
    assert not ok and reasons
    rec = ax.open_for_sample(s, smp(), {"chosen": "B"}, now=None, transport=fk.transport)
    assert rec is None or rec["mode"] != "live"
    assert not fk.live_hits(), fk.live_hits()      # 非时钟的门不过 → 连真钱端点都不碰
    assert not fk.orders(ax.BASE_LIVE)


@pytest.mark.parametrize("is_open,mins", [(False, 300), (True, 9)])
def test_clock_gate(s, monkeypatch, is_open, mins):
    make_all_pass(s, monkeypatch)
    fk = FakeAlpaca(is_open=is_open, mins_to_close=mins)
    ok, reasons = ax.can_go_live(s, NOW, fk.transport)
    assert not ok and any("开盘" in r or "收盘" in r for r in reasons)
    rec = ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport)
    assert rec["mode"] == "paper"
    assert not fk.orders(ax.BASE_LIVE)                       # 只问过时钟，没下真钱单
    assert all(c[1].endswith("/v2/clock") for c in fk.live_hits())
    assert len(fk.orders(ax.BASE_PAPER)) == 1


def test_daily_loss_stop_uses_today_only(s, monkeypatch):
    make_all_pass(s, monkeypatch)
    led = Ledger(s.data, "alpaca")
    led.append("close", "EVT-A", {"mode": "live", "pnl_usd": -60.0, "final": True})
    led.append("close", "EVT-B", {"mode": "paper", "pnl_usd": -500.0, "final": True})   # 纸面亏损不算
    today = datetime.now(UTC)
    assert ax.realized_today(s, "live", today) == pytest.approx(-60.0)
    assert ax.realized_today(s, "live", today + timedelta(days=2)) == 0.0
    chk = {g["key"]: g for g in ax.gate_checks(s, today, check_clock=False)}
    assert chk["daily_loss"]["ok"]
    led.append("close", "EVT-C", {"mode": "live", "pnl_usd": -45.0, "final": True})
    chk = {g["key"]: g for g in ax.gate_checks(s, today, check_clock=False)}
    assert not chk["daily_loss"]["ok"]


# ---------------------------------------------------------------- 模式 / 生命周期
def test_dry_mode_without_keys(s):
    fk = FakeAlpaca()
    rec = ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport)
    assert rec["mode"] == "dry" and rec["sent"] is False and rec["payload"] is None
    assert not fk.calls
    assert ax.open_positions(s, "dry")
    cl = ax.close_for_sample(s, smp(), now=WIN_END, transport=fk.transport)
    assert cl["kind"] == "close" and cl["status"] == "dry" and not fk.calls
    assert not ax.open_positions(s)


def test_disabled_and_skips(s, monkeypatch):
    fk = FakeAlpaca()
    s.raw["alpaca"]["enabled"] = False
    assert ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport) is None
    s.raw["alpaca"]["enabled"] = True
    assert ax.open_for_sample(s, smp(), {"chosen": "C"}, now=NOW, transport=fk.transport) is None
    assert ax.open_for_sample(s, smp(kind="daily", et="none", sid="DAY-2026-10-15"), {"chosen": "A"}, now=NOW,
                              transport=fk.transport) is None
    assert not Path(s.data, "ledger", "alpaca.jsonl").exists() or not Ledger(s.data, "alpaca").all()


def test_qty_zero_skips_with_reason(s, monkeypatch):
    paper_keys(s, monkeypatch, risk=100.0)
    fk = FakeAlpaca()
    rec = ax.open_for_sample(s, smp(), {"chosen": "B"}, now=NOW, transport=fk.transport)   # 买跨式每张 ≈ $600 > $100
    assert rec["kind"] == "skip" and "0 张" in rec["reason"] and rec["mode"] == "paper"
    assert not fk.orders()


def test_paper_open_then_close_lifecycle(s, monkeypatch):
    paper_keys(s, monkeypatch)
    fk = FakeAlpaca()
    rec = ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport)
    assert rec["mode"] == "paper" and rec["sent"] and rec["endpoint"] == ax.BASE_PAPER
    assert ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport) is None   # 幂等
    # 另一个样本：纸面里已有 1 个未平 → 跳过
    other = ax.open_for_sample(s, smp(sid="EVT-FOMC-X", et="FOMC"), {"chosen": "A"}, now=NOW, transport=fk.transport)
    assert other["kind"] == "skip" and "最多同时 1 个" in other["reason"]
    # 平仓：市场开着，反向多腿市价单
    fk.S = 585.0
    fk.snaps = quotes(fk.contracts, 585.0)
    close_now = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
    cl = ax.close_for_sample(s, smp(), now=close_now, transport=fk.transport)
    assert cl["kind"] == "close" and cl["status"] == "closed"
    body = fk.orders()[-1][2]
    assert body["type"] == "market" and body["order_class"] == "mleg" and body["qty"] == str(rec["qty"])
    assert sorted((l["side"], l["position_intent"]) for l in body["legs"]) == sorted(
        [("buy", "buy_to_close")] * 2 + [("sell", "sell_to_close")] * 2)
    assert cl["pnl_usd"] is not None
    assert not ax.open_positions(s)
    assert ax.close_for_sample(s, smp(), now=close_now, transport=fk.transport) is None


def test_close_waits_for_open_then_retries(s, monkeypatch):
    paper_keys(s, monkeypatch)
    fk = FakeAlpaca()
    ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport)
    fk.is_open = False
    p = ax.close_for_sample(s, smp(), now=WIN_END, transport=fk.transport)
    assert p["kind"] == "close_pending" and ax.open_positions(s)
    fk.is_open = True
    res = ax.retry_pending(s, now=datetime(2026, 10, 16, 14, 0, tzinfo=UTC), transport=fk.transport)
    assert len(res) == 1 and res[0]["status"] == "closed" and not ax.open_positions(s)


def test_close_falls_back_to_per_leg(s, monkeypatch):
    paper_keys(s, monkeypatch)
    fk = FakeAlpaca()
    ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport)
    fk.order_fail = True
    cl = ax.close_for_sample(s, smp(), now=datetime(2026, 10, 16, 14, 0, tzinfo=UTC), transport=fk.transport)
    assert cl["kind"] == "close" and cl["error"] and len(cl["fallback"]) == 4
    assert sum(1 for c in fk.calls if c[0] == "DELETE") == 4


def test_expired_records_and_skips(s, monkeypatch):
    paper_keys(s, monkeypatch)
    fk = FakeAlpaca()
    rec = ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport)
    n_orders = len(fk.orders())
    cl = ax.close_for_sample(s, smp(), now=datetime(2026, 10, 17, 1, 0, tzinfo=UTC), transport=fk.transport)
    assert cl["status"] == "expired" and len(fk.orders()) == n_orders
    assert cl["pnl_usd"] == pytest.approx(rec["pricing"]["credit"] * 100 * rec["qty"])   # 收盘在平值 → 全额权利金
    assert not ax.open_positions(s)


def test_panic_close(s, monkeypatch):
    paper_keys(s, monkeypatch)
    fk = FakeAlpaca()
    ax.open_for_sample(s, smp(), {"chosen": "A"}, now=NOW, transport=fk.transport)
    res = ax.panic_close(s, transport=fk.transport)
    assert len(res) == 1 and res[0]["final"]
    assert sum(1 for c in fk.calls if c[0] == "DELETE" and c[1].startswith(ax.BASE_PAPER)) == 4
    assert not ax.open_positions(s)


def test_approve_cli(s, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DESK_ROOT", str(s.root))
    monkeypatch.setenv("DESK_DATA", str(s.data))
    assert ax.main(["approve", "--who", "kea"]) == 0
    assert ax.main(["approve", "--who", "boss"]) == 0
    ap = ax.approvals(s)
    assert set(ap) == {"kea", "boss"} and datetime.fromisoformat(ap["kea"]["ts"]) and ap["boss"]["max_risk_usd"] == 100.0
    chk = {g["key"]: g for g in ax.gate_checks(s, check_clock=False)}
    assert chk["approvals"]["ok"]
    assert ax.main(["approve", "--who", "boss", "--revoke"]) == 0
    assert "boss" not in ax.approvals(s)
    with pytest.raises(SystemExit):
        ax.main(["approve", "--who", "someone"])
    assert ax.main(["status"]) == 0
    assert "真钱条件" in capsys.readouterr().out


def test_job_lock_hook_never_breaks_research(s, monkeypatch):
    """job_lock 选了 A 就调 open_for_sample；它抛错也不能影响研究账本。"""
    from desk import options, options_paper
    called = []

    def boom(settings, sample, plan):
        called.append((sample.sid, plan["chosen"]))
        raise RuntimeError("boom")
    monkeypatch.setattr(ax, "open_for_sample", boom)
    monkeypatch.setattr(options, "chain_at", lambda settings, t: (1, [{"n": "x"}]))
    monkeypatch.setattr(options, "atm_term_structure", lambda items, now: [])
    monkeypatch.setattr(options, "implied_vol_for", lambda ts, h: 0.5)
    monkeypatch.setattr(options, "predict", lambda *a: {"pred": 0.0, "baseline_B": 0.0, "model_version": "test"})
    monkeypatch.setattr(options, "lock_text", lambda rec: "测试")
    monkeypatch.setattr(options_paper, "plan", lambda *a: {"chosen": "A", "ok": False, "problems": ["测试"]})
    rec = options.job_lock(s, smp(), run_extract=False)
    assert rec["kind"] == "lock" and called == [("EVT-CPI-2026-10", "A")]


def test_backtest_gate_writer(s):
    from desk import options_backtest as ob
    rows = [{"short": x, "long": -x, "log_ratio": 0.0, "ratio": 1.0, "fees": 0.1, "premium": 1.0} for x in [5.0] * 70 + [-20.0]]
    g = ob.write_gate(s, rows, "SPY", "us", False)
    d = json.loads(ax.gate_file(s).read_text())
    assert d["n_events"] == 71 and d["net_usd"] == pytest.approx(330.0) and d["worst_usd"] == -20.0 and d["pass"] and g["pass"]
    assert set(d) >= {"n_events", "net_usd", "net_ex_best3_usd", "worst_usd", "pass"}
    assert not ob.write_gate(s, rows, "SPY", "us", True)["pass"]


def test_backtest_gate_excludes_weekends(s):
    """周末样本（DVOL 近似在周末系统性高估卖方）不进回测门：事件全亏、周末大赚 → 门不过。"""
    from desk import options_backtest as ob
    ev = [{"kind": "CPI", "short": -1.0, "long": 0.5, "log_ratio": 0.0, "ratio": 1.0, "fees": 0.1, "premium": 1.0}] * 70
    wk = [{"kind": "WEEKEND", "short": 50.0, "long": -51.0, "log_ratio": -0.9, "ratio": 0.4, "fees": 0.1, "premium": 1.0}] * 300
    g = ob.write_gate(s, ev + wk, "BTC", "crypto", False)
    d = json.loads(ax.gate_file(s).read_text())
    assert d["method"] == "dvol_proxy" and d["events_only"] and d["n_weekend_excluded"] == 300
    assert d["n_events"] == 70 and d["net_usd"] == pytest.approx(-70.0) and not g["pass"]
    chk = {x["key"]: x for x in ax.gate_checks(s, check_clock=False)}
    assert not chk["backtest"]["ok"] and "周末" in chk["backtest"]["detail"]
    # 同一批周末行要是算进去会"通过"——这正是要防的假象
    assert ob.aggregate(ev + wk)["net"] > 0
    # 报告里周末行有明确标注
    rows = [dict(r, year=2024) for r in ev + wk]
    lines = ob.verdict_lines(rows, ob.by_kind_year(rows))
    assert any(ob.WEEKEND_NOTE in x for x in lines) and any("只算排期事件" in x for x in lines)


def test_can_go_live_needs_both_backtest_gates(s, monkeypatch):
    make_all_pass(s, monkeypatch)
    fk = FakeAlpaca()
    assert ax.can_go_live(s, NOW, fk.transport)[0]
    # 只有近似门过、真实报价门不过
    Ledger(s.data, "options").path.unlink()
    seed_paper(s, 5, 10.0)
    ok, reasons = ax.can_go_live(s, NOW, fk.transport)
    assert not ok and any(r.startswith("真实报价门") and "要 ≥ 10" in r for r in reasons)
    assert not any(r.startswith("近似回测门") for r in reasons)
    # 只有真实报价门过、近似门不过
    seed_real(s, 10)
    write_gate(s, net_usd=-5.0)
    ok, reasons = ax.can_go_live(s, NOW, fk.transport)
    assert not ok and any(r.startswith("近似回测门") for r in reasons)
    assert not any(r.startswith("真实报价门") for r in reasons)
    assert not fk.orders(ax.BASE_LIVE)


def test_web_sections(s, monkeypatch, tmp_path):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DESK_ROOT", str(s.root))
    monkeypatch.setenv("DESK_DATA", str(s.data))
    monkeypatch.setenv("WEB_INSECURE", "1")
    monkeypatch.setenv("WEB_SECRET", "x" * 32)
    import desk.web as web
    web = importlib.reload(web)
    web.add_user(web.S, "kea", "correct horse battery")
    Ledger(web.S.data, "alpaca").append("skip", "EVT-CPI-X", {"mode": "dry", "reason": "测试原因"})
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    h = c.get("/settings").text
    assert "美股期权执行（Alpaca）" in h and "两人批准" in h and "✗" in h
    assert "近似回测门" in h and "真实报价门" in h
    o = c.get("/options").text
    assert "美股期权执行（Alpaca）" in o and "测试原因" in o
    assert "真实报价 vs 近似回测" in o and "周末" in o
    r = c.post("/settings/alpaca", data={"key_id": "PK1234567890ABCDEFGH", "secret": "sec", "paper": "1"}, follow_redirects=False)
    assert r.status_code == 303
    env = Path(web.S.root, ".env").read_text()
    assert "ALPACA_KEY_ID=PK1234567890ABCDEFGH" in env and "ALPACA_PAPER=1" in env
