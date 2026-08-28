# -*- coding: utf-8 -*-
"""P9 数据源加固测试（2026-08-20）：东财K线源 + UA轮换 + 三冗余降级链

覆盖：
- _em_secid：sina/腾讯 symbol → 东财 secid 映射
- _fetch_kline_em：CSV 解析（×100 手转股、升序、尾部截取）、多主机 fallback、
  全主机失败返回 []、非法 symbol 快速失败
- fetch_index_kline：新浪 → 腾讯 → 东财 三冗余降级链
- _get_ua / _http_get：UA 轮换池
全部 mock，不触网。
"""
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factor_collector import (  # noqa: E402
    _em_secid,
    _fetch_kline_em,
    _fetch_kline_sina,
    _fetch_kline_tencent,
    fetch_index_kline,
    _get_ua,
    _UA_POOL,
    _http_get,
)

pytestmark = pytest.mark.unit


# ============================================================
# secid 映射
# ============================================================
def test_em_secid_mapping():
    assert _em_secid("sh000001") == "1.000001"
    assert _em_secid("sz399006") == "0.399006"
    assert _em_secid("sh600183") == "1.600183"
    assert _em_secid("sz300308") == "0.300308"


def test_em_secid_invalid():
    assert _em_secid("bj430047") == ""   # 北交所未映射（当前因子仅沪深指数）
    assert _em_secid("") == ""
    assert _em_secid("bad") == ""
    assert _em_secid(None) == ""


# ============================================================
# 东财K线解析（mock _http_get）
# ============================================================
_EM_JSON = {
    "rc": 0,
    "data": {
        "code": "000001",
        "klines": [
            "2026-08-18,3900,3901,3905,3895,100000,3.9e8,0.26,0.10,4.00,0.15",
            "2026-08-19,3901,3894,3910,3888,120000,4.6e8,-0.18,-0.18,-7.00,0.20",
            "2026-08-20,3907,3900,3925,3888,150000,5.9e8,0.15,0.15,6.00,0.25",
        ],
    },
}


def test_fetch_kline_em_parses_csv(monkeypatch):
    """解析正确：字段顺序、手→股 ×100、升序保留"""
    calls = {"n": 0, "url": "", "params": None}

    def fake_get(url, params=None, headers=None, encoding=None, rotate_ua=True):
        calls["n"] += 1
        calls["url"] = url
        calls["params"] = params
        import json
        return json.dumps(_EM_JSON)

    monkeypatch.setattr("factor_collector._http_get", fake_get)
    out = _fetch_kline_em("sh000001", 3)
    assert len(out) == 3
    assert out[0]["date"] == "2026-08-18"
    assert out[-1]["date"] == "2026-08-20"
    assert out[-1]["open"] == 3907.0
    assert out[-1]["close"] == 3900.0
    assert out[-1]["volume"] == 150000 * 100  # 手 → 股
    # 请求参数检查
    assert calls["params"]["secid"] == "1.000001"
    assert calls["params"]["klt"] == "101"
    assert calls["params"]["fqt"] == "0"


def test_fetch_kline_em_tail_truncate(monkeypatch):
    """lmt 上游忽略：本地截取尾部 N 根（窗口内多于 N 根时）"""
    rows = []
    for i in range(1, 31):  # 30 根 > lmt=5
        rows.append(f"2026-07-{i:02d},100,100,101,99,1000,1e6,0,0,0,0")
    payload = {"rc": 0, "data": {"code": "000001", "klines": rows}}

    monkeypatch.setattr("factor_collector._http_get",
                        lambda *a, **k: __import__("json").dumps(payload))
    out = _fetch_kline_em("sh000001", 5)
    assert len(out) == 5
    assert out[-1]["date"] == "2026-07-30"


def test_fetch_kline_em_host_fallback(monkeypatch):
    """多主机 fallback：首个主机返回空 → 换下一台成功"""
    seq = [""]  # 第一台失败（空响应）
    import json

    def fake_get(url, params=None, headers=None, encoding=None, rotate_ua=True):
        if seq:
            return seq.pop(0)
        return json.dumps(_EM_JSON)

    monkeypatch.setattr("factor_collector._http_get", fake_get)
    out = _fetch_kline_em("sh000001", 3)
    assert len(out) == 3  # 第二台主机成功


