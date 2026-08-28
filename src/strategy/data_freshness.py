"""免费行情/估值序列的新鲜度判断。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

BJT = timezone(timedelta(hours=8))


def _is_workday(day: date, calendar: str) -> bool:
    """按指定日历判断工作日；中国日历按 A 股交易日语义处理。"""
    if calendar == "cn":
        try:
            import chinese_calendar

            # 法定补班日可能落在周六/周日，但 A 股周末不开市；
            # 只接受周一至周五且不是法定节假日的日期。
            return day.weekday() < 5 and bool(chinese_calendar.is_workday(day))
        except (ImportError, AttributeError, TypeError, ValueError):
            pass
    return day.weekday() < 5


def is_recent_data_date(value, max_lag_days: int = 3,
                        calendar: str = "weekday", today: date | None = None) -> bool:
    """判断序列末日期是否在最近 N 个工作日内，且不晚于当前日期。

    ``max_lag_days`` 按工作日计数，能够覆盖周末和中国法定长假；未来日期
    一律拒绝，避免测试数据或异常接口响应冒充最新行情。
    """
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return False
    if pd.isna(timestamp):
        return False
    last = timestamp.date()
    if today is None:
        today = datetime.now(BJT).date()
    if last > today or max_lag_days < 0:
        return False

    lag = 0
    cursor = last
    while cursor < today:
        cursor += timedelta(days=1)
        if _is_workday(cursor, calendar):
            lag += 1
    return lag <= max_lag_days
