"""总账户止损线（2026-10-19）：所有自动策略的真实盈亏加在一起，亏到上限就全部停开新仓。

- 周末策略（autotrade）：已实现盈亏（账本 exit 记录）。持仓只有 ~20 分钟，期间由 autotrade.kill_check 看守。
- 跨平台套利（pairs_exec）：已平仓配对的已实现盈亏 + 仍开着的配对按账本最后成交价估不了 → 只算已实现；
  开着的配对受单对 $4 / 单日 $8 止损约束。
触发后：两条线都切到 off（套利 off 时仍会照管已开仓位直到平完），写 state/account_stop.json，最高优先级推送。
只有人手动 `python -m desk.account_stop --reset` 才会解除（并不会自动把模式切回 live）。
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from . import notify

log = logging.getLogger(__name__)
DEFAULT_MAX_LOSS = 500.0


def _path(settings) -> Path:
    p = Path(settings.data) / "state" / "account_stop.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write(settings, d: dict) -> None:
    """原子写：先写临时文件再 os.replace，读的人永远看不到写了一半的文件。"""
    import os
    p = _path(settings)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    os.replace(tmp, p)


def mark_killed(settings, weekends: list[str]) -> None:
    st = state(settings)
    if st.get("_unreadable"):
        return
    _write(settings, {**st, "killed_weekends": sorted(set(st.get("killed_weekends", [])) | set(weekends))})


def limit(settings) -> float:
    try:
        return float((settings["account"] or {}).get("max_loss_usd", DEFAULT_MAX_LOSS))
    except (KeyError, TypeError):
        return DEFAULT_MAX_LOSS


def state(settings) -> dict:
    p = _path(settings)
    if not p.exists():
        return {"tripped": False}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"tripped": False, "_unreadable": True}


def tripped(settings) -> bool:
    return bool(state(settings).get("tripped"))


def components(settings) -> dict:
    out = {"weekend_usd": 0.0, "pairs_usd": 0.0, "errors": []}
    try:
        from . import autotrade
        out["weekend_usd"] = float(autotrade.realized_total(settings))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"weekend: {e}")
    try:
        from . import pairs_exec
        st = pairs_exec.load_state(settings)
        out["pairs_usd"] = float(sum((p.get("closed") or {}).get("pnl_usd", 0.0) or 0.0
                                     for p in st.get("pairs", {}).values() if p.get("closed")))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"pairs: {e}")
    raw = out["weekend_usd"] + out["pairs_usd"]
    base = float(state(settings).get("baseline_usd", 0.0) or 0.0)   # --reset 时记下的累计盈亏，之后只算新增亏损
    out["raw_total_usd"] = raw
    out["baseline_usd"] = base
    out["total_usd"] = raw - base
    return out


def check(settings) -> dict:
    """每分钟调用一次。读不到某条线的盈亏 → 报警但不误触发。"""
    c = components(settings)
    lim = limit(settings)
    c["limit_usd"] = lim
    st = state(settings)
    c["tripped"] = bool(st.get("tripped"))
    if c["errors"]:
        log.warning("account_stop: %s", c["errors"])
        now = time.time()
        if now - float(st.get("err_alert_ts", 0) or 0) >= 3600:      # 读不到盈亏 = 止损线失明：每小时推一次
            notify.push(settings, "总账户止损线读不到盈亏", "；".join(c["errors"])[:300], priority="high")
            cur = state(settings)                       # 写前重读，别用开头那份旧的覆盖掉刚触发的状态
            if not cur.get("_unreadable"):
                _write(settings, {**cur, "err_alert_ts": now})
    if c["tripped"] or c["total_usd"] > -lim:
        return c
    _trip(settings, c)
    c["tripped"] = True
    return c


def _trip(settings, c: dict) -> None:
    from . import autotrade, pairs_exec
    keep = {k: v for k, v in state(settings).items() if k in ("killed_weekends",)}
    _write(settings, {**keep, **c, "tripped": True, "ts": time.time()})
    for name, mod in (("周末策略", autotrade), ("跨平台套利", pairs_exec)):
        try:
            mod.put(settings, mode="off", updated_by="account_stop")
        except Exception as e:  # noqa: BLE001
            log.exception("account_stop: 关闭%s失败：%s", name, e)
    notify.push(settings, "总账户止损触发：所有自动下单已停",
                f"累计真实盈亏 ${c['total_usd']:.2f}（周末 ${c['weekend_usd']:.2f}，套利 ${c['pairs_usd']:.2f}），"
                f"上限 −${c['limit_usd']:.0f}。已开的套利仓位会继续按规则平掉，不再开新仓。", priority="max")


def main(argv=None) -> int:
    from . import config
    ap = argparse.ArgumentParser(description="总账户止损线")
    ap.add_argument("--reset", action="store_true", help="解除已触发的总止损（不会自动把模式切回真下单）")
    a = ap.parse_args(argv)
    s = config.load()
    if a.reset:
        old = state(s)
        _write(s, {"tripped": old.get("tripped", False)})       # 先去掉旧基线，算原始合计
        comp = components(s)
        if comp["errors"]:
            _write(s, old)
            print("没解除：有一条线的盈亏读不到，基线会算错。", comp["errors"])
            return 1
        raw = comp["raw_total_usd"]
        _write(s, {"tripped": False, "reset_ts": time.time(), "baseline_usd": raw,
                   "killed_weekends": old.get("killed_weekends", [])})
        print(f"已解除总账户止损（从现在的累计 ${raw:.2f} 起重新算 −${limit(s):.0f}）。需要的话再手动把各条线切回真下单。")
    c = components(s)
    print(json.dumps({**c, "limit_usd": limit(s), "tripped": tripped(s)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
