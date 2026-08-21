# -*- coding: utf-8 -*-
"""策略层单元测试（合成数据，无网络/无LLM，-m unit）"""
import numpy as np
import pandas as pd
import pytest

from src.strategy.data import PanelData
from src.strategy.factors import build_factors, factor_frames, circulating_mv
from src.strategy.preprocess import winsorize_mad, zscore, neutralize, preprocess_factor
from src.strategy.evaluate import (forward_returns, calc_ic_series, ic_summary,
                                   layered_returns, factor_corr_matrix)
from src.strategy.synthesize import synthesize_ic_weighted, rolling_ic_weights
from src.strategy.risk import estimate as risk_estimate, ledoit_wolf_shrinkage
from src.strategy.optimizer import (PortfolioConfig, build_rank_portfolio,
                                    optimize_mv, _cap_normalize)
from src.strategy.backtest import run_backtest, perf_metrics, BacktestConfig

pytestmark = pytest.mark.unit

N, T = 40, 320
DATES = pd.bdate_range("2025-01-01", periods=T)
CODES = [f"{600000 + i:06d}" for i in range(N)]


def make_panel(seed=7, n=N, t=T, alpha_codes=None, alpha_drift=0.004, vol=0.015):
    rng = np.random.default_rng(seed)
    ret = pd.DataFrame(rng.normal(0.0005, vol, (t, n)), index=DATES[:t],
                       columns=CODES[:n])
    if alpha_codes:
        ret[alpha_codes] += alpha_drift  # 给部分票注入持续alpha
    close = (1 + ret).cumprod() * 100
    volume = pd.DataFrame(rng.uniform(1e5, 5e5, (t, n)), index=close.index, columns=close.columns)
    amount = volume * close * 100
    turnover = pd.DataFrame(rng.uniform(0.5, 5.0, (t, n)), index=close.index, columns=close.columns)
    index_close = pd.DataFrame({"close": (1 + pd.Series(
        rng.normal(0.0004, 0.01, t), index=close.index)).cumprod() * 4000})
    return PanelData(close=close, high=close * 1.01, low=close * 0.99,
                     volume=volume, amount=amount, turnover=turnover,
                     index_close=index_close, codes=list(close.columns))


@pytest.fixture(scope="module")
def panel():
    return make_panel(alpha_codes=CODES[:5])


@pytest.fixture(scope="module")
def frames(panel):
    industries = ["银行", "白酒", "半导体", "新能源", "医药"]
    imap = {c: industries[i % len(industries)] for i, c in enumerate(CODES)}
    return factor_frames(panel, imap)


# ---------- preprocess ----------

def test_winsorize_mad_clips_outliers():
    s = pd.Series(np.concatenate([np.zeros(50), [100.0]]))
    out = winsorize_mad(s)
    assert out.max() < 100.0
    assert out.iloc[:50].abs().max() <= 1e-9


def test_zscore_standardizes():
    s = pd.Series(np.arange(1, 101, dtype=float))
    z = zscore(s)
    assert abs(z.mean()) < 1e-9
    assert abs(z.std() - 1.0) < 1e-9


def test_neutralize_removes_industry_effect():
    ind = pd.Series(["A"] * 20 + ["B"] * 20, index=range(40))
    y = pd.Series(np.where(ind == "A", 3.0, -3.0) + np.random.default_rng(1).normal(0, 0.1, 40),
                  index=range(40))
    mv = pd.Series(np.linspace(20, 22, 40), index=range(40))
    resid = neutralize(y, ind, mv)
    assert abs(resid[:20].mean()) < 0.5
    assert abs(resid[20:].mean()) < 0.5


def test_preprocess_factor_output_shape(frames):
    f = frames["rev5"].copy()
    out = preprocess_factor(f, frames["industry"], frames["lnmv"])
    assert out.shape == f.shape
    assert out.index.equals(f.index)


# ---------- factors ----------

def test_factor_directions(panel, frames):
    ret5 = panel.close.pct_change(5).iloc[-1]
    assert np.allclose(frames["rev5"].iloc[-1], -ret5, equal_nan=True)
    mom = panel.close.shift(5).pct_change(60).iloc[-1]
    assert np.allclose(frames["mom60_5"].iloc[-1], mom, equal_nan=True)
    assert (frames["low_vol"].iloc[-1].dropna() <= 0).all()
    assert (frames["low_turn"].iloc[-1].dropna() <= 0).all()


