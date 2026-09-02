# -*- coding: utf-8 -*-
"""数据层缓存单元测试（tmp 目录隔离，无网络）"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.strategy import data as sdata

pytestmark = pytest.mark.unit


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sdata, "CACHE_DIR", tmp_path)
    assert sdata._cache_get("k") is None
    sdata._cache_set("k", {"a": 1})
    assert sdata._cache_get("k") == {"a": 1}


def test_cache_ttl_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(sdata, "CACHE_DIR", tmp_path)
    sdata._cache_set("k", "v")
    # 把文件时间拨回 10 天前
    p = tmp_path / "k.pkl"
    old = p.stat().st_mtime - 10 * 86400
    import os
    os.utime(p, (old, old))
    assert sdata._cache_get("k", ttl_days=7) is None
    assert sdata._cache_get("k", ttl_days=30) == "v"   # 无TTL/长TTL仍命中


def test_panel_returns_and_mask(tmp_path):
    idx = pd.bdate_range("2025-01-01", periods=5)
    cols = ["600000", "600001"]
    close = pd.DataFrame([[10, 20], [11, 20.2], [12, 20.4], [13, 20.6], [14, 20.8]],
                         index=idx, columns=cols)
    amount = pd.DataFrame([[1e8, 1e8]] * 5, index=idx, columns=cols)
    panel = sdata.PanelData(close=close, high=close, low=close,
                            volume=close, amount=amount, turnover=amount * 0 + 1.5,
                            index_close=close[["600000"]] * 100, codes=cols)
    rets = panel.returns()
    assert abs(rets.iloc[1]["600000"] - 0.1) < 1e-9
    mask = panel.tradable_mask()
    assert mask.all().all()


def _mk_df(last_date):
    return pd.DataFrame({"close": [1.0], "amount": [1e8]},
                        index=[pd.Timestamp(last_date)])


def _is_cn_workday(day):
    """A 股交易日判定；日历库缺失/不支持该年份时退化为周一至周五。"""
    try:
        from src.strategy.data_freshness import _is_workday

        return _is_workday(day, "cn")
    except Exception:
        return day.weekday() < 5


def _stale_date(target_lag: int = 5):
    """返回按 A 股交易日口径确定"陈旧"的日期。

    生产侧 is_recent_data_date 按**工作日**计数（max_lag_days=3），而自然日
    -5 在周末/节后只覆盖 ≤3 个交易日，会被误判为新鲜（2026-08-31 回归）。
    这里改为倒推 target_lag(>3) 个交易日，任何星期几/长假期都成立。
    """
    day = datetime.now().date()
    lag = 0
    while lag < target_lag:
        day -= timedelta(days=1)
        if _is_cn_workday(day):
            lag += 1
    return day


def _stale_day_str(target_lag: int = 5):
    return _stale_date(target_lag).strftime("%Y-%m-%d")


def test_index_sina_cache_fresh_hits(monkeypatch):
    """缓存末根不早于昨天 → 直接命中，不触发网络"""
    fresh = _mk_df(datetime.now().date())
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: fresh)
    import requests
    hit = {"net": False}

    class _Sess:
        def get(self, *a, **k):
            hit["net"] = True
            return _empty_resp()
        def _unused(self):
            pass

    def _empty_resp():
        return type("R", (), {"json": lambda self: []})()
    monkeypatch.setattr(requests, "Session", lambda: _Sess())
    df = sdata.load_index_sina("399006", datalen=10)
    assert df is fresh
    assert not hit["net"]
    assert df.attrs["strategy_data_source"] == "sina_volume"
    assert df.attrs["strategy_amount_unit"] == "shares"


def test_index_sina_cache_stale_refetch(monkeypatch):
    """缓存末根早于昨天 → 判为陈旧，强制重拉（修复隔日滞后）"""
    stale = _mk_df(_stale_date())
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: stale)
    import requests
    hit = {"net": False}

    # 拉取返回异常空列表 → 走降级链，用空 df 兜底，验证确实绕过了陈旧缓存
    def _empty_resp():
        return type("R", (), {"json": lambda self: []})()
    class _Sess:
        def get(self, *a, **k):
            hit["net"] = True
            return _empty_resp()
    monkeypatch.setattr(requests, "Session", lambda: _Sess())
    monkeypatch.setattr(sdata, "load_index_daily_full",
                        lambda *a, **k: pd.DataFrame())
    df = sdata.load_index_sina("399006", datalen=10)
    assert hit["net"]  # 必须发起网络重拉，而不是复用陈旧缓存
    assert df.empty  # 网络无数据 → fallback 空帧，证明已从重拉分支走出


def test_index_sina_rejects_stale_live_response(monkeypatch):
    """新浪接口返回旧日线时不得把旧响应当成成功数据。"""
    stale_day = _stale_day_str()
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    import requests

    payload = [{"day": stale_day, "close": "1.0", "volume": "100"}]

    class _Sess:
        def get(self, *a, **k):
            return type("R", (), {"json": lambda self: payload})()

    monkeypatch.setattr(requests, "Session", lambda: _Sess())
    monkeypatch.setattr(sdata, "load_index_daily_full",
                        lambda *a, **k: pd.DataFrame())

    assert sdata.load_index_sina("399006", datalen=10).empty


def test_index_sina_skips_malformed_live_response(monkeypatch):
    """新浪接口返回字段损坏的列表时，应继续走短链而不是抛结构异常。"""
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    import requests

    class _Sess:
        def get(self, *a, **k):
            return type("R", (), {"json": lambda self: [{"unexpected": "value"}]})()

    monkeypatch.setattr(requests, "Session", lambda: _Sess())
    monkeypatch.setattr(sdata, "load_index_daily_full",
                        lambda *a, **k: pd.DataFrame())

    assert sdata.load_index_sina("399006", datalen=10).empty


@pytest.mark.parametrize("invalid_volume", ["NaN", "-1"])
def test_index_sina_rejects_invalid_amount_anywhere_and_falls_back(
        monkeypatch, invalid_volume):
    """新浪历史任意一根成交量非法时，不能把坏量能缓存或交给量价因子。"""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    payload = [
        {"day": yesterday, "close": "1.0", "volume": invalid_volume},
        {"day": today, "close": "2.0", "volume": "100"},
    ]
    fallback = _mk_df(datetime.now().date())
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    monkeypatch.setattr(sdata, "load_index_daily_full",
                        lambda *a, **k: fallback)

    class _Sess:
        def get(self, *a, **k):
            return type("R", (), {"json": lambda self: payload})()

    monkeypatch.setattr("requests.Session", lambda: _Sess())

    out = sdata.load_index_sina("399006", datalen=10)

    assert out is fallback


def test_last_bar_fresh_rejects_future_bar():
    """未来日期不是已确认行情，不能被新鲜度检查放行。"""
    future = _mk_df(datetime.now().date() + timedelta(days=1))

    assert not sdata._last_bar_is_fresh(future)


def test_last_bar_fresh_rejects_non_dataframe_cache():
    """合法 pickle 但根节点类型错误时，应按缓存未命中处理而非抛异常。"""
    assert not sdata._last_bar_is_fresh({"close": 1.0})


def test_index_sina_ignores_non_dataframe_cache(monkeypatch):
    """指数全量入口遇到错误类型缓存时，应降级网络链而不是抛异常。"""
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: {"bad": True})
    monkeypatch.setattr(sdata, "load_index_daily_full",
                        lambda *a, **k: pd.DataFrame())

    class _Session:
        trust_env = False

        def get(self, *args, **kwargs):
            return type("R", (), {"json": lambda self: []})()

    monkeypatch.setattr("requests.Session", lambda: _Session())

    assert sdata.load_index_sina("399006", datalen=10).empty


def test_index_daily_full_ignores_non_dataframe_cache(monkeypatch):
    """指数增量入口遇到错误类型缓存时，应按缓存未命中处理。"""
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: {"bad": True})
    monkeypatch.setattr(sdata, "_fetch_index_full_frame", lambda *a, **k: None)

    assert sdata.load_index_daily_full("399006", "20200101").empty


def test_hs300_index_loader_ignores_non_dataframe_cache(monkeypatch):
    """沪深300基准缓存根节点类型错误时，应继续按空数据处理。"""
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: {"bad": True})
    monkeypatch.setattr(sdata, "_fetch_index_frame", lambda *a, **k: None)

    assert sdata._load_index_daily("000300", "20200101").empty


def test_hs300_index_loader_ignores_structurally_invalid_cache(monkeypatch):
    """基准缓存缺少 close 列时，应重新走免费源而不是放行损坏表。"""
    today = pd.Timestamp(datetime.now().date())
    cached = pd.DataFrame({"amount": [100.0]}, index=[today])
    fresh = pd.DataFrame({"close": [3010.0]}, index=[today])
    calls = []
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr(sdata, "_fetch_index_frame",
                        lambda *args, **kwargs: calls.append(True) or fresh)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)

    out = sdata._load_index_daily("000300", "20200101")

    assert calls
    assert list(out["close"]) == [3010.0]


def test_index_sina_ignores_structurally_invalid_cache(monkeypatch):
    """399006 缓存缺少 close 列时，应重新请求新浪免费源。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cached = pd.DataFrame({"amount": [100.0]},
                          index=[pd.Timestamp(today)])
    live = [{"day": today, "close": "2.0", "volume": "20"}]
    hit = []
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: cached)

    class _Session:
        trust_env = False

        def get(self, *args, **kwargs):
            hit.append(True)
            return type("R", (), {"json": lambda self: live})()

    monkeypatch.setattr("requests.Session", lambda: _Session())
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)

    out = sdata.load_index_sina("399006", datalen=10)

    assert hit
    assert list(out["close"]) == [2.0]


