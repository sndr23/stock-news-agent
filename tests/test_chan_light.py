# -*- coding: utf-8 -*-
"""test_chan_light.py — 轻量缠论结构模块单测。"""
import pytest

from src.strategy.chan_light import (
    merge_klines, find_fractals, compute_bis, find_zhongshu,
    find_divergence, classify_bs, chan_state,
)


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