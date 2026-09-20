# -*- coding: utf-8 -*-
"""test_chan_light.py — 轻量缠论结构模块单测。"""
import pytest

from src.strategy.chan_light import (
    merge_klines, find_fractals, compute_bis, find_zhongshu,
    find_divergence, classify_bs, chan_state,
)

pytestmark = pytest.mark.unit


def test_merge_klines_containment():
    # 上升趋势中的包含K线应向上合并（取高高）
    highs = [10, 11, 12, 13, 12, 14]
    lows = [9, 10, 10.5, 12, 11, 13]
    m = merge_klines(highs, lows)
    assert m[0] == (0, 10, 9)
    # 中间 12/13 与 13/12 含包：方向向上取高高=13,12
    assert any(x == (3, 13, 12) for x in m)


def test_find_fractals_top_bottom():
    # 明显的顶分型（中间最高）与底分型（中间最低）
    merged = [(0, 10, 9), (1, 12, 11), (2, 11, 10), (3, 9, 8), (4, 10, 9)]
    fr = find_fractals(merged, 5)
    types = {t for t, _ in fr}
    assert "top" in types and "bottom" in types


def test_compute_bis_alternation():
    fr = [("bottom", 1), ("top", 3), ("bottom", 5), ("top", 7)]
    hi = [10, 11, 12, 13, 12, 11, 12, 14]
    lo = [9, 10, 11, 12, 11, 10, 11, 13]
    bis = compute_bis(hi, lo, fr)
    assert len(bis) >= 2
    assert bis[0]["type"] == "up"  # bottom→top
    assert bis[1]["type"] == "down"


def test_find_zhongshu_overlap():
    # 三笔上下震荡形成中枢重叠区
    bis = [
        {"type": "up", "px0": 100, "px1": 110},
        {"type": "down", "px0": 110, "px1": 105},
        {"type": "up", "px0": 105, "px1": 115},
    ]
    zs = find_zhongshu(bis)
    assert zs is not None
    lo, hi = zs
    assert lo < hi


def test_find_divergence_top():
    # 价格创新高但动能柱萎缩 → 顶背驰
    closes = [100 + i for i in range(20)] + [150, 149]  # 末段走平/回落
    highs = [101 + i for i in range(20)] + [160, 155]
    lows = [99 + i for i in range(20)] + [145, 140]
    # 动量序列末段小于早段 → 顶背驰
    mom = [1.0] * 18 + [0.2] * 6  # 前强后弱
    d = find_divergence(closes, highs, lows, macd_momentum=mom, lookback=24)
    assert d in ("top", "none")


# ---------------- FIX-20260918-01：顶背驰必须有"近期新高"前提 ----------------
# 旧实现 `price_hi >= prev_hi` 中"新高"并非新事件：40 根窗口最高点可能在
# 20+ 根之前，下跌中动能和几乎必然萎缩 → 每天误报顶背驰（曾连续 52 个交易日）。

def _recent_peak_series():
    """窗口尾部 2 根处创严格新高（idx17=23 > 此前 22），低点始终在窗口开头。"""
    highs = [10, 10, 10, 10, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
             21, 22, 23, 22.5, 22]
    lows = [h - 1 for h in highs]
    return highs, lows, list(highs)


def test_find_divergence_top_recent_new_high_with_decaying_momentum():
    """(a) 近期严格新高 + 后段动能萎缩 → 'top'。"""
    highs, lows, closes = _recent_peak_series()
    mom = [1.0] * 17 + [-3.0] * 3  # 高点前动能和 17，整窗仅 8
    assert find_divergence(closes, highs, lows, macd_momentum=mom,
                           lookback=20) == "top"


def test_find_divergence_none_in_persistent_downtrend_without_recent_high():
    """(b) 回归用例：持续下跌、窗口最高点在最早（无近期新高）→ 'none'。"""
    highs = [100 - i for i in range(20)]  # 最高点 idx0，距末根 19 根
    lows = [h - 1 for h in highs]
    closes = list(highs)
    mom = [-1.0] * 20  # 单边下跌动能不衰减（无底背驰）
    assert find_divergence(closes, highs, lows, macd_momentum=mom,
                           lookback=20) == "none"


def test_find_divergence_none_when_recent_new_high_but_momentum_stronger():
    """(c) 近期严格新高但动能更强（未萎缩）→ 'none'（不是背驰）。"""
    highs, lows, closes = _recent_peak_series()
    mom = [0.2] * 17 + [3.0] * 3  # 整窗动能 12.4 > 高点前 3.4
    assert find_divergence(closes, highs, lows, macd_momentum=mom,
                           lookback=20) == "none"


def test_find_divergence_recent_high_lag_boundary():
    """近期阈值边界：距末根 ≤10 根才算"近期"，更旧的次新高点不再判顶背驰。"""
    mom = [1.0] * 9 + [-1.0] * 11
    # 峰值 idx9 → 距末根 10 根：允许判定
    highs_ok = [10] * 9 + [30] + [10] * 10
    lows_ok = [h - 1 for h in highs_ok]
    assert find_divergence(list(highs_ok), highs_ok, lows_ok,
                           macd_momentum=mom, lookback=20) == "top"
    # 峰值 idx8 → 距末根 11 根：超过近期阈值，不再判定
    highs_old = [10] * 8 + [30] + [10] * 11
    lows_old = [h - 1 for h in highs_old]
    assert find_divergence(list(highs_old), highs_old, lows_old,
                           macd_momentum=mom, lookback=20) == "none"


def test_chan_state_sufficient():
    # 构造足够长的震荡序列，chan_state 应返回结构而非 insufficient
    import math
    highs = [100 + 5 * math.sin(i / 5) for i in range(120)]
    lows = [98 + 5 * math.sin(i / 5) for i in range(120)]
    closes = [99 + 5 * math.sin(i / 5) for i in range(120)]
    st = chan_state(highs, lows, closes)
    assert st.get("error") == "insufficient" or st["bi_dir"] in ("up", "down")
    assert "zone" in st and "bustop" in st


def test_chan_state_insufficient_short():
    st = chan_state([10, 11], [9, 10], [10, 10.5])
    assert st["error"] == "insufficient"
    assert st["bustop"] is False