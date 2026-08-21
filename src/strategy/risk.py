# -*- coding: utf-8 -*-
"""
风险模型（risk.py）—— 简化 Barra 多因子风险模型
====================================================
结构（与 CNE5/CNE6 同构，规模收敛到日频可估）：
1. 暴露矩阵 X(N×K)：行业哑变量（去一列防共线） + 风格因子 z 分数（size/vol/mom/turn）
2. 因子收益率 F(T×K)：逐日截面回归 ret = X·f + ε（WLS 可选，v1 OLS）
3. 因子协方差 Σf(K×K)：样本协方差 + 手写 Ledoit-Wolf 收缩（保证正定、稳）
4. 个股协方差 Σs ≈ X·Σf·X' + diag(特质方差)

对外：
- estimate(returns, industry_df, style_dict, window) -> RiskModel(暴露/因子收益/收缩强度/Σs)
- RiskModel.cov(frame_codes) 取给定代码子块的协方差 DataFrame
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np
import pandas as pd


@dataclass
class RiskModel:
    exposures: pd.DataFrame        # N×K（index=code）
    factor_returns: pd.DataFrame   # T×K
    shrinkage: float
    factor_cov: pd.DataFrame       # K×K
    idio_var: pd.Series            # N，特质方差（日）

    def cov(self, codes) -> pd.DataFrame:
        """给定代码顺序的个股协方差（日频）。"""
        X = self.exposures.reindex(codes).fillna(0.0)
        cov_f = self.factor_cov.values
        sigma = X.values @ cov_f @ X.values.T
        idio = self.idio_var.reindex(codes).fillna(0.0).values
        sigma[np.diag_indices(len(codes))] += idio
        # 数值防护：对称化 + 特征值下限，保证优化器拿到的矩阵正定
        sigma = (sigma + sigma.T) / 2.0
        eig = np.linalg.eigvalsh(sigma)
        if eig.min() <= 0:
            sigma += np.eye(len(codes)) * (abs(eig.min()) + 1e-10)
        return pd.DataFrame(sigma, index=codes, columns=codes)


def build_exposures(industry_row: pd.Series, style_rows: Dict[str, pd.Series]) -> pd.DataFrame:
    """最新截面暴露矩阵：行业哑变量(drop_first) + 风格 z 分数 + 常数列。"""
    dummies = pd.get_dummies(industry_row.fillna("未知"), drop_first=True).astype(float)
    dummies.index = industry_row.index
    parts = [dummies]
    for k, v in style_rows.items():
        parts.append(v.astype(float).rename(k).to_frame().T)
    style = pd.concat([p.T.stack().unstack() for p in parts[1:]], axis=1) if len(parts) > 1 else None
    X = pd.concat([dummies, style], axis=1) if style is not None else dummies
    X["const"] = 1.0
    return X.astype(float)


def ledoit_wolf_shrinkage(returns: pd.DataFrame) -> tuple:
    """手写 Ledoit-Wolf 线性收缩：(收缩后协方差, 收缩强度δ)。免 sklearn 依赖。"""
    R = returns.dropna(axis=0, how="all").dropna(axis=1, how="any").values
    T, N = R.shape
    if T < 3 or N < 1:
        return np.cov(R, rowvar=False) if R.size else np.zeros((1, 1)), 0.0
    S = np.cov(R, rowvar=False)
    mu = R.mean(axis=0)
    Xc = R - mu
    # 单变量收缩目标 F = 均值对角阵
    var_diag = np.diag(S)
    F = np.diag(var_diag)
    # LW π 与 ρ（Ledoit & Wolf 2004 常数相关目标简化版：对角目标）
    y = Xc ** 2
    phi = (y.T @ y / T - S ** 2).sum() / N
    theta_diag = ((Xc ** 3).sum(axis=0) / T / (Xc.std(axis=0, ddof=1) ** 4).clip(1e-12)).sum() / N
    gamma = ((S - F) ** 2).sum() / N
    delta = max(0.0, min(1.0, phi / gamma)) if gamma > 0 else 1.0
    shrunk = delta * F + (1 - delta) * S
    return shrunk, delta


def estimate(returns: pd.DataFrame, industry_df: pd.DataFrame,
             style_dict: Dict[str, pd.DataFrame], window: int = 250) -> RiskModel:
    """滚动窗口末截面估计风险模型。returns: T×N 日收益。"""
    dates = returns.index[-(window + 1):]
    rets = returns.loc[dates].dropna(axis=1, how="any")
    t_last = rets.index[-1]
    ind_row = industry_df.loc[t_last].reindex(rets.columns).fillna("未知")
    style_rows = {k: v.loc[t_last].reindex(rets.columns) for k, v in style_dict.items()}
    X = build_exposures(ind_row, style_rows)
    # 因子收益率：逐日截面回归（矩阵形式一次求解）
    Xv = X.values
    beta, *_ = np.linalg.lstsq(Xv, rets.values.T, rcond=None)  # K×T
    factor_returns = pd.DataFrame(beta.T, index=rets.index, columns=X.columns)
    resid = rets.values - (Xv @ beta).T
    idio_var = pd.Series(resid.var(axis=0, ddof=1), index=rets.columns)
    cov_f_np, delta = ledoit_wolf_shrinkage(factor_returns)
    cov_f = pd.DataFrame(cov_f_np, index=X.columns, columns=X.columns)
    return RiskModel(exposures=X, factor_returns=factor_returns,
                     shrinkage=round(delta, 3), factor_cov=cov_f, idio_var=idio_var)
