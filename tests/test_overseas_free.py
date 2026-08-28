# -*- coding: utf-8 -*-
"""海外免费数据缓存时效回归测试。"""
from datetime import datetime, timedelta
import json

import pandas as pd
import pytest

from src.strategy import overseas as ovs

pytestmark = pytest.mark.unit


def test_load_overseas_refreshes_stale_cache(monkeypatch, tmp_path):
    """外盘缓存末根过期时必须重新读取免费源，不能静默沿用旧值。"""
    cache = tmp_path / "strategy_cache" / "ovs_cache.json"
    cache.parent.mkdir(parents=True)
    old_day = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    cache.write_text(json.dumps({
        "sox": {old_day: 99.0},
        "ndx": {old_day: 99.0},
        "inx": {old_day: 99.0},
    }), encoding="utf-8")
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr("akshare.macro_global_sox_index",
                        lambda: pd.DataFrame({"日期": [fresh_day], "最新值": [101.0]}))
    monkeypatch.setattr("akshare.index_us_stock_sina",
                        lambda **_kwargs: pd.DataFrame({"date": [fresh_day],
                                                        "close": [101.0]}))

    out = ovs.load_overseas(tmp_path)

    assert out["sox"] == {fresh_day: 101.0}
    assert out["ndx"] == {fresh_day: 101.0}
    assert out["inx"] == {fresh_day: 101.0}


def test_load_overseas_drops_only_stale_series(monkeypatch, tmp_path):
    """单个外盘序列过期时保留新鲜序列，过期序列失败则独立降级。"""
    cache = tmp_path / "strategy_cache" / "ovs_cache.json"
    cache.parent.mkdir(parents=True)
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    old_day = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    cache.write_text(json.dumps({
        "sox": {fresh_day: 101.0},
        "ndx": {old_day: 99.0},
        "inx": {old_day: 99.0},
    }), encoding="utf-8")

    def failed_source(*_args, **_kwargs):
        raise RuntimeError("temporary failure")

    monkeypatch.setattr("akshare.macro_global_sox_index", failed_source)
    monkeypatch.setattr("akshare.index_us_stock_sina", failed_source)
    monkeypatch.setattr(ovs, "_fetch_yahoo_series", lambda _symbol: {}, raising=False)
    monkeypatch.setattr(ovs, "_fetch_stooq_series", lambda _symbol: {}, raising=False)

    out = ovs.load_overseas(tmp_path)

    assert out["sox"] == {fresh_day: 101.0}
    assert out["ndx"] == {}
    assert out["inx"] == {}


def test_load_overseas_rejects_stale_live_response(monkeypatch, tmp_path):
    """海外免费接口返回旧序列时不得写入并参与隔夜风控。"""
    stale_day = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    monkeypatch.setattr(
        "akshare.macro_global_sox_index",
        lambda: pd.DataFrame({"日期": [stale_day], "最新值": [101.0]}),
    )
    monkeypatch.setattr(
        "akshare.index_us_stock_sina",
        lambda **_kwargs: pd.DataFrame({"date": [stale_day], "close": [101.0]}),
    )
    monkeypatch.setattr(ovs, "_fetch_yahoo_series", lambda _symbol: {}, raising=False)
    monkeypatch.setattr(ovs, "_fetch_stooq_series", lambda _symbol: {}, raising=False)

    out = ovs.load_overseas(tmp_path)

    assert out == {"sox": {}, "ndx": {}, "inx": {}}


def test_load_overseas_filters_nonfinite_values_from_live_sources(monkeypatch, tmp_path):
    """实时免费源混入无穷价格时，只保留有限正数记录。"""
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    previous_day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(
        "akshare.macro_global_sox_index",
        lambda: pd.DataFrame({"日期": [previous_day, fresh_day],
                              "最新值": [float("inf"), 101.0]}),
    )
    monkeypatch.setattr(
        "akshare.index_us_stock_sina",
        lambda **_kwargs: pd.DataFrame({"date": [previous_day, fresh_day],
                                        "close": [float("inf"), 101.0]}),
    )

    out = ovs.load_overseas(tmp_path)

    assert all(out[key] == {fresh_day: 101.0} for key in ("sox", "ndx", "inx"))


def test_load_overseas_normalizes_sox_timestamp_keys(monkeypatch, tmp_path):
    """SOX 日期带时间部分时也必须能被 overnight_drop 识别。"""
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr(
        "akshare.macro_global_sox_index",
        lambda: pd.DataFrame({"日期": [f"{fresh_day} 00:00:00"], "最新值": [101.0]}),
    )
    monkeypatch.setattr(
        "akshare.index_us_stock_sina",
        lambda **_kwargs: pd.DataFrame({"date": [fresh_day], "close": [101.0]}),
    )

    out = ovs.load_overseas(tmp_path)

    assert set(out["sox"]) == {fresh_day}
    assert ovs.overnight_drop(
        {"sox": {fresh_day: 99.0, "2026-08-26": 100.0}}, datetime.now()
    ) == -0.01


