# -*- coding: utf-8 -*-
"""创业板择时档位第二轮寻优：先收益，回撤不深于买入持有。

本脚本是 ``optimize_tiers_research.py`` 的独立研究变体，只新增研究代码，
不修改生产参数或既有回测实现。标准收益、回撤和 9 折 OOS 汇总仍直接复用
``backtest_metrics`` / ``walk_forward_validation``；闲置资金利息只在本脚本
内按同一状态机重放仓位后叠加，用于公平展示，不参与默认寻优门禁。

运行：
    python scripts/optimize_tiers_return_first.py

含息口径：
    interest_return = strategy_return + (1 - position) * (0.02 / 244)
    interest_nav[t] = interest_nav[t - 1] * (1 + interest_return[t])

其中买入持有始终满仓，因此其含息与不含息净值相同。
"""
from __future__ import annotations

import itertools
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import run_chinext_timing as rct  # noqa: E402
from scripts import walk_forward_validation as wfv  # noqa: E402
from src.strategy import chinext_factors as cf  # noqa: E402
from src.strategy import chinext_timing as ct  # noqa: E402
from src.strategy import index_pe as ipe  # noqa: E402
from src.strategy.data_freshness import BJT  # noqa: E402


# 搜索空间是研究合同的一部分，保持与第一轮完全一致。
FULL_THRESHOLDS = (0.30, 0.35, 0.40, 0.45)
NINE_THRESHOLDS = (-0.25, -0.15, -0.05)
SIX_THRESHOLDS = (-0.40, -0.30, -0.20)
ERP_OPTIONS = (False, True)

FEE = 0.0
TRAIN_YEARS = 3
TEST_YEARS = 1
EXPECTED_FOLDS = 9
NEIGHBOR_RATIO_FLOOR = 0.50
TRADING_DAYS = wfv.TRADING_DAYS
CASH_ANNUAL_RATE = 0.02
CASH_DAILY_RATE = CASH_ANNUAL_RATE / TRADING_DAYS
VAL_W = 0.10

PRODUCTION_KEY = (0.40, -0.15, -0.30, True)
PRODUCTION_OFF_KEY = (0.40, -0.15, -0.30, False)


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
            # 保持现有状态机的档位顺序；不对搜索空间中的阈值做隐式修正。
            "tiers": ((full, 1.0), (nine, 0.9), (six, 0.6)),
        }


def candidate_key(candidate: dict[str, Any]) -> tuple[float, float, float, bool]:
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


def _direction(value: float, eps: float = 1e-12) -> int:
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _nav(total: float) -> float:
    return 1.0 + float(total)


def _assert_fixed_semantics() -> None:
    if not math.isclose(ct.HYST_MARGIN, 0.05, rel_tol=0.0, abs_tol=1e-12):
        raise RuntimeError(f"HYST_MARGIN changed: {ct.HYST_MARGIN!r}")
    if int(ct.UPGRADE_CONFIRM_DAYS) != 2:
        raise RuntimeError(
            f"UPGRADE_CONFIRM_DAYS changed: {ct.UPGRADE_CONFIRM_DAYS!r}"
        )


def load_df() -> pd.DataFrame:
    """按生产链路加载 399006，全量优先，免费链路降级。"""
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


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    """使用与 backtest_metrics 相同的未来 bar 排除口径。"""
    frame = df.copy()
    frame.index = pd.to_datetime(frame.index)
    today = pd.Timestamp(datetime.now(BJT).date())
    return frame.loc[frame.index.normalize() < today]


