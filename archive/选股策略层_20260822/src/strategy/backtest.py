# -*- coding: utf-8 -*-
"""
回测引擎（backtest.py）
====================================================
向量化日频回测（截面循环、矩阵内积，无逐票模拟）：
- 信号：t 日合成 z 分数（全部因子窗口 ≤t，无 look-ahead）
- 交易：t 日收盘调仓至 w_t，持有 t→t+1，收益 = w_t · ret_{t+1}
- 成本（A股现实口径，单边）：买 佣金2.5bp+冲击5bp；卖 佣金2.5bp+印花5bp+冲击5bp
  → 双边合计 25bp；按换手单边 Σ|Δw|/2 计费：cost = 单边换手 × 0.25% × 2 = 单边×0.5%×? 
  （实现为 cost = turnover_one_side × round_trip_cost，round_trip_cost=0.0025）
- 组合构造默认排序法（快、确定），--opt mv 时逐日 SLSQP（慢，研究用）

输出 BacktestResult：净值/基准/超额、年化·波动·夏普·回撤·Calmar·换手·IC·分年·分层
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np
import pandas as pd

from src.strategy.optimizer import PortfolioConfig, build_rank_portfolio, optimize_mv


@dataclass
class BacktestConfig:
    cost_one_side: float = 0.0010    # 单边成本合计（佣金+印花/冲击近似，10bp）
    start: Optional[str] = None      # 回测起始日（None=信号完备后首日）
    opt: str = "rank"                # rank | mv
    rebalance_every: int = 1         # 每N日调仓（1=日频）


@dataclass
class BacktestResult:
    nav: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    bench: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    excess: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    turnover: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    ic: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    metrics: Dict = field(default_factory=dict)
    yearly: pd.DataFrame = field(default_factory=pd.DataFrame)


def perf_metrics(nav: pd.Series, bench: pd.Series = None,
                 freq: int = 252) -> Dict[str, float]:
    ret = nav.pct_change().dropna()
    if len(ret) < 5:
        return {}
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (freq / max(len(ret), 1)) - 1
    vol = ret.std() * np.sqrt(freq)
    dd = (nav / nav.cummax() - 1).min()
    m = {
        "ann_return": round(ann * 100, 2),
        "ann_vol": round(vol * 100, 2),
        "sharpe": round(ret.mean() / ret.std() * np.sqrt(freq), 2) if ret.std() else 0.0,
        "max_dd": round(dd * 100, 2),
        "calmar": round(ann / abs(dd), 2) if dd else 0.0,
    }
    if bench is not None and len(bench) == len(nav):
        ex = (nav.pct_change() - bench.pct_change()).dropna()
        m["excess_ann"] = round(ex.mean() * freq * 100, 2)
        m["track_err"] = round(ex.std() * np.sqrt(freq) * 100, 2)
        m["info_ratio"] = round(ex.mean() / ex.std() * np.sqrt(freq), 2) if ex.std() else 0.0
        m["excess_win_rate"] = round((ex > 0).mean() * 100, 1)
    return m


def run_backtest(composite: pd.DataFrame, close: pd.DataFrame,
                 index_close: pd.DataFrame,
                 industry_df: Optional[pd.DataFrame] = None,
                 cov_fn=None, w_prev_start: Optional[pd.Series] = None,
                 cfg: BacktestConfig = None, pcfg: PortfolioConfig = None
                 ) -> BacktestResult:
    """cov_fn(t) -> cov DataFrame，opt='mv' 时必传；否则用排序法。"""
    cfg = cfg or BacktestConfig()
    pcfg = pcfg or PortfolioConfig()
    # warmup：跳过合成权重尚未就绪（全零）的日期，而非硬编码切片
    nz = composite.abs().sum(axis=1)
    first_valid = nz[nz > 1e-12].index.min()
    if first_valid is None:
        return BacktestResult()
    dates = [t for t in composite.index if t >= first_valid and t in close.index]
    if cfg.start:
        dates = [t for t in dates if t >= pd.Timestamp(cfg.start)]
    ret1 = close.pct_change()
    idx_close = index_close["close"] if isinstance(index_close, pd.DataFrame) else index_close

    navs, benchs, turns, ics = [], [], [], []
    nav = 1.0
    w_prev: Optional[pd.Series] = w_prev_start
    bench_nav = 1.0
    last_reb = None
    for i, t in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is None or nxt not in ret1.index:
            break
        if last_reb is None or (i - last_reb) >= cfg.rebalance_every:
            alpha = composite.loc[t].dropna()
            ind_row = industry_df.loc[t] if industry_df is not None and t in industry_df.index else None
            if cfg.opt == "mv" and cov_fn is not None:
                w = optimize_mv(alpha, cov_fn(t), ind_row, w_prev, pcfg)
            else:
                w = build_rank_portfolio(alpha, ind_row, w_prev, pcfg)
            if w.empty:
                continue
            one_side = 0.5 * float((w.reindex(alpha.index).fillna(0) -
                                   (w_prev.reindex(alpha.index).fillna(0)
                                    if w_prev is not None else 0)).abs().sum()) if w_prev is not None else 0.5
            last_reb = i
        else:
            w = w_prev if w_prev is not None else None
            one_side = 0.0
        if w is None or w.empty:
            continue
        r_next = ret1.loc[nxt].reindex(w.index).fillna(0.0)
        gross = float(w @ r_next)
        cost = one_side * cfg.cost_one_side * 2  # 单边×双边系数
        nav *= (1 + gross - cost)
        bench_nav *= (1 + float(idx_close.loc[nxt] / idx_close.loc[t] - 1)
                      if t in idx_close.index and nxt in idx_close.index and idx_close.loc[t] != 0 else 1.0)
        navs.append((nxt, nav))
        benchs.append((nxt, bench_nav))
        turns.append((nxt, one_side))
        ics.append((nxt, float(alpha.rank().corr(r_next.rank())) if r_next.std() else 0.0))
        w_prev = w

    nav_s = pd.Series(dict(navs), name="nav")
    bench_s = pd.Series(dict(benchs), name="bench")
    res = BacktestResult(
        nav=nav_s, bench=bench_s, excess=nav_s / bench_s - 1,
        turnover=pd.Series(dict(turns)), ic=pd.Series(dict(ics)),
        metrics=perf_metrics(nav_s, bench_s))
    if len(nav_s):
        yearly = nav_s.groupby(nav_s.index.year).apply(
            lambda s: (s.iloc[-1] / s.iloc[0] - 1) * 100)
        by = bench_s.groupby(bench_s.index.year).apply(
            lambda s: (s.iloc[-1] / s.iloc[0] - 1) * 100)
        res.yearly = pd.DataFrame({"策略%": yearly.round(2), "基准%": by.round(2)})
        res.yearly["超额%"] = (res.yearly["策略%"] - res.yearly["基准%"]).round(2)
    return res
