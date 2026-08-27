# -*- coding: utf-8 -*-
"""test_data_tushare.py — SNA-01 Tushare 付费优先通道单测（全 mock，零网络）。

覆盖验收 ①-③：token 配置时优先走 Tushare 且返回同构 DataFrame；token 缺失/
过期/接口失败自动降级免费源且不抛错；双通道 mock 覆盖。
验收 ⑤（真实 token 的 399006 一致性抽查）待用户提供 token 后执行。
"""
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import data as sdata  # noqa: E402
from src.strategy import index_pe as ipe  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------- 替身 ----------------

class _FakePro:
    """tushare pro api 替身：index_daily 可编程返回/抛错。"""

    def __init__(self, index_ret=None, index_exc=None, dailybasic_ret=None):
        self.index_ret = index_ret
        self.index_exc = index_exc
        self.dailybasic_ret = dailybasic_ret
        self.calls = []

    def index_daily(self, **kw):
        self.calls.append(("index_daily", kw))
        if self.index_exc:
            raise self.index_exc
        return self.index_ret

    def query(self, *a, **kw):
        return pd.DataFrame()

    def index_dailybasic(self, **kw):
        self.calls.append(("index_dailybasic", kw))
        if isinstance(self.dailybasic_ret, Exception):
            raise self.dailybasic_ret
        return self.dailybasic_ret


def _tsu_index_raw(n=5):
    """tushare index_daily 原始返回形状（trade_date YYYYMMDD / vol 单位列）。"""
    return pd.DataFrame({
        "ts_code": ["399006.SZ"] * n,
        "trade_date": [f"2026082{i}" for i in range(n)],
        "close": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "vol": [10000.0 * (i + 1) for i in range(n)],
        "amount": [1e6 * (i + 1) for i in range(n)],
    })


