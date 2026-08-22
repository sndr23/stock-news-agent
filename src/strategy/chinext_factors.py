# -*- coding: utf-8 -*-
"""
创业板择时 · 核心层纯因子计算（chinext_factors.py）
====================================================
v4 方案核心层：10 个可回测因子，全部只依赖 399006 日线（close + amount），
纯函数、无网络、无状态，可被回测与实时打分共用同一实现（杜绝两套口径）。

与打分层解耦：本模块只产出"因子值（原始刻度 + 方向归一化分）"，不决定仓位。
方向归一化分统一映射到 [-1, 1]：越大越看多，越小越看空，0=中性。

因子清单（v4 定稿）：
  趋势  ×2 : trend_ma20_60（双均线状态）, trend_momentum_60（60日时序动量）
  量价  ×2 : volprice_quadrant（量能分位四象限）, volprice_amihud（非流动性）
  波动  ×2 : vol_regime（20日年化vol的1年分位）, vol_term（5日/20日波动期限结构）
  估值  ×1 : value_erp（股债性价比 ERP 分位，数据源可注入，缺则置 0）
  落袋  ×2 : drawdown_pullback（52周高点接近度/落袋）, drawdown_dd60（60日回撤）
  硬风控     : defensive_state（回撤/波动分位/贴水/盘中急跌 → 仓位封顶）

时空口径：t 日因子值使用 ≤t 收盘（含 t 日当日收盘/盘中快照）：
  回测 = d 日收盘价出信号、吃 d+1 收益（收盘后决策，信息无未来泄漏）；
  实盘 = 14:45 盘中快照替代 d 日收盘（最后一根 bar 为当日实时价）。
绝不使用 d+1 及以后的数据，杜绝 look-ahead。所有分位用"滚动 1 年（252 交易日）"窗口。
"""
from __future__ import annotations

from typing import Optional, Sequence

# ---------------- 基础工具 ----------------


def _roll_z(x: Sequence[float], span: int = 252) -> list:
    """滚动 z-score：当前值相对过去 span 窗口的均值和标准差。"""
    out = [0.0] * len(x)
    for i in range(1, len(x)):
        lo = max(0, i - span)
        w = x[lo:i]
        mu = sum(w) / len(w)
        sd = (sum((v - mu) ** 2 for v in w) / (len(w) - 1)) ** 0.5 if len(w) > 1 else 0.0
        out[i] = (x[i] - mu) / sd if sd > 0 else 0.0
    return out


def _roll_pctile(x: Sequence[float], span: int = 252) -> list:
    """当前值在过去 span 窗口内的百分位（0~1，升序典）。"""
    out = [0.5] * len(x)
    for i in range(1, len(x)):
        lo = max(0, i - span)
        w = sorted(x[lo:i])
        if not w:
            continue
        if x[i] >= w[-1]:
            out[i] = 1.0
        elif x[i] <= w[0]:
            out[i] = 0.0
        else:
            # 秩分位：严格小于当前值的比例
            out[i] = sum(1 for v in w if v < x[i]) / len(w)
    return out


