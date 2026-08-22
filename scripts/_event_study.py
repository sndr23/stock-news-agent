# -*- coding: utf-8 -*-
"""事件研究引擎（_event_study.py）
================================================
把"客观/已记录事件 → 创业板指(399006) 次日/3日/5日 前向收益"做成可复用的统计引擎，
供两类研究复用：
  1. 资讯事件后验（pushed_events 强档推送日 vs 无推送日）；
  2. 硬信号事件研究（龙虎榜机构净买 / 业绩预告）。

口径（防前视）：
  - 事件日 event_date（日历日 c）→ 落到 ≥c 的首个交易日 E（事件在 E 当日/盘后已公开）；
  - 前向收益从 E 的收盘起算：fwd_H(E) = close[E+H] / close[E] - 1（H 个交易日）；
  - 仅用历史回看已验证可拉取的客观结构化事件，不依赖 LLM 判定。

产出：按交易日聚合的方向信号（bullish/bearish 天数差）与各 horizon 前向收益的
组间对比（t 检验 + Mann-Whitney U）+ Spearman 方向 IC。
仅用于研究/验门，不构成实盘信号。
"""
from __future__ import annotations

import math
import statistics as st
from collections import OrderedDict


# ---------------- 指数日线 ----------------
def load_cyb_closes(datalen: int = 3000) -> OrderedDict:
    """创业板指(399006) 收盘序：{YYYY-MM-DD: close}，缺源抛错由调用方降级。"""
    from src.strategy.data import load_index_sina
    df = load_index_sina("399006", datalen)
    out = OrderedDict()
    for idx, row in df.iterrows():
        c = row.get("close")
        if c is None or math.isnan(float(c)):
            continue
        day = str(idx.date())
        # 日期升序去重，保留末值
        out[day] = float(c)
    return out


def trading_days(closes: OrderedDict) -> list:
    return list(closes.keys())


def _earliest_trading_day(days: list, cal_date: str) -> str | None:
    """落到 ≥cal_date 的首个交易日；超出样本返回 None。"""
    for d in days:
        if d >= cal_date:
            return d
    return None


def index_return_to(closes: OrderedDict, days: list, e_idx: int) -> dict:
    """从第 e_idx 个交易日收盘起，未来 1/3/5 个交易日的前向收益（小数）。"""
    out = {}
    base = closes[days[e_idx]]
    for h in (1, 3, 5):
        if e_idx + h < len(days):
            out[h] = closes[days[e_idx + h]] / base - 1.0
    return out


# ---------------- 统计工具 ----------------
def _tstat(xs) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 0.0
    return m / math.sqrt(var / n)


def _mannwhitney_u(a, b):
    """两独立样本 Mann-Whitney U 检验，返回 (U, 双侧近似 p)。样本小或有大量 0 时近似粗糙。"""
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        return None, None
    merged = sorted((x, 0) for x in a) + [(x, 1) for x in b]
    merged.sort(key=lambda r: r[0])
    ranks = {}
    i = 0
    while i < len(merged):
        j = i
        while j < len(merged) and merged[j][0] == merged[i][0]:
            j += 1
        rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[id(merged[k])] = rank
        i = j
    rank_a = [ranks[id(m)] for m in merged if m[1] == 0]
    rank_b = [ranks[id(m)] for m in merged if m[1] == 1]
    u1 = sum(rank_a) - na * (na + 1) / 2.0
    u2 = sum(rank_b) - nb * (nb + 1) / 2.0
    u = min(u1, u2)
    mu = na * nb / 2.0
    sigma = math.sqrt(na * nb * (na + nb + 1) / 12.0)
    if sigma <= 0:
        return u, None
    z = (u - mu) / sigma
    # 双侧近似 p（标准正态）
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return u, p


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def summarize_group(rets: list) -> dict:
    """单组前向收益统计。"""
    n = len(rets)
    if n == 0:
        return {"n": 0, "mean_pct": 0.0, "std_pct": 0.0, "t": 0.0, "pos": 0.0}
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / (n - 1) if n > 1 else 0.0
    return {
        "n": n,
        "mean_pct": mean * 100.0,
        "std_pct": math.sqrt(var) * 100.0 if var > 0 else 0.0,
        "t": _tstat(rets),
        "pos": sum(1 for x in rets if x > 0) / n,
    }


def spearman_ic(dirs: list, rets: list):
    """Spearman 相关（IC）：dirs 为数值方向序列，rets 为对应前向收益。"""
    pairs = [(d, r) for d, r in zip(dirs, rets) if d is not None and r is not None]
    n = len(pairs)
    if n < 3:
        return 0.0, n
    def _rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j < len(order) and vals[order[j]] == vals[order[i]]:
                j += 1
            avg = (i + 1 + j) / 2.0
            for k in range(i, j):
                rk[order[k]] = avg
            i = j
        return rk
    rd = _rank([p[0] for p in pairs])
    rr = _rank([p[1] for p in pairs])
    md = sum(rd) / n
    mr = sum(rr) / n
    cov = sum((rd[i] - md) * (rr[i] - mr) for i in range(n))
    sdd = math.sqrt(sum((x - md) ** 2 for x in rd))
    sdr = math.sqrt(sum((x - mr) ** 2 for x in rr))
    ic = cov / (sdd * sdr) if sdd > 0 and sdr > 0 else 0.0
    return ic, n


def format_pct(x):
    return f"{x:+.2f}%"


def print_cmp_row(group_a_name, group_b_name, stat_a, stat_b, p_ab):
    print(f"   {group_a_name:>14} n={stat_a['n']:>3} 均值{stat_a['mean_pct']:+7.2f}% t={stat_a['t']:+5.2f}")
    print(f"   {group_b_name:>14} n={stat_b['n']:>3} 均值{stat_b['mean_pct']:+7.2f}% t={stat_b['t']:+5.2f}")
    p_str = f"MW-p={p_ab:.4f}" if p_ab is not None else "MW-p=  n/a"
    diff = (stat_b["mean_pct"] if stat_b else 0) - (stat_a["mean_pct"] if stat_a else 0)
    print(f"       差({group_b_name}-{group_a_name}) {diff:+.2f}pp  [{p_str}]")