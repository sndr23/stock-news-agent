# filepath: tests/test_p1b_features_20260907.py
"""P1 功能批次（2026-09-07，worktree wt_p1b）单元测试

覆盖：
1. 任务1 源健康告警：data_fetchers.record_source_health 连续空轮统计 +
   real_time_push.evaluate/push_source_health_alerts（≥6 轮告警、24h 限频、
   全源同时空=市场静默不告警）+ run_once 集成
2. 任务2 公告接入（type 白名单）：白名单匹配 + run_once 接入（白名单外一律不进候选）
3. 任务3 watchlist 个股公告直通：跳过预筛竞争与推送闸门直接推送 +
   同股同类别 24h 限 1 条（用解质押公告做用例）
4. 任务4 Gist 状态体积守卫：>950KB 先压缩 seen（保留最近 1800 条）再传，
   压缩后仍超限报错（含压缩前后体积），小状态不触发
"""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送

BJT = timezone(timedelta(hours=8))


def _load_rtp():
    """以文件路径加载 scripts/real_time_push.py（scripts 非包，且 import 时会 chdir）"""
    cwd = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(
            "real_time_push_p1b", _PROJECT_ROOT / "scripts" / "real_time_push.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.chdir(cwd)


rtp = _load_rtp()

from src.tools import data_fetchers as df  # noqa: E402


def _now_str(offset_hours: int = 0) -> str:
    return (datetime.now(BJT) - timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S")


class _FuncHolder:
    def __init__(self, fn):
        self.func = fn


def _setup_run_round(monkeypatch, tmp_path, news_list=None, judge=None,
                     announcements=None, watchlist=None):
    """run_once 单轮测试公共桩：本地状态文件 + 固定 LLM 判定 + 可配置数据源"""
    news = _FuncHolder(lambda: list(news_list or []))
    sig = _FuncHolder(lambda: [])
    ann = _FuncHolder(lambda: list(announcements or []))
    monkeypatch.setattr(rtp, "get_stock_news", news)
    monkeypatch.setattr(rtp, "get_market_signals", sig)
    # raising=False：分任务 commit 的中间版本（仅任务1）尚无 get_announcements
    monkeypatch.setattr(rtp, "get_announcements", ann, raising=False)
    monkeypatch.setenv("GIST_TOKEN", "")
    monkeypatch.setenv("GIST_ID", "")
    monkeypatch.setenv("PUSHPLUS_TOKEN", "test-token")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "real_time_state.json")
    monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set(watchlist or ()))
    monkeypatch.setattr(rtp, "_send_alert_item",
                        lambda cfg, t, c: {"code": 200})
    base_judge = {"push": True, "score": 8, "direction": "bullish", "scope": "market",
                  "sectors": [], "entities": [], "is_leader_stock": False,
                  "reason": "重大事件"}
    if judge:
        base_judge.update(judge)
    monkeypatch.setattr(rtp, "_llm_judge",
                        lambda items, **kw: [dict(base_judge) for _ in items])
    return tmp_path / "real_time_state.json"


def _load_saved_state(state_path):
    return json.loads(state_path.read_text(encoding="utf-8"))


# ============================================================
# 任务1：源健康告警
# ============================================================

class TestRecordSourceHealth:
    """data_fetchers.record_source_health：每源最近拉取结果落本地状态"""

    def test_streak_counts_and_resets(self, tmp_path):
        path = tmp_path / "source_health.json"
        h1 = df.record_source_health({"东财快讯": [{"title": "x"}], "财联社电报": []}, path=path)
        assert h1["东财快讯"]["streak"] == 0 and h1["东财快讯"]["count"] == 1
        assert h1["财联社电报"]["streak"] == 1 and h1["财联社电报"]["count"] == 0
        # 第二轮：财联社继续空 → streak+1；东财转空 → streak 从 0 计 1
        h2 = df.record_source_health({"东财快讯": [], "财联社电报": []}, path=path)
        assert h2["东财快讯"]["streak"] == 1
        assert h2["财联社电报"]["streak"] == 2
        # 状态已落盘（时间 + 是否空）
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["财联社电报"]["t"]
        assert saved["财联社电报"]["count"] == 0

    def test_last_alert_preserved_across_rounds(self, tmp_path):
        path = tmp_path / "source_health.json"
        df.record_source_health({"A": []}, path=path)
        df.save_source_health({"A": {**df.load_source_health(path)["A"],
                                     "last_alert": "2026-09-07 08:00:00"}}, path=path)
        h = df.record_source_health({"A": []}, path=path)
        assert h["A"]["last_alert"] == "2026-09-07 08:00:00"

    def test_non_dict_results_returns_current(self, tmp_path):
        assert df.record_source_health(None, path=tmp_path / "x.json") == {}


