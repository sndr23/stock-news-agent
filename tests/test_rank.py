# filepath: tests/test_rank.py
"""测试综合排名：band 主序 + confidence 加权 + 冲突降级 + 沪深300过滤"""
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

    def test_bullish_above_bearish(self):
        # 强利好略优先于强利空
        assert BAND_PRIORITY["bullish"] > BAND_PRIORITY["bearish"]

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
        band = "bullish" if direction == "bullish" else "bearish"
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

    def test_llm_market_above_sector_above_stock(self):
        """LLM输出influence_scope: market > sector > stock"""
        n_market = self._make_news("央行降息", scope="market")
        n_sector = self._make_news("半导体政策利好", scope="sector")
        n_stock = self._make_news("某股业绩预增", scope="stock")
        s_m = _calc_continuous_score(n_market, None)
        s_s = _calc_continuous_score(n_sector, None)
        s_k = _calc_continuous_score(n_stock, None)
        assert s_m > s_s > s_k, f"market({s_m}) > sector({s_s}) > stock({s_k})"

    def test_llm_scope_overrides_inference(self):
        """LLM输出的influence_scope应优先于规则推断"""
        # LLM说market，但规则推断为stock（无sectors/无hs300）
        n = self._make_news("某小盘股公告", scope="market")
        score_llm = _calc_continuous_score(n, None)
        # 不设influence_scope，走规则推断 → stock
        n_no_scope = dict(n)
        del n_no_scope["influence_scope"]
        score_inferred = _calc_continuous_score(n_no_scope, None)
        assert score_llm > score_inferred, "LLM market应高于规则推断stock"

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