def _patch_cache(monkeypatch):
    """磁盘缓存隔离：get 恒空（强制走取数路径），set 仅记录。"""
    written = []
    monkeypatch.setattr(sdata, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(sdata, "_cache_set", lambda k, obj: written.append((k, obj)))
    return written


@pytest.fixture(autouse=True)
def _reset_tsu_singleton(monkeypatch):
    """_tushare_client 是进程级单例+负缓存：用例间互相污染（前例失败态
    会让后例拿到 None），每个用例前强制归零。"""
    monkeypatch.setattr(sdata, "_TSU_TRIED", False)
    monkeypatch.setattr(sdata, "_TSU_PRO", None)


# ---------------- _tushare_client ----------------

def test_tushare_client_no_token(monkeypatch):
    monkeypatch.setattr(sdata.os, "getenv", lambda k, d="": "")
    assert sdata._tushare_client() is None


def test_tushare_client_init_fail_negative_cache(monkeypatch):
    monkeypatch.setattr(sdata.os, "getenv",
                        lambda k, d="": "bad-token" if k == "TUSHARE_TOKEN" else d)
    fake_ts = types.ModuleType("tushare")
    fake_ts.pro_api = lambda tok: (_ for _ in ()).throw(RuntimeError("init fail"))
    monkeypatch.setitem(sys.modules, "tushare", fake_ts)
    assert sdata._tushare_client() is None
    # 负缓存：二次调用不再重试（pro_api 只会被 import 一次路径）
    assert sdata._tushare_client() is None


def test_tushare_client_ok(monkeypatch):
    monkeypatch.setattr(sdata.os, "getenv",
                        lambda k, d="": "good-token" if k == "TUSHARE_TOKEN" else d)
    fake_ts = types.ModuleType("tushare")
    fake_ts.pro_api = lambda tok: _FakePro()
    monkeypatch.setitem(sys.modules, "tushare", fake_ts)
    pro = sdata._tushare_client()
    assert pro is not None
    assert sdata._tushare_client() is pro  # 进程级单例


# ---------------- load_index_primary ----------------

def test_index_primary_no_token_falls_to_sina(monkeypatch):
    _patch_cache(monkeypatch)
    monkeypatch.setattr(sdata, "_tushare_client", lambda: None)
    sina_df = pd.DataFrame({"close": [1.0], "amount": [2.0]},
                           index=pd.to_datetime(["2026-08-26"]))
    called = []
    monkeypatch.setattr(sdata, "load_index_sina",
                        lambda *a, **k: called.append(a) or sina_df)
    df = sdata.load_index_primary("399006")
    assert len(df) == 1 and called, "无 token 时应走新浪链"


def test_index_primary_tushare_schema(monkeypatch):
    """验收①：token 配置时优先 Tushare，返回与新浪链同构 [close, amount, high, low]。"""
    _patch_cache(monkeypatch)
    pro = _FakePro(index_ret=_tsu_index_raw())
    monkeypatch.setattr(sdata, "_tushare_client", lambda: pro)
    sina_called = []
    monkeypatch.setattr(sdata, "load_index_sina",
                        lambda *a, **k: sina_called.append(1) or pd.DataFrame())
    df = sdata.load_index_primary("399006")
    assert not sina_called, "Tushare 成功时不应触达新浪"
    assert list(df.columns) == ["close", "amount", "high", "low"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.is_monotonic_increasing
    # vol → amount 列映射（量纲无关口径，与腾讯链约定一致）
    assert df["amount"].iloc[0] == 10000.0
    assert pro.calls and pro.calls[0][1]["ts_code"] == "399006.SZ"


def test_index_primary_tushare_exception_falls_back(monkeypatch):
    """验收②：接口抛错自动降级新浪，不抛异常。"""
    _patch_cache(monkeypatch)
    pro = _FakePro(index_exc=RuntimeError("network"))
    monkeypatch.setattr(sdata, "_tushare_client", lambda: pro)
    sina_df = pd.DataFrame({"close": [9.0], "amount": [9.0]},
                           index=pd.to_datetime(["2026-08-26"]))
    monkeypatch.setattr(sdata, "load_index_sina", lambda *a, **k: sina_df)
    df = sdata.load_index_primary("399006")
    assert len(df) == 1 and df["close"].iloc[0] == 9.0


def test_index_primary_tushare_empty_falls_back(monkeypatch):
    _patch_cache(monkeypatch)
    pro = _FakePro(index_ret=pd.DataFrame())
    monkeypatch.setattr(sdata, "_tushare_client", lambda: pro)
    sina_df = pd.DataFrame({"close": [7.0], "amount": [7.0]},
                           index=pd.to_datetime(["2026-08-26"]))
    monkeypatch.setattr(sdata, "load_index_sina", lambda *a, **k: sina_df)
    assert len(sdata.load_index_primary("399006")) == 1


def test_index_primary_cached_fresh_skips_fetch(monkeypatch):
    """缓存末根 ≥ 昨日 → 直接命中，不触达 Tushare（省 API 配额）。"""
    import datetime as _dt
    written = []
    monkeypatch.setattr(sdata, "_cache_get", lambda *a, **k: pd.DataFrame(
        {"close": [1.0], "amount": [1.0]},
        index=pd.to_datetime([_dt.date.today()])))
    monkeypatch.setattr(sdata, "_cache_set", lambda k, obj: written.append(k))
    pro = _FakePro(index_ret=_tsu_index_raw())
    monkeypatch.setattr(sdata, "_tushare_client", lambda: pro)
    df = sdata.load_index_primary("399006")
    assert len(df) == 1 and not pro.calls and not written


# ---------------- load_stock_primary ----------------

def test_stock_primary_tushare_pro_bar(monkeypatch):
    """个股链：pro_bar(adj=qfq) 优先，列含 close/趋势所需字段。"""
    _patch_cache(monkeypatch)
    monkeypatch.setattr(sdata, "_tushare_client", lambda: _FakePro())
    raw = pd.DataFrame({
        "ts_code": ["300308.SZ"] * 3,
        "trade_date": ["20260825", "20260826", "20260827"],
        "open": [10.0, 10.5, 11.0],
        "close": [10.2, 10.8, 11.2],
        "high": [10.5, 11.0, 11.4],
        "low": [10.0, 10.4, 10.9],
        "vol": [1000.0, 1100.0, 1200.0],
        "amount": [1020.0, 1188.0, 1344.0],
    })
    fake_ts = types.ModuleType("tushare")
    fake_ts.pro_bar = lambda **kw: raw
    monkeypatch.setitem(sys.modules, "tushare", fake_ts)
    sina_called = []
    monkeypatch.setattr(sdata, "load_stock_sina",
                        lambda *a, **k: sina_called.append(1) or pd.DataFrame())
    df = sdata.load_stock_primary("300308")
    assert not sina_called
    assert "close" in df.columns and "volume" in df.columns
    assert isinstance(df.index, pd.DatetimeIndex)


def test_stock_primary_no_token_falls_to_sina(monkeypatch):
    _patch_cache(monkeypatch)
    monkeypatch.setattr(sdata, "_tushare_client", lambda: None)
    sina_df = pd.DataFrame({"close": [5.0]},
                           index=pd.to_datetime(["2026-08-26"]))
    monkeypatch.setattr(sdata, "load_stock_sina", lambda *a, **k: sina_df)
    assert sdata.load_stock_primary("300308")["close"].iloc[0] == 5.0


# ---------------- _fresh_by_last_bar ----------------

def test_fresh_by_last_bar_boundary():
    import datetime as _dt
    today = _dt.date.today()
    fresh = pd.DataFrame({"close": [1.0]},
                         index=pd.to_datetime([today - _dt.timedelta(days=1)]))
    stale = pd.DataFrame({"close": [1.0]},
                         index=pd.to_datetime([today - _dt.timedelta(days=3)]))
    assert sdata._fresh_by_last_bar(fresh) is True
    assert sdata._fresh_by_last_bar(stale) is False
    assert sdata._fresh_by_last_bar(pd.DataFrame()) is False


# ---------------- index_pe Tushare 备份链 ----------------

def test_pe_tushare_fallback_date_format(monkeypatch):
    """乐咕失败时 Tushare 兜底；日期键必须转 YYYY-MM-DD（否则永不 align）。"""
    monkeypatch.setattr(sdata, "_tushare_client", lambda: _FakePro(
        dailybasic_ret=pd.DataFrame({
            "trade_date": ["20260825", "20260826"],
            "pe": [30.5, 31.2]})))
    rows = ipe._fetch_pe_tushare()
    assert rows == {"2026-08-25": 30.5, "2026-08-26": 31.2}


def test_pe_tushare_fallback_disabled_without_token(monkeypatch):
    monkeypatch.setattr(sdata, "_tushare_client", lambda: None)
    assert ipe._fetch_pe_tushare() == {}


def test_pe_tushare_exception_returns_empty(monkeypatch):
    monkeypatch.setattr(sdata, "_tushare_client",
                        lambda: _FakePro(dailybasic_ret=RuntimeError("api err")))
    assert ipe._fetch_pe_tushare() == {}


def test_pe_load_falls_to_tushare_when_legu_empty(monkeypatch, tmp_path):
    """端到端备份链：缓存缺失 + 乐咕空 → Tushare 结果被采用并写缓存。"""
    monkeypatch.setattr(ipe, "_fetch_pe_tushare",
                        lambda: {"2026-08-26": 30.5})
    import akshare  # noqa: F401  确认可导入后统一 mock 掉
    monkeypatch.setattr("akshare.stock_index_pe_lg", lambda symbol: pd.DataFrame())
    rows = ipe.load_cy50_pe(cache_dir=tmp_path)
    assert rows == {"2026-08-26": 30.5}
