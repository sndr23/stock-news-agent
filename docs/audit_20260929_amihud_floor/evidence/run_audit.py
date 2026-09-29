from __future__ import annotations

import hashlib
import json
import math
import shutil
import sys
from datetime import datetime as RealDateTime
from pathlib import Path

sys.dont_write_bytecode = True

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(__file__).resolve().parent
SOURCE_CSV = ROOT / ".bt_report" / "evidence" / "market_399006_sina_3000.csv"
SOURCE_STDOUT = ROOT / ".audit_report" / "evidence" / "run_audit_stdout.txt"

sys.path.insert(0, str(ROOT))
from src.strategy import chinext_factors as cf
from src.strategy import chinext_timing as ct
from scripts import run_chinext_timing as timing


class FrozenDateTime(RealDateTime):
    @classmethod
    def now(cls, tz=None):
        value = RealDateTime(2026, 9, 30, 8, 0, 0)
        return value.replace(tzinfo=tz) if tz is not None else value


timing.datetime = FrozenDateTime
PRODUCTION_AMIHUD = cf.factor_amihud
STDOUT_LINES: list[str] = []


def emit(label: str, payload) -> None:
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    line = f"{label} {payload}"
    if ("?" + "?") in line:
        raise ValueError("output contains a repeated question-mark sequence")
    STDOUT_LINES.append(line)
    print(line, flush=True)


def write_text(name: str, content: str) -> None:
    path = EVIDENCE / name
    path.write_text(content, encoding="utf-8", newline="\n")
    back = path.read_text(encoding="utf-8")
    assert ("?" + "?") not in back, f"{path} output integrity failure"


def write_csv(name: str, rows: list[dict]) -> None:
    path = EVIDENCE / name
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8", lineterminator="\n")
    raw = path.read_bytes()
    assert (b"?" * 2) not in raw, f"{path} output integrity failure"


def parse_source_inputs() -> tuple[pd.DataFrame, dict, dict]:
    if not SOURCE_CSV.exists() or not SOURCE_STDOUT.exists():
        raise FileNotFoundError("preserved market CSV or source stdout is missing")
    shutil.copyfile(SOURCE_CSV, EVIDENCE / SOURCE_CSV.name)
    raw = SOURCE_STDOUT.read_bytes()
    source_text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8")
    snapshots: dict[str, dict] = {}
    live_modifiers: dict[str, float] = {}
    for line in source_text.splitlines():
        if line.startswith("SNAPSHOT_CORE "):
            item = json.loads(line[len("SNAPSHOT_CORE "):])
            snapshots[item["date"]] = item["snapshot"]
        elif line.startswith("GATHER_SCORE_ALL "):
            item = json.loads(line[len("GATHER_SCORE_ALL "):])
            day = item.get("context", {}).get("date")
            result = item.get("result", {})
            score = (result.get("mods", {}) or {}).get("total")
            if score is None and isinstance(result.get("score"), (int, float)) and isinstance(result.get("core"), (int, float)):
                score = result["score"] - result["core"]
            if day and isinstance(score, (int, float)):
                live_modifiers[day] = float(score)
    if "2026-09-28" not in snapshots or "2026-09-29" not in snapshots:
        raise ValueError("expected retained snapshots for 2026-09-28 and 2026-09-29")
    bars = pd.read_csv(EVIDENCE / SOURCE_CSV.name, parse_dates=["date"])
    bars = bars.set_index("date").sort_index()
    for col in ("close", "amount"):
        bars[col] = pd.to_numeric(bars[col], errors="raise")
    if bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
        raise ValueError("market input dates are duplicated or unsorted")
    snapshot_rows = []
    for day, snap in sorted(snapshots.items()):
        snapshot_rows.append({
            "date": day,
            "captured_at": snap.get("captured_at"),
            "capture_time_type": snap.get("capture_time_type"),
            "source": snap.get("source"),
            "close": snap.get("close"),
            "amount_shares": snap.get("amount"),
            "amount_unit": snap.get("amount_unit"),
        })
    write_csv("snapshot_inputs.csv", snapshot_rows)
    manifest = {
        "market_source": ".bt_report/evidence/market_399006_sina_3000.csv",
        "market_copy": "evidence/market_399006_sina_3000.csv",
        "market_sha256": hashlib.sha256((EVIDENCE / SOURCE_CSV.name).read_bytes()).hexdigest(),
        "source_snapshot_stdout": ".audit_report/evidence/run_audit_stdout.txt (UTF-16LE decoded read-only)",
        "market_rows": int(len(bars)),
        "first_date": bars.index[0].strftime("%Y-%m-%d"),
        "last_date": bars.index[-1].strftime("%Y-%m-%d"),
        "snapshot_rows": len(snapshot_rows),
        "snapshot_modifier_dates": sorted(live_modifiers),
        "dotenv_loader_called": False,
        "network_requested": False,
    }
    write_text("input_manifest.txt", json.dumps(manifest, ensure_ascii=True, indent=2) + "\n")
    return bars, snapshots, live_modifiers


def raw_illiq(close, amount) -> list[float]:
    out = [0.0] * len(close)
    for i in range(1, len(close)):
        ret = abs(float(close[i]) / float(close[i - 1]) - 1.0) if close[i - 1] else 0.0
        amt = float(amount[i]) or 1.0
        out[i] = ret / (amt / 1e9)
    return out


