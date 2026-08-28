# -*- coding: utf-8 -*-
"""walk-forward 验证窗口与统计口径测试。"""
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import run_chinext_timing as rct  # noqa: E402
from scripts import walk_forward_validation as wfv  # noqa: E402
from scripts.walk_forward_validation import evaluate_test, summarize_oos  # noqa: E402

pytestmark = pytest.mark.unit


def _df(n=420, returns=None):
    dates = pd.bdate_range(end=pd.Timestamp("2026-08-26"), periods=n)
    closes = [100.0]
    for i in range(1, n):
        closes.append(closes[-1] * (1.0 + (returns[i - 1] if returns else 0.001)))
    return pd.DataFrame({"close": closes, "amount": [1e8] * n}, index=dates)


def _always_long(monkeypatch):
    monkeypatch.setattr(rct.cf, "core_signals", lambda *args, **kwargs: {})
    monkeypatch.setattr(rct.cf, "dimension_score",
                        lambda signals, weights: [1.0] * len(signals.get("unused", []))
                        if isinstance(signals, dict) and "unused" in signals else None)

    # The score sequence is supplied directly by the replacement below; using a
    # sequence sized from the input keeps the test independent of factor details.
    def scores(signals, weights):
        return [1.0] * 1000

    monkeypatch.setattr(rct.cf, "dimension_score", scores)
    monkeypatch.setattr(rct.cf, "defensive_state",
                        lambda closes, *args, **kwargs: {"cap": 1.0})
    monkeypatch.setattr(
        rct.ct, "decide_position",
        lambda score, cap, prev, tiers=None: {
            "position": 1.0,
            "pending": None,
            "changed": float((prev or {}).get("position") or 0.0) != 1.0,
            "direction": "up" if float((prev or {}).get("position") or 0.0) < 1.0 else "hold",
        },
    )


def test_backtest_metrics_can_measure_only_requested_window(monkeypatch):
    """窗口回测的收益和基准都必须从测试起点开始计算。"""
    _always_long(monkeypatch)
    df = _df()

    start, end = 300, 340
    metrics = rct.backtest_metrics(df, eval_start=start, eval_end=end,
                                   initial_prev={"position": 1.0, "pending": None})

    expected = df.iloc[end].close / df.iloc[start].close - 1.0
    assert metrics["n_navs"] == end - start
    assert metrics["total"] == pytest.approx(expected)
    assert metrics["bh"] == pytest.approx(expected)


def test_backtest_max_drawdown_includes_first_evaluated_loss(monkeypatch):
    """第一天从净值 1.0 下跌时，最大回撤不能被统计成 0。"""
    _always_long(monkeypatch)
    returns = [0.0] * 60 + [-0.10] + [0.0] * 10
    df = _df(n=len(returns) + 1, returns=returns)

    metrics = rct.backtest_metrics(df, eval_start=60, eval_end=62,
                                   initial_prev={"position": 1.0, "pending": None})

    assert metrics["mdd"] == pytest.approx(-0.10)
    assert metrics["bh_mdd"] == pytest.approx(-0.10)


def test_backtest_daily_returns_match_net_asset_value_after_fee(monkeypatch):
    """带费用时，日收益序列复合结果必须与净值相同。"""
    _always_long(monkeypatch)
    returns = [0.0] * 60 + [-0.10] + [0.0] * 10
    df = _df(n=len(returns) + 1, returns=returns)

    metrics = rct.backtest_metrics(df, fee=0.10, eval_start=60, eval_end=61)

    assert metrics["daily_rets"][0] == pytest.approx(-0.19)
    assert metrics["total"] == pytest.approx(-0.19)


