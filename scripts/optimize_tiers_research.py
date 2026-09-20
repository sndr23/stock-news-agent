# -*- coding: utf-8 -*-
"""创业板择时档位研究脚本。

本脚本只做研究，不修改生产参数。它复用现有的 ``backtest_metrics`` 和
``walk_forward_validation`` 窗口口径，固定 fee=0，并且只搜索题目规定的
4 x 3 x 3 x 2 = 72 个参数组合。

运行：
    python scripts/optimize_tiers_research.py
"""
from __future__ import annotations

import itertools
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import run_chinext_timing as rct  # noqa: E402
from scripts import walk_forward_validation as wfv  # noqa: E402
from src.strategy import chinext_timing as ct  # noqa: E402
from src.strategy import index_pe as ipe  # noqa: E402
from src.strategy.data_freshness import BJT  # noqa: E402


# 搜索空间是研究合同的一部分，禁止在此处增加维度。
FULL_THRESHOLDS = (0.30, 0.35, 0.40, 0.45)
NINE_THRESHOLDS = (-0.25, -0.15, -0.05)
SIX_THRESHOLDS = (-0.40, -0.30, -0.20)
ERP_OPTIONS = (False, True)

FEE = 0.0
TRAIN_YEARS = 3
TEST_YEARS = 1
EXPECTED_FOLDS = 9
OOS_CALMAR_FLOOR = 0.30
NEIGHBOR_RATIO_FLOOR = 0.50
TRADING_DAYS = wfv.TRADING_DAYS


def candidate_grid() -> Iterable[dict[str, Any]]:
    """返回题目规定的 72 个候选，不扩展滞回或确认日维度。"""
    for full, nine, six, erp_cap in itertools.product(
        FULL_THRESHOLDS,
        NINE_THRESHOLDS,
        SIX_THRESHOLDS,
        ERP_OPTIONS,
    ):
        yield {
            "full": full,
            "nine": nine,
            "six": six,
            "erp_cap": bool(erp_cap),
            # 保持现有状态机的档位顺序；即使某组阈值交叉，也按搜索
            # 空间中声明的“满仓/九成/六成”映射原样传入，不偷偷修正参数。
            "tiers": ((full, 1.0), (nine, 0.9), (six, 0.6)),
        }


def candidate_key(candidate: dict[str, Any]) -> tuple[float, float, float, bool]:
    """候选的稳定哈希键，避免把 tiers 元组作为比较主键。"""
    return (
        float(candidate["full"]),
        float(candidate["nine"]),
        float(candidate["six"]),
        bool(candidate["erp_cap"]),
    )


def format_candidate(candidate: dict[str, Any]) -> str:
    return (
        f"F{candidate['full']:.2f}/N{candidate['nine']:+.2f}/"
        f"S{candidate['six']:+.2f}/ERP{'ON' if candidate['erp_cap'] else 'OFF'}"
    )


def _calmar(cagr: float, mdd: float) -> float:
    return cagr / abs(mdd) if mdd else 0.0