def test_fetch_kline_em_all_hosts_fail(monkeypatch):
    """全部主机失败 → 返回 []"""
    monkeypatch.setattr("factor_collector._http_get", lambda *a, **k: "")
    out = _fetch_kline_em("sh000001", 3)
    assert out == []


def test_fetch_kline_em_bad_symbol(monkeypatch):
    """非法 symbol 快速失败：不发起任何请求"""
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return "{}"

    monkeypatch.setattr("factor_collector._http_get", fake_get)
    assert _fetch_kline_em("bad", 3) == []
    assert calls["n"] == 0


def test_fetch_kline_em_adjust_param(monkeypatch):
    """fqt 复权参数透传"""
    got = {}

    def fake_get(url, params=None, headers=None, encoding=None, rotate_ua=True):
        got.update(params or {})
        import json
        return json.dumps(_EM_JSON)

    monkeypatch.setattr("factor_collector._http_get", fake_get)
    _fetch_kline_em("sh000001", 3, adjust=1)
    assert got["fqt"] == "1"


# ============================================================
# 三冗余降级链（新浪 → 腾讯 → 东财）
# ============================================================
def test_fetch_index_kline_sina_first(monkeypatch):
    """新浪可用 → 不触腾讯/东财"""
    calls = {"sina": 0, "tencent": 0, "em": 0}

    today = date.today().isoformat()

    def fake_sina(symbol, lmt):
        calls["sina"] += 1
        return [{"date": today, "open": 1, "close": 2, "high": 3, "low": 1, "volume": 100}]

    def fake_tencent(symbol, lmt):
        calls["tencent"] += 1
        return []

    def fake_em(symbol, lmt):
        calls["em"] += 1
        return []

    monkeypatch.setattr("factor_collector._fetch_kline_sina", fake_sina)
    monkeypatch.setattr("factor_collector._fetch_kline_tencent", fake_tencent)
    monkeypatch.setattr("factor_collector._fetch_kline_em", fake_em)
    out = fetch_index_kline("sh000001", 5)
    assert len(out) == 1
    assert calls == {"sina": 1, "tencent": 0, "em": 0}


def test_fetch_index_kline_tier2_tencent(monkeypatch):
    """新浪失败 → 腾讯兜底"""
    calls = {"sina": 0, "tencent": 0, "em": 0}
    today = date.today().isoformat()

    monkeypatch.setattr("factor_collector._fetch_kline_sina",
                        lambda s, l: calls.__setitem__("sina", calls["sina"] + 1) or [])
    monkeypatch.setattr("factor_collector._fetch_kline_tencent",
                        lambda s, l: calls.__setitem__("tencent", calls["tencent"] + 1) or
                        [{"date": today, "open": 1, "close": 2, "high": 3, "low": 1, "volume": 100}])
    monkeypatch.setattr("factor_collector._fetch_kline_em",
                        lambda s, l: calls.__setitem__("em", calls["em"] + 1) or [])
    out = fetch_index_kline("sh000001", 5)
    assert len(out) == 1
    assert calls == {"sina": 1, "tencent": 1, "em": 0}


def test_fetch_index_kline_tier3_em(monkeypatch):
    """新浪、腾讯均失败 → 东财兜底（P9 新增第三冗余）"""
    today = date.today().isoformat()
    monkeypatch.setattr("factor_collector._fetch_kline_sina", lambda s, l: [])
    monkeypatch.setattr("factor_collector._fetch_kline_tencent", lambda s, l: [])
    monkeypatch.setattr("factor_collector._fetch_kline_em", lambda s, l: [
        {"date": today, "open": 3907, "close": 3900, "high": 3925, "low": 3888, "volume": 15000000}])
    out = fetch_index_kline("sh000001", 5)
    assert len(out) == 1
    assert out[0]["date"] == today


def test_fetch_index_kline_rejects_stale_source(monkeypatch):
    """所有免费源只返回旧 K 线时，不能把旧数据交给技术因子。"""
    stale = [{"date": "2020-01-02", "open": 1, "close": 2,
              "high": 3, "low": 1, "volume": 100}]
    calls = {"sina": 0, "tencent": 0, "em": 0}

    def _stale(name):
        def _load(symbol, lmt):
            calls[name] += 1
            return stale
        return _load

    monkeypatch.setattr("factor_collector._fetch_kline_sina", _stale("sina"))
    monkeypatch.setattr("factor_collector._fetch_kline_tencent", _stale("tencent"))
    monkeypatch.setattr("factor_collector._fetch_kline_em", _stale("em"))

    assert fetch_index_kline("sh000001", 5) == []
    assert calls == {"sina": 1, "tencent": 1, "em": 1}