def test_backtest_forward_diagnostics_stop_at_evaluation_window(monkeypatch):
    """OOS 折的减仓后 10 日诊断不能读取测试段之外的收益。"""
    monkeypatch.setattr(rct.cf, "core_signals", lambda *args, **kwargs: {})
    monkeypatch.setattr(rct.cf, "dimension_score", lambda signals, weights: [0.0] * 1000)
    monkeypatch.setattr(rct.cf, "defensive_state",
                        lambda closes, *args, **kwargs: {"cap": 1.0})
    monkeypatch.setattr(
        rct.ct, "decide_position",
        lambda score, cap, prev, tiers=None: {
            "position": 0.0,
            "pending": None,
            "changed": True,
            "direction": "down",
        },
    )
    df = _df(n=100)

    metrics = rct.backtest_metrics(
        df, eval_start=60, eval_end=61,
        initial_prev={"position": 1.0, "pending": None},
    )

    assert metrics["n_down"] == 0


def test_evaluate_test_carries_training_state_and_uses_long_lookback(monkeypatch):
    """OOS 段继承训练末状态，并保留足够历史计算 52 周因子。"""
    _always_long(monkeypatch)
    df = _df()
    test = (300, 340)

    metrics = evaluate_test(df, test, (rct.ct.TIERS, False), None, 0.0)

    expected = df.iloc[test[1]].close / df.iloc[test[0]].close - 1.0
    assert metrics["n_navs"] == test[1] - test[0]
    assert metrics["avg_pos"] == pytest.approx(1.0)
    assert metrics["total"] == pytest.approx(expected)


def test_evaluate_test_uses_supplied_state_at_test_boundary(monkeypatch):
    """连续 OOS 拼接时，测试段首日必须承接上一折的状态。"""
    _always_long(monkeypatch)
    observed = []

    def decide(score, cap, prev, tiers=None):
        observed.append(prev["position"])
        return {"position": prev["position"], "pending": prev.get("pending"),
                "changed": False, "direction": "hold"}

    monkeypatch.setattr(rct.ct, "decide_position", decide)
    evaluate_test(
        _df(), (300, 340), (rct.ct.TIERS, False), None, 0.0,
        initial_prev={"position": 0.6, "pending": None},
    )

    assert observed[0] == pytest.approx(0.6)


def test_best_on_train_keeps_history_before_training_window(monkeypatch):
    """训练折只评估指定区间，但因子计算要保留区间前历史。"""
    df = _df(500)
    calls = []

    def fake_backtest(frame, **kwargs):
        calls.append((len(frame), kwargs))
        return {"calmar": 1.0}

    monkeypatch.setattr(wfv, "backtest_metrics", fake_backtest)
    wfv.best_on_train(df, (200, 400), None, 0.0)

    assert len(calls) == len(wfv.TIER_CANDIDATES) * len(wfv.ERP_CANDIDATES)
    assert all(length == 400 for length, _ in calls)
    assert all(kwargs["eval_start"] == 200 for _, kwargs in calls)
    assert all(kwargs["eval_end"] == 399 for _, kwargs in calls)


def test_summarize_oos_uses_compound_curve_and_buy_hold_benchmark():
    """OOS 汇总应复合各连续测试段，并按复合曲线计算回撤和夏普。"""
    rows = [
        {"total": 0.10, "bh": 0.20, "n_navs": 2, "sharpe": 0.5, "calmar": 0.2,
         "navs": [1.10, 1.10], "bh_navs": [1.20, 1.20],
         "daily_rets": [0.10, 0.0]},
        {"total": -0.10, "bh": -0.05, "n_navs": 2, "sharpe": -0.5, "calmar": -0.2,
         "navs": [0.99, 0.90], "bh_navs": [0.95, 0.95],
         "daily_rets": [-0.10, 0.0]},
    ]

    summary = summarize_oos(rows)

    assert summary["total"] == pytest.approx(-0.01)
    assert summary["bh"] == pytest.approx(0.14)
    assert summary["mdd"] == pytest.approx(-0.10)
    assert summary["bh_mdd"] == pytest.approx(-0.05)
    assert summary["n_navs"] == 4
