# filepath: tests/test_llm_filter.py
"""测试 LLM 分析冲突护栏与 band/score 对齐"""
import pytest
from src.schemas import ImpactBand, Confidence, NewsAnalysisItem
from src.agent.nodes import (

    _apply_guardrails, _band_to_direction,
    llm_filter_node, _has_explicit_neutral_marker,
)


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

    def test_high_score_bearish_preserved(self):
        """重大利空(高分)保持 bearish 方向，不再被 score 翻转为 bullish（解耦修复）"""
        item = NewsAnalysisItem(
            title="半导体板块暴跌", market_impact_score=8.0,
            impact_band=ImpactBand.BEARISH, confidence=Confidence.HIGH,
            impact_reason="板块大跌利空",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BEARISH
        assert result[0].sentiment == "bearish"

    def test_low_score_directional_forced_neutral(self):
        """低分(sc<4)方向性 band 强制中性（score 不再反推方向）"""
        item = NewsAnalysisItem(
            title="测试", market_impact_score=2.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.LOW,
            impact_reason="弱信号",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.NEUTRAL

    def test_mixed_score_neutral_band_kept(self):
        item = NewsAnalysisItem(
            title="测试", market_impact_score=5.0,
            impact_band=ImpactBand.MIXED, confidence=Confidence.MEDIUM,
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.MIXED


class TestGuardrailChainNoContamination:
    """analysis_chain 标签回显不得污染方向推断（实证: 寒武纪章程修订误标 bearish）"""

    def test_neutral_reason_with_bearish_chain_forces_neutral(self):
        """reason='常规章程修订,无实质经营影响' + chain 含'弱利空' → 应判 neutral,
        而非被 chain 的'利空'污染判 bearish 后跳过中性护栏"""
        item = NewsAnalysisItem(
            title="寒武纪:关于变更注册资本及修订《公司章程》的公告",
            market_impact_score=3.0,
            impact_band=ImpactBand.BEARISH, confidence=Confidence.MEDIUM,
            impact_reason="常规章程修订,无实质经营影响",
            analysis_chain="寒武纪变更注册资本→非重大事项→弱利空→高置信",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.NEUTRAL

    def test_bullish_reason_preserved_with_bearish_chain(self):
        """reason 含'利好'且 band 本就 bullish → chain 不影响，保持 bullish"""
        item = NewsAnalysisItem(
            title="半导体龙头业绩预增",
            market_impact_score=8.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
            impact_reason="业绩超预期,利好长期发展",
            analysis_chain="业绩预增→板块龙头→强利好→高置信",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BULLISH

    def test_text_dir_infer_reason_only(self):
        from src.agent.nodes import _infer_direction_from_text
        # chain 含'弱利空'但 reason 中性 → neutral（不并入 chain）
        assert _infer_direction_from_text("常规章程修订,无实质经营影响", "弱利空") == "neutral"
        # reason 为空时退用 chain
        assert _infer_direction_from_text("", "弱利空→偏负面") == "bearish"


class TestExplicitNeutralMarker:
    """P2-② 修复: LLM 写明'无明确多空信号/无实质影响'等 → 强制中性，盖过'回购'等关键词误触发"""

    def test_detector(self):
        assert _has_explicit_neutral_marker("无明确多空信号，例行披露") is True
        assert _has_explicit_neutral_marker("业绩超预期利好") is False

    def test_atesi_bullish_band_forced_neutral_dict(self):
        """阿特斯'回购进展公告': LLM 标 mildly_bullish 但 reason 写'无明确多空信号' → 强制中性"""
        items = [{
            "title": "阿特斯:回购进展公告",
            "market_impact_score": 5.5,
            "impact_band": "mildly_bullish",
            "impact_reason": "无明确多空信号，属例行披露",
            "analysis_chain": "",
            "impact_direction": "bullish",
        }]
        result = _apply_guardrails(items)
        assert result[0]["impact_band"] == "neutral"
        assert result[0]["impact_direction"] == "neutral"

    def test_atesi_bullish_band_forced_neutral_item(self):
        item = NewsAnalysisItem(
            title="阿特斯:回购进展公告",
            market_impact_score=5.5,
            impact_band=ImpactBand.MILDLY_BULLISH,
            confidence=Confidence.MEDIUM,
            impact_reason="无明确多空信号，例行披露",
            analysis_chain="",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.NEUTRAL
        assert result[0].sentiment == "neutral"


class TestLLMFilterIdxMerge:
    """P0-1 修复: LLM 回显 idx 后 merge 必须精确命中，标题改写也不丢失分析"""

    def _run_with_fake(self, monkeypatch, prefiltered, fake_return_builder):
        def fake_analyze(batch, deadline=0):
            return fake_return_builder(batch)
        monkeypatch.setattr("src.agent.nodes._llm_analyze_batch_structured", fake_analyze)
        return llm_filter_node({"prefiltered_news": prefiltered, "raw_news": []})

    def test_idx_merge_applies_llm_analysis_despite_title_rewrite(self, monkeypatch):
        """LLM 回显 idx 但改写 title（实跑中 57% 不匹配的根因）→ 仍应精确命中并应用分析"""
        prefiltered = [
            {"title": "某公司签订重大合同公告", "content": "", "category": "news",
             "published_at": "2026-07-29 10:00:00", "name": "", "affected_stocks": [],
             "source": "财联社"},
            {"title": "另一公司发布业绩预增预告", "content": "", "category": "news",
             "published_at": "2026-07-29 11:00:00", "name": "", "affected_stocks": [],
             "source": "证券时报"},
        ]

        def builder(batch):
            out = []
            for n in batch:
                i = n.get("idx")
                out.append({
                    "idx": i,
                    "title": f"LLM改写标题_{i}",  # 与原始标题不同
                    "market_impact_score": 9.0,
                    "impact_band": "bullish",
                    "impact_reason": f"LLM深度分析{i}",
                    "affected_sectors": ["半导体"],
                    "affected_stocks": [],
                    "confidence": "high",
                    "influence_scope": "sector",
                    "analysis_chain": "",
                })
            return out

        result = self._run_with_fake(monkeypatch, prefiltered, builder)
        filtered = result["filtered_news"]
        assert len(filtered) == 2
        # 关键: LLM 分析被应用（reason 来自 LLM，而非规则兜底文案），证明 idx 命中
        assert all("LLM深度分析" in n.get("impact_reason", "") for n in filtered)
        assert all(n.get("market_impact_score") == 9.0 for n in filtered)
        # idx 临时字段已清理，不泄漏到输出
        assert all("idx" not in n for n in filtered)

    def test_title_only_fallback_still_works(self, monkeypatch):
        """LLM 未回显 idx（兼容老格式）时，title 兜底仍命中"""
        prefiltered = [
            {"title": "同一标题A", "content": "", "category": "news",
             "published_at": "2026-07-29 10:00:00", "name": "", "affected_stocks": [],
             "source": "财联社"},
        ]

        def builder(batch):
            return [{
                "title": "同一标题A",  # 不带 idx（老格式）
                "market_impact_score": 7.0,
                "impact_band": "mildly_bullish",
                "impact_reason": "兜底命中",
                "affected_sectors": [],
                "affected_stocks": [],
                "confidence": "medium",
                "influence_scope": "stock",
                "analysis_chain": "",
            }]

        result = self._run_with_fake(monkeypatch, prefiltered, builder)
        filtered = result["filtered_news"]
        assert "兜底命中" in filtered[0].get("impact_reason", "")


class TestLowScoreNeutralAndGeopoliticalGuardrails:
    """低分方向性 band 强制中性 + 地缘风险方向校正（第八轮残余问题修复）"""

    def test_low_score_bullish_forced_neutral_item(self):
        """sc<4 却标 bullish（如特朗普×联合航空机场扩建 sc=3.0）→ 强制 neutral"""
        item = NewsAnalysisItem(
            title="特朗普宣布机场扩建计划",
            market_impact_score=3.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.LOW,
            impact_reason="主题性事件,影响有限", analysis_chain="",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.NEUTRAL
        assert result[0].sentiment == "neutral"

    def test_low_score_bearish_forced_neutral_item(self):
        """sc<4 却标 bearish → 同样强制 neutral（弱信号不带动市场情绪）"""
        item = NewsAnalysisItem(
            title="某小盘股公告",
            market_impact_score=3.5,
            impact_band=ImpactBand.BEARISH, confidence=Confidence.LOW,
            impact_reason="业绩小幅波动", analysis_chain="",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.NEUTRAL

    def test_low_score_neutral_skips_high_score(self):
        """sc>=4 的方向性 band 不被中性化（避免误伤真实高影响资讯）"""
        item = NewsAnalysisItem(
            title="某龙头业绩超预期",
            market_impact_score=7.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
            impact_reason="业绩超预期利好", analysis_chain="",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BULLISH

    def test_low_score_forced_neutral_dict(self):
        items = [{
            "title": "弱影响个股事件",
            "market_impact_score": 3.0,
            "impact_band": "bearish",
            "impact_reason": "影响有限",
            "analysis_chain": "",
        }]
        result = _apply_guardrails(items)
        assert result[0]["impact_band"] == "neutral"

    def test_geopolitical_risk_detector(self):
        from src.agent.nodes import _has_geopolitical_risk
        assert _has_geopolitical_risk("美军警告驻中东士兵 冲突升级") is True
        # 含明确受益涨幅词（原油大涨因冲突）→ 不翻转，应为 False
        assert _has_geopolitical_risk("原油大涨因中东地缘冲突避险") is False

    def test_geopolitical_risk_flips_bullish_to_bearish_item(self):
        """#9 美军警告驻中东士兵 sc=8.0 标 bullish → 实为 risk-off，翻 bearish"""
        item = NewsAnalysisItem(
            title="美军警告驻中东士兵做好安全防护",
            market_impact_score=8.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
            impact_reason="中东局势紧张", analysis_chain="",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BEARISH

    def test_geopolitical_risk_not_flip_when_benefit(self):
        """地缘事件同时描述原油受益（含'涨'）→ 不翻转，保留 bullish（真实原油利好）"""
        item = NewsAnalysisItem(
            title="原油价格大涨因中东地缘冲突",
            market_impact_score=8.0,
            impact_band=ImpactBand.BULLISH, confidence=Confidence.HIGH,
            impact_reason="地缘冲突推升油价,原油大涨", analysis_chain="",
        )
        result = _apply_guardrails([item])
        assert result[0].impact_band == ImpactBand.BULLISH

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
