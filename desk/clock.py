"""Weekend windows in US/Eastern, returned as UTC datetimes.

一个"周末" = 周五外部价格停止 → 周日外部价格恢复。以周五日期作为周末编号（备忘 7.6：实验编号/周末）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Weekend:
    friday: date
    close: datetime        # 周五外部价停止（UTC）
    news_start: datetime   # 抽取窗口起点（UTC）
    decision: datetime     # 决策时刻（UTC）
    resume: datetime       # 外部价恢复（UTC）
    exit: datetime         # 模拟平仓时刻（UTC）

    @property
    def wid(self) -> str:
        return self.friday.isoformat()


def weekend_for_friday(friday: date, cfg: dict) -> Weekend:
    tz = ZoneInfo(cfg["clock"]["timezone"])
    c = cfg["clock"]
    close = datetime.combine(friday, _hm(c["friday_close"]), tz)
    news_start = datetime.combine(friday, _hm(cfg["llm"]["window_start"]), tz)
    sunday = friday + timedelta(days=2)
    resume = datetime.combine(sunday, _hm(c["resume"]), tz)
    decision = resume - timedelta(minutes=c["decision_lead_min"])
    exit_ = resume + timedelta(minutes=c["exit_delay_min"])
    return Weekend(friday, close.astimezone(UTC), news_start.astimezone(UTC),
                   decision.astimezone(UTC), resume.astimezone(UTC), exit_.astimezone(UTC))


def current_or_next_weekend(now: datetime, cfg: dict) -> Weekend:
    """The weekend whose resume time has not passed yet (relative to `now`)."""
    tz = ZoneInfo(cfg["clock"]["timezone"])
    local = now.astimezone(tz).date()
    # most recent Friday on or before today
    fri = local - timedelta(days=(local.weekday() - 4) % 7)
    w = weekend_for_friday(fri, cfg)
    if now > w.exit + timedelta(hours=1):
        w = weekend_for_friday(fri + timedelta(days=7), cfg)
    return w


def last_completed_weekend(now: datetime, cfg: dict) -> Weekend:
    w = current_or_next_weekend(now, cfg)
    if now >= w.exit:
        return w
    return weekend_for_friday(w.friday - timedelta(days=7), cfg)


def to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)
