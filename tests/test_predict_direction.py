# filepath: tests/test_predict_direction.py
"""测试 predict_direction_by_rules 方向兜底规则"""
import pytest
from src.tools.calculators import predict_direction_by_rules


class TestPredictDirection:
    def test_bullish_keywords(self):
        assert predict_direction_by_rules("业绩预增100%", "") == "bullish"
        assert predict_direction_by_rules("扭亏为盈", "") == "bullish"
        assert predict_direction_by_rules("大额回购", "") == "bullish"

    def test_bearish_keywords(self):
        assert predict_direction_by_rules("立案调查", "") == "bearish"
        assert predict_direction_by_rules("跌停", "") == "bearish"
        assert predict_direction_by_rules("退市风险", "") == "bearish"

    def test_neutral_no_keywords(self):
        assert predict_direction_by_rules("公司召开股东大会", "") == "neutral"
        assert predict_direction_by_rules("日常公告", "") == "neutral"

    def test_strong_bullish_overrides_bearish(self):
        """强正向组合优先，避免利空短词误判"""
        # 如果同时出现利好和利空词，强正向应优先
        result = predict_direction_by_rules("业绩预增但遭减持", "")
        assert result == "bullish"

    def test_empty_input(self):
        assert predict_direction_by_rules("", "") == "neutral"
        assert predict_direction_by_rules(None, None) == "neutral"

    def test_st_bearish(self):
        assert predict_direction_by_rules("*ST公司", "") == "bearish"
        assert predict_direction_by_rules("实施退市风险", "") == "bearish"

    def test_policy_keywords(self):
        """政策利好词"""
        result = predict_direction_by_rules("央行降准", "")
        assert result == "bullish"
        result = predict_direction_by_rules("国务院减税", "")
        assert result == "bullish"
