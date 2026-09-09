"""LLM reasoning 参数开关测试。"""

from unittest.mock import Mock

import pytest
import requests

import src.llm_client as llm_client


pytestmark = pytest.mark.unit


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def _call_once(monkeypatch, base_url):
    monkeypatch.setattr(llm_client, "OPENROUTER_BASE_URL", base_url)
    monkeypatch.setattr(llm_client, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "OPENROUTER_MODEL_NAME", "test-model")
    for name in (
        "LLM_FALLBACK1_API_KEY",
        "LLM_FALLBACK2_API_KEY",
        "LLM_FALLBACK3_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    post = Mock(return_value=_Response())
    monkeypatch.setattr(requests.Session, "post", post)
    assert llm_client._call_llm_api("system", "user", max_retries=0) == "ok"
    return post.call_args.kwargs["json"]


def test_agnes_defaults_to_disabled_reasoning(monkeypatch):
    monkeypatch.delenv("LLM_DISABLE_REASONING", raising=False)

    payload = _call_once(monkeypatch, "https://APIHUB.AGNES-AI.CN/v1")

    assert payload["reasoning_effort"] == "none"


def test_agnes_reasoning_can_be_enabled_by_environment_toggle(monkeypatch):
    monkeypatch.setenv("LLM_DISABLE_REASONING", "0")

    payload = _call_once(monkeypatch, "https://apihub.agnes-ai.cn/v1")

    assert "reasoning_effort" not in payload


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.b.ai/v1",
        "https://api.longcat.chat/openai/v1",
        "https://openrouter.ai/api/v1",
    ],
)
def test_non_agnes_endpoints_never_receive_reasoning_parameter(monkeypatch, base_url):
    monkeypatch.setenv("LLM_DISABLE_REASONING", "1")

    payload = _call_once(monkeypatch, base_url)

    assert "reasoning_effort" not in payload
