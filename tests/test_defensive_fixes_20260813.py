# filepath: tests/test_defensive_fixes_20260813.py
"""2026-08-13 第四轮复审修复的回归测试

覆盖：
1. P1-1 类型防御 _as_list：sectors/entities/affected_stocks 为字符串时不拆字
2. P1-2 watchlist 空名单告警（仅日志，无单测断言，行为由 run_once e2e 隐式覆盖）
3. P2-1 _env_int 非法值回退默认
4. P2-2 format_push_alert 对 score=None 显示兜底
5. P2-4 _llm_judge 单批超时 60s（由 deadline 熔断逻辑测试覆盖批次完成熔断）
6. P2-5 企业微信致命 errcode 不重试
7. run_once 主流程 mock 端到端（此前无集成测试）
"""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

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
# P1-1 _as_list 类型防御
# ============================================================
class TestAsList:
    def test_none(self):
        assert rtp._as_list(None) == []

    def test_str_becomes_single_element(self):
        assert rtp._as_list("半导体") == ["半导体"]

    def test_list_tuple_set_passthrough(self):
        assert rtp._as_list(["a", "b"]) == ["a", "b"]
        assert rtp._as_list(("a", "b")) == ["a", "b"]
        assert rtp._as_list({"a"}) == ["a"]

    def test_other_types_empty(self):
        assert rtp._as_list(123) == []
        assert rtp._as_list({"k": "v"}) == []
        assert rtp._as_list(True) == []


class TestSectorsOverlapStringDefense:
    def test_string_vs_list_no_split(self):
        """字符串 sectors 不再拆成单字，与数组板块正确求交集"""
        hit = rtp._sectors_overlap("半导体", ["半导体", "AI算力"])
        assert hit == {"半导体"}

    def test_string_vs_string(self):
        hit = rtp._sectors_overlap("AI算力", "算力")
        assert hit == {"算力"}


class TestEventSignatureLightStringDefense:
    def test_affected_stocks_string_not_split(self):
        """affected_stocks 为字符串（'宁德时代'）时不再拆成单字"""
        news = {"title": "宁德时代回购5亿", "content": "", "affected_stocks": "宁德时代"}
        stocks, events, numbers = rtp._event_signature_light(news)
        assert stocks == {"宁德时代"}
        assert len(stocks) == 1


class TestLlmJudgeStringDefense:
    def test_sectors_and_entities_string_normalized(self, monkeypatch):
        """LLM 返回 sectors/entities 为字符串时，判定结果保留整词而非拆字"""
        items = [{"title": "半导体板块大涨", "content": "英伟达业绩超预期",
                  "published_at": "2026-08-13 10:00:00", "_judge_idx": 0}]

        def fake_llm(system_prompt, user_prompt, **kw):
            return json.dumps([{
                "idx": 0, "title": "半导体板块大涨", "push": True, "score": 8,
                "direction": "bullish", "scope": "sector",
                "sectors": "半导体", "entities": "英伟达",
                "is_leader_stock": True, "reason": "科技龙头",
            }], ensure_ascii=False)

        monkeypatch.setattr(rtp, "_call_llm_api", fake_llm)
        judges = rtp._llm_judge(items)
        j = judges[0]
        assert j["judged"] is True
        assert j["sectors"] == ["半导体"]
        assert j["entities"] == ["英伟达"]

    def test_sectors_string_keeps_tech_override_alive(self, monkeypatch):
        """字符串 sectors 下 _is_domestic_tech 仍命中（科技兜底不静默失效）"""
        news = {"title": "台积电CoWoS良率升至98%", "content": "先进封装产能满载",
                "source": "财联社电报", "published_at": "2026-08-13 10:00:00"}
        judge = {"push": False, "score": 7, "direction": "bullish", "scope": "sector",
                 "sectors": "半导体", "entities": ["台积电"], "is_leader_stock": False,
                 "reason": "LLM 否决测试"}
        # 未命中研报/观点措辞 → 科技兜底应放行
        assert rtp._tech_override_enabled(news, judge, set()) is True


