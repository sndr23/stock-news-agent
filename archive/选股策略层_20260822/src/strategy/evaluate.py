# -*- coding: utf-8 -*-
"""
因子评价（evaluate.py）
====================================================
机构标准评价指标（全部基于"t日因子值 vs t+1日起的前瞻收益"，无 look-ahead）：
- IC / RankIC 序列：截面 Pearson / Spearman 相关
- IC 汇总：均值、标准差、IR=mean/std、t 统计、正率
- 分层收益：按因子值分 N 层（Q1 最看空 ~ QN 最看多），各层平均日收益与累计
- 因子相关矩阵：跨期堆叠后的 Spearman 相关，用于诊断共线与合成权重

核心函数：
- forward_returns(close, horizon=1)     t→t+horizon 收益（截面常用）
- calc_ic_series(factor_df, fwd_df)     逐日 RankIC
- ic_summary(ic_series)                 汇总指标 dict
- layered_returns(factor_df, fwd_df, n) 分层收益 DataFrame
- factor_corr_matrix(factor_dict)       相关矩阵
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


def forward_returns(close: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    """t 日因子对应的 t→t+horizon 前瞻收益（最后一行自然为 NaN）。"""
    return close.shift(-horizon) / close - 1.0


def calc_ic_series(factor_df: pd.DataFrame, fwd_df: pd.DataFrame,
                   method: str = "rank") -> pd.Series:
    """逐日截面 IC。method='rank' 为 RankIC（主口径），'pearson' 为 IC。"""
    out = {}
    for t in factor_df.index:
        if t not in fwd_df.index:
            continue
        a = factor_df.loc[t]
        b = fwd_df.loc[t]
        pair = pd.concat([a, b], axis=1, keys=["f", "r"]).dropna()
        if len(pair) < 10:
            continue
        if pair["f"].std() == 0 or pair["r"].std() == 0:
            out[t] = 0.0
            continue
        out[t] = (pair["f"].corr(pair["r"], method="spearman")
                  if method == "rank" else pair["f"].corr(pair["r"]))
    return pd.Series(out, dtype=float)


def ic_summary(ic: pd.Series) -> Dict[str, float]:
    ic = ic.dropna()
    if len(ic) < 3:
        return {"mean": 0.0, "std": 0.0, "ir": 0.0, "t_stat": 0.0, "pos_ratio": 0.0, "n": len(ic)}
    mean, std = ic.mean(), ic.std()
    return {
        "mean": round(mean, 4),
        "std": round(std, 4),
        "ir": round(mean / std, 3) if std else 0.0,
        "t_stat": round(mean / (std / np.sqrt(len(ic))), 2) if std else 0.0,
        "pos_ratio": round((ic > 0).mean(), 3),
        "n": int(len(ic)),
    }


def layered_returns(factor_df: pd.DataFrame, fwd_df: pd.DataFrame,
                    n_layers: int = 5) -> pd.DataFrame:
    """按因子值分层（Q1 空 → QN 多），返回各层日均收益（%）与多空 QN-Q1。"""
    rows = []
    for t in factor_df.index:
        if t not in fwd_df.index:
            continue
        pair = pd.concat([factor_df.loc[t], fwd_df.loc[t]], axis=1, keys=["f", "r"]).dropna()
        if len(pair) < n_layers * 3:
            continue
        try:
            q = pd.qcut(pair["f"].rank(method="first"), n_layers, labels=False) + 1
        except ValueError:
            continue
        rows.append(pair.groupby(q)["r"].mean())
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    layered = df.mean() * 100
    layered["QN-Q1"] = layered[n_layers] - layered[1]
    return layered.round(4)


def factor_corr_matrix(factor_dict: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """跨期堆叠截面 Rank 相关：诊断共线（|ρ|>0.7 的因子对合成权重会被稀释）。"""
    stacked = {k: v.stack() for k, v in factor_dict.items()}
    wide = pd.DataFrame(stacked)
    return wide.corr(method="spearman").round(3)
