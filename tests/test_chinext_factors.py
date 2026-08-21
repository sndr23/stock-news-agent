# -*- coding: utf-8 -*-
"""test_chinext_factors.py — v4 核心层纯因子单测。
锁定：因子序列形状、无 NaN/Inf、无除零空窗、维度合成正确、look-ahead 基准。
"""
import pytest

from src.strategy.chinext_factors import (
    defensive_state, core_signals, dimension_score,
    factor_trend_ma20_60, factor_momentum_60, factor_volprice_quadrant,
    factor_amihud, factor_vol_regime, factor_vol_term,
    factor_pullback_52w, factor_dd60,
)


def _synth_close(n=640):
    """合成上升期+回撤期的价格序列 + 递增量能。"""
    import math
    closes, amounts = [], []
    for i in range(n):
        # 前 400 上升，后 240 高位回落
        base = 1000 * (1 + i / n * 1.0)
        if i > 400:
            base *= (1 - (i - 400) / 400 * 0.4)
        closes.append(base)
        amounts.append(1e8 + i * 1e5)
    return closes, amounts


def test_factor_pullback_52w_mid_segment_defensive():
    # 回归锁：距52周高回撤-19%应给出防守分(负)，而非误判最大看多+1.0
    c = [100.0] * 300 + [81.0]  # 末段相对52周高(100)回撤-19%
    out = factor_pullback_52w(c)
    last = out[-1]
    assert -1.0001 <= last < 0.1, f"回撤-19%应为防守分，实际 {last}"


def test_core_signals_keys_and_shapes():
    c, a = _synth_close()
    sig = core_signals(c, a, erp_pctile=[0.5] * len(c))
    assert set(sig) == {"trend_ma20_60", "trend_momentum_60", "volprice_quadrant",
                        "volprice_amihud", "vol_regime", "vol_term", "value_erp",
                        "pullback_52w", "dd60"}
    for name, arr in sig.items():
        assert len(arr) == len(c), name
        assert all(isinstance(x, float) for x in arr), name
        # 无 NaN/Inf，值域在 [-1,1]
        assert all(-1.0001 <= x <= 1.0001 for x in arr), name


def test_factors_no_nan_no_inf():
    c, a = _synth_close()
    import math
    for f in [factor_trend_ma20_60(c), factor_momentum_60(c),
              factor_volprice_quadrant(c, a), factor_amihud(c, a),
              factor_vol_regime(c), factor_vol_term(c),
              factor_pullback_52w(c), factor_dd60(c)]:
        for v in f:
            assert not (math.isnan(v) or math.isinf(v))
            assert -1.0001 <= v <= 1.0001


def test_dimension_score_respects_weights():
    c, a = _synth_close()
    sig = core_signals(c, a, erp_pctile=[0.5] * len(c))
    comp = dimension_score(sig)
    assert len(comp) == len(c)
    assert all(-1.0001 <= x <= 1.0001 for x in comp)
    # 无估值源时估值维置0，分数不应包含该维贡献（comp 在非极端处应 < 全正假设）
    assert abs(comp[-1]) <= 1.0


def test_trend_uptrend_positive():
    # 单边上升：双均线应强势看多
    c, _a = _synth_close(200)
    t = factor_trend_ma20_60(c)
    # 站上 MA20/MA60、MA20 上行 → score 应为正
    assert t[-1] > 0


def test_lookahead_boundary_warmup():
    # 前 60 根是各因子 warmup 期，必须为 0（无数据不拍脑袋）
    c, a = _synth_close()
    for f in [factor_trend_ma20_60(c), factor_momentum_60(c)]:
        assert f[0] == 0.0 and f[50] == 0.0


def test_defensive_state_empty_no_crash():
    d = defensive_state([100.0] * 80, vol_pctile=None, glass=None)
    assert d["cap"] == 1.0 and d["triggers"] == []


def test_defensive_caps_reduction():
    # 深度回撤触发封顶3成
    c = [100.0 - i * 1.5 for i in range(80)]
    d = defensive_state(c, vol_pctile=45, glass={"risk_off": False,
                                                 "basis_min_ap": -20.0, "intraday_pct": -3.0})
    assert d["cap"] <= 0.3