def test_index_daily_full_ignores_structurally_invalid_cache(monkeypatch):
    """全量指数缓存缺少 close 列时，应重建免费数据而不是直接返回。"""
    today = pd.Timestamp(datetime.now().date())
    cached = pd.DataFrame({"amount": [100.0]}, index=[today])
    fresh = pd.DataFrame({"close": [3990.0], "amount": [200.0]},
                         index=[today])
    calls = []
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr(sdata, "_fetch_index_full_frame",
                        lambda *args, **kwargs: calls.append(True) or fresh)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)

    out = sdata.load_index_daily_full("399006", "20200101")

    assert calls
    assert list(out["close"]) == [3990.0]


def test_stock_loader_ignores_non_dataframe_cache(monkeypatch):
    """旭创缓存根节点类型错误时，应继续免费回退链而不是抛异常。"""
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: {"bad": True})
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_: pd.DataFrame())
    monkeypatch.setattr(sdata, "_fetch_tencent_daily", lambda *a, **k: None)
    monkeypatch.setattr(sdata, "_fetch_stock_daily", lambda *a, **k: None)

    assert sdata.load_stock_sina("300308").empty


def test_stock_loader_ignores_structurally_invalid_cache(monkeypatch):
    """旭创缓存缺少 close 列时，应重新走新浪免费源。"""
    today = pd.Timestamp(datetime.now().date())
    cached = pd.DataFrame({"open": [120.0] * 61},
                          index=pd.bdate_range(end=today, periods=61))
    fresh = _fresh_stock_history().set_index("date")
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_: fresh)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)

    out = sdata.load_stock_sina("300308")

    assert "close" in out.columns
    assert len(out) == len(fresh)


