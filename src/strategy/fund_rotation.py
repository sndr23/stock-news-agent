# -*- coding: utf-8 -*-
"""
基金轮动信号层（fund_rotation.py）
====================================================
纯逻辑层（无网络），输入基金净值历史/盘中估算，输出操作建议：

信号 = 0.5×近20日 + 0.3×近60日 + 0.2×近5日(含盘中) 的复合动量
趋势门 = 净值在 MA20 之上才允许新买入（持有不受阻）
滞回   = 持有者跌出 Top N+缓冲带才卖；空仓者进 Top N 才买（防日频抖动换手）
防守   = 近20日跌幅超阈值触发防守性减仓建议

设计约束：场外基金申赎费高（<7天赎回1.5%），信号宁钝勿敏，
每天推送但"今日无操作"是正常输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------- 动量与趋势 ----------------

def cum_pct(rets: list, n: int) -> float:
    """近 n 期累计涨幅%（rets 为日增长率序列，升序）。不足 n 期用全部。"""
    if not rets:
        return 0.0
    vals = [v for _, v in rets[-n:]]
    out = 1.0
    for v in vals:
        out *= 1.0 + v / 100.0
    return round((out - 1.0) * 100.0, 2)


def nav_ma(rets: list, n: int = 20) -> float:
    """由日增长率重建净值序列后取 n 日均线（以末值=100 归一）。"""
    if not rets:
        return 0.0
    nav = 100.0
    navs = []
    for _, v in rets:
        nav *= 1.0 + v / 100.0
        navs.append(nav)
    window = navs[-n:] if len(navs) >= n else navs
    return sum(window) / len(window) if window else 0.0


def momentum_score(rets: list, intraday_pct: float = 0.0) -> dict:
    """复合动量分数与趋势门。rets: [(date, 日增长率%), ...] 升序。"""
    m20 = cum_pct(rets, 20)
    m60 = cum_pct(rets, 60)
    m5 = cum_pct(rets, 5) + intraday_pct
    score = round(0.5 * m20 + 0.3 * m60 + 0.2 * m5, 2)
    navs = []
    nav = 100.0
    for _, v in rets:
        nav *= 1.0 + v / 100.0
        navs.append(nav)
    ma20 = nav_ma(rets, 20)
    trend_ok = bool(navs and navs[-1] > ma20) if navs else False
    return {"score": score, "m20": m20, "m60": m60, "m5": round(m5, 2),
            "trend_ok": trend_ok}


# ---------------- 滞回建议状态机 ----------------

@dataclass
class AdviceResult:
    exposure: float                      # 总仓位（仓位层给定）
    target: dict = field(default_factory=dict)   # {code: weight}
    actions: list = field(default_factory=list)  # [{code, action, detail}]
    scores: list = field(default_factory=list)   # 全部基金信号明细（按分数降序）


def build_rotation_advice(fund_signals: list, current_holdings: dict,
                          exposure: float = 1.0,
                          max_positions: int = 3,
                          per_fund_cap: float = 0.40,
                          buffer_rank: int = 1,
                          reduce_pct: float = -10.0) -> AdviceResult:
    """生成轮动操作建议。

    fund_signals: [{code, name, score, trend_ok, m20, ...}]（含盘中信号）
    current_holdings: {code: 当前权重}
    exposure: 仓位层输出的总仓位（0~1，无债 → 空出部分持币）
    滞回：持有者可留在 Top (max_positions+buffer_rank)；新买入仅 Top max_positions 且过趋势门。
    """
    ranked = sorted(fund_signals, key=lambda x: x["score"], reverse=True)
    holding_codes = {c for c, w in current_holdings.items() if w > 0}
    keep_zone = max_positions + buffer_rank
    target_codes = []

    for i, f in enumerate(ranked):
        code = f["code"]
        in_hold = code in holding_codes
        if i < max_positions and (f.get("trend_ok") or in_hold):
            target_codes.append(code)
        elif in_hold and i < keep_zone:
            target_codes.append(code)  # 缓冲带：持有者暂留

    # 防守性减仓：近20日跌幅触发 → 移出目标（即使排名靠前）
    for f in ranked:
        if f["code"] in target_codes and f.get("m20", 0.0) <= reduce_pct:
            target_codes.remove(f["code"])

    # 目标权重：等权分配 × 总仓位，单基金上限 cap，超出部分均摊给未超限者
    n = len(target_codes)
    target = {}
    if n and exposure > 0:
        w = min(1.0 / n, per_fund_cap)
        raw = {c: w for c in target_codes}
        overflow = 1.0 - sum(raw.values())
        if overflow > 1e-9:  # cap 截断后有余量，均摊给未到 cap 的
            for c in target_codes:
                add = min(overflow, per_fund_cap - raw[c])
                if add > 0:
                    raw[c] += add
                    overflow -= add
                if overflow <= 1e-9:
                    break
        scale = exposure if sum(raw.values()) > exposure else 1.0
        target = {c: round(min(v, per_fund_cap) * (scale if scale < 1 else 1), 4)
                  for c, v in raw.items()}
        total = sum(target.values())
        if total > 0:
            target = {c: round(v / total * exposure, 4) for c, v in target.items()}
            # round 残差给末位补齐，保证合计精确等于 exposure
            drift = round(exposure - sum(target.values()), 4)
            if abs(drift) >= 1e-6 and target:
                last = target_codes[-1]
                target[last] = round(target[last] + drift, 4)

    # 动作生成：current vs target 的 diff
    actions = []
    min_move = 0.05  # 权重差 <5% 不动
    for code in sorted(set(target) | set(holding_codes)):
        cur_w = float(current_holdings.get(code) or 0.0)
        tgt_w = float(target.get(code) or 0.0)
        name = next((f["name"] for f in ranked if f["code"] == code), code)
        sig = next((f for f in ranked if f["code"] == code), {})
        if tgt_w <= 1e-9 and cur_w > 0:
            reason = "跌出信号区" if sig else "清仓"
            if sig and sig.get("m20", 0.0) <= reduce_pct:
                reason = f"近20日 {sig['m20']:.1f}% 触发防守"
            actions.append({"code": code, "name": name, "action": "卖出",
                            "detail": f"{reason}，建议全部赎回"})
        elif cur_w <= 1e-9 and tgt_w > 0:
            actions.append({"code": code, "name": name, "action": "买入",
                            "detail": f"信号第{ranked.index(sig)+1}名"
                                      f"（分数 {sig.get('score', 0):+.1f}），"
                                      f"建议买入 {tgt_w:.0%}"})
        elif tgt_w - cur_w > min_move:
            actions.append({"code": code, "name": name, "action": "加仓",
                            "detail": f"{cur_w:.0%} → {tgt_w:.0%}"})
        elif cur_w - tgt_w > min_move:
            actions.append({"code": code, "name": name, "action": "减仓",
                            "detail": f"{cur_w:.0%} → {tgt_w:.0%}"})
        elif cur_w > 0:
            actions.append({"code": code, "name": name, "action": "持有",
                            "detail": f"维持 {cur_w:.0%}"})

    return AdviceResult(exposure=exposure, target=target,
                        actions=actions, scores=ranked)