def _build_replay_context(df: pd.DataFrame, pe_map: dict[str, float] | None) -> dict[str, Any]:
    """预计算含息重放所需的公共因子和硬风控序列。

    ``backtest_metrics`` 仍是所有标准指标的唯一来源；这里的公共序列只避免
    为每个候选重复计算同一组因子，且只服务于核对每日 position 和现金利息。
    """
    frame = _clean_frame(df)
    closes = frame["close"].tolist()
    amounts = frame["amount"].tolist() if "amount" in frame else [0.0] * len(closes)
    date_strs = [d.strftime("%Y-%m-%d") for d in frame.index]

    erp_series = None
    if pe_map:
        pe = ipe.align_pe_by_dates(pe_map, date_strs)
        erp_series = ipe.pe_to_cheap_pctile(pe, 500)

    # 与 backtest_metrics 的生产调用完全相同：估值维不进入 core 打分。
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    scores = cf.dimension_score(signals, rct._default_weights(VAL_W))

    caps = []
    for d in range(len(closes)):
        caps.append(
            cf.defensive_state(
                closes[: d + 1],
                None,
                {"risk_off": False, "basis_min_ap": None, "intraday_pct": 0.0},
            )["cap"]
        )

    return {
        "frame": frame,
        "scores": scores,
        "caps": caps,
        "erp_series": erp_series,
    }


def _positions_from_context(
    context: dict[str, Any],
    candidate: dict[str, Any],
    start: int,
    end: int,
    initial_prev: dict[str, Any] | None = None,
) -> list[float]:
    """按 backtest_metrics 同一状态机返回 [start, end) 的每日仓位。"""
    scores = context["scores"]
    caps = context["caps"]
    erp_series = context["erp_series"]
    prev = dict(initial_prev or {"position": 0.0, "pending": None})
    positions = []

    for d in range(start, end):
        cap = caps[d]
        if (
            candidate["erp_cap"]
            and erp_series is not None
            and erp_series[d] is not None
            and erp_series[d] < 0.10
        ):
            cap = min(cap, 0.6)
        dec = ct.decide_position(scores[d], cap, prev, tiers=candidate["tiers"])
        positions.append(float(dec["position"]))
        prev = {"position": dec["position"], "pending": dec["pending"]}
    return positions


def _cash_stats(daily_rets: list[float], positions: list[float]) -> dict[str, Any]:
    """在策略收益上加入闲置资金日息，并重算净值和回撤。"""
    if len(daily_rets) != len(positions):
        raise RuntimeError(
            f"interest replay length mismatch: returns={len(daily_rets)} "
            f"positions={len(positions)}"
        )
    interest_rets = [
        float(ret) + (1.0 - float(position)) * CASH_DAILY_RATE
        for ret, position in zip(daily_rets, positions)
    ]
    stats = wfv._curve_stats(interest_rets)
    stats["daily_rets"] = interest_rets
    stats["nav"] = stats["navs"][-1] if stats["navs"] else 1.0
    return stats