def test_panel_stock_loader_ignores_cache_missing_panel_columns(monkeypatch):
    """选股面板缓存缺少 amount 等列时，应重新走免费日线接口。"""
    today = pd.Timestamp(datetime.now().date())
    cached = pd.DataFrame({"close": [100.0]}, index=[today])
    fresh = pd.DataFrame({
        "open": [99.0], "close": [100.0], "high": [101.0], "low": [98.0],
        "volume": [1000.0], "amount": [100000.0], "turnover": [1.0],
    }, index=[today])
    calls = []
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr(sdata, "_fetch_stock_daily",
                        lambda *args, **kwargs: calls.append(True) or fresh)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)

    out = sdata._load_one_stock("600000", "20200101")

    assert calls
    assert list(out["amount"]) == [100000.0]


def test_panel_stock_loader_rejects_stale_first_full_response(monkeypatch):
    """首次全量返回过期日线时，不得缓存或交给选股面板。"""
    full = _fresh_stock_history().copy()
    full["date"] = pd.bdate_range(
        end=pd.Timestamp(datetime.now().date()) - timedelta(days=10),
        periods=len(full),
    )
    full = full.set_index("date")
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: None)
    monkeypatch.setattr(sdata, "_fetch_stock_daily", lambda *args, **kwargs: full)
    monkeypatch.setattr(sdata, "_cache_set",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("过期全量结果不得写入缓存")))
    monkeypatch.setattr(sdata.time, "sleep", lambda *_: None)

    assert sdata._load_one_stock("600000", "20200101").empty


def test_panel_stock_loader_rebuilds_string_index_cache(monkeypatch):
    """字符串日期索引缓存应按未命中处理，避免增量日期运算异常。"""
    cached = _fresh_stock_history().set_index("date")
    fresh = cached.copy()
    fresh.index = pd.to_datetime(fresh.index)
    calls = []
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr(sdata, "_fetch_stock_daily",
                        lambda *args, **kwargs: calls.append(True) or fresh)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr(sdata.time, "sleep", lambda *_: None)

    out = sdata._load_one_stock("600000", "20200101")

    assert calls
    assert out.index[-1] == pd.Timestamp(fresh.index[-1])


def test_panel_stock_loader_rejects_stale_cache_after_refresh_failures(monkeypatch):
    """增量和全量免费源均失败时，不得把过期个股缓存继续交给下游。"""
    stale = _fresh_stock_history().copy()
    stale["date"] = pd.bdate_range(
        end=pd.Timestamp(datetime.now().date()) - timedelta(days=20),
        periods=len(stale),
    )
    stale = stale.set_index("date")
    calls = []

    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: stale)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sdata,
        "_fetch_stock_daily",
        lambda *args, **kwargs: calls.append(True) or None,
    )

    out = sdata._load_one_stock("600000", "20200101")

    assert len(calls) == 2  # 增量失败后还应尝试一次全量重建
    assert out.empty


