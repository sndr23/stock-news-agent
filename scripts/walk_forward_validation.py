# -*- coding: utf-8 -*-
"""
walk-forward 样本外验证（walk_forward_validation.py）
====================================================
目的：回答"因子权重/档位线是否过拟合"——当前参数在 12 年同一样本上
in-sample 寻优，本脚本做时间序列滚动切分，让训练段定参、测试段评估。

切分（默认）：训练 3 年 / 测试 1 年，逐年滚动。每折：
  1) 训练段：候选参数网格（档位线 × ERP 滤波）逐一回测，按卡玛选最优；
  2) 测试段：用训练段选出的最优参数评估（含 60 日 warmup 前缀）；
  3) 记录该折测试段指标。

诊断输出：
  - 各折 OOS 指标 vs 全样本 in-sample 指标 → 差距大 = 过拟合信号；
  - 各折最优参数是否稳定（换参数频繁 = 参数不稳定信号）。

用法：
  python scripts/walk_forward_validation.py                # 默认切分
  python scripts/walk_forward_validation.py --train-years 2 --test-years 1
  python scripts/walk_forward_validation.py --fee 0.003    # 带申赎费

说明：数据加载复用 run_chinext_timing 的现成链路（新浪全量 + 创业板50 PE），
无网络环境下可用 --synthetic 生成随机游走数据做冒烟验证。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

import pandas as pd

from scripts.run_chinext_timing import backtest_metrics
from src.strategy import chinext_timing as ct
from src.strategy import index_pe as ipe

TRADING_DAYS = 244

# 候选参数网格（训练段寻优空间，务必保持小——每折跑 网格数 次回测）
TIER_CANDIDATES = {
    "标准": ct.TIERS,                                   # (0.40,1.0),(-0.15,0.9),(-0.30,0.6)
    "保守": ((0.35, 1.0), (-0.10, 0.9), (-0.25, 0.6)),  # 更易满仓/更早降档
}
ERP_CANDIDATES = (False, True)   # 是否启用"估值极贵封顶6成"


def load_df() -> pd.DataFrame:
    """加载 399006 日线（新浪全量优先）。失败返回空。"""
    from scripts.run_chinext_timing import load_index_daily_full, load_index_sina
    df = load_index_sina("399006")
    if df is None or df.empty:
        df = load_index_daily_full("399006", "20200101")
    return df


def synthetic_df(days: int = 4000, seed: int = 42) -> pd.DataFrame:
    """随机游走合成数据（冒烟验证用，无网络环境跑通流程）。"""
    import numpy as np
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0003, 0.012, days)
    closes = 1000.0 * np.cumprod(1 + rets)
    dates = pd.bdate_range(end=pd.Timestamp("2026-08-21"), periods=days)
    return pd.DataFrame({
        "close": closes,
        "amount": rng.uniform(1e8, 5e8, days),
    }, index=dates)


def split_folds(dates: pd.DatetimeIndex, train_years: int, test_years: int):
    """滚动切分：返回 [(train_slice, test_slice)]，slice 为日期索引下标区间。"""
    total = len(dates)
    start = 0
    train_n = train_years * TRADING_DAYS
    test_n = test_years * TRADING_DAYS
    folds = []
    while start + train_n + 60 + test_n <= total:
        train_lo, train_hi = start, start + train_n
        test_lo = train_hi
        test_hi = train_hi + test_n
        folds.append(((train_lo, train_hi), (test_lo, test_hi)))
        start += test_n
    return folds


def best_on_train(df: pd.DataFrame, train: tuple, pe_map, fee: float) -> tuple:
    """训练段网格寻优：返回 (最优参数名, (tiers, erp_cap), 训练段卡玛)。"""
    dft = df.iloc[train[0]:train[1]]
    best = None
    for tname, tiers in TIER_CANDIDATES.items():
        for erp in ERP_CANDIDATES:
            m = backtest_metrics(dft, fee=fee, pe_map=pe_map,
                                 tiers=tiers, erp_cap=erp)
            key = (tname, erp)
            if best is None or m["calmar"] > best[2]:
                best = (key, (tiers, erp), m["calmar"])
    return best


def evaluate_test(df: pd.DataFrame, test: tuple, params, pe_map,
                  fee: float) -> dict:
    """测试段评估（含 60 日 warmup 前缀，指标从测试段起点算起）。"""
    tiers, erp_cap = params
    lo = max(0, test[0] - 60)
    dft = df.iloc[lo:test[1]]
    m = backtest_metrics(dft, fee=fee, pe_map=pe_map,
                         tiers=tiers, erp_cap=erp_cap)
    # navs 从 index 60 起（即 test[0]），指标即测试段口径
    return m


def main():
    ap = argparse.ArgumentParser(description="walk-forward 样本外验证")
    ap.add_argument("--train-years", type=int, default=3)
    ap.add_argument("--test-years", type=int, default=1)
    ap.add_argument("--fee", type=float, default=0.0, help="单次换仓成本（如 0.003）")
    ap.add_argument("--synthetic", action="store_true", help="用随机游走数据（冒烟）")
    args = ap.parse_args()

    if args.synthetic:
        df = synthetic_df()
        pe_map = None
        print("⚠ 合成数据（随机游走）——仅验证流程，无量化含义\n")
    else:
        df = load_df()
        if df is None or df.empty:
            raise SystemExit("399006 日线获取失败（无网络可用 --synthetic 冒烟）")
        pe_map = ipe.load_cy50_pe(PROJECT_ROOT)

    folds = split_folds(df.index, args.train_years, args.test_years)
    if not folds:
        raise SystemExit("数据长度不足一个训练+测试折（需 "
                         f"{(args.train_years + args.test_years) * TRADING_DAYS + 60} 根）")

    print(f"walk-forward 样本外验证｜训练 {args.train_years} 年 / 测试 {args.test_years} 年"
          f"｜fee={args.fee:.3f}｜共 {len(folds)} 折\n")
    print(f"{'折':<4}{'训练区间':<24}{'测试区间':<24}{'最优参数':<20}"
          f"{'OOS累计':>9}{'OOS夏普':>8}{'OOS回撤':>9}{'OOS卡玛':>8}")

    oos_rows = []
    params_hist = []
    for i, (train, test) in enumerate(folds, 1):
        (tname, erp), _params, train_calmar = best_on_train(
            df, train, pe_map, args.fee)
        m = evaluate_test(df, test, _params, pe_map, args.fee)
        oos_rows.append(m)
        params_hist.append(tname)
        ts = df.index[train[0]].date(), df.index[train[1] - 1].date()
        es = df.index[test[0]].date(), df.index[test[1] - 1].date()
        print(f"{i:<4}{str(ts[0])+'~'+str(ts[1]):<24}{str(es[0])+'~'+str(es[1]):<24}"
              f"{tname + ('+ERP' if erp else ''):<20}"
              f"{m['total'] * 100:>+8.1f}%{m['sharpe']:>8.2f}"
              f"{m['mdd'] * 100:>8.1f}%{m['calmar']:>8.2f}")

    # 汇总诊断
    import statistics
    oos_tot = statistics.mean([m["total"] for m in oos_rows])
    oos_cal = statistics.mean([m["calmar"] for m in oos_rows])
    oos_shp = statistics.mean([m["sharpe"] for m in oos_rows])
    stable = len(set(params_hist)) == 1
    print("\n───── 汇总诊断 ─────")
    print(f"OOS 平均累计 {oos_tot * 100:+.1f}% ｜ OOS 平均夏普 {oos_shp:.2f} ｜ "
          f"OOS 平均卡玛 {oos_cal:.2f}")
    if stable:
        print(f"最优参数稳定性：全部折一致（{params_hist[0]}）✓")
    else:
        print(f"最优参数稳定性：跨折漂移（{params_hist}）✗——参数对样本敏感，过拟合信号")

    # 与全样本 in-sample 对比（同参数默认档）
    if not args.synthetic:
        m_full = backtest_metrics(df, fee=args.fee, pe_map=pe_map, erp_cap=True)
        print(f"\n全样本 in-sample 对比（默认参数+ERP）：累计 {m_full['total'] * 100:+.1f}% ｜ "
              f"夏普 {m_full['sharpe']:.2f} ｜ 卡玛 {m_full['calmar']:.2f}")
        gap = m_full["calmar"] - oos_cal
        verdict = ("⚠ 卡玛差距显著（in-sample 明显好于 OOS）——存在过拟合，"
                   "实盘预期弱于回测" if gap > 0.15 else
                   "OOS 与 in-sample 差距在可接受范围（<0.15 卡玛）")
        print(f"卡玛差距 {gap:+.2f} → {verdict}")
    print("\n口径：训练段按卡玛选参（档位线×ERP 滤波网格），测试段独立评估；"
          "因子只用既往数据、吃次日收益（对齐场外基金 T+1）。")


if __name__ == "__main__":
    main()
