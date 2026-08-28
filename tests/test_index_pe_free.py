# -*- coding: utf-8 -*-
"""创业板估值免费数据源回归测试。"""

import pandas as pd
import pytest
from datetime import datetime, timedelta
import json

from src.strategy import index_pe as ipe

pytestmark = pytest.mark.unit


def test_load_cy50_pe_does_not_call_paid_fallback(monkeypatch, tmp_path):
    """乐咕无数据时直接返回空估值，估值维度按设计降为 0。"""
    monkeypatch.setattr("akshare.stock_index_pe_lg",
                        lambda **_kwargs: pd.DataFrame())
    assert ipe.load_cy50_pe(cache_dir=tmp_path) == {}


def test_load_cy50_pe_refreshes_stale_cache(monkeypatch, tmp_path):
    """PE 缓存末根过期时必须重拉免费源，不得直接沿用旧估值。"""
    cache = tmp_path / "strategy_cache" / "cy50_pe_cache.json"
    cache.parent.mkdir(parents=True)
    stale_day = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    cache.write_text('{"rows": {"%s": 88.0}}' % stale_day, encoding="utf-8")
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr(
        "akshare.stock_index_pe_lg",
        lambda **_kwargs: pd.DataFrame({"日期": [fresh_day], "滚动市盈率": [22.5]}),
    )

    assert ipe.load_cy50_pe(cache_dir=tmp_path) == {fresh_day: 22.5}


def test_load_cy50_pe_rejects_stale_live_response(monkeypatch, tmp_path):
    """乐咕接口返回旧估值时不得缓存并参与当日信号。"""
    stale_day = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    monkeypatch.setattr(
        "akshare.stock_index_pe_lg",
        lambda **_kwargs: pd.DataFrame({"日期": [stale_day], "滚动市盈率": [22.5]}),
    )

    assert ipe.load_cy50_pe(cache_dir=tmp_path) == {}


def test_load_cy50_pe_filters_nonfinite_values_from_live_source(monkeypatch, tmp_path):
    """乐咕实时结果混入无穷 PE 时，只保留有限正数记录。"""
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    previous_day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(
        "akshare.stock_index_pe_lg",
        lambda **_kwargs: pd.DataFrame({"日期": [previous_day, fresh_day],
                                        "滚动市盈率": [float("inf"), 22.5]}),
    )

    assert ipe.load_cy50_pe(cache_dir=tmp_path) == {fresh_day: 22.5}


def test_load_cy50_pe_normalizes_timestamp_keys(monkeypatch, tmp_path):
    """PE 日期带时间部分时也必须与指数 YYYY-MM-DD 日期正确对齐。"""
    timestamp = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(
        "akshare.stock_index_pe_lg",
        lambda **_kwargs: pd.DataFrame({"日期": [timestamp], "滚动市盈率": [22.5]}),
    )

    out = ipe.load_cy50_pe(cache_dir=tmp_path)

    assert out == {timestamp.strftime("%Y-%m-%d"): 22.5}


def test_cache_fresh_rejects_future_date():
    """未来日期的 PE 不能证明当前估值已更新。"""
    future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    assert not ipe._cache_is_fresh({future: 22.5})


def test_pe_pctile_ignores_missing_values_without_zero_contamination():
    """对齐初期缺失 PE 应保持中性，不得伪造成 0 PE 参与滚动分位。"""
    cheap = ipe.pe_to_cheap_pctile([None, None, 10.0, 20.0])

    assert cheap == [0.5, 0.5, 0.5, 0.0]


def test_normalize_rows_rejects_nonfinite_pe():
    """无穷 PE 不能进入估值缓存或滚动分位。"""
    assert ipe._normalize_rows({"2026-08-27": float("inf")}) == {}


def test_load_cy50_pe_accepts_free_source_ttm_column_aliases(monkeypatch, tmp_path):
    """乐咕字段改为英文别名时，免费估值仍应保持同一 TTM 口径。"""
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr(
        "akshare.stock_index_pe_lg",
        lambda **_kwargs: pd.DataFrame({"date": [fresh_day], "ttmPe": [22.5]}),
    )

    assert ipe.load_cy50_pe(cache_dir=tmp_path) == {fresh_day: 22.5}


def test_load_cy50_pe_ignores_malformed_cache_and_uses_free_source(monkeypatch, tmp_path):
    """缓存 JSON 结构损坏时应重拉乐咕免费源，不得直接抛异常。"""
    cache = tmp_path / "strategy_cache" / "cy50_pe_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"rows": []}), encoding="utf-8")
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr(
        "akshare.stock_index_pe_lg",
        lambda **_kwargs: pd.DataFrame({"日期": [fresh_day], "滚动市盈率": [22.5]}),
    )

    assert ipe.load_cy50_pe(cache_dir=tmp_path) == {fresh_day: 22.5}
