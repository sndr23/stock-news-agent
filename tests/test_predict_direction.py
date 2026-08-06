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


class TestDirectionFix20260803:
    """2026-08-03 修复回归：利空标题不再因正文上涨词被误判为利好

    用户反馈：跳过 LLM 的资讯"利空显示利好"。根因：旧实现对 title+content
    全文做单遍关键词扫描，正文中"仅XX逆势大涨/XX涨停"等非事件句污染方向。
    修复：标题优先 + 正文仅扫描首句 + 反向修正 + 词表扩充。
    """

    def test_bearish_market_headlines(self):
        """今日实证利空标题 → bearish（词表扩充：跌超/低开低走/走弱/熔断/监管问责/下跌/重创/回调）"""
        assert predict_direction_by_rules("韩国综指收盘跌超5%", "") == "bearish"
        assert predict_direction_by_rules("A股午评：科创50指数低开低走跌3.73%", "") == "bearish"
        assert predict_direction_by_rules("存储芯片概念持续走弱 兆易创新逼近跌停", "") == "bearish"
        assert predict_direction_by_rules("韩国交易所启动熔断机制，暂停科斯达克程序化买入委托", "") == "bearish"
        assert predict_direction_by_rules("一日8家券商遭监管问责", "") == "bearish"
        assert predict_direction_by_rules("美伊或将举行谈判 国际油价显著下跌", "") == "bearish"
        assert predict_direction_by_rules("欧洲央行称伊朗战争重创欧元区消费，信心下滑拖累经济增长", "") == "bearish"

    def test_bearish_title_ignores_other_sector_bullish_words_in_content(self):
        """利空标题 + 正文提到其他板块/个股涨停 → 仍为利空（不再全文翻成利好）"""
        title = "A股午评：科创50指数低开低走跌3.73%，算力硬件股集体回调"
        content = "8月3日午间，A股三大指数集体下挫，科创50跌3.73%。算力硬件股集体回调，多股跌超5%。仅核电概念逆势活跃，利柏特涨停。"
        assert predict_direction_by_rules(title, content) == "bearish"

    def test_bearish_title_ignores_oil_price_surge_in_content(self):
        title = "欧洲央行称伊朗战争重创欧元区消费，信心下滑拖累经济增长"
        content = "欧洲央行表示，伊朗战争导致能源价格大涨，重创欧元区消费信心，拖累经济增长前景。"
        assert predict_direction_by_rules(title, content) == "bearish"

    def test_reverse_prefix_relax_export_control(self):
        """反向修正：放宽/放松 + 利空词 → 利好（"放宽稀土出口管制"是利好）"""
        assert predict_direction_by_rules("马来西亚考虑放宽稀土出口管制以满足市场需求", "") == "bullish"
        assert predict_direction_by_rules("美国宣布解除对某公司制裁", "") == "bullish"

    def test_reverse_prefix_cancel_bearish(self):
        """反向修正：取消/终止 + 利好词 → 利空（"取消增持""终止回购"是利空）"""
        assert predict_direction_by_rules("大股东取消增持计划", "") == "bearish"
        assert predict_direction_by_rules("公司终止股份回购计划", "") == "bearish"

    def test_bullish_headlines_still_bullish(self):
        """今日实证利多标题 → bullish"""
        assert predict_direction_by_rules("核电概念表现活跃 利伯特涨停", "") == "bullish"
        assert predict_direction_by_rules("存储、半导体概念股夜盘普涨，SK海力士、美光科技涨超2%", "") == "bullish"
        assert predict_direction_by_rules("高盛上调三星电子目标价至49万韩元", "") == "bullish"

    def test_content_first_sentence_fallback(self):
        """标题无信号时用正文首句兜底（首句=主表述，非全文）"""
        title = "晚间公告速览"
        content = "多家公司业绩预增超100%，机构看好后续走势。其中某公司签署重大合同。"
        assert predict_direction_by_rules(title, content) == "bullish"

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
