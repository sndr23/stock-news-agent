# -*- coding: utf-8 -*-
"""创业板择时第三轮：嵌套 walk-forward 选参与受控维度扩展。

本脚本是独立研究入口，只读复用现有回测实现和二轮脚本的预计算重放
上下文，不修改生产参数、既有脚本或测试。候选的选择只发生在每个外层
训练段内部的 3 个严格按时间排列的内层验证窗；外层测试结果不参与选择。

运行：
    python scripts/nested_selection_eval.py

选择目标（均为不含息口径；含息结果仅并列展示）：
    return  = 内层复合收益
    calmar  = 内层 CAGR / |内层最大回撤|
    penalty = 内层复合收益 / |内层最大回撤|

闲置资金利息按 2%/年、244 个交易日、(1-position) 计入展示列。
"""
from __future__ import annotations

import itertools
import math
import statistics
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import optimize_tiers_return_first as opt  # noqa: E402
from scripts import run_chinext_timing as rct  # noqa: E402
from scripts import walk_forward_validation as wfv  # noqa: E402
from src.strategy import chinext_timing as ct  # noqa: E402
from src.strategy import index_pe as ipe  # noqa: E402


FEE = 0.0
TRAIN_YEARS = 3
TEST_YEARS = 1
EXPECTED_OUTER_FOLDS = 9
INNER_FOLDS = 3
WARMUP = 60
TRADING_DAYS = wfv.TRADING_DAYS
CASH_ANNUAL_RATE = 0.02
CASH_DAILY_RATE = CASH_ANNUAL_RATE / TRADING_DAYS
DEFAULT_HYST_MARGIN = 0.05
DEFAULT_CONFIRM_DAYS = 2

V52_KEY = (0.30, -0.25, -0.30, False)
V51_KEY = (0.40, -0.15, -0.30, True)
EXPECTED_BASELINES = {
    "fixed v5.2": (1.231, -0.323),
    "fixed v5.1": (0.953, -0.236),
    "buy-and-hold": (1.192, -0.570),
}
BASELINE_ROUNDED_TOLERANCE = 0.0005

CRITERIA = ("return", "calmar", "penalty")
CRITERION_LABELS = {
    "return": "inner return max",
    "calmar": "inner Calmar max",
    "penalty": "inner return/|MDD| max",
}

B1_THRESHOLDS = (-0.55, -0.50, -0.45)
B2_HYST_MARGINS = (0.0, 0.03, 0.05, 0.08)
B2_CONFIRM_DAYS = (1, 2, 3)

LOG_PATH = PROJECT_ROOT / "logs" / "nested_selection_eval_20260920.log"


class _Tee:
    """Mirror stdout/stderr to the terminal and the required log file."""

    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
        return len(value)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def isatty(self) -> bool:
        return any(bool(getattr(stream, "isatty", lambda: False)()) for stream in self.streams)


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _pp(value: float) -> str:
    return f"{value * 100:+.2f}pp"


def _calmar(cagr: float, mdd: float) -> float:
    return cagr / abs(mdd) if mdd else 0.0


def _clone_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {"position": 0.0, "pending": None}
    pending = state.get("pending")
    copied_pending = None
    if pending is not None:
        copied_pending = {
            "target": float(pending.get("target", 0.0)),
            "days": int(pending.get("days", 0)),
        }
    return {
        "position": float(state.get("position", 0.0)),
        "pending": copied_pending,
    }


@contextmanager
def _state_parameters(candidate: dict[str, Any]):
    """Temporarily set research-only state-machine parameters."""
    old_hyst = ct.HYST_MARGIN
    old_confirm = ct.UPGRADE_CONFIRM_DAYS
    ct.HYST_MARGIN = float(candidate.get("hyst_margin", DEFAULT_HYST_MARGIN))
    ct.UPGRADE_CONFIRM_DAYS = int(candidate.get("confirm_days", DEFAULT_CONFIRM_DAYS))
    try:
        yield
    finally:
        ct.HYST_MARGIN = old_hyst
        ct.UPGRADE_CONFIRM_DAYS = old_confirm


def _candidate_signature(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        candidate["family"],
        candidate["key"],
        float(candidate.get("hyst_margin", DEFAULT_HYST_MARGIN)),
        int(candidate.get("confirm_days", DEFAULT_CONFIRM_DAYS)),
    )


def _format_candidate(candidate: dict[str, Any]) -> str:
    family = candidate["family"]
    label = candidate.get("label")
    if family == "A":
        body = (
            f"F{float(candidate['full']):.2f}/N{float(candidate['nine']):+.2f}/"
            f"S{float(candidate['six']):+.2f}/ERP"
            f"{'ON' if candidate['erp_cap'] else 'OFF'}"
        )
    elif family == "B1":
        body = f"B1-30%@{float(candidate['thirty']):+.2f}"
    elif family == "B2":
        body = (
            f"B2-H{float(candidate['hyst_margin']):.2f}/"
            f"C{int(candidate['confirm_days'])}"
        )
    else:
        body = str(candidate.get("key"))
    return f"{label}({body})" if label else body