def test_cache_frame_rejects_invalid_values_on_latest_date():
    """缓存最新日期的必要字段无效时，不得因旧日期有效而放行。"""
    cached = _fresh_stock_history().copy()
    cached["date"] = pd.to_datetime(cached["date"])
    cached = cached.set_index("date")
    cached.iloc[-1, cached.columns.get_loc("close")] = float("nan")
    cached.iloc[-1, cached.columns.get_loc("amount")] = float("nan")

    assert not sdata._cache_frame_has_valid_columns(
        cached, sdata._STOCK_CACHE_COLUMNS)


def test_cache_frame_rejects_invalid_values_in_history():
    """滚动因子不能消费历史 NaN/Inf，即使最新一行完整也应拒绝缓存。"""
    cached = _fresh_stock_history().copy()
    cached["date"] = pd.to_datetime(cached["date"])
    cached = cached.set_index("date")
    cached.iloc[10, cached.columns.get_loc("close")] = float("nan")

    assert not sdata._cache_frame_has_valid_columns(
        cached, sdata._STOCK_CACHE_COLUMNS)


def test_fetch_index_full_frame_rejects_invalid_historical_row(monkeypatch):
    """指数源历史行出现非有限收盘价时，应回退而不是丢弃坏行后继续计算。"""
    import akshare as ak

    today = datetime.now().strftime("%Y-%m-%d")
    malformed = pd.DataFrame({
        "日期": ["2026-08-26", today],
        "收盘": [float("nan"), 2.0],
        "成交额": [10.0, 20.0],
    })
    fresh = pd.DataFrame({"date": [today], "close": [100.0], "amount": [30.0]})
    fresh.attrs["strategy_data_source"] = "tencent_volume"
    calls = []

    monkeypatch.setattr(ak, "index_zh_a_hist", lambda **_: malformed)
    monkeypatch.setattr(sdata, "_fetch_kline_direct",
                        lambda *a, **k: calls.append("direct") or None)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily",
                        lambda *a, **k: calls.append("tencent") or fresh)

    out = sdata._fetch_index_full_frame(
        "399006", "20200101", datetime.now().strftime("%Y%m%d"))

    assert calls == ["direct", "tencent"]
    assert out is not None
    assert out["close"].iloc[-1] == 100.0


def test_fetch_index_full_frame_accepts_datetime_index_fallback(monkeypatch):
    """指数回退源使用日期索引时，也应统一成可消费的结果。"""
    import akshare as ak

    today = pd.Timestamp(datetime.now().date())
    fresh = pd.DataFrame({"close": [100.0], "amount": [30.0]}, index=[today])
    calls = []

    monkeypatch.setattr(ak, "index_zh_a_hist", lambda **_: None)
    monkeypatch.setattr(sdata, "_fetch_kline_direct",
                        lambda *a, **k: calls.append("direct") or None)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily",
                        lambda *a, **k: calls.append("tencent") or fresh)

    out = sdata._fetch_index_full_frame(
        "399006", "20200101", datetime.now().strftime("%Y%m%d"))

    assert calls == ["direct", "tencent"]
    assert out is not None
    assert out.index[-1] == today
    assert out["close"].iloc[-1] == 100.0
    assert out.attrs["strategy_data_source"] == "tencent_volume"
    assert out.attrs["strategy_amount_unit"] == "hands"


def test_fetch_stock_daily_rejects_invalid_historical_field(monkeypatch):
    """个股源任一历史 OHLC/成交字段损坏时，应整源失败而非静默删行。"""
    import akshare as ak

    raw = pd.DataFrame({
        "日期": ["2026-08-27", "2026-08-28"],
        "开盘": [1.0, 1.1], "收盘": [1.1, 1.2],
        "最高": [1.2, 1.3], "最低": [0.9, 1.0],
        "成交量": [100.0, -1.0], "成交额": [1000.0, 1100.0],
        "换手率": [1.0, 1.1],
    })
    monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **_: raw)

    assert sdata._fetch_stock_daily(
        "300308", "20200101", "20260828") is None


def test_stock_loader_uses_beijing_date_for_api_end(monkeypatch):
    """UTC 次日凌晨请求免费行情时，截止日应使用北京时间日期。"""
    instant = datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    captured = {}
    frame = _mk_df("2026-08-27")
    monkeypatch.setattr(sdata, "datetime", _FakeDateTime)
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: None)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr(sdata.time, "sleep", lambda *_: None)

    def _fetch(code, start, end):
        captured["end"] = end
        return frame

    monkeypatch.setattr(sdata, "_fetch_stock_daily", _fetch)

    sdata._load_one_stock("600000", "20200101")

    assert captured["end"] == "20260828"


