# -*- coding: utf-8 -*-
"""V6 研究指标、连续仓位映射和因子增量 IC 测试。"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.research import (  # noqa: E402
    DEFAULT_POSITION_BANDS,
    evaluate_position_path,
    factor_ic_report,
    performance_metrics,
    score_to_continuous_position,
)

pytestmark = pytest.mark.unit


def test_score_to_continuous_position_interpolates_and_clamps():
    assert score_to_continuous_position(-1.0) == pytest.approx(0.0)
    assert score_to_continuous_position(-0.4) == pytest.approx(0.15)
    assert score_to_continuous_position(-0.2) == pytest.approx(0.45)
    assert score_to_continuous_position(0.0) == pytest.approx(0.70)
    assert score_to_continuous_position(0.3) == pytest.approx(0.95)
    assert score_to_continuous_position(0.8) == pytest.approx(1.0)


def test_score_to_continuous_position_rejects_non_finite_score():
    with pytest.raises(ValueError, match="score"):
        score_to_continuous_position(float("nan"))


def test_performance_metrics_reports_capture_and_path_quality():
    strategy = [0.10, -0.05, 0.00, 0.02]
    benchmark = [0.20, -0.10, 0.01, 0.01]
    positions = [1.0, 0.5, 0.0, 0.25]

    result = performance_metrics(strategy, benchmark, positions=positions)

    assert result["total"] == pytest.approx(1.10 * 0.95 * 1.02 - 1.0)
    assert result["mdd"] == pytest.approx(-0.05)
    assert result["bull_capture"] == pytest.approx(0.12 / 0.22)
    assert result["bear_capture"] == pytest.approx(0.5)
    assert result["hit_rate"] == pytest.approx(0.5)
    assert result["profit_factor"] == pytest.approx(2.4)
    assert result["avg_pos"] == pytest.approx(0.4375)
    assert result["turnover"] == pytest.approx(2.25)
    assert result["ulcer"] > 0.0


def test_evaluate_position_path_charges_fee_on_position_change():
    result = evaluate_position_path(
        [100.0, 110.0, 100.0],
        [1.0, 0.0],
        fee=0.01,
    )

    expected = (1.0 - 0.01) * 1.10 * (1.0 - 0.01)
    assert result["total"] == pytest.approx(expected - 1.0)
    assert result["switches"] == 2
    assert result["turnover"] == pytest.approx(2.0)


def test_factor_ic_report_exposes_tail_and_incremental_gate():
    n = 16
    closes = [100.0]
    for i in range(n - 1):
        closes.append(closes[-1] * (1.0 + 0.001 * (i + 1)))
    forward = [closes[i + 1] / closes[i] - 1.0 for i in range(n - 1)]
    signals = {name: [0.0] * n for name in (
        "trend_ma20_60", "trend_momentum_60", "volprice_quadrant",
        "volprice_amihud", "vol_regime", "vol_term", "value_erp",
        "pullback_52w", "dd60",
    )}
    signals["trend_ma20_60"] = forward + [0.0]
    weights = {
        "趋势": 1.0,
        "量价": 0.0,
        "波动": 0.0,
        "估值": 0.0,
        "落袋": 0.0,
    }

    report = factor_ic_report(
        signals,
        closes,
        weights=weights,
        start=0,
        tail_days=6,
        horizons=(1,),
        min_samples=3,
    )
    item = report["factors"]["trend_ma20_60"]["next_ret"]

    assert item["full"]["n"] == 15
    assert item["full"]["ic"] > 0.99
    assert item["tail"]["ic"] > 0.99
    assert item["incremental_full"]["ic"] > 0.99
    assert item["eligible"] is True

    assert report["factors"]["value_erp"]["next_ret"]["eligible"] is False
    assert DEFAULT_POSITION_BANDS[-1] == (0.40, 1.0)
