"""免费数据新鲜度判断回归测试。"""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from src.strategy import data_freshness
from src.strategy.data_freshness import _is_workday, is_recent_data_date

pytestmark = pytest.mark.unit


def test_recent_data_rejects_nat_without_raising():
    """无效日期不能被当成新鲜数据，也不能打断降级链。"""
    assert not is_recent_data_date(pd.NaT)


def test_recent_data_counts_workdays_across_weekend():
    """周末不应额外消耗行情新鲜度窗口。"""
    friday = date(2026, 8, 21)
    monday = date(2026, 8, 24)

    assert is_recent_data_date(friday, max_lag_days=1, today=monday)
    assert not is_recent_data_date(friday, max_lag_days=0, today=monday)


def test_cn_calendar_excludes_makeup_saturday_from_a_share_workdays():
    """法定补班周六仍不是 A 股交易日。"""
    makeup_saturday = date(2026, 10, 10)

    assert not _is_workday(makeup_saturday, "cn")
    assert is_recent_data_date(
        date(2026, 10, 9), max_lag_days=0, calendar="cn",
        today=makeup_saturday,
    )


def test_default_freshness_today_uses_beijing_date_on_utc_boundary(monkeypatch):
    """UTC 云端北京时间凌晨不应把当前 A 股日期误判为未来。"""
    real_datetime = datetime

    class _FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            instant = real_datetime(2026, 8, 27, 16, 30,
                                    tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(data_freshness, "datetime", _FakeDateTime)

    assert is_recent_data_date("2026-08-28", max_lag_days=0, calendar="cn")