class TestEvaluateSourceHealthAlerts:
    """real_time_push.evaluate_source_health_alerts：阈值/限频/市场静默"""

    def _health(self, streak=6, count=0, last_alert=""):
        return {"财联社电报": {"t": _now_str(), "count": count, "streak": streak,
                              "last_alert": last_alert},
                "东财快讯": {"t": _now_str(), "count": 5, "streak": 0,
                            "last_alert": ""}}

    def test_streak_at_threshold_alerts_with_source_and_rounds(self):
        alerts = rtp.evaluate_source_health_alerts(self._health(streak=6))
        assert len(alerts) == 1
        label, msg = alerts[0]
        assert label == "财联社电报"
        assert "财联社电报" in msg and "6 轮" in msg

    def test_streak_below_threshold_no_alert(self):
        assert rtp.evaluate_source_health_alerts(self._health(streak=5)) == []

    def test_all_sources_empty_is_market_silence(self):
        health = self._health(streak=9)
        health["东财快讯"] = {**health["东财快讯"], "count": 0, "streak": 9}
        assert rtp.evaluate_source_health_alerts(health) == []

    def test_rate_limited_within_24h(self):
        alerts = rtp.evaluate_source_health_alerts(
            self._health(streak=8, last_alert=_now_str(offset_hours=2)))
        assert alerts == []

    def test_alerts_again_after_24h(self):
        alerts = rtp.evaluate_source_health_alerts(
            self._health(streak=8, last_alert=_now_str(offset_hours=25)))
        assert len(alerts) == 1

    def test_empty_health_no_alert(self):
        assert rtp.evaluate_source_health_alerts({}) == []


class TestPushSourceHealthAlerts:
    """real_time_push.push_source_health_alerts：推送 + 限频回写"""

    def _health(self, streak=6):
        return {"财联社电报": {"t": _now_str(), "count": 0, "streak": streak,
                              "last_alert": ""},
                "东财快讯": {"t": _now_str(), "count": 5, "streak": 0, "last_alert": ""}}

    def test_alert_pushed_and_last_alert_saved(self, monkeypatch):
        sent = []
        monkeypatch.setattr(rtp, "_send_alert_item",
                            lambda cfg, t, c: sent.append((t, c)) or {"code": 200})
        saved = {}
        monkeypatch.setattr(rtp, "save_source_health",
                            lambda health, path=None: saved.update(health))
        health = self._health()
        n = rtp.push_source_health_alerts({}, health=health)
        assert n == 1
        assert sent and "财联社电报" in sent[0][1]
        assert saved["财联社电报"]["last_alert"] == _now_str()

    def test_dry_run_logs_only(self, monkeypatch):
        sent = []
        monkeypatch.setattr(rtp, "_send_alert_item",
                            lambda cfg, t, c: sent.append(c) or {"code": 200})
        saved = {}
        monkeypatch.setattr(rtp, "save_source_health",
                            lambda health, path=None: saved.update(health))
        n = rtp.push_source_health_alerts({}, dry_run=True, health=self._health())
        assert n == 0 and not sent and not saved

    def test_push_failure_keeps_retry_next_round(self, monkeypatch):
        monkeypatch.setattr(rtp, "_send_alert_item",
                            lambda cfg, t, c: {"code": 500, "msg": "err"})
        saved = {}
        monkeypatch.setattr(rtp, "save_source_health",
                            lambda health, path=None: saved.update(health))
        health = self._health()
        n = rtp.push_source_health_alerts({}, health=health)
        assert n == 0
        assert saved == {}, "推送失败不得回写 last_alert（下轮重试）"
        assert health["财联社电报"]["last_alert"] == ""

    def test_run_once_triggers_source_health_alert(self, monkeypatch, tmp_path):
        """run_once 主循环集成：单源连续空轮 → 推送一条告警"""
        health = self._health()
        monkeypatch.setattr(rtp, "load_source_health", lambda *a, **k: health)
        sent = []
        state_path = _setup_run_round(monkeypatch, tmp_path, news_list=[])
        monkeypatch.setattr(rtp, "_send_alert_item",
                            lambda cfg, t, c: sent.append((t, c)) or {"code": 200})
        rtp.run_once(dry_run=False)
        assert len(sent) == 1
        assert sent[0][0] == "源健康告警"
        assert "财联社电报" in sent[0][1]


