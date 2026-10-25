"""推送质量 P1 回归测试：P1-1 相关性加权排序 + P1-2 时段静默（2026-10-26）。

P1-1：_relevance_bonus 只影响候选溢出排序（不放宽预筛入口），
      _candidate_sort_key 四层：高信号 > 宏观数据发布 > 相关性 > 预筛分。
P1-2：静默窗（周末全天 / 每日 21:30–08:00 BJT）只放行持仓公告、宏观数据、
      硬经营事件、高分利空；其余写 seen 不进 pending。
"""

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


def _load_rtp():
    spec = importlib.util.spec_from_file_location(
        "real_time_push_p1_relevance_quiet", _PROJECT_ROOT / "scripts" / "real_time_push.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rtp = _load_rtp()

WATCH = {"中际旭创", "新易盛", "生益科技"}


# ============================================================
# P1-1 相关性加权
# ============================================================

def test_relevance_bonus_watchlist_hit():
    assert rtp._relevance_bonus("中际旭创获40亿元订单", "", WATCH) == 0.25
    # 多只持仓股同时命中只加一次
    assert rtp._relevance_bonus("中际旭创与新易盛同日发布公告", "", WATCH) == 0.25


def test_relevance_bonus_chain_keywords_capped():
    assert rtp._relevance_bonus("覆铜板价格上调", "", set()) == 0.05
    assert rtp._relevance_bonus("覆铜板与铜箔同步涨价", "", set()) == 0.10
    # 6 个产业链词命中，封顶 0.15
    text = "CPO 光模块 覆铜板 HBM DRAM 存储 同涨"
    assert rtp._relevance_bonus(text, "", set()) == 0.15


def test_relevance_bonus_total_capped():
    # 持仓 +0.25 与产业链封顶 +0.15 叠加 = 0.40（上限）
    assert rtp._relevance_bonus(
        "中际旭创 CPO 光模块 光芯片 覆铜板 HBM", "", WATCH
    ) == 0.40


def test_relevance_bonus_no_hit_is_zero():
    assert rtp._relevance_bonus("油价大涨金价银价大跌", "海外市场异动", WATCH) == 0.0
    assert rtp._relevance_bonus("", "", WATCH) == 0.0
    # watch_names 为空/None 不抛异常
    assert rtp._relevance_bonus("中际旭创获订单", "", None) == 0.0


def test_chain_keywords_table_complete():
    """词表完整性：漏逗号会让相邻词被 Python 隐式拼接成一个词（静默失效）"""
    assert len(rtp._CHAIN_KEYWORDS) == 18
    assert "数据中心" in rtp._CHAIN_KEYWORDS
    assert "液冷" in rtp._CHAIN_KEYWORDS
    assert all(kw and kw == kw.strip() for kw in rtp._CHAIN_KEYWORDS)
    assert rtp._relevance_bonus("数据中心液冷", "", set()) == 0.10


def test_sort_key_layers_signal_macro_relevance_prefscore():
    def key(**kw):
        base = {"title": "某标题", "content": "", "_hit_signal": False,
                "_pref_score": 0.6}
        base.update(kw)
        return rtp._candidate_sort_key(base)

    signal = key(_hit_signal=True)
    macro = key(title="美国9月CPI同比增长3%", content="数据公布")
    relevant = key(_relevance=0.25)
    plain_high = key(_pref_score=0.95)
    plain_low = key(_pref_score=0.60)

    assert signal > macro > relevant > plain_high > plain_low


def test_sort_key_relevance_beats_prefscore():
    """同层（无高信号/非宏观数据）时相关性优先于预筛分"""
    chain_title = "光模块厂商订单饱满"
    geo_title = "胡塞武装对沙特发起袭击"
    hot_chain = {"title": chain_title, "content": "", "_hit_signal": False,
                 "_pref_score": 0.56,
                 "_relevance": rtp._relevance_bonus(chain_title, "", WATCH)}
    high_score_geo = {"title": geo_title, "content": "", "_hit_signal": False,
                      "_pref_score": 0.98,
                      "_relevance": rtp._relevance_bonus(geo_title, "", WATCH)}
    assert hot_chain["_relevance"] == 0.05
    assert high_score_geo["_relevance"] == 0.0
    assert rtp._candidate_sort_key(hot_chain) > rtp._candidate_sort_key(high_score_geo)


def test_overflow_prefers_watchlist_over_geopolitical():
    """B4：溢出竞争 40 个坑位——watchlist/产业链条目入选，地缘条目落选"""
    watch_title = "中际旭创：拟40亿元回购公司股份"
    geo_title = "中东局势升级：胡塞武装对沙特发起多次袭击"
    watch_item = {"title": watch_title, "content": "", "_hit_signal": False,
                  "_pref_score": 0.56,
                  "_relevance": rtp._relevance_bonus(watch_title, "", WATCH)}
    geo_item = {"title": geo_title, "content": "", "_hit_signal": False,
                "_pref_score": 0.95,
                "_relevance": rtp._relevance_bonus(geo_title, "", WATCH)}
    # 40 条产业链普通候选（预筛分中等、相关性 0.05）占满坑位
    fillers = []
    for i in range(40):
        t = f"覆铜板厂商{i}月度排产环比提升"
        fillers.append({"title": t, "content": "", "_hit_signal": False,
                        "_pref_score": 0.70,
                        "_relevance": rtp._relevance_bonus(t, "", WATCH)})

    ranked = sorted([geo_item, watch_item] + fillers,
                    key=rtp._candidate_sort_key, reverse=True)
    slots = ranked[:40]

    assert watch_item in slots
    assert geo_item not in slots
    # 地缘条目仍被 _is_macro_policy 判为宏观政策类——旧第二优先级已失效（P1-1 收窄）
    assert rtp._is_macro_policy(geo_item) is True
    assert rtp._is_macro_data_release(geo_item["title"]) is False


def test_overflow_keeps_macro_data_release_priority():
    """第二层保留 2026-08-12 修复初衷：确定性数据发布仍压过产业链候选"""
    cpi = {"title": "美国9月CPI同比增长3%", "content": "数据公布，前值2.9%",
           "_hit_signal": False, "_pref_score": 0.56, "_relevance": 0.0}
    chain = {"title": "覆铜板厂商排产环比提升", "content": "",
             "_hit_signal": False, "_pref_score": 0.90, "_relevance": 0.15}
    ranked = sorted([chain, cpi], key=rtp._candidate_sort_key, reverse=True)
    assert ranked[0] is cpi


# ============================================================
# P1-2 时段静默：窗口判定
# ============================================================

def test_quiet_hours_window_weekend_all_day():
    sat = datetime(2026, 10, 24, 10, 5)   # 周六
    sun = datetime(2026, 10, 25, 15, 0)   # 周日
    assert sat.weekday() == 5 and sun.weekday() == 6
    assert rtp._quiet_hours_window(sat) == "周末静默"
    assert rtp._quiet_hours_window(sun) == "周末静默"


@pytest.mark.parametrize("hour,minute,blocked", [
    (21, 29, False),
    (21, 30, True),   # 边界：整数小时写法 8<=h<21.5 会漏掉 21:30–22:00
    (21, 35, True),
    (22, 5, True),
    (23, 59, True),
    (0, 30, True),
    (7, 59, True),
    (8, 0, False),    # 边界：08:00 恢复
    (12, 0, False),
])
def test_quiet_hours_window_weekday_night(hour, minute, blocked):
    day = datetime(2026, 10, 26, hour, minute)   # 周一
    assert day.weekday() == 0
    assert bool(rtp._quiet_hours_window(day)) is blocked


def test_quiet_hours_env_switch(monkeypatch):
    monkeypatch.setenv("RT_QUIET_HOURS", "0")
    sat = datetime(2026, 10, 24, 10, 5)
    assert rtp._quiet_hours_enabled() is False
    assert rtp._quiet_hours_block_reason(
        {"title": "油价大涨金价银价大跌"}, {"direction": "bullish", "score": 8},
        False, now=sat) == ""
    monkeypatch.setenv("RT_QUIET_HOURS", "1")
    assert rtp._quiet_hours_enabled() is True
    assert rtp._quiet_hours_block_reason(
        {"title": "油价大涨金价银价大跌"}, {"direction": "bullish", "score": 8},
        False, now=sat) == "周末静默"


# ============================================================
# P1-2 放行条件
# ============================================================

@pytest.mark.parametrize("news,judge,watch_direct,reason", [
    ({"title": "中际旭创：关于回购公司股份的公告"}, {"direction": "neutral",
                                          "score": 3}, True, "持仓公告直通"),
    ({"title": "美国9月CPI同比增长3%", "content": "数据公布"}, {"direction": "bullish",
                                                    "score": 8}, False, "宏观数据发布"),
    ({"title": "宁德时代签订200亿元储能订单"}, {"direction": "bullish", "score": 8},
     False, "硬事件"),
    ({"title": "某公司被立案调查", "content": "重大风险"}, {"direction": "bearish",
                                                "score": 7}, False, "高分利空"),
])
def test_quiet_hours_exempt_reasons(news, judge, watch_direct, reason):
    assert rtp._quiet_hours_exempt_reason(news, judge, watch_direct) == reason


def test_quiet_hours_weak_bearish_not_exempt():
    # 利空但分数不够 7 → 静默期仍拦（避免弱利空刷屏）
    assert rtp._quiet_hours_exempt_reason(
        {"title": "某公司业绩承压"}, {"direction": "bearish", "score": 6}) == ""


# ============================================================
# B3 静默锚点（样本来自真实推送流水）
# ============================================================

@pytest.mark.parametrize("when,title,judge,expected", [
    (datetime(2026, 10, 24, 10, 5), "油价大涨金价银价大跌",
     {"direction": "bullish", "score": 8}, "周末静默"),
    (datetime(2026, 10, 25, 11, 0), "周末要闻回顾：前三季度全国财政收入同比增长5.1%",
     {"direction": "bullish", "score": 7}, ""),
    (datetime(2026, 10, 25, 16, 0), "冲突升级！胡塞武装对沙特发起多次袭击",
     {"direction": "bearish", "score": 6}, "周末静默"),
    (datetime(2026, 10, 22, 22, 5), "近一周融资资金呈现净偿还态势…",
     {"direction": "bearish", "score": 5}, "夜间静默(21:30-08:00)"),
])
def test_b3_quiet_hours_anchors(monkeypatch, when, title, judge, expected):
    monkeypatch.setenv("RT_QUIET_HOURS", "1")
    assert rtp._quiet_hours_block_reason({"title": title}, judge, False, now=when) == expected


def test_b3_watch_announce_passes_any_time(monkeypatch):
    monkeypatch.setenv("RT_QUIET_HOURS", "1")
    when = datetime(2026, 10, 24, 10, 5)   # 周六
    assert rtp._quiet_hours_block_reason(
        {"title": "中际旭创：关于回购公司股份的公告"},
        {"direction": "neutral", "score": 3}, True, now=when) == ""


# ============================================================
# 集成：run_once 在静默窗内 gate=quiet_hours，candidate_events 仍完整
# ============================================================

class _FixedDateTime(datetime):
    _now = datetime(2026, 10, 24, 10, 5)   # 周六 10:05 BJT

    @classmethod
    def now(cls, tz=None):
        return cls._now.replace(tzinfo=tz) if tz is not None else cls._now


def _empty_state():
    return {"version": 2, "seen": {}, "pending": {}, "pushed_events": [],
            "candidate_events": [], "watch_announce": [], "backtest_events": []}


def _run_round(monkeypatch, state, news_items, judge, *, quiet=True):
    empty_tool = type("T", (), {"func": staticmethod(lambda: [])})()
    news_tool = type("T", (), {"func": staticmethod(lambda: list(news_items))})()
    sent = []

    monkeypatch.setattr(rtp, "datetime", _FixedDateTime)
    monkeypatch.setenv("RT_QUIET_HOURS", "1" if quiet else "0")
    monkeypatch.setenv("PUSHPLUS_TOKEN", "test-token")
    monkeypatch.setenv("WECOM_WEBHOOK", "")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(rtp, "get_stock_news", news_tool)
    monkeypatch.setattr(rtp, "get_market_signals", empty_tool)
    monkeypatch.setattr(rtp, "get_announcements", empty_tool)
    monkeypatch.setattr(rtp, "load_state", lambda: state)
    monkeypatch.setattr(rtp, "save_state", lambda value: None)
    monkeypatch.setattr(rtp, "dedup_news_3layer", lambda values: list(values))
    monkeypatch.setattr(rtp, "push_source_health_alerts", lambda *a, **k: None)
    monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set(WATCH))
    monkeypatch.setattr(rtp, "_load_factor_state",
                        lambda: {"risk_state": "neutral", "snapshot": {}})
    monkeypatch.setattr(rtp, "_prefilter", lambda news: (0.9, True))
    monkeypatch.setattr(rtp, "_llm_judge",
                        lambda items, **kwargs: [dict(judge) for _ in items])
    monkeypatch.setattr(rtp, "_send_alert_item",
                        lambda *a, **k: (sent.append(a) or {"code": 200}))
    return rtp.run_once(dry_run=False), sent


