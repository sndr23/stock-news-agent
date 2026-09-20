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


def test_cli_backtest_disables_erp_filter(monkeypatch):
    """CLI 回测必须与生产口径一致：不启用 ERP（不加载 pe_map、erp_cap 关闭）。

    FIX-20260918-01：ERP 滤波整体下线，CLI 不再加载估值源；v5.2 生产口径同为 ERP OFF。
    """
    dates = pd.bdate_range(end=pd.Timestamp(datetime.now().date() - pd.Timedelta(days=1)),
                           periods=65)
    frame = pd.DataFrame({
        "close": [100.0 + i for i in range(len(dates))],
        "amount": [1e8] * len(dates),
    }, index=dates)
    seen = {}

    monkeypatch.setattr(rct, "_load_local_env", lambda: None)
    monkeypatch.setattr(rct, "load_index_sina", lambda *args, **kwargs: frame.copy())
    monkeypatch.setattr(rct, "_append_intraday_bar_if_needed", lambda df, symbol: df)
    monkeypatch.setattr(rct.ipe, "load_cy50_pe", lambda *args, **kwargs: None)

    def fake_run_backtest(*args, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(rct, "run_backtest", fake_run_backtest)
    monkeypatch.setattr(sys, "argv", ["run_chinext_timing.py", "--backtest"])

    rct.main()

    assert seen.get("erp_cap", False) is False  # 未启用 ERP 滤波
    assert "pe_map" not in seen  # 不加载估值源（FIX-20260918-01）


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


# ============================================================
# 择时质量周报测试（对齐 chinext_timing_backtest.py）
# ============================================================
import random as _random  # noqa: E402

from scripts.chinext_timing_backtest import (  # noqa: E402
    _spearman, _rank, _max_drawdown, score_to_tier,
    compute_ic, compute_stratification, compute_decision_audit,
    compute_miss_avoid, compute_factor_ics, build_report,
)


pytestmark = pytest.mark.unit


def _make_history(n=30, seed=42):
    """生成合成 history，含仓位变动、部分 r5/r10 置 None"""
    rng = _random.Random(seed)
    history = []
    position = 0.0
    nav = 1.0
    bh_nav = 1.0
    for i in range(n):
        day_num = 24 + i
        if day_num <= 31:
            date = f"2026-08-{day_num:02d}"
        else:
            date = f"2026-09-{day_num - 31:02d}"
        score = rng.uniform(-0.5, 0.5)
        # 仓位变动：第 5 日升至 0.6，第 15 日升至 1.0，第 25 日降至 0.0
        if i == 5:
            position = 0.6
        elif i == 15:
            position = 1.0
        elif i == 25:
            position = 0.0
        next_ret = rng.gauss(0, 1.5)
        r3 = next_ret + rng.gauss(0, 0.5) if i < n - 3 else None
        r5 = next_ret * 1.5 + rng.gauss(0, 0.8) if i < n - 5 else None
        r10 = next_ret * 2.0 + rng.gauss(0, 1.0) if i < n - 10 else None
        nav *= (1 + next_ret / 100 * position)
        bh_nav *= (1 + next_ret / 100)
        history.append({
            "date": date,
            "score": round(score, 3),
            "core": round(score * 0.8, 3),
            "basis": round(rng.uniform(-0.1, 0.1), 3),
            "flow": round(rng.uniform(-0.1, 0.1), 3),
            "mood": round(rng.uniform(-0.08, 0.08), 3),
            "news": round(rng.uniform(-0.15, 0.15), 3),
            "chan": round(rng.uniform(-0.1, 0.1), 3),
            "stock": round(rng.uniform(-0.1, 0.1), 3),
            "position": position,
            "prev_pos": history[-1]["position"] if history else 0.0,
            "next_ret": round(next_ret, 4),
            "r3": round(r3, 4) if r3 is not None else None,
            "r5": round(r5, 4) if r5 is not None else None,
            "r10": round(r10, 4) if r10 is not None else None,
            "nav": round(nav, 6),
            "bh_nav": round(bh_nav, 6),
        })
    return history


def test_spearman_perfect_positive():
    assert _spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_spearman_perfect_negative():
    assert _spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_too_few():
    assert _spearman([1, 2], [3, 4]) == 0.0


def test_rank_basic():
    assert _rank([3, 1, 2]) == [3.0, 1.0, 2.0]


def test_rank_ties():
    result = _rank([2, 2, 1])
    assert result[0] == 2.5
    assert result[1] == 2.5
    assert result[2] == 1.0


def test_max_drawdown_basic():
    nav = [1.0, 1.1, 0.8, 0.9, 1.0]
    assert _max_drawdown(nav) == pytest.approx(-27.27, abs=0.01)


def test_max_drawdown_empty():
    assert _max_drawdown([]) == 0.0


def test_score_to_tier():
    assert score_to_tier(0.5) == 1.0
    assert score_to_tier(0.4) == 1.0
    assert score_to_tier(0.30) == 1.0
    assert score_to_tier(0.0) == 0.9
    assert score_to_tier(-0.15) == 0.9
    assert score_to_tier(-0.25) == 0.9
    assert score_to_tier(-0.2) == 0.9
    assert score_to_tier(-0.3) == 0.6
    assert score_to_tier(-0.5) == 0.0


def test_compute_ic_basic():
    history = _make_history(30)
    result = compute_ic(history)
    assert result["n"] > 0
    assert -1 <= result["ic"] <= 1


def test_compute_ic_insufficient():
    history = _make_history(2)
    result = compute_ic(history)
    assert result["ic"] is None


def test_compute_stratification_structure():
    history = _make_history(30)
    result = compute_stratification(history)
    assert result["n"] > 0
    assert "layers" in result
    assert "direction" in result


def test_compute_decision_audit():
    history = _make_history(30)
    audits = compute_decision_audit(history)
    assert len(audits) >= 4  # 首日 + 3 次变动
    assert audits[0]["date"] == history[0]["date"]


def test_decision_audit_avoid_correct():
    """减仓后下跌=正确避险"""
    history = [
        {"date": "2026-08-24", "score": 0.5, "position": 1.0, "next_ret": 1.0,
         "r3": -1.0, "r5": -2.0, "r10": -3.0, "nav": 1.0, "bh_nav": 1.0, "prev_pos": 1.0},
        {"date": "2026-08-25", "score": -0.5, "position": 0.0, "next_ret": -1.0,
         "r3": -2.0, "r5": -3.0, "r10": -4.0, "nav": 1.0, "bh_nav": 0.99, "prev_pos": 1.0},
    ]
    audits = compute_decision_audit(history)
    assert audits[1]["verdict"] == "正确避险"


def test_decision_audit_miss_verdict():
    """减仓后上涨=踏空"""
    history = [
        {"date": "2026-08-24", "score": 0.5, "position": 1.0, "next_ret": 1.0,
         "r3": 1.0, "r5": 2.0, "r10": 3.0, "nav": 1.0, "bh_nav": 1.0, "prev_pos": 1.0},
        {"date": "2026-08-25", "score": -0.5, "position": 0.0, "next_ret": 1.0,
         "r3": 2.0, "r5": 3.0, "r10": 4.0, "nav": 1.0, "bh_nav": 1.01, "prev_pos": 1.0},
    ]
    audits = compute_decision_audit(history)
    assert audits[1]["verdict"] == "踏空（减仓后上涨）"


def test_decision_audit_pending():
    """收益未回填时应为待验证"""
    history = [
        {"date": "2026-08-24", "score": 0.5, "position": 1.0, "next_ret": 1.0,
         "r3": None, "r5": None, "r10": None, "nav": 1.0, "bh_nav": 1.0, "prev_pos": 1.0},
    ]
    audits = compute_decision_audit(history)
    assert audits[0]["verdict"] == "待验证（收益未回填）"


def test_compute_miss_avoid():
    history = _make_history(30)
    result = compute_miss_avoid(history)
    assert "total_contrib" in result
    assert "nav_final" in result
    assert "bh_final" in result
    assert "diff_pp" in result
    assert "nav_mdd" in result
    assert "bh_mdd" in result


def test_compute_miss_avoid_empty():
    result = compute_miss_avoid([])
    assert result["total_contrib"] == 0.0
    assert result["nav_final"] is None


def test_compute_factor_ics():
    history = _make_history(30)
    result = compute_factor_ics(history)
    assert len(result) == 7
    for factor in ("core", "basis", "flow", "mood", "news", "chan", "stock"):
        assert factor in result
        assert "n" in result[factor]
        assert "ic" in result[factor]


def test_build_report_full():
    history = _make_history(30)
    report = build_report(history)
    assert "# 创业板择时质量周报" in report
    assert "score→次日收益 IC" in report
    assert "分层单调性" in report
    assert "仓位决策审计" in report
    assert "踏空/躲跌归因" in report
    assert "七层因子 IC" in report
    assert "结论提示" in report
    assert "暂无统计显著性" in report
    assert "不构成任何收益承诺" in report


def test_build_report_small_n():
    """n<20 时应标注样本不足"""
    history = _make_history(10)
    report = build_report(history)
    assert "样本不足" in report


def test_build_report_contains_dates():
    history = _make_history(25)
    report = build_report(history)
    dates = [h["date"] for h in history]
    assert min(dates) in report or "覆盖" in report


def test_decision_audit_includes_first_day():
    history = _make_history(25)
    audits = compute_decision_audit(history)
    assert audits[0]["date"] == history[0]["date"]


# ============================================================
# 修复验证测试（2026-09-19 返修）
# ============================================================


def test_stratification_direction_mono_down():
    """构造高档均值低、低档均值高的 history，断言 direction 含"单调递减"。

    验证 compute_stratification 的 mono_up/mono_down 方向标签修复：
    sorted_tiers 为高档→低档，mono_up 应表示"高档收益≥低档收益"。
    """
    # 100% 档（score≥0.40）→ next_ret 均值为负（-0.05）
    # 0% 档（score<-0.30）→ next_ret 均值为正（+0.05）
    history = []
    for i in range(10):
        history.append({
            "date": f"2026-08-{24 + i:02d}",
            "score": 0.5,          # → 100% 档
            "position": 1.0,
            "next_ret": -0.05,
            "r3": -0.05, "r5": -0.05, "r10": -0.05,
            "nav": 1.0, "bh_nav": 1.0,
        })
    for i in range(10):
        history.append({
            "date": f"2026-09-{(4 + i):02d}",
            "score": -0.5,         # → 0% 档
            "position": 0.0,
            "next_ret": 0.05,
            "r3": 0.05, "r5": 0.05, "r10": 0.05,
            "nav": 1.0, "bh_nav": 1.0,
        })
    result = compute_stratification(history)
    assert "单调递减" in result["direction"]


def test_report_shows_ret_as_percentage():
    """验证报告展示层 ×100：r3=0.0121 应显示为 +1.21% 而非 +0.01%。"""
    history = [
        {
            "date": "2026-08-24",
            "score": 0.5,
            "position": 1.0,
            "next_ret": 0.0121,
            "r3": 0.0121,
            "r5": 0.02,
            "r10": 0.03,
            "nav": 1.0,
            "bh_nav": 1.0,
            "core": 0.4, "basis": 0.0, "flow": 0.0, "mood": 0.0,
            "news": 0.0, "chan": 0.0, "stock": 0.0,
        },
    ]
    report = build_report(history)
    assert "+1.21%" in report, "r3=0.0121 应显示为 +1.21%"
    assert "+0.01%" not in report, "不应出现 +0.01%（量纲 bug）"
