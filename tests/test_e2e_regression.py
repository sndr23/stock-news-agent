# filepath: tests/test_e2e_regression.py
"""端到端回归：mock 数据源 → 全链路 → 验证 band 排序与 confidence"""
from unittest.mock import patch
from src.agent.nodes import _python_prefilter, _apply_guardrails
from src.tools.calculators import rank_news
from src.schemas import ImpactBand, Confidence, NewsAnalysisItem


def test_e2e_mock_pipeline():
    """模拟完整链路：聚合(mocks) → 预筛 → LLM(mock) → 排名"""
    raw_news = [
        {"title": "贵州茅台业绩预增200%", "content": "业绩大增", "url": "https://a.com/1", "source": "财联社", "published_at": "", "category": "news"},
        {"title": "贵州茅台业绩预增200%", "content": "业绩大增", "url": "https://a.com/1", "source": "财联社", "published_at": "", "category": "news"},
        {"title": "某公司30周年庆典", "content": "周年庆", "url": "https://b.com/2", "source": "自媒体", "published_at": "", "category": "news"},
        {"title": "央行降准释放流动性", "content": "大盘利好", "url": "https://c.com/3", "source": "新华社", "published_at": "", "category": "news"},
        {"title": "半导体板块国产替代加速", "content": "光模块需求爆发", "url": "https://d.com/4", "source": "财联社", "published_at": "", "category": "news"},
    ]

    kept, removed = _python_prefilter(raw_news, top_n=40)
    assert "某公司30周年庆典" not in [n["title"] for n in kept]
    assert removed >= 1

    llm_results = [
        NewsAnalysisItem(title="贵州茅台业绩预增200%", market_impact_score=8.0,
                         impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
                         affected_sectors=["白酒"], affected_stocks=["贵州茅台"],
                         impact_reason="业绩大增"),
        NewsAnalysisItem(title="央行降准释放流动性", market_impact_score=9.0,
                         impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
                         affected_sectors=["银行"], impact_reason="流动性利好"),
        NewsAnalysisItem(title="半导体板块国产替代加速", market_impact_score=7.0,
                         impact_band=ImpactBand.BULLISH, confidence=Confidence.MEDIUM,
                         affected_sectors=["半导体", "CPO"], impact_reason="国产替代"),
    ]
    llm_results = _apply_guardrails(llm_results)

    llm_by_title = {item.title: item.model_dump() for item in llm_results}
    merged = []
    for news in kept:
        title = news.get("title", "")
        if title in llm_by_title:
            merged_news = {**news, **llm_by_title[title]}
            merged_news["impact_direction"] = "bullish"
            merged.append(merged_news)

    ranked = rank_news(merged)
    assert len(ranked) > 0
    assert ranked[0]["impact_band"] == "bullish"
    for item in ranked:
        assert "confidence" in item
        assert "impact_band" in item
        assert "band_priority" in item


def test_e2e_conflict_guardrail_in_pipeline():
    """端到端：LLM 返回冲突 band/score → 护栏校正 → 排名正确"""
    # 标题含事件词"业绩预增"，避免被预筛零分过滤（sector 类零分丢弃）
    raw = [{"title": "测试冲突业绩预增", "content": "", "url": "", "source": "", "published_at": "", "category": "news"}]
    kept, _ = _python_prefilter(raw, top_n=40)
    assert len(kept) >= 1, "预筛应保留含事件词的条目"

    conflict_item = NewsAnalysisItem(
        title="测试冲突业绩预增", market_impact_score=8.0,
        impact_band=ImpactBand.BEARISH,
        confidence=Confidence.MEDIUM,
    )
    corrected = _apply_guardrails([conflict_item])
    assert corrected[0].impact_band == ImpactBand.BULLISH

    merged = {**kept[0], **corrected[0].model_dump()}
    merged["impact_direction"] = "bullish"
    ranked = rank_news([merged])
    assert ranked[0]["impact_band"] == "bullish"


def test_e2e_empty_input():
    """空输入不崩溃"""
    kept, removed = _python_prefilter([], top_n=40)
    assert kept == []
    ranked = rank_news([])
    assert ranked == []
