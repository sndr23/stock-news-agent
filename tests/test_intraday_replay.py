# -*- coding: utf-8 -*-
"""盘中快照回放回测测试。"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src.strategy import intraday_replay as replay  # noqa: E402
from src.strategy.intraday_snapshot import make_snapshot_record  # noqa: E402
from backtest_intraday_snapshots import load_snapshot_payload  # noqa: E402

pytestmark = pytest.mark.unit


def _daily_bars(n=65):
    dates = pd.bdate_range("2026-01-05", periods=n)
    return pd.DataFrame(
        {
            "close": [100.0 + i for i in range(n)],
            "amount": [1000.0 + i for i in range(n)],
        },
        index=dates,
    )


def _snapshots(daily, start=60, closes=None):
    closes = closes or [200.0, 210.0, 220.0, 230.0, 240.0]
    records = []
    for offset, close in enumerate(closes):
        day = daily.index[start + offset].strftime("%Y-%m-%d")
        records.append(make_snapshot_record(
            date=day,
            close=close,
            amount=2000.0 + offset,
            high=close + 1.0,
            low=close - 1.0,
            source="fixture",
            captured_at=f"{day}T14:45:00+08:00",
            amount_unit="shares",
        ))
    return records


def test_replay_uses_snapshot_close_and_amount_without_future_bar(monkeypatch):
    daily = _daily_bars()
    snapshots = _snapshots(daily)
    observed = []

    def fake_core(close, amount, **kwargs):
        observed.append((len(close), close[-1], amount[-1]))
        return {"fixture": [0.0] * len(close)}

    monkeypatch.setattr(replay.cf, "core_signals", fake_core)
    monkeypatch.setattr(
        replay.cf,
        "dimension_score",
        lambda signals, weights: [0.0] * len(next(iter(signals.values()))),
    )
    monkeypatch.setattr(
        replay.cf,
        "defensive_state",
        lambda close, *args, **kwargs: {"cap": 1.0, "triggers": []},
    )
    monkeypatch.setattr(
        replay.ct,
        "decide_position",
        lambda score, cap, prev, tiers=None: {
            "position": 1.0,
            "pending": None,
            "changed": prev.get("position") != 1.0,
            "direction": "up" if prev.get("position") != 1.0 else "hold",
        },
    )

    metrics = replay.replay_snapshot_backtest(daily, snapshots)

    assert observed[0] == (61, 200.0, 2000.0)
    # 第二个信号日只能看到前一日完整日线和当日快照，不能提前看到 220。
    assert observed[1] == (62, 210.0, 2001.0)
    assert metrics["return_basis"] == "execution_close_to_next_close"
    assert metrics["total"] == pytest.approx(164.0 / 160.0 - 1.0)
    assert metrics["events"][0]["next_date"] == snapshots[1]["date"]
    assert metrics["events"][0]["snapshot_to_next_snapshot_return"] == pytest.approx(0.05)


def test_replay_rejects_missing_trading_day_by_default():
    daily = _daily_bars()
    snapshots = _snapshots(daily)
    missing = snapshots.pop(2)

    with pytest.raises(ValueError, match="快照缺失") as exc_info:
        replay.replay_snapshot_backtest(daily, snapshots)

    # 错误信息必须指出被删掉的交易日，便于补采而不是盲目调参。
    assert missing["date"] in str(exc_info.value)


def test_replay_allows_explicit_gaps_and_rejects_invalid_snapshot():
    daily = _daily_bars()
    snapshots = _snapshots(daily)
    snapshots.pop(2)
    metrics = replay.replay_snapshot_backtest(daily, snapshots, allow_gaps=True)
    assert metrics["n_events"] == 3

    bad = dict(snapshots[0])
    bad["close"] = -1.0
    with pytest.raises(ValueError, match="快照归档无效"):
        replay.replay_snapshot_backtest(daily, [bad] + snapshots[1:],
                                        allow_gaps=True)


def test_snapshot_loader_accepts_state_history_shape(tmp_path):
    snapshot = _snapshots(_daily_bars())[0]
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"history": [{
            "date": snapshot["date"],
            "index_snapshot": snapshot,
        }]}),
        encoding="utf-8",
    )

    loaded = load_snapshot_payload(path)

    assert loaded == [snapshot]