def _candidate_grid_a() -> list[dict[str, Any]]:
    """Reuse the exact 72-candidate second-round grid."""
    candidates: list[dict[str, Any]] = []
    for base in opt.candidate_grid():
        candidate = dict(base)
        candidate.update(
            {
                "family": "A",
                "key": opt.candidate_key(base),
                "hyst_margin": DEFAULT_HYST_MARGIN,
                "confirm_days": DEFAULT_CONFIRM_DAYS,
            }
        )
        candidates.append(candidate)
    if len(candidates) != 72:
        raise RuntimeError(f"A grid size changed: {len(candidates)}")
    return candidates


def _candidate_grid_b1() -> list[dict[str, Any]]:
    base_tiers = ((0.30, 1.0), (-0.25, 0.9), (-0.30, 0.6))
    return [
        {
            "family": "B1",
            "key": ("B1", threshold),
            "thirty": threshold,
            "tiers": base_tiers + ((threshold, 0.3),),
            "erp_cap": False,
            "hyst_margin": DEFAULT_HYST_MARGIN,
            "confirm_days": DEFAULT_CONFIRM_DAYS,
        }
        for threshold in B1_THRESHOLDS
    ]


def _candidate_grid_b2() -> list[dict[str, Any]]:
    tiers = ((0.30, 1.0), (-0.25, 0.9), (-0.30, 0.6))
    return [
        {
            "family": "B2",
            "key": ("B2", hyst, confirm),
            "tiers": tiers,
            "erp_cap": False,
            "hyst_margin": hyst,
            "confirm_days": confirm,
        }
        for hyst, confirm in itertools.product(B2_HYST_MARGINS, B2_CONFIRM_DAYS)
    ]


def _find_candidate(candidates: Iterable[dict[str, Any]], key: tuple[Any, ...]) -> dict[str, Any]:
    for candidate in candidates:
        if candidate["key"] == key:
            return candidate
    raise KeyError(f"candidate not found: {key!r}")


def _fixed_candidates() -> tuple[dict[str, Any], dict[str, Any]]:
    a_grid = _candidate_grid_a()
    v52 = dict(_find_candidate(a_grid, V52_KEY))
    v52["label"] = "v5.2"
    v51 = dict(_find_candidate(a_grid, V51_KEY))
    v51["label"] = "v5.1"
    return v52, v51


