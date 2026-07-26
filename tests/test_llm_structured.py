# filepath: tests/test_llm_structured.py
"""测试 LLM 结构化输出构建与降级逻辑（mock LLM，不发真实请求）"""
from unittest.mock import patch, MagicMock
from src.agent.nodes import _build_analysis_prompt, _llm_analyze_batch_structured
from src.schemas import NewsAnalysisItem, ImpactBand, Confidence


def test_build_analysis_prompt_contains_contract():
    batch = [{"title": "测试", "content": "内容", "source": "财联社", "published_at": "", "category": "news"}]
    prompt = _build_analysis_prompt(batch)
    assert "impact_band" in prompt
    assert "confidence" in prompt
    assert "market_impact_score" in prompt


def test_build_analysis_prompt_truncates_content():
    long_content = "x" * 200
    batch = [{"title": "测试", "content": long_content, "source": "", "published_at": "", "category": "news"}]
    prompt = _build_analysis_prompt(batch)
    assert len(prompt) < 5000


def test_structured_output_success_via_mock():
    """mock llm.invoke 返回 JSON 字符串的成功路径"""
    fake_json = '{"filtered_news": [{"title": "测试", "market_impact_score": 8.0, "impact_band": "bullish", "confidence": "high", "affected_sectors": ["半导体"]}], "removed_count": 0}'

    mock_resp = MagicMock()
    mock_resp.content = fake_json

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_resp

    with patch("src.agent.nodes._build_llm", return_value=mock_llm):
        result = _llm_analyze_batch_structured([{"title": "测试", "content": "", "source": ""}])

    assert len(result) == 1
    assert result[0].impact_band == ImpactBand.BULLISH


def test_structured_output_fallback_to_freetext():
    """llm.invoke 返回无效内容 → 降级 _call_llm_api 自由文本"""
    mock_llm = MagicMock()

    fake_json = '{"filtered_news": [{"title": "测试", "market_impact_score": 7.0, "impact_band": "bullish", "confidence": "high"}], "removed_count": 0}'

    with patch("src.agent.nodes._build_llm", return_value=mock_llm):
        with patch("src.agent.nodes._call_llm_api", return_value=fake_json):
            result = _llm_analyze_batch_structured([{"title": "测试", "content": "", "source": ""}])

    assert len(result) == 1
    assert result[0].impact_band == ImpactBand.BULLISH
