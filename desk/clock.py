"""Weekend windows, returned as UTC datetimes.

一个"周末" = 该合约的外部价格停止 → 外部价格恢复。以周五日期作为周末编号（备忘 7.6：实验编号/周末）；
多合约时编号为 "周五日期|合约名"，例如 "2026-10-09|NVDA"。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
DAYS = {"FRI": 0, "SAT": 1, "SUN": 2, "MON": 3}


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Weekend:
    friday: date
    close: datetime        # 外部价停止（UTC）
    news_start: datetime   # 抽取窗口起点（UTC）
    decision: datetime     # 决策时刻（UTC）
    resume: datetime       # 外部价恢复（UTC）
    exit: datetime         # 模拟平仓时刻（UTC）
    coin: str = ""
    name: str = ""

    @property
    def wid(self) -> str:
        return f"{self.friday.isoformat()}|{self.name}" if self.name else self.friday.isoformat()


def weekend_for_friday(friday: date, cfg, inst: dict | None = None) -> Weekend:
    et = ZoneInfo(cfg["clock"]["timezone"])
    c = cfg["clock"]
    news_default = datetime.combine(friday, _hm(cfg["llm"]["window_start"]), et)
    if inst and "tz" in inst:
        tz = ZoneInfo(inst["tz"])
        close = datetime.combine(friday + timedelta(days=DAYS[inst["close_day"]]), _hm(inst["close"]), tz)
        resume = datetime.combine(friday + timedelta(days=DAYS[inst["resume_day"]]), _hm(inst["resume"]), tz)
        coin, name = inst["coin"], inst["name"]
    else:
        close = datetime.combine(friday, _hm(c["friday_close"]), et)
        resume = datetime.combine(friday + timedelta(days=2), _hm(c["resume"]), et)
        coin = inst["coin"] if inst else ""
        name = inst["name"] if inst else ""
    news_start = min(close, news_default)
    decision = resume - timedelta(minutes=c["decision_lead_min"])
    exit_ = resume + timedelta(minutes=c["exit_delay_min"])
    return Weekend(friday, close.astimezone(UTC), news_start.astimezone(UTC), decision.astimezone(UTC),
                   resume.astimezone(UTC), exit_.astimezone(UTC), coin, name)


def current_or_next_weekend(now: datetime, cfg, inst: dict | None = None) -> Weekend:
    """The weekend whose exit has not passed by more than an hour (relative to `now`)."""
    tz = ZoneInfo(cfg["clock"]["timezone"])
    local = now.astimezone(tz).date()
    fri = local - timedelta(days=(local.weekday() - 4) % 7)
    w = weekend_for_friday(fri, cfg, inst)
    if now > w.exit + timedelta(hours=1):
        w = weekend_for_friday(fri + timedelta(days=7), cfg, inst)
    return w


def last_completed_weekend(now: datetime, cfg, inst: dict | None = None) -> Weekend:
    w = current_or_next_weekend(now, cfg, inst)
    if now >= w.exit:
        return w
    return weekend_for_friday(w.friday - timedelta(days=7), cfg, inst)


def friday_of(wid: str) -> str:
    return wid.split("|")[0]


def name_of(wid: str, default: str = "") -> str:
    return wid.split("|")[1] if "|" in wid else default


def to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)