def _inner_folds(outer_train: tuple[int, int]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Split an outer training segment into 3 expanding, time-ordered folds.

    With a 3-year outer train (732 bars), this gives 183-bar validation windows
    and expanding pre-validation histories of 183/366/549 bars. Every inner
    test end is <= outer_train[1], so no outer-test observation can enter a
    candidate score.
    """
    train_lo, train_hi = outer_train
    total = train_hi - train_lo
    inner_test_n = total // (INNER_FOLDS + 1)
    first_train_n = total - INNER_FOLDS * inner_test_n
    if inner_test_n <= 0 or first_train_n < WARMUP:
        raise RuntimeError(
            f"invalid inner split for outer train {outer_train}: "
            f"first_train={first_train_n}, inner_test={inner_test_n}"
        )
    folds = []
    for index in range(INNER_FOLDS):
        test_lo = train_lo + first_train_n + index * inner_test_n
        test_hi = test_lo + inner_test_n
        inner_train = (train_lo, test_lo)
        inner_test = (test_lo, test_hi)
        if not (inner_train[0] < inner_train[1] <= inner_test[0] < inner_test[1] <= train_hi):
            raise RuntimeError(f"inner future-leak split: {inner_train}, {inner_test}, outer={outer_train}")
        folds.append((inner_train, inner_test))
    return folds


def _date_window(frame: pd.DataFrame, start: int, end: int) -> str:
    return f"{frame.index[start].date()}..{frame.index[end].date()}"


def _replay_segment(
    context: dict[str, Any],
    candidate: dict[str, Any],
    start: int,
    end: int,
    initial_prev: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay only positions/returns from the precomputed factor context."""
    frame = context["frame"]
    scores = context["scores"]
    caps = context["caps"]
    erp_series = context["erp_series"]
    if start < WARMUP or end <= start or end >= len(frame):
        raise ValueError(f"invalid replay window: {start}:{end} for {len(frame)} bars")

    prev = _clone_state(initial_prev)
    positions: list[float] = []
    daily_rets: list[float] = []
    bh_rets: list[float] = []
    with _state_parameters(candidate):
        for day in range(start, end):
            cap = caps[day]
            if (
                candidate["erp_cap"]
                and erp_series is not None
                and erp_series[day] is not None
                and erp_series[day] < 0.10
            ):
                cap = min(cap, 0.6)
            decision = ct.decide_position(
                scores[day], cap, prev, tiers=candidate["tiers"]
            )
            position = float(decision["position"])
            ret = float(frame["close"].iloc[day + 1] / frame["close"].iloc[day] - 1.0)
            fee_cost = 0.0  # fee=0 is a task invariant
            daily_rets.append((1.0 - fee_cost) * (1.0 + position * ret) - 1.0)
            bh_rets.append(ret)
            positions.append(position)
            prev = {
                "position": decision["position"],
                "pending": decision["pending"],
            }
    return {
        "positions": positions,
        "daily_rets": daily_rets,
        "bh_rets": bh_rets,
        "final_state": _clone_state(prev),
    }


def _state_at_boundary(
    context: dict[str, Any],
    candidate: dict[str, Any],
    boundary: int,
    state_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, Any]:
    """Match backtest_metrics' cold-start boundary replay exactly."""
    cache_key = (_candidate_signature(candidate), boundary)
    if cache_key in state_cache:
        return _clone_state(state_cache[cache_key])
    if boundary <= WARMUP:
        state = _clone_state(None)
    else:
        state = _replay_segment(context, candidate, WARMUP, boundary)["final_state"]
    state_cache[cache_key] = _clone_state(state)
    return _clone_state(state)


def _segment_row(
    context: dict[str, Any],
    segment: dict[str, Any],
    start: int,
    end: int,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    frame = context["frame"]
    stats = wfv._curve_stats(segment["daily_rets"])
    bh_navs = [
        float(frame["close"].iloc[index + 1] / frame["close"].iloc[start])
        for index in range(start, end)
    ]
    bh_equity = [1.0] + bh_navs
    bh_mdd = (
        min(value / max(bh_equity[:i + 1]) - 1.0 for i, value in enumerate(bh_equity))
        if bh_navs
        else 0.0
    )
    bh_total = float(frame["close"].iloc[end] / frame["close"].iloc[start] - 1.0)
    stats["calmar"] = _calmar(stats["cagr"], stats["mdd"])
    stats.update(
        {
            "bh": bh_total,
            "bh_mdd": bh_mdd,
            "bh_navs": bh_navs,
            "bh_rets": list(segment["bh_rets"]),
            "daily_rets": list(segment["daily_rets"]),
            "positions": list(segment["positions"]),
            "candidate": candidate,
            "start": start,
            "end": end,
            "final_state": _clone_state(segment["final_state"]),
        }
    )
    return stats


def _summary_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    daily_rets = [ret for row in rows for ret in row["daily_rets"]]
    bh_rets = [ret for row in rows for ret in row["bh_rets"]]
    positions = [position for row in rows for position in row["positions"]]
    strategy = wfv._curve_stats(daily_rets)
    benchmark = wfv._curve_stats(bh_rets)
    strategy["calmar"] = _calmar(strategy["cagr"], strategy["mdd"])
    cash = opt._cash_stats(daily_rets, positions)
    cash["calmar"] = _calmar(cash["cagr"], cash["mdd"])
    cash["bh"] = benchmark["total"]
    cash["bh_mdd"] = benchmark["mdd"]
    return {
        **strategy,
        "bh": benchmark["total"],
        "bh_cagr": benchmark["cagr"],
        "bh_sharpe": benchmark["sharpe"],
        "bh_mdd": benchmark["mdd"],
        "n_navs": len(daily_rets),
        "rows": rows,
        "fold_totals": [row["total"] for row in rows],
        "cash": cash,
    }


def _evaluate_candidate_on_folds(
    context: dict[str, Any],
    candidate: dict[str, Any],
    folds: list[tuple[tuple[int, int], tuple[int, int]]],
    state_cache: dict[tuple[Any, ...], dict[str, Any]],
    progress_label: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    running_state: dict[str, Any] | None = None
    for fold_index, (_train, test) in enumerate(folds, 1):
        test_start, test_end = test
        if running_state is None:
            running_state = _state_at_boundary(context, candidate, test_start, state_cache)
        segment = _replay_segment(
            context, candidate, test_start, test_end, running_state
        )
        rows.append(_segment_row(context, segment, test_start, test_end, candidate))
        running_state = _clone_state(segment["final_state"])
        if progress_label is not None:
            print(f"    {progress_label}: outer fold {fold_index}/{len(folds)}")
    return _summary_from_rows(rows)


def _objective_value(summary: dict[str, Any], criterion: str) -> float:
    if criterion == "return":
        return float(summary["total"])
    if criterion == "calmar":
        return float(summary["calmar"])
    if criterion == "penalty":
        mdd = abs(float(summary["mdd"]))
        if mdd == 0.0:
            return float("inf") if summary["total"] > 0 else float("-inf")
        return float(summary["total"]) / mdd
    raise KeyError(criterion)


def _select_candidates(
    candidates: list[dict[str, Any]],
    inner_by_signature: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    selections: dict[str, dict[str, Any]] = {}
    for criterion in CRITERIA:
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                _objective_value(inner_by_signature[_candidate_signature(candidate)], criterion),
                -candidates.index(candidate),
            ),
            reverse=True,
        )
        selected = ranked[0]
        selected_summary = inner_by_signature[_candidate_signature(selected)]
        selections[criterion] = {
            "candidate": selected,
            "score": _objective_value(selected_summary, criterion),
        }
    return selections


def _neighbor_candidates(
    candidate: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_key = {item["key"]: item for item in candidates}
    family = candidate["family"]
    neighbors: list[dict[str, Any]] = []
    if family == "A":
        axes = (
            ("full", tuple(float(x) for x in opt.FULL_THRESHOLDS)),
            ("nine", tuple(float(x) for x in opt.NINE_THRESHOLDS)),
            ("six", tuple(float(x) for x in opt.SIX_THRESHOLDS)),
        )
        values = [float(candidate["full"]), float(candidate["nine"]), float(candidate["six"])]
        for axis, (_name, axis_values) in enumerate(axes):
            index = axis_values.index(values[axis])
            for offset in (-1, 1):
                neighbor_index = index + offset
                if 0 <= neighbor_index < len(axis_values):
                    key_values = values.copy()
                    key_values[axis] = axis_values[neighbor_index]
                    key = (key_values[0], key_values[1], key_values[2], bool(candidate["erp_cap"]))
                    if key in by_key:
                        neighbors.append(by_key[key])
    elif family == "B1":
        values = list(B1_THRESHOLDS)
        index = values.index(float(candidate["thirty"]))
        for offset in (-1, 1):
            neighbor_index = index + offset
            if 0 <= neighbor_index < len(values):
                key = ("B1", values[neighbor_index])
                if key in by_key:
                    neighbors.append(by_key[key])
    elif family == "B2":
        hyst = float(candidate["hyst_margin"])
        confirm = int(candidate["confirm_days"])
        hyst_index = B2_HYST_MARGINS.index(hyst)
        confirm_index = B2_CONFIRM_DAYS.index(confirm)
        for neighbor_index in (hyst_index - 1, hyst_index + 1):
            if 0 <= neighbor_index < len(B2_HYST_MARGINS):
                key = ("B2", B2_HYST_MARGINS[neighbor_index], confirm)
                if key in by_key:
                    neighbors.append(by_key[key])
        for neighbor_index in (confirm_index - 1, confirm_index + 1):
            if 0 <= neighbor_index < len(B2_CONFIRM_DAYS):
                key = ("B2", hyst, B2_CONFIRM_DAYS[neighbor_index])
                if key in by_key:
                    neighbors.append(by_key[key])
    return neighbors


def _relative_neighbor_score(
    criterion: str, chosen: dict[str, Any], neighbor: dict[str, Any]
) -> float | None:
    chosen_value = _objective_value(chosen, criterion)
    neighbor_value = _objective_value(neighbor, criterion)
    if criterion == "return":
        chosen_value += 1.0
        neighbor_value += 1.0
    if not math.isfinite(chosen_value) or chosen_value <= 0.0:
        return None
    return neighbor_value / chosen_value


def _flatness_report(
    family_candidates: list[dict[str, Any]],
    events: list[dict[str, Any]],
    criterion: str,
) -> dict[str, Any]:
    ratios: list[float] = []
    gaps: list[float] = []
    entries: list[dict[str, Any]] = []
    for event in events:
        selected = event["selections"][criterion]["candidate"]
        selected_sig = _candidate_signature(selected)
        chosen_summary = event["inner_by_signature"][selected_sig]
        neighbor_rows = []
        for neighbor in _neighbor_candidates(selected, family_candidates):
            neighbor_summary = event["inner_by_signature"][_candidate_signature(neighbor)]
            ratio = _relative_neighbor_score(criterion, chosen_summary, neighbor_summary)
            if ratio is not None:
                ratios.append(ratio)
            gaps.append(
                _objective_value(neighbor_summary, criterion)
                - _objective_value(chosen_summary, criterion)
            )
            neighbor_rows.append(
                {
                    "candidate": _format_candidate(neighbor),
                    "ratio": ratio,
                    "score_gap": gaps[-1],
                }
            )
        entries.append(
            {
                "fold": event["fold"],
                "selected": _format_candidate(selected),
                "neighbors": neighbor_rows,
            }
        )
    return {
        "min_ratio": min(ratios) if ratios else None,
        "median_ratio": statistics.median(ratios) if ratios else None,
        "min_gap": min(gaps) if gaps else None,
        "max_gap": max(gaps) if gaps else None,
        "pairs": len(gaps),
        "entries": entries,
    }


def _print_inner_fold_audit(
    frame: pd.DataFrame,
    outer_fold: int,
    outer_train: tuple[int, int],
    outer_test: tuple[int, int],
    inner_folds: list[tuple[tuple[int, int], tuple[int, int]]],
) -> None:
    print(
        f"  outer {outer_fold}/{EXPECTED_OUTER_FOLDS}: "
        f"train={_date_window(frame, *outer_train)} "
        f"test={_date_window(frame, *outer_test)}"
    )
    for index, (inner_train, inner_test) in enumerate(inner_folds, 1):
        if inner_test[1] > outer_train[1]:
            raise RuntimeError("inner fold reaches beyond outer training end")
        print(
            f"    inner {index}/{INNER_FOLDS}: "
            f"train={_date_window(frame, *inner_train)} "
            f"validate={_date_window(frame, *inner_test)} "
            f"(validate_end<=outer_train_end: {inner_test[1] <= outer_train[1]})"
        )


def _run_nested_family(
    family_name: str,
    candidates: list[dict[str, Any]],
    context: dict[str, Any],
    outer_folds: list[tuple[tuple[int, int], tuple[int, int]]],
    state_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, Any]:
    frame = context["frame"]
    print(
        f"\n[{family_name}] nested selection: candidates={len(candidates)}, "
        f"outer={len(outer_folds)}, inner={INNER_FOLDS}, "
        "selection uses no-interest inner OOS only"
    )
    events: list[dict[str, Any]] = []
    for outer_index, (outer_train, outer_test) in enumerate(outer_folds, 1):
        inner_folds = _inner_folds(outer_train)
        _print_inner_fold_audit(
            frame, outer_index, outer_train, outer_test, inner_folds
        )
        inner_by_signature: dict[tuple[Any, ...], dict[str, Any]] = {}
        for candidate_index, candidate in enumerate(candidates, 1):
            inner_by_signature[_candidate_signature(candidate)] = _evaluate_candidate_on_folds(
                context, candidate, inner_folds, state_cache
            )
            if candidate_index % max(1, min(12, len(candidates))) == 0 or candidate_index == len(candidates):
                print(
                    f"    inner candidates {candidate_index}/{len(candidates)} "
                    f"(outer {outer_index}/{len(outer_folds)})"
                )
        selections = _select_candidates(candidates, inner_by_signature)
        for criterion in CRITERIA:
            selected = selections[criterion]["candidate"]
            print(
                f"    selected {CRITERION_LABELS[criterion]}: "
                f"{_format_candidate(selected)} "
                f"score={selections[criterion]['score']:+.6f}"
            )
        events.append(
            {
                "fold": outer_index,
                "outer_train": outer_train,
                "outer_test": outer_test,
                "inner_folds": inner_folds,
                "inner_by_signature": inner_by_signature,
                "selections": selections,
            }
        )

    nested_by_criterion: dict[str, dict[str, Any]] = {}
    for criterion in CRITERIA:
        print(f"  [{family_name}] outer evaluation: {CRITERION_LABELS[criterion]}")
        rows: list[dict[str, Any]] = []
        running_state: dict[str, Any] | None = None
        selected_sequence: list[str] = []
        for event in events:
            candidate = event["selections"][criterion]["candidate"]
            test_start, test_end = event["outer_test"]
            if running_state is None:
                running_state = _state_at_boundary(
                    context, candidate, test_start, state_cache
                )
            segment = _replay_segment(
                context, candidate, test_start, test_end, running_state
            )
            row = _segment_row(context, segment, test_start, test_end, candidate)
            row["bh_rets"] = list(segment["bh_rets"])
            rows.append(row)
            running_state = _clone_state(segment["final_state"])
            selected_sequence.append(_format_candidate(candidate))
            print(
                f"    outer {event['fold']}/{len(events)}: "
                f"{_format_candidate(candidate):<30} "
                f"return={_pct(row['total'])} MDD={_pct(row['mdd'])} "
                f"state={running_state['position']:.1%}"
            )
        summary = _summary_from_rows(rows)
        summary["selected_sequence"] = selected_sequence
        nested_by_criterion[criterion] = summary

    flatness = {
        criterion: _flatness_report(candidates, events, criterion)
        for criterion in CRITERIA
    }
    print(f"[{family_name}] inner-only neighborhood flatness:")
    for criterion in CRITERIA:
        report = flatness[criterion]
        min_ratio = "NA" if report["min_ratio"] is None else f"{report['min_ratio']:.3f}"
        median_ratio = "NA" if report["median_ratio"] is None else f"{report['median_ratio']:.3f}"
        print(
            f"  {criterion}: neighbor_pairs={report['pairs']} "
            f"min_ratio={min_ratio} median_ratio={median_ratio} "
            "(computed from inner scores; outer test not used)"
        )
        for entry in report["entries"]:
            neighbor_parts = []
            for row in entry["neighbors"]:
                ratio_text = "NA" if row["ratio"] is None else f"{row['ratio']:.3f}"
                neighbor_parts.append(f"{row['candidate']} ratio={ratio_text}")
            neighbor_text = ", ".join(neighbor_parts) or "none"
            print(
                f"    fold {entry['fold']}: {entry['selected']} | {neighbor_text}"
            )
    return {
        "family": family_name,
        "candidates": candidates,
        "events": events,
        "nested": nested_by_criterion,
        "flatness": flatness,
    }


def _hold_summary(context: dict[str, Any], folds: list[tuple[tuple[int, int], tuple[int, int]]]) -> dict[str, Any]:
    frame = context["frame"]
    rows: list[dict[str, Any]] = []
    for _train, test in folds:
        start, end = test
        rets = [
            float(frame["close"].iloc[index + 1] / frame["close"].iloc[index] - 1.0)
            for index in range(start, end)
        ]
        rows.append(
            {
                "total": wfv._curve_stats(rets)["total"],
                "daily_rets": rets,
                "bh_rets": list(rets),
                "positions": [1.0] * len(rets),
                "mdd": wfv._curve_stats(rets)["mdd"],
            }
        )
    summary = _summary_from_rows(rows)
    summary["selected_sequence"] = ["buy-and-hold"] * len(rows)
    return summary


def _run_fixed_baselines(
    context: dict[str, Any],
    folds: list[tuple[tuple[int, int], tuple[int, int]]],
    v52: dict[str, Any],
    v51: dict[str, Any],
    state_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    print("\nFixed baselines: replaying v5.2, v5.1, and buy-and-hold")
    out: dict[str, dict[str, Any]] = {}
    for label, candidate in (("fixed v5.2", v52), ("fixed v5.1", v51)):
        print(f"  {label}: outer folds 0/{len(folds)}")
        summary = _evaluate_candidate_on_folds(
            context, candidate, folds, state_cache, progress_label=label
        )
        out[label] = summary
        print(
            f"  {label}: OOS no-interest={_pct(summary['total'])} "
            f"MDD={_pct(summary['mdd'])}; with-interest="
            f"{_pct(summary['cash']['total'])} MDD={_pct(summary['cash']['mdd'])}"
        )
    hold = _hold_summary(context, folds)
    out["buy-and-hold"] = hold
    print(
        f"  buy-and-hold: OOS={_pct(hold['total'])} MDD={_pct(hold['mdd'])} "
        f"(interest identical because position=100%)"
    )
    return out


def _print_summary_table(
    title: str,
    summaries: dict[str, dict[str, Any]],
    fixed: dict[str, dict[str, Any]] | None = None,
) -> None:
    print(f"\n{title}")
    print(
        f"{'configuration':<38}"
        "no-interest total/CAGR/MDD/Calmar                 "
        "with-interest total/CAGR/MDD/Calmar"
    )
    for label, summary in summaries.items():
        cash = summary["cash"]
        print(
            f"{label:<38}"
            f"{_pct(summary['total']):>8}/{_pct(summary['cagr']):>8}/"
            f"{_pct(summary['mdd']):>8}/{summary['calmar']:.3f}       "
            f"{_pct(cash['total']):>8}/{_pct(cash['cagr']):>8}/"
            f"{_pct(cash['mdd']):>8}/{cash['calmar']:.3f}"
        )
        if fixed is not None and label in fixed:
            baseline = fixed[label]
            print(
                f"  vs fixed v5.2: return={_pp(summary['total'] - baseline['total'])} "
                f"MDD gap={_pp(summary['mdd'] - baseline['mdd'])} "
                f"with-interest return={_pp(cash['total'] - baseline['cash']['total'])}"
            )


def _dominates(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return (
        candidate["total"] >= baseline["total"]
        and candidate["mdd"] >= baseline["mdd"]
        and (
            candidate["total"] > baseline["total"]
            or candidate["mdd"] > baseline["mdd"]
        )
    )


def _print_baseline_assertions(baselines: dict[str, dict[str, Any]]) -> None:
    print("\nRequired baseline cross-checks (rounded reference values)")
    for label, (expected_total, expected_mdd) in EXPECTED_BASELINES.items():
        observed = baselines[label]
        total_diff = abs(observed["total"] - expected_total)
        mdd_diff = abs(observed["mdd"] - expected_mdd)
        print(
            f"  {label}: observed={_pct(observed['total'])}/{_pct(observed['mdd'])} "
            f"expected={_pct(expected_total)}/{_pct(expected_mdd)} "
            f"diff={total_diff:.8f}/{mdd_diff:.8f}"
        )
        if total_diff > BASELINE_ROUNDED_TOLERANCE or mdd_diff > BASELINE_ROUNDED_TOLERANCE:
            raise RuntimeError(
                f"baseline mismatch for {label}: observed "
                f"{observed['total']}/{observed['mdd']} vs "
                f"reference {expected_total}/{expected_mdd}"
            )
    print("  baseline rounded-value checks: PASS")


def _compare_replay_to_backtest(
    label: str,
    df: pd.DataFrame,
    context: dict[str, Any],
    candidate: dict[str, Any],
    start: int,
    end: int,
    initial_prev: dict[str, Any] | None,
) -> None:
    """Compare daily replay and authoritative backtest_metrics at 1e-9."""
    replay = _replay_segment(context, candidate, start, end, initial_prev)
    with _state_parameters(candidate):
        metrics = rct.backtest_metrics(
            df,
            fee=FEE,
            pe_map=context.get("pe_map"),
            tiers=candidate["tiers"],
            erp_cap=bool(candidate["erp_cap"]),
            eval_start=start,
            eval_end=end,
            initial_prev=initial_prev,
        )
    replay_stats = wfv._curve_stats(replay["daily_rets"])
    replay_stats["calmar"] = _calmar(replay_stats["cagr"], replay_stats["mdd"])
    replay_bh = float(
        context["frame"]["close"].iloc[end]
        / context["frame"]["close"].iloc[start]
        - 1.0
    )
    replay_bh_navs = [
        float(context["frame"]["close"].iloc[index + 1]
              / context["frame"]["close"].iloc[start])
        for index in range(start, end)
    ]
    replay_bh_equity = [1.0] + replay_bh_navs
    replay_bh_mdd = min(
        value / max(replay_bh_equity[:i + 1]) - 1.0
        for i, value in enumerate(replay_bh_equity)
    ) if replay_bh_navs else 0.0
    fields = {
        "total": replay_stats["total"] - metrics["total"],
        "cagr": replay_stats["cagr"] - metrics["cagr"],
        "sharpe": replay_stats["sharpe"] - metrics["sharpe"],
        "mdd": replay_stats["mdd"] - metrics["mdd"],
        "calmar": replay_stats["calmar"] - metrics["calmar"],
        "bh": replay_bh - metrics["bh"],
        "bh_mdd": replay_bh_mdd - metrics["bh_mdd"],
    }
    daily_diff = max(
        [
            abs(left - right)
            for left, right in zip(replay["daily_rets"], metrics["daily_rets"])
        ]
        or [0.0]
    )
    max_metric_diff = max(abs(value) for value in fields.values())
    replay_state = replay["final_state"]
    metric_state = _clone_state(metrics["final_state"])
    state_diff = abs(replay_state["position"] - metric_state["position"])
    if replay_state["pending"] != metric_state["pending"]:
        state_diff = max(state_diff, 1.0)
    print(
        f"  {label}: daily_max_diff={daily_diff:.3e} "
        f"metric_max_diff={max_metric_diff:.3e} state_diff={state_diff:.3e}"
    )
    if max(daily_diff, max_metric_diff, state_diff) >= 1e-9:
        raise RuntimeError(
            f"replay validation failed for {label}: "
            f"daily={daily_diff}, metric={max_metric_diff}, state={state_diff}"
        )


def _run_replay_validations(
    df: pd.DataFrame,
    context: dict[str, Any],
    outer_folds: list[tuple[tuple[int, int], tuple[int, int]]],
    v52: dict[str, Any],
    v51: dict[str, Any],
    b1_candidates: list[dict[str, Any]],
    b2_candidates: list[dict[str, Any]],
    state_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> None:
    print("\nReplay correctness validation against backtest_metrics")
    n = len(context["frame"])
    checks = [
        ("v5.2 full window", v52, WARMUP, n - 1, None),
        ("v5.1 full window", v51, WARMUP, n - 1, None),
    ]
    first_test = outer_folds[0][1]
    first_state = _state_at_boundary(context, v52, first_test[0], state_cache)
    checks.append(("v5.2 outer fold 1", v52, first_test[0], first_test[1], first_state))
    b1 = _find_candidate(b1_candidates, ("B1", -0.50))
    b1_test = outer_folds[4][1]
    b1_state = _state_at_boundary(context, b1, b1_test[0], state_cache)
    checks.append(("B1 -0.50 outer fold 5", b1, b1_test[0], b1_test[1], b1_state))
    b2 = _find_candidate(b2_candidates, ("B2", 0.08, 3))
    b2_test = outer_folds[-1][1]
    b2_state = _state_at_boundary(context, b2, b2_test[0], state_cache)
    checks.append(("B2 H0.08/C3 outer fold 9", b2, b2_test[0], b2_test[1], b2_state))
    for label, candidate, start, end, initial_prev in checks:
        _compare_replay_to_backtest(
            label, df, context, candidate, start, end, initial_prev
        )
    print("  replay correctness checks: PASS (all differences < 1e-9)")


def _print_nested_answer(
    family_name: str,
    result: dict[str, Any],
    fixed_v52: dict[str, Any],
) -> None:
    print(f"\n{family_name} nested OOS vs fixed v5.2")
    for criterion in CRITERIA:
        summary = result["nested"][criterion]
        better = _dominates(summary, fixed_v52)
        print(
            f"  {CRITERION_LABELS[criterion]}: "
            f"{_pct(summary['total'])}/{_pct(summary['mdd'])} vs "
            f"{_pct(fixed_v52['total'])}/{_pct(fixed_v52['mdd'])}; "
            f"with-interest { _pct(summary['cash']['total']) } vs "
            f"{_pct(fixed_v52['cash']['total'])}; "
            f"{'YES: dominates on return and MDD' if better else 'NO: not superior on both return and MDD'}"
        )


def _run() -> None:
    started = time.perf_counter()
    print("Chinext timing third round | nested walk-forward selection | fee=0")
    print(
        f"fixed state: HYST_MARGIN={DEFAULT_HYST_MARGIN:.2f}, "
        f"UPGRADE_CONFIRM_DAYS={DEFAULT_CONFIRM_DAYS}; "
        f"cash interest={CASH_ANNUAL_RATE:.2%}/year, daily={CASH_DAILY_RATE:.12f}"
    )
    print(
        "penalty objective: inner compound return / abs(inner MDD); "
        "selection columns are no-interest, cash columns are display-only"
    )

    v52, v51 = _fixed_candidates()
    a_candidates = _candidate_grid_a()
    b1_candidates = _candidate_grid_b1()
    b2_candidates = _candidate_grid_b2()
    if len(b1_candidates) != 3 or len(b2_candidates) != 12:
        raise RuntimeError("controlled extension grid size changed")
    print(
        f"candidate grids: A={len(a_candidates)}; "
        f"B1={len(b1_candidates)}; B2={len(b2_candidates)}"
    )

    df = opt.load_df()
    pe_map = ipe.load_cy50_pe(PROJECT_ROOT)
    if not pe_map:
        pe_map = None
        print("WARNING: cy50 PE unavailable; ERP ON/OFF are equivalent")
    context = opt._build_replay_context(df, pe_map)
    context["pe_map"] = pe_map
    frame = context["frame"]
    folds = wfv.split_folds(frame.index, TRAIN_YEARS, TEST_YEARS)
    if len(folds) != EXPECTED_OUTER_FOLDS:
        raise SystemExit(
            f"expected {EXPECTED_OUTER_FOLDS} outer folds, got {len(folds)} "
            f"for {len(frame)} bars"
        )
    print(
        f"data: {len(frame)} bars {frame.index.min().date()} -> {frame.index.max().date()}; "
        f"outer={TRAIN_YEARS}y train/{TEST_YEARS}y test/{len(folds)} folds"
    )
    print(
        "outer rule: selected candidate is applied to each test fold; "
        "the previous fold's actual {position,pending} is inherited"
    )

    state_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    _run_replay_validations(
        df, context, folds, v52, v51, b1_candidates, b2_candidates, state_cache
    )
    baselines = _run_fixed_baselines(context, folds, v52, v51, state_cache)
    _print_baseline_assertions(baselines)

    a_result = _run_nested_family("A", a_candidates, context, folds, state_cache)
    b1_result = _run_nested_family("B1", b1_candidates, context, folds, state_cache)
    b2_result = _run_nested_family("B2", b2_candidates, context, folds, state_cache)

    _print_summary_table(
        "A: nested selection results (each criterion has its own outer OOS path)",
        a_result["nested"],
    )
    _print_nested_answer("A", a_result, baselines["fixed v5.2"])

    _print_summary_table(
        "B1: five-tier nested results vs fixed v5.2",
        b1_result["nested"],
    )
    _print_nested_answer("B1", b1_result, baselines["fixed v5.2"])

    _print_summary_table(
        "B2: state-machine nested results vs fixed v5.2",
        b2_result["nested"],
    )
    _print_nested_answer("B2", b2_result, baselines["fixed v5.2"])

    print("\nReference comparison: fixed v5.2 / fixed v5.1 / buy-and-hold")
    _print_summary_table("Fixed references", baselines)

    for family_name, result in (("B1", b1_result), ("B2", b2_result)):
        dominators = [
            criterion
            for criterion in CRITERIA
            if _dominates(result["nested"][criterion], baselines["fixed v5.2"])
        ]
        if not dominators:
            print(f"{family_name} conclusion: 嵌套 OOS 不优于固定 v5.2，不建议启用")
        else:
            print(
                f"{family_name} conclusion: only {', '.join(dominators)} "
                "dominates fixed v5.2 on return and MDD; no automatic production change"
            )

    a_dominators = [
        criterion
        for criterion in CRITERIA
        if _dominates(a_result["nested"][criterion], baselines["fixed v5.2"])
    ]
    print(
        "\nA direct answer: "
        + (
            f"YES for {', '.join(a_dominators)}; "
            "the other criteria are reported separately."
            if a_dominators
            else "NO; no inner selection criterion dominates fixed v5.2 on both return and MDD."
        )
    )
    print(
        "All displayed returns/MDDs are computed from this run; "
        "no full-sample best candidate was used for the nested conclusions."
    )
    elapsed = time.perf_counter() - started
    print(f"runtime_seconds={elapsed:.3f} target_under_900s={'PASS' if elapsed < 900 else 'WARN'}")


def main() -> None:
    configure_stdout = getattr(rct, "_configure_stdout", None)
    if configure_stdout is not None:
        configure_stdout()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    with LOG_PATH.open("w", encoding="utf-8") as log_file:
        tee = _Tee(old_stdout, log_file)
        sys.stdout = tee
        sys.stderr = tee
        try:
            _run()
        except Exception:
            traceback.print_exc()
            raise
        finally:
            tee.flush()
            sys.stdout = old_stdout
            sys.stderr = old_stderr


if __name__ == "__main__":
    main()
