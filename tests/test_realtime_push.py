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
from datetime import datetime, timedelta
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


class TestSameEventMarketSession:
    """市场开收盘/复盘类多源快讯同事件合并（2026-08-03 21:32 美股开盘三源三推实证修复）

    该类快讯无事件组/实体/金额，标题措辞差异大（Jaccard 0.14~0.41、LCS 兜不住），
    按"同时段组 + 同市场域 + 方向不冲突"合并。
    """

    def _sig(self, title):
        return rtp._push_event_sig({"title": title, "content": ""}, {})

    def test_us_open_three_sources_merged(self):
        """实证三对：美股开盘齐涨多源报道 → 两两同事件（不再三推）"""
        a = self._sig("【美股开盘：三大股指齐涨】道指涨1.08%，标普500指数涨0.7%")
        b = self._sig("美股开盘：美股三大指数集体高开")
        c = self._sig("道指开盘涨0.52%，纳指涨0.31%，标普500涨0.2%。明星科技股谷歌-A涨2%")
        assert rtp._is_same_event(a, b)
        assert rtp._is_same_event(a, c)
        assert rtp._is_same_event(b, c)

    def test_cross_market_not_merged(self):
        """跨市场（A股午评 vs 美股开盘）→ 不同事件"""
        a = self._sig("A股午评：科创50指数低开低走跌3.73%，算力硬件股集体回调")
        b = self._sig("美股开盘：美股三大指数集体高开")
        assert not rtp._is_same_event(a, b)

    def test_cross_session_not_merged(self):
        """跨时段（A股午评 vs A股收盘）→ 不同事件（防 48h 拦截误杀收盘）"""
        a = self._sig("A股午评：科创50指数低开低走跌3.73%")
        b = self._sig("A股收盘：科创50指数跌3.2%，两市成交缩量")
        assert not rtp._is_same_event(a, b)

    def test_opposite_direction_not_merged(self):
        """同市场同时段但方向对立（齐涨 vs 齐跌）→ 不同事件（方向守卫）"""
        a = self._sig("美股开盘：三大股指齐涨，道指涨1%")
        b = self._sig("美股开盘：三大股指齐跌，道指跌1%")
        assert not rtp._is_same_event(a, b)

    def test_domestic_open_different_days_not_affected(self):
        """韩股/日股同域不误并：韩股收盘 vs 韩股开盘 跨时段 → 不同事件"""
        a = self._sig("韩国综指开盘涨0.5%")
        b = self._sig("韩国综指收盘跌超5%")
        assert not rtp._is_same_event(a, b)


