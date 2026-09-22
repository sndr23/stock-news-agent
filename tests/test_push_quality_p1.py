"""推送质量 P1 回归测试（Q-01 ~ Q-04）。"""

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


def _load_rtp():
    spec = importlib.util.spec_from_file_location(
        "real_time_push_push_quality_p1", _PROJECT_ROOT / "scripts" / "real_time_push.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rtp = _load_rtp()


def test_save_state_uses_shorter_window_only_for_unpushed_seen(monkeypatch, tmp_path):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2026, 9, 22, 12, 0, 0)
            return current.replace(tzinfo=tz) if tz is not None else current

    monkeypatch.setattr(rtp, "datetime", FixedDateTime)
    monkeypatch.setenv("GIST_TOKEN", "")
    monkeypatch.setenv("GIST_ID", "")
    monkeypatch.delenv("CI", raising=False)
    state_path = tmp_path / "real_time_state.json"
    monkeypatch.setattr(rtp, "_state_path", lambda: state_path)

    state = {
        "seen": {
            "unpushed-fresh": {"t": "2026-09-21 13:00:00", "pushed": False},
            "unpushed-expired": {"t": "2026-09-21 11:59:00", "pushed": False},
            "pushed-fresh": {"t": "2026-09-20 13:00:00", "pushed": True},
            "pushed-expired": {"t": "2026-09-20 11:59:00", "pushed": True},
        },
        "pending": {},
        "pushed_events": [],
        "candidate_events": [],
    }

    rtp.save_state(state)

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(saved["seen"]) == {"unpushed-fresh", "pushed-fresh"}


def _snapshot_sig(title, *, entities=(), sectors=(), numbers=(), scope="sector"):
    return {
        "stocks": [],
        "entities": list(entities),
        "events": [],
        "numbers": list(numbers),
        "sectors": list(sectors),
        "scope": scope,
        "title_norm": title,
    }


def test_replay_d1_security_alert_reports_are_the_same_event():
    first = _snapshot_sig(
        "美驻中东多国使馆发布安全警示",
        entities=["美国"],
        sectors=["军工", "原油", "黄金"],
        scope="market",
    )
    second = _snapshot_sig(
        "军事冲突或迅速升级美国针对中东地区发布新的安全警报",
        entities=["也门胡塞武装", "沙特阿拉伯", "美国"],
        sectors=["大盘"],
        scope="market",
    )

    assert rtp._is_same_event(first, second)


def test_replay_d2_cxmt_mass_production_reports_are_the_same_event():
    first = _snapshot_sig(
        "长鑫科技宣布第五代技术平台正式量产",
        entities=["长鑫科技"],
        sectors=["半导体", "存储芯片"],
    )
    second = _snapshot_sig(
        "冲击七连红长鑫第五代DRAM平台正式量产科创芯片设计ETF国联安588780跟踪指数涨超1%",
        entities=["长鑫存储"],
        numbers=["万:849.2"],
        sectors=["半导体", "存储"],
    )

    assert rtp._is_same_event(first, second)


