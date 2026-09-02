# -*- coding: utf-8 -*-
"""盘中快照归档的可复现回放回测。

回测的时序约束：信号日 d 只把 d-1 及以前的完整日线，加上 d 日归档快照
送进因子；收益按 d 日收盘成交到 d+1 日完整收盘计算。这样不会把 d 日收盘
结果偷偷灌进 d 日 14:45 的信号，也不会把“日线收盘代理回测”冒充真实盘中回测。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

from src.strategy import chinext_factors as cf
from src.strategy import chinext_timing as ct
from src.strategy.intraday_snapshot import validate_snapshot

DEFAULT_WEIGHTS = dict(cf.CHINEXT_V51_WEIGHTS)


def _day_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def normalize_snapshots(snapshots: Sequence[Mapping[str, Any]]) -> list[dict]:
    """校验并按日期排序快照，重复日期或非法记录直接失败。"""
    if not isinstance(snapshots, Sequence) or isinstance(snapshots, (str, bytes)):
        raise ValueError("快照归档必须是数组")
    out = []
    seen = set()
    errors = []
    for number, item in enumerate(snapshots, 1):
        result = validate_snapshot(item)
        if not result["ok"]:
            errors.append(f"第{number}条:{result['reason']}")
            continue
        day = result["date"]
        if day in seen:
            errors.append(f"重复日期:{day}")
            continue
        seen.add(day)
        record = dict(item)
        record["date"] = day
        for key in ("close", "amount", "high", "low"):
            record[key] = float(record[key])
        out.append(record)
    if errors:
        raise ValueError("快照归档无效：" + "; ".join(errors))
    if len(out) < 2:
        raise ValueError("快照归档至少需要2条有效记录")
    return sorted(out, key=lambda item: item["date"])


def _prepare_daily_bars(daily_bars: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(daily_bars, pd.DataFrame) or daily_bars.empty:
        raise ValueError("完整日线不能为空")
    missing = [key for key in ("close", "amount") if key not in daily_bars]
    if missing:
        raise ValueError("完整日线缺少字段：" + ",".join(missing))
    frame = daily_bars.copy()
    frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
    frame = frame.loc[~frame.index.isna()]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    if frame["close"].isna().any() or (frame["close"] <= 0).any():
        raise ValueError("完整日线含非法收盘价")
    if frame["amount"].isna().any() or (frame["amount"] < 0).any():
        raise ValueError("完整日线含非法成交额")
    return frame


def _date_range_missing(frame_dates: list[str], start: str, end: str,
                        available: set[str], warmup: int) -> list[str]:
    required = [day for i, day in enumerate(frame_dates)
                if i >= warmup and day >= start and day < end]
    return [day for day in required if day not in available]


def _max_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    result = 0.0
    for value in values:
        peak = max(peak, value)
        result = min(result, value / peak - 1.0)
    return result


def replay_snapshot_backtest(
        daily_bars: pd.DataFrame,
        snapshots: Sequence[Mapping[str, Any]],
        fee: float = 0.0,
        weights: Optional[dict] = None,
        tiers: tuple = ct.TIERS,
        eval_start: Optional[str] = None,
        eval_end: Optional[str] = None,
        allow_gaps: bool = False) -> dict:
    """用归档快照回放策略，返回指标和逐日审计事件。

    ``eval_end`` 为半开区间上界；缺省时取最后一条快照日期，因此最后一条
    快照只作为前一条信号之后的归档边界，不会被错误地当成可评估信号。
    默认禁止交易日缺快照；研究稀疏样本时必须显式传 ``allow_gaps=True``。
    """
    if not math.isfinite(float(fee)) or fee < 0 or fee >= 1:
        raise ValueError("fee 必须在 [0,1) 内")
    frame = _prepare_daily_bars(daily_bars)
    records = normalize_snapshots(snapshots)
    frame_dates = [day.strftime("%Y-%m-%d") for day in frame.index]
    frame_pos = {day: i for i, day in enumerate(frame_dates)}
    snapshot_map = {item["date"]: item for item in records}
    unknown = sorted(set(snapshot_map) - set(frame_pos))
    if unknown:
        raise ValueError("快照日期不在完整日线中：" + ",".join(unknown[:5]))

    warmup = 60
    first = max(warmup, min(frame_pos[item["date"]] for item in records))
    start = _day_text(eval_start) if eval_start is not None else frame_dates[first]
    end = _day_text(eval_end) if eval_end is not None \
        else records[-1]["date"]
    if not start or not end or start >= end:
        raise ValueError("回放评估窗口无效")

    missing = _date_range_missing(frame_dates, start, end,
                                  set(snapshot_map), warmup)
    if missing and not allow_gaps:
        preview = ",".join(missing[:8])
        suffix = "..." if len(missing) > 8 else ""
        raise ValueError(f"快照缺失{len(missing)}个交易日：{preview}{suffix}；"
                         "如需稀疏研究必须显式 --allow-gaps")

    selected = [item for item in records
                if start <= item["date"] < end
                and frame_pos[item["date"]] >= warmup]
    if not selected:
        raise ValueError("评估窗口没有可用快照")
    record_pos = {item["date"]: i for i, item in enumerate(records)}

    closes = frame["close"].tolist()
    amounts = frame["amount"].tolist()
    weights = dict(weights or DEFAULT_WEIGHTS)
    prev = {"position": 0.0, "pending": None}
    nav = bh = 1.0
    navs, bh_navs, daily_rets, events = [], [], [], []
    switches = 0
    pos_sum = 0.0
    for item in selected:
        day = item["date"]
        i = frame_pos[day]
        # 只替换当前信号日，较早日期使用当时已经知道的完整日线；未来行
        # 从未切入序列，防止把下一日收盘或成交额泄露给当前因子。
        close_history = closes[:i] + [item["close"]]
        amount_history = amounts[:i] + [item["amount"]]
        signals = cf.core_signals(close_history, amount_history,
                                  erp_pctile=None)
        comp = cf.dimension_score(signals, weights)
        score = float(comp[-1])
        caps = cf.defensive_state(
            close_history, None,
            {"risk_off": False, "basis_min_ap": None,
             "intraday_pct": 0.0},
        )
        dec = ct.decide_position(score, caps["cap"], prev, tiers=tiers)
        fee_cost = fee * abs(float(dec["position"]) -
                             float(prev.get("position") or 0.0)) \
            if dec.get("changed") else 0.0
        if dec.get("changed"):
            switches += 1
        if i + 1 >= len(closes):
            raise ValueError(f"评估信号 {day} 缺少下一交易日完整收盘")
        next_day = frame_dates[i + 1]
        next_index = record_pos[day] + 1
        next_item = records[next_index] if next_index < len(records) else None
        execution_close = closes[i]
        next_close = closes[i + 1]
        ret = next_close / execution_close - 1.0
        nav *= (1.0 - fee_cost) * (1.0 + dec["position"] * ret)
        bh *= 1.0 + ret
        daily_rets.append((1.0 - fee_cost) *
                          (1.0 + dec["position"] * ret) - 1.0)
        navs.append(nav)
        bh_navs.append(bh)
        pos_sum += float(dec["position"])
        events.append({
            "date": day,
            "next_date": next_day,
            "snapshot_close": item["close"],
            "execution_close": execution_close,
            "next_close": next_close,
            "next_snapshot_date": next_item["date"] if next_item else None,
            "next_snapshot_close": next_item["close"] if next_item else None,
            "snapshot_amount": item["amount"],
            "source": item.get("source"),
            "score": round(score, 6),
            "cap": caps["cap"],
            "position": dec["position"],
            "changed": bool(dec.get("changed")),
            "return": ret,
            "snapshot_to_next_snapshot_return": (
                next_item["close"] / item["close"] - 1.0
                if next_item else None
            ),
        })
        prev = {"position": dec["position"], "pending": dec.get("pending")}

    years = len(navs) / 244.0
    cagr = nav ** (1.0 / years) - 1.0 if years > 0 else 0.0
    mu = sum(daily_rets) / len(daily_rets) if daily_rets else 0.0
    sd = (sum((value - mu) ** 2 for value in daily_rets) /
          max(1, len(daily_rets) - 1)) ** 0.5
    sharpe = mu / sd * (244 ** 0.5) if sd > 0 else 0.0
    mdd = _max_drawdown([1.0] + navs)
    bh_mdd = _max_drawdown([1.0] + bh_navs)
    return {
        "start": selected[0]["date"],
        "end": selected[-1]["date"],
        "total": nav - 1.0,
        "cagr": cagr,
        "sharpe": sharpe,
        "mdd": mdd,
        "bh": bh - 1.0,
        "bh_mdd": bh_mdd,
        "switches": switches,
        "avg_pos": pos_sum / len(navs),
        "n_events": len(events),
        "final_state": prev,
        "navs": navs,
        "bh_navs": bh_navs,
        "daily_rets": daily_rets,
        "events": events,
        "return_basis": "execution_close_to_next_close",
        "allow_gaps": bool(allow_gaps),
    }
