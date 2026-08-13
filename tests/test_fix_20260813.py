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


# ============================================================
# 6. 2026-08-13 审核修复：盘面异动词表 + 预筛主体词 + prompt + 板块政策合并
# ============================================================
class TestIntradayNoiseExpanded:
    def test_concept_active_filtered(self):
        """盘面异动漏网："PCB概念表现活跃 方邦股份涨超10%" → 盘面异动不推"""
        t = "PCB概念表现活跃 方邦股份涨超10%"
        assert rtp._is_noise_push({"title": t}, {"is_leader_stock": False}, set()) == "盘面异动"

    def test_leader_active_kept(self):
        """龙头"表现活跃"仍放行（中际旭创是龙头）"""
        t = "中际旭创表现活跃 成交额超百亿"
        assert rtp._is_noise_push({"title": t}, {"is_leader_stock": True}, set()) == ""


class TestHeadlineEntityPrefilter:
    def test_tech_leader_events_direct_llm(self):
        """科技龙头发布/里程碑/适配类不再被预筛丢弃（此前 0.14 被拦）"""
        cases = [
            "DeepSeek V4 Pro 正式版API上线 大幅增强Agent能力",
            "长江存储首次跻身全球NAND出货量前三 超越铠侠",
            "寒武纪董事长陈天石：五大国产模型完成适配",
            "腾讯单季营收首超2000亿元",
            "中国钙钛矿量产技术再登《自然》",
        ]
        for t in cases:
            score, hit = rtp._prefilter({"title": t, "content": "", "category": "news"})
            assert hit, f"科技龙头/重要主体事件应直通 LLM: {t} (score={score})"

    def test_ordinary_company_not_bypassed(self):
        """无主体词、无动作词、无金额的普通消息不直通"""
        _, hit = rtp._prefilter({"title": "某公司召开例行股东大会", "content": "", "category": "news"})
        assert not hit


class TestPromptRejectsResearch:
    def test_prompt_rejects_research_views(self):
        """LLM prompt 明确不推研报/机构观点/主题性分析"""
        p = rtp._LLM_SYSTEM_PROMPT
        assert "券商研报" in p or "机构观点" in p
        assert "主题性分析" in p


class TestSameEventPolicyMerge:
    def test_sichuan_compute_policy_merged(self):
        """四川算力政策同轮双推 → 应判同事件（同实体+同板块+无事件组）"""
        a = rtp._push_event_sig(
            {"title": "四川：建强成都平原算力核心区 打造攀西—川西北算电融合发展带", "content": ""},
            {"entities": ["四川省政府"], "sectors": ["东数西算", "算力"]})
        b = rtp._push_event_sig(
            {"title": "四川：布局建设万卡级以上智算集群 探索建设高安全算力设施", "content": ""},
            {"entities": ["四川省政府"], "sectors": ["东数西算", "智算集群", "算力"]})
        assert rtp._is_same_event(a, b)

    def test_different_sector_same_entity_not_merged(self):
        """守卫：同实体但板块交集<2（仅宽泛AI）→ 不同事件不误并"""
        a = rtp._push_event_sig(
            {"title": "英伟达发布新一代AI芯片", "content": ""},
            {"entities": ["英伟达"], "sectors": ["AI芯片"]})
        b = rtp._push_event_sig(
            {"title": "英伟达拿下大型数据中心订单", "content": ""},
            {"entities": ["英伟达"], "sectors": ["AI服务器"]})
        assert not rtp._is_same_event(a, b)