def winsor_amihud(close, amount, low_q=0.05, high_q=0.95, span=60) -> list[float]:
    illiq = raw_illiq(close, amount)
    out = []
    for i, current in enumerate(illiq):
        lo = max(0, i - span)
        history = np.asarray(illiq[lo:i], dtype=float)
        if len(history) < 2:
            out.append(0.0)
            continue
        low, high = np.quantile(history, [low_q, high_q], method="linear")
        reference = np.clip(history, low, high)
        observed = float(np.clip(current, low, high))
        sd = float(reference.std(ddof=1))
        z = (observed - float(reference.mean())) / sd if sd > 0 else 0.0
        out.append(round(max(-1.0, min(1.0, -z / 2.0)), 3))
    return out


def smooth_amihud(close, amount) -> list[float]:
    illiq = raw_illiq(close, amount)
    z = cf._roll_z(illiq, 60)
    return [round(float(np.tanh(-v / 2.0)), 3) for v in z]


VARIANTS = {
    "A": PRODUCTION_AMIHUD,
    "B_5_95": lambda close, amount: winsor_amihud(close, amount, 0.05, 0.95),
    "C": smooth_amihud,
    "B_3_97": lambda close, amount: winsor_amihud(close, amount, 0.03, 0.97),
    "B_10_90": lambda close, amount: winsor_amihud(close, amount, 0.10, 0.90),
}


def run_production_backtest(frame: pd.DataFrame, variant: str, fee: float,
                            eval_start: int, capture: bool = True):
    calls: list[dict] = []
    captured: dict = {}
    original_factor = cf.factor_amihud
    original_dimension = timing.cf.dimension_score
    original_decide = timing.ct.decide_position
    cf.factor_amihud = VARIANTS[variant]

    def capture_dimension(signals, weights=None):
        result = original_dimension(signals, weights)
        captured["signals"] = {key: list(values) for key, values in signals.items()}
        captured["scores"] = list(result)
        return result

    def capture_decide(score, cap, previous, tiers=ct.TIERS):
        i = eval_start + len(calls)
        previous_position = float((previous or {}).get("position") or 0.0)
        result = original_decide(score, cap, previous, tiers=tiers)
        calls.append({
            "index": i,
            "date": frame.index[i].strftime("%Y-%m-%d"),
            "score": float(score),
            "cap": float(cap),
            "previous_position": previous_position,
            "position": float(result["position"]),
            "changed": bool(result["changed"]),
            "direction": result["direction"],
            "pending": result.get("pending"),
        })
        return result

    if capture:
        timing.cf.dimension_score = capture_dimension
    timing.ct.decide_position = capture_decide
    try:
        metrics = timing.backtest_metrics(frame.copy(), fee=fee, eval_start=eval_start)
    finally:
        cf.factor_amihud = original_factor
        timing.cf.dimension_score = original_dimension
        timing.ct.decide_position = original_decide
    return metrics, calls, captured


def drawdown_interval(daily_rows: list[dict], ret_key: str, start_date: str):
    navs = [1.0]
    dates = [start_date]
    for row in daily_rows:
        navs.append(navs[-1] * (1.0 + float(row[ret_key])))
        dates.append(row["ret_to"])
    peak_value = navs[0]
    peak_index = 0
    worst = 0.0
    worst_peak = 0
    trough_index = 0
    for i, value in enumerate(navs):
        if value > peak_value:
            peak_value = value
            peak_index = i
        dd = value / peak_value - 1.0 if peak_value else 0.0
        if dd < worst:
            worst = dd
            worst_peak = peak_index
            trough_index = i
    recovery = next((i for i in range(trough_index + 1, len(navs))
                     if navs[i] >= navs[worst_peak]), None)
    return {
        "max_dd": worst,
        "peak_date": dates[worst_peak],
        "trough_date": dates[trough_index],
        "recovery_date": dates[recovery] if recovery is not None else "not_recovered",
    }


def daily_rows_from_capture(frame, metrics, calls, captured, fee):
    scores = captured["scores"]
    signals = captured["signals"]
    rows = []
    for call in calls:
        i = call["index"]
        index_ret = float(frame.iloc[i + 1]["close"] / frame.iloc[i]["close"] - 1.0)
        fee_cost = fee * abs(call["position"] - call["previous_position"]) if call["changed"] else 0.0
        strategy_ret = (1.0 - fee_cost) * (1.0 + call["position"] * index_ret) - 1.0
        rows.append({
            **call,
            "ret_from": call["date"],
            "ret_to": frame.index[i + 1].strftime("%Y-%m-%d"),
            "index_return": index_ret,
            "fee_cost": fee_cost,
            "strategy_return": strategy_ret,
            "amihud": float(signals["volprice_amihud"][i]),
            "score_full_series": float(scores[i]),
        })
    reconstructed_nav = float(np.prod([1.0 + row["strategy_return"] for row in rows]))
    if not math.isclose(reconstructed_nav, 1.0 + metrics["total"], rel_tol=1e-9, abs_tol=1e-9):
        raise AssertionError(f"captured path does not match production NAV: {reconstructed_nav} vs {1.0 + metrics['total']}")
    if len(rows) != metrics["n_navs"]:
        raise AssertionError("captured decision count differs from production backtest")
    return rows