def _full_result(
    candidate: dict[str, Any],
    df: pd.DataFrame,
    pe_map: dict[str, float] | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    metrics = rct.backtest_metrics(
        df,
        fee=FEE,
        pe_map=pe_map,
        tiers=candidate["tiers"],
        erp_cap=bool(candidate["erp_cap"]),
    )
    positions = _positions_from_context(
        context,
        candidate,
        metrics["start"],
        metrics["end"],
    )
    cash = _cash_stats(metrics["daily_rets"], positions)
    cash["bh"] = metrics["bh"]
    cash["bh_mdd"] = metrics["bh_mdd"]
    return {"metrics": metrics, "cash": cash}


def _oos_for_candidate(
    candidate: dict[str, Any],
    df: pd.DataFrame,
    pe_map: dict[str, float] | None,
    context: dict[str, Any],
    folds: list[tuple[tuple[int, int], tuple[int, int]]],
) -> dict[str, Any]:
    """固定候选，按现有 WFV 9 折边界和跨折状态继承规则评估。"""
    rows: list[dict[str, Any]] = []
    all_interest_rets: list[float] = []
    cash_fold_totals: list[float] = []
    running_state = None
    params = (candidate["tiers"], bool(candidate["erp_cap"]))

    for _train, test in folds:
        test_start, test_end = test
        initial_prev = running_state
        if initial_prev is None:
            # 与 evaluate_test 的首折冷启动完全一致，但显式拿到边界状态，
            # 以便含息重放使用相同的 pending/position。
            boundary = rct.backtest_metrics(
                df.iloc[: test_start + 1],
                fee=FEE,
                pe_map=pe_map,
                tiers=candidate["tiers"],
                erp_cap=bool(candidate["erp_cap"]),
                eval_end=test_start,
            )
            initial_prev = boundary["final_state"]

        row = wfv.evaluate_test(
            df,
            test,
            params,
            pe_map,
            FEE,
            initial_prev=initial_prev,
        )
        positions = _positions_from_context(
            context,
            candidate,
            test_start,
            test_end,
            initial_prev=initial_prev,
        )
        cash_rets = _cash_stats(row["daily_rets"], positions)
        all_interest_rets.extend(cash_rets["daily_rets"])
        cash_fold_totals.append(cash_rets["total"])
        row["cash_daily_rets"] = cash_rets["daily_rets"]
        row["cash_total"] = cash_rets["total"]
        rows.append(row)
        running_state = row["final_state"]

    summary = wfv.summarize_oos(rows)
    summary["calmar"] = _calmar(summary["cagr"], summary["mdd"])
    summary["rows"] = rows
    summary["fold_totals"] = [row["total"] for row in rows]
    cash = wfv._curve_stats(all_interest_rets)
    cash["daily_rets"] = all_interest_rets
    cash["nav"] = cash["navs"][-1] if cash["navs"] else 1.0
    cash["bh"] = summary["bh"]
    cash["bh_mdd"] = summary["bh_mdd"]
    cash["fold_totals"] = cash_fold_totals
    summary["cash"] = cash
    return summary


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


def neighbor_check(
    target: dict[str, Any],
    oos_by_key: dict[tuple[float, float, float, bool], dict[str, Any]],
) -> dict[str, Any]:
    """检查同 ERP 下三个阈值轴的 ±1 档邻居 OOS 收益是否达到 50%。"""
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
            if key in oos_by_key:
                neighbors.append(key)

    base_key = candidate_key(target)
    base_total = float(oos_by_key[base_key]["total"])
    neighbor_totals = [float(oos_by_key[key]["total"]) for key in neighbors]
    if not neighbors or base_total <= 0:
        return {
            "passed": False,
            "neighbor_count": len(neighbors),
            "min_ratio": None,
            "neighbors": neighbors,
            "neighbor_totals": neighbor_totals,
        }

    ratios = [value / base_total for value in neighbor_totals]
    min_ratio = min(ratios)
    return {
        "passed": min_ratio >= NEIGHBOR_RATIO_FLOOR,
        "neighbor_count": len(neighbors),
        "min_ratio": min_ratio,
        "neighbors": neighbors,
        "neighbor_totals": neighbor_totals,
    }


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
        first["total"] > 0
        and second["total"] > 0
        and _direction(first["cagr"]) == _direction(second["cagr"])
    )
    return {"metrics": metrics, "passed": passed}


def _set_gate_flags(row: dict[str, Any], production_oos: dict[str, Any]) -> None:
    oos = row["oos"]
    row["gates"] = {
        # MDD 为负值，数值更大代表回撤更浅；等价于绝对回撤小于持有。
        "A_mdd_shallower_than_bh": oos["mdd"] > production_oos["bh_mdd"],
        "B_oos_not_below_production": oos["total"] >= production_oos["total"],
        "C_neighbor_return_flat": row["neighbor"]["passed"],
        "D_subsample": (
            None if row.get("subsample") is None else row["subsample"]["passed"]
        ),
        "E_fold_direction": oos["direction_ok"],
    }


def _is_qualified(row: dict[str, Any]) -> bool:
    gates = row.get("gates", {})
    return all(value is True for value in gates.values())


def _gate_code(row: dict[str, Any]) -> str:
    letters = []
    for key in (
        "A_mdd_shallower_than_bh",
        "B_oos_not_below_production",
        "C_neighbor_return_flat",
        "D_subsample",
        "E_fold_direction",
    ):
        value = row["gates"][key]
        letters.append("Y" if value is True else "-" if value is None else "N")
    return "/".join(letters)


