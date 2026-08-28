# -*- coding: utf-8 -*-
"""业绩预告免费数据源与日期对齐回归测试。"""
import pickle

import pandas as pd
import pytest

from src.strategy import event_data as ed

pytestmark = pytest.mark.unit


def test_fetch_events_ignores_non_dataframe_source_response(monkeypatch, tmp_path):
    """业绩预告接口返回非 DataFrame 时应跳过季度，不抛 AttributeError。"""
    monkeypatch.setattr(ed, "CACHE_PATH", tmp_path / "event.pkl")
    monkeypatch.setattr(ed, "_quarter_ends", lambda: ["20260630"])
    import akshare as ak
    monkeypatch.setattr(ak, "stock_yjyg_em", lambda **kwargs: [])

    assert ed.fetch_events(force_refresh=True) == []


def test_fetch_events_rejects_malformed_cache_root(monkeypatch, tmp_path):
    """事件缓存根节点损坏时应重建，不把任意对象返回给下游。"""
    cache = tmp_path / "event.pkl"
    with cache.open("wb") as fh:
        pickle.dump({"bad": "payload"}, fh)
    monkeypatch.setattr(ed, "CACHE_PATH", cache)
    monkeypatch.setattr(ed, "_quarter_ends", lambda: [])

    assert ed.fetch_events() == []


def test_build_sentiment_aligns_pandas_timestamp_trading_dates():
    """交易日为 pandas.Timestamp 时，公告日期仍应正确对齐。"""
    dates = pd.bdate_range("2026-01-01", periods=30)
    event_day = dates[20].strftime("%Y-%m-%d")

    out = ed.build_sentiment([(event_day, "300001", 1)], dates, window=1)

    assert out[20] > 0
