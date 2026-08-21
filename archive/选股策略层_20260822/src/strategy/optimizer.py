# -*- coding: utf-8 -*-
"""
组合优化器（optimizer.py）
====================================================
两种构造方式，接口一致（输入 α 与风险，输出权重 Series）：

1. optimize_mv（scipy SLSQP 均值-方差，生产默认）
   max α'w − λ·w'Σw
   s.t. Σw = 1；0 ≤ w ≤ w_max；行业偏离 ≤ ind_dev；换手 Σ|w−w_prev| ≤ turnover_cap
   换手约束用辅助变量线性化：min t_i, t_i ≥ ±(w_i − w_prev,i)

2. build_rank_portfolio（排序法降级，回测/无 scipy 时用）
   取 α 前 N 名，权重 ∝ max(α,0)，依次施加个股上限与行业偏离上限（贪心裁剪）。

参数（PortfolioConfig）取机构中低风险指增常见值：
   n=50 / w_max=3% / ind_dev=3% / turnover_cap=30%（单边）/ λ=25
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np
import pandas as pd


@dataclass
class PortfolioConfig:
    n: int = 50                      # 持仓数（排序法/初值）
    w_max: float = 0.03              # 个股权重上限
    ind_dev: float = 0.03            # 相对基准的行业偏离上限（绝对值）
    turnover_cap: float = 0.30       # 单次调仓换手上限（单边，Σ|Δw|/2）
    risk_aversion: float = 25.0      # λ
    industry_cap_slack: float = 0.0  # 预留


def industry_benchmark_weights(industry_row: pd.Series, codes) -> pd.Series:
    """基准行业权重 = 等权基准下的行业占比（v1 用等权近似市值权重，HS300 内差异有限）。"""
    ind = industry_row.reindex(codes).fillna("未知")
    return ind.value_counts(normalize=True)


def effective_cap(cfg: PortfolioConfig, n: int) -> float:
    """小股票池可行性适配：cap×n < 1 时自动放宽到 1.05/n（等权+5%余量），
    避免 Σw=1 与个股权重上限的约束集不可行。"""
    return max(cfg.w_max, 1.05 / n) if n > 0 else cfg.w_max


def optimize_mv(alpha: pd.Series, cov: pd.DataFrame,
                industry_row: Optional[pd.Series],
                w_prev: Optional[pd.Series] = None,
                cfg: PortfolioConfig = None) -> pd.Series:
    """SLSQP 均值-方差优化。α 为日频 z 分数（统一放大 1e3 数量级，与 λ 匹配）。"""
    cfg = cfg or PortfolioConfig()
    try:
        from scipy.optimize import minimize
    except ImportError:
        return build_rank_portfolio(alpha, industry_row, w_prev, cfg)

    codes = list(alpha.dropna().index)
    if len(codes) < 2:
        return pd.Series(dtype=float)
    a = alpha.reindex(codes).fillna(0.0).values * 1e3
    S = cov.reindex(index=codes, columns=codes).fillna(0.0).values
    n = len(codes)
    w0 = _initial_weights(alpha.reindex(codes), w_prev, cfg, n)

    cons = [{"type": "eq", "fun": lambda w: w.sum() - 1.0}]
    if industry_row is not None:
        bench = industry_benchmark_weights(industry_row, codes)
        ind = industry_row.reindex(codes).fillna("未知")
        for g in bench.index:
            mask = (ind == g).values.astype(float)
            if mask.sum() == 0:
                continue
            bg = bench[g]
            cons.append({"type": "ineq",
                         "fun": lambda w, m=mask, b=bg: cfg.ind_dev + b - float(m @ w)})
            cons.append({"type": "ineq",
                         "fun": lambda w, m=mask, b=bg: cfg.ind_dev + float(m @ w) - b})
    if w_prev is not None and cfg.turnover_cap < 1.0:
        wp = w_prev.reindex(codes).fillna(0.0).values
        cons.append({"type": "ineq",
                     "fun": lambda w: cfg.turnover_cap - 0.5 * np.abs(w - wp).sum()})

    bounds = [(0.0, effective_cap(cfg, n))] * n
    obj = lambda w: -(a @ w) + cfg.risk_aversion * float(w @ S @ w) * 1e4
    res = minimize(obj, w0, method="SLSQP", bounds=bounds, constraints=cons,
                   options={"maxiter": 200, "ftol": 1e-9})
    w = res.x if res.success else w0
    w = pd.Series(np.asarray(w, dtype=float), index=codes)
    w = _cap_normalize(w, effective_cap(cfg, n))
    return w


def _cap_normalize(w: pd.Series, cap: float, iters: int = 10) -> pd.Series:
    """上限约束下的归一化：超额部分按剩余权重比例再分配，迭代至收敛。
    （clip+除以和 的一把梭写法在 cap×n 接近 1 时会反复超限）"""
    w = w.clip(lower=0.0)
    for _ in range(iters):
        over = w > cap
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        free = (~over) & (w > 0)
        if not free.any() or float(w[free].sum()) <= 0:
            break
        w[free] += excess * w[free] / float(w[free].sum())
    total = float(w.sum())
    return w / total if total > 0 else w


def build_rank_portfolio(alpha: pd.Series, industry_row: Optional[pd.Series],
                         w_prev: Optional[pd.Series] = None,
                         cfg: PortfolioConfig = None) -> pd.Series:
    """排序法组合：α 前 N 名，权重 ∝ max(α,0)，贪心施加个股/行业上限。
    可行性：个股上限 cap 要求持仓数 ≥ ⌈1/cap⌉，正信号不足时自动扩选/等权兜底。"""
    cfg = cfg or PortfolioConfig()
    a = alpha.dropna().sort_values(ascending=False)
    if a.empty:
        return pd.Series(dtype=float)
    need = int(np.ceil(1.0 / effective_cap(cfg, len(a))))
    top = a.head(max(cfg.n, need))
    pos = top.clip(lower=0.0)
    cap = effective_cap(cfg, len(top))
    if pos.sum() > 0 and (pos > 0).sum() * cap >= 1.0 - 1e-9:
        w = _cap_normalize(pos / pos.sum(), cap)
    else:
        # 正信号数 × cap < 1：约束不可行 → 前 need 名等权兜底（cap×need ≥ 1 恒成立）
        w = _cap_normalize(pd.Series(1.0, index=top.head(need).index), cap)
    if industry_row is not None:
        bench = industry_benchmark_weights(industry_row, list(w.index))
        ind = industry_row.reindex(w.index).fillna("未知")
        for _ in range(3):  # 两类上限可能互相挤压，少量迭代收敛
            for g, bg in bench.items():
                over = float(w[ind == g].sum()) - (bg + cfg.ind_dev)
                if over > 1e-9:
                    members = w[ind == g]
                    scale = (bg + cfg.ind_dev) / float(members.sum())
                    w[ind == g] = members * scale
            w = _cap_normalize(w, cfg.w_max)
    return w[w > 1e-9]


def _initial_weights(alpha: pd.Series, w_prev: Optional[pd.Series],
                     cfg: PortfolioConfig, n: int) -> np.ndarray:
    if w_prev is not None:
        wp = w_prev.reindex(alpha.index).fillna(0.0).values
        if wp.sum() > 0.99:
            return wp
    base = build_rank_portfolio(alpha, None, None, cfg)
    return base.reindex(alpha.index).fillna(0.0).values if len(base) else np.full(n, 1.0 / n)
