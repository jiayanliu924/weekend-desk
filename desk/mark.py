"""稳健公允价与资金费率工具（借鉴竞品 Meridian/Keel 文档，但只作为本 bot 的"安全 + 信号"层）。

竞品文档的两条可用要点（第 7、10 节）：
1. 休市时链上中间价（midPx）最容易被一笔盘外成交"插针"：SK 海力士永续 2 分钟跌约 18%、
   强平约 $5740 万就是这样。防御是"别只信中间价，拿它跟更稳的参考价比；偏离太大就判为插针，
   少信或不动手"，并"取偏离较小的那个价"（竞品硬规则第 10 节）。
2. 周末资金费率会很极端（Binance 白银永续周末年化约 56.7%、Hyperliquid 原油撞 ±5% 上限），
   极端资金费率 = 仓位太挤 = 回归动力更强，同时也是风险信号。

关于 Hyperliquid 给的两个价（对这些"外部休市"的股票/商品永续）：
- oraclePx：外部参考价。休市时基本冻结在周五外部收盘价——它是"锚"，不是周末的活价。
  所以我们的"偏离"信号 = 中间价 / 周五锚价 - 1，这是对的。
- markPx：链上标记价，由中间价 / 预言机 / 盘口冲击价做过阻尼和中位数处理，比纯 midPx 稳。
  所以周末真正能用来"去插针"的活参考是 markPx，而不是 oraclePx（oracle 是陈旧的锚）。

本模块只在本地用这些量做决策，不对外发布价格、不做基准生意（那是竞品的 B2B 产品，不是一个几千美元的 bot 该做的）。
"""
from __future__ import annotations


def annualized_funding_pct(hourly_rate: float | None) -> float | None:
    """把每小时资金费率（小数，如 0.0000625）换成年化百分比，便于和竞品引用的数字比较。"""
    if hourly_rate is None:
        return None
    return hourly_rate * 24 * 365 * 100.0


def robust_price(mid: float, mark: float | None, dislocation_bps: float) -> dict:
    """用更稳的标记价给中间价"去插针"。

    中间价和标记价差得越远，越像一笔插针/盘口脱节；此时信标记价（被阻尼过），不信中间价。
    返回 robust_px（去插针后的当前价）、mid_mark_gap_bps（中间价相对标记价的偏离）、dislocated（是否判为脱节）。
    """
    if not mark or mark <= 0 or not mid or mid <= 0:
        return {"robust_px": mid, "mid_mark_gap_bps": 0.0, "dislocated": False}
    gap = (mid / mark - 1) * 1e4
    dislocated = abs(gap) > dislocation_bps
    return {"robust_px": mark if dislocated else mid, "mid_mark_gap_bps": gap, "dislocated": dislocated}


def deviation_set(fri_px: float, mid: float, mark: float | None, oracle: float | None,
                  dislocation_bps: float) -> dict:
    """算出一组"偏离"：原始中间价偏离、标记价偏离、以及去插针后的稳健偏离（竞品"取偏离较小/更稳的那个"）。

    - dev_mid_bps：中间价 / 周五锚价 - 1（原来的 dev_bps，保留做对照）
    - dev_mark_bps：标记价 / 周五锚价 - 1（更稳）
    - fair_dev_bps：脱节时用标记价偏离，正常时用中间价偏离——这就是我们真正下注的"偏离"
    - mid_mark_gap_bps / dislocated：插针检测
    """
    if not fri_px or fri_px <= 0:
        return {"dev_mid_bps": None, "dev_mark_bps": None, "fair_dev_bps": None,
                "mid_mark_gap_bps": 0.0, "dislocated": False, "robust_px": mid}
    rp = robust_price(mid, mark, dislocation_bps)
    dev_mid = (mid / fri_px - 1) * 1e4
    dev_mark = (mark / fri_px - 1) * 1e4 if mark and mark > 0 else dev_mid
    fair_dev = (rp["robust_px"] / fri_px - 1) * 1e4
    return {"dev_mid_bps": dev_mid, "dev_mark_bps": dev_mark, "fair_dev_bps": fair_dev,
            "mid_mark_gap_bps": rp["mid_mark_gap_bps"], "dislocated": rp["dislocated"],
            "robust_px": rp["robust_px"]}


def wick_ratio(dev_series: list[float]) -> float:
    """插针比：周末偏离序列里"最极端的一刻"相对"典型值"的倍数。

    一笔插针会让 max_abs_dev 很大，但中位数仍小 → 比值很高 = 多半是瞬时插针，不是真脱节。
    """
    devs = [abs(d) for d in dev_series if d is not None]
    if len(devs) < 3:
        return 1.0
    s = sorted(devs)
    median = s[len(s) // 2]
    if median <= 0:
        return float("inf") if max(devs) > 0 else 1.0
    return max(devs) / median


def funding_carry_bps(side: int, hourly_rate: float | None, hold_hours: float) -> float:
    """持仓期间的资金费率盈亏（bps，站在我们这一侧）。

    资金费率为正时，多头付钱、空头收钱。我们做空（side<0）且费率为正 → 收钱（正的 carry）。
    """
    if not hourly_rate or hold_hours <= 0:
        return 0.0
    # 多头每小时付 hourly_rate；我们这一侧的收益 = -side * (-hourly_rate) ... 直接写清楚：
    # 多头(side>0)付费 → carry = -rate*hold；空头(side<0)收费 → carry = +rate*hold
    return (-side) * hourly_rate * hold_hours * 1e4