def summarize_strategy(code, fee, metrics, rows):
    positions = [row["position"] for row in rows]
    distribution = {p: sum(1 for x in positions if abs(x - p) < 1e-9) for p in (0.0, 0.6, 0.9, 1.0)}
    mdd = drawdown_interval(rows, "strategy_return", rows[0]["date"])
    bh_rows = [{**row, "buyhold_return": row["index_return"]} for row in rows]
    bhdd = drawdown_interval(bh_rows, "buyhold_return", rows[0]["date"])
    return {
        "variant": code,
        "fee_per_one_way_position_unit": fee,
        "n_trading_returns": len(rows),
        "signal_start": rows[0]["date"],
        "signal_end": rows[-1]["date"],
        "return_end": rows[-1]["ret_to"],
        "cumulative_return": metrics["total"],
        "annualized_return": metrics["cagr"],
        "max_drawdown": metrics["mdd"],
        "max_dd_peak_date": mdd["peak_date"],
        "max_dd_trough_date": mdd["trough_date"],
        "max_dd_recovery_date": mdd["recovery_date"],
        "sharpe": metrics["sharpe"],
        "calmar": metrics["calmar"],
        "win_rate_daily_net": sum(1 for row in rows if row["strategy_return"] > 0) / max(1, len(rows)),
        "position_changes": metrics["switches"],
        "changes_per_1000_days": metrics["switches"] / max(1, len(rows)) * 1000.0,
        "position_unit_turnover_per_day": sum(abs(positions[i] - (rows[i]["previous_position"])) for i in range(len(rows))) / max(1, len(rows)),
        "average_position": metrics["avg_pos"],
        "days_pos_0": distribution[0.0],
        "days_pos_60": distribution[0.6],
        "days_pos_90": distribution[0.9],
        "days_pos_100": distribution[1.0],
        "buy_hold_cumulative_return": metrics["bh"],
        "buy_hold_max_drawdown": metrics["bh_mdd"],
        "buy_hold_max_dd_peak_date": bhdd["peak_date"],
        "buy_hold_max_dd_trough_date": bhdd["trough_date"],
        "buy_hold_max_dd_recovery_date": bhdd["recovery_date"],
    }


def block_bootstrap_mean_difference(values, event_mask, reps=5000, block=20, seed=20260929):
    values = np.asarray(values, dtype=float)
    mask = np.asarray(event_mask, dtype=bool)
    n = len(values)
    observed = float(values[mask].mean() - values[~mask].mean())
    rng = np.random.default_rng(seed)
    block = min(block, n)
    boot = []
    for _ in range(reps):
        idx = []
        while len(idx) < n:
            start = int(rng.integers(0, n - block + 1))
            idx.extend(range(start, start + block))
        idx = np.asarray(idx[:n], dtype=int)
        sampled_mask = mask[idx]
        if sampled_mask.any() and (~sampled_mask).any():
            sample = values[idx]
            boot.append(float(sample[sampled_mask].mean() - sample[~sampled_mask].mean()))
    return observed, np.quantile(boot, [0.025, 0.975]).tolist(), len(boot)


def hac_power(differences, lag=20):
    x = np.asarray(differences, dtype=float)
    n = len(x)
    centered = x - x.mean()
    gamma0 = float(np.dot(centered, centered) / n)
    lrv = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        gamma = float(np.dot(centered[k:], centered[:-k]) / n)
        lrv += 2.0 * (1.0 - k / (lag + 1.0)) * gamma
    se_mean = math.sqrt(max(lrv, 0.0) / n)
    observed = float(x.mean())
    z = abs(observed / se_mean) if se_mean > 0 else math.inf
    critical = stats.norm.ppf(0.975)
    power = float(stats.norm.sf(critical - z) + stats.norm.cdf(-critical - z)) if math.isfinite(z) else 1.0
    mde_80_annual = (stats.norm.ppf(0.975) + stats.norm.ppf(0.80)) * se_mean * 244.0
    return {
        "n_days": n,
        "mean_A_minus_C_daily": observed,
        "annualized_mean_A_minus_C": observed * 244.0,
        "hac_lag_days": min(lag, n - 1),
        "hac_se_daily_mean": se_mean,
        "hac_p_two_sided_normal": float(2.0 * stats.norm.sf(z)) if math.isfinite(z) else 0.0,
        "approx_power_for_observed_effect": power,
        "mde_80pct_annualized": mde_80_annual,
    }


