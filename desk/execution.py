"""实盘下单（备忘 7.7 第 9–12 周）。

按备忘的顺序和第八章风险清单，这一模块在以下条件全部满足前不会下任何单：
1. 已积累至少 [live] min_paper_weekends 个运行干净的模拟周末（默认 3；备忘原文为 8 周）；
2. config.toml 里 [live] enabled = true；
3. config.toml 里 [live] lawyer_confirmed = true（第八章："实盘前由律师书面确认主体与账户结构"）。

满足后再接入：只挂单（post-only / ALO）、不追单、单笔 ≤ 盘口可见深度 10%、禁止任何以影响价格为目的的下单。
"""
from __future__ import annotations


class LiveTradingLocked(RuntimeError):
    pass


def gate(settings, n_paper_weekends: int) -> list[str]:
    missing = []
    need = settings["live"].get("min_paper_weekends", 8)
    if n_paper_weekends < need:
        missing.append(f"运行干净的模拟周末只有 {n_paper_weekends} 个，至少需要 {need} 个")
    if not settings["live"]["enabled"]:
        missing.append("[live] enabled 未开启")
    if not settings["live"]["lawyer_confirmed"]:
        missing.append("未取得律师书面确认（第八章合规项）")
    return missing


def place_order(settings, *_, n_paper_weekends: int = 0, **__):
    missing = gate(settings, n_paper_weekends)
    if missing:
        raise LiveTradingLocked("实盘未解锁：" + "；".join(missing))
    raise LiveTradingLocked("实盘下单模块将在第 9 周、条件满足后接入")
