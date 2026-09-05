# -*- coding: utf-8 -*-
"""当日盘中 bar 拼接守卫（run_chinext_timing._append_intraday_bar_if_needed）单元测试。

背景（2026-09-01 排查）：GitHub Actions 海外出口盘中拉新浪全量拿不到当日实时
bar，v5.1"d 日快照"口径在云端从未生效（实盘跑 d-1）。守卫在交易日盘中用腾讯
实时构造当日 partial bar 拼到日线末尾，把实盘信息集对齐回已拍板口径。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import run_chinext_timing as rct  # noqa: E402
import src.strategy.data as sdata  # noqa: E402

pytestmark = pytest.mark.unit

BJT = rct.BJT
# 2026-09-01 是周二；df 末根 8-31（周一）
FAKE_NOW = datetime(2026, 9, 1, 14, 45, tzinfo=BJT)


class _FakeDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FAKE_NOW


def _df_upto(prev_day: str = "2026-08-31") -> pd.DataFrame:
    idx = pd.to_datetime(["2026-08-27", "2026-08-28", prev_day])
    return pd.DataFrame(
        {"close": [3473.4, 3424.4, 3438.7],
         "amount": [1.83e10, 1.86e10, 1.93e10],
         "high": [3480.0, 3521.5, 3438.7],
         "low": [3408.8, 3423.1, 3359.1]},
        index=idx)


_TENCENT_BAR = {"close": 3393.43, "amount": 19489930600.0,
                "high": 3442.48, "low": 3375.01}


def _patch_market_open(monkeypatch, trading_day=True):
    monkeypatch.setattr(rct, "datetime", _FakeDateTime)
    monkeypatch.setattr(rct, "_is_trading_day", lambda: trading_day)


def test_append_intraday_bar_appends_today_partial(monkeypatch):
    """交易日 14:45 且 df 末根早于今天 → 拼当日 partial bar，列序不变。"""
    _patch_market_open(monkeypatch)
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent",
                        lambda sym: dict(_TENCENT_BAR))
    df = _df_upto()

    out = rct._append_intraday_bar_if_needed(df, "399006")

    assert len(out) == 4
    assert out.index[-1] == pd.Timestamp("2026-09-01")
    row = out.iloc[-1]
    assert row["close"] == pytest.approx(3393.43)
    assert row["amount"] == pytest.approx(19489930600.0)
    assert row["high"] == pytest.approx(3442.48)
    assert row["low"] == pytest.approx(3375.01)
    assert list(out.columns) == list(df.columns)  # 列序与原 df 一致
    assert out.iloc[:-1].equals(df)  # 历史行不受影响


def test_append_intraday_bar_skipped_after_close(monkeypatch):
    """收盘后（15:30）不拼接——新浪收盘数据迟早更新，避免重复/冲突 bar。"""
    _patch_market_open(monkeypatch)
    monkeypatch.setattr(rct, "datetime",
                        type("_FD", (datetime,),
                             {"now": classmethod(
                                 lambda c, tz=None: FAKE_NOW + timedelta(hours=1))}))
    called = []
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent",
                        lambda sym: called.append(sym) or dict(_TENCENT_BAR))

    out = rct._append_intraday_bar_if_needed(_df_upto(), "399006")

    assert called == []
    assert len(out) == 3


def test_append_intraday_bar_skipped_when_today_bar_exists(monkeypatch):
    """df 已含当日 bar（本地国内出口新浪盘中本就含）→ 不重复拼接。"""
    _patch_market_open(monkeypatch)
    called = []
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent",
                        lambda sym: called.append(sym) or dict(_TENCENT_BAR))

    out = rct._append_intraday_bar_if_needed(_df_upto("2026-09-01"), "399006")

    assert called == []
    assert len(out) == 3


def test_append_intraday_bar_keeps_d_minus_1_on_failure(monkeypatch):
    """腾讯失败 → 保持 d-1 口径原样返回，绝不写假数据。"""
    _patch_market_open(monkeypatch)
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent",
                        lambda sym: None)

    df = _df_upto()
    out = rct._append_intraday_bar_if_needed(df, "399006")

    assert out.equals(df)
    assert out.index[-1] == pd.Timestamp("2026-08-31")


def test_append_intraday_bar_skipped_on_non_trading_day(monkeypatch):
    """非交易日（周末/节假日）不拼接。"""
    _patch_market_open(monkeypatch, trading_day=False)
    called = []
    monkeypatch.setattr(sdata, "fetch_intraday_bar_tencent",
                        lambda sym: called.append(sym) or dict(_TENCENT_BAR))

    out = rct._append_intraday_bar_if_needed(_df_upto(), "399006")

    assert called == []
    assert len(out) == 3