def _print_full_ranking(rows: list[dict[str, Any]]) -> None:
    print("\nFULL 72 RANKING BY OOS TOTAL (objective = summarize_oos total, no interest)")
    print(
        "# config                         IS     IS+cash  IS MDD  IS+cMDD "
        "OOS    OOS+cash OOS MDD OOS+cMDD Calmar Nbr Fold Sub Gate"
    )
    for rank, row in enumerate(rows, 1):
        ins = row["metrics"]
        inc = row["cash"]
        oos = row["oos"]
        oosc = oos["cash"]
        ratio = "NA" if row["neighbor"]["min_ratio"] is None else f"{row['neighbor']['min_ratio']:.2f}"
        neighbor = f"{ratio:>4}"
        subsample = row.get("subsample")
        subsample_status = "Y" if subsample is not None and subsample["passed"] else "-" if subsample is None else "N"
        print(
            f"{rank:>2} {format_candidate(row['candidate']):<30}"
            f" {_pct(ins['total']):>8} {_pct(inc['total']):>8}"
            f" {_pct(ins['mdd']):>8} {_pct(inc['mdd']):>8}"
            f" {_pct(oos['total']):>8} {_pct(oosc['total']):>8}"
            f" {_pct(oos['mdd']):>8} {_pct(oosc['mdd']):>8}"
            f" {oos['calmar']:>5.2f} {neighbor:>4}"
            f" {'Y' if oos['direction_ok'] else 'N':>4}"
            f" {subsample_status:>3}"
            f" {_gate_code(row)}"
        )
    print(
        "Legend: IS/OOS = total return without cash interest; +cash = 2%/244 "
        "idle-cash interest; MDD is shown in the same order."
    )


def _print_subsample_detail(row: dict[str, Any]) -> None:
    subsample = row.get("subsample")
    if subsample is None:
        print("subsample: NOT RUN (another gate already failed)")
        return
    print(f"subsample {format_candidate(row['candidate'])}")
    for label in ("2014-2020", "2020-2026"):
        m = subsample["metrics"][label]
        print(
            f"  {label}: total={_pct(m['total'])} CAGR={_pct(m['cagr'])} "
            f"MDD={_pct(m['mdd'])}"
        )
    print(f"  gate D={'PASS' if subsample['passed'] else 'FAIL'}")


def _print_gate_detail(row: dict[str, Any], production_oos: dict[str, Any]) -> None:
    oos = row["oos"]
    gates = row["gates"]
    print(f"\nGate detail: {format_candidate(row['candidate'])}")
    print(
        f"A MDD: candidate={_pct(oos['mdd'])}, hold={_pct(production_oos['bh_mdd'])} "
        f"=> {'PASS' if gates['A_mdd_shallower_than_bh'] else 'FAIL'}"
    )
    print(
        f"B OOS total: candidate={_pct(oos['total'])}, production={_pct(production_oos['total'])} "
        f"=> {'PASS' if gates['B_oos_not_below_production'] else 'FAIL'}"
    )
    print(
        f"C neighbors: count={row['neighbor']['neighbor_count']} "
        f"min_return_ratio={row['neighbor']['min_ratio']} "
        f"=> {'PASS' if gates['C_neighbor_return_flat'] else 'FAIL'}"
    )
    print(
        "E fold direction: "
        + " ".join("OK" if value else "BAD" for value in oos["fold_direction_ok"])
        + f" => {'PASS' if gates['E_fold_direction'] else 'FAIL'}"
    )
    print(
        "candidate fold totals: "
        + ", ".join(_pct(value) for value in oos["fold_totals"])
    )
    print(
        "production fold totals: "
        + ", ".join(_pct(value) for value in production_oos["fold_totals"])
    )
    print(f"all gates={'PASS' if _is_qualified(row) else 'FAIL/PENDING'}")
    _print_subsample_detail(row)


