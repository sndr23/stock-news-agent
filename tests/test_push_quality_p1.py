"""推送质量 P1 回归测试（Q-01 ~ Q-04）。"""

import importlib.util
import json
from datetime import datetime
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
