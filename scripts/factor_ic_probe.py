# -*- coding: utf-8 -*-
"""
候选因子历史 IC 验证探针（factor_ic_probe.py）
====================================================
对候选因子（目前：短期反转 short_reversal_5）做历史 IC 验证，
按验门纪律判定是否可接入核心层权重（|IC|≥0.05 且样本≥10）。

输出：全历史 + 近 3 年分段的 Spearman IC（vs 次日/后 5 日收益），
     及验门判定。分段看稳健性——全历史达标但近 3 年反转（符号变）→ 不接入。

用法：
  python scripts/factor_ic_probe.py
  python scripts/factor_ic_probe.py --synthetic   # 冒烟（随机游走）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_factors as cf
from src.strategy import chinext_timing as ct
from scripts.walk_forward_validation import load_df, synthetic_df

GATE_IC = 0.05   # 验门：|IC| 门槛
GATE_N = 10      # 验门：最小样本


def ic_series(factor: list, closes: list, fwd: int):
    """因子序列 vs 前瞻收益：返回 [(因子值, 前瞻收益)]（两端对齐去 None）。"""
    pairs = []
    for i in range(len(factor)):
        if factor[i] is None or i + fwd >= len(closes):
            continue
        base = closes[i]
        if not base:
            continue
        pairs.append((float(factor[i]), closes[i + fwd] / base - 1.0))
    return pairs


def report(name: str, factor: list, closes: list, n_years: int = 3):
    print(f"\n── {name} ──")
    for fwd, label in ((1, "次日"), (5, "后5日")):
        pairs = ic_series(factor, closes, fwd)
        if len(pairs) < GATE_N:
            print(f"  {label}: 样本不足（n={len(pairs)}）")
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        ic = ct.spearman_ic(xs, ys)
        # 近 n 年分段（尾部）
        seg = pairs[-(n_years * 244):]
        if len(seg) >= GATE_N:
            ic_seg = ct.spearman_ic([p[0] for p in seg], [p[1] for p in seg])
            seg_tag = f"近{n_years}年 {ic_seg:+.4f}"
            sign_ok = (ic >= 0) == (ic_seg >= 0)
        else:
            ic_seg = None
            seg_tag = "分段样本不足"
            sign_ok = True
        gate = abs(ic) >= GATE_IC and len(pairs) >= GATE_N and sign_ok
        print(f"  {label}: 全历史 IC={ic:+.4f}（n={len(pairs)}）｜{seg_tag}"
              f"｜{'✓ 通过验门' if gate else '✗ 未过（不接入）'}")


def main():
    ap = argparse.ArgumentParser(description="候选因子历史 IC 验证探针")
    ap.add_argument("--synthetic", action="store_true", help="随机游走数据（冒烟）")
    args = ap.parse_args()

    if args.synthetic:
        df = synthetic_df()
        print("⚠ 合成数据（随机游走）——仅验证流程，无量化含义\n")
    else:
        df = load_df()
        if df is None or df.empty:
            raise SystemExit("399006 日线获取失败（无网络可用 --synthetic 冒烟）")

    closes = df["close"].tolist()
    print(f"数据 {df.index[0].date()} ~ {df.index[-1].date()}｜{len(closes)} 根")

    # 候选 1：短期反转（5 日）
    report("短期反转 short_reversal_5（候选）",
           cf.factor_short_reversal(closes, horizon=5), closes)

    # 对照：核心层既有因子（趋势/动量）——看候选相对既有是否补充信息
    report("对照·60日动量（核心层已有）",
           cf.factor_momentum_60(closes), closes)
    print("\n说明：验门通过（|IC|≥0.05、样本≥10、近3年符号一致）才建议接入权重；"
          "候选因子与既有因子低相关时接入才有增量。")


if __name__ == "__main__":
    main()