class TestLlmJudgeBatchDeadline:
    def test_deadline_after_batch_breaks_remaining(self, monkeypatch):
        """批次完成后逼近 deadline，剩余条目全部挂起（P2-4 熔断粒度）"""
        items = [{"title": f"测试{i}", "content": "内容", "published_at": "",
                  "_judge_idx": i} for i in range(20)]  # 3 批（8+8+4）
        import time as _time

        def fake_llm(system_prompt, user_prompt, **kw):
            return json.dumps([{
                "idx": i, "title": f"测试{i}", "push": True, "score": 8,
                "direction": "bullish", "scope": "market",
                "sectors": [], "entities": [], "is_leader_stock": False,
            } for i in range(len(items))], ensure_ascii=False)

        monkeypatch.setattr(rtp, "_call_llm_api", fake_llm)
        # deadline 设为 1s 后——批次 1 完成后熔断，剩余 12 条挂起
        deadline = _time.monotonic() + 1
        # 让批次 1 完成后 deadline 已过：给批次间加一点耗时
        real_parse = rtp._parse_llm_array
        calls = {"n": 0}

        def slow_wrapper(content):
            calls["n"] += 1
            if calls["n"] == 1:
                # 模拟批次 1 耗时超过 deadline（等待 1.2s）
                _time.sleep(1.2)
            return real_parse(content)

        monkeypatch.setattr(rtp, "_parse_llm_array", slow_wrapper)
        judges = rtp._llm_judge(items, deadline=deadline)
        judged = [j for j in judges if j.get("judged")]
        hung = [j for j in judges if not j.get("judged")]
        # 批次 1（8 条）已判定，剩余 12 条挂起
        assert len(judged) == 8
        assert len(hung) == 12
        assert all(j.get("reason") == "LLM未判定，挂起下轮重试" for j in hung)


# ============================================================
# P2-1 _env_int 防御
# ============================================================
class TestEnvInt:
    def test_valid(self, monkeypatch):
        monkeypatch.setenv("RT_TOPIC_LIMIT", "7")
        assert rtp._env_int("RT_TOPIC_LIMIT", 5) == 7

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("RT_TOPIC_LIMIT", "abc")
        assert rtp._env_int("RT_TOPIC_LIMIT", 5) == 5

    def test_missing_falls_back(self, monkeypatch):
        monkeypatch.delenv("RT_TOPIC_LIMIT", raising=False)
        assert rtp._env_int("RT_TOPIC_LIMIT", 5) == 5


# ============================================================
# P2-2 format_push_alert score=None 兜底
# ============================================================
class TestFormatPushAlertScoreNone:
    def test_score_none_shows_zero(self):
        news = {"title": "央行降准", "content": "释放流动性", "source": "财联社电报",
                "published_at": "2026-08-13 10:00:00"}
        judge = {"direction": "bullish", "scope": "market", "score": None,
                 "sectors": [], "reason": "宏观重大", "entities": []}
        out = rtp.format_push_alert(news, judge)
        assert "影响分**: 0" in out
        assert "None" not in out

    def test_score_string_ok(self):
        news = {"title": "央行降准", "content": "", "source": "s", "published_at": ""}
        judge = {"direction": "bullish", "scope": "market", "score": "7.5",
                 "sectors": [], "reason": "", "entities": []}
        out = rtp.format_push_alert(news, judge)
        assert "影响分**: 7.5" in out

    def test_sectors_string_not_split_in_display(self):
        news = {"title": "央行降准", "content": "", "source": "s", "published_at": ""}
        judge = {"direction": "bullish", "scope": "market", "score": 8,
                 "sectors": "半导体", "reason": "", "entities": []}
        out = rtp.format_push_alert(news, judge)
        assert "半导体" in out
        assert "半、导、体" not in out


# ============================================================
# P2-5 企业微信致命 errcode 不重试
# ============================================================
class TestWecomFatalErrcode:
    def test_fatal_errcode_no_retry(self, monkeypatch):
        import src.tools.push as push_mod
        calls = []

        class FakeResp:
            status_code = 200

            def json(self):
                return {"errcode": 93000, "errmsg": "invalid webhook key"}

        def fake_post(url, json=None, timeout=None):
            calls.append(1)
            return FakeResp()

        monkeypatch.setattr(push_mod.requests, "post", fake_post)
        result = push_mod.push_via_wecom("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
                                         "标题", "内容")
        assert result["errcode"] == 93000
        assert len(calls) == 1  # 致命错误不重试


