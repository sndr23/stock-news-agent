# filepath: tests/test_self_only_prefilter.py
"""预筛剔除"仅影响个股自身"的非龙头资讯（用户核心需求：最终只保留能带动市场情绪的价值资讯）

判定逻辑 (calculators._is_self_only_individual_stock)：
  - 公告类 → False（由 is_leader_or_high_impact 处理）
  - 龙头股命中（沪深300/科技龙头 名称/代码）→ False（带动板块情绪，保留）
  - 未提及具体个股（无 name/code/6位A股代码）→ False（保守保留，交 ranking 的 scope 沉底）
  - 含板块/宏观关键词 → False（具备板块或市场联动，保留）
  - 其余：提及某非龙头个股 + 无板块/市场联动 → True（仅影响自身，剔除）
"""
import pytest
from unittest.mock import patch
from src.tools.calculators import _is_self_only_individual_stock
from src.agent.nodes import _python_prefilter

_EMPTY_HS300 = {"codes": set(), "names": set()}


class TestIsSelfOnlyIndividualStock:
    """_is_self_only_individual_stock 单元判定"""

    def test_non_leader_stock_without_sector_keyword_is_self_only(self):
        """非龙头个股、无板块/宏观联动 → 仅影响自身，应剔除"""
        news = {"title": "某某科技业绩预增", "content": "公司Q2净利增长", "name": "某某科技", "code": "605888"}
        assert _is_self_only_individual_stock(news, _EMPTY_HS300) is True

    def test_six_digit_code_counts_as_specific_stock(self):
        """标题含 6 位 A 股代码（无 name 字段）→ 具体个股，无板块关键词 → 剔除"""
        news = {"title": "603259公司签订供货协议", "content": "日常经营合同"}
        assert _is_self_only_individual_stock(news, _EMPTY_HS300) is True

    def test_leader_stock_preserved(self):
        """龙头股（科技龙头名称）命中 → 带动板块情绪，保留"""
        news = {"title": "中际旭创业绩超预期", "content": "光模块龙头", "name": "中际旭创"}
        assert _is_self_only_individual_stock(news, _EMPTY_HS300) is False

    def test_sector_keyword_preserved(self):
        """非龙头个股但含板块关键词（半导体）→ 具备板块联动，保留"""
        news = {"title": "某公司半导体设备中标", "content": "国产替代", "name": "某公司", "code": "605888"}
        assert _is_self_only_individual_stock(news, _EMPTY_HS300) is False

    def test_macro_keyword_preserved(self):
        """含宏观关键词（央行/美联储）→ 市场联动，保留"""
        news = {"title": "某公司受益美联储降息", "content": "流动性宽松", "name": "某公司", "code": "605888"}
        assert _is_self_only_individual_stock(news, _EMPTY_HS300) is False

    def test_no_specific_stock_conservative_keep(self):
        """未提及具体个股 → 保守保留，由 ranking 的 scope 维度处理"""
        news = {"title": "行业景气度回升", "content": "整体需求改善"}
        assert _is_self_only_individual_stock(news, _EMPTY_HS300) is False

    def test_announcement_not_handled_here(self):
        """公告类 → False（交由 is_leader_or_high_impact 决定）"""
        news = {"title": "某公司董事会决议公告", "content": "常规公告", "name": "某公司", "code": "605888", "category": "announcement"}
        assert _is_self_only_individual_stock(news, _EMPTY_HS300) is False


@pytest.fixture(autouse=True)
def mock_hs300():
    with patch("src.agent.nodes.get_hs300_constituents", return_value=_EMPTY_HS300), \
         patch("src.tools.data_fetchers.get_hs300_constituents", return_value=_EMPTY_HS300):
        yield


class TestPrefilterSelfOnlyIntegration:
    """_python_prefilter 集成：仅影响自身的非龙头个股应在预筛被剔除"""

    def test_self_only_removed_at_prefilter(self):
        news = [
            {"title": "某某科技业绩预增", "content": "公司Q2净利增长", "url": "", "source": "财联社", "name": "某某科技", "code": "605888"},
            {"title": "美联储释放降息信号", "content": "全球流动性宽松", "url": "", "source": "新华社"},
            {"title": "半导体板块国产替代加速", "content": "光模块需求爆发", "url": "", "source": "财联社"},
        ]
        kept, removed = _python_prefilter(news, top_n=40)
        titles = [n["title"] for n in kept]
        assert "某某科技业绩预增" not in titles, "仅影响自身的非龙头个股应在预筛剔除"
        assert "美联储释放降息信号" in titles, "市场级资讯应保留"
        assert "半导体板块国产替代加速" in titles, "板块级资讯应保留"

    def test_mixed_self_only_counted_in_removed(self):
        news = [
            {"title": f"60{i:04d}公司签订日常合同", "content": "经营合同", "url": "", "source": ""}
            for i in range(10)
        ]
        kept, removed = _python_prefilter(news, top_n=40)
        assert len(kept) == 0, "10 条全为仅影响自身的非龙头个股，应全部剔除"