def test_fetch_stock_daily_skips_malformed_source(monkeypatch):
    """东财个股免费接口返回非表对象时，应按失败处理而非抛结构异常。"""
    import akshare as ak

    monkeypatch.setattr(ak, "stock_zh_a_hist", lambda **_: {"data": "bad"})

    assert sdata._fetch_stock_daily(
        "300308", "20200101", datetime.now().strftime("%Y%m%d")) is None


def test_index_daily_full_rejects_stale_bar_when_cache_metadata_is_fresh(monkeypatch):
    """缓存文件虽新但末根交易日过期时，免费源失败不得继续返回旧指数。"""
    stale = _mk_df(_stale_date())
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: stale)
    monkeypatch.setattr(sdata, "_fetch_index_full_frame",
                        lambda *a, **k: None)

    df = sdata.load_index_daily_full("399006", "20200101")

    assert df.empty


def test_index_daily_rejects_stale_cache_after_incremental_fetch_failure(monkeypatch):
    """增量补数失败后，旧指数缓存不得继续交给基金轮动使用。"""
    stale = _mk_df(_stale_date())
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: stale)
    monkeypatch.setattr(sdata, "_fetch_index_frame",
                        lambda *a, **k: None)

    df = sdata._load_index_daily("000300", "20200101")

    assert df.empty


def test_fetch_index_frame_skips_stale_source_and_uses_next_free_source(monkeypatch):
    """指数首个免费源返回旧根时，应继续尝试后续免费源。"""
    import akshare as ak

    stale_day = _stale_day_str()
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    stale = pd.DataFrame({"日期": [stale_day], "收盘": [1.0]})
    fresh = pd.DataFrame({"date": [fresh_day], "close": [2.0]})
    monkeypatch.setattr(ak, "index_zh_a_hist", lambda **_: stale)
    monkeypatch.setattr(ak, "stock_zh_index_daily_em", lambda **_: fresh)
    monkeypatch.setattr(ak, "stock_zh_index_daily",
                        lambda **_: (_ for _ in ()).throw(AssertionError(
                            "不应继续调用第三源")))

    out = sdata._fetch_index_frame("000300", "20200101",
                                   datetime.now().strftime("%Y%m%d"))

    assert out is not None
    assert out["close"].iloc[-1] == 2.0


def test_fetch_index_full_frame_skips_stale_source_and_uses_next_free_source(monkeypatch):
    """399006 全量链首源有旧数据时，也必须继续尝试后续免费源。"""
    import akshare as ak

    stale_day = _stale_day_str()
    fresh_day = datetime.now().strftime("%Y-%m-%d")
    stale = pd.DataFrame({"日期": [stale_day], "收盘": [1.0], "成交额": [10.0]})
    fresh = pd.DataFrame({"date": [fresh_day], "close": [2.0], "amount": [20.0]})
    calls = []

    monkeypatch.setattr(ak, "index_zh_a_hist", lambda **_: stale)
    monkeypatch.setattr(sdata, "_fetch_kline_direct",
                        lambda *a, **k: calls.append("direct") or None)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily",
                        lambda *a, **k: calls.append("tencent") or fresh)
    monkeypatch.setattr(ak, "stock_zh_index_daily_em",
                        lambda **_: (_ for _ in ()).throw(AssertionError(
                            "腾讯成功时不应继续调用后续源")))

    out = sdata._fetch_index_full_frame(
        "399006", "20200101", datetime.now().strftime("%Y%m%d"))

    assert out is not None
    assert out["close"].iloc[-1] == 2.0
    assert calls == ["direct", "tencent"]


def test_fetch_index_full_frame_rejects_stale_final_fallback(monkeypatch):
    """所有免费源都过期时，末级新浪响应也不得继续下传。"""
    import akshare as ak

    stale_day = _stale_day_str()
    stale = pd.DataFrame({
        "date": [stale_day], "close": [1.0], "amount": [10.0],
    })
    monkeypatch.setattr(ak, "index_zh_a_hist", lambda **_: stale)
    monkeypatch.setattr(sdata, "_fetch_kline_direct", lambda *a, **k: stale)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily", lambda *a, **k: stale)
    monkeypatch.setattr(ak, "stock_zh_index_daily_em", lambda **_: stale)
    monkeypatch.setattr(ak, "stock_zh_index_daily", lambda **_: stale)

    assert sdata._fetch_index_full_frame(
        "399006", "20200101", datetime.now().strftime("%Y%m%d")) is None


