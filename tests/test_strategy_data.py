# -*- coding: utf-8 -*-
"""数据层缓存单元测试（tmp 目录隔离，无网络）"""
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
