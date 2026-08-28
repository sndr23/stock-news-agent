# -*- coding: utf-8 -*-
"""Gist 读取函数认证头回归测试（2026-08-14 审核发现：三处 requests.get 漏传 headers）

根因：_gist_load_factor / _gist_load / _load_factor_risk_state 定义了带
Authorization 的 headers 但调用 requests.get 时漏传，导致未认证请求按 IP 限流
（60 次/小时），本机/共享 IP 触顶后返回 403 rate limit，状态持久化失效。

本测试断言这三个函数在请求 Gist 时确实传入了 headers（含 Authorization），
防止复制粘贴式漏传回归。
"""
import sys
from pathlib import Path

import pytest
import requests as requests_mod

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import factor_collector as fc  # noqa: E402
import real_time_push as rtp  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock requests，无网络


class _FakeResp:
    """最小可用的 requests.Response 替身"""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_factor_gist_load_passes_headers(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _FakeResp({"files": {"factor_state.json": {"content": '{"risk_state":"risk_off"}'}}})

    monkeypatch.setattr(fc.requests, "get", fake_get)
    state = fc._gist_load_factor("tok123", "gid123")

    assert captured["headers"] is not None, "漏传 headers（未认证请求会触发 IP 限流 403）"
    assert "Authorization" in captured["headers"]
    assert state == {"risk_state": "risk_off"}


def test_factor_gist_load_failure_raises_instead_of_returning_empty(monkeypatch):
    """因子 Gist 连续读取失败时必须 fail-stop，禁止空状态覆盖云端历史。"""

    def fake_get(url, **kwargs):
        raise IOError("network down")

    monkeypatch.setattr(fc.requests, "get", fake_get)
    monkeypatch.setattr(fc.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="读取失败"):
        fc._gist_load_factor("tok123", "gid123")


def test_factor_load_state_does_not_fallback_to_local_after_gist_failure(monkeypatch, tmp_path):
    """已配置 Gist 且读取失败时，不得拿本地旧状态继续运行并回写云端。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(fc, "STATE_PATH", tmp_path / "factor_state.json")
    fc.STATE_PATH.write_text('{"basis_history": {"IC": [{"v": -1.2}]}}', encoding="utf-8")
    monkeypatch.setattr(fc, "_gist_load_factor", lambda token, gist_id: (_ for _ in ()).throw(
        RuntimeError("Gist 读取失败")
    ))

    with pytest.raises(RuntimeError, match="Gist 读取失败"):
        fc._load_state()


def test_factor_gist_load_corrupt_json_raises(monkeypatch):
    """因子 Gist 内容损坏时必须 fail-stop，禁止按空状态继续。"""

    class _BrokenResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"files": {"factor_state.json": {"content": '{"basis_history":'}}}

    monkeypatch.setattr(fc.requests, "get", lambda url, **kwargs: _BrokenResp())
    monkeypatch.setattr(fc.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="读取失败"):
        fc._gist_load_factor("tok123", "gid123")


def test_factor_gist_load_non_object_json_raises(monkeypatch):
    """因子 Gist 根节点不是对象时必须拒绝，避免后续按空状态运行。"""

    class _ListResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"files": {"factor_state.json": {"content": "[]"}}}

    monkeypatch.setattr(fc.requests, "get", lambda url, **kwargs: _ListResp())
    monkeypatch.setattr(fc.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="读取失败"):
        fc._gist_load_factor("tok123", "gid123")


def test_factor_gist_load_empty_content_raises(monkeypatch):
    """因子 Gist 文件为空时必须拒绝，空文件通常意味着写入截断或损坏。"""

    class _EmptyContentResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"files": {"factor_state.json": {"content": ""}}}

    monkeypatch.setattr(fc.requests, "get", lambda url, **kwargs: _EmptyContentResp())
    monkeypatch.setattr(fc.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="读取失败"):
        fc._gist_load_factor("tok123", "gid123")


def test_factor_gist_load_missing_file_is_empty_on_first_deploy(monkeypatch):
    """Gist 请求成功但尚无因子文件时，允许首次部署从空状态开始。"""

    class _EmptyResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"files": {}}

    monkeypatch.setattr(fc.requests, "get", lambda url, **kwargs: _EmptyResp())
    monkeypatch.setattr(fc.time, "sleep", lambda _seconds: None)

    assert fc._gist_load_factor("tok123", "gid123") == {}


def test_factor_gist_load_missing_file_after_transient_failure_is_empty(monkeypatch):
    """临时失败后连续成功确认文件不存在时，应允许首次初始化为空状态。"""
    calls = {"n": 0}

    class _EmptyResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"files": {}}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IOError("transient network down")
        return _EmptyResp()

    monkeypatch.setattr(fc.requests, "get", fake_get)
    monkeypatch.setattr(fc.time, "sleep", lambda _seconds: None)

    assert fc._gist_load_factor("tok123", "gid123") == {}


def test_factor_save_state_failure_is_reported_to_caller(monkeypatch, tmp_path):
    """因子快照 Gist 写入失败时返回失败，避免采集任务假成功。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(fc, "STATE_PATH", tmp_path / "factor_state.json")
    monkeypatch.setattr(fc, "_gist_save_factor", lambda token, gist_id, state: (_ for _ in ()).throw(
        RuntimeError("Gist 写入失败")
    ))

    assert fc._save_state({"risk_state": "neutral"}) is False


def test_factor_load_state_rejects_non_object_local_state(monkeypatch, tmp_path):
    """本地状态根节点不是对象时，因子入口应从空状态安全启动。"""
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.setattr(fc, "STATE_PATH", tmp_path / "factor_state.json")
    fc.STATE_PATH.write_text("[]", encoding="utf-8")

    assert fc._load_state() == {}


def test_realtime_gist_load_passes_headers(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _FakeResp({"files": {rtp.GIST_STATE_FILENAME: {"content": '{"seen":{}}'}}})

    monkeypatch.setattr(requests_mod, "get", fake_get)
    state = rtp._gist_load("tok123", "gid123")

    assert captured["headers"] is not None, "漏传 headers（未认证请求会触发 IP 限流 403）"
    assert "Authorization" in captured["headers"]
    assert "seen" in state


def test_realtime_risk_state_passes_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(rtp, "_FACTOR_STATE_PATH", tmp_path / "factor_state.json")

    captured = {}

    def fake_get(url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _FakeResp({"files": {"factor_state.json": {"content": '{"risk_state":"risk_off"}'}}})

    monkeypatch.setattr(requests_mod, "get", fake_get)
    rs = rtp._load_factor_risk_state()

    assert captured["headers"] is not None, "漏传 headers（未认证请求会触发 IP 限流 403）"
    assert "Authorization" in captured["headers"]
    assert rs == "risk_off"