def test_fetch_index_kline_all_fail(monkeypatch):
    """三源全失败 → 返回 []（不抛异常）"""
    monkeypatch.setattr("factor_collector._fetch_kline_sina", lambda s, l: [])
    monkeypatch.setattr("factor_collector._fetch_kline_tencent", lambda s, l: [])
    monkeypatch.setattr("factor_collector._fetch_kline_em", lambda s, l: [])
    assert fetch_index_kline("sh000001", 5) == []


def test_fetch_index_kline_continues_after_source_exception(monkeypatch):
    """首个免费源抛异常时，调度器仍应继续尝试后续免费源。"""
    today = date.today().isoformat()

    def broken_sina(symbol, lmt):
        raise OSError("sina unavailable")

    monkeypatch.setattr("factor_collector._fetch_kline_sina", broken_sina)
    monkeypatch.setattr("factor_collector._fetch_kline_tencent", lambda s, l: [
        {"date": today, "open": 1, "close": 2, "high": 3,
         "low": 1, "volume": 100}])
    monkeypatch.setattr("factor_collector._fetch_kline_em", lambda s, l: [
        {"date": today, "open": 1, "close": 2, "high": 3,
         "low": 1, "volume": 100}])

    out = fetch_index_kline("sh000001", 5)

    assert len(out) == 1
    assert out[0]["date"] == today


# ============================================================
# UA 轮换池
# ============================================================
def test_get_ua_rotates_within_pool():
    """轮换返回池内 UA，且多次调用覆盖池内不同值（round-robin）"""
    seen = {_get_ua() for _ in range(len(_UA_POOL) * 2)}
    assert seen
    assert seen <= set(_UA_POOL)  # 全部来自池内


def test_http_get_rotate_ua(monkeypatch):
    """rotate_ua=True 时 User-Agent 来自轮换池；False 保留调用方指定"""
    captured = {}

    class FakeResp:
        text = "ok"

        def __getattr__(self, name):
            return lambda *a, **k: None

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["ua"] = headers.get("User-Agent")
        return FakeResp()

    monkeypatch.setattr("factor_collector.requests.get", fake_get)
    _http_get("http://x", rotate_ua=True)
    assert captured["ua"] in _UA_POOL

    _http_get("http://x", headers={"User-Agent": "custom/1.0"}, rotate_ua=False)
    assert captured["ua"] == "custom/1.0"


# ============================================================
# 分钟K线重试（P9：腾讯 ifzq 瞬时限流兜底）
# ============================================================
def test_fetch_minute_kline_retry_once(monkeypatch):
    """空结果重试一次：首次空、二次成功 → 返回数据（不因瞬时限流丢因子）"""
    import json
    from factor_collector import fetch_minute_kline
    seq = [""]  # 第一次空响应（限流）

    def fake_get(url, params=None, headers=None, encoding=None, rotate_ua=True):
        if seq:
            return seq.pop(0)
        payload = {"code": 0, "data": {"sh000001": {"m5": [
            ["202608201435", 3900, 3901, 3902, 3899, 1000, {}],
            ["202608201440", 3901, 3898, 3902, 3897, 1100, {}],
        ]}}}
        return json.dumps(payload)

    monkeypatch.setattr("factor_collector._http_get", fake_get)
    out = fetch_minute_kline("sh000001", "m5", 8)
    assert len(out) == 2
    assert out[-1]["time"] == "202608201440"
    assert out[-1]["close"] == 3898.0


def test_fetch_minute_kline_retry_gives_up(monkeypatch):
    """重试后仍空 → 返回 []（降级为分钟因子记 0 分，不影响主流程）"""
    import json
    from factor_collector import fetch_minute_kline

    monkeypatch.setattr("factor_collector._http_get", lambda *a, **k: "")
    assert fetch_minute_kline("sh000001", "m5", 8) == []
