# filepath: tests/test_fix_20260813.py
"""2026-08-13 缺陷修复回归测试

覆盖五类修复（均基于生产代码实测复现的漏判/误判）：
1. 噪声过滤误杀重大事件（栏目词内嵌高信号词/宏观数据应放行）
2. 预筛大额经营事件漏判（动作词+金额/科技词直通 LLM）
3. 科技词裸子串误命中（AI 词边界，DUBAI 不误判外围科技）
4. SimHash 短标题碰撞（<5 字标题不再误去重）
5. news 类事件指纹碰撞（无个股名时掺标题主语，不同公司同类事件不再互撞）
"""
import importlib.util
import os
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent


def _load_rtp():
    cwd = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(
            "real_time_push", _PROJECT_ROOT / "scripts" / "real_time_push.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.chdir(cwd)


rtp = _load_rtp()


# ============================================================
# 1. 噪声过滤误杀修复
# ============================================================
class TestNoisePushMajorEvent:
    def _judge(self, **kw):
        j = {"push": True, "score": 8, "direction": "bullish", "scope": "market",
             "is_leader_stock": False, "entities": []}
        j.update(kw)
        return j

    def test_column_embedding_rate_cut_kept(self):
        """晚间新闻精选：央行降准 → 剥离栏目词后含高信号词，放行"""
        assert rtp._is_noise_push({"title": "晚间新闻精选：央行宣布降准0.5个百分点 释放流动性"},
                                  self._judge(), set()) == ""

    def test_column_embedding_leader_deal_kept(self):
        """涨停分析：中际旭创获50亿大单涨停 → 含龙头高信号词，放行"""
        assert rtp._is_noise_push({"title": "涨停分析：中际旭创获50亿大单涨停"},
                                  self._judge(), set()) == ""

    def test_column_embedding_cpi_kept(self):
        """盘前速递：美国7月CPI超预期 → 含宏观数据词，放行"""
        assert rtp._is_noise_push({"title": "盘前速递：美国7月CPI超预期 美股期货跳水"},
                                  self._judge(), set()) == ""

    def test_pure_column_still_filtered(self):
        """纯栏目（无重大事件内容）仍过滤"""
        for t in ["财联社8月10日晚间新闻精选", "8月10日午间涨停分析",
                  "九点特供", "风口研报公司模拟芯片多轮涨价驱动周期反转"]:
            assert rtp._is_noise_push({"title": t}, self._judge(), set()) == "栏目汇总", t


# ============================================================
# 2. 预筛大额经营事件直通
# ============================================================
class TestMajorDealPrefilter:
    def test_leader_order_direct_llm(self):
        """科技/龙头大额订单/建厂不再被预筛丢弃（此前 0.19/0.39/0.14 被拦）"""
        cases = [
            "宁德时代签订200亿元储能订单",
            "贵州茅台中标50亿元重大工程合同",
            "SK海力士重启中国NAND工厂",
            "台积电投建3纳米新厂",
        ]
        for t in cases:
            score, hit = rtp._prefilter({"title": t, "content": "", "category": "news"})
            assert hit, f"大额经营事件应直通 LLM: {t} (score={score})"

    def test_small_deal_not_bypassed(self):
        """小额非科技经营动作不直通（防中小市值日常经营消息挤占）"""
        _, hit = rtp._prefilter({"title": "某公司签订供货协议 金额800万",
                                 "content": "", "category": "news"})
        assert not hit


# ============================================================
# 3. 科技词裸子串误命中修复
# ============================================================
class TestTechWordBoundary:
    def test_dubai_not_overseas_tech(self):
        """DUBAI 含 AI 但不应判外围科技（词边界）"""
        n = {"title": "DUBAI 迪拜主权基金增持美股", "content": "", "source": "富途全球快讯"}
        assert rtp._is_overseas_tech(n, []) is False
        assert rtp._is_domestic_tech(n, []) is False

    def test_openai_not_matched_by_ai_substring(self):
        """OpenAI 中的 AI 不应作为裸子串命中（OpenAI 靠 LLM prompt 判定，不靠词表兜底）"""
        n = {"title": "OpenAI发布新模型", "content": "", "source": "富途全球快讯"}
        assert rtp._is_overseas_tech(n, []) is False

    def test_real_tech_still_matched(self):
        """正常科技词仍命中：半导体/存储/GPU（词边界）"""
        n = {"title": "美股半导体板块大涨 GPU需求旺盛", "content": "", "source": "富途全球快讯"}
        assert rtp._is_overseas_tech(n, []) is True
        n2 = {"title": "国内存储芯片扩产", "content": "", "source": "东方财富快讯"}
        assert rtp._is_domestic_tech(n2, []) is True


# ============================================================
# 4. SimHash 短标题碰撞修复
# ============================================================
class TestSimhashShortTitle:
    def test_short_titles_not_collapsed(self):
        from src.tools.data_fetchers import dedup_news_3layer
        items = [{"title": "涨停", "url": "", "content": ""},
                 {"title": "跌停", "url": "", "content": ""},
                 {"title": "异动", "url": "", "content": ""},
                 {"title": "回购", "url": "", "content": ""}]
        out = dedup_news_3layer(items)
        assert len(out) == 4, f"短标题不应被 SimHash 误去重: {[i['title'] for i in out]}"


# ============================================================
# 5. news 类事件指纹碰撞修复
# ============================================================
class TestNewsFingerprintCollision:
    def _news(self, title, **kw):
        return {"title": title, "content": "", "published_at": "2026-08-13 10:00:00", **kw}

    def test_different_company_same_event_not_collide(self):
        """寒武纪回购5亿 vs 宁德时代回购5亿（news 类无个股名）→ 不同指纹"""
        a = rtp._news_fingerprint(self._news("寒武纪拟回购5亿元股份"))
        b = rtp._news_fingerprint(self._news("宁德时代拟回购5亿元股份"))
        assert a != b, "不同公司的同类事件不得共享指纹（漏推）"

    def test_different_company_same_deal_not_collide(self):
        """宁德时代签订200亿 vs 贵州茅台签订200亿 → 不同指纹"""
        a = rtp._news_fingerprint(self._news("宁德时代签订200亿元储能订单"))
        b = rtp._news_fingerprint(self._news("贵州茅台签订200亿元工程合同"))
        assert a != b

    def test_same_company_amount_insensitive_still_merged(self):
        """回归：有 name 字段时金额不敏感合并（寒武纪回购5亿 vs 5.5亿 同指纹）"""
        a = rtp._news_fingerprint(self._news("寒武纪回购5亿元", name="寒武纪"))
        b = rtp._news_fingerprint(self._news("寒武纪回购5.5亿元", name="寒武纪"))
        assert a == b

    def test_same_macro_multi_source_still_merged(self):
        """回归：央行降准多源报道（无个股名，标题主语一致）仍同指纹"""
        a = rtp._news_fingerprint(self._news("央行宣布降准0.5个百分点"))
        b = rtp._news_fingerprint(self._news("央行宣布降准0.5个百分点 释放长期流动性"))
        assert a == b


pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