def test_fetch_index_full_frame_skips_malformed_numeric_source(monkeypatch):
    """日期虽新但收盘价非数值时，必须继续回退免费源。"""
    import akshare as ak

    today = datetime.now().strftime("%Y-%m-%d")
    malformed = pd.DataFrame({
        "日期": [today], "收盘": ["not-a-number"], "成交额": [10.0],
    })
    fresh = pd.DataFrame({
        "date": [today], "close": [2.0], "amount": [20.0],
    })
    calls = []

    monkeypatch.setattr(ak, "index_zh_a_hist", lambda **_: malformed)
    monkeypatch.setattr(sdata, "_fetch_kline_direct",
                        lambda *a, **k: calls.append("direct") or None)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily",
                        lambda *a, **k: calls.append("tencent") or fresh)

    out = sdata._fetch_index_full_frame(
        "399006", "20200101", datetime.now().strftime("%Y%m%d"))

    assert calls == ["direct", "tencent"]
    assert out is not None
    assert out["close"].iloc[-1] == 2.0


def test_fetch_index_full_frame_skips_non_dataframe_source(monkeypatch):
    """指数首个免费接口返回非表对象时，必须继续回退而不能在.empty处崩溃。"""
    import akshare as ak

    today = datetime.now().strftime("%Y-%m-%d")
    fresh = pd.DataFrame({
        "date": [today], "close": [2.0], "amount": [20.0],
    })
    calls = []

    monkeypatch.setattr(ak, "index_zh_a_hist", lambda **_: {"data": "bad"})
    monkeypatch.setattr(sdata, "_fetch_kline_direct",
                        lambda *a, **k: calls.append("direct") or None)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily",
                        lambda *a, **k: calls.append("tencent") or fresh)

    out = sdata._fetch_index_full_frame(
        "399006", "20200101", datetime.now().strftime("%Y%m%d"))

    assert calls == ["direct", "tencent"]
    assert out is not None
    assert out["close"].iloc[-1] == 2.0


def test_fetch_index_full_frame_rejects_invalid_latest_close(monkeypatch):
    """最新交易日收盘为空时，不能接受同一响应中的旧收盘价。"""
    import akshare as ak

    today = datetime.now().strftime("%Y-%m-%d")
    older = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    malformed = pd.DataFrame({
        "日期": [older, today],
        "收盘": [1.0, "not-a-number"],
        "成交额": [10.0, 20.0],
    })
    fresh = pd.DataFrame({
        "date": [today], "close": [2.0], "amount": [20.0],
    })
    calls = []

    monkeypatch.setattr(ak, "index_zh_a_hist", lambda **_: malformed)
    monkeypatch.setattr(sdata, "_fetch_kline_direct",
                        lambda *a, **k: calls.append("direct") or None)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily",
                        lambda *a, **k: calls.append("tencent") or fresh)

    out = sdata._fetch_index_full_frame(
        "399006", "20200101", datetime.now().strftime("%Y%m%d"))

    assert calls == ["direct", "tencent"]
    assert out is not None
    assert out.index[-1] == pd.Timestamp(today)
    assert out["close"].iloc[-1] == 2.0


def test_stock_sina_rejects_stale_bar_when_cache_metadata_is_fresh(monkeypatch):
    """旭创缓存末根过期时免费源失败不得继续参与双确认。"""
    stale = _mk_df((datetime.now() - timedelta(days=5)).date())
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: stale)
    calls = []

    def empty_source(**_kwargs):
        calls.append(True)
        return pd.DataFrame()

    monkeypatch.setattr("akshare.stock_zh_a_daily", empty_source)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily", lambda *a, **k: None)
    monkeypatch.setattr(sdata, "_fetch_stock_daily", lambda *a, **k: None)

    df = sdata.load_stock_sina("300308")

    assert calls
    assert df.empty


def test_stock_sina_refreshes_stale_sufficient_cache(monkeypatch):
    """结构完整但末根过期的足够长缓存不得直接返回。"""
    stale = _fresh_stock_history().copy()
    stale["date"] = pd.bdate_range(
        end=pd.Timestamp(datetime.now().date()) - timedelta(days=10),
        periods=len(stale),
    )
    stale = stale.set_index("date")
    fresh = _fresh_stock_history().set_index("date")
    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: stale)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_kwargs: fresh)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("新鲜新浪响应成功时不应调用腾讯")))
    monkeypatch.setattr(sdata, "_fetch_stock_daily", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("新鲜新浪响应成功时不应调用东财")))

    out = sdata.load_stock_sina("300308")

    assert out.index[-1] == pd.Timestamp(fresh.index[-1])


def test_stock_sina_rejects_stale_live_response(monkeypatch):
    """新浪旭创接口返回旧日线时不得进入双确认。"""
    stale_day = pd.Timestamp(datetime.now() - timedelta(days=5)).normalize()
    raw = pd.DataFrame({
        "open": [1.0], "close": [1.0], "high": [1.0], "low": [1.0],
        "volume": [100.0], "amount": [100.0], "turnover": [1.0],
    }, index=pd.DatetimeIndex([stale_day], name="date"))
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_kwargs: raw)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily", lambda *a, **k: None)
    monkeypatch.setattr(sdata, "_fetch_stock_daily", lambda *a, **k: None)

    assert sdata.load_stock_sina("300308").empty


