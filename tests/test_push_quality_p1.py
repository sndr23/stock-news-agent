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