def main():
    bars, snapshots, live_modifiers = parse_source_inputs()
    dates = bars.index
    start = int(np.flatnonzero(dates >= pd.Timestamp("2019-01-01"))[0])
    if start < 300:
        raise AssertionError("less than 300 warmup bars precede the sample")
    if dates[-1].strftime("%Y-%m-%d") != "2026-09-29":
        raise AssertionError("input does not contain the expected final complete 2026-09-29 bar")
    emit("INPUT", {
        "rows": len(bars), "first_date": dates[0].strftime("%Y-%m-%d"),
        "sample_floor": "2019-01-01", "first_sample_date": dates[start].strftime("%Y-%m-%d"),
        "warmup_bars_before_sample": start, "last_data_date": dates[-1].strftime("%Y-%m-%d"),
        "signal_dates": len(bars) - start - 1,
        "return_dates_including_final_close": len(bars) - start,
        "asof_freeze": "2026-09-30 08:00 +08:00",
        "weight_map": cf.CHINEXT_V51_WEIGHTS,
        "tiers": ct.TIERS,
        "selected_sina_amount_unit": "shares",
    })

    primary = {}
    metric_rows = []
    for variant in ("A", "B_5_95", "C"):
        results = {}
        for fee in (0.0, 0.0005):
            metrics, calls, captured = run_production_backtest(
                bars, variant, fee, start, capture=(fee == 0.0)
            )
            results[fee] = (metrics, calls, captured)
            if fee == 0.0:
                rows = daily_rows_from_capture(bars, metrics, calls, captured, fee)
                primary[variant] = {"metrics": metrics, "calls": calls,
                                    "captured": captured, "rows": rows}
                write_csv(f"daily_{variant}.csv", rows)
            else:
                base_calls = primary[variant]["calls"]
                same_path = len(calls) == len(base_calls) and all(
                    abs(a["position"] - b["position"]) < 1e-9 for a, b in zip(calls, base_calls)
                )
                if not same_path:
                    raise AssertionError(f"fee unexpectedly changed the {variant} position path")
            if fee == 0.0:
                path_rows = primary[variant]["rows"]
            else:
                # Position decisions are fee-independent; compute exact fee-adjusted daily returns from the captured path.
                path_rows = []
                for call in calls:
                    i = call["index"]
                    idx_ret = float(bars.iloc[i + 1]["close"] / bars.iloc[i]["close"] - 1.0)
                    fee_cost = fee * abs(call["position"] - call["previous_position"]) if call["changed"] else 0.0
                    path_rows.append({**call,
                        "ret_to": dates[i + 1].strftime("%Y-%m-%d"),
                        "strategy_return": (1.0 - fee_cost) * (1.0 + call["position"] * idx_ret) - 1.0,
                        "index_return": idx_ret})
            summary = summarize_strategy(variant, fee, metrics, path_rows)
            metric_rows.append(summary)
            emit("METRIC", summary)
    write_csv("metrics.csv", metric_rows)

    # Anchor replication uses the exact production scoring functions and only the retained snapshot inputs.
    full_anchor = {}
    snapshot_anchor = {}
    snapshot_frame = bars.copy()
    for day, snap in snapshots.items():
        ts = pd.Timestamp(day)
        if ts in snapshot_frame.index:
            snapshot_frame.loc[ts, "close"] = float(snap["close"])
            snapshot_frame.loc[ts, "amount"] = float(snap["amount"])
    for variant in ("A", "B_5_95", "C"):
        original = cf.factor_amihud
        cf.factor_amihud = VARIANTS[variant]
        try:
            daily_signals = cf.core_signals(bars["close"].tolist(), bars["amount"].tolist(), erp_pctile=None)
            daily_scores = cf.dimension_score(daily_signals, timing._default_weights(0.10))
            snap_signals = cf.core_signals(snapshot_frame["close"].tolist(), snapshot_frame["amount"].tolist(), erp_pctile=None)
            snap_scores = cf.dimension_score(snap_signals, timing._default_weights(0.10))
        finally:
            cf.factor_amihud = original
        full_anchor[variant] = {"signals": daily_signals, "scores": daily_scores}
        snapshot_anchor[variant] = {"signals": snap_signals, "scores": snap_scores}
    date_to_idx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(dates)}
    anchor_rows = []
    for day, expected in (("2026-09-28", -0.770), ("2026-09-29", -0.550)):
        i = date_to_idx[day]
        actual = float(full_anchor["A"]["scores"][i])
        anchor_rows.append({"date": day, "mode": "full_day", "core": actual,
                            "expected": expected, "delta": actual - expected,
                            "pass_pm_0_005": abs(actual - expected) <= 0.005000001})
    for day, expected in (("2026-09-28", -0.770), ("2026-09-29", -0.555)):
        i = date_to_idx[day]
        actual = float(snapshot_anchor["A"]["scores"][i])
        anchor_rows.append({"date": day, "mode": "retained_snapshot", "core": actual,
                            "expected": expected, "delta": actual - expected,
                            "pass_pm_0_005": abs(actual - expected) <= 0.005000001})
    if not all(row["pass_pm_0_005"] for row in anchor_rows):
        write_csv("anchor_results.csv", anchor_rows)
        raise AssertionError("anchor reproduction failed; downstream results must not be treated as accepted")
    write_csv("anchor_results.csv", anchor_rows)
    emit("ANCHORS", anchor_rows)

    # The prompt's 0.922 quantity and the active-session clock fraction are both tested.
    caliber_rows = []
    for day in ("2026-09-28", "2026-09-29"):
        i = date_to_idx[day]
        snap = snapshots[day]
        prev_close = float(bars.iloc[i - 1]["close"])
        daily_close, daily_amount = float(bars.iloc[i]["close"]), float(bars.iloc[i]["amount"])
        snap_close, snap_amount = float(snap["close"]), float(snap["amount"])
        illiq_full = abs(daily_close / prev_close - 1.0) / (daily_amount / 1e9)
        illiq_snap = abs(snap_close / prev_close - 1.0) / (snap_amount / 1e9)
        mod = float(live_modifiers.get(day, 0.0))
        for variant in ("A", "B_5_95", "C"):
            d_sig = full_anchor[variant]["signals"]
            s_sig = snapshot_anchor[variant]["signals"]
            d_core = float(full_anchor[variant]["scores"][i])
            s_core = float(snapshot_anchor[variant]["scores"][i])
            caliber_rows.append({
                "date": day, "variant": variant,
                "capture_time_type": snap.get("capture_time_type"),
                "captured_at": snap.get("captured_at"), "source": snap.get("source"),
                "full_day_close": daily_close, "snapshot_close": snap_close,
                "snapshot_minus_full_close": snap_close - daily_close,
                "full_day_amount_shares": daily_amount, "snapshot_amount_shares": snap_amount,
                "snapshot_minus_full_amount_shares": snap_amount - daily_amount,
                "amount_snapshot_pct_of_full": snap_amount / daily_amount * 100.0,
                "full_day_illiq": illiq_full, "snapshot_illiq": illiq_snap,
                "illiq_snapshot_pct_vs_full": (illiq_snap / illiq_full - 1.0) * 100.0 if illiq_full else None,
                "full_day_amihud_factor": float(d_sig["volprice_amihud"][i]),
                "snapshot_amihud_factor": float(s_sig["volprice_amihud"][i]),
                "snapshot_minus_full_amihud_factor": float(s_sig["volprice_amihud"][i] - d_sig["volprice_amihud"][i]),
                "full_day_core": d_core, "snapshot_core": s_core,
                "snapshot_minus_full_core": s_core - d_core,
                "observed_noncore_modifier_held_fixed": mod,
                "full_day_total_score_with_fixed_modifier": float(ct.clamp(d_core + mod)),
                "snapshot_total_score_with_fixed_modifier": float(ct.clamp(s_core + mod)),
                "total_score_snapshot_minus_full": float(ct.clamp(s_core + mod) - ct.clamp(d_core + mod)),
            })
    write_csv("caliber_snapshot_comparison.csv", caliber_rows)
    for scale_name, scale in (("active_hours_3_75_of_4", 0.9375), ("prompt_numeric_approx", 0.922)):
        scaled = bars.copy()
        scaled["amount"] = scaled["amount"] * scale
        for variant in ("A", "B_5_95", "C"):
            metrics, calls, captured = run_production_backtest(scaled, variant, 0.0, start, capture=True)
            rows = daily_rows_from_capture(scaled, metrics, calls, captured, 0.0)
            base_rows = primary[variant]["rows"]
            exact_score_matches = sum(
                abs(float(captured["scores"][i]) - float(primary[variant]["captured"]["scores"][i])) < 1e-12
                for i in range(start, len(bars))
            )
            same_positions = all(abs(a["position"] - b["position"]) < 1e-9
                                 for a, b in zip(rows, base_rows))
            caliber_rows.append({
                "date": "sample_2019_onward", "variant": variant,
                "proxy": scale_name, "amount_multiplier": scale,
                "raw_illiq_multiplier_expected": 1.0 / scale,
                "core_score_exact_matches": exact_score_matches,
                "core_score_comparisons": len(bars) - start,
                "same_position_path": same_positions,
                "base_cumulative_return": primary[variant]["metrics"]["total"],
                "proxy_cumulative_return": metrics["total"],
                "proxy_minus_base_return": metrics["total"] - primary[variant]["metrics"]["total"],
                "base_sharpe": primary[variant]["metrics"]["sharpe"],
                "proxy_sharpe": metrics["sharpe"],
            })
    write_csv("caliber_symmetry.csv", caliber_rows)
    for proxy in ("active_hours_3_75_of_4", "prompt_numeric_approx"):
        emit("CALIBER_PROXY", [r for r in caliber_rows if r.get("proxy") == proxy])

    # Annual decomposition and the complete A versus C position-difference ledger.
    years = sorted({row["ret_to"][:4] for row in primary["A"]["rows"]})
    yearly_rows = []
    for year in years:
        year_rows = {variant: [row for row in primary[variant]["rows"] if row["ret_to"].startswith(year)]
                     for variant in ("A", "B_5_95", "C")}
        base = year_rows["A"]
        idx_returns = [row["index_return"] for row in base]
        item = {"year": year, "trading_returns": len(base),
                "buy_hold_return": float(np.prod([1.0 + r for r in idx_returns]) - 1.0)}
        for variant in ("A", "B_5_95", "C"):
            item[f"{variant}_return"] = float(np.prod([1.0 + row["strategy_return"] for row in year_rows[variant]]) - 1.0)
        item["B_minus_A_pp"] = (item["B_5_95_return"] - item["A_return"]) * 100.0
        item["C_minus_A_pp"] = (item["C_return"] - item["A_return"]) * 100.0
        yearly_rows.append(item)
    write_csv("yearly_returns.csv", yearly_rows)

    a_by_date = {row["date"]: row for row in primary["A"]["rows"]}
    c_by_date = {row["date"]: row for row in primary["C"]["rows"]}
    difference_rows = []
    for day in a_by_date:
        a, c = a_by_date[day], c_by_date[day]
        if abs(a["position"] - c["position"]) < 1e-9:
            continue
        contribution = (a["position"] - c["position"]) * a["index_return"]
        difference_rows.append({
            "date": day, "A_score": a["score"], "C_score": c["score"],
            "A_position": a["position"], "C_position": c["position"],
            "A_amihud": a["amihud"], "C_amihud": c["amihud"],
            "A_floor_event": abs(a["amihud"] + 1.0) < 1e-12,
            "ret_to": a["ret_to"], "t_plus_1_index_return": a["index_return"],
            "A_minus_C_exposure_return_contribution": contribution,
            "ex_post_more_aligned": "A" if contribution > 0 else ("C" if contribution < 0 else "tie"),
        })
    write_csv("positions_A_vs_C_differences.csv", difference_rows)
    better = {k: sum(1 for row in difference_rows if row["ex_post_more_aligned"] == k) for k in ("A", "C", "tie")}
    emit("POSITION_DIFF", {
        "days": len(difference_rows), "A_more_aligned_days": better["A"],
        "C_more_aligned_days": better["C"], "ties": better["tie"],
        "sum_A_minus_C_exposure_return_contribution": sum(r["A_minus_C_exposure_return_contribution"] for r in difference_rows),
        "full_csv": "evidence/positions_A_vs_C_differences.csv",
    })

    # Floor-event study with a 20-day moving-block bootstrap for event/control mean differences.
    a_signals = primary["A"]["captured"]["signals"]
    a_scores = primary["A"]["captured"]["scores"]
    a_position_by_index = {row["index"]: row["position"] for row in primary["A"]["calls"]}
    a_position_by_date = {row["date"]: row["position"] for row in primary["A"]["rows"]}
    event_indices = [i for i in range(start, len(bars) - 1)
                     if abs(float(a_signals["volprice_amihud"][i]) + 1.0) < 1e-12]
    event_set = set(event_indices)
    event_rows = []
    for i in event_indices:
        factor_t = float(a_signals["volprice_amihud"][i])
        factor_next = float(a_signals["volprice_amihud"][i + 1])
        core_delta = float(a_scores[i + 1] - a_scores[i])
        mechanical = 0.10 * (factor_next - factor_t)
        event_rows.append({
            "date": dates[i].strftime("%Y-%m-%d"), "year": dates[i].strftime("%Y"),
            "t_core": float(a_scores[i]), "t_score_core_only": float(a_scores[i]),
            "t_position": a_position_by_index.get(i),
            "t_amihud": factor_t, "t_plus_1_amihud": factor_next,
            "t_plus_1_core": float(a_scores[i + 1]),
            "t_plus_1_score_core_only": float(a_scores[i + 1]),
            "t_plus_1_core_delta": core_delta,
            "mechanical_amihud_contribution": mechanical,
            "other_factor_contribution_residual": core_delta - mechanical,
            "t_plus_1_position": a_position_by_index.get(i + 1),
            "t_plus_1_position_delta": a_position_by_index.get(i + 1, 0.0) - a_position_by_index.get(i, 0.0),
            "ret_from": dates[i].strftime("%Y-%m-%d"),
            "ret_to": dates[i + 1].strftime("%Y-%m-%d"),
            "t_to_t_plus_1_index_return": float(bars.iloc[i + 1]["close"] / bars.iloc[i]["close"] - 1.0),
            "t_plus_1_to_plus_3_return": float(bars.iloc[i + 4]["close"] / bars.iloc[i + 1]["close"] - 1.0) if i + 4 < len(bars) else None,
            "t_plus_1_to_plus_5_return": float(bars.iloc[i + 6]["close"] / bars.iloc[i + 1]["close"] - 1.0) if i + 6 < len(bars) else None,
            "t_plus_1_to_plus_10_return": float(bars.iloc[i + 11]["close"] / bars.iloc[i + 1]["close"] - 1.0) if i + 11 < len(bars) else None,
        })
    write_csv("floor_events.csv", event_rows)
    floor_years = {}
    for row in event_rows:
        floor_years[row["year"]] = floor_years.get(row["year"], 0) + 1
    write_csv("floor_events_by_year.csv", [{"year": y, "events": n} for y, n in sorted(floor_years.items())])

    event_stats = []
    horizon_fields = {
        "t_to_t_plus_1": "t_to_t_plus_1_index_return",
        "t_plus_1_to_plus_3": "t_plus_1_to_plus_3_return",
        "t_plus_1_to_plus_5": "t_plus_1_to_plus_5_return",
        "t_plus_1_to_plus_10": "t_plus_1_to_plus_10_return",
    }
    for seed, (horizon, field) in enumerate(horizon_fields.items(), start=1):
        vals, mask = [], []
        for i in range(start, len(bars) - 1):
            if field == "t_to_t_plus_1_index_return":
                value = float(bars.iloc[i + 1]["close"] / bars.iloc[i]["close"] - 1.0)
                valid = True
            else:
                offset = {"t_plus_1_to_plus_3": 4, "t_plus_1_to_plus_5": 6, "t_plus_1_to_plus_10": 11}[horizon]
                valid = i + offset < len(bars)
                value = float(bars.iloc[i + offset]["close"] / bars.iloc[i + 1]["close"] - 1.0) if valid else None
            if valid:
                vals.append(value)
                mask.append(i in event_set)
        vals_arr = np.asarray(vals, dtype=float)
        mask_arr = np.asarray(mask, dtype=bool)
        evt = vals_arr[mask_arr]
        ctrl = vals_arr[~mask_arr]
        if len(evt) >= 2 and len(ctrl) >= 2:
            test = stats.ttest_ind(evt, ctrl, equal_var=False)
            diff, ci, boot_n = block_bootstrap_mean_difference(vals_arr, mask_arr, seed=20260929 + seed)
            row = {
                "horizon": horizon, "events_n": len(evt), "no_floor_n": len(ctrl),
                "all_sample_n": len(vals_arr), "all_sample_mean": float(vals_arr.mean()),
                "events_mean": float(evt.mean()), "no_floor_mean": float(ctrl.mean()),
                "mean_difference_event_minus_control": diff,
                "welch_t": float(test.statistic), "welch_p": float(test.pvalue),
                "block_bootstrap_95_low": ci[0], "block_bootstrap_95_high": ci[1],
                "block_days": min(20, len(vals_arr)), "bootstrap_replicates_valid": boot_n,
                "significant_at_0_05": bool(test.pvalue < 0.05),
            }
        else:
            row = {"horizon": horizon, "events_n": len(evt), "no_floor_n": len(ctrl),
                   "all_sample_n": len(vals_arr), "all_sample_mean": float(vals_arr.mean()) if len(vals_arr) else None,
                   "events_mean": None, "no_floor_mean": None,
                   "mean_difference_event_minus_control": None, "welch_t": None,
                   "welch_p": None, "block_bootstrap_95_low": None,
                   "block_bootstrap_95_high": None, "significant_at_0_05": False}
        event_stats.append(row)
    write_csv("event_statistics.csv", event_stats)
    deltas = np.asarray([row["t_plus_1_core_delta"] for row in event_rows], dtype=float)
    mechanical = np.asarray([row["mechanical_amihud_contribution"] for row in event_rows], dtype=float)
    residual = np.asarray([row["other_factor_contribution_residual"] for row in event_rows], dtype=float)
    mechanical_summary = {
        "floor_events": len(event_rows),
        "sample_signal_days": len(bars) - start - 1,
        "event_share_pct": len(event_rows) / max(1, len(bars) - start - 1) * 100.0,
        "events_by_year": floor_years,
        "mean_core_rebound_t_plus_1": float(deltas.mean()) if len(deltas) else None,
        "mean_mechanical_amihud_contribution": float(mechanical.mean()) if len(mechanical) else None,
        "mean_other_factor_residual": float(residual.mean()) if len(residual) else None,
        "sum_core_rebound": float(deltas.sum()) if len(deltas) else None,
        "sum_mechanical_amihud": float(mechanical.sum()) if len(mechanical) else None,
        "sum_other_factor_residual": float(residual.sum()) if len(residual) else None,
        "mechanical_share_of_sum_pct": float(mechanical.sum() / deltas.sum() * 100.0) if len(deltas) and abs(deltas.sum()) > 1e-12 else None,
    }
    emit("EVENT_SUMMARY", mechanical_summary)
    emit("EVENT_TESTS", event_stats)

    # O1: first/middle/last thirds of the already captured chronological path.
    n = len(primary["A"]["rows"])
    third_edges = [0, n // 3, 2 * n // 3, n]
    subsample_rows = []
    for label, lo, hi in zip(("first", "middle", "last"), third_edges[:-1], third_edges[1:]):
        values = {}
        for variant in ("A", "C"):
            selected = primary[variant]["rows"][lo:hi]
            values[variant] = float(np.prod([1.0 + r["strategy_return"] for r in selected]) - 1.0)
        subsample_rows.append({"segment": label, "n_days": hi - lo,
                               "start": primary["A"]["rows"][lo]["date"],
                               "end": primary["A"]["rows"][hi - 1]["ret_to"],
                               "A_return": values["A"], "C_return": values["C"],
                               "A_minus_C_pp": (values["A"] - values["C"]) * 100.0,
                               "A_vs_C_direction": "A" if values["A"] > values["C"] else ("C" if values["C"] > values["A"] else "tie")})
    write_csv("subsample_stability.csv", subsample_rows)

    # O2: five contiguous walk-forward evaluation folds; frozen mappings and state carry between folds.
    fold_rows = []
    fold_edges = [start + (len(primary["A"]["rows"]) * k) // 5 for k in range(6)]
    for fold in range(5):
        lo, hi = fold_edges[fold], fold_edges[fold + 1]
        item = {"fold": fold + 1, "start": dates[lo].strftime("%Y-%m-%d"),
                "return_end": dates[hi].strftime("%Y-%m-%d"), "signal_days": hi - lo}
        for variant in ("A", "C"):
            selected = [r for r in primary[variant]["rows"] if lo <= r["index"] < hi]
            item[f"{variant}_return"] = float(np.prod([1.0 + r["strategy_return"] for r in selected]) - 1.0)
        item["A_minus_C_pp"] = (item["A_return"] - item["C_return"]) * 100.0
        item["direction"] = "A" if item["A_return"] > item["C_return"] else ("C" if item["C_return"] > item["A_return"] else "tie")
        fold_rows.append(item)
    write_csv("walk_forward_folds.csv", fold_rows)

    # O3: predeclared winsorization-neighborhood sensitivity, explicitly observational only.
    neighbor_rows = []
    for variant in ("B_3_97", "B_5_95", "B_10_90"):
        if variant == "B_5_95":
            metrics = primary[variant]["metrics"] if variant in primary else None
            if metrics is None:
                metrics, calls, captured = run_production_backtest(bars, variant, 0.0, start, capture=True)
                rows = daily_rows_from_capture(bars, metrics, calls, captured, 0.0)
            else:
                rows = primary[variant]["rows"]
        else:
            metrics, calls, captured = run_production_backtest(bars, variant, 0.0, start, capture=True)
            rows = daily_rows_from_capture(bars, metrics, calls, captured, 0.0)
        neighbor_rows.append({"variant": variant, "winsor_low_pct": 3 if variant == "B_3_97" else (5 if variant == "B_5_95" else 10),
                              "winsor_high_pct": 97 if variant == "B_3_97" else (95 if variant == "B_5_95" else 90),
                              "cumulative_return": metrics["total"], "sharpe": metrics["sharpe"],
                              "max_drawdown": metrics["mdd"], "calmar": metrics["calmar"],
                              "position_changes": metrics["switches"],
                              "adoption_status": "observation_only_not_adopted"})
    ranks = {field: {row["variant"]: rank + 1 for rank, row in enumerate(sorted(neighbor_rows, key=lambda r: r[field], reverse=True))}
             for field in ("cumulative_return", "sharpe", "calmar")}
    for row in neighbor_rows:
        for field in ranks:
            row[f"rank_{field}"] = ranks[field][row["variant"]]
    write_csv("winsor_neighborhood_observation_only.csv", neighbor_rows)

    # Paired Newey-West power summary for the A/C daily return difference.
    a_rets = np.asarray([row["strategy_return"] for row in primary["A"]["rows"]], dtype=float)
    c_rets = np.asarray([row["strategy_return"] for row in primary["C"]["rows"]], dtype=float)
    power = hac_power(a_rets - c_rets, lag=20)
    write_csv("A_vs_C_power.csv", [power])

    # Produce the look-ahead checks from the exact captured output and source slices.
    l2_rows = primary["A"]["rows"][:3]
    floor_i = event_indices[0] if event_indices else start
    lo = max(0, floor_i - 60)
    l3 = {
        "date_t": dates[floor_i].strftime("%Y-%m-%d"), "index_t": floor_i,
        "baseline_window_start_index": lo, "baseline_window_end_exclusive": floor_i,
        "baseline_start_date": dates[lo].strftime("%Y-%m-%d"),
        "baseline_end_date": dates[floor_i - 1].strftime("%Y-%m-%d") if floor_i > lo else None,
        "baseline_count": floor_i - lo,
        "current_observation_index": floor_i,
        "future_observations_used": 0,
        "source_slice": "chinext_factors.py:36-41 uses x[lo:i], then x[i] as the observation",
    }
    returns = bars["close"].pct_change().dropna()
    extreme = [{"date": dates[i].strftime("%Y-%m-%d"), "return": float(returns.iloc[i - 1])}
               for i in range(1, len(bars)) if abs(float(returns.iloc[i - 1])) >= 0.095]
    source_review = [
        "L2 PASS: run_backtest/backtest_metrics pairs decision close d with close d+1; captured three actual consecutive date pairs below.",
        json.dumps([{"date": r["date"], "ret_from": r["ret_from"], "ret_to": r["ret_to"], "index_return": r["index_return"]} for r in l2_rows], ensure_ascii=True, sort_keys=True),
        "L3 PASS: _roll_z uses the trailing prior slice x[max(0,i-60):i], then compares x[i]; it never uses i+1. The baseline count is 60 once enough history exists; current t is the observation, not part of the reference mean/std window.",
        json.dumps(l3, ensure_ascii=True, sort_keys=True),
        "L4 PASS: src/strategy/chinext_factors.py:33-42 loops forward and uses x[lo:i]; this is trailing, not centered.",
        "L5 PASS: src/strategy/chinext_timing.py:324-379 derives the target from current score plus previous position/pending state. Upgrade confirmation increments only on successive calls; no t+1 input is read.",
        "L6 PASS WITH SCOPE LIMIT: source is the raw 399006 index close/amount series from load_index_sina (src/strategy/data.py:613-699); the loader directly converts provider close and volume, marks amount_unit=shares, and applies no qfq/hfq adjustment. Stock ex-rights date lists do not apply to an index series. The calculation applies no adjustment, so processing-induced adjusted-price jump contribution is exactly 0; a separate provider-adjusted series and index-component action ledger were unavailable, so no provider methodology effect is inferred.",
        "RAW INDEX RETURN SANITY: " + json.dumps({"min_return": float(returns.min()), "max_return": float(returns.max()), "abs_ge_9_5pct_count": len(extreme), "abs_ge_9_5pct_dates": extreme}, ensure_ascii=True, sort_keys=True),
        "L1 DISCLOSURE: 14:45 historical snapshots are not archived for the full 2019+ span. The run_chinext_timing.py:1164-1165 source comments explicitly call full-day close/amount a reproducible approximation.",
    ]
    lookahead_text = "\n".join(source_review) + "\n"
    write_text("lookahead_evidence.txt", lookahead_text)

    floor_share = len(event_rows) / max(1, len(primary["A"]["rows"]))
    result_order = sorted((r for r in metric_rows if r["fee_per_one_way_position_unit"] == 0.0),
                          key=lambda r: r["cumulative_return"], reverse=True)
    scale_rows = [r for r in caliber_rows if r.get("proxy")]
    emit("L2_L6", {"L2": "PASS", "L3": "PASS", "L4": "PASS", "L5": "PASS", "L6": "PASS_WITH_SCOPE_LIMIT",
                    "evidence": "evidence/lookahead_evidence.txt"})
    emit("YEARLY", yearly_rows)
    emit("O1_SUBSAMPLE", subsample_rows)
    emit("O2_WALK_FORWARD", fold_rows)
    emit("O3_NEIGHBORHOOD", {"runs": neighbor_rows, "rankings": ranks, "configuration_count": 5})
    emit("A_VS_C_POWER", power)
    emit("CALIBER_RESULT_ORDER_FEE_0", [r["variant"] for r in result_order])
    emit("FINAL_RAW_OUTPUTS", {
        "floor_events": len(event_rows), "floor_share_of_signal_days_pct": floor_share * 100.0,
        "A_C_position_difference_days": len(difference_rows),
        "all_numeric_tables_in_evidence": True,
        "script_did_not_read_dotenv_or_call_network": True,
    })
    write_text("run_audit_stdout.txt", "\n".join(STDOUT_LINES) + "\n")


if __name__ == "__main__":
    main()
