# -*- coding: utf-8 -*-
"""
因子预处理（preprocess.py）
====================================================
机构标准三步（截面逐日处理，全程无未来信息）：
1. MAD 去极值   x = clip(x, med - n*1.4826*MAD, med + n*1.4826*MAD)，n 默认 3
2. 标准化      z = (x - mean) / std  （std=0 的截面返回 0 向量）
3. 中性化      对 [行业哑变量, ln(流通市值)] 回归取残差，剥离行业与规模暴露

用法：因子库输出原始因子宽表后统一走 preprocess_factor()。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def winsorize_mad(cs: pd.Series, n: float = 3.0) -> pd.Series:
    s = cs.astype(float).dropna()
    if len(s) < 3:
        return cs
    med = s.median()
    mad = (s - med).abs().median() * 1.4826
    if not np.isfinite(mad) or mad == 0:
        # 退化场景（截面大量重复值，如停牌/一字板）：退化为 1%/99% 分位裁剪
        lo, hi = s.quantile([0.01, 0.99])
        return cs.clip(lo, hi)
    return cs.clip(med - n * mad, med + n * mad)


def zscore(cs: pd.Series) -> pd.Series:
    s = cs.astype(float)
    std = s.std()
    if not np.isfinite(std) or std == 0 or np.isnan(std):
        return s * 0.0
    return (s - s.mean()) / std


def neutralize(cs: pd.Series, industry: pd.Series, lnmv: pd.Series) -> pd.Series:
    """截面回归取残差：X = [行业哑变量(去一列), ln市值, 1]；行业缺失归入'未知'。"""
    df = pd.concat([cs.rename("y"), industry.rename("ind"), lnmv.rename("mv")], axis=1).dropna()
    if len(df) < 10:
        return cs
    dummies = pd.get_dummies(df["ind"].fillna("未知"), drop_first=True).astype(float)
    X = pd.concat([dummies, df["mv"].astype(float), pd.Series(1.0, index=df.index, name="const")], axis=1)
    try:
        beta, *_ = np.linalg.lstsq(X.values, df["y"].values, rcond=None)
        resid = df["y"] - X.values @ beta
    except np.linalg.LinAlgError:
        return cs
    out = cs.copy()
    out.loc[df.index] = resid
    return out


def preprocess_factor(factor_df: pd.DataFrame, industry_df: pd.DataFrame,
                      lnmv_df: pd.DataFrame, winsor_n: float = 3.0,
                      do_neutralize: bool = True) -> pd.DataFrame:
    """对整张因子宽表逐日执行 去极值→标准化→(行业+市值)中性化→再标准化。"""
    out = {}
    dates = factor_df.index
    for t in dates:
        cs = factor_df.loc[t]
        cs = zscore(winsorize_mad(cs, winsor_n))
        if do_neutralize and t in industry_df.index and t in lnmv_df.index:
            cs = neutralize(cs, industry_df.loc[t], lnmv_df.loc[t])
            cs = zscore(cs)
        out[t] = cs
    return pd.DataFrame(out).T.reindex(dates).astype(float)