def _print_three_way(
    selected: dict[str, Any],
    selected_label: str,
    production: dict[str, Any],
) -> None:
    hold_is_total = production["metrics"]["bh"]
    hold_is_mdd = production["metrics"]["bh_mdd"]
    hold_oos = production["oos"]

    print(f"\nTHREE-WAY COMPARISON: {selected_label} vs production vs buy-and-hold")
    print(
        "source/config                    IS total no/2%   IS NAV no/2% "
        "IS MDD no/2%   OOS total no/2%  OOS NAV no/2% OOS MDD no/2%"
    )

    def line(label: str, row: dict[str, Any] | None = None, hold: bool = False) -> None:
        if hold:
            is_total = (hold_is_total, hold_is_total)
            is_mdd = (hold_is_mdd, hold_is_mdd)
            oos_total = (hold_oos["bh"], hold_oos["bh"])
            oos_mdd = (hold_oos["bh_mdd"], hold_oos["bh_mdd"])
        else:
            assert row is not None
            is_total = (row["metrics"]["total"], row["cash"]["total"])
            is_mdd = (row["metrics"]["mdd"], row["cash"]["mdd"])
            oos_total = (row["oos"]["total"], row["oos"]["cash"]["total"])
            oos_mdd = (row["oos"]["mdd"], row["oos"]["cash"]["mdd"])
        print(
            f"{label:<32}"
            f" {_pct(is_total[0])}/{_pct(is_total[1]):>8}"
            f" {_nav(is_total[0]):.3f}/{_nav(is_total[1]):.3f}"
            f" {_pct(is_mdd[0])}/{_pct(is_mdd[1]):>8}"
            f" {_pct(oos_total[0])}/{_pct(oos_total[1]):>8}"
            f" {_nav(oos_total[0]):.3f}/{_nav(oos_total[1]):.3f}"
            f" {_pct(oos_mdd[0])}/{_pct(oos_mdd[1]):>8}"
        )

    line(selected_label, selected)
    line("production ERP ON", production)
    line("buy-and-hold 399006", hold=True)
    print("Pair order in every field: no-interest / with idle-cash interest.")


def _print_reference_cross_checks(
    production: dict[str, Any],
    production_off: dict[str, Any],
    anchor: dict[str, Any],
) -> None:
    print("\nREFERENCE CROSS-CHECKS (standard no-interest metrics)")
    for label, row in (
        ("production ERP ON", production),
        ("production ERP OFF", production_off),
        ("F0.45/N-0.25/S-0.40/ERPOFF", anchor),
    ):
        ins = row["metrics"]
        oos = row["oos"]
        print(
            f"{label:<32} IS total={_pct(ins['total'])} MDD={_pct(ins['mdd'])} "
            f"Calmar={ins['calmar']:.2f}; OOS total={_pct(oos['total'])} "
            f"MDD={_pct(oos['mdd'])} Calmar={oos['calmar']:.2f}"
        )
    print(
        f"OOS buy-and-hold: total={_pct(production['oos']['bh'])} "
        f"MDD={_pct(production['oos']['bh_mdd'])}; "
        f"full-sample buy-and-hold: total={_pct(production['metrics']['bh'])} "
        f"MDD={_pct(production['metrics']['bh_mdd'])}"
    )


def _print_erp_group_summary(rows: list[dict[str, Any]]) -> None:
    print("\nERP ON vs ERP OFF: 36-candidate group comparison")
    group_rows = {}
    for erp in (True, False):
        group = [row for row in rows if bool(row["candidate"]["erp_cap"]) is erp]
        best = max(group, key=lambda row: row["oos"]["total"])
        qualified = [row for row in group if _is_qualified(row)]
        group_rows[erp] = group
        print(
            f"ERP {'ON' if erp else 'OFF':<3}: mean OOS={_pct(statistics.mean(row['oos']['total'] for row in group))} "
            f"median OOS={_pct(statistics.median(row['oos']['total'] for row in group))} "
            f"best={format_candidate(best['candidate'])} {_pct(best['oos']['total'])} "
            f"best MDD={_pct(best['oos']['mdd'])} qualified={len(qualified)}/36"
        )

    on = group_rows[True]
    off = group_rows[False]
    on_mean = statistics.mean(row["oos"]["total"] for row in on)
    off_mean = statistics.mean(row["oos"]["total"] for row in off)
    on_best = max(on, key=lambda row: row["oos"]["total"])
    off_best = max(off, key=lambda row: row["oos"]["total"])
    if on_mean > off_mean and on_best["oos"]["total"] > off_best["oos"]["total"]:
        conclusion = "ERP ON is stronger on both group mean and best OOS total."
    elif on_mean < off_mean and on_best["oos"]["total"] < off_best["oos"]["total"]:
        conclusion = "ERP OFF is stronger on both group mean and best OOS total."
    else:
        conclusion = "ERP ON/OFF is mixed; group mean and best-point rankings disagree."
    print(f"Overall conclusion: {conclusion}")
    print("This group comparison is descriptive; it does not change production ERP settings.")


