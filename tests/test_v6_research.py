# -*- coding: utf-8 -*-
"""V6 研究 CLI 的纯内存回归。"""
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import run_v6_research as v6  # noqa: E402

pytestmark = pytest.mark.unit


def _daily(n=70):
    dates = pd.bdate_range("2026-01-05", periods=n)
    return pd.DataFrame(
        {"close": [100.0 + i for i in range(n)],
         "amount": [1000.0 + i for i in range(n)]},
        index=dates,
    )


def test_build_position_paths_returns_production_and_continuous_paths():
    daily = _daily()

    result = v6.build_position_paths(daily)

    assert result["start"] == 60
    assert len(result["scores"]) == len(daily)
    assert len(result["production"]) == len(daily) - 1
    assert len(result["continuous"]) == len(daily) - 1
    assert all(0.0 <= value <= 1.0 for value in result["continuous"])


def test_format_factor_report_shows_raw_incremental_and_verdict():
    report = {
        "gate_ic": 0.05,
        "min_samples": 10,
        "factors": {
            "trend_ma20_60": {
                "next_ret": {
                    "full": {"ic": 0.123456, "n": 30},
                    "tail": {"ic": 0.101234, "n": 12},
                    "incremental_full": {"ic": 0.080001, "n": 30},
                    "incremental_tail": {"ic": 0.070001, "n": 12},
                    "eligible": True,
                    "reason": "通过",
                }
            }
        }
    }

    output = v6.format_factor_report(report)

    assert "trend_ma20_60" in output
    assert "0.123" in output
    assert "增量" in output
    assert "通过" in output


def test_format_metrics_formats_capture_rates_as_percent():
    metrics = {
        "total": 0.1,
        "cagr": 0.05,
        "sharpe": 0.7,
        "sortino": 1.0,
        "mdd": -0.2,
        "calmar": 0.25,
        "ulcer": 0.1,
        "hit_rate": 0.5,
        "profit_factor": 1.2,
        "bull_capture": 0.381,
        "bear_capture": 0.358,
    }

    output = v6._format_metrics("test", metrics)

    assert "牛捕获 38.1%" in output
    assert "熊捕获 35.8%" in output
