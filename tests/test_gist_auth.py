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