def pareto_frontier(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """收益最大化、回撤绝对值最小化下的非支配点。"""
    frontier: list[dict[str, Any]] = []
    for row in rows:
        dominated = False
        for other in rows:
            if other is row:
                continue
            other_no_worse = (
                other["total"] >= row["total"]
                and abs(other["mdd"]) <= abs(row["mdd"])
            )
            other_strict = (
                other["total"] > row["total"]
                or abs(other["mdd"]) < abs(row["mdd"])
            )
            if other_no_worse and other_strict:
                dominated = True
                break
        if not dominated:
            frontier.append(row)
    return sorted(frontier, key=lambda row: (abs(row["mdd"]), -row["total"]))


def neighbor_check(
    target: dict[str, Any],
    metrics_by_key: dict[tuple[float, float, float, bool], dict[str, Any]],
) -> dict[str, Any]:
    """检查同 ERP 下三个阈值轴的 ±1 档邻居是否保持至少 50% 卡玛。

    “邻域”只包含单轴相邻格点，不包含对角点，也不把 ERP 开关当作档位
    邻居。这样与“±1 档”的研究约束一致。
    """
    values = [FULL_THRESHOLDS, NINE_THRESHOLDS, SIX_THRESHOLDS]
    target_values = [target["full"], target["nine"], target["six"]]
    neighbors: list[tuple[float, float, float, bool]] = []
    for axis, axis_values in enumerate(values):
        index = axis_values.index(target_values[axis])
        for offset in (-1, 1):
            neighbor_index = index + offset
            if not 0 <= neighbor_index < len(axis_values):
                continue
            params = target_values.copy()
            params[axis] = axis_values[neighbor_index]
            key = (params[0], params[1], params[2], bool(target["erp_cap"]))
            if key in metrics_by_key:
                neighbors.append(key)

    base_key = candidate_key(target)
    base_calmar = float(metrics_by_key[base_key]["calmar"])
    neighbor_calmars = [float(metrics_by_key[key]["calmar"]) for key in neighbors]
    if not neighbors or base_calmar <= 0:
        return {
            "passed": False,
            "neighbor_count": len(neighbors),
            "min_ratio": None,
            "max_drop": None,
            "neighbors": neighbors,
        }

    ratios = [value / base_calmar for value in neighbor_calmars]
    min_ratio = min(ratios)
    return {
        "passed": min_ratio >= NEIGHBOR_RATIO_FLOOR,
        "neighbor_count": len(neighbors),
        "min_ratio": min_ratio,
        "max_drop": 1.0 - min_ratio,
        "neighbors": neighbors,
    }


def _assert_fixed_semantics() -> None:
    if not math.isclose(ct.HYST_MARGIN, 0.05, rel_tol=0.0, abs_tol=1e-12):
        raise RuntimeError(f"HYST_MARGIN changed: {ct.HYST_MARGIN!r}")
    if int(ct.UPGRADE_CONFIRM_DAYS) != 2:
        raise RuntimeError(
            f"UPGRADE_CONFIRM_DAYS changed: {ct.UPGRADE_CONFIRM_DAYS!r}"
        )


def load_df() -> pd.DataFrame:
    """按生产链路加载 399006，全量优先，短链路作为免费源降级。"""
    df = rct.load_index_sina("399006")
    if df is None or df.empty:
        df = rct.load_index_daily_full("399006", "20200101")
    if df is None or df.empty:
        raise SystemExit("399006 daily data unavailable from local/free-source chain")

    frame = df.copy()
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.loc[frame.index.notna()].sort_index()
    frame = frame.loc[~frame.index.duplicated(keep="last")]
    today = pd.Timestamp(datetime.now(BJT).date())
    frame = frame.loc[frame.index.normalize() < today]
    missing = {"close", "amount"} - set(frame.columns)
    if missing or len(frame) < 62:
        raise SystemExit(
            f"399006 data invalid: missing={sorted(missing)}, bars={len(frame)}"
        )
    return frame


def _full_metrics(
    candidate: dict[str, Any], df: pd.DataFrame, pe_map: dict[str, float] | None
) -> dict[str, Any]:
    return rct.backtest_metrics(
        df,
        fee=FEE,
        pe_map=pe_map,
        tiers=candidate["tiers"],
        erp_cap=bool(candidate["erp_cap"]),
    )


def _oos_for_candidate(
    candidate: dict[str, Any],
    df: pd.DataFrame,
    pe_map: dict[str, float] | None,
    folds: list[tuple[tuple[int, int], tuple[int, int]]],
) -> dict[str, Any]:
    """固定一个候选参数，按现有 WFV 的 9 折边界和仓位继承规则评估。"""
    rows: list[dict[str, Any]] = []
    running_state = None
    params = (candidate["tiers"], bool(candidate["erp_cap"]))
    for _train, test in folds:
        row = wfv.evaluate_test(
            df,
            test,
            params,
            pe_map,
            FEE,
            initial_prev=running_state,
        )
        rows.append(row)
        running_state = row["final_state"]

    summary = wfv.summarize_oos(rows)
    summary["calmar"] = _calmar(summary["cagr"], summary["mdd"])
    summary["rows"] = rows
    summary["fold_totals"] = [row["total"] for row in rows]
    return summary


def _direction(value: float, eps: float = 1e-12) -> int:
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


def _add_oos_direction_check(
    oos: dict[str, Any], baseline_oos: dict[str, Any]
) -> None:
    candidate_totals = oos["fold_totals"]
    baseline_totals = baseline_oos["fold_totals"]
    checks = [
        _direction(candidate) >= _direction(baseline)
        for candidate, baseline in zip(candidate_totals, baseline_totals)
    ]
    oos["fold_direction_ok"] = checks
    oos["direction_ok"] = all(checks)


def _subsample_windows(
    df: pd.DataFrame,
) -> list[tuple[str, pd.Timestamp, pd.Timestamp, int, int]]:
    dates = pd.DatetimeIndex(pd.to_datetime(df.index)).normalize()
    windows = [
        ("2014-2020", pd.Timestamp("2014-01-01"), pd.Timestamp("2020-01-01")),
        ("2020-2026", pd.Timestamp("2020-01-01"), pd.Timestamp("2027-01-01")),
    ]
    out = []
    for label, start_date, end_date in windows:
        lo = int(dates.searchsorted(start_date, side="left"))
        hi = int(dates.searchsorted(end_date, side="left"))
        eval_start = max(60, lo)
        # backtest_metrics 的 eval_end 是半开区间终点，保留到 end_date
        # 前最后一根可计算 d+1 收益的信号日。
        eval_end = hi - 1
        if eval_end <= eval_start or eval_end >= len(df):
            raise SystemExit(
                f"subsample window too short: {label} {eval_start}:{eval_end}"
            )
        out.append((label, start_date, end_date, eval_start, eval_end))
    return out


def _subsample_for_candidate(
    candidate: dict[str, Any],
    df: pd.DataFrame,
    pe_map: dict[str, float] | None,
    windows: list[tuple[str, pd.Timestamp, pd.Timestamp, int, int]],
) -> dict[str, Any]:
    metrics: dict[str, dict[str, Any]] = {}
    for label, _start_date, _end_date, eval_start, eval_end in windows:
        metrics[label] = rct.backtest_metrics(
            df,
            fee=FEE,
            pe_map=pe_map,
            tiers=candidate["tiers"],
            erp_cap=bool(candidate["erp_cap"]),
            eval_start=eval_start,
            eval_end=eval_end,
        )
    first = metrics["2014-2020"]
    second = metrics["2020-2026"]
    passed = (
        first["calmar"] > 0
        and second["calmar"] > 0
        and _direction(first["cagr"]) == _direction(second["cagr"])
    )
    return {"metrics": metrics, "passed": passed}


def _drift_flag(row: dict[str, Any]) -> str:
    flags = []
    oos = row["oos"]
    if oos["calmar"] < OOS_CALMAR_FLOOR:
        flags.append("OOS<.30")
    if not oos["direction_ok"]:
        flags.append("FOLD_DIR")
    if not row["neighbor"]["passed"]:
        flags.append("NEIGHBOR")
    subsample = row.get("subsample")
    if subsample is None:
        flags.append("HALF_UNRUN")
    elif not subsample["passed"]:
        flags.append("HALF")
    return "NO" if not flags else "YES:" + ",".join(flags)


def _is_gate_qualified(row: dict[str, Any]) -> bool:
    return (
        row["oos"]["calmar"] >= OOS_CALMAR_FLOOR
        and row["oos"]["direction_ok"]
        and row["neighbor"]["passed"]
        and row.get("subsample") is not None
        and row["subsample"]["passed"]
    )


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _print_top10(rows: list[dict[str, Any]]) -> None:
    print("\nTOP10 full-sample Calmar ranking (fee=0)")
    print(
        "#  config                         total     CAGR      MDD  Calmar "
        "switches avg_pos OOSCalmar neighbor drift"
    )
    for rank, row in enumerate(rows[:10], 1):
        m = row["metrics"]
        neighbor = row["neighbor"]
        ratio = "NA" if neighbor["min_ratio"] is None else f"{neighbor['min_ratio']:.2f}"
        neighbor_text = f"{'PASS' if neighbor['passed'] else 'FAIL'}@{ratio}"
        print(
            f"{rank:>2} {format_candidate(row['candidate']):<30}"
            f" {_pct(m['total']):>8} {_pct(m['cagr']):>8} {_pct(m['mdd']):>8}"
            f" {m['calmar']:>6.2f} {m['switches']:>8} {m['avg_pos']:>7.2f}"
            f" {row['oos']['calmar']:>9.2f} {neighbor_text:<13}"
            f" {_drift_flag(row)}"
        )


def _print_pareto(rows: list[dict[str, Any]]) -> None:
    frontier = pareto_frontier(rows)
    print("\nReturn-drawdown Pareto frontier (full sample; total high, |MDD| low)")
    print("config                         total     MDD  Calmar  ERP")
    for row in frontier:
        m = row["metrics"]
        c = row["candidate"]
        print(
            f"{format_candidate(c):<30} {_pct(m['total']):>8} {_pct(m['mdd']):>8}"
            f" {m['calmar']:>6.2f}  {'ON' if c['erp_cap'] else 'OFF'}"
        )


def _print_oos_detail(
    title: str,
    row: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    oos = row["oos"]
    directions = " ".join(
        "OK" if value else "BAD" for value in oos["fold_direction_ok"]
    )
    print(f"\n{title}: {format_candidate(row['candidate'])}")
    print(
        f"OOS total={_pct(oos['total'])} CAGR={_pct(oos['cagr'])} "
        f"MDD={_pct(oos['mdd'])} Calmar={oos['calmar']:.2f}; "
        f"fold direction vs production={directions}"
    )
    print(
        "OOS fold totals candidate: "
        + ", ".join(_pct(value) for value in oos["fold_totals"])
    )
    print(
        "OOS fold totals production: "
        + ", ".join(_pct(value) for value in baseline["fold_totals"])
    )


def _print_subsample_detail(row: dict[str, Any]) -> None:
    subsample = row["subsample"]
    print(f"\nSubsample stability: {format_candidate(row['candidate'])}")
    for label in ("2014-2020", "2020-2026"):
        m = subsample["metrics"][label]
        print(
            f"{label}: total={_pct(m['total'])} CAGR={_pct(m['cagr'])} "
            f"MDD={_pct(m['mdd'])} Calmar={m['calmar']:.2f}"
        )
    print(f"subsample_gate={'PASS' if subsample['passed'] else 'FAIL'}")


def _print_comparison(
    recommended: dict[str, Any], production: dict[str, Any], label: str
) -> None:
    print(f"\n{label} vs production (in-sample and OOS)")
    print(
        "config                         IS total/CAGR/MDD/Calmar "
        "OOS total/CAGR/MDD/Calmar"
    )
    for title, row in ((label, recommended), ("production", production)):
        ins = row["metrics"]
        oos = row["oos"]
        print(
            f"{title:<30}"
            f" {_pct(ins['total'])}/{_pct(ins['cagr'])}/{_pct(ins['mdd'])}/{ins['calmar']:.2f}"
            f" {_pct(oos['total'])}/{_pct(oos['cagr'])}/{_pct(oos['mdd'])}/{oos['calmar']:.2f}"
        )
    print(f"candidate: {format_candidate(recommended['candidate'])}")
    print(f"production: {format_candidate(production['candidate'])}")


def main() -> None:
    configure_stdout = getattr(rct, "_configure_stdout", None)
    if configure_stdout is not None:
        configure_stdout()
    _assert_fixed_semantics()

    print("Chinext timing tier research | objective=full-sample Calmar | fee=0")
    print(
        f"fixed semantics: HYST_MARGIN={ct.HYST_MARGIN:.2f}, "
        f"UPGRADE_CONFIRM_DAYS={ct.UPGRADE_CONFIRM_DAYS}; grid=72"
    )

    df = load_df()
    pe_map = ipe.load_cy50_pe(PROJECT_ROOT)
    if not pe_map:
        pe_map = None
        print("WARNING: cy50 PE unavailable; ERP ON/OFF will be equivalent")

    folds = wfv.split_folds(df.index, TRAIN_YEARS, TEST_YEARS)
    if len(folds) != EXPECTED_FOLDS:
        raise SystemExit(
            f"expected {EXPECTED_FOLDS} WFV folds, got {len(folds)} "
            f"for {len(df)} bars"
        )
    windows = _subsample_windows(df)
    candidates = list(candidate_grid())
    if len(candidates) != 72:
        raise RuntimeError(f"grid size changed: {len(candidates)}")

    print(
        f"data: {len(df)} bars {df.index.min().date()} -> {df.index.max().date()}; "
        f"WFV: train={TRAIN_YEARS}y/test={TEST_YEARS}y/folds={len(folds)}"
    )

    results: list[dict[str, Any]] = []
    print("running full-sample grid...")
    for index, candidate in enumerate(candidates, 1):
        metrics = _full_metrics(candidate, df, pe_map)
        results.append(
            {
                "candidate": candidate,
                "key": candidate_key(candidate),
                "metrics": metrics,
                "total": metrics["total"],
                "mdd": metrics["mdd"],
            }
        )
        if index % 12 == 0 or index == len(candidates):
            print(f"  full {index}/{len(candidates)}")

    results.sort(
        key=lambda row: (
            row["metrics"]["calmar"],
            row["metrics"]["cagr"],
            -abs(row["metrics"]["mdd"]),
            -row["metrics"]["switches"],
        ),
        reverse=True,
    )
    metrics_by_key = {row["key"]: row["metrics"] for row in results}
    for row in results:
        row["neighbor"] = neighbor_check(row["candidate"], metrics_by_key)

    production_key = (0.40, -0.15, -0.30, True)
    production = next(row for row in results if row["key"] == production_key)
    production_off_key = (0.40, -0.15, -0.30, False)
    production_off = next(row for row in results if row["key"] == production_off_key)

    print("running fixed-parameter WFV for all 72 candidates...")
    for index, row in enumerate(results, 1):
        row["oos"] = _oos_for_candidate(row["candidate"], df, pe_map, folds)
        if index % 8 == 0 or index == len(results):
            print(f"  oos {index}/{len(results)}")
    baseline_oos = production["oos"]
    for row in results:
        _add_oos_direction_check(row["oos"], baseline_oos)

    # 先覆盖 Top10 和生产基线，再覆盖所有已经通过 OOS+邻域门槛的候选；
    # 这样推荐不被“只检查单点”的实现细节限制。
    top10_keys = {row["key"] for row in results[:10]}
    subsample_keys = top10_keys | {production["key"]}
    subsample_keys |= {
        row["key"]
        for row in results
        if row["oos"]["calmar"] >= OOS_CALMAR_FLOOR
        and row["oos"]["direction_ok"]
        and row["neighbor"]["passed"]
    }
    rows_by_key = {row["key"]: row for row in results}
    print(f"running subsample checks for {len(subsample_keys)} candidates...")
    for index, key in enumerate(sorted(subsample_keys), 1):
        row = rows_by_key[key]
        row["subsample"] = _subsample_for_candidate(
            row["candidate"], df, pe_map, windows
        )
        if index % 8 == 0 or index == len(subsample_keys):
            print(f"  subsample {index}/{len(subsample_keys)}")

    for row in results:
        row.setdefault("subsample", None)
        row["drift"] = _drift_flag(row)

    top10 = results[:10]
    _print_top10(top10)
    _print_pareto(results)

    global_best = results[0]
    eligible = [row for row in results if _is_gate_qualified(row)]
    recommended = eligible[0] if eligible else None
    print(
        f"\nGate summary: eligible={len(eligible)}/{len(results)}; "
        f"global_best={format_candidate(global_best['candidate'])}"
    )
    _print_oos_detail("Global-best OOS detail", global_best, production["oos"])
    _print_subsample_detail(global_best)
    print(
        "Global-best neighbor check: "
        f"{'PASS' if global_best['neighbor']['passed'] else 'FAIL'}, "
        f"neighbors={global_best['neighbor']['neighbor_count']}, "
        f"min_calmar_ratio={global_best['neighbor']['min_ratio']}"
    )
    if recommended is not None and recommended is not global_best:
        _print_oos_detail("Recommended OOS detail", recommended, production["oos"])
        _print_subsample_detail(recommended)
        print(
            "Recommended neighbor check: "
            f"{'PASS' if recommended['neighbor']['passed'] else 'FAIL'}, "
            f"neighbors={recommended['neighbor']['neighbor_count']}, "
            f"min_calmar_ratio={recommended['neighbor']['min_ratio']}"
        )

    comparison_row = recommended or global_best
    comparison_label = "recommended" if recommended else "best candidate (not gate-qualified)"
    _print_comparison(comparison_row, production, comparison_label)
    print("\nProduction reference (same tiers, ERP switch):")
    for title, row in (("ERP ON", production), ("ERP OFF", production_off)):
        m = row["metrics"]
        print(
            f"{title:<8} IS total={_pct(m['total'])} MDD={_pct(m['mdd'])} "
            f"Calmar={m['calmar']:.2f} OOS Calmar={row['oos']['calmar']:.2f}"
        )

    print("\nConclusion")
    if recommended is None:
        print("No candidate passed all three anti-overfitting gates; do not switch production TIERS.")
    else:
        rec = recommended
        production_improved = (
            rec["metrics"]["calmar"] > production["metrics"]["calmar"]
            and rec["oos"]["calmar"] >= production["oos"]["calmar"]
        )
        same_tiers = rec["candidate"]["tiers"] == production["candidate"]["tiers"]
        print(
            f"Gate-qualified candidate: {format_candidate(rec['candidate'])}; "
            f"all gates={'PASS' if _is_gate_qualified(rec) else 'FAIL'}"
        )
        if same_tiers:
            print("Recommendation: no TIERS change; the qualified candidate uses production tiers.")
        elif not production_improved:
            print(
                "Recommendation: do not switch production TIERS; the qualified candidate "
                "does not improve both in-sample and OOS Calmar versus production."
            )
        else:
            print(
                "Recommendation: suggest switching TIERS only after human review; "
                "no production file was modified."
            )


if __name__ == "__main__":
    main()