def _soft_news(title="白酒渠道库存降至三年低位"):
    return {"title": title, "content": title, "source": "财联社",
            "published_at": "2026-10-24 09:30:00"}


def test_run_once_quiet_hours_blocks_soft_item(monkeypatch):
    state = _empty_state()
    judge = {"push": True, "score": 8, "direction": "bullish", "scope": "sector",
             "sectors": ["白酒"], "entities": [], "is_leader_stock": False,
             "reason": "渠道数据"}
    news = _soft_news()

    stats, sent = _run_round(monkeypatch, state, [news], judge)

    assert stats["pushed"] == 0
    assert sent == []                       # 未推送
    assert state["pending"] == {}           # 不进 pending（周一早晨不重复涌出）
    rec = state["seen"][rtp._news_fingerprint(news)]
    assert rec["pushed"] is False
    assert rec["title"].endswith("[静默时段不推]")
    # candidate_events 记录完整（LLM 判定结果照常落库，gate 可复核）
    assert len(state["candidate_events"]) == 1
    cand = state["candidate_events"][0]
    assert cand["gate"] == "quiet_hours"
    assert cand["dir"] == "bullish" and cand["score"] == 8
    assert cand["pushed"] is False


def test_run_once_quiet_hours_exempt_item_pushed(monkeypatch):
    state = _empty_state()
    judge = {"push": True, "score": 8, "direction": "bearish", "scope": "market",
             "sectors": ["宏观"], "entities": ["美国"], "is_leader_stock": False,
             "reason": "CPI 数据"}
    news = {"title": "美国9月CPI同比增长3%", "content": "数据公布，前值2.9%",
            "source": "金十数据", "published_at": "2026-10-24 09:30:00"}

    stats, sent = _run_round(monkeypatch, state, [news], judge)

    assert stats["pushed"] == 1
    assert len(sent) == 1
    assert state["candidate_events"][0]["gate"] == "pushed"


