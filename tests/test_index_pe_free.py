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


# ---------------- FIX-20260918-01：分位窗口必须排序后再取极值 ----------------
# 旧实现用未排序窗口的 w[-1]（上一交易日值，月频缓存下是 30 天前的 ffill 值）
# 当窗口最大值比较，把 98% 的回测日误判成"便宜度<0.10 极贵"（正确约 18%）。

def test_pe_pctile_monthly_ffill_not_below_010_when_below_window_max():
    """(a) 月频粒度（30 天间隔 ffill）当前 PE 低于窗口最大值 → cheap 不得 < 0.10。

    场景等价 2026-09 生产口径：窗口内早前有更高 PE，当前 PE 处于中位而非顶部，
    排序修复后应给出中性偏便宜的便宜度；旧实现因 w[-1]（昨日 ffill 值）== cur
    直接判成 0（极贵）。
    """
    monthly = [60.0, 55.0, 20.0, 25.0, 30.0, 40.0, 45.0]
    pe = [v for v in monthly for _ in range(30)]  # 月频值 30 天 ffill 近似

    cheap = ipe.pe_to_cheap_pctile(pe, span=500)

    assert cheap[-1] >= 0.10, f"低于窗口最大值却判极贵：{cheap[-1]}"


def test_pe_pctile_monotone_lower_cheap_for_higher_pe():
    """(b) 同一滚动窗内 PE 越高便宜度越低（单调非增，实测严格递减）。"""
    pe = [100.0, 50.0, 60.0, 70.0, 80.0, 90.0, 95.0, 99.0]
    cheap = ipe.pe_to_cheap_pctile(pe, span=500)

    for i in range(1, len(cheap) - 1):
        # PE 单调抬升（50→99）但均低于窗口最高点 100，便宜度必须逐点下降
        assert cheap[i] > cheap[i + 1], (
            f"PE 抬高时便宜度未下降：i={i} {cheap[i]} <= {cheap[i + 1]}")


def test_pe_pctile_20260917_caliber_cheap_approx_0638():
    """(c) 2026-09-17 真实口径（PE=30.23 处窗口 36% 分位）→ cheap≈0.638。

    用 500 条等价窗口构造秩分位：181 条低于 30.23 / 500 → p=0.362 → cheap=0.638。
    旧实现会因 w[-1] 比较把该点判成 0。
    """
    window = [30.0] * 181 + [31.0] * 319  # 500 条，rank(30.23)=0.362
    pe = window + [30.23]

    cheap = ipe.pe_to_cheap_pctile(pe, span=500)

    assert cheap[-1] == pytest.approx(0.638, abs=1e-3)
