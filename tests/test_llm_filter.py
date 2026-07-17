# filepath: tests/test_llm_filter.py
"""测试 LLM 分析冲突护栏与 band/score 对齐"""
import pytest
from src.schemas import ImpactBand, Confidence, NewsAnalysisItem
from src.agent.nodes import (
    _apply_guardrails, _band_from_score, _band_to_direction,
    BAND_SCORE_RANGE,
)


class TestBandFromScore:
    def test_bullish(self):
        assert _band_from_score(8.0) == ImpactBand.BULLISH

    def test_mildly_bullish(self):
        assert _band_from_score(6.0) == ImpactBand.MILDLY_BULLISH

    def test_neutral(self):
        assert _band_from_score(5.0) == ImpactBand.NEUTRAL

    def test_mildly_bearish(self):
        assert _band_from_score(4.0) == ImpactBand.MILDLY_BEARISH

    def test_bearish(self):
        assert _band_from_score(2.0) == ImpactBand.BEARISH


class TestBandToDirection:
    def test_bullish_band_to_bullish(self):
        assert _band_to_direction(ImpactBand.BULLISH) == "bullish"
        assert _band_to_direction(ImpactBand.MILDLY_BULLISH) == "bullish"

    def test_bearish_band_to_bearish(self):
        assert _band_to_direction(ImpactBand.BEARISH) == "bearish"
        assert _band_to_direction(ImpactBand.MILDLY_BEARISH) == "bearish"

    def test_neutral_band_to_neutral(self):
        assert _band_to_direction(ImpactBand.NEUTRAL) == "neutral"
        assert _band_to_direction(ImpactBand.MIXED) == "neutral"


class TestApplyGuardrails:
    def test_no_conflict_unchanged(self):
        item = NewsAnalysisItem(
            title="测试", market_impact_score=8.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BULLISH

    def test_conflict_score_high_band_low_corrected(self):
        item = NewsAnalysisItem(
            title="测试", market_impact_score=8.0,
            impact_band=ImpactBand.BEARISH, confidence=Confidence.HIGH,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BULLISH
        assert result[0].sentiment == "bullish"

    def test_conflict_score_low_band_high_corrected(self):
        item = NewsAnalysisItem(
            title="测试", market_impact_score=2.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.LOW,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BEARISH

    def test_mixed_score_neutral_band_kept(self):
        item = NewsAnalysisItem(
            title="测试", market_impact_score=5.0,
            impact_band=ImpactBand.MIXED, confidence=Confidence.MEDIUM,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.MIXED