def _print_final_answer(
    eligible: list[dict[str, Any]],
    global_best: dict[str, Any],
    production: dict[str, Any],
) -> None:
    hold_total = production["oos"]["cash"]["bh"]
    hold_mdd = production["oos"]["cash"]["bh_mdd"]
    hold_beaters = [
        row
        for row in eligible
        if row["oos"]["cash"]["total"] >= hold_total
        and row["oos"]["cash"]["mdd"] > hold_mdd
    ]

    print("\nFINAL ANSWER")
    print(
        f"Question: qualified OOS return >= hold ({_pct(hold_total)} with-interest) "
        f"and OOS drawdown shallower than {_pct(hold_mdd)}?"
    )
    if hold_beaters:
        winner = max(hold_beaters, key=lambda row: row["oos"]["cash"]["total"])
        print(
            f"YES: {format_candidate(winner['candidate'])}; "
            f"OOS return={_pct(winner['oos']['cash']['total'])} vs hold={_pct(hold_total)}, "
            f"OOS MDD={_pct(winner['oos']['cash']['mdd'])} vs hold={_pct(hold_mdd)}."
        )
        print(
            "Switch suggestion only: review this parameter for a possible TIERS switch; "
            "production files were not modified."
        )
        return

    reference = eligible[0] if eligible else global_best
    label = "best qualified candidate" if eligible else "global OOS-return leader (not gate-qualified)"
    return_gap = reference["oos"]["cash"]["total"] - hold_total
    mdd_gap = reference["oos"]["cash"]["mdd"] - hold_mdd
    print(
        f"NO: no qualified candidate satisfies both comparisons. Reference {label}: "
        f"{format_candidate(reference['candidate'])}."
    )
    print(
        f"Quantified gap vs hold on with-interest columns: OOS return "
        f"{_pct(reference['oos']['cash']['total'])} vs {_pct(hold_total)} "
        f"({return_gap * 100:+.1f} percentage points); OOS MDD "
        f"{_pct(reference['oos']['cash']['mdd'])} vs {_pct(hold_mdd)} "
        f"({mdd_gap * 100:+.1f} percentage points, positive = shallower)."
    )
    if not eligible:
        print("No TIERS switch is suggested.")


