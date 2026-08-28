# -*- coding: utf-8 -*-
"""
walk-forward 样本外验证（walk_forward_validation.py）
====================================================
目的：回答"因子权重/档位线是否过拟合"——当前参数在 12 年同一样本上
in-sample 寻优，本脚本做时间序列滚动切分，让训练段定参、测试段评估。

切分（默认）：训练 3 年 / 测试 1 年，逐年滚动。每折：
  1) 训练段：候选参数网格（标准/宽松档位线 × ERP 滤波）逐一回测，按卡玛选最优；
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


def _configure_stdout() -> None:
    """Windows 控制台无法编码诊断符号时，替换而不是中断验证。"""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

import pandas as pd

from scripts.run_chinext_timing import backtest_metrics
from src.strategy import chinext_timing as ct
from src.strategy import index_pe as ipe

TRADING_DAYS = 244

# 候选参数网格（训练段寻优空间，务必保持小——每折跑 网格数 次回测）
TIER_CANDIDATES = {
    "标准": ct.TIERS,                                   # (0.40,1.0),(-0.15,0.9),(-0.30,0.6)
    # 该组阈值更低，实际更容易升到高仓位，命名为“宽松”避免误导。
    "宽松": ((0.35, 1.0), (-0.10, 0.9), (-0.25, 0.6)),
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
    train_start, train_end = train
    # 保留训练段之前的历史供滚动因子 warmup；评估收益只到训练段末端。
    dft = df.iloc[:train_end]
    eval_start = max(60, train_start)
    eval_end = train_end - 1
    best = None
    for tname, tiers in TIER_CANDIDATES.items():
        for erp in ERP_CANDIDATES:
            m = backtest_metrics(dft, fee=fee, pe_map=pe_map,
                                 tiers=tiers, erp_cap=erp,
                                 eval_start=eval_start, eval_end=eval_end)
            key = (tname, erp)
            if best is None or m["calmar"] > best[2]:
                best = (key, (tiers, erp), m["calmar"])
    return best


def evaluate_test(df: pd.DataFrame, test: tuple, params, pe_map,
                  fee: float, initial_prev: dict = None) -> dict:
    """测试段评估：全历史计算因子，收益和状态从测试起点开始。"""
    tiers, erp_cap = params
    test_start, test_end = test
    if initial_prev is None:
        # 首折从空仓重放到测试段边界，避免状态机冷启动影响 OOS。
        boundary = backtest_metrics(
            df.iloc[:test_start + 1], fee=fee, pe_map=pe_map,
            tiers=tiers, erp_cap=erp_cap, eval_end=test_start,
        )
        initial_prev = boundary["final_state"]
    # 因子使用完整历史；收益、费用和统计严格限制在测试段。
    return backtest_metrics(
        df.iloc[:test_end + 1], fee=fee, pe_map=pe_map,
        tiers=tiers, erp_cap=erp_cap, eval_start=test_start,
        eval_end=test_end, initial_prev=initial_prev,
    )


def _curve_stats(returns: list) -> dict:
    """从按时间拼接的日收益计算净值、年化、夏普和最大回撤。"""
    nav = 1.0
    navs = []
    for ret in returns:
        nav *= 1.0 + ret
        navs.append(nav)
    years = len(navs) / TRADING_DAYS
    cagr = nav ** (1 / years) - 1 if years > 0 else 0.0
    equity_curve = [1.0] + navs
    mdd = min(v / max(equity_curve[:i + 1]) - 1.0
              for i, v in enumerate(equity_curve)) if navs else 0.0
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = (sum((ret - mean) ** 2 for ret in returns) /
                max(1, len(returns) - 1)) if returns else 0.0
    sd = variance ** 0.5
    sharpe = mean / sd * (TRADING_DAYS ** 0.5) if sd > 0 else 0.0
    return {"total": nav - 1.0, "cagr": cagr, "sharpe": sharpe,
            "mdd": mdd, "navs": navs}


def summarize_oos(rows: list) -> dict:
    """汇总连续 OOS 测试段，避免用各折简单平均代替整体表现。"""
    returns = [ret for row in rows for ret in row.get("daily_rets", [])]
    bh_returns = []
    for row in rows:
        navs = row.get("bh_navs", [])
        previous = 1.0
        for nav in navs:
            bh_returns.append(nav / previous - 1.0)
            previous = nav
    strategy = _curve_stats(returns)
    benchmark = _curve_stats(bh_returns)
    return {**strategy, "bh": benchmark["total"], "bh_cagr": benchmark["cagr"],
            "bh_sharpe": benchmark["sharpe"], "bh_mdd": benchmark["mdd"],
            "n_navs": len(returns), "avg_sharpe": (
                sum(row["sharpe"] for row in rows) / len(rows) if rows else 0.0),
            "avg_calmar": (
                sum(row["calmar"] for row in rows) / len(rows) if rows else 0.0)}


def main():
    _configure_stdout()
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
          f"{'OOS累计':>9}{'年化':>8}{'夏普':>7}{'回撤':>8}{'卡玛':>7}{'持有':>9}")

    oos_rows = []
    params_hist = []
    running_state = None
    previous_params = None
    for i, (train, test) in enumerate(folds, 1):
        (tname, erp), _params, train_calmar = best_on_train(
            df, train, pe_map, args.fee)
        params_key = (tname, bool(erp))
        # 参数切换时旧 pending 不再属于新参数定义；实际仓位保持连续。
        if running_state is not None and params_key != previous_params:
            running_state = {"position": running_state.get("position", 0.0),
                             "pending": None}
        m = evaluate_test(df, test, _params, pe_map, args.fee,
                          initial_prev=running_state)
        oos_rows.append(m)
        running_state = m["final_state"]
        previous_params = params_key
        params_hist.append(params_key)
        ts = df.index[train[0]].date(), df.index[train[1] - 1].date()
        es = df.index[test[0]].date(), df.index[test[1] - 1].date()
        print(f"{i:<4}{str(ts[0])+'~'+str(ts[1]):<24}{str(es[0])+'~'+str(es[1]):<24}"
              f"{tname + ('+ERP' if erp else ''):<20}"
              f"{m['total'] * 100:>+8.1f}%{m['cagr'] * 100:>+7.1f}%"
              f"{m['sharpe']:>7.2f}{m['mdd'] * 100:>7.1f}%"
              f"{m['calmar']:>7.2f}{m['bh'] * 100:>+8.1f}%")

    # 汇总诊断：按日收益拼接连续测试段，不把各折指标简单平均当作整体表现。
    summary = summarize_oos(oos_rows)
    stable = len(set(params_hist)) == 1
    print("\n───── 汇总诊断 ─────")
    print(f"OOS 复合累计 {summary['total'] * 100:+.1f}% ｜ 年化 {summary['cagr'] * 100:+.1f}% ｜ "
          f"夏普 {summary['sharpe']:.2f} ｜ 最大回撤 {summary['mdd']:.1%} ｜ "
          f"卡玛 {summary['cagr'] / abs(summary['mdd']) if summary['mdd'] else 0.0:.2f}")
    print(f"OOS 买入持有累计 {summary['bh'] * 100:+.1f}% ｜ 年化 {summary['bh_cagr'] * 100:+.1f}% ｜ "
          f"最大回撤 {summary['bh_mdd']:.1%} ｜ 各折平均夏普 {summary['avg_sharpe']:.2f} ｜ "
          f"各折平均卡玛 {summary['avg_calmar']:.2f}")
    if stable:
        print(f"最优参数稳定性：全部折一致（{params_hist[0]}）✓")
    else:
        print(f"最优参数稳定性：跨折漂移（{params_hist}）✗——参数对样本敏感，过拟合信号")

    # 与全样本 in-sample 对比（同参数默认档）
    if not args.synthetic:
        m_full = backtest_metrics(df, fee=args.fee, pe_map=pe_map, erp_cap=True)
        print(f"\n全样本 in-sample 对比（默认参数+ERP）：累计 {m_full['total'] * 100:+.1f}% ｜ "
              f"夏普 {m_full['sharpe']:.2f} ｜ 卡玛 {m_full['calmar']:.2f}")
        oos_cal = summary["cagr"] / abs(summary["mdd"]) if summary["mdd"] else 0.0
        gap = m_full["calmar"] - oos_cal
        verdict = ("⚠ 卡玛差距显著（in-sample 明显好于 OOS）——存在过拟合，"
                   "实盘预期弱于回测" if gap > 0.15 else
                   "OOS 与 in-sample 差距在可接受范围（<0.15 卡玛）")
        print(f"卡玛差距 {gap:+.2f} → {verdict}")
    print("\n口径：训练段按卡玛选参（标准/宽松档位线×ERP 滤波网格），测试段独立评估；"
          "因子只用既往数据、吃次日收益（对齐场外基金 T+1）。")


if __name__ == "__main__":
    main()
