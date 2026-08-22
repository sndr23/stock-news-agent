# -*- coding: utf-8 -*-
"""
回测交易成本敏感性（backtest_fee_sensitivity.py）
====================================================
回答"跑赢持有是否成立"：当前回测 fee=0（用户决策：场外底仓已建、以 C 类为主），
但换仓 369 次/12 年的节奏下，若涉及 A 类（申购+赎回费）结论会变。

输出：fee ∈ {0, 0.001, 0.003, 0.005} × ERP 滤波 {关, 开} 的指标对比表。
fee 语义：单次换仓的费率（双边合计；C 类 ≈0，A 类约 0.5%+）。

用法：
  python scripts/backtest_fee_sensitivity.py
  python scripts/backtest_fee_sensitivity.py --synthetic   # 冒烟（随机游走）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_chinext_timing import backtest_metrics
from scripts.walk_forward_validation import load_df, synthetic_df
from src.strategy import index_pe as ipe

FEES = (0.0, 0.001, 0.003, 0.005)
ERP_OPTS = (False, True)


def main():
    ap = argparse.ArgumentParser(description="回测交易成本敏感性")
    ap.add_argument("--synthetic", action="store_true", help="随机游走数据（冒烟）")
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

    print("回测交易成本敏感性（创业板择时 v4 核心层，全区间）")
    print(f"数据 {df.index[0].date()} ~ {df.index[-1].date()}｜"
          f"{len(df)} 根｜费用=单次换仓双边费率\n")
    print(f"{'fee':<8}{'ERP':<6}{'累计':>9}{'年化':>8}{'夏普':>7}"
          f"{'回撤':>8}{'卡玛':>7}{'换仓':>6}{'平均仓位':>8}")
    for fee in FEES:
        for erp in ERP_OPTS:
            m = backtest_metrics(df, fee=fee, pe_map=pe_map, erp_cap=erp)
            print(f"{fee:<8.3f}{str(erp):<6}{m['total'] * 100:>+8.1f}%"
                  f"{m['cagr'] * 100:>+7.1f}%{m['sharpe']:>7.2f}"
                  f"{m['mdd'] * 100:>7.1f}%{m['calmar']:>7.2f}"
                  f"{m['switches']:>6}{m['avg_pos'] * 100:>7.0f}%")
    print("\n解读：fee 从 0 → 0.5% 的累计收益衰减 = 换仓成本的真实侵蚀；"
          "若衰减后仍显著跑赢买入持有（对比 --backtest 的 bh 行），"
          "则'跑赢持有'结论对费用稳健；否则 A 类费率下需重估。")


if __name__ == "__main__":
    main()