def test_all_factors_shape_aligned(panel, frames):
    # 回归锁：任何因子不得出现宽表列污染（DataFrame×Series 按列广播事故）
    for name, df in frames.items():
        if name in ("industry", "lnmv"):
            continue
        assert df.shape == panel.close.shape, f"{name} 形状异常: {df.shape}"
        assert list(df.columns) == list(panel.close.columns), f"{name} 列污染"


def test_circulating_mv_positive(panel):
    mv = circulating_mv(panel)
    assert (mv.dropna() > 0).all().all()


# ---------- evaluate ----------

def test_ic_perfect_predictor(panel):
    # 机制自检：因子=前瞻收益本身 → 截面 RankIC 恒为 1
    close = panel.close
    fwd = forward_returns(close, 1)
    ic = calc_ic_series(fwd, fwd)
    body = ic.dropna().iloc[5:-5]
    assert (body > 0.999).mean() > 0.98


def test_ic_detects_injected_alpha():
    # 低噪声面板：8 只 +0.6%/日 持续α票 → 20日动量应稳定预测次日收益
    p = make_panel(seed=11, alpha_codes=CODES[:8], alpha_drift=0.006, vol=0.008)
    fwd = forward_returns(p.close, 1)
    mom = p.close / p.close.shift(20) - 1
    ic = calc_ic_series(mom, fwd)
    assert ic.mean() > 0.15


def test_ic_summary_fields():
    s = pd.Series(np.random.default_rng(2).normal(0.05, 0.2, 100))
    summ = ic_summary(s)
    assert summ["n"] == 100
    assert abs(summ["ir"] - summ["mean"] / summ["std"]) < 1e-3


def test_layered_returns_monotonic_for_perfect(panel):
    fwd = forward_returns(panel.close, 1)
    layered = layered_returns(fwd, fwd, 5)
    assert len(layered) >= 5
    assert layered[5] > layered[1]
    assert layered["QN-Q1"] > 0


def test_factor_corr_matrix_symmetric(frames):
    facs = {k: frames[k] for k in ["rev5", "mom60_5", "low_vol"]}
    corr = factor_corr_matrix(facs)
    assert corr.shape == (3, 3)
    assert np.allclose(corr.values, corr.values.T)


# ---------- synthesize ----------

def test_synthesize_weights_sum_and_sign(frames, panel):
    fwd = forward_returns(panel.close, 1)
    perfect = fwd  # 完美前瞻因子（IC=1，仅测试合成机制）
    noise = pd.DataFrame(np.random.default_rng(3).normal(0, 1, frames["rev5"].shape),
                         index=frames["rev5"].index, columns=frames["rev5"].columns)
    composite, weights = synthesize_ic_weighted(
        {"perfect": perfect, "noise": noise}, fwd, window=60, min_ic=0.02)
    tail = weights.dropna(how="all").iloc[-1]
    assert abs(tail["perfect"]) >= 0.99  # 噪声因子被门槛滤掉
    assert abs(tail.abs().sum() - 1.0) < 1e-6
    last_t = composite.index[-2]  # 末日 fwd 全 NaN，取前一日
    common = composite.loc[last_t].dropna().index
    rho = composite.loc[last_t][common].corr(perfect.loc[last_t][common])
    assert rho > 0.95


# ---------- risk ----------

def test_ledoit_wolf_psd():
    rng = np.random.default_rng(4)
    R = pd.DataFrame(rng.normal(0, 0.01, (200, 5)))
    cov, delta = ledoit_wolf_shrinkage(R)
    assert 0.0 <= delta <= 1.0
    assert np.linalg.eigvalsh(cov).min() >= -1e-12


def test_risk_estimate_cov_psd(panel, frames):
    rets = panel.returns().iloc[-260:].fillna(0.0)
    ind = frames["industry"]
    style = {k: frames[k] for k in ["size", "low_vol", "mom60_5", "low_turn"]}
    model = risk_estimate(rets, ind, style, window=250)
    codes = list(rets.columns[:20])
    cov = model.cov(codes)
    assert cov.shape == (20, 20)
    assert np.linalg.eigvalsh(cov.values).min() > 0
    assert (np.diag(cov.values) > 0).all()


