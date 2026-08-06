# filepath: tests/test_rank.py
"""测试综合排名：band 主序 + confidence 加权 + 冲突降级 + 沪深300过滤"""
import pytest
from unittest.mock import patch
from src.tools.calculators import (
    rank_news, BAND_PRIORITY, CONFIDENCE_WEIGHT,
    _band_direction_conflict, _downgrade_band,
    _calc_continuous_score, _is_hs300_stock,
)


class TestBandPriority:
    def test_bullish_highest(self):
        assert BAND_PRIORITY["bullish"] == 6

    def test_bearish_above_neutral(self):
        # 重大利空(退市/立案/暴雷)应排在中性前面，不再垫底
        assert BAND_PRIORITY["bearish"] > BAND_PRIORITY["neutral"]

    def test_bullish_equal_bearish(self):
        # 强利好与强利空并列（重要资讯在前，不区分利空利好）
        assert BAND_PRIORITY["bullish"] == BAND_PRIORITY["bearish"]

    def test_mixed_above_neutral(self):
        assert BAND_PRIORITY["mixed"] > BAND_PRIORITY["neutral"]

class TestConfidenceWeight:
    def test_high_is_1(self):
        assert CONFIDENCE_WEIGHT["high"] == 1.0

    def test_low_is_0_7(self):
        assert CONFIDENCE_WEIGHT["low"] == 0.7

    def test_medium_between(self):
        # medium 惩罚 0.90（修复 confidence 惩罚抵消 LLM 1 分差距导致的感知倒挂）
        assert CONFIDENCE_WEIGHT["medium"] == 0.90


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

    def test_mildly_bullish_downgrade(self):
        assert _downgrade_band("mildly_bullish") == "neutral"

    def test_neutral_stays(self):
        assert _downgrade_band("neutral") == "neutral"

    def test_bearish_downgrade(self):
        assert _downgrade_band("bearish") == "mildly_bearish"

    def test_mildly_bearish_downgrade(self):
        assert _downgrade_band("mildly_bearish") == "neutral"

    def test_mixed_downgrade(self):
        assert _downgrade_band("mixed") == "neutral"


class TestRankNews:
    @pytest.fixture(autouse=True)
    def mock_hs300(self):
        """mock get_hs300_constituents 避免测试发起真实 akshare 网络请求

        rank_news 内部从 src.tools.data_fetchers 延迟导入 get_hs300_constituents，
        不 mock 会触发真实网络调用（实测单次 54s，CI 不稳定）。
        """
        with patch("src.tools.data_fetchers.get_hs300_constituents",
                   return_value={"codes": set(), "names": set()}):
            yield

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