# ============================================================
# 7. 实体/板块归一化（2026-08-13 二轮：上海算力补贴×2 重复实证）
# ============================================================
class TestEntitySectorNormalize:
    def test_normalize_entity_code_suffix(self):
        """实体归一化：剥离股票代码后缀"""
        assert rtp._normalize_entity("台积电(TSM.N)") == "台积电"
        assert rtp._normalize_entity("苹果(AAPL.O)") == "苹果"

    def test_normalize_entity_alias(self):
        """实体归一化：别名映射"""
        assert rtp._normalize_entity("大摩") == "摩根士丹利"
        assert rtp._normalize_entity("三星电子") == "三星"
        assert rtp._normalize_entity("上海市") == "上海"

    def test_entity_overlap_alias(self):
        """大摩 vs 摩根士丹利 → 同实体"""
        assert rtp._entity_overlap({"大摩"}, {"摩根士丹利"})

    def test_sectors_overlap_substring(self):
        """板块子串包含：AI算力 vs AI+算力 → 交集≥2"""
        assert len(rtp._sectors_overlap({"AI算力", "大模型"}, {"AI", "算力"})) >= 2

    def test_shanghai_compute_policy_merged(self):
        """上海算力补贴×2：实体别名 + 板块子串交集 → 同事件"""
        a = rtp._push_event_sig(
            {"title": "上海：推广发放算力券模型券语料券 降低公共数据", "content": ""},
            {"entities": ["上海"], "sectors": ["AI算力", "大模型"]})
        b = rtp._push_event_sig(
            {"title": "上海：依法依规开展算力补贴 支持民营企业租用智算资源", "content": ""},
            {"entities": ["上海市"], "sectors": ["AI", "算力"]})
        assert rtp._is_same_event(a, b)


# ============================================================
# 8. 2026-08-13 全链路复审修复：tech_override 排除观点 + 饱和口径对齐
# ============================================================
class TestTechOverrideViewGuard:
    """P0：tech_override 不得放行研报/观点/主题类（即使 LLM 判 push=false）"""

    def _judge(self, **kw):
        j = {"push": False, "score": 6, "direction": "bullish", "scope": "sector",
             "sectors": ["AI"], "is_leader_stock": False}
        j.update(kw)
        return j

    def test_research_view_not_override(self):
        """机构称/市场空间 → 不放行（LLM 的 push=false 生效）"""
        n = {"title": "产业资本加速投入 机构称物理AI市场空间将迈向星辰大海", "content": "",
             "source": "东方财富快讯"}
        assert rtp._tech_override_enabled(n, self._judge(), set()) is False

    def test_transmission_view_not_override(self):
        """研报/传导类 → 不放行"""
        n = {"title": "AI服务器液冷投资向零部件纵深传导 液冷产业链迎订单验证关键窗口", "content": "",
             "source": "东方财富快讯"}
        assert rtp._tech_override_enabled(n, self._judge(sectors=["AI算力", "液冷散热"]), set()) is False

    def test_hard_event_still_override(self):
        """硬数据科技事件（无观点词）仍兜底放行"""
        n = {"title": "台积电部分CoWoS产品生产良率升至98%", "content": "", "source": "财联社"}
        assert rtp._tech_override_enabled(n, self._judge(sectors=["先进封装", "半导体"]), set()) is True

    def test_leader_hard_event_still_override(self):
        """科技龙头硬事件（无观点词）仍兜底放行"""
        n = {"title": "寒武纪签署50亿元大额订单", "content": "", "source": "东方财富快讯"}
        assert rtp._tech_override_enabled(
            n, self._judge(scope="stock", is_leader_stock=True, sectors=["AI芯片"]), set()) is True


class TestTopicSaturatedConsistency:
    """P1：饱和判定与合并口径对齐（板块子串包含 + 实体归一化）"""

    @staticmethod
    def _ts(hours_ago=2):
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def test_sector_substring_counts(self):
        """板块'AI算力' vs 已推'AI/算力'（实体空）→ 计入饱和"""
        sig = {"entities": [], "sectors": ["AI算力"], "stocks": [], "scope": "sector"}
        pushed = [{"sectors": ["AI", "算力"], "stocks": [], "entities": [], "t": self._ts()} for _ in range(5)]
        assert rtp._topic_saturated(sig, pushed) is True

    def test_entity_alias_counts(self):
        """实体'摩根士丹利'（归一化）vs 旧数据'大摩'（未归一化）→ 计入饱和"""
        sig = {"entities": ["摩根士丹利"], "sectors": [], "stocks": [], "scope": "sector"}
        pushed = [{"sectors": [], "stocks": [], "entities": ["大摩"], "t": self._ts()} for _ in range(5)]
        assert rtp._topic_saturated(sig, pushed) is True

    def test_different_topic_not_saturated(self):
        """不同板块不互相影响（回归）"""
        sig = {"entities": [], "sectors": ["光模块"], "stocks": [], "scope": "sector"}
        pushed = [{"sectors": ["存储"], "stocks": [], "entities": [], "t": self._ts()} for _ in range(5)]
        assert rtp._topic_saturated(sig, pushed) is False


pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
