# -*- coding: utf-8 -*-
"""事件硬信号研究：龙虎榜机构动向 + 业绩预告 → 创业板次日/3/5日收益（_study_hard_signals.py）
================================================
补上"能回测、能验门"的事件硬信号。龙虎榜机构净买净卖、业绩预告均为交易所官方
结构化披露、不依赖 LLM，可做纯历史事件研究：
  - 按交易日聚合净方向信号（bullish 事件数 - bearish 事件数）；
  - 对比"有信号日 vs 无信号日"创业板指前向 1/3/5 日收益差；
  - 方向 Spearman IC 验门：|IC|≥0.05 且样本≥10 → 影子转正可进核心层。

数据源（akshare 官方接口，历史可拉取）：
  - 业绩预告：stock_yjyg_em(date=报告期末)，回看近 N 个季度（含净利润/扣非多行，去重取净利润）；
  - 龙虎榜机构：stock_lhb_jgmmtj_em(start_date,end_date) 周窗口滚动回看近 M 周；
  - 指数：load_index_sina(399006)。

口径（防前视）：事件在交易日 E 公开，前向收益自 E 收盘起算（close[E+H]/close[E]-1）。
仅研究/验门，不改生产逻辑；打点耗时，建议后台运行。
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts._event_study import (  # noqa: E402
    load_cyb_closes, trading_days, _earliest_trading_day,
    summarize_group, spearman_ic, _tstat, _mannwhitney_u)

LHB_NET_BUY_MIN = 30000000  # 机构净买入 ≥ 3000万（与生产 data_fetchers 同口径）
LHB_NET_SELL_MIN = -30000000  # 机构净卖出 ≤ -3000万
YJ_AMP_RISE = 100.0  # 预增显著阈值
YJ_AMP_FALL = -50.0  # 预减显著阈值

# 业绩预告类型 -> 方向
_YJ_DIR = {
    "预增": "bullish", "略增": "bullish", "扭亏": "bullish",
    "续盈": "bullish", "预盈": "bullish",
    "预减": "bearish", "略减": "bearish", "首亏": "bearish",
    "续亏": "bearish", "减亏": "bearish", "预亏": "bearish",
}


def log(msg):
    print(msg, flush=True)


def fetch_yjyg(quarters: int) -> list:
    """回看近 quarters 个季度末的业绩预告（净利润为主事件，去重）。"""
    import akshare as ak
    today = date.today()
    ends = []
    seen_keys = set()
    for m, d in [(3, 31), (6, 30), (9, 30), (12, 31)]:
        rd = today.replace(month=m, day=d)
        if rd > today:
            rd = rd.replace(year=rd.year - 1)
        ends.append(rd)
    ends = sorted(set(ends), reverse=True)[:quarters]
    events = []
    for rd in ends:
        date_str = rd.strftime("%Y%m%d")
        try:
            df = ak.stock_yjyg_em(date=date_str)
        except Exception as e:
            log(f"  [yjyg {date_str}] 拉取失败: {e}")
            continue
        if df is None or df.empty:
            continue
        # 每股每公告取"归属于上市公司股东的净利润"优先；无则取首行
        per_key = {}
        for _, row in df.iterrows():
            code = str(row.get("股票代码", ""))
            pub = str(row.get("公告日期", ""))[:10]
            if not pub or not pub.startswith("20"):
                continue
            metric = str(row.get("预测指标", ""))
            key = (code, pub)
            cur = per_key.get(key)
            if cur is None or ("归属于上市公司股东的净利润" in metric and "扣除非经常" not in metric):
                per_key[key] = {
                    "code": code, "name": str(row.get("股票简称", "")),
                    "pub": pub, "type": str(row.get("预告类型", "")),
                    "amp": _to_f(row.get("业绩变动幅度")),
                    "metric": metric,
                }
        for k, e in per_key.items():
            if k in seen_keys:
                continue
            seen_keys.add(k)
            events.append(e)
        log(f"  [yjyg {date_str}] {len(per_key)} 条事件（去重后累计 {len(events)}）")
    return events


def fetch_lhb(weeks: int) -> list:
    """周窗口滚动回看近 weeks 周龙虎榜机构动向。"""
    import akshare as ak
    events = []
    end = date.today()
    for _ in range(weeks):
        start = end - timedelta(days=7)
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        try:
            df = ak.stock_lhb_jgmmtj_em(start_date=s, end_date=e)
        except Exception as ex:
            log(f"  [lhb {s}~{e}] 拉取失败: {ex}")
            end = start
            continue
        for _, row in df.iterrows():
            net = _to_f(row.get("机构买入净额"))
            pub = str(row.get("上榜日期", ""))[:10]
            if not pub.startswith("20"):
                continue
            if net is None:
                continue
            if net >= LHB_NET_BUY_MIN:
                events.append({"code": str(row.get("代码", "")),
                               "name": str(row.get("名称", "")),
                               "pub": pub, "net": net, "kind": "lhb"})
            elif net <= LHB_NET_SELL_MIN:
                events.append({"code": str(row.get("代码", "")),
                               "name": str(row.get("名称", "")),
                               "pub": pub, "net": net, "kind": "lhb"})
        log(f"  [lhb {s}~{e}] 周累计净买/净卖事件 {len(events)}")
        end = start
        if len(events) > 8000:  # 防爆量
            break
    return events


def _to_f(v):
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return None


def _yj_direction(e: dict):
    """业绩预告方向：类型映射 → 幅度阈值复核（显著才计信号）。"""
    d = _YJ_DIR.get(e.get("type", ""))
    amp = e.get("amp")
    if d == "bullish" and (amp is None or amp < YJ_AMP_RISE):
        return None
    if d == "bearish" and (amp is None or amp > YJ_AMP_FALL):
        return None
    return d


def aggregate_signal_days(events, kind_filter, days) -> dict:
    """事件日 -> 净方向信号（bullish 数 - bearish 数），按交易日 E 聚合。"""
    net = defaultdict(float)
    for ev in events:
        pub = ev.get("pub", "")
        E = _earliest_trading_day(days, pub)
        if E is None:
            continue
        d = _yj_direction(ev) if kind_filter == "yjyg" else ("bullish" if ev.get("net", 0) >= 0 else "bearish")
        if d:
            net[E] += 1.0 if d == "bullish" else -1.0
    return net


def run_study(title, events, kind_filter, closes, days, horizons=(1, 3, 5)):
    idx = {d: i for i, d in enumerate(days)}
    day_net = aggregate_signal_days(events, kind_filter, days)
    # 有信号日 vs 无信号日
    sig_days = sorted(d for d, n in day_net.items() if n != 0)
    bull_days = sorted(d for d, n in day_net.items() if n > 0)
    bear_days = sorted(d for d, n in day_net.items() if n < 0)
    no_days = [d for d in days if d not in day_net or day_net[d] == 0]
    print("\n" + "=" * 72)
    print(f"事件研究：{title}")
    print(f"事件数={len(events)}  有信号交易日={len(sig_days)} "
          f"(bullish@日={len(bull_days)} / bearish@日={len(bear_days)})  无信号交易日={len(no_days)}")
    print("=" * 72)
    if not sig_days:
        print("  信号日为空，跳过。")
        return

    for h in horizons:
        g_sig, g_no, g_bull, g_bear = [], [], [], []
        for d in sig_days:
            r = _fwd(closes, days, idx[d], h)
            if r is not None:
                g_sig.append(r)
                if day_net[d] > 0:
                    g_bull.append(r)
                else:
                    g_bear.append(r)
        for d in no_days:
            r = _fwd(closes, days, idx[d], h)
            if r is not None:
                g_no.append(r)
        # IC：全交易日 net vs fwd
        dirs = [day_net.get(d, 0.0) for d in days]
        rets = [_fwd(closes, days, i, h) or 0.0 for i in range(len(days))]
        implements_ic, n_ic = spearman_ic(dirs, rets)
        sa = summarize_group(g_sig)
        sn = summarize_group(g_no)
        sbull = summarize_group(g_bull)
        sbear = summarize_group(g_bear)
        _ua, pa = _mannwhitney_u(g_sig, g_no)
        _ub, pb = _mannwhitney_u(g_bull, g_bear)
        print(f"\n[+{h}日] 有信号 n={sa['n']} 均值{sa['mean_pct']:+7.2f}% | "
              f"无信号 n={sn['n']} 均值{sn['mean_pct']:+7.2f}% | 差{sa['mean_pct']-sn['mean_pct']:+6.2f}pp (MW-p={_p(pa)})")
        print(f"    bull@日 n={sbull['n']} {sbull['mean_pct']:+7.2f}%  |  bear@日 n={sbear['n']} {sbear['mean_pct']:+7.2f}%  |  bull-bear 差 {sbull['mean_pct']-sbear['mean_pct']:+6.2f}pp (MW-p={_p(pb)})")
        verdict = "← 方向IC达标(|IC|≥0.05)" if abs(implements_ic) >= 0.05 and n_ic >= 10 else "← IC未达标/样本不足"
        print(f"    方向 Spearman IC={implements_ic:+.3f} (n={n_ic}) {verdict}")
    return sig_days


def _fwd(closes, days, i, h):
    if i + h < len(days):
        return closes[days[i + h]] / closes[days[i]] - 1.0
    return None


def _p(x):
    return f"{x:.4f}" if x is not None else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=20, help="业绩预告回看季度数")
    ap.add_argument("--weeks", type=int, default=78, help="龙虎榜回看周数")
    ap.add_argument("--no-yjyg", action="store_true", help="跳过业绩预告")
    ap.add_argument("--no-lhb", action="store_true", help="跳过龙虎榜")
    args = ap.parse_args()

    closes = load_cyb_closes(3000)
    days = trading_days(closes)
    if not days:
        print("创业板日线加载失败。")
        return
    k = 0
    if not args.no_yjyg:
        log("[1/2] 拉取业绩预告历史…")
        yj = fetch_yjyg(args.quarters)
        run_study(f"业绩预告（近{args.quarters}季度, 净利润事件）", yj, "yjyg", closes, days)
        k += 1
    if not args.no_lhb:
        log("[2/2] 拉取龙虎榜机构动向历史…")
        lhb = fetch_lhb(args.weeks)
        run_study(f"龙虎榜机构（近{args.weeks}周, 净买≥3kw/净卖≤-3kw）", lhb, "lhb", closes, days)
        k += 1
    print("\n验门槛：方向 Spearman IC |IC|≥0.05 且样本≥10 → 影子转正可进核心层（需结合回测与纪律复核）。")


if __name__ == "__main__":
    main()