def test_run_once_quiet_hours_off_restores_old_behavior(monkeypatch):
    state = _empty_state()
    judge = {"push": True, "score": 8, "direction": "bullish", "scope": "sector",
             "sectors": ["白酒"], "entities": [], "is_leader_stock": False,
             "reason": "渠道数据"}

    stats, sent = _run_round(monkeypatch, state, [_soft_news()], judge, quiet=False)

    assert stats["pushed"] == 1
    assert len(sent) == 1


def test_prefilter_threshold_not_relaxed_by_relevance(monkeypatch):
    """相关性加分不影响预筛入口：预筛分不达标 + 未命中信号 → 仍不进候选"""
    state = _empty_state()
    judge = {"push": True, "score": 8, "direction": "bullish", "scope": "sector",
             "sectors": ["光模块"], "entities": [], "is_leader_stock": False,
             "reason": "x"}
    news = _soft_news("中际旭创 CPO 光模块覆铜板 HBM 排产提升")

    empty_tool = type("T", (), {"func": staticmethod(lambda: [])})()
    news_tool = type("T", (), {"func": staticmethod(lambda: [news])})()
    monkeypatch.setenv("PUSHPLUS_TOKEN", "test-token")
    monkeypatch.setenv("WECOM_WEBHOOK", "")
    monkeypatch.setenv("RT_QUIET_HOURS", "0")
    monkeypatch.setattr(rtp, "get_stock_news", news_tool)
    monkeypatch.setattr(rtp, "get_market_signals", empty_tool)
    monkeypatch.setattr(rtp, "get_announcements", empty_tool)
    monkeypatch.setattr(rtp, "load_state", lambda: state)
    monkeypatch.setattr(rtp, "save_state", lambda value: None)
    monkeypatch.setattr(rtp, "dedup_news_3layer", lambda values: list(values))
    monkeypatch.setattr(rtp, "push_source_health_alerts", lambda *a, **k: None)
    monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())
    monkeypatch.setattr(rtp, "_load_factor_state",
                        lambda: {"risk_state": "neutral", "snapshot": {}})
    monkeypatch.setattr(rtp, "_prefilter", lambda item: (0.30, False))
    monkeypatch.setattr(rtp, "_llm_judge", lambda items, **kw: [dict(judge) for _ in items])
    monkeypatch.setattr(rtp, "_send_alert_item", lambda *a, **k: {"code": 200})

    stats = rtp.run_once(dry_run=False)

    assert rtp._relevance_bonus(news["title"], news["content"], set()) == 0.15
    assert stats["prefiltered"] == 0
