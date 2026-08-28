# -*- coding: utf-8 -*-
"""两融因子日期对齐、无前视与异常免费响应回归测试。"""
import pickle

import pandas as pd
import pytest

from src.strategy import margin_data as md
from src.strategy.margin_data import build_leverage


pytestmark = pytest.mark.unit


def test_build_leverage_uses_previous_trading_day_balance():
    """t 日因子只能使用 t-1 及更早的两融余额。"""
    dates = [f"2026-08-{day:02d}" for day in range(1, 36)]
    margin = {day: 100.0 for day in dates}
    margin[dates[32]] = 200.0
    margin[dates[33]] = 100.0

    changed_after_signal = dict(margin)
    changed_after_signal[dates[34]] = 500.0

    out = build_leverage(margin, dates, span=1)
    out_with_current_day = build_leverage(changed_after_signal, dates, span=1)

    # 第 35 日的余额是在该日之后披露，改变它不能改变第 35 日因子。
    assert out[-1] == out_with_current_day[-1]


@pytest.mark.parametrize("response", [[], {"unexpected": "payload"}])
def test_fetch_margin_ignores_non_dataframe_source_response(
        monkeypatch, tmp_path, response):
    """两融免费接口返回非 DataFrame 时应降级为空而不是抛异常。"""
    monkeypatch.setattr(md, "CACHE_PATH", tmp_path / "margin.pkl")
    monkeypatch.setattr(md, "_segments", lambda: [("20260101", "20261231")])
    import akshare as ak
    monkeypatch.setattr(ak, "stock_margin_sse", lambda **kwargs: response)

    assert md.fetch_margin(force_refresh=True) == {}


def test_fetch_margin_ignores_response_missing_date_column(monkeypatch, tmp_path):
    """两融响应缺少日期列时应跳过该批次，不在逐行解析处 KeyError。"""
    monkeypatch.setattr(md, "CACHE_PATH", tmp_path / "margin.pkl")
    monkeypatch.setattr(md, "_segments", lambda: [("20260101", "20261231")])
    import akshare as ak
    monkeypatch.setattr(ak, "stock_margin_sse",
                        lambda **kwargs: pd.DataFrame({"融资余额": [100.0]}))

    assert md.fetch_margin(force_refresh=True) == {}


def test_fetch_margin_rejects_malformed_cache_root(monkeypatch, tmp_path):
    """两融缓存根节点损坏时应重走免费源，不把任意 pickle 对象当余额映射。"""
    cache = tmp_path / "margin.pkl"
    with cache.open("wb") as fh:
        pickle.dump(["bad"], fh)
    monkeypatch.setattr(md, "CACHE_PATH", cache)
    monkeypatch.setattr(md, "_segments", lambda: [])

    assert md.fetch_margin() == {}
