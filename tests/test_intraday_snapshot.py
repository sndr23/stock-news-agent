# -*- coding: utf-8 -*-
"""盘中快照规范化、审计与严格门禁测试。"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src.strategy.intraday_snapshot import (  # noqa: E402
    make_snapshot_record,
    snapshot_time,
    validate_snapshot,
)
import run_chinext_timing as rct  # noqa: E402
from src.strategy import data as sdata  # noqa: E402

pytestmark = pytest.mark.unit

BJT = timezone(timedelta(hours=8))


def _valid_snapshot(**overrides):
    values = {
        "date": "2026-09-02",
        "close": 3393.43,
        "amount": 19489930600.0,
        "high": 3442.48,
        "low": 3375.01,
        "source": "tencent_realtime",
        "captured_at": "2026-09-02T14:46:43+08:00",
        "amount_unit": "shares",
    }
    values.update(overrides)
    return values


def test_make_snapshot_record_is_canonical_and_auditable():
    record = make_snapshot_record(
        date="2026-09-02",
        close=3393.43,
        amount=19489930600.0,
        high=3442.48,
        low=3375.01,
        captured_at=datetime(2026, 9, 2, 14, 46, 43, tzinfo=BJT),
        source="tencent_realtime",
        amount_unit="shares",
    )

    result = validate_snapshot(record, expected_date="2026-09-02")

    assert result["ok"] is True
    assert record["captured_at"] == "2026-09-02T14:46:43+08:00"
    assert snapshot_time(record) == "14:46"
    assert record["capture_time_type"] == "local_request_time"


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"close": 0}, "close"),
        ({"amount": -1}, "amount"),
        ({"high": 3300}, "high"),
        ({"low": 3500}, "low"),
        ({"date": "2026-09-01"}, "date"),
        ({"source": ""}, "source"),
    ],
)
def test_validate_snapshot_rejects_bad_market_input(overrides, expected):
    result = validate_snapshot(_valid_snapshot(**overrides),
                               expected_date="2026-09-02")

    assert result["ok"] is False
    assert expected in result["reason"]


def test_validate_snapshot_rejects_provenance_date_mismatch():
    result = validate_snapshot(
        _valid_snapshot(captured_at="2026-09-01T14:46:43+08:00"),
        expected_date="2026-09-02",
    )

    assert result["ok"] is False
    assert "captured_at" in result["reason"]


def test_validate_snapshot_rejects_unknown_amount_unit():
    result = validate_snapshot(_valid_snapshot(amount_unit="unknown"),
                               expected_date="2026-09-02")

    assert result["ok"] is False
    assert "amount_unit" in result["reason"]


def test_snapshot_gate_strict_mode_blocks_missing_intraday_bar():
    ctx = {
        "intraday_snapshot_required": True,
        "intraday_snapshot": False,
        "snapshot_quality": {"ok": False, "reason": "snapshot_missing"},
    }

    result = rct.snapshot_gate(ctx, strict=True)

    assert result == {"ok": False, "reason": "snapshot_missing"}


def test_snapshot_gate_allows_valid_snapshot_and_after_hours_fallback():
    valid = {
        "intraday_snapshot_required": True,
        "intraday_snapshot": True,
        "snapshot_quality": {"ok": True, "reason": "ok"},
    }
    after_hours = {
        "intraday_snapshot_required": False,
        "intraday_snapshot": False,
        "snapshot_quality": {"ok": False, "reason": "snapshot_missing"},
    }

    assert rct.snapshot_gate(valid, strict=True)["ok"] is True
    assert rct.snapshot_gate(after_hours, strict=True)["ok"] is True


def test_signal_data_quality_marks_degraded_enhanced_inputs_as_b():
    result = rct.signal_data_quality({"snapshot_stale": True}, score=0.0)

    assert result["level"] == "B"


def test_append_snapshot_adds_high_low_to_low_fallback_daily_frame(monkeypatch):
    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 1, 14, 45, tzinfo=BJT)

    monkeypatch.setattr(rct, "datetime", _FakeDT)
    monkeypatch.setattr(rct, "_is_trading_day", lambda: True)
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent", lambda *_: {
        "close": 105.0, "amount": 2000.0,
        "high": 106.0, "low": 104.0, "amount_unit": "shares",
    })
    daily = pd.DataFrame(
        {"close": [100.0], "amount": [1000.0]},
        index=pd.to_datetime(["2026-08-31"]),
    )

    out = rct._append_intraday_bar_if_needed(daily, "399006")

    assert out.iloc[-1]["high"] == 106.0
    assert out.iloc[-1]["low"] == 104.0
    assert out.attrs["intraday_snapshot_meta"]["amount_unit"] == "shares"


def test_append_snapshot_refreshes_cached_same_day_bar(monkeypatch):
    """缓存已有当天末根时，交易时段仍必须刷新真正的盘中快照。"""
    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 1, 14, 45, tzinfo=BJT)

    monkeypatch.setattr(rct, "datetime", _FakeDT)
    monkeypatch.setattr(rct, "_is_trading_day", lambda: True)
    calls = []
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent", lambda *_: (
        calls.append(True) or {
            "close": 107.0, "amount": 2200.0,
            "high": 108.0, "low": 106.0, "amount_unit": "shares",
        }
    ))
    daily = pd.DataFrame(
        {"close": [100.0, 105.0], "amount": [1000.0, 2000.0],
         "high": [101.0, 106.0], "low": [99.0, 104.0]},
        index=pd.to_datetime(["2026-08-31", "2026-09-01"]),
    )
    daily.attrs["strategy_data_source"] = "sina_volume"
    daily.attrs["strategy_amount_unit"] = "shares"

    out = rct._append_intraday_bar_if_needed(daily, "399006")

    assert calls == [True]
    assert out.iloc[-1]["close"] == 107.0
    assert out.iloc[-1]["amount"] == 2200.0
    assert out.attrs["intraday_snapshot_meta"]["close"] == 107.0


def test_append_snapshot_converts_tencent_shares_to_history_hands(monkeypatch):
    """腾讯实时量为股时，必须转换到腾讯日K历史的手，禁止跨量纲比较。"""
    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 1, 14, 45, tzinfo=BJT)

    monkeypatch.setattr(rct, "datetime", _FakeDT)
    monkeypatch.setattr(rct, "_is_trading_day", lambda: True)
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent", lambda *_: {
        "close": 105.0, "amount": 2000.0,
        "high": 106.0, "low": 104.0,
        "amount_unit": "shares",
    })
    daily = pd.DataFrame(
        {"close": [100.0], "amount": [1000.0]},
        index=pd.to_datetime(["2026-08-31"]),
    )
    daily.attrs["strategy_data_source"] = "tencent_volume"
    daily.attrs["strategy_amount_unit"] = "hands"

    out = rct._append_intraday_bar_if_needed(daily, "399006")

    assert out.iloc[-1]["amount"] == pytest.approx(20.0)
    assert out.attrs["intraday_snapshot_meta"]["amount"] == pytest.approx(20.0)
    assert out.attrs["intraday_snapshot_meta"]["amount_unit"] == "hands"


def test_shadow_history_records_snapshot_and_uses_execution_return():
    meta = _valid_snapshot(
        date="2026-09-03", close=105.0, amount=2000.0, high=106.0, low=104.0,
        captured_at="2026-09-03T14:46:43+08:00",
    )
    state = {"history": [{
        "date": "2026-09-01",
        "snapshot_close": 100.0,
        "next_ret": None,
    }]}
    ctx = {
        # 当前日是 9-03；9-02 已经是完整收盘，验证不会把当前盘中价
        # 当成前一日的执行结果。
        "dates": ["2026-09-01", "2026-09-02", "2026-09-03"],
        "closes": [1000.0, 1050.0, 105.0],
        "intraday_snapshot_meta": meta,
        "snapshot_quality": {"ok": True, "reason": "ok"},
        "snapshot": {},
        "overseas_drop": 0.0,
        "intraday": 0.0,
    }
    res = {
        "core": {"score": 0.0, "signals": {}},
        "mods": {"basis": 0.0, "flow": 0.0, "mood": 0.0, "news": 0.0,
                 "chan": {}, "stock": {}},
        "caps": {"cap": 1.0, "triggers": []},
    }

    rct.update_shadow_history(state, ctx, "2026-09-03", 0.0, res, 0.0)

    previous = state["history"][0]
    current = state["history"][-1]
    assert previous["next_ret"] == pytest.approx(0.05)
    assert previous["next_ret_basis"] == "execution_close_to_next_close"
    assert current["index_snapshot"]["close"] == 105.0
    assert current["snapshot_source"] == "tencent_realtime"


def test_shadow_nav_does_not_mix_daily_proxy_and_snapshot_returns():
    history = [
        {"position": 1.0, "next_ret": 0.20, "next_ret_basis": "daily_bar_proxy"},
        {"position": 0.0, "next_ret": 0.02,
         "next_ret_basis": "execution_close_to_next_close"},
    ]

    nav, benchmark = rct._cumulative_nav(history)
    health = rct.strategy_health(history, window=1)

    assert nav == pytest.approx(1.0)
    assert benchmark == pytest.approx(1.02)
    assert health["stats"]["n"] == 1
