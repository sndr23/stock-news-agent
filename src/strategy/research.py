# -*- coding: utf-8 -*-
"""创业板 V6 研究工具。

本模块只服务研究脚本和单元测试，不被生产信号入口导入，也不改变 v5.1
的权重、档位线、状态机或硬风控。所有收益函数都要求调用方先构造同一
信息集下的决策序列，避免把研究指标和生产决策混在一起。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from src.strategy import chinext_factors as cf
from src.strategy import chinext_timing as ct

TRADING_DAYS = 244

# 与当前档位线对齐的连续映射：在相邻档位附近平滑过渡，区间外封顶/封底。
DEFAULT_POSITION_BANDS = (
    (-0.50, 0.00),
    (-0.30, 0.30),
    (-0.10, 0.60),
    (0.20, 0.90),
    (0.40, 1.00),
)


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _validate_bands(bands: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    if isinstance(bands, (str, bytes)) or len(bands) < 2:
        raise ValueError("position bands 至少需要两个节点")
    result = []
    previous_score = None
    previous_position = None
    for item in bands:
        if len(item) != 2:
            raise ValueError("position bands 节点必须是(score, position)")
        score, position = _finite(item[0]), _finite(item[1])
        if score is None or position is None:
            raise ValueError("position bands 必须是有限数字")
        if not 0.0 <= position <= 1.0:
            raise ValueError("position bands 仓位必须在 [0,1]")
        if previous_score is not None and score <= previous_score:
            raise ValueError("position bands 分数节点必须严格递增")
        if previous_position is not None and position < previous_position:
            raise ValueError("position bands 仓位节点必须单调不降")
        result.append((score, position))
        previous_score, previous_position = score, position
    return tuple(result)


def score_to_continuous_position(
        score: float,
        bands: Sequence[Sequence[float]] = DEFAULT_POSITION_BANDS) -> float:
    """把综合分线性映射为连续仓位，仅用于 V6 研究对照。"""
    value = _finite(score)
    if value is None:
        raise ValueError("score 必须是有限数字")
    points = _validate_bands(bands)
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for (left_score, left_pos), (right_score, right_pos) in zip(points, points[1:]):
        if value <= right_score:
            ratio = (value - left_score) / (right_score - left_score)
            return left_pos + ratio * (right_pos - left_pos)
    return points[-1][1]


def _validated_returns(name: str, values: Sequence[float]) -> list[float]:
    result = []
    for value in values:
        number = _finite(value)
        if number is None or number <= -1.0:
            raise ValueError(f"{name} 含非法收益")
        result.append(number)
    return result


def _curve(returns: Sequence[float]) -> tuple[float, list[float]]:
    nav = 1.0
    navs = []
    for value in returns:
        nav *= 1.0 + value
        navs.append(nav)
    return nav, navs


def _max_drawdown(navs: Sequence[float]) -> float:
    peak = 1.0
    result = 0.0
    for nav in navs:
        peak = max(peak, nav)
        result = min(result, nav / peak - 1.0)
    return result


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((value - mean) ** 2 for value in values) /
            (len(values) - 1)) ** 0.5


def _capture(strategy: Sequence[float], benchmark: Sequence[float], positive: bool):
    selected = [i for i, value in enumerate(benchmark)
                if (value > 0 if positive else value < 0)]
    if not selected:
        return None
    denominator = sum(benchmark[i] for i in selected)
    return (sum(strategy[i] for i in selected) / denominator
            if denominator else None)


def performance_metrics(
        strategy_returns: Sequence[float],
        benchmark_returns: Sequence[float],
        positions: Optional[Sequence[float]] = None,
        initial_position: float = 0.0,
        annualization: int = TRADING_DAYS) -> dict:
    """从同日对齐的策略/基准收益计算 V6 研究指标。

    `bull_capture` / `bear_capture` 分别是基准上涨日/下跌日的策略收益和
    基准收益之比；熊市捕获率同样为正数，越低通常表示防守越强。`ulcer`
    是净值相对历史峰值回撤百分比的均方根。
    """
    strategy = _validated_returns("strategy_returns", strategy_returns)
    benchmark = _validated_returns("benchmark_returns", benchmark_returns)
    if len(strategy) != len(benchmark):
        raise ValueError("策略收益和基准收益长度不一致")
    if annualization <= 0:
        raise ValueError("annualization 必须为正")

    if positions is not None:
        if len(positions) != len(strategy):
            raise ValueError("positions 与收益长度不一致")
        pos = []
        for value in positions:
            number = _finite(value)
            if number is None or not 0.0 <= number <= 1.0:
                raise ValueError("positions 必须在 [0,1]")
            pos.append(number)
        initial = _finite(initial_position)
        if initial is None or not 0.0 <= initial <= 1.0:
            raise ValueError("initial_position 必须在 [0,1]")
    else:
        pos = None
        initial = 0.0

    n = len(strategy)
    nav, navs = _curve(strategy)
    years = n / annualization if n else 0.0
    cagr = nav ** (1.0 / years) - 1.0 if years else 0.0
    mean = sum(strategy) / n if n else 0.0
    sd = _sample_std(strategy)
    sharpe = mean / sd * annualization ** 0.5 if sd else 0.0
    downside = (sum(min(value, 0.0) ** 2 for value in strategy) / n) ** 0.5 \
        if n else 0.0
    sortino = mean / downside * annualization ** 0.5 if downside else 0.0
    mdd = _max_drawdown(navs)
    calmar = cagr / abs(mdd) if mdd else 0.0
    drawdowns = []
    peak = 1.0
    for value in navs:
        peak = max(peak, value)
        drawdowns.append(value / peak - 1.0)
    ulcer = (sum(value * value for value in drawdowns) / n) ** 0.5 if n else 0.0
    positive_count = sum(value > 0 for value in strategy)
    loss = sum(value for value in strategy if value < 0)
    gain = sum(value for value in strategy if value > 0)

    result = {
        "n": n,
        "total": nav - 1.0,
        "cagr": cagr,
        "sharpe": sharpe,
        "sortino": sortino,
        "mdd": mdd,
        "calmar": calmar,
        "ulcer": ulcer,
        "hit_rate": positive_count / n if n else 0.0,
        "profit_factor": gain / abs(loss) if loss else None,
        "bull_capture": _capture(strategy, benchmark, True),
        "bear_capture": _capture(strategy, benchmark, False),
    }
    if pos is None:
        result.update({"avg_pos": None, "turnover": None, "switches": None})
    else:
        changes = [abs(pos[0] - initial)] if pos else []
        changes.extend(abs(pos[i] - pos[i - 1]) for i in range(1, len(pos)))
        result.update({
            "avg_pos": sum(pos) / n if n else 0.0,
            "turnover": sum(changes),
            "switches": sum(change > 1e-12 for change in changes),
        })
    return result


def evaluate_position_path(
        closes: Sequence[float],
        positions: Sequence[float],
        fee: float = 0.0,
        start: int = 0,
        end: Optional[int] = None,
        initial_position: float = 0.0) -> dict:
    """按收盘成交→次日收益评估一条仓位路径（研究用）。"""
    prices = []
    for value in closes:
        number = _finite(value)
        if number is None or number <= 0:
            raise ValueError("closes 含非法价格")
        prices.append(number)
    if len(prices) < 2:
        raise ValueError("closes 至少需要两根")
    if not math.isfinite(float(fee)) or fee < 0.0 or fee >= 1.0:
        raise ValueError("fee 必须在 [0,1) 内")
    path = list(positions)
    if len(path) > len(prices) - 1:
        raise ValueError("positions 最多对应每根可成交收盘")
    end = len(path) if end is None else int(end)
    if start < 0 or end <= start or end > len(path) or end >= len(prices):
        raise ValueError("invalid position evaluation window")
    prev = _finite(initial_position)
    if prev is None or not 0.0 <= prev <= 1.0:
        raise ValueError("initial_position 必须在 [0,1]")
    strategy, benchmark, selected = [], [], []
    for i in range(start, end):
        target = _finite(path[i])
        if target is None or not 0.0 <= target <= 1.0:
            raise ValueError("positions 必须在 [0,1]")
        ret = prices[i + 1] / prices[i] - 1.0
        cost = float(fee) * abs(target - prev)
        strategy.append((1.0 - cost) * (1.0 + target * ret) - 1.0)
        benchmark.append(ret)
        selected.append(target)
        prev = target
    result = performance_metrics(strategy, benchmark, selected,
                                 initial_position=initial_position)
    result["fee"] = float(fee)
    result["return_basis"] = "close_to_next_close"
    return result


def _ic_stats(pairs: Sequence[tuple[float, float]]) -> dict:
    if not pairs:
        return {"ic": 0.0, "n": 0}
    ic = ct.spearman_ic([pair[0] for pair in pairs],
                        [pair[1] for pair in pairs])
    return {"ic": round(float(ic), 6), "n": len(pairs)}


def _factor_pairs(values: Sequence[Any], closes: Sequence[Any], start: int,
                  horizon: int, end: Optional[int] = None) -> list[tuple[float, float]]:
    limit = min(len(values), len(closes))
    upper = min(limit - horizon, len(closes) - horizon)
    if end is not None:
        upper = min(upper, end)
    pairs = []
    for i in range(max(0, start), upper):
        factor = _finite(values[i])
        base = _finite(closes[i])
        future = _finite(closes[i + horizon])
        if factor is None or base is None or future is None or base <= 0:
            continue
        pairs.append((factor, future / base - 1.0))
    return pairs


def factor_ic_report(
        signals: Mapping[str, Sequence[Any]],
        closes: Sequence[Any],
        weights: Optional[Mapping[str, float]] = None,
        start: int = 60,
        tail_days: int = 3 * TRADING_DAYS,
        horizons: Sequence[int] = (1, 5),
        min_samples: int = 10,
        gate_ic: float = 0.05) -> dict:
    """输出核心因子单因子 IC、尾部 OOS 稳定性和增量分值 IC。

    增量 IC 定义为：完整核心分数减去将该因子置零后的核心分数，再与同一
    前瞻收益做 Spearman 相关。它不是因果证明，而是判断该因子在当前权重
    下是否提供额外排序信息的研究诊断。
    """
    if not signals:
        raise ValueError("signals 不能为空")
    n = len(closes)
    if n == 0 or any(len(values) != n for values in signals.values()):
        raise ValueError("signals 与 closes 长度不一致")
    if start < 0 or tail_days <= 0 or min_samples <= 0 or gate_ic < 0:
        raise ValueError("factor IC 参数无效")
    horizon_values = tuple(int(horizon) for horizon in horizons)
    if not horizon_values or any(horizon <= 0 for horizon in horizon_values):
        raise ValueError("horizons 必须为正整数")

    weight_map = dict(cf.CHINEXT_V51_WEIGHTS if weights is None else weights)
    try:
        full_scores = cf.dimension_score(dict(signals), weight_map)
        reduced_scores = {}
        for name in signals:
            reduced = {key: list(values) for key, values in signals.items()}
            reduced[name] = [0.0] * n
            reduced_scores[name] = cf.dimension_score(reduced, weight_map)
    except KeyError as exc:
        raise ValueError("signals 必须是 chinext_factors.core_signals 输出") from exc

    tail_start = max(start, n - int(tail_days))
    factors = {}
    for name, values in signals.items():
        by_horizon = {}
        for horizon in horizon_values:
            full_pairs = _factor_pairs(values, closes, start, horizon)
            tail_pairs = _factor_pairs(values, closes, tail_start, horizon)
            incremental = [full_scores[i] - reduced_scores[name][i]
                           for i in range(n)]
            inc_full_pairs = _factor_pairs(incremental, closes, start, horizon)
            inc_tail_pairs = _factor_pairs(incremental, closes, tail_start, horizon)
            full = _ic_stats(full_pairs)
            tail = _ic_stats(tail_pairs)
            inc_full = _ic_stats(inc_full_pairs)
            inc_tail = _ic_stats(inc_tail_pairs)
            stats = (full, tail, inc_full, inc_tail)
            enough = all(item["n"] >= min_samples for item in stats)
            ic_ok = all(abs(item["ic"]) >= gate_ic for item in stats)
            signs = [item["ic"] >= 0 for item in stats]
            stable = len(set(signs)) == 1
            eligible = bool(enough and ic_ok and stable)
            if not enough:
                reason = "样本不足"
            elif not ic_ok:
                reason = "IC未达标"
            elif not stable:
                reason = "OOS符号不稳定"
            else:
                reason = "通过"
            horizon_key = "next_ret" if horizon == 1 else f"r{horizon}"
            by_horizon[horizon_key] = {
                "full": full,
                "tail": tail,
                "incremental_full": inc_full,
                "incremental_tail": inc_tail,
                "eligible": eligible,
                "reason": reason,
            }
        factors[name] = by_horizon
    return {
        "n": n,
        "start": start,
        "tail_start": tail_start,
        "tail_days": tail_days,
        "gate_ic": gate_ic,
        "min_samples": min_samples,
        "factors": factors,
    }