# ---------- optimizer ----------

def test_cap_normalize_respects_cap():
    w = pd.Series([0.9] + [0.1 / 9] * 9)  # 单票占90%
    out = _cap_normalize(w, 0.2)
    assert out.max() <= 0.2 + 1e-9
    assert abs(out.sum() - 1.0) < 1e-9


def test_rank_portfolio_constraints(frames):
    cfg = PortfolioConfig(n=20, w_max=0.08, ind_dev=0.10)
    alpha = frames["rev5"].iloc[-1]
    ind_row = frames["industry"].iloc[-1]
    w = build_rank_portfolio(alpha, ind_row, None, cfg)
    assert abs(w.sum() - 1.0) < 1e-6
    assert w.max() <= cfg.w_max + 1e-9
    # 行业偏离上限（相对等权基准）
    bench = ind_row.reindex(w.index).value_counts(normalize=True)
    port = ind_row.reindex(w.index).groupby(w).sum()
    port = ind_row.reindex(w.index).map(w).groupby(ind_row.reindex(w.index)).sum()
    for g in port.index:
        assert port[g] <= bench.get(g, 0) + cfg.ind_dev + 1e-6


def test_optimize_mv_basic(frames, panel):
    pytest.importorskip("scipy")
    rets = panel.returns().iloc[-120:].fillna(0.0)
    style = {k: frames[k] for k in ["size", "low_vol"]}
    model = risk_estimate(rets, frames["industry"], style, window=120)
    alpha = frames["rev5"].iloc[-1].dropna()
    w = optimize_mv(alpha, model.cov(list(alpha.index)),
                    frames["industry"].iloc[-1], None,
                    PortfolioConfig(w_max=0.05, ind_dev=0.10))
    assert abs(w.sum() - 1.0) < 1e-6
    assert w.max() <= 0.05 + 1e-6
    assert (w >= -1e-9).all()


# ---------- backtest ----------

def test_backtest_no_lookahead(panel, frames):
    # 信号冻结，仅扰动 m 日之后的收盘价 → m 日前的净值必须逐点不变
    fwd = forward_returns(panel.close, 1)
    composite = fwd.copy()
    res1 = run_backtest(composite, panel.close, panel.index_close,
                        frames["industry"], cfg=BacktestConfig())
    m = len(panel.close) // 2
    close2 = panel.close.copy()
    pert = 1 + pd.Series(np.random.default_rng(9).normal(0, 0.05, len(close2) - m)).cumprod()
    close2.iloc[m:] = close2.iloc[m:].mul(pert.values, axis=0)
    res2 = run_backtest(composite, close2, panel.index_close,
                        frames["industry"], cfg=BacktestConfig())
    common = res1.nav.index[res1.nav.index < close2.index[m]]
    assert len(common) > 20
    assert np.allclose(res1.nav[common], res2.nav[common], rtol=1e-12)


def test_backtest_perfect_foresight_beats_bench(panel, frames):
    fwd = forward_returns(panel.close, 1)
    res = run_backtest(fwd, panel.close, panel.index_close,
                       frames["industry"], cfg=BacktestConfig())
    assert len(res.nav) > 50
    assert res.nav.iloc[-1] > res.bench.iloc[-1]


def test_backtest_metrics_and_yearly(panel, frames):
    fwd = forward_returns(panel.close, 1)
    res = run_backtest(fwd, panel.close, panel.index_close,
                       frames["industry"], cfg=BacktestConfig())
    m = res.metrics
    for k in ["ann_return", "sharpe", "max_dd", "excess_ann", "info_ratio"]:
        assert k in m
    assert not res.yearly.empty
    assert res.turnover.max() <= 1.0 + 1e-6  # 单边换手有界（Σ|Δw|/2≤1）


def test_perf_metrics_flat_nav():
    nav = pd.Series(np.ones(100))
    m = perf_metrics(nav, nav)
    assert m["ann_return"] == 0.0
    assert m["max_dd"] == 0.0