# ============================================================
# P0: Gist 状态读取/合并失败 fail-stop（2026-08-13 状态丢失事故根因）
# ============================================================
class TestGistLoadFailStop:
    def test_read_failure_raises(self, monkeypatch):
        """3 次读取失败 → raise（禁止静默空状态）"""
        import requests as real_requests

        def fake_get(url, timeout=None, headers=None):
            raise IOError("network down")

        monkeypatch.setattr(real_requests, "get", fake_get)
        with pytest.raises(RuntimeError, match="读取失败"):
            rtp._gist_load("token", "gid")

    def test_json_corrupt_raises(self, monkeypatch):
        """JSON 截断损坏 → raise JSONDecodeError（18:02 轮 0.72MB 截断实证）"""
        import requests as real_requests
        broken = '{"seen": {"a": {"t": "2026-08-13 10:00:00", "pushed": true, "title": "未闭合'

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"files": {"real_time_state.json": {"content": broken}}}

        monkeypatch.setattr(real_requests, "get", lambda url, timeout=None, headers=None: FakeResp())
        with pytest.raises(json.JSONDecodeError):
            rtp._gist_load("token", "gid")

    def test_missing_seen_key_raises(self, monkeypatch):
        """结构缺 seen 键 → raise ValueError（拒绝把畸形内容当空状态）"""
        import requests as real_requests

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"files": {"real_time_state.json": {"content": '{"pending": {}}'}}}

        monkeypatch.setattr(real_requests, "get", lambda url, timeout=None, headers=None: FakeResp())
        with pytest.raises(ValueError, match="seen"):
            rtp._gist_load("token", "gid")

    def test_valid_state_ok(self, monkeypatch):
        """合法状态正常返回"""
        import requests as real_requests
        good = '{"seen": {"fp": {"t": "2026-08-13 10:00:00", "pushed": true}}, "pending": {}, "pushed_events": []}'

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"files": {"real_time_state.json": {"content": good}}}

        monkeypatch.setattr(real_requests, "get", lambda url, timeout=None, headers=None: FakeResp())
        st = rtp._gist_load("token", "gid")
        assert "fp" in st["seen"]


class TestSaveStateFailStop:
    def test_merge_failure_ci_raises(self, monkeypatch):
        """CI 下合并失败 → 拒绝覆盖并报错（防 19:03 覆盖事故重演）"""
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("GIST_TOKEN", "t")
        monkeypatch.setenv("GIST_ID", "g")

        def boom(token, gid):
            raise RuntimeError("gist down")

        monkeypatch.setattr(rtp, "_gist_load", boom)
        with pytest.raises(RuntimeError, match="拒绝覆盖"):
            rtp.save_state(rtp._empty_state())

    def test_merge_failure_local_does_not_write_gist(self, monkeypatch, tmp_path):
        """本地模式合并失败 → 拒绝写 Gist，降级写本地文件"""
        monkeypatch.setenv("CI", "false")
        monkeypatch.setenv("GIST_TOKEN", "t")
        monkeypatch.setenv("GIST_ID", "g")
        gist_called = []

        def boom(token, gid):
            raise RuntimeError("gist down")

        monkeypatch.setattr(rtp, "_gist_load", boom)
        monkeypatch.setattr(rtp, "_gist_save", lambda token, gid, state: gist_called.append(1))
        monkeypatch.setattr(rtp, "_state_path", lambda: Path(tmp_path) / "state.json")
        rtp.save_state(rtp._empty_state())
        assert gist_called == []  # 未写 Gist
        assert (Path(tmp_path) / "state.json").exists()  # 降级写本地

    def test_load_state_ci_gist_failure_raises(self, monkeypatch):
        """CI 下 Gist 读取失败 → load_state 报错退出（不再空状态运行）"""
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("GIST_TOKEN", "t")
        monkeypatch.setenv("GIST_ID", "g")

        def boom(token, gid):
            raise RuntimeError("gist down")

        monkeypatch.setattr(rtp, "_gist_load", boom)
        with pytest.raises(RuntimeError):
            rtp.load_state()

    def test_seen_cap_limits_state_size(self, monkeypatch, tmp_path):
        """seen 超上限按时间保留最新（状态文件体积控制，防 Gist 写入截断）"""
        monkeypatch.setenv("CI", "false")
        monkeypatch.delenv("GIST_TOKEN", raising=False)
        monkeypatch.delenv("GIST_ID", raising=False)
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = rtp._empty_state()
        for i in range(rtp.SEEN_MAX + 100):
            state["seen"][f"fp{i:04d}"] = {"t": now, "pushed": False, "title": f"t{i}"}
        monkeypatch.setattr(rtp, "_state_path", lambda: Path(tmp_path) / "state.json")
        rtp.save_state(state)
        saved = json.loads((Path(tmp_path) / "state.json").read_text(encoding="utf-8"))
        assert len(saved["seen"]) <= rtp.SEEN_MAX
        assert "fp0000" not in saved["seen"]  # 最早的被截断