def _saturated_storage_state():
    now = datetime.now(rtp.BJT)
    pushed_events = [
        {
            "stocks": [],
            "entities": [],
            "events": [],
            "numbers": [],
            "sectors": ["存储"],
            "scope": "sector",
            "title_norm": f"存储题材既有报道{i}",
            "dir": "bullish",
            "t": (now - timedelta(hours=1, minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for i in range(5)
    ]
    return {
        "version": 2,
        "seen": {},
        "pending": {},
        "pushed_events": pushed_events,
        "candidate_events": [],
        "watch_announce": [],
        "backtest_events": [],
    }


def _run_replay_round(monkeypatch, state, news_items, judge):
    empty_tool = type("T", (), {"func": staticmethod(lambda: [])})()
    news_tool = type("T", (), {"func": staticmethod(lambda: list(news_items))})()
    monkeypatch.setattr(rtp, "get_stock_news", news_tool)
    monkeypatch.setattr(rtp, "get_market_signals", empty_tool)
    monkeypatch.setattr(rtp, "get_announcements", empty_tool)
    monkeypatch.setattr(rtp, "load_state", lambda: state)
    monkeypatch.setattr(rtp, "save_state", lambda value: None)
    monkeypatch.setattr(rtp, "dedup_news_3layer", lambda values: list(values))
    monkeypatch.setattr(rtp, "push_source_health_alerts", lambda *args, **kwargs: None)
    monkeypatch.setattr(rtp, "_load_leader_watchlist", lambda: set())
    monkeypatch.setattr(rtp, "_load_factor_state",
                        lambda: {"risk_state": "neutral", "snapshot": {}})
    monkeypatch.setattr(rtp, "_prefilter", lambda news: (0.9, True))
    monkeypatch.setattr(
        rtp,
        "_llm_judge",
        lambda items, **kwargs: [dict(judge) for _ in items],
    )
    monkeypatch.setattr(rtp, "_send_alert_item", lambda *args, **kwargs: {"code": 200})
    monkeypatch.setenv("PUSHPLUS_TOKEN", "test-token")
    monkeypatch.setenv("WECOM_WEBHOOK", "")
    monkeypatch.delenv("CI", raising=False)
    return rtp.run_once(dry_run=False)


def _hbm_news(title):
    return {
        "title": title,
        "content": title,
        "source": "金十数据",
        "published_at": "2026-09-21 22:06:31",
    }


def _hbm_judge():
    return {
        "push": True,
        "score": 8,
        "direction": "bullish",
        "scope": "sector",
        "sectors": ["HBM", "存储"],
        "entities": ["三星"],
        "is_leader_stock": False,
        "reason": "产能硬事实",
    }


def test_replay_hbm_capacity_fact_breaks_saturated_topic_once(monkeypatch):
    state = _saturated_storage_state()
    title = "消息称三星HBM4产能明年或翻倍"

    _run_replay_round(monkeypatch, state, [_hbm_news(title)], _hbm_judge())

    assert len(state["pushed_events"]) == 6
    pushed = state["pushed_events"][-1]
    assert pushed["title"] == title
    assert pushed["topic_exemption"] is True


def test_replay_hbm_same_capacity_chain_cannot_reuse_exemption(monkeypatch):
    state = _saturated_storage_state()
    first = _hbm_news("消息称三星HBM4产能明年或翻倍")
    second = _hbm_news("三星存储业务产能预计未来扩张")

    # LLM 本轮未抽出实体时仍应按“存储 + 产能扩张”事件链只豁免一次，
    # 同时避免现有同实体标题合并规则把两条回放样本提前合并。
    judge = {**_hbm_judge(), "entities": []}
    _run_replay_round(monkeypatch, state, [first, second], judge)

    assert len(state["pushed_events"]) == 6
    assert state["pushed_events"][-1]["title"] == first["title"]
    second_fp = rtp._news_fingerprint(second)
    assert "同题材已饱和" in state["seen"][second_fp]["title"]


def test_high_signal_overflow_has_separate_retry_cap(monkeypatch):
    target = {
        "title": "主体40重大立案调查",
        "content": "重大立案调查",
        "source": "财联社",
        "published_at": "2026-09-22 10:00:00",
    }
    fillers = [
        {
            "title": f"主体{i}重大立案调查",
            "content": "重大立案调查",
            "source": "财联社",
            "published_at": "2026-09-22 10:00:00",
        }
        for i in range(40)
    ]
    target_fp = rtp._news_fingerprint(target)
    state = {
        "version": 2,
        "seen": {},
        "pending": {
            target_fp: {
                "t": "2026-09-22 09:00:00",
                "retry": 9,
                "title": target["title"],
                "payload": dict(target),
            }
        },
        "pushed_events": [],
        "candidate_events": [],
        "watch_announce": [],
        "backtest_events": [],
    }

    _run_replay_round(monkeypatch, state, fillers + [target], _hbm_judge())

    assert rtp.MAX_PENDING_RETRY_HIGH_SIGNAL == 10
    assert target_fp not in state["pending"]
    assert "溢出放弃" in state["seen"][target_fp]["title"]