def test_stock_sina_falls_back_to_tencent_daily(monkeypatch):
    """新浪旭创日线失败时，应使用腾讯免费日K继续提供双确认数据。"""
    today = datetime.now().strftime("%Y-%m-%d")
    fallback = _fresh_stock_history()
    fallback.loc[fallback.index[-1], "date"] = today
    fallback.loc[fallback.index[-1], "close"] = 123.4
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    monkeypatch.setattr("akshare.stock_zh_a_daily",
                        lambda **_kwargs: (_ for _ in ()).throw(
                            OSError("sina unavailable")))
    calls = []

    def fake_tencent(*args, **kwargs):
        calls.append(args[0])
        return fallback

    monkeypatch.setattr(sdata, "_fetch_tencent_daily", fake_tencent)
    monkeypatch.setattr(sdata, "_fetch_stock_daily",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("腾讯成功时不应调用东财")))

    out = sdata.load_stock_sina("300308")

    assert calls == ["sz300308"]
    assert out.index[-1] == pd.Timestamp(today)
    assert out["close"].iloc[-1] == 123.4


def test_stock_sina_falls_back_to_eastmoney_after_stale_tencent(monkeypatch):
    """腾讯也返回过期数据时，应继续尝试东财免费历史日K。"""
    today = datetime.now().strftime("%Y-%m-%d")
    stale = pd.DataFrame({
        "date": [(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")],
        "close": [111.0], "amount": [900.0],
    })
    fresh = _fresh_stock_history()
    fresh.loc[fresh.index[-1], "date"] = today
    fresh.loc[fresh.index[-1], "open"] = 120.0
    fresh.loc[fresh.index[-1], "close"] = 121.0
    fresh.loc[fresh.index[-1], "high"] = 122.0
    fresh.loc[fresh.index[-1], "low"] = 119.0
    fresh = fresh.set_index("date")
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_kwargs: pd.DataFrame())
    monkeypatch.setattr(sdata, "_fetch_tencent_daily", lambda *a, **k: stale)
    monkeypatch.setattr(sdata, "_fetch_stock_daily", lambda *a, **k: fresh)

    out = sdata.load_stock_sina("300308")

    assert out.index[-1] == pd.Timestamp(today)
    assert out["close"].iloc[-1] == 121.0


def test_stock_sina_does_not_call_fallbacks_when_sina_is_fresh(monkeypatch):
    """新浪返回新鲜数据时，不应额外请求腾讯和东财，避免无谓限流。"""
    sina = _fresh_stock_history()
    sina.loc[sina.index[-1], "close"] = 121.0
    sina = sina.set_index("date")
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_kwargs: sina)
    monkeypatch.setattr(sdata, "_fetch_tencent_daily", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("新浪成功时不应调用腾讯")))
    monkeypatch.setattr(sdata, "_fetch_stock_daily", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("新浪成功时不应调用东财")))

    out = sdata.load_stock_sina("300308")

    assert out["close"].iloc[-1] == 121.0


def _fresh_stock_history(n=61):
    """构造足够支撑旭创双确认的完整日线历史。"""
    dates = pd.bdate_range(end=pd.Timestamp(datetime.now().date()), periods=n)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": [120.0] * n,
        "close": [121.0] * n,
        "high": [122.0] * n,
        "low": [119.0] * n,
        "volume": [100.0] * n,
        "amount": [10000.0] * n,
        "turnover": [1.0] * n,
    })


def test_stock_sina_continues_after_fresh_but_short_sina_history(monkeypatch):
    """新浪末根虽新鲜但完整历史不足时，不得静默关闭旭创双确认。"""
    today = datetime.now().strftime("%Y-%m-%d")
    short_sina = pd.DataFrame({"date": [today], "close": [123.4]})
    fallback = _fresh_stock_history()
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_kwargs: short_sina)
    calls = []

    def fake_tencent(*args, **kwargs):
        calls.append("tencent")
        return fallback

    monkeypatch.setattr(sdata, "_fetch_tencent_daily", fake_tencent)
    monkeypatch.setattr(sdata, "_fetch_stock_daily", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("腾讯提供足够历史时不应调用东财")))

    out = sdata.load_stock_sina("300308")

    assert calls == ["tencent"]
    assert len(out) == len(fallback)