# ============================================================
# run_once 主流程 mock 端到端（P3-2 补集成测试）
# ============================================================
class _FuncHolder:
    def __init__(self, fn):
        self.func = fn


def _e2e_patch(monkeypatch, news_list):
    monkeypatch.setattr(rtp, "get_stock_news", _FuncHolder(lambda: [dict(n) for n in news_list]))
    monkeypatch.setattr(rtp, "get_market_signals", _FuncHolder(lambda: []))
    monkeypatch.setattr(rtp, "load_state", lambda: rtp._empty_state())
    monkeypatch.setattr(rtp, "save_state", lambda state: None)


def _e2e_fake_llm(push=True, direction="bullish", scope="market", sectors="宏观政策"):
    """按 user_prompt 内 idx 回显判定（sectors 故意用字符串验证类型防御链路）"""

    def fake(system_prompt, user_prompt, **kw):
        try:
            start = user_prompt.index("[")
            arr = json.loads(user_prompt[start:])
        except Exception:
            arr = []
        return json.dumps([{
            "idx": it["idx"], "title": it["title"], "push": push,
            "score": 9, "direction": direction, "scope": scope,
            "sectors": sectors, "entities": ["央行"], "is_leader_stock": False,
            "reason": "e2e mock",
        } for it in arr], ensure_ascii=False)

    return fake


class TestRunOnceE2E:
    def test_strong_market_news_pushed(self, monkeypatch):
        news_list = [
            {"title": "央行宣布降准0.5个百分点", "content": "释放长期资金约1万亿",
             "source": "财联社电报", "published_at": "2026-08-13 10:00:00",
             "category": "news", "sentiment": "neutral"},
            {"title": "证监会发布退市新规", "content": "完善常态化退市机制",
             "source": "东方财富快讯", "published_at": "2026-08-13 10:05:00",
             "category": "news", "sentiment": "neutral"},
        ]
        _e2e_patch(monkeypatch, news_list)
        monkeypatch.setattr(rtp, "_call_llm_api", _e2e_fake_llm(push=True, direction="bullish", scope="market"))
        stats = rtp.run_once(dry_run=True)
        # 两条高信号直通 + LLM 判 market/bullish 强档 → 均推送
        assert stats["pushed"] == 2
        assert stats["new"] == 2

    def test_non_strong_direction_not_pushed(self, monkeypatch):
        news_list = [
            {"title": "央行宣布降准0.5个百分点", "content": "释放长期资金约1万亿",
             "source": "财联社电报", "published_at": "2026-08-13 10:00:00",
             "category": "news", "sentiment": "neutral"},
        ]
        _e2e_patch(monkeypatch, news_list)
        # LLM 判 mildly_bullish（弱档）→ 强档门槛拦截，不推
        monkeypatch.setattr(rtp, "_call_llm_api", _e2e_fake_llm(push=True, direction="mildly_bullish", scope="market"))
        stats = rtp.run_once(dry_run=True)
        assert stats["pushed"] == 0
        assert stats["skipped"] == 1

    def test_llm_rejected_not_pushed(self, monkeypatch):
        news_list = [
            {"title": "某中小公司回购5000万", "content": "股份回购方案",
             "source": "财联社电报", "published_at": "2026-08-13 10:00:00",
             "category": "news", "sentiment": "neutral"},
        ]
        _e2e_patch(monkeypatch, news_list)
        # LLM 判 push=false（非龙头中小市值）→ 一票否决不推
        monkeypatch.setattr(rtp, "_call_llm_api", _e2e_fake_llm(push=False, direction="bullish", scope="stock"))
        stats = rtp.run_once(dry_run=True)
        assert stats["pushed"] == 0
        assert stats["skipped"] == 1