def main() -> None:
    configure_stdout = getattr(rct, "_configure_stdout", None)
    if configure_stdout is not None:
        configure_stdout()
    _assert_fixed_semantics()

    print("Chinext timing tier research | objective=OOS compound return | fee=0")
    print(
        f"fixed semantics: HYST_MARGIN={ct.HYST_MARGIN:.2f}, "
        f"UPGRADE_CONFIRM_DAYS={ct.UPGRADE_CONFIRM_DAYS}; grid=72"
    )
    print(
        f"cash-interest formula: idle=(1-position), annual={CASH_ANNUAL_RATE:.2%}, "
        f"daily={CASH_DAILY_RATE:.12f}; no-interest columns remain the gate/objective basis"
    )

    candidates = list(candidate_grid())
    if len(candidates) != 72:
        raise RuntimeError(f"grid size changed: {len(candidates)}")

    df = load_df()
    pe_map = ipe.load_cy50_pe(PROJECT_ROOT)
    if not pe_map:
        pe_map = None
        print("WARNING: cy50 PE unavailable; ERP ON/OFF will be equivalent")

    folds = wfv.split_folds(df.index, TRAIN_YEARS, TEST_YEARS)
    if len(folds) != EXPECTED_FOLDS:
        raise SystemExit(
            f"expected {EXPECTED_FOLDS} WFV folds, got {len(folds)} for {len(df)} bars"
        )
    windows = _subsample_windows(df)
    context = _build_replay_context(df, pe_map)
    print(
        f"data: {len(df)} bars {df.index.min().date()} -> {df.index.max().date()}; "
        f"WFV: train={TRAIN_YEARS}y/test={TEST_YEARS}y/folds={len(folds)}"
    )

    rows: list[dict[str, Any]] = []
    print("running full-sample grid and cash-interest replay...")
    for index, candidate in enumerate(candidates, 1):
        result = _full_result(candidate, df, pe_map, context)
        rows.append(
            {
                "candidate": candidate,
                "key": candidate_key(candidate),
                **result,
            }
        )
        if index % 12 == 0 or index == len(candidates):
            print(f"  full {index}/{len(candidates)}")

    production = next(row for row in rows if row["key"] == PRODUCTION_KEY)
    production_off = next(row for row in rows if row["key"] == PRODUCTION_OFF_KEY)

    print("running fixed-parameter WFV for all 72 candidates...")
    for index, row in enumerate(rows, 1):
        row["oos"] = _oos_for_candidate(
            row["candidate"], df, pe_map, context, folds
        )
        if index % 8 == 0 or index == len(rows):
            print(f"  oos {index}/{len(rows)}")

    baseline_oos = production["oos"]
    oos_by_key = {row["key"]: row["oos"] for row in rows}
    for row in rows:
        row["neighbor"] = neighbor_check(row["candidate"], oos_by_key)
        _add_oos_direction_check(row["oos"], baseline_oos)

    # 子样本只对已经通过 A/B/C/E 的候选运行；其他候选不可能合格，表中标记 SKIP。
    pre_subsample = [
        row
        for row in rows
        if row["oos"]["mdd"] > baseline_oos["bh_mdd"]
        and row["oos"]["total"] >= baseline_oos["total"]
        and row["neighbor"]["passed"]
        and row["oos"]["direction_ok"]
    ]
    print(f"running subsample checks for {len(pre_subsample)} candidates...")
    for index, row in enumerate(pre_subsample, 1):
        row["subsample"] = _subsample_for_candidate(
            row["candidate"], df, pe_map, windows
        )
        if index % 8 == 0 or index == len(pre_subsample):
            print(f"  subsample {index}/{len(pre_subsample)}")

    for row in rows:
        row.setdefault("subsample", None)
        _set_gate_flags(row, baseline_oos)

    # 排名主键只有 OOS total；其余键只用于稳定复现同收益时的顺序。
    rows.sort(
        key=lambda row: (
            row["oos"]["total"],
            row["oos"]["cash"]["total"],
            -abs(row["oos"]["mdd"]),
            tuple(-value if isinstance(value, (int, float)) else value for value in row["key"][:3]),
            bool(row["candidate"]["erp_cap"]),
        ),
        reverse=True,
    )

    _print_full_ranking(rows)

    global_best = rows[0]
    eligible = [row for row in rows if _is_qualified(row)]
    recommended = eligible[0] if eligible else None
    print(
        f"\nGate summary: eligible={len(eligible)}/{len(rows)}; "
        f"pre_subsample={len(pre_subsample)}; "
        f"global_best={format_candidate(global_best['candidate'])} "
        f"OOS={_pct(global_best['oos']['total'])}"
    )
    detail_row = recommended or global_best
    _print_gate_detail(detail_row, baseline_oos)

    comparison_row = recommended or global_best
    comparison_label = (
        "qualified OOS-return leader"
        if recommended is not None
        else "global OOS-return leader (not qualified)"
    )
    _print_three_way(comparison_row, comparison_label, production)

    anchor_key = (0.45, -0.25, -0.40, False)
    anchor = next(row for row in rows if row["key"] == anchor_key)
    _print_reference_cross_checks(production, production_off, anchor)
    _print_erp_group_summary(rows)
    _print_final_answer(eligible, global_best, production)


if __name__ == "__main__":
    main()
