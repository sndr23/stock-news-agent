# -*- coding: utf-8 -*-
"""V6 研究入口：因子 IC、仓位映射与真实快照回放。

该脚本只读行情/快照输入，不调用生产推送和状态写入。默认报告同时给出
当前 v5.1 的官方基线和连续仓位映射实验；实验结果不会修改生产参数。

示例：
  python scripts/run_v6_research.py
  python scripts/run_v6_research.py --snapshot-file logs/snapshots.json
  python scripts/run_v6_research.py --synthetic
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_intraday_snapshots import (  # noqa: E402
    load_snapshot_payload,
)
from scripts.run_chinext_timing import (  # noqa: E402
    backtest_metrics,
    load_index_daily_full,
    load_index_sina,
)
from scripts.walk_forward_validation import synthetic_df  # noqa: E402
from src.strategy import chinext_factors as cf  # noqa: E402
from src.strategy import chinext_timing as ct  # noqa: E402
from src.strategy import index_pe as ipe  # noqa: E402
from src.strategy.intraday_replay import replay_snapshot_backtest  # noqa: E402
from src.strategy.research import (  # noqa: E402
    TRADING_DAYS,
    evaluate_position_path,
    factor_ic_report,
    performance_metrics,
    score_to_continuous_position,
)

BJT = timezone(timedelta(hours=8))
FACTOR_LABELS = {
    "trend_ma20_60": "趋势·均线",
    "trend_momentum_60": "趋势·动量",
    "volprice_quadrant": "量价·四象限",
    "volprice_amihud": "量价·Amihud",
    "vol_regime": "波动·状态",
    "vol_term": "波动·期限",
    "value_erp": "估值·ERP",
    "pullback_52w": "落袋·52周回撤",
    "dd60": "落袋·60日回撤",
}


def load_daily_bars(path: str | Path | None = None,
                    synthetic: bool = False) -> pd.DataFrame:
    """加载 399006 完整日线；CSV 与合成数据仅用于研究。"""
    if synthetic:
        return synthetic_df()
    if path:
        frame = pd.read_csv(path)
        date_col = "date" if "date" in frame.columns else frame.columns[0]
        frame.index = pd.to_datetime(frame.pop(date_col), errors="coerce")
        return frame
    frame = load_index_sina("399006", datalen=3001)
    if frame is None or frame.empty:
        frame = load_index_daily_full("399006", "20200101")
    if frame is None or frame.empty:
        raise ValueError("399006 完整日线获取失败")
    return frame


def _erp_series(daily: pd.DataFrame, pe_map: dict | None) -> list | None:
    if not pe_map:
        return None
    dates = [pd.Timestamp(value).strftime("%Y-%m-%d") for value in daily.index]
    pe = ipe.align_pe_by_dates(pe_map, dates)
    return ipe.pe_to_cheap_pctile(pe, 500)


def build_position_paths(
        daily: pd.DataFrame,
        pe_map: dict | None = None,
        erp_cap: bool = False,
        weights: dict | None = None,
        tiers: tuple = ct.TIERS,
        start: int = 60,
        end: int | None = None) -> dict:
    """用同一分数构造生产分档和连续映射两条研究仓位路径。"""
    if not isinstance(daily, pd.DataFrame) or daily.empty:
        raise ValueError("daily 不能为空")
    if "close" not in daily or "amount" not in daily:
        raise ValueError("daily 必须包含 close/amount")
    closes = [float(value) for value in daily["close"].tolist()]
    amounts = [float(value) for value in daily["amount"].tolist()]
    n = len(closes)
    end = n - 1 if end is None else int(end)
    if n < start + 2 or start < 0 or end <= start or end >= n:
        raise ValueError("invalid position path window")

    weights = dict(weights or cf.CHINEXT_V51_WEIGHTS)
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    scores = cf.dimension_score(signals, weights)
    erp = _erp_series(daily, pe_map)
    production = [0.0] * (n - 1)
    continuous = [0.0] * (n - 1)
    caps = [1.0] * (n - 1)
    previous = {"position": 0.0, "pending": None}
    for index in range(start, end):
        cap = cf.defensive_state(
            closes[:index + 1], None,
            {"risk_off": False, "basis_min_ap": None,
             "intraday_pct": 0.0},
        )["cap"]
        if erp_cap and erp is not None and erp[index] is not None \
                and erp[index] < 0.10:
            cap = min(cap, 0.6)
        caps[index] = cap
        decision = ct.decide_position(scores[index], cap, previous, tiers=tiers)
        production[index] = float(decision["position"])
        continuous[index] = min(score_to_continuous_position(scores[index]), cap)
        previous = {"position": decision["position"],
                    "pending": decision["pending"]}
    return {
        "closes": closes,
        "amounts": amounts,
        "signals": signals,
        "scores": scores,
        "caps": caps,
        "production": production,
        "continuous": continuous,
        "start": start,
        "end": end,
        "erp_cap": bool(erp_cap and erp is not None),
    }


def run_mapping_experiment(daily: pd.DataFrame, fee: float = 0.0,
                           pe_map: dict | None = None,
                           erp_cap: bool = False) -> dict:
    paths = build_position_paths(daily, pe_map=pe_map, erp_cap=erp_cap)
    result = {}
    for name in ("production", "continuous"):
        result[name] = evaluate_position_path(
            paths["closes"], paths[name], fee=fee,
            start=paths["start"], end=paths["end"],
        )
    return {"paths": paths, "metrics": result}


def _official_baseline_metrics(daily: pd.DataFrame, fee: float,
                               pe_map: dict | None, erp_cap: bool) -> dict:
    raw = backtest_metrics(daily, fee=fee, pe_map=pe_map, erp_cap=erp_cap)
    benchmark = []
    previous = 1.0
    for value in raw["bh_navs"]:
        benchmark.append(value / previous - 1.0)
        previous = value
    return performance_metrics(raw["daily_rets"], benchmark)


def format_factor_report(report: dict) -> str:
    lines = [
        "因子 IC（全样本 / 尾部留出；增量=完整分数−去掉该因子分数）",
        "因子                     窗口       原始IC(n)    尾部IC(n)   增量IC(n)   判定",
    ]
    for name, horizons in report["factors"].items():
        for horizon, item in horizons.items():
            full = item["full"]
            tail = item["tail"]
            inc = item["incremental_full"]
            verdict = item["reason"]
            label = f"{FACTOR_LABELS.get(name, name)}[{name}]"
            lines.append(
                f"{label:<24} {horizon:<10} "
                f"{full['ic']:+.3f}({full['n']:>4})  "
                f"{tail['ic']:+.3f}({tail['n']:>4})  "
                f"{inc['ic']:+.3f}({inc['n']:>4})  {verdict}"
            )
    lines.append(
        f"验门：|IC|≥{report['gate_ic']:.2f}、样本≥{report['min_samples']}，"
        "同时要求全样本/尾部原始与增量 IC 通过且符号稳定。"
    )
    return "\n".join(lines)


def _format_metrics(label: str, metrics: dict) -> str:
    profit_factor = ("—" if metrics["profit_factor"] is None
                     else f"{metrics['profit_factor']:.2f}")
    bull_capture = ("—" if metrics["bull_capture"] is None
                    else f"{metrics['bull_capture']:.1%}")
    bear_capture = ("—" if metrics["bear_capture"] is None
                    else f"{metrics['bear_capture']:.1%}")
    return (
        f"{label}：累计 {metrics['total']:+.2%}｜年化 {metrics['cagr']:+.2%}｜"
        f"夏普 {metrics['sharpe']:.2f}｜Sortino {metrics['sortino']:.2f}｜"
        f"回撤 {metrics['mdd']:.2%}｜卡玛 {metrics['calmar']:.2f}｜"
        f"Ulcer {metrics['ulcer']:.2%}｜命中 {metrics['hit_rate']:.1%}｜"
        f"盈亏比 {profit_factor}｜牛捕获 {bull_capture}｜熊捕获 {bear_capture}"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="创业板 V6 研究报告（只读）")
    ap.add_argument("--daily-csv", help="完整日线 CSV，需含 date/close/amount")
    ap.add_argument("--snapshot-file", help="JSON/JSONL 快照或择时状态文件")
    ap.add_argument("--synthetic", action="store_true",
                    help="使用随机游走，仅验证流程，无量化含义")
    ap.add_argument("--fee", type=float, default=0.0,
                    help="按仓位变化计的单次成本")
    ap.add_argument("--factor-start", type=int, default=252,
                    help="因子 IC 起始 warmup 根数，默认 252")
    ap.add_argument("--tail-days", type=int, default=3 * TRADING_DAYS)
    ap.add_argument("--no-erp-cap", action="store_true",
                    help="不启用与官方回测一致的 ERP 极端封顶")
    args = ap.parse_args(argv)

    daily = load_daily_bars(args.daily_csv, synthetic=args.synthetic)
    daily = daily.copy()
    daily.index = pd.to_datetime(daily.index, errors="coerce")
    daily = daily.loc[daily.index.notna()]
    daily = daily.loc[daily.index.normalize() < pd.Timestamp(datetime.now(BJT).date())]
    if len(daily) < 62:
        raise SystemExit("399006 完整日线不足 62 根")

    pe_map = None if args.synthetic else ipe.load_cy50_pe(PROJECT_ROOT)
    use_erp_cap = bool(pe_map) and not args.no_erp_cap
    closes = daily["close"].astype(float).tolist()
    amounts = daily["amount"].astype(float).tolist()
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    report = factor_ic_report(
        signals, closes, weights=cf.CHINEXT_V51_WEIGHTS,
        start=args.factor_start, tail_days=args.tail_days,
    )
    official = _official_baseline_metrics(daily, args.fee, pe_map, use_erp_cap)
    mapping = run_mapping_experiment(
        daily, fee=args.fee, pe_map=pe_map, erp_cap=use_erp_cap,
    )

    print("=== V6 研究（只读，不改变 v5.1 生产策略）===")
    print(f"数据：399006｜{daily.index[0].date()} ~ {daily.index[-1].date()}｜"
          f"{len(daily)} 根｜ERP封顶={'开' if use_erp_cap else '关'}")
    print(_format_metrics("v5.1 官方基线", official))
    for name, metrics in mapping["metrics"].items():
        label = "生产状态机分档" if name == "production" else "连续仓位映射（实验）"
        print(_format_metrics(label, metrics))
        if metrics["avg_pos"] is not None:
            print(f"  平均仓位 {metrics['avg_pos']:.1%}｜换仓 {metrics['switches']} 次｜"
                  f"累计换手 {metrics['turnover']:.2f}")
    print()
    print(format_factor_report(report))

    if args.snapshot_file:
        snapshots = load_snapshot_payload(args.snapshot_file)
        replay = replay_snapshot_backtest(
            daily, snapshots, fee=args.fee, allow_gaps=False,
        )
        print()
        print(
            f"真实盘中回放：{replay['start']} ~ {replay['end']}｜"
            f"策略 {replay['total']:+.2%}｜夏普 {replay['sharpe']:.2f}｜"
            f"回撤 {replay['mdd']:.2%}｜{replay['n_events']} 个信号"
        )
    else:
        print("\n真实盘中回放：未提供 --snapshot-file，未生成回放数字；"
              "当前以上为日线代理/研究对照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