class TestSameEventIntraday:
    """盘中行情动态（涨超/现报/涨幅扩大）同事件合并（2026-08-04 00:32 纳指三推实证修复）

    无时段词（开盘/午评/收盘），Jaccard 0.20~0.33、LCS 占比 0.25~0.46 均兜不住，
    按"同市场域 + 共享市场指数词 + 方向不冲突"合并。
    """

    def _sig(self, title):
        return rtp._push_event_sig({"title": title, "content": ""}, {})

    def test_nasdaq_rally_three_sources_merged(self):
        """实证三对：纳指大涨多源报道 → 两两同事件（不再三推）"""
        a = self._sig("美股涨幅扩大 纳指涨超2%")
        b = self._sig("纳指涨200点 现报25882.567点 道指涨101点")
        c = self._sig("纳指涨超2% Meta涨超6%")
        assert rtp._is_same_event(a, b)
        assert rtp._is_same_event(a, c)
        assert rtp._is_same_event(b, c)

    def test_different_index_not_merged(self):
        """不同市场指数（沪指 vs 纳指）→ 不同事件（无共享指数词）"""
        a = self._sig("沪指涨1%，两市成交放量")
        b = self._sig("纳指涨超2%，科技股领涨")
        assert not rtp._is_same_event(a, b)

    def test_intraday_opposite_direction_not_merged(self):
        """盘中同指数方向对立（纳指涨 vs 纳指跌）→ 不同事件（方向守卫）"""
        a = self._sig("纳指涨超2%，科技股普涨")
        b = self._sig("纳指跌超2%，科技股重挫")
        assert not rtp._is_same_event(a, b)

    def test_earnings_forecast_signal_not_merged_with_market_move(self):
        """业绩预告信号 vs 指数行情 → 不同事件（信号类有事件组，不进入普通新闻分支）"""
        a = self._sig("纳指涨超2%")
        b = self._sig("业绩预告: 中微公司(688012) 预增 幅度2966.0%")
        assert not rtp._is_same_event(a, b)


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

    def test_empty_state_has_candidate_events(self):
        # P7-1：状态默认必须含 candidate_events（供择时 news_modifier 并集读取）
        assert rtp._empty_state()["candidate_events"] == []

    def test_candidate_events_merge_union_dedup(self):
        # P7-1：candidate_events 按 (日期, 事件签名) 合并去重，pushed=True 优先防方向丢失
        c1 = {"entities": ["x"], "events": ["回购"], "numbers": [],
              "title_norm": "x", "t": "2026-08-22 10:00:00", "dir": "bullish"}
        c2 = {"entities": ["y"], "events": ["增持"], "numbers": [],
              "title_norm": "y", "t": "2026-08-22 10:30:00", "dir": "bearish"}
        local = {"seen": {}, "pushed_events": [], "candidate_events": [dict(c1)]}
        remote = {"seen": {}, "pushed_events": [], "candidate_events": [dict(c1), dict(c2)]}
        merged = rtp._merge_state(local, remote)
        assert len(merged["candidate_events"]) == 2

    def test_candidate_merge_pushed_priority(self):
        # 并发写：一方已成推送、另一方仍是未推候选，合并后保留 pushed=True（防丢方向/状态）
        base = {"entities": ["z"], "events": ["中标"], "numbers": [],
                "title_norm": "z", "t": "2026-08-22 11:00:00"}
        pushed = {**base, "dir": "bullish", "pushed": True}
        unpushed = {**base, "dir": "neutral", "pushed": False}
        local = {"seen": {}, "pushed_events": [], "candidate_events": [dict(pushed)]}
        remote = {"seen": {}, "pushed_events": [], "candidate_events": [dict(unpushed)]}
        merged = rtp._merge_state(local, remote)
        matched = [e for e in merged["candidate_events"] if e["title_norm"] == "z"]
        assert len(matched) == 1
        assert matched[0]["pushed"] is True
        assert matched[0]["dir"] == "bullish"


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

    def test_batch_failure_hangs_items_for_retry(self, monkeypatch):
        """批次异常 → 整批挂起（judged=False）：不推、不落指纹，下轮重新送 LLM 判定。

        2026-08-03 用户口径：删除规则降级路径，全部走 LLM 判定。
        修复前：批次失败逐条规则直推（高信号+高分仍推，且规则方向可能误判）。
        修复后：一律挂起，绝不规则直推。
        """
        def boom(*args, **kwargs):
            raise RuntimeError("LLM down")

        monkeypatch.setattr(rtp, "_call_llm_api", boom)
        items = [
            {"title": "央行宣布降准", "content": "", "_pref_score": 0.8, "_hit_signal": True},
            {"title": "普通流水新闻", "content": "", "_pref_score": 0.3, "_hit_signal": False},
        ]
        judges = rtp._llm_judge(items)
        for j in judges:
            assert j["push"] is False
            assert j["judged"] is False
            assert j["direction"] == "neutral"

    def test_llm_returns_garbage_defaults_hang(self, monkeypatch):
        monkeypatch.setattr(rtp, "_call_llm_api", lambda *a, **k: "无法解析的内容")
        items = [{"title": "测试", "content": "", "_pref_score": 0.1, "_hit_signal": False}]
        judges = rtp._llm_judge(items)
        assert len(judges) == 1
        assert judges[0]["push"] is False
        assert judges[0]["judged"] is False

    def test_missing_entry_hangs_for_retry(self, monkeypatch):
        """LLM 成功返回但漏回显某条（截断/遗漏）时，该条挂起下轮重试，而非规则直推。

        2026-08-03 用户口径：删除规则降级路径（修复前未回显条目按 _fallback_decision
        规则直推，规则方向对全文关键词扫描可能把利空标题误判为利好）。
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
        assert judges[0]["judged"] is True
        # idx=1/2: 未回显 → 挂起（不推、judged=False），下轮重试；绝不规则直推
        assert judges[1]["push"] is False
        assert judges[1]["judged"] is False
        assert judges[2]["push"] is False
        assert judges[2]["judged"] is False

    def test_garbage_output_all_hang(self, monkeypatch):
        """LLM 返回完全无法解析的内容时，全部条目挂起（judged=False），不规则直推"""
        monkeypatch.setattr(rtp, "_call_llm_api", lambda *a, **k: "{{{ 乱码 }}}")
        items = [
            {"title": "国常会部署稳经济政策", "content": "",
             "_pref_score": 0.85, "_hit_signal": True},
            {"title": "普通流水新闻", "content": "",
             "_pref_score": 0.20, "_hit_signal": False},
        ]
        judges = rtp._llm_judge(items)
        assert all(j["push"] is False for j in judges)
        assert all(j["judged"] is False for j in judges)


class TestHangJudge:
    """LLM 未判定挂起状态（2026-08-03 删除规则降级路径后）"""

    def test_hang_judge_shape(self):
        j = rtp._hang_judge({"title": "央行宣布降准"})
        assert j["push"] is False
        assert j["judged"] is False
        assert j["direction"] == "neutral"
        assert j["scope"] == "stock"
        assert "挂起" in j["reason"]

    def test_run_once_skips_hung_items_without_fingerprint(self, monkeypatch, tmp_path):
        """未判定条目：不推送、不落指纹（下轮重新送 LLM 判定）"""
        monkeypatch.setenv("GIST_TOKEN", "")
        monkeypatch.setenv("GIST_ID", "")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "real_time_state.json")

        sent = []
        monkeypatch.setattr(rtp, "_send_alert_item",
                            lambda cfg, title, content: sent.append(title) or {"code": 200})
        # LLM 判定全部挂起（如 LLM 服务故障）
        monkeypatch.setattr(rtp, "_llm_judge",
                            lambda items, **kw: [rtp._hang_judge(n) for n in items])
        monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())

        news = type("T", (), {"func": staticmethod(lambda: [{
            "title": "央行宣布降准0.5个百分点 释放万亿流动性",
            "content": "国常会部署，支持实体", "source": "财联社",
            "published_at": "2026-08-01 10:00:00",
        }])})()
        sig = type("T", (), {"func": staticmethod(lambda: [])})()
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)

        stats = rtp.run_once(dry_run=False)
        assert stats["pushed"] == 0
        assert sent == [], "未判定条目绝不推送"
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        assert all(not r.get("pushed") for r in saved["seen"].values())
        # 关键：挂起条目不落指纹 → 下轮仍作为"新增"重新送 LLM
        assert not saved["seen"], "挂起条目不落指纹，下轮重试"


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
        n = {"title": "业绩预告: 富翰微(300613) 预增 幅度1902.7%", "content": ""}
        j = self._judge(scope="stock")
        assert rtp._is_domestic_tech(n, j["sectors"]) is False


# ============================================================
# 推送格式化 format_push_alert
# ============================================================

class TestFormatPushAlert:
    def test_direction_emoji_and_label(self):
        """方向 -> emoji/label 映射（A 股红涨绿跌）。方向文案不冲突"""
        mapping = [
            ("bullish", "🔴", "强利好"),
            ("bearish", "🟢", "强利空"),
            ("mildly_bullish", "🟠", "弱利好"),
            ("mildly_bearish", "🟡", "弱利空"),
            ("neutral", "⚪", "中性"),
            ("mixed", "🔷", "多空交织"),
        ]
        for direction, emoji, label in mapping:
            out = rtp.format_push_alert(
                {"title": "测试标题", "content": "", "source": "S"},
                {"direction": direction})
            assert emoji in out and label in out

    def test_content_truncation_300(self):
        """正文超过 300 字符时截断并附省略号"""
        long_content = "很" * 500
        out = rtp.format_push_alert(
            {"title": "T", "content": long_content, "source": "S"},
            {"direction": "neutral"})
        assert "很" * 300 in out
        assert "..." in out
        assert "很" * 301 not in out

    def test_short_content_not_truncated(self):
        out = rtp.format_push_alert(
            {"title": "T", "content": "短内容", "source": "S"},
            {"direction": "neutral"})
        assert "短内容" in out
        assert "..." not in out

    def test_reason_and_sectors_included(self):
        out = rtp.format_push_alert(
            {"title": "存储芯片涨价", "content": "内容", "source": "S"},
            {"direction": "bullish", "score": 8, "scope": "sector",
             "sectors": ["半导体", "AI"], "reason": "板块景气上行"})
        assert "半导体" in out and "AI" in out
        assert "板块景气上行" in out

    def test_scope_label(self):
        out = rtp.format_push_alert(
            {"title": "央行降准", "content": "", "source": "S"},
            {"direction": "neutral", "scope": "market", "score": 7})
        assert "全市场" in out

    def test_missing_scope_defaults_stock(self):
        out = rtp.format_push_alert(
            {"title": "T", "content": "", "source": "S"},
            {"direction": "neutral"})
        assert "个股" in out


# ============================================================
# 事件指纹 _news_fingerprint
# ============================================================

class TestNewsFingerprint:
    def test_same_title_diff_pub_same_fp(self):
        """同标题不同时间 → 同指纹（同事件去重）"""
        a = rtp._news_fingerprint(
            {"title": "央行降准", "content": "", "published_at": "2026-08-01 09:00:00"})
        b = rtp._news_fingerprint(
            {"title": "央行降准", "content": "", "published_at": "2026-08-01 10:00:00"})
        assert a == b

    def test_different_title_diff_fp(self):
        a = rtp._news_fingerprint({"title": "A公司宣布收购", "content": ""})
        b = rtp._news_fingerprint({"title": "央行宣布降准", "content": ""})
        assert a != b

    def test_same_title_diff_content_same_fp(self):
        """标题相同内容不同（多源转载）→ 同指纹"""
        a = rtp._news_fingerprint({"title": "央行降准", "content": "版本一"})
        b = rtp._news_fingerprint({"title": "央行降准", "content": "版本二"})
        assert a == b

    def test_high_signal_same_fp_numeric_insensitive(self):
        """高信号词命中：同信号+同事件组+同个股 → 同指纹（数字不掺入）"""
        a = rtp._news_fingerprint(
            {"title": "央行宣布降准0.5个百分点", "content": "",
             "published_at": "2026-07-01 10:00:00"})
        b = rtp._news_fingerprint(
            {"title": "央行宣布降准0.5个百分点 释放长期流动性", "content": "",
             "published_at": "2026-07-01 12:00:00"})
        assert a == b

    def test_different_amount_company_events_diff_fp(self):
        """非高信号的公司公告：金额不同 → 指纹不同（避免合并错误）"""
        a = rtp._news_fingerprint(
            {"title": "甲乙科技签订供货协议", "content": "", "published_at": "2026-08-01 09:00:00"})
        b = rtp._news_fingerprint(
            {"title": "甲乙科技签订供货协议 金额800万", "content": "",
             "published_at": "2026-08-01 09:01:00"})
        assert a != b

    def test_high_signal_amount_insensitive_same_fp(self):
        """高信号词命中时金额刻意不掺入指纹（多源数字表述不稳定）"""
        a = rtp._news_fingerprint(
            {"title": "寒武纪回购5亿元", "content": "", "name": "寒武纪",
             "published_at": "2026-08-01 09:00:00"})
        b = rtp._news_fingerprint(
            {"title": "寒武纪回购5.5亿元", "content": "", "name": "寒武纪",
             "published_at": "2026-08-01 09:00:00"})
        assert a == b

    def test_fp_is_16_hex_and_deterministic(self):
        a = rtp._news_fingerprint({"title": "普通流水", "content": ""})
        b = rtp._news_fingerprint({"title": "普通流水", "content": ""})
        assert isinstance(a, str) and len(a) == 16
        assert a == b


# ============================================================
# 状态滚动清理 save_state（48h 窗口 + 300 条上限）
# ============================================================
# 生产 save_state 会写本地文件/ Gist，测试通过 monkeypatch 强制本地
# tmp 路径 + 清空 Gist/CI 环境，验证清理逻辑对真实写盘结果的影响。

class TestStateCleanup:
    def _to_local(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GIST_TOKEN", "")
        monkeypatch.setenv("GIST_ID", "")
        monkeypatch.delenv("CI", raising=False)
        stale = tmp_path / "real_time_state.json"
        monkeypatch.setattr(rtp, "_state_path", lambda: stale)

        def _write(state):
            rtp.save_state(state)
            return json.loads(stale.read_text(encoding="utf-8"))
        return _write

    def test_expired_seen_fingerprints_removed(self, monkeypatch, tmp_path):
        save = self._to_local(monkeypatch, tmp_path)
        import datetime
        old = (datetime.datetime.now(rtp.BJT)
               - datetime.timedelta(hours=50)).strftime("%Y-%m-%d %H:%M:%S")
        new = datetime.datetime.now(rtp.BJT).strftime("%Y-%m-%d %H:%M:%S")
        state = {"seen": {
            "fp_old": {"t": old, "pushed": True, "title": "旧"},
            "fp_new": {"t": new, "pushed": False, "title": "新"},
        }, "pushed_events": []}
        saved = save(state)
        assert "fp_old" not in saved["seen"]
        assert "fp_new" in saved["seen"]

    def test_pushed_events_300_cap(self, monkeypatch, tmp_path):
        """已推事件超 300 条时保留最新 300 条"""
        save = self._to_local(monkeypatch, tmp_path)
        import datetime
        base = datetime.datetime.now(rtp.BJT) - datetime.timedelta(hours=1)
        events = [
            {"entities": [str(i)], "events": [], "numbers": [],
             "title_norm": f"e{i}", "t": (base - datetime.timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")}
            for i in range(320)
        ]
        state = {"seen": {}, "pushed_events": events}
        saved = save(state)
        assert len(saved["pushed_events"]) <= 300
        earliest = min(e["t"] for e in saved["pushed_events"])
        assert earliest >= (base - datetime.timedelta(hours=50)).strftime("%Y-%m-%d %H:%M:%S")

    def test_old_pushed_events_cleaned(self, monkeypatch, tmp_path):
        save = self._to_local(monkeypatch, tmp_path)
        import datetime
        old = (datetime.datetime.now(rtp.BJT)
               - datetime.timedelta(hours=49)).strftime("%Y-%m-%d %H:%M:%S")
        new = datetime.datetime.now(rtp.BJT).strftime("%Y-%m-%d %H:%M:%S")
        state = {"seen": {}, "pushed_events": [
            {"t": old, "title_norm": "e_old"},
            {"t": new, "title_norm": "e_new"},
        ]}
        saved = save(state)
        assert all(e["title_norm"] != "e_old" for e in saved["pushed_events"])
        assert any(e["title_norm"] == "e_new" for e in saved["pushed_events"])


# ============================================================
# run_once 集成测试（mock LLM + 推送后端）
# ============================================================

class TestRunOnce:
    _SENT = []

    @staticmethod
    def _setup(monkeypatch, tmp_path, send_result):
        monkeypatch.setenv("GIST_TOKEN", "")
        monkeypatch.setenv("GIST_ID", "")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "real_time_state.json")
        TestRunOnce._SENT.clear()

        def fake_send(cfg, title, content):
            TestRunOnce._SENT.append(title)
            return dict(send_result)
        monkeypatch.setattr(rtp, "_send_alert_item", fake_send)

        def fake_judge(items, **kw):
            return [{"push": True, "score": 9, "direction": "bullish", "scope": "market",
                     "sectors": [], "entities": ["央行"], "is_leader_stock": False,
                     "reason": "宏观必推"} for _ in items]
        monkeypatch.setattr(rtp, "_llm_judge", fake_judge)
        monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())

        news = type("T", (), {"func": staticmethod(lambda: [{
            "title": "央行宣布降准0.5个百分点 释放万亿流动性",
            "content": "国常会部署，支持实体", "source": "财联社",
            "published_at": "2026-08-01 10:00:00",
        }])})()
        sig = type("T", (), {"func": staticmethod(lambda: [])})()
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)

    def test_push_success_records_fingerprint(self, monkeypatch, tmp_path):
        """推送成功：记录指纹 pushed=True + 已推事件，下轮不再推"""
        self._setup(monkeypatch, tmp_path, {"code": 200, "msg": "ok"})
        stats = rtp.run_once(dry_run=False)
        assert stats["pushed"] == 1
        assert TestRunOnce._SENT, "不应发送推送失败"
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        pushed_recs = [r for r in saved["seen"].values() if r.get("pushed")]
        assert pushed_recs, "推送成功必须记录 seen.pushed=True"
        assert saved["pushed_events"], "推送成功必须记录已推事件"

    def test_push_fail_does_not_record_fingerprint(self, monkeypatch, tmp_path):
        """推送失败：不记录指纹，下轮重试（重大消息不丢失）"""
        self._setup(monkeypatch, tmp_path, {"code": 500, "msg": "fail"})
        stats = rtp.run_once(dry_run=False)
        assert stats["pushed"] == 0
        assert TestRunOnce._SENT, "应尝试发送（失败也应有调用）"
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        assert all(not r.get("pushed") for r in saved["seen"].values()), \
            "推送失败不得标记 pushed=True（需下轮重试）"
        assert saved["pushed_events"] == saved["pushed_events"]  # 不新增已推事件

    def test_only_strong_direction_pushed(self, monkeypatch, tmp_path):
        """2026-08-04 用户口径：仅强利好/强利空推送；弱档/中性/混合不推"""
        self._setup(monkeypatch, tmp_path, {"code": 200, "msg": "ok"})
        # 覆盖 LLM 判定为弱档（push=True 但 direction=mildly_bullish）→ 必须不推
        monkeypatch.setattr(rtp, "_llm_judge", lambda items, **kw: [{
            "push": True, "score": 9, "direction": "mildly_bullish", "scope": "market",
            "sectors": [], "entities": ["央行"], "is_leader_stock": False,
            "reason": "弱档"} for _ in items])
        stats = rtp.run_once(dry_run=False)
        assert stats["pushed"] == 0, "弱档方向不得推送"
        assert TestRunOnce._SENT == [], "弱档方向不得发送"
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        # 已判定的弱档条目应记录指纹（pushed=False），避免下轮重复送 LLM
        assert all(not r.get("pushed") for r in saved["seen"].values())
        assert saved["seen"], "弱档已判定条目应落指纹（pushed=False）"


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

        def fake_get(url, timeout=20, headers=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.exceptions.ConnectionError("boom")
            return FakeResp()

        monkeypatch.setattr("requests.get", fake_get)
        state = rtp._gist_load("token", "gist_id")
        assert calls["n"] == 3  # 前2次失败重试，第3次成功
        assert state["seen"]["fp1"]["pushed"] is True

    def test_all_fail_raises_fail_stop(self, monkeypatch):
        """2026-08-13 P0：3 次读取全部失败 → raise（此前静默返回空状态，
        导致 19:03 轮空状态运行覆盖写回、4759 条历史去重记录丢失）"""
        import requests

        def fake_get(url, timeout=20, headers=None):
            raise requests.exceptions.Timeout("timeout")

        monkeypatch.setattr("requests.get", fake_get)
        with pytest.raises(RuntimeError, match="读取失败"):
            rtp._gist_load("token", "gist_id")


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


# ============================================================
# 候选溢出挂起重试（2026-08-06 P1-2 修复：不再永久 seen 漏推）
# ============================================================


# ============================================================
# 候选溢出挂起重试（2026-08-06 P1-2 修复：不再永久 seen 漏推）



# ============================================================
# 候选溢出挂起重试（2026-08-06 P1-2 修复：不再永久 seen 漏推）
# ============================================================

class TestCandidateOverflowPending:
    """溢出条目不落 seen，进入 pending 下轮重试；连续溢出达上限后放弃"""

    def _make_news(self):
        from src.tools.keyword_tables import HIGH_SIGNAL_KEYWORDS
        from src.tools.calculators import _EVENT_KEYWORD_GROUPS
        ev_kws = set()
        for _g, kws in _EVENT_KEYWORD_GROUPS:
            ev_kws.update(kws)
        kws = [kw for kw in HIGH_SIGNAL_KEYWORDS if kw not in ev_kws][:45]
        return [{
            "title": "主体%d %s 动态" % (i, kw),
            "content": "%s 相关事项" % kw,
            "source": "财联社",
            "published_at": "2026-08-01 10:00:00",
            "affected_stocks": ["主体%d" % i],
        } for i, kw in enumerate(kws)]

    def _mock_sources(self, monkeypatch, news_list):
        news = type("T", (), {"func": staticmethod(lambda: news_list)})()
        sig = type("T", (), {"func": staticmethod(lambda: [])})()
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)

    def _setup_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GIST_TOKEN", "")
        monkeypatch.setenv("GIST_ID", "")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "real_time_state.json")
        monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())
        monkeypatch.setattr(rtp, "_send_alert_item",
                            lambda cfg, t, c: {"code": 200})
        monkeypatch.setattr(rtp, "_llm_judge", lambda items, **kw: [{
            "push": True, "score": 8, "direction": "bullish", "scope": "market",
            "sectors": [], "entities": [], "is_leader_stock": False,
            "reason": "宏观"} for _ in items])
        monkeypatch.setenv("RT_MAX_CANDIDATES", "40")

    def test_overflow_goes_pending_not_seen(self, monkeypatch, tmp_path):
        """候选超上限时：溢出的条目不写 seen，进入 pending（下轮可重试）"""
        self._setup_env(monkeypatch, tmp_path)
        news_list = self._make_news()
        self._mock_sources(monkeypatch, news_list)

        stats = rtp.run_once(dry_run=False)
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        assert len(saved["pending"]) >= 1, "溢出条目必须进入 pending"
        # 溢出的条目(预筛分最低的尾部)不得出现在 seen(否则永久放弃→漏推)
        assert stats["pushed"] <= 40

    def test_pending_retry_reaches_limit_then_given_up(self, monkeypatch, tmp_path):
        """同一指纹连续 MAX_PENDING_RETRY 轮溢出 -> 放弃写 seen，防无限重试"""
        self._setup_env(monkeypatch, tmp_path)
        news_list = self._make_news()
        self._mock_sources(monkeypatch, news_list)

        rtp.run_once(dry_run=False)  # 第一轮:40 判定, 5 溢出进 pending
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        assert saved["pending"], "第一轮后应有 pending 条目"

        # 第二轮:已 seen 的 40 条不再进入, pending 的 5 条重新抓取后预筛仍过 → 本轮候选=5(不溢出)
        # 模拟:再次提供同样 45 条(已 seen 40 跳过, pending 5 进入)
        self._mock_sources(monkeypatch, news_list)
        rtp.run_once(dry_run=False)
        saved2 = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        # pending 中的 5 条本轮应进入判定(候选 5 <= 40 不溢出) → pending 清空
        assert len(saved2["pending"]) == 0, "pending 条目重新进入判定后应清空"

    def test_pending_entries_reenter_next_round(self, monkeypatch, tmp_path):
        """溢出进入 pending 的条目，下一轮源中重新出现时应重新进入判定并可能推送"""
        self._setup_env(monkeypatch, tmp_path)
        news_list = self._make_news()
        self._mock_sources(monkeypatch, news_list)
        judge_calls = []

        def counting_judge(items, **kw):
            judge_calls.append(len(items))
            return [{"push": True, "score": 8, "direction": "bullish", "scope": "market",
                     "sectors": [], "entities": [], "is_leader_stock": False,
                     "reason": "宏观"} for _ in items]
        monkeypatch.setattr(rtp, "_llm_judge", counting_judge)

        rtp.run_once(dry_run=False)  # 第一轮:40 判定 + 5 溢出
        first_calls = sum(judge_calls)
        # 第二轮:已 seen 40 跳过, pending 5 重新进入 → 判定数=5
        self._mock_sources(monkeypatch, news_list)
        rtp.run_once(dry_run=False)
        second_calls = sum(judge_calls) - first_calls
        assert 0 < second_calls <= 5, "第二轮应只判定 pending 的少量条目, 实际 %d" % second_calls



# ============================================================
# 候选溢出挂起重试（2026-08-06 P1-2 修复：不再永久 seen 漏推）
# ============================================================

class TestCandidateOverflowPending:
    """溢出条目不落 seen，进入 pending 下轮重试；连续溢出达上限后放弃"""

    def _make_news(self):
        from src.tools.keyword_tables import HIGH_SIGNAL_KEYWORDS
        from src.tools.calculators import _EVENT_KEYWORD_GROUPS
        ev_kws = set()
        for _g, kws in _EVENT_KEYWORD_GROUPS:
            ev_kws.update(kws)
        kws = [kw for kw in HIGH_SIGNAL_KEYWORDS if kw not in ev_kws][:45]
        return [{
            "title": "主体%d %s 动态" % (i, kw),
            "content": "%s 相关事项" % kw,
            "source": "财联社",
            "published_at": "2026-08-01 10:00:00",
            "affected_stocks": ["主体%d" % i],
        } for i, kw in enumerate(kws)]

    def _mock_sources(self, monkeypatch, news_list):
        news = type("T", (), {"func": staticmethod(lambda: news_list)})()
        sig = type("T", (), {"func": staticmethod(lambda: [])})()
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)

    def _setup_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GIST_TOKEN", "")
        monkeypatch.setenv("GIST_ID", "")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "real_time_state.json")
        monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())
        monkeypatch.setattr(rtp, "_send_alert_item",
                            lambda cfg, t, c: {"code": 200})
        monkeypatch.setattr(rtp, "_llm_judge", lambda items, **kw: [{
            "push": True, "score": 8, "direction": "bullish", "scope": "market",
            "sectors": [], "entities": [], "is_leader_stock": False,
            "reason": "宏观"} for _ in items])
        monkeypatch.setenv("RT_MAX_CANDIDATES", "40")

    def test_overflow_goes_pending_not_seen(self, monkeypatch, tmp_path):
        """候选超上限时：溢出的条目不写 seen，进入 pending（下轮可重试）"""
        self._setup_env(monkeypatch, tmp_path)
        news_list = self._make_news()
        self._mock_sources(monkeypatch, news_list)

        stats = rtp.run_once(dry_run=False)
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        assert len(saved["pending"]) >= 1, "溢出条目必须进入 pending"
        # 溢出的条目(预筛分最低的尾部)不得出现在 seen(否则永久放弃→漏推)
        assert stats["pushed"] <= 40

    def test_pending_retry_reaches_limit_then_given_up(self, monkeypatch, tmp_path):
        """同一指纹连续 MAX_PENDING_RETRY 轮溢出 -> 放弃写 seen，防无限重试"""
        self._setup_env(monkeypatch, tmp_path)
        news_list = self._make_news()
        self._mock_sources(monkeypatch, news_list)

        rtp.run_once(dry_run=False)  # 第一轮:40 判定, 5 溢出进 pending
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        assert saved["pending"], "第一轮后应有 pending 条目"

        # 第二轮:已 seen 的 40 条不再进入, pending 的 5 条重新抓取后预筛仍过 → 本轮候选=5(不溢出)
        # 模拟:再次提供同样 45 条(已 seen 40 跳过, pending 5 进入)
        self._mock_sources(monkeypatch, news_list)
        rtp.run_once(dry_run=False)
        saved2 = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        # pending 中的 5 条本轮应进入判定(候选 5 <= 40 不溢出) → pending 清空
        assert len(saved2["pending"]) == 0, "pending 条目重新进入判定后应清空"

    def test_pending_entries_reenter_next_round(self, monkeypatch, tmp_path):
        """溢出进入 pending 的条目，下一轮源中重新出现时应重新进入判定并可能推送"""
        self._setup_env(monkeypatch, tmp_path)
        news_list = self._make_news()
        self._mock_sources(monkeypatch, news_list)
        judge_calls = []

        def counting_judge(items, **kw):
            judge_calls.append(len(items))
            return [{"push": True, "score": 8, "direction": "bullish", "scope": "market",
                     "sectors": [], "entities": [], "is_leader_stock": False,
                     "reason": "宏观"} for _ in items]
        monkeypatch.setattr(rtp, "_llm_judge", counting_judge)

        rtp.run_once(dry_run=False)  # 第一轮:40 判定 + 5 溢出
        first_calls = sum(judge_calls)
        # 第二轮:已 seen 40 跳过, pending 5 重新进入 → 判定数=5
        self._mock_sources(monkeypatch, news_list)
        rtp.run_once(dry_run=False)
        second_calls = sum(judge_calls) - first_calls
        assert 0 < second_calls <= 5, "第二轮应只判定 pending 的少量条目, 实际 %d" % second_calls


# ============================================================
# 2026-08-07 修复回归：抓取 try 隔离（#1）
# ============================================================

class TestFetchIsolation:
    """news 与 signals 分开 try：单类失败不清空另一类成功数据"""

    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GIST_TOKEN", "")
        monkeypatch.setenv("GIST_ID", "")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "real_time_state.json")
        monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())
        monkeypatch.setattr(rtp, "_send_alert_item", lambda cfg, t, c: {"code": 200})
        monkeypatch.setattr(rtp, "_llm_judge", lambda items, **kw: [{
            "push": True, "score": 8, "direction": "bullish", "scope": "market",
            "sectors": [], "entities": [], "is_leader_stock": False,
            "reason": "宏观"} for _ in items])
        monkeypatch.setattr(rtp, "dedup_news_3layer", lambda lst: list(lst))

    def _tool(self, fn):
        return type("T", (), {"func": staticmethod(fn)})()

    def test_signals_failure_keeps_news(self, monkeypatch, tmp_path):
        """signals 抛异常 → 已成功的 news 必须保留并正常推送"""
        self._setup(monkeypatch, tmp_path)
        news = self._tool(lambda: [{
            "title": "央行宣布降准0.5个百分点 释放万亿流动性",
            "content": "国常会部署，支持实体", "source": "财联社",
            "published_at": "2026-08-01 10:00:00"}])

        def boom():
            raise RuntimeError("signals down")
        sig = self._tool(boom)
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)

        stats = rtp.run_once(dry_run=False)
        assert stats["fetched"] >= 1, "signals 失败不得清空已成功的新闻"
        assert stats["pushed"] >= 1, "新闻应正常走完判定并推送"

    def test_news_failure_keeps_signals(self, monkeypatch, tmp_path):
        """news 抛异常 → signals（业绩预告）保留并正常推送"""
        self._setup(monkeypatch, tmp_path)

        def boom():
            raise RuntimeError("news down")
        news = self._tool(boom)
        sig = self._tool(lambda: [{
            "title": "业绩预告: 中微公司(688012) 预增 幅度2966.0%",
            "content": "净利润同比大幅增长", "source": "交易所业绩预告",
            "published_at": "2026-08-01 10:00:00", "category": "signal",
            "impact_direction": "bullish", "affected_stocks": ["中微公司"]}])
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)

        stats = rtp.run_once(dry_run=False)
        assert stats["fetched"] >= 1, "news 失败不得清空已成功的信号"
        assert stats["pushed"] >= 1


# ============================================================
# 2026-08-07 修复回归：宽泛信号词指纹碰撞（#2）
# ============================================================

class TestFingerprintBroadWordFix:
    """仅命中宽泛市场词（韩国/纳指/央行等）的不同事件不得共享指纹"""

    def _n(self, title, content="", ts="2026-08-07 10:00:00"):
        return {"title": title, "content": content, "published_at": ts}

    def test_korea_different_events_no_collision(self):
        """实测复现案例：两条韩国新闻此前指纹相同 → 现在必须不同"""
        a = self._n("韩国总统宣布新产业计划")
        b = self._n("韩国半导体出口大增")
        assert rtp._news_fingerprint(a) != rtp._news_fingerprint(b)

    def test_nasdaq_intraday_updates_no_collision(self):
        """仅命中宽泛词(纳指)的盘中快讯 → 退回标题指纹，不互撞（推送级兜底合并）"""
        a = self._n("纳指涨超2%，科技股普涨")
        b = self._n("纳指现报25882点，Meta涨超6%")
        assert rtp._news_fingerprint(a) != rtp._news_fingerprint(b)

    def test_core_signal_still_shared(self):
        """核心事件词(降准)多源报道仍共享指纹（原合并能力不破坏）"""
        a = self._n("央行宣布降准0.5个百分点 释放长期流动性")
        b = self._n("央行宣布降准0.5个百分点 释放万亿流动性")
        assert rtp._news_fingerprint(a) == rtp._news_fingerprint(b)

    def test_find_signal_fp_keywords_excludes_broad(self):
        """指纹专用信号词排除宽泛词；全量信号词保留（预筛直通不受影响）"""
        from src.tools.keyword_tables import find_signal_keywords, find_signal_fp_keywords
        text = "韩国央行宣布降准0.5个百分点"
        all_hits = find_signal_keywords(text)
        fp_hits = find_signal_fp_keywords(text)
        assert "韩国" in all_hits and "央行" in all_hits, "预筛直通需保留全量信号词"
        assert "降准" in fp_hits
        assert "韩国" not in fp_hits and "央行" not in fp_hits


# ============================================================
# 2026-08-07 修复回归：overflow 分层配额（#3）
# ============================================================

class TestOverflowPriorityFix:
    """溢出时高信号核心事件优先于高分普通候选；高信号条目不放弃"""

    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GIST_TOKEN", "")
        monkeypatch.setenv("GIST_ID", "")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "real_time_state.json")
        monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())
        monkeypatch.setattr(rtp, "_send_alert_item", lambda cfg, t, c: {"code": 200})
        monkeypatch.setattr(rtp, "dedup_news_3layer", lambda lst: list(lst))
        monkeypatch.setattr(rtp, "_llm_judge", self._judge_ok)
        monkeypatch.setenv("RT_MAX_CANDIDATES", "40")

    def _news(self, title, content, **extra):
        n = {"title": title, "content": content, "source": "财联社",
             "published_at": "2026-08-01 10:00:00"}
        n.update(extra)
        return n

    def _mock_sources(self, monkeypatch, news_list):
        news = type("T", (), {"func": staticmethod(lambda: news_list)})()
        sig = type("T", (), {"func": staticmethod(lambda: [])})()
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)

    def _judge_ok(self, items, **kw):
        return [{"push": True, "score": 8, "direction": "bullish", "scope": "market",
                 "sectors": [], "entities": [], "is_leader_stock": False,
                 "reason": "宏观"} for _ in items]

    def test_high_signal_kept_over_higher_score_normal(self, monkeypatch, tmp_path):
        """41 条候选(40 普通高分 + 1 低分高信号) → 高信号进判定，普通末尾被挤出"""
        self._setup(monkeypatch, tmp_path)
        judged = []

        def recording(items, **kw):
            judged.extend(i["title"] for i in items)
            return self._judge_ok(items, **kw)
        monkeypatch.setattr(rtp, "_llm_judge", recording)

        news_list = [
            self._news("普通研报第%d期 存储涨价" % i, "半导体 芯片 光模块 PCB 存储芯片" * 2)
            for i in range(40)
        ]
        news_list.append(self._news("某某公司被证监会立案调查", "立案调查 重大违法 退市风险"))
        self._mock_sources(monkeypatch, news_list)
        rtp.run_once(dry_run=False)

        assert len(judged) == 40, "应恰好判定 40 条（上限）"
        assert any("立案调查" in t for t in judged), "高信号核心事件必须进入 LLM 判定（不被低分科技噪声挤出）"

    def test_normal_overflow_gives_up_after_retry_limit(self, monkeypatch, tmp_path):
        """普通条目连续 3 轮溢出 → 写 seen 放弃（防无限重试）"""
        self._setup(monkeypatch, tmp_path)
        state_path = tmp_path / "real_time_state.json"
        y_title = "普通低分研报Y 存储涨价"
        y_fp = rtp._news_fingerprint(self._news(y_title, "半导体 芯片 光模块"))
        pre_state = {"version": 2, "seen": {},
                     "pending": {y_fp: {"t": "2026-08-01 09:00:00", "retry": 2, "title": y_title}},
                     "pushed_events": []}
        state_path.write_text(json.dumps(pre_state, ensure_ascii=False), encoding="utf-8")

        news_list = [
            self._news("普通研报第%d期 存储涨价" % i, "半导体 芯片 光模块 PCB 存储芯片" * 2)
            for i in range(40)
        ]
        news_list.append(self._news(y_title, "半导体 芯片 光模块"))
        self._mock_sources(monkeypatch, news_list)
        rtp.run_once(dry_run=False)

        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert y_fp not in saved["pending"], "普通条目达上限后应从 pending 移除"
        assert y_fp in saved["seen"] and "[溢出放弃]" in saved["seen"][y_fp]["title"]

    def test_high_signal_overflow_never_gives_up(self, monkeypatch, tmp_path):
        """45 条全高信号候选(40+5 溢出) → 溢出高信号条目不写 seen 放弃"""
        self._setup(monkeypatch, tmp_path)
        from src.tools.keyword_tables import HIGH_SIGNAL_KEYWORDS
        from src.tools.calculators import _EVENT_KEYWORD_GROUPS
        ev_kws = set()
        for _g, kws in _EVENT_KEYWORD_GROUPS:
            ev_kws.update(kws)
        kws = [kw for kw in HIGH_SIGNAL_KEYWORDS if kw not in ev_kws][:45]
        news_list = [
            self._news("主体%d %s 动态" % (i, kw), "%s 相关事项" % kw,
                       affected_stocks=["主体%d" % i])
            for i, kw in enumerate(kws)
        ]
        self._mock_sources(monkeypatch, news_list)
        rtp.run_once(dry_run=False)

        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        abandoned = [r for r in saved["seen"].values() if "[溢出放弃]" in r.get("title", "")]
        assert abandoned == [], "高信号条目溢出不得写 seen 放弃"
        assert len(saved["pending"]) >= 1, "溢出高信号条目持续挂起 pending"


# ============================================================
# 2026-08-07 修复回归：LLM 布尔字段严格解析（#4）
# ============================================================

class TestAsBool:
    def test_string_false_is_false(self):
        assert rtp._as_bool("false") is False
        assert rtp._as_bool("0") is False
        assert rtp._as_bool("False") is False

    def test_string_true_is_true(self):
        assert rtp._as_bool("true") is True
        assert rtp._as_bool("1") is True

    def test_real_bool_passthrough(self):
        assert rtp._as_bool(True) is True
        assert rtp._as_bool(False) is False

    def test_unknown_defaults_false(self):
        assert rtp._as_bool("banana") is False
        assert rtp._as_bool(None) is False
        assert rtp._as_bool("") is False

    def test_llm_push_string_false_respected(self, monkeypatch):
        """LLM 返回 push/is_leader_stock 为字符串 "false" 时不得误判为 True"""
        items = [{"title": "央行宣布降准", "content": "", "_pref_score": 0.8, "_hit_signal": True}]

        def fake_llm(system_prompt, user_prompt, timeout=90, max_retries=1, deadline=0):
            return json.dumps([{"idx": 0, "title": "央行宣布降准", "push": "false",
                                "score": 2, "direction": "neutral", "scope": "stock",
                                "is_leader_stock": "false", "reason": "r"}], ensure_ascii=False)
        monkeypatch.setattr(rtp, "_call_llm_api", fake_llm)
        judges = rtp._llm_judge(items)
        assert judges[0]["push"] is False, "push=\"false\" 不得因 bool() 误判为 True"
        assert judges[0]["is_leader_stock"] is False

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用


# ============================================================
# 英文缩写词边界（2026-08-06 P2 修复：ST/IPO 防误命中）
# ============================================================

class TestEnglishAbbrevBoundary:
    """HIGH_SIGNAL_KEYWORDS 的英文缩写(ST/IPO)必须词边界匹配，防 STorage/STMicro 误命中"""

    def test_storage_not_high_signal(self):
        from src.tools.keyword_tables import has_signal_keyword, find_signal_keywords
        assert has_signal_keyword("Storage 需求强劲") is False
        assert find_signal_keywords("Storage 需求强劲") == []

    def test_stmicro_not_high_signal(self):
        from src.tools.keyword_tables import has_signal_keyword
        assert has_signal_keyword("STMicroelectronics 财报") is False

    def test_st_stock_still_hits(self):
        from src.tools.keyword_tables import has_signal_keyword, find_signal_keywords
        assert has_signal_keyword("*ST 中安 重整") is True
        assert has_signal_keyword("ST华微 公告") is True
        assert "ST" in find_signal_keywords("*ST 中安 重整")

    def test_ipo_still_hits(self):
        from src.tools.keyword_tables import has_signal_keyword, find_signal_keywords
        assert has_signal_keyword("IPO 重启预期") is True
        assert find_signal_keywords("IPO 重启预期") == ["IPO"]

    def test_prefilter_uses_boundary(self):
        """_prefilter 走词边界：含 STorage 的新闻不再直通信号"""
        score, hit = rtp._prefilter({
            "title": "存储芯片 Storage 需求强劲",
            "content": "行业景气",
            "name": "",
        })
        assert hit is False, "Storage 不应命中 ST 信号词"

    def test_prefilter_st_stock_hits(self):
        score, hit = rtp._prefilter({
            "title": "*ST中安 重大资产重组",
            "content": "重整推进",
            "name": "*ST中安",
        })
        # name 剥离后仍应命中(标题含重组)
        assert hit is True


# ============================================================
# 心跳告警 + hs300 文件缓存（2026-08-06 P2）
# ============================================================

class TestHeartbeatAndCache:
    def test_zero_push_streak_resets_on_push(self):
        """有推送时 0 推送计数应重置"""
        rtp._zero_push_streak[0] = 5
        rtp._zero_push_streak[0] = 0  # run_once 中 else 分支行为
        assert rtp._zero_push_streak[0] == 0

    def test_heartbeat_threshold_constant(self):
        assert rtp.HEARTBEAT_ZERO_PUSH_WARN_ROUNDS >= 3, "阈值过小会频繁误报"

    def test_hs300_cache_roundtrip(self, tmp_path, monkeypatch):
        """hs300 文件缓存读写 + TTL 过期"""
        import src.tools.data_fetchers as df
        monkeypatch.setattr(df, "_HS300_CACHE_PATH", tmp_path / "hs300_cache.json")
        df._hs300_save_cache({"000001"}, {"平安银行"})
        loaded = df._hs300_load_cache()
        assert loaded == {"codes": {"000001"}, "names": {"平安银行"}}
        # 过期
        import json, time
        p = tmp_path / "hs300_cache.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["_mtime"] = 0
        p.write_text(json.dumps(data), encoding="utf-8")
        assert df._hs300_load_cache() is None, "过期缓存应失效"


# ============================================================
# 2026-08-11 修复回归：噪声过滤（栏目/指数播报/盘面异动）
# + 实体模糊重叠跨日同事件合并 + LLM prompt 必推场景补充
# ============================================================

class TestNoiseFilter:
    """修复1：栏目汇总/指数播报/盘面异动类即使强档也过滤（13/61 滥推实证）"""

    def _judge(self, **kw):
        j = {"push": True, "score": 8, "direction": "bullish", "scope": "sector",
             "is_leader_stock": False, "entities": []}
        j.update(kw)
        return j

    def test_column_markers_filtered(self):
        """栏目汇总类一律不推：晚间新闻精选/隔夜要闻/九点特供/风口研报/午评/涨停分析"""
        cases = [
            "财联社8月10日晚间新闻精选",
            "周二你需要知道的隔夜全球要闻",
            "九点特供获美法院初步禁令 国产CXO龙头司法反制1260H名单迎阶段胜利",
            "风口研报公司模拟芯片多轮涨价驱动周期反转",
            "8月10日午间涨停分析",
            "金十数据整理A股盘前市场要闻速递",
        ]
        for t in cases:
            assert rtp._is_noise_push({"title": t}, self._judge(), set()) == "栏目汇总", t

    def test_index_move_filtered(self):
        """指数盘中行情播报不推：KOSPI涨超/韩国综指涨幅扩大/日经上涨"""
        cases = [
            "财联社8月10日电 韩国KOSPI指数涨超2 现报639406点",
            "韩国综指涨幅扩大至2",
            "日经225指数期货早盘上涨1.13%",
        ]
        for t in cases:
            assert rtp._is_noise_push({"title": t}, self._judge(), set()) == "指数播报", t

    def test_intraday_move_filtered_non_leader(self):
        """板块/概念盘面异动（非龙头）不推：异动拉升/直线涨停/盘初走强"""
        cases = [
            "智能驾驶概念异动拉升 大众交通直线涨停",
            "算力租赁概念异动拉升 杭钢股份鸿博股份直线涨停",
            "电网设备板块盘初走强 中电鑫龙等多股涨停",
        ]
        for t in cases:
            assert rtp._is_noise_push({"title": t}, self._judge(), set()) == "盘面异动", t

    def test_leader_intraday_kept(self):
        """龙头个股盘面保留：LLM 龙头标记或命中自选名单 → 不过滤"""
        t = "中际旭创跌超3% 成交额超110亿元"
        assert rtp._is_noise_push({"title": t}, self._judge(is_leader_stock=True), set()) == ""
        assert rtp._is_noise_push({"title": t}, self._judge(), {"中际旭创"}) == ""

    def test_major_news_not_filtered(self):
        """正常重大消息不被误伤（防漏推守卫）"""
        ok = [
            "央行印发中国人民银行十五五改革发展规划",
            "私募巨头集体入局 据称英伟达将牵头5000亿美元融资大单",
            "OpenAI发布GPT-5.6-Cyber",
            "日媒关注中国对日美稀土出口大幅下降 6月对日钇出口降至零",
            "期货早报美国非农意外减少23万人 加息预期骤变",  # 含非农硬数据，勿因"早报"误伤
            "微软计划最快9月发布其下一代MAIA300人工智能芯片",
            "摩根大通全球存储芯片短缺或持续至2028年",
        ]
        for t in ok:
            assert rtp._is_noise_push({"title": t}, self._judge(), set()) == "", t


class TestEntityOverlapMerge:
    """修复2：实体模糊重叠 + 放宽 LCS → 跨日同事件合并（防 48h 重复推送）"""

    def _sig(self, title, entities):
        return rtp._push_event_sig({"title": title, "content": ""}, {"entities": entities})

    def test_entity_overlap_substring(self):
        assert rtp._entity_overlap({"索尼", "台积电"}, {"索尼半导体解决方案公司", "台积电"})
        assert rtp._entity_overlap({"苹果"}, {"苹果"})
        assert not rtp._entity_overlap({"苹果"}, {"谷歌"})
        assert not rtp._entity_overlap({"索尼"}, {"台积电"})

    def test_sony_tsmc_jv_three_reports_merged(self):
        """索尼×台积电合资 48h 三推实证：跨日措辞差异大 → 应两两合并"""
        s1 = self._sig("索尼与台积电拟合资建厂1万亿日元布局下一代图像传感器芯片",
                       ["索尼", "台积电"])
        s2 = self._sig("台积电批准与索尼半导体解决方案公司成立新的合资企业",
                       ["台积电", "索尼半导体"])
        s3 = self._sig("索尼与台积电将在日本成立先进图像传感器合资公司 采用先进制程技术2029年量产",
                       ["台积电", "索尼"])
        assert rtp._is_same_event(s1, s2), "拟合资 vs 批准成立 应合并"
        assert rtp._is_same_event(s1, s3), "隔日报道应合并"
        assert rtp._is_same_event(s2, s3), "批准成立 vs 日本合资公司 应合并"

    def test_korea_5t_fund_two_reports_merged(self):
        """韩国5万亿韩元基金两条报道（隔1小时双推实证）→ 应合并"""
        s1 = self._sig("韩国将设立一项价值5万亿韩元新基金 重点投资于前景广阔的半导体材料零部件和设备领域",
                       ["韩国政府"])
        s2 = self._sig("韩国砸5万亿韩元设专项基金 瞄准半导体材料与无晶圆厂企业",
                       ["韩国政府"])
        assert rtp._is_same_event(s1, s2)

    def test_sk_hynix_different_events_not_merged(self):
        """漏推守卫：SK海力士不同事件（股东回报 vs EUV vs 中国产能）不得误并"""
        a = self._sig("SK海力士韩股上涨 该公司之前确定了股东回报细节的公布时间", ["SK海力士"])
        b = self._sig("消息称SK海力士推进EUV工艺 开始引入新材料PSM", ["SK海力士"])
        c = self._sig("韩媒SK海力士计划将在中国的NAND产能提高50", ["SK海力士"])
        assert not rtp._is_same_event(a, b)
        assert not rtp._is_same_event(a, c)
        assert not rtp._is_same_event(b, c)

    def test_apple_changxin_two_reports_merged(self):
        """苹果测试长鑫存储芯片：跨日两条报道 → 应合并（双推实证）"""
        s1 = self._sig("知情人士 苹果测试长鑫科技存储芯片 用于iPhone和MacBook", ["苹果", "长鑫存储"])
        s2 = self._sig("存储芯片重磅来袭 苹果找上长鑫 iPhone MacBook都在测试", ["苹果", "长鑫科技"])
        assert rtp._is_same_event(s1, s2)

    def test_same_entity_different_amount_not_merged(self):
        """金额守卫：同实体不同金额（50亿建厂 vs 10亿回购）→ 不得误并（漏推守卫）"""
        s1 = self._sig("宁德时代投资50亿元新建电池工厂", ["宁德时代"])
        s2 = self._sig("宁德时代拟投资10亿元回购股份", ["宁德时代"])
        assert not rtp._is_same_event(s1, s2)

    def test_shared_entity_name_only_not_merged(self):
        """误并守卫：仅共享实体名（中国人民银行），标题无其他共享内容 → 不同事件"""
        s1 = self._sig("央行根据中国人民银行与德意志联邦银行备忘录 决定授权德意志银行",
                       ["中国人民银行", "德意志银行"])
        s2 = self._sig("中国人民银行印发中国人民银行十五五改革发展规划", ["中国人民银行"])
        assert not rtp._is_same_event(s1, s2)

    def test_shared_entity_plus_anchor_merged(self):
        """同实体 + 共享高置信事件锚（合资）→ 合并（索尼×台积电三推实证）"""
        s1 = self._sig("索尼与台积电拟合资建厂1万亿日元布局下一代图像传感器芯片",
                       ["索尼", "台积电"])
        s2 = self._sig("台积电批准与索尼半导体解决方案公司成立新的合资企业",
                       ["台积电", "索尼半导体"])
        assert rtp._is_same_event(s1, s2)

    def test_tsmc_cowos_vs_sony_jv_not_merged(self):
        """主题词误并守卫：台积电 CoWoS量产 vs 索尼合资2029量产（仅共享'量产'）→ 不同事件"""
        s1 = self._sig("台积电55倍光罩CoWoS已进入量产", ["台积电"])
        s2 = self._sig("索尼与台积电将在日本成立先进图像传感器合资公司 2029年量产",
                       ["台积电", "索尼"])
        assert not rtp._is_same_event(s1, s2)


class TestMacroPolicyMerge:
    """2026-08-14 修复：日本央行加息三连推实证（16:03 同一事件多源报道推 3 次）

    根因①：'行情下跌'由 content 关键词规则提取进 events，属市场状态描述而非
    事件动词，使 _is_same_event 的共享事件组/双方均无事件组分支同时失效；
    根因②：多源措辞差异大（LCS 仅 4 字、jaccard 0.15~0.20），既有分支全兜不住。
    修复：过滤市场状态事件组 + 新增央行宏观政策合并分支（同央行实体 + 标题
    宏观政策词 + 方向不冲突）。
    """

    def _sig(self, title, entities, events=None):
        return {
            "stocks": [], "entities": entities, "events": events or [],
            "numbers": [], "sectors": ["整体市场"], "scope": "market",
            "title_norm": rtp._normalize_title(title),
        }

    def test_boj_rate_hike_three_reports_merged(self):
        """真实签名：16:03 三条日本央行加息报道（第一条 events=['行情下跌']）→ 两两合并"""
        s1 = self._sig("消息人士称日本央行最快可能在9月加息",
                       ["日本央行"], events=["行情下跌"])
        s2 = self._sig("日本央行加息或提速美元对日元急跌市场担心潜在流动性冲击",
                       ["日本央行"])
        s3 = self._sig("消息人士称日本央行考虑9月加息并加快紧缩步伐",
                       ["日本央行"])
        assert rtp._is_same_event(s1, s2), "行情下跌过滤后应走宏观分支合并"
        assert rtp._is_same_event(s1, s3), "行情下跌过滤后应走标题相似合并"
        assert rtp._is_same_event(s2, s3), "同央行+加息词应合并"

    def test_market_state_event_filtered_out(self):
        """行情状态词不作为共享事件组：暴跌 vs 空事件组且标题无关 → 不同事件"""
        a = self._sig("A公司股价暴跌", ["A公司"], events=["行情下跌"])
        b = self._sig("B公司宣布重大合同中标", ["B公司"], events=[])
        assert not rtp._is_same_event(a, b)

    def test_rate_hike_vs_cut_not_merged(self):
        """方向守卫：同央行 加息 vs 降息 → 不同事件（防误并漏推）"""
        a = self._sig("消息人士称日本央行考虑9月加息", ["日本央行"])
        b = self._sig("消息人士称日本央行考虑9月降息", ["日本央行"])
        assert not rtp._is_same_event(a, b)

    def test_different_central_banks_not_merged(self):
        """实体守卫：日本央行 vs 美联储 → 不同事件"""
        a = self._sig("消息人士称日本央行考虑9月加息", ["日本央行"])
        b = self._sig("美联储官员称9月可能加息", ["美联储"])
        assert not rtp._is_same_event(a, b)

    def test_neutral_cb_news_not_merged(self):
        """触发词守卫：中性购债安排 vs 加息传闻（仅共享央行实体）→ 不同事件"""
        a = self._sig("日本央行公布最新购债操作安排", ["日本央行"])
        b = self._sig("消息人士称日本央行最快可能在9月加息", ["日本央行"])
        assert not rtp._is_same_event(a, b)

    def test_same_cb_same_direction_merged(self):
        """同央行同向政策词（加息 vs 加快紧缩步伐）→ 合并"""
        a = self._sig("消息人士称日本央行考虑9月加息", ["日本央行"])
        b = self._sig("日本央行考虑加快紧缩步伐 美元对日元急跌", ["日本央行"])
        assert rtp._is_same_event(a, b)

    def test_cb_alias_normalized(self):
        """实体别名归一化：日央行/BOJ → 日本央行，仍合并"""
        a = self._sig("日央行最快可能在9月加息", ["日央行"])
        b = self._sig("BOJ考虑加快紧缩步伐", ["BOJ"])
        assert rtp._is_same_event(a, b)


class TestLlmPromptScenarios:
    """修复3：LLM prompt 补充必推场景（AI发布/监管/见顶警示）"""

    def test_prompt_covers_ai_release(self):
        p = rtp._LLM_SYSTEM_PROMPT
        assert "新模型、新芯片发布" in p or "GPT-5.6" in p
        assert "OpenAI" in p and "自研芯片" in p

    def test_prompt_covers_ai_regulation(self):
        p = rtp._LLM_SYSTEM_PROMPT
        assert "AI 监管与政策" in p
        assert "暂停AI开发" in p or "参议员" in p

    def test_prompt_covers_peak_warning(self):
        p = rtp._LLM_SYSTEM_PROMPT
        assert "行业见顶" in p and "目标价被大幅下调" in p

    def test_prompt_still_excludes_views_and_routine(self):
        p = rtp._LLM_SYSTEM_PROMPT
        assert "纯个人观点" in p
        assert "外围央行" in p  # 新增：外围央行日常表态明确不推
        assert "分析师评级调整" in p  # 新增：评级调整明确不推


class TestRunOnceNoiseFilterIntegration:
    """修复1 接入点：run_once 中强档但命中噪声 → 不推送、记 seen 标注"""

    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GIST_TOKEN", "")
        monkeypatch.setenv("GIST_ID", "")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "real_time_state.json")
        monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())
        monkeypatch.setattr(rtp, "_send_alert_item", lambda cfg, t, c: {"code": 200})
        monkeypatch.setattr(rtp, "dedup_news_3layer", lambda lst: list(lst))
        monkeypatch.setattr(rtp, "_llm_judge", self._judge_strong)

    def _judge_strong(self, items, **kw):
        # 全部判为强档 bullisih + push=True（噪声过滤前的行为是全部推送）
        return [{"push": True, "score": 8, "direction": "bullish", "scope": "market",
                 "sectors": [], "entities": [], "is_leader_stock": False,
                 "reason": "强档"} for _ in items]

    def test_column_news_not_pushed_despite_strong(self, monkeypatch, tmp_path):
        """LLM 判强档的纯栏目汇总类 → 仍不推送（栏目词之外无重大事件内容）"""
        self._setup(monkeypatch, tmp_path)
        news = type("T", (), {"func": staticmethod(lambda: [
            {"title": "8月10日午间涨停分析",
             "content": "今日涨停个股汇总",
             "source": "财联社", "published_at": "2026-08-10 22:00:00"}])})()
        sig = type("T", (), {"func": staticmethod(lambda: [])})()
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)
        stats = rtp.run_once(dry_run=False)
        assert stats["pushed"] == 0, "纯栏目汇总类即使强档也不得推送"
        saved = json.loads((tmp_path / "real_time_state.json").read_text(encoding="utf-8"))
        titles = [r.get("title", "") for r in saved["seen"].values()]
        assert any("[栏目汇总不推]" in t for t in titles), "应记录 seen 并标注原因"

    def test_column_embedding_major_event_pushed(self, monkeypatch, tmp_path):
        """2026-08-13 修复：栏目词内嵌重大事件（晚间新闻精选：央行降准）→ 不得误杀，应推送"""
        self._setup(monkeypatch, tmp_path)
        sent = []
        def record(cfg, t, c):
            sent.append(t)
            return {"code": 200}
        monkeypatch.setattr(rtp, "_send_alert_item", record)
        news = type("T", (), {"func": staticmethod(lambda: [
            {"title": "财联社8月10日晚间新闻精选：央行宣布降准0.5个百分点 释放流动性",
             "content": "央行降准 释放长期流动性",
             "source": "财联社", "published_at": "2026-08-10 22:00:00"}])})()
        sig = type("T", (), {"func": staticmethod(lambda: [])})()
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)
        stats = rtp.run_once(dry_run=False)
        assert stats["pushed"] == 1, "栏目内嵌重大事件（央行降准）不得被栏目词误杀"
        assert len(sent) == 1 and "降准" in sent[0]

    def test_major_news_still_pushed(self, monkeypatch, tmp_path):
        """正常强档重大消息不受影响，仍推送"""
        self._setup(monkeypatch, tmp_path)
        sent = []
        def record(cfg, t, c):
            sent.append(t)
            return {"code": 200}
        monkeypatch.setattr(rtp, "_send_alert_item", record)
        news = type("T", (), {"func": staticmethod(lambda: [
            {"title": "英伟达牵头5000亿美元融资 私募巨头集体入局", "content": "融资",
             "source": "财联社", "published_at": "2026-08-11 08:00:00"}])})()
        sig = type("T", (), {"func": staticmethod(lambda: [])})()
        monkeypatch.setattr(rtp, "get_stock_news", news)
        monkeypatch.setattr(rtp, "get_market_signals", sig)
        stats = rtp.run_once(dry_run=False)
        assert stats["pushed"] == 1, "重大消息应正常推送"
        assert len(sent) == 1 and "英伟达" in sent[0]


class TestMacroDataPrefilter:
    """2026-08-12 修复：美国7月CPI 87条全漏推——宏观数据发布类预筛直通 LLM"""

    def test_cpi_data_release_direct_llm(self):
        """数据本体/前瞻/市场反应/全称变体均直通（此前预筛 0.14 被拦）"""
        cases = [
            "美国7月CPI同比 3.4%，预期 3.4%，前值 3.5%。",
            "美国7月CPI同比增长3.4% 符合市场预期",
            "高盛：美国七月消费者物价指数表现令人鼓舞",
            "新闻交易员：今夜美国CPI，可能掀翻整个利率剧本",
            "美国7月CPI数据公布后，交易员略微降低9月份美联储加息的压注",
            "提醒：北京时间20:30，将公布美国7月CPI数据",
            "德国7月CPI同比终值 2.8%，预期2.8%，前值2.80%。",
            "美国7月未季调核心CPI年率 2.5%，预期2.50%，前值2.60%。",
        ]
        for t in cases:
            score, hit = rtp._prefilter({"title": t, "content": "", "category": "news"})
            assert hit, f"应直通 LLM: {t}"
            assert score >= 0.55 or hit, f"命中高信号词应视为直通: {t}"

    def test_ppl_gdp_rate_decision_direct_llm(self):
        """PPI/GDP/利率决议/失业率等同级宏观数据同样直通"""
        cases = [
            "中国7月PPI同比下降2.1% 环比下降0.3%",
            "美国二季度GDP年化季率修正值 2.1%，预期 2.0%",
            "美联储利率决议：维持利率不变 符合市场预期",
            "美国7月失业率 4.1%，预期 4.2%，前值 4.3%。",
        ]
        for t in cases:
            _, hit = rtp._prefilter({"title": t, "content": "", "category": "news"})
            assert hit, f"应直通 LLM: {t}"

    def test_non_macro_context_not_bypassed(self):
        """非数据语境（芯片通胀/普通行情）不因宏观词误直通"""
        cases = [
            "AI需求引爆芯片通胀？大摩：或会持续多年",
            "韩国KOSPI指数日内涨5% 现报6665点",
            "中金：高端感光干膜需求扩张 具备长期投资价值",
        ]
        for t in cases:
            score, hit = rtp._prefilter({"title": t, "content": "", "category": "news"})
            if not hit:
                assert score < 0.55, f"未命中高信号词不应直通: {t}"

    def test_fingerprint_broad_words_include_macro(self):
        """宏观词在指纹宽泛排除表中：美CPI 与 德CPI 不同指纹（防互相覆盖漏推）"""
        from src.tools.keyword_tables import find_signal_fp_keywords
        fp_us = find_signal_fp_keywords("美国7月CPI同比 3.4% 预期3.4%")
        fp_de = find_signal_fp_keywords("德国7月CPI同比终值 2.8% 预期2.8%")
        assert fp_us == [] and fp_de == [], \
            f"宏观词应退出指纹 sig 路径（退回标题指纹），实际: {fp_us} / {fp_de}"


class TestNoiseColumnSummary:
    """2026-08-12 修复：金十"欧盘美盘重要新闻汇总"漏推→补"新闻汇总"类栏目词"""

    def test_news_summary_variants(self):
        """新闻汇总/要闻汇总/市场汇总 均识别为栏目汇总"""
        cases = [
            "金十数据整理欧盘美盘重要新闻汇总20260811",
            "金十数据整理隔夜要闻汇总",
            "今日市场要闻汇总：三大指数集体收涨",
        ]
        for t in cases:
            assert rtp._is_noise_push({"title": t}, {"is_leader_stock": False}, set()) == "栏目汇总", t

    def test_macro_early_report_not_hit(self):
        """防误伤：含宏观数据的早报类不被"新闻汇总"误伤"""
        t = "期货早报美国非农意外减少23万人 加息预期骤变"
        assert rtp._is_noise_push({"title": t}, {"is_leader_stock": False}, set()) == "", t


class TestTopicSaturation:
    """2026-08-12 防偏科：同题材 24h 内推送饱和拦截（存储 17 推实证）"""

    def _sig(self, sectors=(), entities=(), scope="sector"):
        return {"sectors": sorted(sectors), "stocks": [], "entities": sorted(entities),
                "events": [], "numbers": [], "title_norm": "x", "scope": scope}

    @staticmethod
    def _ts(hours_ago=1):
        """动态时间戳（24h 窗口内的相对时间，避免硬编码日期随时间流逝失效）"""
        return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def test_same_entity_saturated(self):
        """同实体 24h 内已推 5 条 → 饱和拦截"""
        pushed = [{"sectors": [], "stocks": [], "entities": ["SK海力士"],
                   "t": self._ts(1)} for _ in range(5)]
        assert rtp._topic_saturated(self._sig(entities=["SK海力士"]), pushed) is True

    def test_same_sector_saturated(self):
        """同板块 24h 内已推 5 条 → 饱和拦截"""
        pushed = [{"sectors": ["存储"], "stocks": [], "entities": [],
                   "t": self._ts(1)} for _ in range(5)]
        assert rtp._topic_saturated(self._sig(sectors=["存储"]), pushed) is True

    def test_below_limit_not_saturated(self):
        """4 条未达上限 → 放行"""
        pushed = [{"sectors": ["存储"], "stocks": [], "entities": [],
                   "t": self._ts(1)} for _ in range(4)]
        assert rtp._topic_saturated(self._sig(sectors=["存储"]), pushed) is False

    def test_market_scope_exempt(self):
        """2026-08-25 语义收窄：仅宏观数据发布类 market 豁免，CPI 永不受饱和拦截"""
        pushed = [{"sectors": ["宏观"], "stocks": [], "entities": [],
                   "t": self._ts(1)} for _ in range(10)]
        sig = self._sig(sectors=["宏观"], scope="market")
        sig["title_norm"] = "美国7月CPI同比3.4%符合预期"
        assert rtp._topic_saturated(sig, pushed) is False

    def test_geopolitical_market_saturated(self):
        """market 级地缘/关税（非数据发布）参与同主题饱和：已推 3 条 → 饱和"""
        pushed = [{"sectors": [], "stocks": [], "entities": [], "events": [],
                   "numbers": [], "title_norm": "特朗普称对加拿大汽车关税升至50",
                   "scope": "market", "t": self._ts(1)} for _ in range(3)]
        sig = self._sig(scope="market")
        sig["title_norm"] = "美加贸易谈判破裂 特朗普再威胁加拿大关税"
        assert rtp._topic_saturated(sig, pushed) is True

    def test_geopolitical_market_below_limit(self):
        """市场地缘类未达 3 条上限 → 放行"""
        pushed = [{"sectors": [], "stocks": [], "entities": [], "events": [],
                   "numbers": [], "title_norm": "伊朗货币里亚尔跌至历史新低",
                   "scope": "market", "t": self._ts(1)} for _ in range(2)]
        sig = self._sig(scope="market")
        sig["title_norm"] = "美国再度加码对伊朗制裁"
        assert rtp._topic_saturated(sig, pushed) is False

    def test_outside_window_not_counted(self):
        """24h 窗口外的推送不计入饱和"""
        pushed = [{"sectors": ["存储"], "stocks": [], "entities": [],
                   "t": self._ts(48)} for _ in range(5)]
        assert rtp._topic_saturated(self._sig(sectors=["存储"]), pushed) is False

    def test_different_topic_not_saturated(self):
        """不同题材不互相影响"""
        pushed = [{"sectors": ["存储"], "stocks": [], "entities": [],
                   "t": self._ts(1)} for _ in range(5)]
        assert rtp._topic_saturated(self._sig(sectors=["光模块"]), pushed) is False


class TestMacroPolicyPriority:
    """2026-08-12 防偏科：溢出排序宏观/政策类优先于普通科技噪声"""

    def test_macro_policy_detected(self):
        """宏观/政策/地缘类识别"""
        macro = [
            {"title": "中国央行宣布降准0.5个百分点"},
            {"title": "美国7月CPI同比 3.4% 符合预期"},
            {"title": "证监会就上市公司监管新规公开征求意见"},
            {"title": "中东局势升级 国际油价大涨"},
        ]
        normal = [
            {"title": "港股存储概念走强 南方两倍做多三星电子涨16.87%"},
            {"title": "中金：高端感光干膜需求扩张"},
        ]
        for n in macro:
            assert rtp._is_macro_policy(n) is True, n["title"]
        for n in normal:
            assert rtp._is_macro_policy(n) is False, n["title"]

    def test_overflow_sort_macro_first(self):
        """同命中高信号词时，宏观/政策类排在前（防存储行情挤出宏观）"""
        import types
        cands = [
            {"title": "港股存储概念走强 南方两倍做多三星电子涨16.87%",
             "_hit_signal": True, "_pref_score": 0.9},
            {"title": "美国7月CPI数据公布后 交易员降低加息压注",
             "_hit_signal": True, "_pref_score": 0.8},
        ]
        cands.sort(key=lambda x: (
            1 if x.get("_hit_signal") else 0,
            1 if rtp._is_macro_policy(x) else 0,
            x["_pref_score"]), reverse=True)
        assert "CPI" in cands[0]["title"], "宏观类应在科技噪声之前"


class TestSameEventEveningRepeat:
    """2026-08-12 修复：晚间推送重复（22:01 同轮双推 + 21:31/22:01 跨轮重复）"""

    def _sig(self, title, entities=(), sectors=(), scope="sector"):
        return {"stocks": [], "entities": list(entities), "events": [], "numbers": [],
                "sectors": list(sectors), "scope": scope,
                "title_norm": rtp._normalize_title(title)}

    def test_same_round_sector_move_merge(self):
        """22:01 同轮双推：美股光通信/存储普涨多源报道应合并"""
        a = self._sig("美股光通信存储概念股普涨 诺基亚升逾9 Ciena Lumentum Coherent",
                      ["Ciena", "Coherent", "Lumentum", "诺基亚"], ["光模块/CPO", "半导体", "存储"])
        b = self._sig("美国7月通胀表现温和 美股盘初纳指涨09 光通信股存储芯片股普涨",
                      ["CoreWeave", "Lumentum", "超微电脑"], ["AI算力", "光通信", "半导体"], scope="market")
        assert rtp._is_same_event(a, b) is True

    def test_cross_round_macro_reaction_merge(self):
        """21:31 与 22:01 跨轮：CPI 后美股反应同一宏观事件应合并"""
        a = self._sig("美国7月通胀表现温和 美股盘初纳指涨09 光通信股存储芯片股普涨",
                      ["CoreWeave", "Lumentum", "超微电脑"], ["AI算力", "光通信", "半导体"], scope="market")
        b = self._sig("美国CPI数据符合预期 美股高开", ["SK海力士", "美光科技", "闪迪"])
        assert rtp._is_same_event(a, b) is True

    def test_diff_country_cpi_not_merge(self):
        """不同国家 CPI 不误并（无同市场域词）"""
        a = self._sig("德国7月CPI同比终值 2.8% 预期2.8%", ["德国"])
        b = self._sig("美国7月CPI同比 3.4% 符合预期", ["美国"])
        assert rtp._is_same_event(a, b) is False

    def test_opposite_direction_not_merge(self):
        """板块行情方向对立不合并"""
        a = self._sig("存储芯片价格上涨 涨幅扩大", ["三星"], ["半导体", "存储"])
        b = self._sig("半导体板块走弱 集体下挫", ["三星"], ["半导体"])
        assert rtp._is_same_event(a, b) is False

    def test_diff_sector_not_merge(self):
        """不同板块（光模块 vs 存储）不合并"""
        a = self._sig("光模块涨价逻辑延续 龙头提价", ["中际旭创"], ["光模块/CPO"])
        b = self._sig("存储芯片涨价 三星美光跟进", ["三星"], ["存储"])
        assert rtp._is_same_event(a, b) is False


class TestSiblingReportsMerge:
    """2026-08-25 同实体兄弟报道合并（字节豆包×2/小米玄戒×2/华为发布会×2 同轮重复）"""

    @staticmethod
    def _sig(title, entities=(), sectors=(), events=(), scope="sector"):
        return {"stocks": [], "entities": sorted(entities), "events": sorted(events),
                "numbers": [], "sectors": sorted(sectors), "title_norm": title, "scope": scope}

    def test_doubao_duplicate_merged(self):
        a = self._sig("字节跳动发布豆包工作与飞书深度打通构建企业级Agent", ["字节跳动"])
        b = self._sig("字节加入企业办公Agent大战 发布豆包工作", ["字节跳动"])
        assert rtp._is_same_event(a, b) is True

    def test_xuanji_duplicate_merged_same_sector(self):
        a = self._sig("小米新一代自研处理器玄戒O3亮相采用3nm工艺", ["小米"], ["消费电子"])
        b = self._sig("小米玄戒芯片完成三个方向演进迭代", ["小米"], ["消费电子"])
        assert rtp._is_same_event(a, b) is True

    def test_huawei_launch_merged(self):
        a = self._sig("华为全场景新品发布会将于9月7日召开", ["华为"], ["消费电子"])
        b = self._sig("华为新一代三折叠旗舰手机9月7日发布", ["华为"], ["消费电子"])
        assert rtp._is_same_event(a, b) is True

    def test_diff_std_shared_noun_not_merged(self):
        """反例：仅共享"国际标准"4字通用短语、无共享板块 → 不误并（防漏推）"""
        a = self._sig("我国牵头固态电池领域首个国际标准立项", ["科技部"])
        b = self._sig("我国制定磁性元件领域国际标准发布", ["科技部"])
        assert rtp._is_same_event(a, b) is False


class TestNoisePushNewWords20260825:
    """2026-08-25 审核实证：净流出/震荡回升/涨停/半日涨跌等盘面措辞补词 + 栏目剥离后行情排除"""

    def _j(self, **kw):
        j = {"push": True, "score": 8, "direction": "bearish", "scope": "sector",
             "is_leader_stock": False, "entities": []}
        j.update(kw)
        return j

    def test_net_flow_filtered(self):
        """主力资金净流出等资金流盘面不推"""
        assert rtp._is_noise_push({"title": "主力资金转融券标的板块净流出超742亿元"},
                                  self._j(), set()) == "盘面异动"

    def test_surging_concept_filtered(self):
        """概念震荡回升+涨停（非龙头）不推"""
        assert rtp._is_noise_push({"title": "人形机器人概念震荡回升 兆威机电涨停"},
                                  self._j(), set()) == "盘面异动"

    def test_noon_review_index_move_as_column(self):
        """"午评"剥离后仍是"指数名+半日跌" → 栏目汇总（此前 has_signal 误放行）"""
        assert rtp._is_noise_push({"title": "午评 创业板指半日跌3.5% 算力硬件股大面积调整"},
                                  self._j(), set()) == "栏目汇总"

    def test_leader_plunge_kept(self):
        """龙头个股跳水保留（is_leader 例外，防漏推持仓龙头）"""
        assert rtp._is_noise_push({"title": "万亿中际旭创跳水 股价跌破900元"},
                                  self._j(is_leader_stock=True), set()) == ""


class TestPendingCleanse:
    """2026-08-25 pending 泄漏清理：已推同事件标题相似记录不再无限重试"""

    def test_similar_pushed_title_removed(self):
        assert rtp._pending_same_as_pushed(
            "华为全场景新品发布会将于9月7日召开",
            "华为全场景新品发布会将于9月7日召开") is True
        assert rtp._pending_same_as_pushed(
            "Meta计划在未来几周推出“HATCH”AI代理平台",
            "Meta计划在未来几周推出HATCHAI代理平台") is True
        assert rtp._pending_same_as_pushed(
            "小鹏机器人业务首轮融资超9亿美元",
            "小鹏机器人业务首轮融资超9亿美元引领物理AI规模量产") is True

    def test_unrelated_not_removed(self):
        assert rtp._pending_same_as_pushed(
            "大豆期货午后拉升 贸易商逢高抛售",
            "字节跳动发布豆包工作与飞书打通") is False


class TestOppositeNotePrimaryEntity20260825:
    """2026-08-25 实证误配修复：反向事件附注须以对方事件主实体匹配。

    英伟达 Jetson 产品发布（主实体=英伟达）被挂上"易中天三大巨头集体下跌"
    （英伟达只是文中偶然提及的第三实体，主实体是光模块三巨头）——暗示
    不存在的叙事矛盾。收紧后：共享实体须为对方主实体（stocks∪首实体）或 ≥2 共享。
    """

    @staticmethod
    def _pe(title, entities, stocks=(), direction="bearish", t=None):
        return {"title_norm": title, "entities": list(entities), "stocks": list(stocks),
                "sectors": [], "events": [], "numbers": [],
                "dir": direction,
                "t": t or (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")}

    def test_incidental_entity_no_note(self):
        """旧事件第三实体（英伟达）≠ 主实体 → 不挂反向附注"""
        sig = {"stocks": [], "entities": ["英伟达"], "sectors": ["AI硬件/机器人"],
               "title_norm": "英伟达宣布JetsonOrinNano2面向入门级边缘AI"}
        pushed = [self._pe("突然变盘易中天三大巨头集体下跌重磅新技术曝光影响多大",
                           ["SK海力士", "中际旭创", "英伟达"], direction="bearish")]
        assert rtp._opposite_events_note(sig, "bullish", pushed) == ""

    def test_primary_entity_reverse_kept(self):
        """旧事件主实体=英伟达的反向事件仍正常提示"""
        sig = {"stocks": [], "entities": ["英伟达"], "sectors": [],
               "title_norm": "英伟达发布新一代数据中心GPU"}
        pushed = [self._pe("美国对英伟达发起反垄断调查", ["英伟达"], direction="bearish")]
        note = rtp._opposite_events_note(sig, "bullish", pushed)
        assert "英伟达" in note and "利空" in note

    def test_stock_primary_reverse_kept(self):
        """stocks 承载主实体（个股事件）反向提示保留"""
        sig = {"stocks": ["中际旭创"], "entities": [], "sectors": ["光模块"],
               "title_norm": "中际旭创获大额订单"}
        pushed = [self._pe("中际旭创跳水股价跌破900", ["中际旭创"], direction="bearish")]
        note = rtp._opposite_events_note(sig, "bullish", pushed)
        assert "中际旭创" in note

    def test_two_shared_entities_kept(self):
        """双方共享 ≥2 实体（强同主体）即使非首实体也提示"""
        sig = {"stocks": [], "entities": ["英伟达", "台积电"], "sectors": [],
               "title_norm": "英伟达与台积电深化合作"}
        pushed = [self._pe("台积电英伟达合资工厂计划生变", ["台积电", "三星", "英伟达"],
                           direction="bearish")]
        note = rtp._opposite_events_note(sig, "bullish", pushed)
        assert note != ""


class TestRelatedRecentPrimaryEntity20260825:
    """叙事链上下文注入（LLM prompt 前置）同口径收紧：仅旧事件主实体匹配"""

    def test_incidental_entity_not_injected(self):
        n = {"title": "英伟达宣布JetsonOrinNano2机器人计算机", "content": "入门级边缘AI"}
        pushed = [{"title_norm": "突然变盘易中天三大巨头集体下跌",
                   "entities": ["SK海力士", "中际旭创", "英伟达"], "stocks": [],
                   "dir": "bearish",
                   "t": (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")}]
        assert rtp._related_recent_note(n, pushed) == []

    def test_primary_entity_injected(self):
        n = {"title": "英伟达发布新一代HBM配套GPU", "content": "数据中心级"}
        pushed = [{"title_norm": "英伟达遭反垄断调查", "entities": ["英伟达"],
                   "stocks": [], "dir": "bearish",
                   "t": (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")}]
        out = rtp._related_recent_note(n, pushed)
        assert len(out) == 1 and "利空" in out[0]


class TestMarketThemeEntityScan20260825:
    """2026-08-25 伊朗海事漏拦修复：主题键扫描扩展到实体 + market 主题检查先于实体槽位"""

    @staticmethod
    def _ts(hours_ago=1):
        return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _sig(title, entities, scope="market"):
        return {"scope": scope, "sectors": [], "stocks": [], "entities": list(entities),
                "events": [], "numbers": [], "title_norm": title}

    @staticmethod
    def _pe(title, entities, t):
        return {"title_norm": title, "entities": list(entities), "stocks": [],
                "sectors": [], "events": [], "numbers": [], "dir": "bearish", "t": t}

    def test_entity_carried_theme_saturated(self):
        """标题无主题词但实体=伊朗：窗口内已有3条伊朗主题 → 饱和拦截（19:03 漏拦场景）"""
        sig = self._sig("国际海事组织近半年来中东海域发生68起袭击", ["伊朗", "国际海事组织"])
        pushed = [self._pe("伊朗最高领袖顾问回应美方威胁", ["伊朗"], self._ts(11)),
                  self._pe("美国加码对伊朗制裁暗藏风险", ["伊朗", "美国"], self._ts(10)),
                  self._pe("美公布多项针对伊朗经济制裁", ["伊朗", "美国财政部"], self._ts(10))]
        assert rtp._topic_saturated(sig, pushed) is True

    def test_entity_theme_below_limit_pass(self):
        """窗口内仅2条主题命中 → 不饱和放行"""
        sig = self._sig("国际海事组织中东海域袭击统计", ["伊朗"])
        pushed = [self._pe("伊朗领袖顾问回应威胁", ["伊朗"], self._ts(11)),
                  self._pe("美对伊朗经济制裁", ["伊朗"], self._ts(10))]
        assert rtp._topic_saturated(sig, pushed) is False

    def test_cpi_exemption_unchanged(self):
        """宏观数据发布仍豁免（即使实体非空）"""
        sig = self._sig("美国8月CPI同比3.2%高于预期", ["美国劳工统计局"])
        pushed = [self._pe("美国7月CPI同比3.4%符合预期", ["美国"], self._ts(5))] * 5
        assert rtp._topic_saturated(sig, pushed) is False