def _ema(x: Sequence[float], n: int) -> list:
    out = [0.0] * len(x)
    if not x:
        return out
    alpha = 2.0 / (n + 1)
    prev = x[0]
    out[0] = prev
    for i in range(1, len(x)):
        prev = alpha * x[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


# ---------------- 因子计算（输入为收盘序列 close 与金额序列 amount） ----------------


def factor_trend_ma20_60(close: Sequence[float], upscale: float = 0.6) -> list:
    """双均线：收盘vs MA20 + MA20斜率 + 收盘vs MA60 → [-1,1]。0.5/0.3/0.2 结构。"""
    out = [0.0] * len(close)
    for i in range(60, len(close)):
        c = close[: i + 1]
        ma20 = sum(c[-20:]) / 20
        ma20_5ago = sum(c[-25:-5]) / 20
        ma60 = sum(c[-60:]) / 60
        last = c[-1]
        s = (0.5 if last > ma20 else -0.5) + (0.3 if ma20 > ma20_5ago else -0.3) + (
            0.2 if last > ma60 else -0.2)
        out[i] = round(max(-1.0, min(1.0, s)), 3)
    return out


def factor_momentum_60(close: Sequence[float]) -> list:
    """60日时序动量：累涨 20% 记 +1、跌 20% 记 -1，线性 clip → [-1,1]。"""
    out = [0.0] * len(close)
    for i in range(60, len(close)):
        m = close[i] / close[i - 60] - 1.0
        out[i] = round(max(-1.0, min(1.0, m / 0.20)), 3)
    return out


def factor_volprice_quadrant(close: Sequence[float], amount: Sequence[float]) -> list:
    """量价四象限（昨bar 量能分位 × 涨跌方向）。放量涨 +0.8、放量跌 -0.8；
    缩量涨 +0.2、缩量跌 -0.3；中量 ±0.3。→ [-1,1]。"""
    out = [0.0] * len(close)
    pct = _roll_pctile(list(amount), 60)
    for i in range(1, len(close)):
        ret1 = close[i] / close[i - 1] - 1.0 if close[i - 1] else 0.0
        p = pct[i]
        if p >= 0.8:
            base = 0.8 if ret1 > 0 else -0.8
        elif p <= 0.3:
            base = 0.2 if ret1 > 0 else -0.3
        else:
            base = 0.3 if ret1 > 0 else -0.3
        out[i] = round(max(-1.0, min(1.0, base)), 3)
    return out


def factor_amihud(close: Sequence[float], amount: Sequence[float]) -> list:
    """Amihud 非流动性（分钟级冲击成本代理）：|ret|/成交额的 20日均值，取负向 z。
    流动性恶化（值突升）→ 看空。用 -1×滚动 z 映射到 [-1,1]。"""
    illiq = [0.0] * len(close)
    for i in range(1, len(close)):
        ret = abs(close[i] / close[i - 1] - 1.0) if close[i - 1] else 0.0
        amt = amount[i] or 1.0
        illiq[i] = ret / (amt / 1e9)  # 单位收益 per 10亿元（量级无本质影响）
    z = _roll_z(illiq, 60)
    out = []
    for v in z:
        # z 越高流动性越差 → 越看空；clip 到 ±2 再折到 [-1,1]
        out.append(round(max(-1.0, min(1.0, -v / 2.0)), 3))
    return out


def factor_vol_regime(close: Sequence[float]) -> list:
    """已实现波动率：20日年化 vol 的 1 年(252)分位 → 高风险记负分。"""
    ret = [0.0] * len(close)
    for i in range(1, len(close)):
        ret[i] = close[i] / close[i - 1] - 1.0 if close[i - 1] else 0.0
    vol = [0.0] * len(close)
    for i in range(20, len(close)):
        r = ret[i - 19 : i + 1]
        mu = sum(r) / 20
        sd = (sum((v - mu) ** 2 for v in r) / 19) ** 0.5
        vol[i] = sd * (252 ** 0.5) * 100
    pct = _roll_pctile(vol, 252)
    out = []
    for p in pct:
        # 波动 90 分位以上压制，below 60 分位略利好（低波环境）
        if p >= 0.90:
            s = -(p - 0.90) / 0.10
        elif p <= 0.60:
            s = 0.0  # 低波中性，不主动加仓
        else:
            s = -(p - 0.60) / 0.30  # 60~90 分位温和减分
        out.append(round(max(-1.0, min(1.0, s)), 3))
    return out


def factor_vol_term(close: Sequence[float]) -> list:
    """波动期限结构：5日vol / 20日vol。倒挂（短伏高）→ 结构性风险压减分。"""
    ret = [0.0] * len(close)
    for i in range(1, len(close)):
        ret[i] = close[i] / close[i - 1] - 1.0 if close[i - 1] else 0.0
    vol5, vol20 = [0.0] * len(close), [0.0] * len(close)
    for i in range(19, len(close)):
        vol20[i] = (sum((v - sum(ret[i - 19 : i + 1]) / 20) ** 2 for v in ret[i - 19 : i + 1]) / 19) ** 0.5
    for i in range(4, len(close)):
        vol5[i] = (sum((v - sum(ret[i - 4 : i + 1]) / 5) ** 2 for v in ret[i - 4 : i + 1]) / 4) ** 0.5
    out = []
    for i in range(len(close)):
        if vol20[i] <= 1e-9:
            out.append(0.0)
            continue
        ratio = vol5[i] / vol20[i]
        # ratio>1.3 短伏倒挂明确压减；1.0~1.3 温和减分；<0.8 波动收敛利好
        if ratio > 1.3:
            s = -(ratio - 1.3) / 0.5
        elif ratio < 0.8:
            s = 0.2
        else:
            s = -(ratio - 1.0) / 0.6
        out.append(round(max(-1.0, min(1.0, s)), 3))
    return out


def factor_value_erp(close: Sequence[float], ep_series: Optional[Sequence[float]] = None,
                    yield10y: Optional[Sequence[float]] = None,
                    _internal_pctile: Optional[Sequence[float]] = None) -> list:
    """估值：股债性价比 ERP = 盈利收益率 - 10Y 国债率。缺数据源时置 0（防错）。
    正常输入 _internal_pctile（ERP 分位序列，由外部估值源注入）时直接负向化映射。"""
    if not _internal_pctile:
        return [0.0] * len(close)
    out = []
    for p in _internal_pctile:
        # ERP 分位越高（相对股便宜）→ 看多；分位越低 → 看空。
        s = 2 * (p - 0.5)  # [-1,1]，0.5 中性
        out.append(round(max(-1.0, min(1.0, s)), 3))
    return out


def _pad(arr: Sequence[float], n: int) -> list:
    """把不足 n 的序列左补 0 到长度 n（统一 warmup 空窗为零）。"""
    if len(arr) >= n:
        return list(arr)
    return [0.0] * (n - len(arr)) + list(arr)


def factor_pullback_52w(close: Sequence[float]) -> list:
    """52周高点接近度（落袋）：现价相对 252 日高点回撤。回撤深→防守，破新高→利好。"""
    out = [0.0] * len(close)
    for i in range(252, len(close)):
        hi = max(close[i - 251 : i + 1])
        dd = close[i] / hi - 1.0
        # dd<0 回撤越大越防守；刚破新高(dd≈0) 略利好
        if dd >= -0.02:
            s = 0.3
        elif dd <= -0.25:
            s = -1.0
        else:
            # 线性：dd=-2%得+0.3，dd=-25%得-1.0（回撤越深越防守）
            s = 0.3 + 1.3 * (dd + 0.02) / 0.23
        out[i] = round(max(-1.0, min(1.0, s)), 3)
    return out


def factor_dd60(close: Sequence[float]) -> list:
    """60日回撤：现价相对 60 日高点。回撤>12%深度防守、>8%温和削弱。"""
    out = [0.0] * len(close)
    for i in range(60, len(close)):
        hi = max(close[i - 59 : i + 1])
        dd = close[i] / hi - 1.0
        if dd <= -0.12:
            s = -1.0
        elif dd <= -0.08:
            s = -0.5
        elif dd <= -0.04:
            s = -0.2
        else:
            s = 0.0
        out[i] = round(max(-1.0, min(1.0, s)), 3)
    return out


# ---------------- 硬风控状态 ----------------

def defensive_state(close: Sequence[float], vol_pctile: Optional[float] = None,
                    glass: dict = None) -> dict:
    """硬风控：返回 {cap: 0~1, triggers: [...]}。只降不升，覆盖一切。
    glass: 外部注入的 {risk_off: bool, basis_min_ap: float, intraday_pct: float}。"""
    cap = 1.0
    trig = []
    n = len(close)
    if n >= 60:
        dd = close[-1] / max(close[-60:]) - 1.0
        # 两级渐进降档：-8%先到6成，-12%再到3成（平滑降仓减摩擦）
        if dd <= -0.12:
            cap = min(cap, 0.3)
            trig.append(f"距60日高点回撤{dd * 100:.1f}%封顶3成")
        elif dd <= -0.08:
            cap = min(cap, 0.6)
            trig.append(f"距60日高点回撤{dd * 100:.1f}%封顶6成")
    if vol_pctile is not None and vol_pctile >= 95:
        cap = min(cap, 0.6)
        trig.append(f"创业板波动率{vol_pctile:.0f}分位封顶6成")
    if glass:
        if glass.get("risk_off"):
            cap = min(cap, 0.3)
            trig.append("宏观风险状态risk_off封顶3成")
        ap = glass.get("basis_min_ap")
        if ap is not None and ap <= -15:
            cap = min(cap, 0.6)
            trig.append(f"深贴水{ap:.1f}%封顶6成")
        ip = glass.get("intraday_pct")
        if ip is not None and ip <= -2.5:
            cap = min(cap, 0.3)
            trig.append(f"盘中{ip:.1f}%急跌封顶3成")
        ovs_drop = glass.get("overseas_drop")
        # 外盘 t-1 急跌 → 盘中急跌的同源确认（不独立触发，避免近期负相关结束后的反向）
        if ovs_drop is not None and ovs_drop <= -0.03:
            if ip is not None and ip <= -1.5:
                cap = min(cap, 0.3)
                trig.append(f"外围{ovs_drop:.1%}+盘中{ip:.1f}%急跌同源确认封顶3成")
            elif ip is not None and ip <= -1.0:
                cap = min(cap, 0.6)
                trig.append(f"外围{ovs_drop:.1%}下杀+盘中承压封顶6成")
    return {"cap": cap, "triggers": trig}


def core_signals(close: Sequence[float], amount: Sequence[float],
                 erp_pctile: Optional[Sequence[float]] = None,
                 vol_pctile_override: Optional[float] = None) -> dict:
    """计算 v4 全部 10 核心因子并列名。返回 {factor_name: [score...]}。
    因子维度权重（v4 定稿）：趋势 0.35 量价 0.20 波动 0.20 估值 0.10 落袋 0.15。"""
    return {
        "trend_ma20_60": factor_trend_ma20_60(close),
        "trend_momentum_60": factor_momentum_60(close),
        "volprice_quadrant": factor_volprice_quadrant(close, amount),
        "volprice_amihud": factor_amihud(close, amount),
        "vol_regime": factor_vol_regime(close),
        "vol_term": factor_vol_term(close),
        "value_erp": factor_value_erp(close, _internal_pctile=erp_pctile),
        "pullback_52w": factor_pullback_52w(close),
        "dd60": factor_dd60(close),
    }


def dimension_score(signals: dict, weights: dict = None) -> list:
    """按 v4 维度权重合成核心综合分。signals: {name: [score...]}。返回 [score...]。
    维度权重：趋势0.35 量价0.20 波动0.20 估值0.10 落袋0.15。"""
    w = weights or {
        "趋势": 0.35, "量价": 0.20, "波动": 0.20, "估值": 0.10, "落袋": 0.15,
    }
    groups = {
        "趋势": ["trend_ma20_60", "trend_momentum_60"],
        "量价": ["volprice_quadrant", "volprice_amihud"],
        "波动": ["vol_regime", "vol_term"],
        "估值": ["value_erp"],
        "落袋": ["pullback_52w", "dd60"],
    }
    n = len(next(iter(signals.values())))
    totals = [0.0] * n
    for dim, wdim in w.items():
        names = groups[dim]
        # 维度内等权 → 维度间按 w 加权 → 总权重归一(实际 sum(w)=1.0)
        for i in range(n):
            vals = [signals[k][i] for k in names if k in signals and i < len(signals[k])]
            if not vals:
                continue  # 缺因子整体置 0（如估值无源）
            dim_avg = sum(vals) / len(vals)
            totals[i] += wdim * dim_avg
    return [round(max(-1.0, min(1.0, v)), 3) for v in totals]