def test_stock_sina_rejects_history_that_is_short_after_partial_bar(monkeypatch):
    """含当日 partial bar 的 60 根响应剔除后不足 60 根，必须继续回退。

    周末敏感性修复：数据末根由 bdate_range 生成，周六/周日运行时末根会是上一个
    工作日而非"今天"，导致"末根=当日 partial"的前提失效、充分性校验不再 +1。
    这里把数据层取"今天"的时钟固定到数据末根日期，使断言不依赖真实运行日历。
    """
    short_sina = _fresh_stock_history(60)
    fallback = _fresh_stock_history(61)
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_kwargs: short_sina)
    calls = []

    def fake_tencent(*args, **kwargs):
        calls.append("tencent")
        return fallback

    monkeypatch.setattr(sdata, "_fetch_tencent_daily", fake_tencent)

    _bar_day = short_sina["date"].iloc[-1]

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(f"{_bar_day}T14:45:00")

    monkeypatch.setattr(sdata, "datetime", _FakeDT)

    out = sdata.load_stock_sina("300308")

    assert calls == ["tencent"]
    assert len(out) == len(fallback)


def test_fetch_stock_daily_allows_explicit_adjustment(monkeypatch):
    """东财个股抓取器应允许旭创回退链显式选择前复权。"""
    import akshare as ak

    captured = {}
    raw = pd.DataFrame({
        "日期": ["2026-08-27"], "开盘": [1.0], "收盘": [1.1],
        "最高": [1.2], "最低": [0.9], "成交量": [100.0],
        "成交额": [1000.0], "换手率": [1.0],
    })

    def fake_hist(**kwargs):
        captured["adjust"] = kwargs["adjust"]
        return raw

    monkeypatch.setattr(ak, "stock_zh_a_hist", fake_hist)

    out = sdata._fetch_stock_daily("300308", "20260101", "20260828", adjust="qfq")

    assert captured["adjust"] == "qfq"
    assert out["close"].iloc[-1] == 1.1


def test_stock_sina_eastmoney_fallback_requests_qfq(monkeypatch):
    """旭创东财回退必须使用前复权，避免与新浪/腾讯口径混用。"""
    monkeypatch.setattr(sdata, "_cache_get", lambda key, ttl_days=None: None)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **_kwargs: pd.DataFrame())
    monkeypatch.setattr(sdata, "_fetch_tencent_daily", lambda *a, **k: None)
    captured = {}
    fallback = _fresh_stock_history()

    def fake_eastmoney(code, start, end, **kwargs):
        captured["adjust"] = kwargs.get("adjust")
        return fallback.set_index("date")

    monkeypatch.setattr(sdata, "_fetch_stock_daily", fake_eastmoney)

    out = sdata.load_stock_sina("300308")

    assert captured["adjust"] == "qfq"
    assert len(out) == len(fallback)


def _dated_frame(dates, amounts, source=None):
    frame = pd.DataFrame({"close": [100.0] * len(dates), "amount": amounts},
                         index=pd.DatetimeIndex(dates))
    if source is not None:
        frame.attrs["strategy_data_source"] = source
    return frame


def test_index_full_rebuilds_when_incremental_source_changes(monkeypatch):
    """指数缓存源切换时应整段重建，禁止成交额与成交量跨源拼接。"""
    today = pd.Timestamp(datetime.now().date())
    yesterday = today - pd.Timedelta(days=1)
    cached = _dated_frame([yesterday], [1_000_000.0], "eastmoney_hist")
    incremental = _dated_frame([today], [200.0], "tencent_volume")
    rebuilt = _dated_frame([yesterday, today], [10.0, 20.0], "tencent_volume")
    calls = []

    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)

    def fake_fetch(symbol, start, end):
        calls.append(start)
        return incremental if len(calls) == 1 else rebuilt

    monkeypatch.setattr(sdata, "_fetch_index_full_frame", fake_fetch)

    out = sdata.load_index_daily_full("399006", "20200101")

    assert calls == [(yesterday + pd.Timedelta(days=1)).strftime("%Y%m%d"),
                     "20200101"]
    assert list(out["amount"]) == [10.0, 20.0]
    assert out.attrs["strategy_data_source"] == "tencent_volume"


def test_index_full_rebuilds_when_cached_source_is_unknown(monkeypatch):
    """旧缓存无来源标记时不得直接与新源增量拼接。"""
    today = pd.Timestamp(datetime.now().date())
    yesterday = today - pd.Timedelta(days=1)
    cached = _dated_frame([yesterday], [1_000_000.0])
    rebuilt = _dated_frame([yesterday, today], [10.0, 20.0], "sina_volume")
    calls = []

    monkeypatch.setattr(sdata, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr(sdata, "_cache_set", lambda *args, **kwargs: None)

    def fake_fetch(symbol, start, end):
        calls.append(start)
        return rebuilt

    monkeypatch.setattr(sdata, "_fetch_index_full_frame", fake_fetch)

    out = sdata.load_index_daily_full("399006", "20200101")

    assert calls == ["20200101"]
    assert list(out["amount"]) == [10.0, 20.0]
