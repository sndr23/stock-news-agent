# filepath: tests/test_schemas.py
"""测试 Pydantic schema 定义与校验"""
import pytest
from pydantic import ValidationError

from src.schemas import ImpactBand, Confidence, NewsAnalysisItem, NewsAnalysisBatch


def test_impact_band_values():
    assert ImpactBand.BULLISH.value == "bullish"
    assert ImpactBand.MIXED.value == "mixed"
    assert ImpactBand.BEARISH.value == "bearish"
    assert len(list(ImpactBand)) == 6


def test_confidence_values():
    assert Confidence.HIGH.value == "high"
    assert Confidence.LOW.value == "low"
    assert len(list(Confidence)) == 3


def test_news_analysis_item_valid():
    item = NewsAnalysisItem(
        title="测试标题",
        market_impact_score=8.0,
        impact_band=ImpactBand.BULLISH,
        confidence=Confidence.HIGH,
        affected_sectors=["半导体"],
    )
    assert item.title == "测试标题"
    assert item.market_impact_score == 8.0


def test_news_analysis_item_score_out_of_range():
    with pytest.raises(ValidationError):
        NewsAnalysisItem(
            title="测试",
            market_impact_score=15.0,
            impact_band=ImpactBand.BULLISH,
            confidence=Confidence.HIGH,
        )


def test_news_analysis_item_score_negative():
    with pytest.raises(ValidationError):
        NewsAnalysisItem(
            title="测试",
            market_impact_score=-1.0,
            impact_band=ImpactBand.BEARISH,
            confidence=Confidence.LOW,
        )


def test_news_analysis_batch():
    item = NewsAnalysisItem(
        title="测试",
        market_impact_score=5.0,
        impact_band=ImpactBand.NEUTRAL,
        confidence=Confidence.MEDIUM,
    )
    batch = NewsAnalysisBatch(filtered_news=[item], removed_count=2, analysis_summary="摘要")
    assert len(batch.filtered_news) == 1
    assert batch.removed_count == 2
