# -*- coding: utf-8 -*-
"""
信号合成（synthesize.py）
====================================================
滚动 IC 加权合成（统计合成 v1）：
- 每个 t 日：用截至 t-1 的过去 window 日 RankIC 均值作权重（符号自适应，
  IC 均值为负的因子自动翻多空方向），权重归一 Σ|w|=1
- 有效因子门槛：|滚动IC均值| ≥ min_ic，不达标因子当期不参与
- IC 历史不足 window/2 的因子同样不参与（防短期噪声）

输出：
- composite: DataFrame(T×N) 合成 z 分数（越大越看多）
- weights_history: DataFrame(T×F) 每期因子权重（可解释、可推送）
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd


def rolling_ic_weights(ic_series: pd.Series, window: int = 120,
                       min_ic: float = 0.02) -> pd.Series:
    """给定某因子的逐日 RankIC 序列，返回其逐日合成权重（t 日只用 ≤t-1 信息）。"""
    mean = ic_series.shift(1).rolling(window, min_periods=window // 2).mean()
    w = mean.where(mean.abs() >= min_ic, 0.0)
    return w


def synthesize_ic_weighted(factors: Dict[str, pd.DataFrame],
                           fwd_df: pd.DataFrame,
                           window: int = 120,
                           min_ic: float = 0.02
                           ) -> Tuple[pd.DataFrame, pd.DataFrame]:
    names = sorted(factors.keys())
    factor_index = factors[names[0]].index
    # 权重对齐因子全日期并前向填充：最后交易日无前瞻收益（无IC），沿用最近有效
    # 权重作用于当日信号——否则 composite 永远缺最后一行，生产取不到当日α
    weights = pd.DataFrame({n: rolling_ic_weights(
        _rank_ic(factors[n], fwd_df), window, min_ic).reindex(factor_index).ffill()
        for n in names})
    # 归一化：Σ|w|=1（无有效因子时当期权重全 0 → composite 置 0）
    abs_sum = weights.abs().sum(axis=1).replace(0, np.nan)
    weights = weights.div(abs_sum, axis=0).fillna(0.0)

    composite_rows = {}
    for t in weights.index:
        w = weights.loc[t]
        if w.abs().sum() == 0:
            composite_rows[t] = pd.Series(0.0, index=factors[names[0]].columns)
            continue
        acc = None
        for n in names:
            if w[n] == 0 or t not in factors[n].index:
                continue
            part = factors[n].loc[t] * w[n]
            acc = part if acc is None else acc.add(part, fill_value=0.0)
        composite_rows[t] = acc if acc is not None else pd.Series(
            0.0, index=factors[names[0]].columns)
    composite = pd.DataFrame(composite_rows).T
    composite = composite.reindex(columns=factors[names[0]].columns)
    return composite.astype(float), weights


def _rank_ic(factor_df: pd.DataFrame, fwd_df: pd.DataFrame) -> pd.Series:
    from src.strategy.evaluate import calc_ic_series
    return calc_ic_series(factor_df, fwd_df, method="rank")
