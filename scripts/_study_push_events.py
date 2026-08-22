# -*- coding: utf-8 -*-
"""事件后验研究：资讯强档推送日 vs 无推送日（_study_push_events.py）
================================================
中期任务（影子期后）：用已累积的 pushed_events 验证"资讯方向是否真的有效"——
"有强档（bullish/bearish）推送的日子" vs "无强档推送的日子"，对比创业板指
次日/3日/5日前向收益差；并测方向 IC。有效再决定是否把资讯维度从 ±0.06 提权。

数据源：
  - pushed_events：Gist real_time_state（news_link.load_realtime_state，只读）；
  - 创业板指日线：load_index_sina(399006)。

结论判定（验门）：
  - 强档日数样本 <10 或区间跨度 <20 个自然日 → 判定"影子期数据不足，继续积累"；
  - 否则给出组间均值差、t 值、MW-p、方向 IC，仅当 bullish vs bearish 方向收益差
    与 IC 同向且 |IC|≥0.05 才算"资讯方向成立（可考虑提权）"。

仅研究/验门，不构成实盘信号；不改任何生产逻辑。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts._event_study import (  # noqa: E402
    load_cyb_closes, trading_days, _earliest_trading_day,
    summarize_group, spearman_ic, _tstat, _mannwhitney_u)
from src.strategy import news_link as nl  # noqa: E402

# 方向->数值（与 news_link._DIR_LABEL 同口径）
_DIR_NUM = {
    "bullish": 1.0, "mildly_bullish": 0.5, "neutral": 0.0, "mixed": 0.0,
    "mildly_bearish": -0.5, "bearish": -1.0,
}

# 旧 pushed_events（未存 dir 字段）的文本方向启发式
_POS_WORDS = ("买", "增持", "预增", "涨停", "利好", "中标", "涨", "超预期", "获批")
_NEG_WORDS = ("卖", "减持", "预减", "跌停", "利空", "下修", "亏", "低于预期", "风险")


def _event_dir(e: dict):
    """方向：优先 dir 字段，缺省按标题文本启发式（研究用粗粒度）。"""
    d = str(e.get("dir") or "").strip()
    if d in _DIR_NUM:
        return d
    t = str(e.get("title_norm") or "")
    score = sum(1 for w in _POS_WORDS if w in t) - sum(1 for w in _NEG_WORDS if w in t)
    if score > 0:
        return "bullish"
    if score < 0:
        return "bearish"
    return "neutral"


def main():
    rt = nl.load_realtime_state() or {}
    pe = rt.get("pushed_events") or []
    cands = rt.get("candidate_events") or []
    print("=" * 72)
    print("资讯事件后验：强档推送日 vs 无推送日 → 创业板次日/3/5日收益")
    print("=" * 72)
    print(f"状态：pushed_events={len(pe)} 条, candidate_events={len(cands)} 条")
    if not pe:
        print("结论：影子期数据不足（pushed_events 为空），继续积累。")
        return

    closes = load_cyb_closes(3000)
    days = trading_days(closes)
    if not days:
        print("结论：创业板日线加载失败，无法研究。")
        return

    # 按交易日聚合当日净方向信号（bullish天数 - bearish天数）
    from collections import defaultdict
    day_net = defaultdict(float)
    day_seen = defaultdict(set)
    for e in pe:
        d = _event_dir(e)
        cal = str(e.get("t") or "")[:10]
        E = _earliest_trading_day(days, cal)
        if E is None:
            continue
        if e.get("t") in day_seen[E]:
            continue
        day_seen[E].add(e.get("t"))
        day_net[E] += _DIR_NUM.get(d, 0.0)

    strong_days = sorted(d for d, net in day_net.items() if net != 0)
    no_days = [d for d in days if d not in day_net or day_net[d] == 0]

    print(f"样本区间：{days[0]} ~ {days[-1]}（{len(days)} 个交易日）")
    print(f"有信号交易日：{len(strong_days)}  | 无信号交易日：{len(no_days)}")
    if not strong_days:
        print("结论：尚无强档推送累积到交易日，影子期数据不足，继续积累。")
        return

    from datetime import date
    try:
        d0 = date.fromisoformat(min(strong_days))
        d1 = date.fromisoformat(max(strong_days))
        span = (d1 - d0).days
    except ValueError:
        span = 0
    print(f"信号时间跨度：{min(strong_days)} ~ {max(strong_days)}（{span} 个自然日）")

    # 门槛判定：样本数与时间跨度（影子期后应 ≥20 自然日且 ≥10 信号日）
    if span < 20 or len(strong_days) < 10:
        print(f"结论：影子期数据不足（信号日{len(strong_days)}<10 或跨度{span}<20天），"
              f"本脚本仅作演示口径，需继续积累后重跑。")
        # 仍输出当前可用统计（供参考）
        _run_comparison(closes, days, strong_days, no_days, day_net)
        return

    _run_comparison(closes, days, strong_days, no_days, day_net)
    print("\n结论判定：样本充足，以上组间差与 IC 将用于提权决策（见报告）。")


def _run_comparison(closes, days, strong_days, no_days, day_net):
    idx = {d: i for i, d in enumerate(days)}
    horizons = (1, 3, 5)
    for h in horizons:
        grp_s = []
        grp_n = []
        grp_bull = []
        grp_bear = []
        for d in strong_days:
            r = _earliest_fwd(closes, days, idx[d], h)
            if r is not None:
                grp_s.append(r)
                (grp_bull if day_net[d] > 0 else grp_bear).append(r)
        for d in no_days:
            i = idx[d]
            if i + h < len(days):
                grp_n.append(closes[days[i + h]] / closes[d] - 1.0)
        sa = summarize_group(grp_s)
        sb = summarize_group(grp_n)
        sbull = summarize_group(grp_bull)
        sbear = summarize_group(grp_bear)
        _u, p = _mannwhitney_u(grp_s, grp_n)
        _ub, pb = _mannwhitney_u(grp_bull, grp_bear)
        t_s = _tstat(grp_s)
        print(f"\n[信号日起 +{h} 个交易日收益] 有信号日 n={sa['n']} vs 无信号日 n={sb['n']}")
        print(f"   有信号均值 {sa['mean_pct']:+7.2f}% (t={t_s:+5.2f})  vs  无信号均值 {sb['mean_pct']:+7.2f}%")
        diff = sa["mean_pct"] - sb["mean_pct"]
        p_str = f"{p:.4f}" if p is not None else "n/a"
        print(f"   差(信号-无信号) {diff:+.2f}pp  [MW-p={p_str}]  {'方向一致' if diff > 0 else '方向背离'}")
        # 方向 IC：全交易日净方向 vs 前向收益
        dirs = [day_net.get(d, 0.0) for d in days]
        rets = [_earliest_fwd(closes, days, i, h) or 0.0 for i in range(len(days))]
        ic, n_ic = spearman_ic(dirs, rets)
        print(f"   bull@日 n={sbull['n']} {sbull['mean_pct']:+7.2f}% | bear@日 n={sbear['n']} {sbear['mean_pct']:+7.2f}% | "
              f"方向IC={ic:+.3f}  {'验门达标(|IC|≥0.05, n≥10)' if abs(ic) >= 0.05 and n_ic >= 10 else '未达标'}")
    print("\n说明：h 为从信号交易日收盘起的第 h 个交易日收盘的收益（E日收盘→E+h收盘）。")


def _earliest_fwd(closes, days, base_i, h):
    if base_i + h < len(days):
        return closes[days[base_i + h]] / closes[days[base_i]] - 1.0
    return None


if __name__ == "__main__":
    main()