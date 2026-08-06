# filepath: tests/test_llm_structured.py
import pytest
"""测试 LLM 结构化输出构建与降级逻辑（mock LLM，不发真实请求）"""
from unittest.mock import patch, MagicMock
from src.agent.nodes import _build_analysis_prompt, _llm_analyze_batch_structured, _normalize_llm_item
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
    # 返回类型为 dict（容错版 _parse_llm_items 返回标准化 dict）
    assert isinstance(result[0], dict)
    assert result[0]["impact_band"] == "bullish"
    assert result[0]["market_impact_score"] == 8.0


def test_structured_output_fallback_to_freetext():
    """llm.invoke 返回无效内容 → 降级 _call_llm_api 自由文本"""
    mock_llm = MagicMock()

    fake_json = '{"filtered_news": [{"title": "测试", "market_impact_score": 7.0, "impact_band": "bullish", "confidence": "high"}], "removed_count": 0}'

    with patch("src.agent.nodes._build_llm", return_value=mock_llm):
        with patch("src.agent.nodes._call_llm_api", return_value=fake_json):
            result = _llm_analyze_batch_structured([{"title": "测试", "content": "", "source": ""}])

    assert len(result) == 1
    assert isinstance(result[0], dict)
    assert result[0]["impact_band"] == "bullish"


def test_normalize_llm_item_field_aliases():
    """字段名变体（band/score 等）应被正确映射"""
    # 用 band 而非 impact_band，用 score 而非 market_impact_score
    raw = {"title": "测试", "band": "bearish", "score": 3.5, "sectors": ["新能源"], "stocks": "比亚迪,宁德时代"}
    normalized = _normalize_llm_item(raw)
    assert normalized is not None
    assert normalized["impact_band"] == "bearish"
    assert normalized["market_impact_score"] == 3.5
    assert normalized["affected_sectors"] == ["新能源"]
    assert normalized["affected_stocks"] == ["比亚迪", "宁德时代"]
    # 缺失字段有默认值
    assert normalized["confidence"] == "medium"
    assert normalized["impact_reason"] == ""
    assert normalized["sentiment"] == "bearish"


def test_normalize_llm_item_missing_band_defaults_neutral():
    """impact_band 缺失时默认 neutral（score 推不出多空方向）"""
    raw = {"title": "测试", "score": 7.5}
    normalized = _normalize_llm_item(raw)
    assert normalized is not None
    assert normalized["impact_band"] == "neutral"


def test_normalize_llm_item_no_title_returns_none():
    """没有 title 时返回 None（无法匹配原始新闻）"""
    raw = {"score": 5.0, "impact_band": "neutral"}
    normalized = _normalize_llm_item(raw)
    assert normalized is None


def test_normalize_llm_item_invalid_band_defaults_neutral():
    """无效的 band 值默认 neutral，而非用 score 反推方向"""
    raw = {"title": "测试", "impact_band": "positive", "score": 6.0}
    normalized = _normalize_llm_item(raw)
    assert normalized is not None
    assert normalized["impact_band"] == "neutral"


def test_normalize_llm_item_idx_mapped_and_int():
    """LLM 回显的 idx 字段应被正确映射并归整为 int（P0-1: idx 精确 merge 的基础）"""
    raw = {"idx": "3", "title": "测试", "score": 7.0}
    normalized = _normalize_llm_item(raw)
    assert normalized is not None
    assert normalized["idx"] == 3
    assert isinstance(normalized["idx"], int)


def test_normalize_llm_item_idx_underscore_alias():
    """idx 别名（_idx / 编号）也应被识别"""
    raw = {"_idx": 7, "title": "测试", "score": 5.0}
    normalized = _normalize_llm_item(raw)
    assert normalized is not None
    assert normalized["idx"] == 7

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
