"""重开真钱单：开盘前用真实接口逐项检查（python -m desk.reopen_preflight [--order-test]）。

1 名单里每个品种在 xyz 上真实存在
2 每个品种都能查到实时价格
3 每个品种过去 10 分钟有历史数据（判断价格跳变要用；没有也能跑，开盘前 5 分钟实时补）
4 开关 live、没被止损锁住、余额能读到、手续费能查到
5 --order-test：挂一张离市价 20% 远、不可能成交的 ~$12 买单，确认挂上，再撤掉，确认撤掉（走真实下单路径，不会成交）
最后一行打印 全部通过 / 不通过 + 原因。
"""
from __future__ import annotations

import argparse
import time

from . import autotrade, config
from . import reopen_exec as rx
from . import reopen_study as rs

NS = 1_000_000_000


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order-test", action="store_true")
    ap.add_argument("--test-coin", default="SP500")
    a = ap.parse_args(argv)
    s = config.load()
    cf = rx.cfg(s)
    bad: list[str] = []

    def ok(msg):
        print("  ✓", msg)

    def fail(msg):
        print("  ✗", msg)
        bad.append(msg)

    names = list(dict.fromkeys(list(cf["coins"]) + [c for v in (cf.get("asia_coins") or {}).values() for c in v]))
    coins = {n: rx.coin_of(s, n) for n in names}
    c = rx._make_client(s, need_trading=a.order_test)

    print("1. 品种名字是否真实存在")
    mc = c.info.post("/info", {"type": "metaAndAssetCtxs", "dex": autotrade.DEX})
    on_dex = {u["name"] for u in mc[0]["universe"] if not u.get("isDelisted")}
    missing = [n for n, co in coins.items() if co not in on_dex]
    if missing:
        fail(f"这些名字交易所上没有：{missing}")
    else:
        ok(f"{len(coins)} 个全部存在")

    print("2. 实时价格")
    good = [co for co in coins.values() if co in on_dex]
    try:
        cx = c.ctxs(good)
        zero = [co for co in good if not cx.get(co) or not cx[co]["oracle"] or not cx[co]["mid"]]
        fail(f"查不到价格：{zero}") if zero else ok(f"{len(good)} 个都有价格")
    except Exception as e:  # noqa: BLE001
        fail(f"查价格报错：{type(e).__name__}: {e}")

    print("3. 过去 10 分钟历史数据")
    now_ns = int(time.time() * NS)
    thin = []
    for n, co in coins.items():
        if co not in on_dex:
            continue
        try:
            rows = rs.read_ctx(s, co, now_ns - 600 * NS, now_ns)
        except Exception:  # noqa: BLE001
            rows = []
        if len(rows) < 5:
            thin.append(n)
    if thin:
        print(f"  ! 这些没有历史数据（不算失败，开盘前 5 分钟会实时补）：{thin}")
    else:
        ok("全部有历史数据")

    print("4. 开关 / 止损 / 余额 / 手续费")
    h = rx.get(s)
    ok("开关 live") if h.get("mode") == "live" else fail(f"开关是 {h.get('mode')}，不是 live")
    fail(f"被锁住：{h.get('halt_reason')}") if h.get("halt_entries") else ok("没有被止损锁住")
    try:
        from . import account_stop
        fail("总账户止损已触发") if account_stop.check(s).get("tripped") else ok("总账户止损没触发")
    except Exception as e:  # noqa: BLE001
        fail(f"总账户止损读不到：{e}")
    try:
        b = c.balance()
        av = b.get("available")
        ok(f"可用余额 ${float(av):.2f}") if av is not None and float(av) >= 600 else fail(f"可用余额不够一笔 $500：{av}")
    except Exception as e:  # noqa: BLE001
        fail(f"读余额报错：{e}")
    if c.address:
        rt, src = rx.round_trip_fee_bps(c, cf=cf)
        print(f"  · 往返手续费 {rt:.2f} bps（HIP-3，来源 {src}；growth 折扣核实={cf['hip3_growth_verified']}），实际门槛 {max(cf['k_bps'], rt + rx.FEE_MARGIN_BPS):.1f} bps")

    if a.order_test:
        print(f"5. 真实挂单 + 撤单（{a.test_coin}，离市价 20%，不会成交）")
        co = rx.coin_of(s, a.test_coin)
        try:
            dec = int(c.sz_decimals(co))
            bk = c.book(co)
            px = autotrade.round_px(bk["bid"] * 0.8, dec)
            sz = autotrade.round_sz(12.0 / px, dec)
            if sz * px < 10.5:
                sz = autotrade.round_sz(sz + 10 ** (-dec), dec)
            c.set_leverage(co, rx.LEVERAGE)
            ok("1 倍逐仓设置成功")
            r = c.alo(co, True, sz, px, False, f"preflight-{int(time.time())}")
            if r.get("error") or not r.get("oid"):
                fail(f"挂单失败：{r.get('error') or r.get('raw')}")
            elif r.get("filled_sz"):
                fail(f"居然成交了 {r['filled_sz']}，请手动检查")
            else:
                ok(f"挂单成功 买 {sz:g} @ {px:g}（约 ${sz * px:.2f}）")
                time.sleep(2)
                c.cancel(co, r["oid"])
                time.sleep(2)
                st = (c.order_info(r["oid"]) or {}).get("status")
                ok("撤单成功") if st and st != "open" else fail(f"撤单后状态还是 {st}，请到网页手动撤")
        except Exception as e:  # noqa: BLE001
            fail(f"真实下单路径报错：{type(e).__name__}: {e}")

    print()
    print("全部通过" if not bad else f"不通过（{len(bad)} 项）：" + "；".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
