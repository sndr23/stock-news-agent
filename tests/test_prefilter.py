# filepath: tests/test_prefilter.py
"""测试预筛权重表 score_news_relevance"""
from src.tools.calculators import (
    score_news_relevance, _is_official_source,
    _COMPANY_EVENT_TERMS, _SECTOR_NEWS_TERMS, _MACRO_NEWS_TERMS,
)


class TestScoreNewsRelevance:
    def test_stock_code_in_title(self):
        item = {"title": "贵州茅台发布年报", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item, stock_code="600519", stock_name="贵州茅台")
        assert score >= 45
        assert cat == "direct"

    def test_company_name_in_title(self):
        item = {"title": "宁德时代签订大单", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item, stock_name="宁德时代")
        assert score >= 45
        assert cat == "direct"

    def test_event_term_alone_in_flow_mode(self):
        item = {"title": "某公司业绩预增200%", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        # 纯资讯流（无 direct_signal）事件词降为 +6
        assert score >= 6

    def test_official_source_bonus(self):
        item = {"title": "上交所公告", "content": "", "url": "https://sse.com.cn/notice", "source": "上交所"}
        score, cat = score_news_relevance(item)
        assert score >= 8

    def test_sector_term_bonus(self):
        item = {"title": "半导体行业景气度提升", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert score >= 6

    def test_macro_penalty(self):
        item = {"title": "央行降准释放流动性", "content": "大盘上涨", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert cat == "macro"

    def test_tech_hardware_bonus(self):
        item = {"title": "光模块需求爆发", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert score >= 10

    def test_score_clamped_0_100(self):
        item = {"title": "x", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item)
        assert 0 <= score <= 100

    def test_direct_category_threshold(self):
        item = {"title": "600519业绩预增", "content": "", "url": "", "source": ""}
        score, cat = score_news_relevance(item, stock_code="600519")
        assert cat == "direct"


class TestIsOfficialSource:
    def test_host_match(self):
        assert _is_official_source(url="https://sse.com.cn/x", source="") is True

    def test_label_match(self):
        assert _is_official_source(url="", source="上海证券交易所公告") is True

    def test_non_official(self):
        assert _is_official_source(url="https://random.com/x", source="随机媒体") is False

    def test_empty(self):
        assert _is_official_source(url="", source="") is False