class TestHS300Filter:
    """沪深300成分股过滤：非沪深300个股降权，ST+非沪深300双重降权"""

    _HS300_MOCK = {"codes": {"000001", "600519"}, "names": {"平安银行", "贵州茅台"}}

    def _make_stock_news(self, title, affected_stocks, direction="bullish", score=8.0,
                         category="news", band=None, name="", code=""):
        if band is None:
            band = "bullish" if direction == "bullish" else "bearish"
        return {
            "title": title, "source": "财联社", "content": "", "published_at": "",
            "category": category, "sentiment": direction, "impact_direction": direction,
            "market_impact_score": score, "impact_band": band, "confidence": "medium",
            "affected_sectors": [], "affected_stocks": affected_stocks, "cluster_weight": 0,
            "name": name, "code": code,
        }

    def test_is_hs300_stock_by_code(self):
        assert _is_hs300_stock("任意名", "000001", self._HS300_MOCK) is True

    def test_is_hs300_stock_by_name(self):
        assert _is_hs300_stock("平安银行", "", self._HS300_MOCK) is True

    def test_not_hs300_stock(self):
        assert _is_hs300_stock("某小盘股", "999999", self._HS300_MOCK) is False

    def test_hs300_empty_returns_true(self):
        """hs300 为空（获取失败）时保守不降权"""
        assert _is_hs300_stock("某ST股", "999999", {}) is True
        assert _is_hs300_stock("某ST股", "999999", None) is True

    def test_non_hs300_stock_downweighted(self):
        """非沪深300个股分数应低于沪深300个股（同档同分）"""
        n1 = self._make_stock_news("平安银行利好", ["平安银行"])
        n2 = self._make_stock_news("某小盘股利好", ["某小盘股"])
        score1 = _calc_continuous_score(n1, self._HS300_MOCK)
        score2 = _calc_continuous_score(n2, self._HS300_MOCK)
        assert score1 > score2, f"沪深300({score1})应高于非沪深300({score2})"

    def test_st_non_hs300_double_downweighted(self):
        """ST + 非沪深300 应双重降权，低于普通非沪深300"""
        n1 = self._make_stock_news("某小盘股利好", ["某小盘股"])
        n2 = self._make_stock_news("*ST公司立案调查", ["*ST公司"],
                                    direction="bearish", score=2.0, band="bearish")
        score1 = _calc_continuous_score(n1, self._HS300_MOCK)
        score2 = _calc_continuous_score(n2, self._HS300_MOCK)
        assert score1 > score2, f"普通非沪深300({score1})应高于ST+非沪深300({score2})"

    def test_no_stock_info_not_downweighted(self):
        """无个股信息的板块资讯不应被降权"""
        n1 = {
            "title": "半导体板块利好", "source": "财联社", "content": "", "published_at": "",
            "category": "news", "sentiment": "bullish", "impact_direction": "bullish",
            "market_impact_score": 8.0, "impact_band": "bullish", "confidence": "medium",
            "affected_sectors": ["半导体"], "affected_stocks": [], "cluster_weight": 0,
        }
        score_with_hs300 = _calc_continuous_score(n1, self._HS300_MOCK)
        score_no_hs300 = _calc_continuous_score(n1, None)
        assert abs(score_with_hs300 - score_no_hs300) < 0.001

    def test_rank_news_non_hs300_ranks_lower(self, monkeypatch):
        """rank_news 中非沪深300个股应排在沪深300之后"""
        from src.tools import data_fetchers
        monkeypatch.setattr(
            data_fetchers, "get_hs300_constituents",
            lambda: self._HS300_MOCK
        )
        news = [
            self._make_stock_news("某小盘股利好", ["某小盘股"]),
            self._make_stock_news("平安银行利好", ["平安银行"]),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "平安银行利好"
        assert ranked[1]["title"] == "某小盘股利好"

    def test_announcement_non_hs300_downweighted(self, monkeypatch):
        """非沪深300公告类应低于沪深300公告类"""
        from src.tools import data_fetchers
        monkeypatch.setattr(
            data_fetchers, "get_hs300_constituents",
            lambda: self._HS300_MOCK
        )
        news = [
            self._make_stock_news("某小盘股业绩预增", ["某小盘股"],
                                  category="announcement", name="某小盘股", code="999999"),
            self._make_stock_news("平安银行业绩预增", ["平安银行"],
                                  category="announcement", name="平安银行", code="000001"),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "平安银行业绩预增"


class TestTechWeighting:
    """科技板块统一加权 / 非科技统一降权"""

    def _make_news(self, title, direction="bullish", score=8.0, category="news"):
        band = "bullish" if direction == "bullish" else ("bearish" if direction == "bearish" else "neutral")
        return {
            "title": title, "source": "财联社", "content": "", "published_at": "",
            "category": category, "sentiment": direction, "impact_direction": direction,
            "market_impact_score": score, "impact_band": band, "confidence": "medium",
            "affected_sectors": [], "affected_stocks": [], "cluster_weight": 0,
        }

    def test_tech_bullish_vs_non_tech_bullish(self):
        """科技利好应高于非科技利好（同档同分）"""
        n1 = self._make_news("半导体板块利好", direction="bullish")
        n2 = self._make_news("某消费股利好", direction="bullish")
        s1 = _calc_continuous_score(n1, None)
        s2 = _calc_continuous_score(n2, None)
        assert s1 > s2, f"科技({s1})应高于非科技({s2})"

    def test_tech_bearish_vs_non_tech_bearish(self):
        """科技利空应高于非科技利空（同档同分，科技不管方向都加权）"""
        n1 = self._make_news("半导体板块利空跌停", direction="bearish", score=2.0)
        n2 = self._make_news("某消费股利空跌停", direction="bearish", score=2.0)
        s1 = _calc_continuous_score(n1, None)
        s2 = _calc_continuous_score(n2, None)
        assert s1 > s2, f"科技利空({s1})应高于非科技利空({s2})"

    def test_tech_neutral_vs_non_tech_neutral(self):
        """科技中性应高于非科技中性（同档同分）"""
        n1 = self._make_news("半导体行业会议召开", direction="neutral", score=5.0)
        n2 = self._make_news("某消费行业会议召开", direction="neutral", score=5.0)
        s1 = _calc_continuous_score(n1, None)
        s2 = _calc_continuous_score(n2, None)
        assert s1 > s2, f"科技中性({s1})应高于非科技中性({s2})"

    def test_non_tech_downweighted(self):
        """非科技资讯应被降权（×0.85）"""
        n = self._make_news("某消费股利好", direction="bullish")
        score = _calc_continuous_score(n, None)
        # 手动计算：无科技、无国家级、非沪深300（hs300=None 不降权）
        # base = 0.15*cred + 0.70*llm_impact + 0 = 0.15*0.89 + 0.70*0.8 = 0.1335 + 0.56 = 0.6935
        # sentiment_factor = 1.0 (bullish)
        # total = 0.6935
        # 非科技降权 ×0.85 = 0.5895
        # 影响范围: 无 sectors/无 hs300 → stock ×1.00
        # 验证确实被降权了（低于未降权的 0.6935）
        assert score < 0.6935, f"非科技应被降权({score} < 0.6935)"

    def test_cpo_keyword_weighted(self):
        """CPO关键词命中应获科技加成"""
        n = self._make_news("CPO光模块技术突破", direction="bullish")
        score = _calc_continuous_score(n, None)
        # 科技加成 ×1.20，应明显高于非科技
        n2 = self._make_news("某服装品牌涨价", direction="bullish")
        score2 = _calc_continuous_score(n2, None)
        assert score > score2

    def test_pcb_keyword_weighted(self):
        """PCB关键词命中应获科技加成"""
        n = self._make_news("PCB覆铜板涨价", direction="bullish")
        n2 = self._make_news("某食品股利好", direction="bullish")
        assert _calc_continuous_score(n, None) > _calc_continuous_score(n2, None)

    def test_rank_news_tech_above_non_tech(self, monkeypatch):
        """rank_news 中科技资讯应排在非科技之前"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        news = [
            self._make_news("某消费股利好", direction="bullish"),
            self._make_news("半导体芯片涨价利好", direction="bullish"),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "半导体芯片涨价利好"


class TestInfluenceScope:
    """影响范围加权：市场级 > 板块级 > 个股级"""

    _HS300_MOCK = {"codes": {"300308"}, "names": {"中际旭创"}}

    def _make_news(self, title, scope="", sectors=None, stocks=None,
                   direction="bullish", score=8.0, source="财联社"):
        # band 与 direction 正确映射（原实现 neutral 被误映射为 bearish，
        # 导致 band 主序测试在同档内竞争，无法验证设计意图）
        if direction == "bullish":
            band = "bullish"
        elif direction == "bearish":
            band = "bearish"
        else:
            band = "neutral"
        d = {
            "title": title, "source": source, "content": "", "published_at": "",
            "category": "news", "sentiment": direction, "impact_direction": direction,
            "market_impact_score": score, "impact_band": band, "confidence": "high",
            "affected_sectors": sectors or [], "affected_stocks": stocks or [],
            "cluster_weight": 0,
        }
        if scope:
            d["influence_scope"] = scope
        return d

    def test_rank_scope_priority_market_sector_stock(self, monkeypatch):
        """rank_news 中 scope 加成：同 band 同分时 market > sector > stock（scope_boost 差异）"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        # 三条同分同 band，无科技词差异（确保 scope_boost 是唯一区分因素）
        news = [
            self._make_news("某常规个股公告A", scope="stock", score=5.0),
            self._make_news("某常规板块公告B", scope="sector", score=5.0),
            self._make_news("某常规市场公告C", scope="market", score=5.0),
        ]
        ranked = rank_news(news)
        # 同 band(bullish) 同 base score，scope_boost: market+0.12 > sector+0.0 > stock-0.05
        assert [n["influence_scope"] for n in ranked] == ["market", "sector", "stock"]

    def test_rank_scope_overrides_inference(self, monkeypatch):
        """LLM 输出 market 应排在规则推断 stock 之前（rank 层面）"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        n_market = self._make_news("某小盘股公告", scope="market")
        n_stock = self._make_news("另一小盘股公告", scope="stock")
        ranked = rank_news([n_market, n_stock])
        assert ranked[0]["influence_scope"] == "market"

    def test_rank_market_neutral_above_stock_bullish(self, monkeypatch):
        """band 主序优先于 scope：个股级利好(band=6)应排市场级中性(band=1)前面
        旧设计 scope 绝对主键会反转此序，导致低分 market+neutral 压住高分 stock+bullish；
        新设计 band 为主键，scope 仅作分数加成，方向性资讯不再被中性宏观压底。"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        news = [
            self._make_news("美联储释放降息信号", scope="market", direction="neutral", score=5.0),
            self._make_news("某龙头股业绩大涨", scope="stock", direction="bullish", score=9.0),
        ]
        ranked = rank_news(news)
        # band_priority: bullish(6) > neutral(1)，stock+bullish 排前
        assert ranked[0]["influence_scope"] == "stock"
        assert ranked[0]["title"] == "某龙头股业绩大涨"

    def test_infer_sector_by_sectors(self):
        """有affected_sectors时推断为sector"""
        from src.tools.calculators import _infer_influence_scope
        n = self._make_news("半导体涨价", sectors=["半导体"])
        assert _infer_influence_scope(n, None) == "sector"

    def test_infer_sector_by_hs300_leader(self):
        """沪深300龙头股推断为sector（龙头带动效应）"""
        from src.tools.calculators import _infer_influence_scope
        n = self._make_news("中际旭创业绩预增", stocks=["中际旭创"])
        assert _infer_influence_scope(n, self._HS300_MOCK) == "sector"

    def test_infer_stock_no_sectors_no_leader(self):
        """无板块/非龙头推断为stock"""
        from src.tools.calculators import _infer_influence_scope
        n = self._make_news("某小盘股公告", stocks=["某小盘股"])
        assert _infer_influence_scope(n, self._HS300_MOCK) == "stock"

    def test_rank_news_market_above_stock(self, monkeypatch):
        """rank_news中市场级应排在个股级前面（同band同score）"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        news = [
            self._make_news("某小盘股业绩预增", scope="stock"),
            self._make_news("央行全面降准0.5个百分点", scope="market"),
        ]
        ranked = rank_news(news)
        assert ranked[0]["title"] == "央行全面降准0.5个百分点"
        assert ranked[0]["influence_scope"] == "market"

    def test_ranked_item_has_scope_field(self, monkeypatch):
        """RankedNewsItem应包含influence_scope字段"""
        from src.tools import data_fetchers
        monkeypatch.setattr(data_fetchers, "get_hs300_constituents", lambda: {})
        news = [self._make_news("测试", scope="sector")]
        ranked = rank_news(news)
        assert "influence_scope" in ranked[0]
        assert ranked[0]["influence_scope"] == "sector"


# ============================================================
# LLM 调分 + 同 scope 同 band 内微调 (nodes._llm_adjust_scores)
# ============================================================
from src.agent.nodes import _llm_adjust_scores


class TestLLMAdjustSameScopeSameBand:
    """LLM 调整影响分后，仅在同 scope 同 band 内按 total_score 重排，不跨档"""

    def _mk(self, title, band, score=5.0, scope="market"):
        return {"title": title, "impact_band": band, "market_impact_score": score,
                "total_score": score / 10, "category": "news", "confidence": "medium",
                "impact_direction": "neutral", "published_at": "2026-07-29 10:00:00",
                "influence_scope": scope,
                "band_priority": {"bullish": 6, "bearish": 6, "mixed": 6,
                                  "mildly_bullish": 4, "mildly_bearish": 4,
                                  "neutral": 1}.get(band, 1),
                "time_factor": 1.0}

    @patch("src.tools.data_fetchers.get_hs300_constituents", return_value={"codes": set(), "names": set()})
    def test_cannot_cross_band(self, _mock_hs300):
        """LLM 不能跨 band 重排：bullish 组始终在 neutral 组之前"""
        news = [
            self._mk("利好A", "bullish", 6.0),
            self._mk("中性B", "neutral", 3.0),
            self._mk("中性C", "neutral", 3.0),
            self._mk("中性D", "neutral", 3.0),
        ]
        # LLM 把中性分数调到 10，利好调到 1 —— 仍不能跨档
        fake = {
            "adjustments": [
                {"title": "利好A", "adjusted_score": 1.0},
                {"title": "中性B", "adjusted_score": 10.0},
                {"title": "中性C", "adjusted_score": 10.0},
                {"title": "中性D", "adjusted_score": 10.0},
            ]
        }
        with patch("src.agent.nodes._call_llm_api", return_value=__import__("json").dumps(fake)):
            out = _llm_adjust_scores(news, top_n=20)
        # bullish 始终在 neutral 之前（band 主序不被破坏）
        assert out[0]["title"] == "利好A"
        # neutral 组在后
        bullish_idx = [i for i, n in enumerate(out) if n["impact_band"] == "bullish"][0]
        neutral_idx = [i for i, n in enumerate(out) if n["impact_band"] == "neutral"][0]
        assert bullish_idx < neutral_idx

    @patch("src.tools.data_fetchers.get_hs300_constituents", return_value={"codes": set(), "names": set()})
    def test_cannot_cross_scope(self, _mock_hs300):
        """LLM 不能跨 scope 重排：market 组始终在 stock 组之前"""
        news = [
            self._mk("宏观A", "neutral", 3.0, scope="market"),
            self._mk("个股B", "neutral", 8.0, scope="stock"),
            self._mk("个股C", "neutral", 7.0, scope="stock"),
            self._mk("个股D", "neutral", 6.0, scope="stock"),
        ]
        # LLM 把个股调到 10，宏观调到 1 —— 仍不能跨 scope
        fake = {
            "adjustments": [
                {"title": "宏观A", "adjusted_score": 1.0},
                {"title": "个股B", "adjusted_score": 10.0},
                {"title": "个股C", "adjusted_score": 10.0},
                {"title": "个股D", "adjusted_score": 10.0},
            ]
        }
        with patch("src.agent.nodes._call_llm_api", return_value=__import__("json").dumps(fake)):
            out = _llm_adjust_scores(news, top_n=20)
        # market scope 始终在 stock scope 之前
        market_idx = [i for i, n in enumerate(out) if n["influence_scope"] == "market"][0]
        stock_idx = [i for i, n in enumerate(out) if n["influence_scope"] == "stock"][0]
        assert market_idx < stock_idx

    @patch("src.tools.data_fetchers.get_hs300_constituents", return_value={"codes": set(), "names": set()})
    def test_reorder_within_same_group(self, _mock_hs300):
        """同 scope 同 band 内：LLM 调分后按 total_score 重排"""
        news = [
            self._mk("A", "bullish", 9.0, scope="sector"),
            self._mk("B", "bullish", 6.0, scope="sector"),
            self._mk("C", "bullish", 7.0, scope="sector"),
            self._mk("D", "bullish", 8.0, scope="sector"),
        ]
        # LLM 把 B 调到 10 —— B 应排第一
        fake = {
            "adjustments": [
                {"title": "A", "adjusted_score": 9.0},
                {"title": "B", "adjusted_score": 10.0},
                {"title": "C", "adjusted_score": 7.0},
                {"title": "D", "adjusted_score": 8.0},
            ]
        }
        with patch("src.agent.nodes._call_llm_api", return_value=__import__("json").dumps(fake)):
            out = _llm_adjust_scores(news, top_n=20)
        # 同组内按 total_score 降序，B 分最高排第一
        assert out[0]["title"] == "B"

    def test_small_list_no_adjust(self):
        news = [self._mk("x", "neutral"), self._mk("y", "neutral"), self._mk("z", "neutral")]
        assert _llm_adjust_scores(news) == news

    @patch("src.tools.data_fetchers.get_hs300_constituents", return_value={"codes": set(), "names": set()})
    def test_same_group_noncontiguous_not_split(self, _mock_hs300):
        """同 (band, scope) 组在输入中不连续时，修复前 groupby 会拆组、
        修复后按 dict 分组聚合并保持组间原始顺序。

        输入顺序: A(bullish,sector) B(neutral,market) C(bullish,sector) D(neutral,market)
        → 期望: bullish/sector 组(内按分排 C,A) → neutral/market 组(内按分排 D,B)
        """
        news = [
            self._mk("A", "bullish", 6.0, scope="sector"),
            self._mk("B", "neutral", 3.0, scope="market"),
            self._mk("C", "bullish", 9.0, scope="sector"),
            self._mk("D", "neutral", 5.0, scope="market"),
        ]
        fake = {
            "adjustments": [
                {"title": "A", "adjusted_score": 6.0},
                {"title": "B", "adjusted_score": 3.0},
                {"title": "C", "adjusted_score": 9.0},
                {"title": "D", "adjusted_score": 5.0},
            ]
        }
        with patch("src.agent.nodes._call_llm_api", return_value=__import__("json").dumps(fake)):
            out = _llm_adjust_scores(news, top_n=20)
        # 组间保持首次出现顺序：bullish/sector 组在前，neutral/market 组在后
        assert [n["title"] for n in out] == ["C", "A", "D", "B"]


class TestSafeParseJsonAdjustments:
    """adjustments 结构在非完美 JSON 时也必须被解析"""

    def test_perfect_adjustments_parsed(self):
        content = '{"adjustments": [{"title": "A", "adjusted_score": 8.5}, {"title": "B", "adjusted_score": 7.0}]}'
        parsed = _safe_parse_json(content)
        assert len(parsed.get("adjustments", [])) == 2

    def test_adjustments_with_trailing_text_recovered(self):
        content = (
            '{\n  "adjustments": [\n'
            '    {"title": "利好A", "adjusted_score": 9.0},\n'
            '    {"title": "利好B", "adjusted_score": 8.0}\n'
            '  ]\n}\n注：以上为调分建议'
        )
        parsed = _safe_parse_json(content)
        adjustments = parsed.get("adjustments", [])
        assert len(adjustments) == 2
        assert adjustments[0]["title"] == "利好A"


# ============================================================
# _safe_parse_json 的 ranking 结构兜底恢复 (P0-2 修复)
# ============================================================
from src.agent.nodes import _safe_parse_json



class TestSafeParseJsonRanking:
    """rerank 返回的 ranking 结构在非完美 JSON 时也必须被解析（否则智能重排静默失效）"""

    def test_perfect_ranking_parsed(self):
        content = '{"ranking": [{"title": "A", "final_rank": 1}, {"title": "B", "final_rank": 2}]}'
        parsed = _safe_parse_json(content)
        assert len(parsed.get("ranking", [])) == 2

    def test_ranking_with_trailing_text_recovered(self):
        """LLM 在 JSON 后追加说明文字 → 首层 json.loads 失败，必须逐字符恢复 ranking"""
        content = (
            '{\n  "ranking": [\n'
            '    {"title": "利好A", "final_rank": 1},\n'
            '    {"title": "利好B", "final_rank": 2}\n'
            '  ]\n}\n注：以上为最终排序建议'
        )
        parsed = _safe_parse_json(content)
        ranking = parsed.get("ranking", [])
        assert len(ranking) == 2
        assert ranking[0]["title"] == "利好A"

    def test_ranking_without_fenced_block_recovered(self):
        """rerank 未用代码块包裹且含尾部文本 → 仍能恢复"""
        content = '{"ranking": [{"title": "X", "final_rank": 3}]} 补充：已综合考虑时效性'
        parsed = _safe_parse_json(content)
        assert len(parsed.get("ranking", [])) == 1

    def test_filtered_news_still_works(self):
        """ranking 恢复不影响原有的 filtered_news 解析路径"""
        content = '{"filtered_news": [{"title": "T", "market_impact_score": 8, "impact_band": "bullish"}]}'
        parsed = _safe_parse_json(content)
        assert len(parsed.get("filtered_news", [])) == 1

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
