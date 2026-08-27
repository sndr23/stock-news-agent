# -*- coding: utf-8 -*-
"""创业板择时 · 端到端推送链路测试（E2E）

背景：三代生产事故（--push --shadow 短路致云端从不推送；推送后 update_shadow_history
KeyError 致状态不写/影子不积累；ERP 语义反转）全部逃过了纯单测——单测只测纯函数，
从不走 main() 的完整推送链路。本文件用 mock 数据层（无网络）驱动 main() 全流程，
把"推送→状态写→影子记录→去重"这条链路关进测试门禁。

覆盖场景：
  1. --push 首次：推送 + 状态写（last_date/position/pending）+ 影子记录
  2. --push 同日重复：last_date 去重跳过（不重复推送）
  3. --push 推送失败：状态不更新（明日重跑，防漏推）
  4. --push --shadow：推送正常（短路回归，曾致云端从不推送）
  5. --shadow 纯报告：打印多期影子 IC（不推送不写状态）
"""
import datetime as _dt
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

pytestmark = pytest.mark.unit

import pandas as pd

import run_chinext_timing as rct


class _FakeDT(_dt.datetime):
    """固定时钟：2026-08-24（周一）14:45。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 24, 14, 45)


def _make_df(n: int = 320, end: str = "2026-08-21") -> pd.DataFrame:
    """399006 合成日线（收盘/成交额/高低），末根为 08-21（周五，完整日）。"""
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    closes = [100.0 * (1 + 0.001 * i) for i in range(n)]
    return pd.DataFrame({
        "close": closes,
        "amount": [1e8 + i * 1e6 for i in range(n)],
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
    }, index=dates)


def _patch_all(monkeypatch, push_ok: bool = True):
    """把 main() 依赖的 数据层/推送/状态 全部替换为可控替身。"""
    monkeypatch.setattr(rct, "datetime", _FakeDT)
    monkeypatch.setattr(rct, "load_index_sina", lambda *a, **k: _make_df())
    monkeypatch.setattr(rct, "load_index_primary", lambda *a, **k: _make_df())
    monkeypatch.setattr(rct, "load_index_daily_full", lambda *a, **k: _make_df())
    monkeypatch.setattr(rct, "get_quotes", lambda *a, **k: {})
    monkeypatch.setattr(rct, "load_stock_sina", lambda *a, **k: None)
    monkeypatch.setattr(rct, "load_stock_primary", lambda *a, **k: None)
    monkeypatch.setattr(rct.nl, "load_factor_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_citic_pos_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_realtime_state", lambda: {})
    monkeypatch.setattr(rct.ovs, "load_overseas", lambda *a, **k: {})
    monkeypatch.setattr(rct, "_load_erp_basis", lambda *a, **k: None)

    sent = []
    monkeypatch.setattr(
        rct, "push_report",
        lambda content, title: (sent.append(title), True)[1] if push_ok else
        (sent.append(title), False)[1])

    states = []
    monkeypatch.setattr(rct, "save_state", lambda s: states.append(dict(s)))
    # 2026-08-24 修复：漏 mock load_state → 场景1/3/4 读真实 Gist state，
    # 真实 last_date 撞上固定时钟（2026-08-24）→ 去重跳过 → 门禁失败 → 云端不推送。
    # 默认空 state（场景2/5 各自再覆盖），与数据层 mock 保持一致，隔离真实状态。
    monkeypatch.setattr(rct, "load_state", lambda: {})
    return sent, states


def _run(argv):
    old = sys.argv
    sys.argv = ["run_chinext_timing.py"] + argv
    try:
        rct.main()
    except SystemExit:
        pass
    finally:
        sys.argv = old


def test_e2e_push_first_time(monkeypatch):
    """场景1：--push 首次 → 推送 + 状态写（含影子记录）+ 档位推进。"""
    sent, states = _patch_all(monkeypatch)
    _run(["--push"])
    assert len(sent) == 1, "应推送一次"
    assert states, "应写状态"
    st = states[-1]
    assert st["last_date"] == "2026-08-24"
    assert "position" in st and "pending" in st
    hist = st.get("history") or []
    assert hist, "影子记录应写入"
    h = hist[-1]
    assert h["date"] == "2026-08-24"
    assert "raw" in h and h["raw"] == {"basis_min_ap": None, "main_net": None,
                                       "down_pct": None, "pcr": None}


def test_e2e_push_same_day_dedup(monkeypatch):
    """场景2：--push 同日重复 → last_date 去重跳过，不重复推送。"""
    sent, states = _patch_all(monkeypatch)
    monkeypatch.setattr(rct, "load_state",
                        lambda: {"last_date": "2026-08-24", "position": 0.6})
    _run(["--push"])
    assert sent == [], "同日已推送过，应去重跳过"
    assert states == [], "不应再写状态"


def test_e2e_push_failure_keeps_state(monkeypatch):
    """场景3：推送失败 → 状态不更新（last_date 不写，明日重跑）。"""
    sent, states = _patch_all(monkeypatch, push_ok=False)
    _run(["--push"])
    assert sent, "应尝试推送"
    assert states == [], "推送失败不得写状态（否则会误记 last_date 导致漏推）"


def test_e2e_push_with_shadow_flag(monkeypatch):
    """场景4：--push --shadow（云端 workflow 实际命令）→ 推送正常。

    回归：曾因 `if args.shadow: return` 短路，云端从不推送/状态不写/影子不积累。
    """
    sent, states = _patch_all(monkeypatch)
    _run(["--push", "--shadow"])
    assert len(sent) == 1, "--push --shadow 必须推送"
    assert states, "--push --shadow 必须写状态"
    assert states[-1]["last_date"] == "2026-08-24"


def test_e2e_shadow_report_only(monkeypatch):
    """场景5：--shadow 纯报告 → 不推送不写状态（影子 IC 报告模式）。"""
    sent, states = _patch_all(monkeypatch)
    monkeypatch.setattr(rct, "load_state",
                        lambda: {"history": [{"date": "2026-08-21", "core": 0.5,
                                              "next_ret": 0.01}]})
    _run(["--shadow"])
    assert sent == [], "纯 shadow 不推送"
    assert states == [], "纯 shadow 不写状态"
