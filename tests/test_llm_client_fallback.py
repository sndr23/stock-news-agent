# filepath: tests/test_llm_client_fallback.py
"""LLM 多模型降级链测试。"""

import logging
from unittest.mock import Mock

import pytest
import requests

import src.llm_client as llm_client


pytestmark = pytest.mark.unit


class _Response:
    """满足客户端调用所需最小接口的响应对象。"""

    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _configure_fallbacks(monkeypatch, *, fallback1_key="fallback1-key", fallback2_key="", fallback3_key=""):
    """隔离模型链配置，避免读取测试进程中的真实环境变量。"""
    monkeypatch.setattr(llm_client, "OPENROUTER_MODEL_NAME", "primary-model")
    monkeypatch.setattr(llm_client, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(llm_client, "OPENROUTER_API_KEY", "primary-key")
    for name in (
        "LLM_FALLBACK1_BASE_URL",
        "LLM_FALLBACK1_API_KEY",
        "LLM_FALLBACK1_MODEL",
        "LLM_FALLBACK2_BASE_URL",
        "LLM_FALLBACK2_API_KEY",
        "LLM_FALLBACK2_MODEL",
        "LLM_FALLBACK3_BASE_URL",
        "LLM_FALLBACK3_API_KEY",
        "LLM_FALLBACK3_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_FALLBACK1_API_KEY", fallback1_key)
    monkeypatch.setenv("LLM_FALLBACK1_MODEL", "fallback1-model")
    monkeypatch.setenv("LLM_FALLBACK2_API_KEY", fallback2_key)
    monkeypatch.setenv("LLM_FALLBACK2_MODEL", "fallback2-model")
    monkeypatch.setenv("LLM_FALLBACK3_API_KEY", fallback3_key)
    monkeypatch.setenv("LLM_FALLBACK3_MODEL", "fallback3-model")


def test_primary_retries_then_fallback_returns_content_and_logs_model(monkeypatch, caplog):
    """主模型重试耗尽后应切换备选模型并返回成功内容。"""
    _configure_fallbacks(monkeypatch)
    post = Mock(side_effect=[
        requests.RequestException("primary down"),
        requests.RequestException("primary down"),
        requests.RequestException("primary down"),
        _Response("fallback result"),
    ])
    monkeypatch.setattr(requests.Session, "post", post)
    monkeypatch.setattr(llm_client.time, "sleep", lambda _seconds: None)

    with caplog.at_level(logging.INFO, logger=llm_client.logger.name):
        result = llm_client._call_llm_api("system", "user", timeout=1, max_retries=2)

    assert result == "fallback result"
    assert post.call_count == 4
    assert [call.kwargs["json"]["model"] for call in post.call_args_list] == [
        "primary-model",
        "primary-model",
        "primary-model",
        "fallback1-model",
    ]
    assert post.call_args_list[3].args[0] == "https://api.b.ai/v1/chat/completions"
    messages = [record.getMessage() for record in caplog.records]
    assert any("模型primary-model失败，降级到模型fallback1-model" in message for message in messages)
    assert any("fallback1-model" in message and "成功" in message for message in messages)


def test_all_models_failure_raises_exception(monkeypatch):
    """模型链全部失败时仍应向上抛出异常。"""
    _configure_fallbacks(monkeypatch, fallback2_key="fallback2-key", fallback3_key="fallback3-key")
    post = Mock(side_effect=requests.RequestException("all down"))
    monkeypatch.setattr(requests.Session, "post", post)

    with pytest.raises(Exception, match="LLM API 调用失败"):
        llm_client._call_llm_api("system", "user", timeout=1, max_retries=0)

    assert post.call_count == 4


def test_missing_fallback_key_skips_level_without_key_error(monkeypatch):
    """未配置某级 key 时应跳过该级并继续尝试后续模型。"""
    _configure_fallbacks(monkeypatch, fallback1_key="", fallback2_key="fallback2-key")
    post = Mock(side_effect=[
        requests.RequestException("primary down"),
        _Response("fallback2 result"),
    ])
    monkeypatch.setattr(requests.Session, "post", post)

    result = llm_client._call_llm_api("system", "user", timeout=1, max_retries=0)

    assert result == "fallback2 result"
    assert post.call_count == 2
    assert post.call_args_list[1].kwargs["json"]["model"] == "fallback2-model"


def test_deadline_reached_before_first_attempt_aborts_immediately(monkeypatch):
    """deadline 已到时应立即熔断，不发起任何模型请求。"""
    _configure_fallbacks(monkeypatch)
    post = Mock()
    monkeypatch.setattr(requests.Session, "post", post)
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: 100.0)

    with pytest.raises(Exception, match="总超时熔断"):
        llm_client._call_llm_api("system", "user", timeout=1, max_retries=0, deadline=100.0)

    post.assert_not_called()


def test_deadline_reached_after_provider_failure_skips_next_model(monkeypatch):
    """某级失败后若已到 deadline，不应再发起下一级请求。"""
    _configure_fallbacks(monkeypatch)
    now = {"value": 0.0}

    def fake_post(_url, **_kwargs):
        now["value"] = 100.0
        raise requests.RequestException("primary down")

    post = Mock(side_effect=fake_post)
    monkeypatch.setattr(requests.Session, "post", post)
    monkeypatch.setattr(llm_client.time, "monotonic", lambda: now["value"])

    with pytest.raises(Exception, match="总超时熔断"):
        llm_client._call_llm_api("system", "user", timeout=1, max_retries=0, deadline=100.0)

    assert post.call_count == 1


def test_trust_env_is_derived_from_each_provider_url(monkeypatch):
    """仅 OpenRouter 官方域名保留代理，国内备选端点应禁用代理。"""
    _configure_fallbacks(monkeypatch)
    observed = []

    def fake_post(session, url, **_kwargs):
        observed.append((url, session.trust_env))
        if len(observed) == 1:
            raise requests.RequestException("primary down")
        return _Response("fallback result")

    monkeypatch.setattr(requests.Session, "post", fake_post)

    result = llm_client._call_llm_api("system", "user", timeout=1, max_retries=0)

    assert result == "fallback result"
    assert observed == [
        ("https://openrouter.ai/api/v1/chat/completions", True),
        ("https://api.b.ai/v1/chat/completions", False),
    ]
