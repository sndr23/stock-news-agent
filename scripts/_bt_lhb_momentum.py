# -*- coding: utf-8 -*-
"""龙虎榜机构上榜后动量：分层多空回测（_bt_lhb_momentum.py）
================================================
对"机构上榜日后创业板前向收益正超额"做分层多空验证，排除幸存/聚集偏差：
  - 仅在有上榜的交易日（事件日）内，按机构动向强度（净买股数广度 / 净买总金额）
    分成 5 档（quintile），看前向 +1/+3/+5 日收益是否随强度单调；
  - 多空 = 最高档日收益 − 最低档日收益（净买卖向的多空），给 t 统计量 + MW-p；
  - 若单调且多空显著为正、样本充足，则"上榜强度→延续上行"成立，可选作氛围/情绪增强；
    若单调性断裂或多空不显著，则推翻"进核心层"结论。

口径与 _study_hard_signals 一致：事件 E（交易日公开）→ 前向收益 close[E+h]/close[E]-1。
仅研究/回测，不走生产链路。数据源 akshare stock_lhb_jgmmtj_em（周窗口滚动）。
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts._event_study import (  # noqa: E402
    load_cyb_closes, trading_days, _earliest_trading_day,
    summarize_group, spearman_ic, _tstat, _mannwhitney_u)
from scripts._study_hard_signals import fetch_lhb  # noqa: E402


def monotic(order_means):
    """五档均值是否单调（Spearman 相关于档序）。"""
    ic, n = spearman_ic(list(range(len(order_means))), list(order_means))
    return ic


def run_layered(days, closes, intensity: dict, big_amt: dict, sig_days,
                horizons=(1, 3, 5)):
    idx = {d: i for i, d in enumerate(days)}
    print("\n" + "=" * 72)
    print(f"龙虎榜上榜后动量分层：事件日 {len(sig_days)} 个")
    print("=" * 72)
    if len(sig_days) < 25:
        print("  事件日样本过少(<25)，分层不可靠，跳过。")
        return
    for h in horizons:
        print(f"\n--- 前向 +{h} 日 收益（按机构净买广度分 5 档）---")
        rows = []
        for d in sig_days:
            r = _fwd(closes, days, idx[d], h)
            if r is not None:
                rows.append((intensity[d], big_amt[d], r))
        rows.sort(key=lambda x: x[0])
        q = len(rows) // 5
        buckets = []
        for k in range(5):
            seg = rows[k * q: (k + 1) * q if k != 4 else len(rows)]
            seg = [r[2] for r in seg]
            buckets.append(segment_mean(seg))
        print("  档1(最弱)<-----档5(最强) 均值收益：")
        print("     " + "  ".join(f"档{i+1} {m*100:+.2f}%" for i, m in enumerate(buckets)))
        m = monotic(buckets)
        top = [r[2] for r in rows[int(len(rows) * 0.8):]]
        bot = [r[2] for r in rows[: int(len(rows) * 0.2)]]
        ta = summarize_group(top)
        ba = summarize_group(bot)
        _u, p = _mannwhitney_u(top, bot)
        spread = ta["mean_pct"] - ba["mean_pct"]
        verdict = "单调↑/多空为正" if m > 0.6 and spread > 0 else ("单调↓/多空为负" if m < -0.6 and spread < 0 else "单调性差")
        print(f"  档5-档1 多空：{spread:+.2f}pp  (top n={ta['n']}, bot n={ba['n']}, MW-p={_p(p)})  {verdict}")
        print(f"  档间单调 Spearman={m:+.3f}  （>0.6 视为严格单调）")


def segment_mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _fwd(closes, days, i, h):
    if i + h < len(days):
        return closes[days[i + h]] / closes[days[i]] - 1.0
    return None


def _p(x):
    return f"{x:.4f}" if x is not None else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=78, help="龙虎榜回看周数")
    args = ap.parse_args()

    print(f"拉取龙虎榜机构历史（{args.weeks} 周）…", flush=True)
    events = fetch_lhb(args.weeks)
    closes = load_cyb_closes(3000)
    days = trading_days(closes)
    if not events or not days:
        print("数据为空，退出。")
        return

    # 每日聚合：净买广度（#净买股−#净卖股）与净买总金额（亿元）
    breadth = defaultdict(float)
    gross = defaultdict(float)
    sig_days_set = set()
    for ev in events:
        net = ev.get("net", 0) or 0
        E = _earliest_trading_day(days, ev.get("pub", ""))
        if E is None:
            continue
        if net >= 0:
            breadth[E] += 1.0
            gross[E] += net
        else:
            breadth[E] -= 1.0
            gross[E] -= net  # 净卖以同号累加（负强度）
        sig_days_set.add(E)
    sig_days = sorted(sig_days_set)
    for d in sig_days:
        gross[d] = gross[d] / 1e8  # -> 亿元

    run_layered(days, closes, breadth, gross, sig_days)


if __name__ == "__main__":
    main()