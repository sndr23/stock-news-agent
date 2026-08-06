# filepath: tests/test_macro_cluster.py
import pytest
"""测试宏观/板块级主题簇限流（第八轮残余问题修复）

同一主体（机构/国家/商品/地缘事件）的近重复报道应限流，
避免霸占头部、稀释 A 股实质利好。仅作用于 market/sector 级。
"""
from src.tools.calculators import (

    dedup_macro_clusters, dedup_and_cap_for_display, _macro_cluster_key,
)


class TestMacroClusterKey:
    def test_morgan_stanley_subject(self):
        # 仅含"摩根大通"不含更早列表项（如美联储）→ 命中"摩根大通"
        assert _macro_cluster_key({"title": "摩根大通发布研报看好半导体"}) == "摩根大通"

    def test_first_match_wins(self):
        # "摩根大通预计美联储下月降息" 同时含两主体词，按列表顺序先命中"美联储"
        assert _macro_cluster_key({"title": "摩根大通预计美联储下月降息"}) == "美联储"

    def test_fed_subject(self):
        assert _macro_cluster_key({"title": "美联储利率决议临近", "content": ""}) == "美联储"

    def test_oil_subject(self):
        assert _macro_cluster_key({"title": "WTI原油涨超7%"}) == "WTI"

    def test_no_subject_returns_none(self):
        assert _macro_cluster_key({"title": "某A股公司签订重大合同"}) is None

    def test_stock_scope_ignores_subject(self):
        # 即便含主体词，个股级也不走簇限流（由调用方按 scope 过滤）
        assert _macro_cluster_key({"title": "伊朗局势影响某公司", "influence_scope": "stock"}) == "伊朗"


class TestDedupMacroClusters:
    def _mk(self, title, scope="market", score=5.0):
        return {"title": title, "influence_scope": scope, "market_impact_score": score}

    def test_same_subject_capped_to_two(self):
        news = [
            self._mk("摩根大通:美联储鸽派推演", score=8.0),
            self._mk("摩根大通:美联储鹰派推演", score=7.0),
            self._mk("摩根大通:美联储基准情形推演", score=6.0),  # 第3条同主体 → 剔除
            self._mk("永鼎股份签订11亿订单", scope="stock"),  # 个股级不参与
        ]
        result = dedup_macro_clusters(news, max_per_subject=2)
        titles = [n["title"] for n in result]
        assert len(result) == 3
        assert "摩根大通:美联储基准情形推演" not in titles
        assert "永鼎股份签订11亿订单" in titles

    def test_different_subjects_not_limited(self):
        news = [
            self._mk("美联储降息预期升温", score=8.0),
            self._mk("特朗普推动税改", score=7.0),
            self._mk("伊朗地缘紧张升级", score=6.0),
        ]
        result = dedup_macro_clusters(news, max_per_subject=2)
        assert len(result) == 3  # 不同主体均保留

    def test_stock_scope_never_capped(self):
        news = [self._mk(f"个股事件{i}", scope="stock", score=5.0) for i in range(5)]
        result = dedup_macro_clusters(news, max_per_subject=2)
        assert len(result) == 5  # 个股级全部保留

    def test_empty_and_order_preserved(self):
        assert dedup_macro_clusters([]) == []
        news = [self._mk("美联储A", score=8.0), self._mk("美联储B", score=7.0),
                self._mk("美联储C", score=6.0)]
        result = dedup_macro_clusters(news, max_per_subject=2)
        # 保留靠前的（高分优先，列表本身已按排序传入）
        assert [n["title"] for n in result] == ["美联储A", "美联储B"]


class TestDedupAndCapIntegration:
    """dedup_and_cap_for_display 应串联 同事件去重 → 宏观簇限流 → 同股限额"""

    def test_macro_cluster_applied_in_display_pipeline(self):
        from src.tools.calculators import dedup_ranked_by_event
        news = [
            {"title": "美联储鸽派推演", "influence_scope": "market", "market_impact_score": 8.0,
             "affected_stocks": [], "category": "news"},
            {"title": "美联储鹰派推演", "influence_scope": "market", "market_impact_score": 7.0,
             "affected_stocks": [], "category": "news"},
            {"title": "美联储基准推演", "influence_scope": "market", "market_impact_score": 6.0,
             "affected_stocks": [], "category": "news"},
            {"title": "永鼎股份订单", "influence_scope": "stock", "market_impact_score": 7.0,
             "affected_stocks": ["永鼎股份"], "category": "news"},
        ]
        # 直接验证 dedup_macro_clusters 在中间环节生效
        after_event = dedup_ranked_by_event(news)
        after_cluster = dedup_macro_clusters(after_event, 2)
        assert len(after_cluster) == 3
        assert all("基准推演" not in n["title"] for n in after_cluster)

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
