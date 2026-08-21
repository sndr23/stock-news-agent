# -*- coding: utf-8 -*-
"""
横截面 Alpha 因子库（factors.py）
====================================================
口径约定：**值越大越看多**（合成层无需再处理符号）。全部为量价类日频因子，
滚动窗口只用 ≤t 数据，无 look-ahead。

因子清单（A股实证强弱排序）：
  rev5       5日反转（负5日收益）：-ret(t-5→t)
  mom60_5    中期动量（剔除近5日）：ret(t-65→t-5)，A股弱但作风格因子保留
  low_vol    低波动异象：-std(ret,20)
  low_turn   低换手异象：-mean(换手率,20)
  size       小市值：-ln(流通市值)，流通市值=amount/换手率(%)×100
  liq        非流动性溢价(Amihud)：-mean(|ret|/amount,20)×1e9
  ppcorr     量价相关（低相关看多）：-corr(close, amount, 20)
  idio_vol   特质波动（低特质波看多）：-std(残差 vs 指数, 20)

输出：build_factors(panel) -> dict[name, DataFrame(T×N)] + 派生表 lnmv / industry 输入由调用方准备
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


def circulating_mv(panel) -> pd.DataFrame:
    """流通市值（元）≈ 成交额 / 换手率（换手率基于流通股本）。停牌日 NaN 会传导，属预期。"""
    turn = panel.turnover.replace(0, np.nan)
    return panel.amount / turn * 100.0


def build_factors(panel) -> Dict[str, pd.DataFrame]:
    close, amount = panel.close, panel.amount
    ret = close.pct_change()
    lnmv = np.log(circulating_mv(panel))
    idx_ret = panel.index_close["close"].pct_change() if isinstance(
        panel.index_close, pd.DataFrame) else panel.index_close.pct_change()
    idx_ret = pd.Series(idx_ret.values.ravel(),
                        index=panel.index_close.index)

    f: Dict[str, pd.DataFrame] = {}
    f["rev5"] = -close.pct_change(5)
    f["mom60_5"] = close.shift(5).pct_change(60)
    f["low_vol"] = -ret.rolling(20).std()
    f["low_turn"] = -panel.turnover.rolling(20).mean()
    f["size"] = -lnmv
    f["liq"] = -(ret.abs() / amount.replace(0, np.nan)).rolling(20).mean() * 1e9
    f["ppcorr"] = -close.rolling(20).corr(amount)
    # 特质波动：对指数回归的日残差 std
    resid = _residual_vs_index(ret, idx_ret, window=20)
    f["idio_vol"] = -resid.rolling(20).std()
    return f


def _residual_vs_index(ret: pd.DataFrame, idx_ret: pd.Series, window: int) -> pd.DataFrame:
    """滚动窗口内 个股收益 = a + b*指数收益 的残差（逐日全截面向量化实现）。
    注意：DataFrame×Series 默认按列对齐，指数收益必须用 mul(axis=0) 按行广播。"""
    idx = pd.DataFrame({c: idx_ret for c in ret.columns}, index=ret.index)
    mean_x = idx.rolling(window).mean()
    mean_y = ret.rolling(window).mean()
    cov_xy = (ret * idx).rolling(window).mean() - mean_y * mean_x
    var_x = (idx * idx).rolling(window).mean() - mean_x ** 2
    beta = cov_xy / var_x.replace(0, np.nan)
    alpha = mean_y - beta * mean_x
    return ret - (alpha + beta.mul(idx_ret, axis=0))


def factor_frames(panel, industry_map) -> Dict[str, pd.DataFrame]:
    """构建因子 + 元数据表（lnmv、行业宽表），供预处理/风险模型共用。"""
    factors = build_factors(panel)
    codes = panel.codes
    industry_df = pd.DataFrame(
        [[industry_map.get(c, "未知") for c in codes]] * len(panel.close.index),
        index=panel.close.index, columns=codes)
    meta = {"lnmv": np.log(circulating_mv(panel)), "industry": industry_df}
    return {**factors, **meta}
