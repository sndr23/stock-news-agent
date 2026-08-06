# filepath: tests/test_fix_regression.py
"""修复回归测试：P0 词边界 / P1 cluster_weight 聚类 / P1 market 加成 / P2 confidence 惩罚"""
import pytest
from unittest.mock import patch
from src.tools import data_fetchers
from src.tools.calculators import (

    _has_tech_keyword, rank_news, CONFIDENCE_WEIGHT, SCOPE_SCORE_BOOST,
)


class TestTechKeywordBoundary:
    """P0 修复：英文缩写词边界匹配，禁止子串误命中"""

    def test_namd_medical_abbrev_not_tech(self):
        """实证案例：nAMD(黄斑变性医学缩写) 不得命中 AMD 科技词"""
        text = "罗氏制药批准罗视佳用于治疗年龄相关性黄斑变性（nAMD）"
        assert _has_tech_keyword(text) is False

    def test_amd_medical_context_excluded(self):
        """AMD 独立出现但为眼科语境时排除（词边界无法防中文相邻，靠排除词）"""
        text = "罗氏公布AMD湿性黄斑变性患者真实世界数据"
        assert _has_tech_keyword(text) is False

    def test_amd_chip_context_matches(self):
        """AMD 芯片语境应正常命中"""
        text = "AMD发布新一代EPYC服务器CPU，算力大幅提升"
        assert _has_tech_keyword(text) is True

    def test_chinese_tech_word_matches(self):
        assert _has_tech_keyword("半导体产业链掀起涨停潮") is True

    def test_nand_matches(self):
        assert _has_tech_keyword("铠侠：第一财季NAND闪存单价环比上涨70%") is True


class TestClusterWeightRanking:
    """P1 修复：cluster_weight 聚类热度因子参与排序（同 band 同分时热度优先）"""

    def _make_news(self, title, score=6.0, cluster_weight=0.0):
        return {
            "title": title, "source": "财联社", "content": "", "published_at": "",
            "category": "news", "sentiment": "bullish", "impact_direction": "bullish",
            "market_impact_score": score, "impact_band": "bullish", "confidence": "high",
            "affected_sectors": ["半导体"], "affected_stocks": [],
            "cluster_weight": cluster_weight,
        }

    def test_cluster_bonus_affects_rank(self, monkeypatch):
        """同 band 同 LLM 分时，cluster_weight 高者排前"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        hot = self._make_news("同事件高热度报道", cluster_weight=4.0)
        cold = self._make_news("单源独立报道", cluster_weight=0.0)
        ranked = rank_news([cold, hot])
        assert ranked[0]["title"] == "同事件高热度报道"


class TestMarketScopeBoost:
    """P1 修复：market 加成足以让同强度 market 压过 sector（实证 #1/#2）"""

    def _make_news(self, title, scope, score, tech_text):
        return {
            "title": title, "source": "财联社", "content": tech_text, "published_at": "",
            "category": "news", "sentiment": "bullish", "impact_direction": "bullish",
            "market_impact_score": score, "impact_band": "bullish", "confidence": "high",
            "affected_sectors": ["半导体"], "affected_stocks": [],
            "cluster_weight": 0.0, "influence_scope": scope,
        }

    def test_market_boost_above_tech_sector(self, monkeypatch):
        """同分同 band：market 应压过获得 1.2 科技倍率的 sector（修复前被反压）"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        market = self._make_news("富达：美联储或延至12月才启动加息周期", "market", 8.0, "美联储 加息周期")
        sector = self._make_news("MSCI：AI成为2026年上半年市场主导因素", "sector", 8.0, "AI 算力 半导体")
        ranked = rank_news([sector, market])
        assert ranked[0]["influence_scope"] == "market"
        assert SCOPE_SCORE_BOOST["market"] >= 0.20

    def test_market_boost_not_overwhelming(self, monkeypatch):
        """高分 sector 仍应越过低分 market（加成远小于正常分数差异）"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        market = self._make_news("弱市场消息", "market", 4.0, "外围消息")
        sector = self._make_news("半导体龙头重大突破涨停", "sector", 9.0, "半导体 龙头 涨停")
        ranked = rank_news([market, sector])
        assert ranked[0]["influence_scope"] == "sector"


class TestConfidenceAdjustment:
    """P2 修复：medium 惩罚 0.90，LLM 1 分差距得以体现"""

    def test_medium_weight(self):
        assert CONFIDENCE_WEIGHT["medium"] == 0.90

    def test_score_gap_overcomes_medium_penalty(self, monkeypatch):
        """sc=6/medium 应高于 sc=5/high（修复前 medium 0.85 惩罚导致倒挂）"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        def mk(title, score, conf):
            return {
                "title": title, "source": "财联社", "content": "半导体 封装 订单", "published_at": "",
                "category": "news", "sentiment": "bullish", "impact_direction": "bullish",
                "market_impact_score": score, "impact_band": "bullish", "confidence": conf,
                "affected_sectors": ["半导体"], "affected_stocks": [],
                "cluster_weight": 0.0,
            }
        higher = mk("半导体封装重大订单", 6.0, "medium")
        lower = mk("医药常规获批", 5.0, "high")
        ranked = rank_news([lower, higher])
        assert ranked[0]["title"] == "半导体封装重大订单"

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
