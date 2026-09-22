"""Verify pending retry-cap cleanup runs in the periodic save path (not just overflow loop)."""
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import scripts.real_time_push as rtp


def _now_str():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _make_pending(start, n, retry, hit):
    now = _now_str()
    return {
        f"fp{start + i}": {
            "t": now,
            "retry": retry,
            "title": f"title{start + i}",
            "_hit_signal": hit,
            "payload": {"title": f"title{start + i}", "content": "", "source": "x", "published_at": now},
        }
        for i in range(n)
    }


def test_pending_retry_cap_in_cleanup_path(tmp_path, monkeypatch):
    pending = {}
    pending.update(_make_pending(0, 3, 5, hit=False))   # normal >= 3 → abandon
    pending.update(_make_pending(3, 2, 5, hit=True))    # high-signal < 10 → keep
    pending.update(_make_pending(5, 2, 12, hit=True))   # high-signal >= 10 → abandon

    state = {
        "version": 2,
        "seen": {},
        "pending": pending,
        "pushed_events": [],
        "candidate_events": [],
        "watch_announce": [],
        "backtest_events": [],
    }

    monkeypatch.setattr(rtp, "get_gist_config", lambda: (None, None))
    monkeypatch.setattr(rtp, "STATE_WINDOW_HOURS", 720)
    monkeypatch.setattr(rtp, "_state_path", lambda: tmp_path / "test_state.json")

    rtp.save_state(state)

    abandoned = [v for v in state["seen"].values() if "[溢出放弃]" in v.get("title", "")]
    assert len(state["pending"]) == 2, f"expected 2 pending, got {len(state['pending'])}"
    assert len(abandoned) == 5, f"expected 5 abandoned, got {len(abandoned)}"
