# filepath: tests/test_realtime_push.py
"""实时推送脚本 (scripts/real_time_push.py) 单元测试

覆盖 2026-08-01 修复的三类问题：
1. 重复推送：推送级事件签名去重（_push_event_sig/_is_same_event/_merge_event_sig）
   + Gist 状态读-改-写合并（_merge_state）
2. 漏推：LLM 判定 idx 对齐（_llm_judge）+ JSON 损坏抢救（_rescue_judge_object）
   + 批次失败逐条降级 + strict 阈值放宽（_passes_threshold）
3. 关键词表收敛：两条管线共用 src.tools.keyword_tables
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent


def _load_rtp():
    """以文件路径加载 scripts/real_time_push.py（scripts 非包，且 import 时会 chdir）"""
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
# 关键词表共享（防再次漂移）
# ============================================================

class TestSharedKeywords:
    def test_realtime_uses_shared_high_signal(self):
        from src.tools.keyword_tables import HIGH_SIGNAL_KEYWORDS as SHARED
        assert rtp.HIGH_SIGNAL_KEYWORDS is SHARED

    def test_nodes_uses_shared_high_signal(self):
        from src.tools.keyword_tables import HIGH_SIGNAL_KEYWORDS as SHARED
        from src.agent import nodes
        assert nodes.HIGH_SIGNAL_KEYWORDS is SHARED

    def test_realtime_uses_shared_overseas_tech(self):
        from src.tools.keyword_tables import OVERSEAS_TECH_KEYWORDS, OVERSEAS_SOURCE_MARKERS
        assert rtp.OVERSEAS_TECH_KEYWORDS is OVERSEAS_TECH_KEYWORDS
        assert rtp.OVERSEAS_SOURCE_MARKERS is OVERSEAS_SOURCE_MARKERS

    def test_user_custom_signal_words_present(self):
        # 用户 2026-08-01 定制的重磅词不丢失
        for kw in ["算力", "英伟达", "中际旭创", "韩国", "降准", "美联储"]:
            assert kw in rtp.HIGH_SIGNAL_KEYWORDS


# ============================================================
# 推送级事件签名去重
# ============================================================

class TestIsSameEvent:
    def test_nxp_three_sources_same_event(self):
        """恩智浦收购Ambarella三源三推实证：LLM实体一致 → 同事件"""
        n1 = {"title": "恩智浦洽谈收购Ambarella", "content": ""}
        n2 = {"title": "据英国金融时报：恩智浦半导体(NXPI.O)正洽谈收购一家价值33亿美元的自动驾驶芯片公司", "content": ""}
        n3 = {"title": "安霸股价因传恩智浦洽谈收购而飙升", "content": ""}
        s1 = rtp._push_event_sig(n1, {"entities": ["恩智浦", "Ambarella"]})
        s2 = rtp._push_event_sig(n2, {"entities": ["恩智浦", "Ambarella"]})
        s3 = rtp._push_event_sig(n3, {"entities": ["安霸", "恩智浦"]})
        assert rtp._is_same_event(s1, s2)
        assert rtp._is_same_event(s1, s3)
        assert rtp._is_same_event(s2, s3)

    def test_same_stock_same_event_group(self):
        """寒武纪股权激励：同个股+同事件组 → 同事件"""
        a = rtp._push_event_sig(
            {"title": "寒武纪股权激励大消息", "content": "", "affected_stocks": ["寒武纪"]}, {})
        b = rtp._push_event_sig(
            {"title": "寒武纪:2026年限制性股票激励计划(草案)摘要公告", "content": "", "name": "寒武纪"}, {})
        assert rtp._is_same_event(a, b)

    def test_same_amount_same_event(self):
        """同事件不同措辞同金额（30.53亿实证）→ 同事件"""
        a = rtp._push_event_sig({"title": "行云科技签约30.53亿算力服务协议", "content": ""}, {})
        b = rtp._push_event_sig({"title": "行云科技：签约30.53亿元算力服务协议", "content": ""}, {})
        assert rtp._is_same_event(a, b)

    def test_lcs_fallback_without_entities(self):
        """无主体信息时靠事件组+标题最长公共子串≥5 合并"""
        a = rtp._push_event_sig({"title": "恩智浦洽谈收购Ambarella", "content": ""}, {})
        b = rtp._push_event_sig({"title": "安霸股价因传恩智浦洽谈收购而飙升", "content": ""}, {})
        assert rtp._is_same_event(a, b)

    def test_different_mna_events_not_merged(self):
        """不同公司的两起并购：主体无交集、公共子串短 → 不同事件（防误合并漏推）"""
        a = rtp._push_event_sig({"title": "甲公司洽谈收购乙科技", "content": ""},
                                {"entities": ["甲公司", "乙科技"]})
        b = rtp._push_event_sig({"title": "丙集团宣布收购丁能源", "content": ""},
                                {"entities": ["丙集团", "丁能源"]})
        assert not rtp._is_same_event(a, b)

    def test_plain_news_title_similarity(self):
        """普通流水新闻（无事件组）：标题高度相似 → 同一条"""
        a = rtp._push_event_sig({"title": "央行宣布降准0.5个百分点释放流动性", "content": ""}, {})
        b = rtp._push_event_sig({"title": "央行宣布降准0.5个百分点 释放长期流动性", "content": ""}, {})
        assert rtp._is_same_event(a, b)

    def test_plain_news_different_not_merged(self):
        a = rtp._push_event_sig({"title": "今日A股三大指数集体收涨", "content": ""}, {})
        b = rtp._push_event_sig({"title": "欧洲央行宣布维持利率不变", "content": ""}, {})
        assert not rtp._is_same_event(a, b)

    def test_lcs_len_basic(self):
        assert rtp._lcs_len("恩智浦洽谈收购", "因传恩智浦洽谈收购而飙升") == 7
        assert rtp._lcs_len("", "abc") == 0
        assert rtp._lcs_len("abc", "xyz") == 0

    def test_merge_event_sig_union(self):
        a = {"stocks": ["A"], "entities": ["X"], "events": ["回购"], "numbers": ["亿:1"], "title_norm": "短"}
        b = {"stocks": ["B"], "entities": ["Y"], "events": ["增持"], "numbers": ["亿:2"], "title_norm": "更长的标题"}
        m = rtp._merge_event_sig(a, b)
        assert m["stocks"] == ["A", "B"]
        assert m["entities"] == ["X", "Y"]
        assert m["events"] == sorted(["增持", "回购"])
        assert m["numbers"] == sorted(["亿:1", "亿:2"])
        assert m["title_norm"] == "更长的标题"


# ============================================================
# Gist 状态合并（读-改-写防并发覆盖）
# ============================================================

class TestMergeState:
    def test_pushed_true_wins(self):
        local = {"seen": {"a": {"t": "2026-08-01 10:00:00", "pushed": True}}, "pushed_events": []}
        remote = {"seen": {"a": {"t": "2026-08-01 09:00:00", "pushed": False}}, "pushed_events": []}
        merged = rtp._merge_state(local, remote)
        assert merged["seen"]["a"]["pushed"] is True

    def test_union_of_fingerprints(self):
        local = {"seen": {"b": {"t": "1", "pushed": False}}, "pushed_events": []}
        remote = {"seen": {"c": {"t": "1", "pushed": True}}, "pushed_events": []}
        merged = rtp._merge_state(local, remote)
        assert set(merged["seen"].keys()) == {"b", "c"}

    def test_pushed_events_dedup_by_key(self):
        e1 = {"entities": ["X"], "events": ["回购"], "numbers": [], "title_norm": "x", "t": "1"}
        e2 = {"entities": ["Y"], "events": ["增持"], "numbers": [], "title_norm": "y", "t": "2"}
        local = {"seen": {}, "pushed_events": [dict(e1)]}
        remote = {"seen": {}, "pushed_events": [dict(e1), dict(e2)]}
        merged = rtp._merge_state(local, remote)
        assert len(merged["pushed_events"]) == 2

    def test_empty_state_has_pushed_events(self):
        assert rtp._empty_state()["pushed_events"] == []


# ============================================================
# LLM 判定：idx 对齐 + JSON 抢救 + 批次失败降级
# ============================================================

class TestParseLlmArray:
    def test_normal_array(self):
        raw = '```json\n[{"idx": 0, "title": "A", "push": true, "score": 8, "direction": "bullish", "scope": "sector"}]\n```'
        entries = rtp._parse_llm_array(raw)
        assert len(entries) == 1
        assert entries[0]["push"] is True
        assert entries[0]["score"] == 8

    def test_rescue_unescaped_chinese_quotes(self):
        """美联储巴尔金"悬而未决"实证：中文引号未转义时正则抢救出判定字段"""
        broken = ('[{"title": "美联储巴尔金：利率是否已足够高是个"悬而未决"的问题", '
                  '"push": false, "score": 5, "direction": "neutral", "scope": "market", "reason": "r"}]')
        entries = rtp._parse_llm_array(broken)
        assert len(entries) == 1
        assert entries[0]["push"] is False
        assert entries[0]["score"] == 5
        assert entries[0]["scope"] == "market"

    def test_rescue_judge_object_direct(self):
        obj = '{"title": "测试"标题"", "idx": 3, "push": true, "score": 7, "direction": "bullish", "scope": "sector"}'
        rescued = rtp._rescue_judge_object(obj)
        assert rescued is not None
        assert rescued["push"] is True
        assert rescued["score"] == 7
        assert rescued["idx"] == 3

    def test_rescue_returns_none_when_no_key_fields(self):
        assert rtp._rescue_judge_object('{"foo": "bar"}') is None


class TestLlmJudge:
    def test_idx_alignment_despite_reorder_and_rewrite(self, monkeypatch):
        """LLM 乱序+改写标题时仍能按 idx 对齐（标题匹配时代会误判不推→漏推）"""
        items = [
            {"title": "央行宣布降准0.5个百分点", "content": "", "_pref_score": 0.8, "_hit_signal": True},
            {"title": "某公司日常经营公告", "content": "", "_pref_score": 0.3, "_hit_signal": False},
        ]

        def fake_llm(system_prompt, user_prompt, timeout=90, max_retries=1, deadline=0):
            return json.dumps([
                {"idx": 1, "title": "完全改写的标题", "push": False, "score": 2,
                 "direction": "neutral", "scope": "stock", "reason": "流水"},
                {"idx": 0, "title": "央行降准（改写版）", "push": True, "score": 9,
                 "direction": "bullish", "scope": "market", "entities": ["央行"], "reason": "货币宽松"},
            ], ensure_ascii=False)

        monkeypatch.setattr(rtp, "_call_llm_api", fake_llm)
        judges = rtp._llm_judge(items)
        assert judges[0]["push"] is True
        assert judges[0]["score"] == 9
        assert judges[0]["entities"] == ["央行"]
        assert judges[1]["push"] is False

    def test_batch_failure_fallback_per_item(self, monkeypatch):
        """批次异常 → 逐条降级规则判定：高信号+高分仍直推，不再整批不推"""
        def boom(*args, **kwargs):
            raise RuntimeError("LLM down")

        monkeypatch.setattr(rtp, "_call_llm_api", boom)
        items = [
            {"title": "央行宣布降准", "content": "", "_pref_score": 0.8, "_hit_signal": True},
            {"title": "普通流水新闻", "content": "", "_pref_score": 0.3, "_hit_signal": False},
        ]
        judges = rtp._llm_judge(items)
        assert judges[0]["push"] is True
        assert judges[0]["scope"] == "market"
        assert judges[1]["push"] is False

    def test_llm_returns_garbage_defaults_no_push(self, monkeypatch):
        monkeypatch.setattr(rtp, "_call_llm_api", lambda *a, **k: "无法解析的内容")
        items = [{"title": "测试", "content": "", "_pref_score": 0.1, "_hit_signal": False}]
        judges = rtp._llm_judge(items)
        assert len(judges) == 1
        assert judges[0]["push"] is False

    def test_missing_entry_falls_back_to_rules_not_silently_skipped(self, monkeypatch):
        """LLM 成功返回但漏回显某条（截断/遗漏）时，该条走规则降级而非静默不推。

        修复前：e = by_idx.get(i) or by_title.get(t) or {} → push=False，重大消息漏推。
        修复后：未回显条目按 _fallback_decision 判定（高信号+高预筛分仍可推）。
        """
        items = [
            {"title": "央行宣布降准0.5个百分点", "content": "释放长期流动性",
             "_pref_score": 0.80, "_hit_signal": True},
            {"title": "某公司签订重大合同", "content": "",
             "_pref_score": 0.72, "_hit_signal": True},
            {"title": "普通流水新闻", "content": "",
             "_pref_score": 0.30, "_hit_signal": False},
        ]

        def fake_llm(system_prompt, user_prompt, timeout=90, max_retries=1, deadline=0):
            # 只回显 idx=0（漏掉 idx=1, 2）
            return json.dumps([
                {"idx": 0, "title": "央行宣布降准", "push": False, "score": 2,
                 "direction": "neutral", "scope": "stock", "reason": "降准幅度小"},
            ], ensure_ascii=False)

        monkeypatch.setattr(rtp, "_call_llm_api", fake_llm)
        judges = rtp._llm_judge(items)
        assert len(judges) == 3
        # idx=0: LLM 明确判定不推 → 尊重 LLM
        assert judges[0]["push"] is False
        # idx=1: 未回显 + 高信号高分 → 规则降级应可推（不再静默漏推）
        assert judges[1]["push"] is True
        assert judges[1]["direction"] == "bullish"
        # idx=2: 未回显 + 低分无信号 → 规则降级不推
        assert judges[2]["push"] is False

    def test_garbage_output_high_signal_not_lost(self, monkeypatch):
        """LLM 返回完全无法解析的内容时，高信号+高预筛分条目经规则降级仍可推（不丢）"""
        monkeypatch.setattr(rtp, "_call_llm_api", lambda *a, **k: "{{{ 乱码 }}}")
        items = [
            {"title": "国常会部署稳经济政策", "content": "",
             "_pref_score": 0.85, "_hit_signal": True},
            {"title": "普通流水新闻", "content": "",
             "_pref_score": 0.20, "_hit_signal": False},
        ]
        judges = rtp._llm_judge(items)
        assert judges[0]["push"] is True
        assert judges[1]["push"] is False


class TestFallbackDecision:
    """LLM 降级判定：方向三态 + scope 按文本语义（不硬编码 market）"""

    def test_bearish_text_maps_bearish(self):
        j = rtp._fallback_decision(
            {"title": "某公司被立案调查", "content": ""}, 0.8, True)
        assert j["push"] is True
        assert j["direction"] == "bearish"

    def test_neutral_text_not_forced_bearish(self):
        """无多空信号的中性流水：修复前被判 bearish+market 直推，修复后 neutral+stock"""
        j = rtp._fallback_decision(
            {"title": "某公司召开年度总结会议", "content": "会议圆满结束"}, 0.8, True)
        assert j["push"] is True  # 高信号高分仍推（宁可多推）
        assert j["direction"] == "neutral"
        assert j["scope"] == "stock"

    def test_macro_text_maps_market(self):
        j = rtp._fallback_decision(
            {"title": "央行宣布降准0.5个百分点", "content": ""}, 0.8, True)
        assert j["scope"] == "market"
        assert j["direction"] == "bullish"

    def test_sector_text_maps_sector(self):
        j = rtp._fallback_decision(
            {"title": "半导体板块多家公司业绩预增", "content": ""}, 0.8, True)
        assert j["scope"] == "sector"
        assert j["direction"] == "bullish"

    def test_low_score_not_pushed(self):
        j = rtp._fallback_decision(
            {"title": "普通流水新闻", "content": ""}, 0.3, False)
        assert j["push"] is False
        assert j["direction"] == "neutral"
        assert j["scope"] == "stock"


# ============================================================
# strict 阈值放宽（板块/龙头 ≥6）
# ============================================================

class TestThresholdRelaxed:
    def test_market_always_passes(self):
        assert rtp._passes_threshold("strict", 1, "neutral", "market") is True

    def test_sector_score6_now_passes(self):
        assert rtp._passes_threshold("strict", 6, "neutral", "sector") is True
        assert rtp._passes_threshold("strict", 5, "neutral", "sector") is False

    def test_sector_strong_direction_passes(self):
        assert rtp._passes_threshold("strict", 4, "bullish", "sector") is True

    def test_leader_stock_score6_passes(self):
        assert rtp._passes_threshold("strict", 6, "neutral", "stock", leader_stock=True) is True

    def test_non_leader_stock_still_blocked(self):
        assert rtp._passes_threshold("strict", 9, "bullish", "stock", leader_stock=False) is False

    def test_score_tolerates_string(self):
        assert rtp._passes_threshold("strict", "6", "neutral", "sector") is True


# ============================================================
# 科技板块/科技龙头规则兜底（防 LLM 漏推科技资讯）
# ============================================================

class TestTechOverride:
    def _judge(self, scope="sector", score=5, direction="mildly_bullish",
               leader=False, sectors=None):
        return {"push": False, "score": score, "direction": direction,
                "scope": scope, "is_leader_stock": leader, "sectors": sectors or []}

    def test_sector_tech_override(self):
        """板块级科技资讯：LLM 判不推也放行（score≥5）"""
        n = {"title": "银河证券：Kimi K3正式开源 重塑大模型商业生态", "content": "建议关注国产超节点"}
        j = self._judge(sectors=["AI"])
        assert rtp._is_domestic_tech(n, j["sectors"]) is True

    def test_small_cap_earnings_not_override(self):
        """只影响小票自身的业绩预告：不触发科技兜底，仍被 LLM 否决"""
        n = {"title": "业绩预告: 富瀚微(300613) 预增 幅度1902.7%", "content": ""}
        j = self._judge(scope="stock")
        assert rtp._is_domestic_tech(n, j["sectors"]) is False


# ============================================================
# Gist 状态读写：网络错误重试（修复前 raise_for_status 无 try，重试形同虚设）
# ============================================================

class TestGistLoadRetry:
    def test_network_error_then_success(self, monkeypatch):
        import requests
        calls = {"n": 0}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"files": {rtp.GIST_STATE_FILENAME: {
                    "content": '{"seen": {"fp1": {"t": "2026-08-01 00:00:00", "pushed": true}}}'
                }}}

        def fake_get(url, timeout=20):
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.exceptions.ConnectionError("boom")
            return FakeResp()

        monkeypatch.setattr("requests.get", fake_get)
        state = rtp._gist_load("token", "gist_id")
        assert calls["n"] == 3  # 前2次失败重试，第3次成功
        assert state["seen"]["fp1"]["pushed"] is True

    def test_all_fail_returns_empty_state(self, monkeypatch):
        import requests

        def fake_get(url, timeout=20):
            raise requests.exceptions.Timeout("timeout")

        monkeypatch.setattr("requests.get", fake_get)
        state = rtp._gist_load("token", "gist_id")
        assert state == {}


# ============================================================
# 规则预筛：信号词检查剥离公司名（防 "*ST XX" 公司名触发 ST 直通）
# ============================================================

class TestPrefilterNameStrip:
    def test_st_company_name_not_trigger_signal(self):
        score, hit = rtp._prefilter({
            "title": "*ST天夏签订日常经营合同",
            "content": "合同金额较小",
            "name": "*ST天夏",
        })
        assert hit is False

    def test_same_text_without_name_still_hits_signal(self):
        score, hit = rtp._prefilter({
            "title": "*ST天夏签订日常经营合同",
            "content": "合同金额较小",
        })
        assert hit is True

    def test_normal_signal_still_hits(self):
        score, hit = rtp._prefilter({
            "title": "央行宣布降准0.5个百分点",
            "content": "",
            "name": "",
        })
        assert hit is True
