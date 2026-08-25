# -*- coding: utf-8 -*-
"""数据层缓存单元测试（tmp 目录隔离，无网络）"""
from datetime import datetime, timedelta

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


def test_index_sina_cache_stale_refetch(monkeypatch):
    """缓存末根早于昨天 → 判为陈旧，强制重拉（修复隔日滞后）"""
    stale = _mk_df((datetime.now() - timedelta(days=5)).date())
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