def test_overnight_drop_normalizes_direct_timestamp_cache_keys():
    """直接传入旧缓存时，带时间部分的日期键也必须正确计算跌幅。"""
    ov = {"sox": {
        "2026-08-26 16:00:00": 100.0,
        "2026-08-27T16:00:00": 99.0,
    }}

    assert ovs.overnight_drop(ov, datetime(2026, 8, 28)) == -0.01


def test_load_overseas_falls_back_to_yahoo_when_akshare_fails(monkeypatch, tmp_path):
    """新浪封装失败时，外盘序列应尝试独立 Yahoo 免费源。"""
    fresh_day = datetime.now().strftime("%Y-%m-%d")

    def failed_source(*_args, **_kwargs):
        raise RuntimeError("akshare unavailable")

    monkeypatch.setattr("akshare.macro_global_sox_index", failed_source)
    monkeypatch.setattr("akshare.index_us_stock_sina", failed_source)
    monkeypatch.setattr(
        ovs, "_fetch_yahoo_series",
        lambda symbol: {fresh_day: 100.0, "2026-08-27": 99.0},
        raising=False,
    )
    monkeypatch.setattr(ovs, "_fetch_stooq_series", lambda _symbol: {}, raising=False)

    out = ovs.load_overseas(tmp_path)

    assert out["sox"]
    assert out["ndx"]
    assert out["inx"]


def test_load_overseas_falls_back_to_stooq_after_yahoo_fails(monkeypatch, tmp_path):
    """Yahoo 不可用时，外盘序列应继续尝试第二个独立免费源。"""
    fresh_day = datetime.now().strftime("%Y-%m-%d")

    def failed_source(*_args, **_kwargs):
        raise RuntimeError("primary unavailable")

    monkeypatch.setattr("akshare.macro_global_sox_index", failed_source)
    monkeypatch.setattr("akshare.index_us_stock_sina", failed_source)
    monkeypatch.setattr(ovs, "_fetch_yahoo_series", lambda _symbol: {}, raising=False)
    monkeypatch.setattr(
        ovs, "_fetch_stooq_series",
        lambda _symbol: {"2026-08-27": 99.0, fresh_day: 100.0},
        raising=False,
    )

    out = ovs.load_overseas(tmp_path)

    assert out["sox"]
    assert out["ndx"]
    assert out["inx"]


def test_yahoo_chart_parser_normalizes_daily_closes(monkeypatch):
    """Yahoo Chart JSON 应解析为标准日期/收盘价序列。"""
    from datetime import timezone
    import time

    ts = int(datetime(2026, 8, 27, tzinfo=timezone.utc).timestamp())

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"chart": {"result": [{
                "timestamp": [ts, ts + 86400],
                "indicators": {"quote": [{"close": [99.0, 100.0]}]},
            }]}}

    class _Session:
        trust_env = True

        def get(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(ovs.requests, "Session", lambda: _Session())

    assert ovs._fetch_yahoo_series("^SOX") == {
        "2026-08-27": 99.0,
        "2026-08-28": 100.0,
    }


def test_stooq_csv_parser_normalizes_daily_closes(monkeypatch):
    """Stooq CSV 应解析为标准日期/收盘价序列。"""
    class _Response:
        text = "Date,Open,High,Low,Close,Volume\n2026-08-27,99,101,98,100,1\n"

        def raise_for_status(self):
            return None

    class _Session:
        trust_env = True

        def get(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(ovs.requests, "Session", lambda: _Session())

    assert ovs._fetch_stooq_series("^sox") == {"2026-08-27": 100.0}


def test_series_fresh_returns_false_when_normalization_removes_all_rows():
    """非空但日期/价格均非法的序列应安全降级，不得触发 max 空序列异常。"""
    assert not ovs._series_is_fresh({"not-a-date": "not-a-price"})


def test_normalize_series_rejects_nonfinite_prices():
    """无穷价格不能进入外盘缓存或跌幅计算。"""
    assert ovs._normalize_series({"2026-08-27": float("inf")}) == {}


def test_load_overseas_ignores_malformed_cache_and_uses_free_fallback(monkeypatch, tmp_path):
    """缓存结构损坏时应重拉免费源，不能让缓存异常阻断降级链。"""
    cache = tmp_path / "strategy_cache" / "ovs_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"sox": [], "ndx": None, "inx": "broken"}),
                     encoding="utf-8")
    fresh_day = datetime.now().strftime("%Y-%m-%d")

    def failed_source(*_args, **_kwargs):
        raise RuntimeError("primary unavailable")

    monkeypatch.setattr("akshare.macro_global_sox_index", failed_source)
    monkeypatch.setattr("akshare.index_us_stock_sina", failed_source)
    monkeypatch.setattr(
        ovs, "_fetch_yahoo_series",
        lambda _symbol: {fresh_day: 100.0}, raising=False,
    )

    out = ovs.load_overseas(tmp_path)

    assert all(out[key] == {fresh_day: 100.0} for key in ("sox", "ndx", "inx"))
