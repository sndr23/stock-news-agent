# filepath: tests/test_rank.py
"""测试综合排名：band 主序 + confidence 加权 + 冲突降级"""
from src.tools.calculators import (
    rank_news, BAND_PRIORITY, CONFIDENCE_WEIGHT,
    _band_direction_conflict, _downgrade_band,
    _calc_continuous_score,
)


class TestBandPriority:
    def test_bullish_highest(self):
        assert BAND_PRIORITY["bullish"] == 6

    def test_bearish_lowest(self):
        assert BAND_PRIORITY["bearish"] == 1

    def test_mixed_above_neutral(self):
        assert BAND_PRIORITY["mixed"] > BAND_PRIORITY["neutral"]


class TestConfidenceWeight:
    def test_high_is_1(self):
        assert CONFIDENCE_WEIGHT["high"] == 1.0

    def test_low_is_0_7(self):
        assert CONFIDENCE_WEIGHT["low"] == 0.7

    def test_medium_between(self):
        assert CONFIDENCE_WEIGHT["medium"] == 0.85


class TestBandDirectionConflict:
    def test_bullish_band_bearish_dir_conflict(self):
        assert _band_direction_conflict("bullish", "bearish") is True

    def test_bearish_band_bullish_dir_conflict(self):
        assert _band_direction_conflict("bearish", "bullish") is True

    def test_no_conflict(self):
        assert _band_direction_conflict("bullish", "bullish") is False
        assert _band_direction_conflict("neutral", "neutral") is False


class TestDowngradeBand:
    def test_bullish_downgrade(self):
        assert _downgrade_band("bullish") == "mildly_bullish"

    def test_neutral_downgrade(self):
        assert _downgrade_band("neutral") == "mildly_bearish"

    def test_bearish_already_lowest(self):
        assert _downgrade_band("bearish") == "bearish"


class TestRankNews:
    def _make_news(self, title, band, direction, score, confidence="medium", category="news"):
        return {
            "title": title, "source": "财联社", "content": "", "published_at": "",
            "category": category, "sentiment": direction, "impact_direction": direction,
            "market_impact_score": score, "impact_band": band, "confidence": confidence,
            "affected_sectors": [], "affected_stocks": [], "cluster_weight": 0,
        }

    def test_bullish_ranks_above_bearish(self):
        news = [
            self._make_news("利空", "bearish", "bearish", 2.0),
            self._make_news("利好", "bullish", "bullish", 8.0),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "利好"

    def test_high_confidence_ranks_above_low_same_band(self):
        news = [
            self._make_news("低置信", "bullish", "bullish", 8.0, confidence="low"),
            self._make_news("高置信", "bullish", "bullish", 8.0, confidence="high"),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "高置信"

    def test_conflict_band_downgraded(self):
        news = [self._make_news("冲突", "bullish", "bearish", 8.0)]
        ranked = rank_news(news)
        assert ranked[0]["impact_band"] == "mildly_bullish"

    def test_ranked_item_has_new_fields(self):
        news = [self._make_news("测试", "bullish", "bullish", 8.0)]
        ranked = rank_news(news)
        assert "impact_band" in ranked[0]
        assert "band_priority" in ranked[0]
        assert "confidence" in ranked[0]

    def test_empty_input(self):
        assert rank_news([]) == []

    def test_mixed_above_neutral_in_ranking(self):
        news = [
            self._make_news("中性", "neutral", "neutral", 5.0),
            self._make_news("多空交织", "mixed", "neutral", 5.0),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "多空交织"
