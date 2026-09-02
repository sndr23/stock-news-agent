# -*- coding: utf-8 -*-
"""用历史盘中快照归档做无前视回放。

示例：
  python scripts/backtest_intraday_snapshots.py \
      --snapshot-file logs/intraday_snapshots.json \
      --daily-csv data/399006_daily.csv

``--snapshot-file`` 可以是快照数组、``{"snapshots": [...]}``，也可以直接
是择时状态文件（从 history[*].index_snapshot 提取）。默认禁止缺交易日；
稀疏研究必须显式加 ``--allow-gaps``，否则不能把“只挑有数据的好日子”当成
策略表现。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.data import load_index_daily_full, load_index_sina  # noqa: E402
from src.strategy.intraday_replay import replay_snapshot_backtest  # noqa: E402


def load_snapshot_payload(path: str | Path) -> list[dict]:
    """读取快照数组、JSONL 或择时状态 history，统一成快照数组。"""
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"快照文件不存在：{source}")
    if source.suffix.lower() == ".jsonl":
        payload = [json.loads(line) for line in
                   source.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"快照文件不是有效 JSON：{source}") from exc

    if isinstance(payload, dict):
        items = payload.get("snapshots")
        if items is None:
            items = payload.get("history")
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("快照文件根节点必须是数组或包含 snapshots/history 数组")

    result = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        # 兼容本项目影子状态：index_snapshot 是新的规范键，snapshot 是
        # 早期/外部归档可能使用的别名；日期从外层 history 记录继承。
        nested = item.get("index_snapshot")
        if not isinstance(nested, dict):
            nested = item.get("snapshot")
        if isinstance(nested, dict):
            snapshot = dict(nested)
            snapshot.setdefault("date", item.get("date"))
            result.append(snapshot)
        else:
            result.append(item)
    return result


def load_daily_bars(path: str | Path | None) -> pd.DataFrame:
    if path:
        source = Path(path)
        frame = pd.read_csv(source)
        date_col = "date" if "date" in frame.columns else frame.columns[0]
        frame.index = pd.to_datetime(frame.pop(date_col), errors="coerce")
        return frame
    frame = load_index_sina("399006", datalen=3001)
    if frame is None or frame.empty:
        frame = load_index_daily_full("399006", "20200101")
    if frame is None or frame.empty:
        raise ValueError("399006 完整日线获取失败")
    return frame


def format_metrics(metrics: dict, fee: float, allow_gaps: bool) -> str:
    return "\n".join([
        f"创业板盘中快照回放（{metrics['start']} ~ {metrics['end']}，"
        f"{metrics['n_events']}个信号，成本{fee:.2%}/次）",
        "",
        f"策略累计 {metrics['total']:+.2%} / 年化 {metrics['cagr']:+.2%} / "
        f"夏普 {metrics['sharpe']:.2f} / 最大回撤 {metrics['mdd']:.2%}",
        f"快照基准 {metrics['bh']:+.2%} / 最大回撤 {metrics['bh_mdd']:.2%}",
        f"换仓 {metrics['switches']} 次｜平均仓位 {metrics['avg_pos']:.0%}",
        f"收益口径：{metrics['return_basis']}｜允许缺日：{allow_gaps}",
        "说明：信号只使用 d-1 完整日线 + d 日盘中快照，按 d 收盘成交到 d+1 完整收盘计收益。",
    ])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="创业板盘中快照回放回测")
    ap.add_argument("--snapshot-file", required=True,
                    help="JSON/JSONL 快照归档，或含 history.index_snapshot 的状态文件")
    ap.add_argument("--daily-csv", help="可选完整日线 CSV，需含 date/close/amount")
    ap.add_argument("--start-date", help="信号起始日，含该日")
    ap.add_argument("--end-date", help="信号结束日，不含该日")
    ap.add_argument("--fee", type=float, default=0.0, help="按换仓仓位幅度计的单次费率")
    ap.add_argument("--allow-gaps", action="store_true",
                    help="允许快照缺交易日，仅用于明确标注的稀疏研究")
    args = ap.parse_args(argv)

    snapshots = load_snapshot_payload(args.snapshot_file)
    daily = load_daily_bars(args.daily_csv)
    metrics = replay_snapshot_backtest(
        daily, snapshots, fee=args.fee,
        eval_start=args.start_date, eval_end=args.end_date,
        allow_gaps=args.allow_gaps,
    )
    print(format_metrics(metrics, args.fee, args.allow_gaps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
