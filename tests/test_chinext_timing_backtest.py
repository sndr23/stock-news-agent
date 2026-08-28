# -*- coding: utf-8 -*-
"""择时回测信息集与缓存回退口径测试。"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import run_chinext_timing as rct  # noqa: E402
from src.strategy import data as sdata  # noqa: E402

pytestmark = pytest.mark.unit


def test_backtest_uses_intraday_snapshot_for_decision(monkeypatch):
    """回测在 d 日评估时使用 d 日 14:45 快照（≈d 日收盘），而非 d-1 收盘（v5.1 口径）。

    背景（2026-08-28 用户拍板）：信号日 d 直接用 d 日快照（当日盘中价/量能）决策，
    让核心因子反映"当天到现在的走势"，对当日加减仓更有意义；15 分钟价差接受为近似。
    """
    dates = pd.bdate_range(end=pd.Timestamp("2026-08-27"), periods=65)
    df = pd.DataFrame({
        "close": [100.0 + i for i in range(len(dates))],
        "amount": [1e8] * len(dates),
    }, index=dates)
    observed_scores = []
    observed_lengths = []

    monkeypatch.setattr(rct.cf, "core_signals",
                        lambda *args, **kwargs: {})
    monkeypatch.setattr(rct.cf, "dimension_score",
                        lambda signals, weights: list(range(len(df))))
    monkeypatch.setattr(rct.cf, "defensive_state",
                        lambda closes, *args, **kwargs: (
                            observed_lengths.append(len(closes)) or {"cap": 1.0}))

    def fake_decide(score, cap, prev, tiers=None):
        observed_scores.append(score)
        return {"position": 0.0, "pending": None, "changed": False,
                "direction": "hold"}

    monkeypatch.setattr(rct.ct, "decide_position", fake_decide)

    rct.backtest_metrics(df)

    # v5.1：决策 d 用 comp[d]（含当日收盘，模拟 14:45 快照）；defensive_state 用 closes[:d+1]
    assert observed_scores[0] == 60
    assert observed_lengths[0] == 61


def test_backtest_excludes_current_partial_bar(monkeypatch):
    """回测输入含当天盘中 bar 时，结果窗口不得包含未收盘数据。"""
    dates = pd.bdate_range(end=pd.Timestamp(datetime.now().date()), periods=65)
    df = pd.DataFrame({
        "close": [100.0 + i for i in range(len(dates))],
        "amount": [1e8] * len(dates),
    }, index=dates)

    metrics = rct.backtest_metrics(df)

    assert metrics["dates"][-1].date() < datetime.now().date()


def test_backtest_rejects_insufficient_history():
    """历史长度不足以完成 60 日 warmup 时返回可操作的输入错误。"""
    dates = pd.bdate_range(end=pd.Timestamp(datetime.now().date() - pd.Timedelta(days=1)),
                           periods=60)
    df = pd.DataFrame({"close": [100.0] * len(dates),
                       "amount": [1e8] * len(dates)}, index=dates)

    with pytest.raises(ValueError, match="至少需要 62 根完整日线"):
        rct.backtest_metrics(df)


def test_index_sina_uses_fresh_bar_when_file_ttl_is_expired(monkeypatch):
    """末根 bar 新鲜时不应因缓存文件超过 TTL 而丢弃全量历史。"""
    fresh = pd.DataFrame({"close": [1.0], "amount": [1e8]},
                         index=[pd.Timestamp(datetime.now().date())])
    calls = []

    def fake_cache(key, ttl_days=None):
        calls.append(ttl_days)
        return fresh

    monkeypatch.setattr(sdata, "_cache_get", fake_cache)

    assert sdata.load_index_sina("399006", datalen=10) is fresh
    assert calls == [None]


def test_index_daily_full_uses_fresh_bar_when_file_ttl_is_expired(monkeypatch):
    """增量缓存也应按末根交易日判定，避免恢复缓存的文件时间误伤历史深度。"""
    fresh = pd.DataFrame({"close": [1.0], "amount": [1e8]},
                         index=[pd.Timestamp(datetime.now().date())])
    calls = []

    def fake_cache(key, ttl_days=None):
        calls.append(ttl_days)
        return fresh

    monkeypatch.setattr(sdata, "_cache_get", fake_cache)
    monkeypatch.setattr(sdata, "_fetch_index_full_frame",
                        lambda *a, **k: pd.DataFrame())

    result = sdata.load_index_daily_full("399006", "20200101")
    pd.testing.assert_frame_equal(result, fresh)
    assert calls == [None]
