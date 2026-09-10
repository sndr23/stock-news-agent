# -*- coding: utf-8 -*-
"""创业板择时 P0-1 硬风控 fail-open 收口测试（缺失显式化 + 保守封顶 + 降级告警）。

对应审计 docs/WB择时优化分析_deepseek_20260910.md P0-1：
盘中行情/外盘/估值PE 任一缺失时，硬风控此前默认"没有风险"（fail-open）——
0.0 被当成真实平盘送入 `defensive_state`，断流当天可能照样输出高仓位且推送
看不出异常。改动后：
  1. `gather_context` 缺失显式化：盘中/外盘/PE 失败置 None（不再用 0.0 冒充）；
  2. `score_all` fail-safe：任一输入缺失 → `min(cap, 0.6)` + `*_missing` 标签；
  3. `render_report` 降级告警：缺哪个风控输入、已保守封顶一眼可见；
  4. 正常路径零变化：全数据源正常时输出与改动前完全一致（金标准）。
"""
import datetime as _dt
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

pytestmark = pytest.mark.unit

import pandas as pd  # noqa: E402

import run_chinext_timing as rct  # noqa: E402
from src.strategy import chinext_factors as cf  # noqa: E402


# ---------------- 合成数据 / 上下文 ----------------

def _closes(n: int = 320, r: float = 0.001):
    """单调上涨收盘序列：defensive_state 的 dd60 不触发（cap 基线 = 1.0）。"""
    return [100.0 * (1 + r) ** i for i in range(n)]


def _ctx(**over) -> dict:
    """构造 score_all 所需的最小上下文（默认全数据源正常）。"""
    closes = _closes()
    base = {
        "closes": closes,
        "amounts": [1e8] * len(closes),
        "highs": [c * 1.01 for c in closes],
        "lows": [c * 0.99 for c in closes],
        "snapshot": {},
        "events": [],
        "snapshot_stale": False,
        "intraday": 1.0,                    # 盘中行情存在（真实值）
        "overseas_drop": 0.0,               # 外盘存在（真实 0.0，非缺失）
        "erp_pctile": [0.5] * len(closes),  # 估值PE存在且非极贵
    }
    base.update(over)
    return base


def _neutral_chan(monkeypatch) -> None:
    """屏蔽缠论，避免合成序列偶然顶背驰污染封顶断言。"""
    monkeypatch.setattr(
        rct, "_chan_signal",
        lambda ctx: {"score": 0.0, "bustop": False, "bi_dir": "-", "zone": "-",
                     "last_signal": "-", "detail": "缠论:中性"})


def _safe_res(cap: float, triggers, bustop: bool = False) -> dict:
    """构造 render_report 所需的完整 res。"""
    return {
        "score": 0.0,
        "core": {"score": 0.0, "signals": {
            "trend_ma20_60": 0.0, "trend_momentum_60": 0.0,
            "volprice_quadrant": 0.0, "volprice_amihud": 0.0,
            "vol_regime": 0.0, "vol_term": 0.0, "value_erp": 0.0,
            "pullback_52w": 0.0, "dd60": 0.0}},
        "mods": {"basis": 0.0, "flow": 0.0, "mood": 0.0, "news": 0.0,
                 "chan": {"score": 0.0, "bustop": bustop, "bi_dir": "-",
                          "zone": "-", "last_signal": "-", "detail": "缠论:中性"},
                 "stock": {"score": 0.0, "detail": "跳过"}},
        "caps": {"cap": cap, "triggers": list(triggers or [])},
    }


# ---------------- 2. score_all：缺失 → 保守封顶 + *_missing ----------------

def test_score_all_normal_path_matches_defensive_state(monkeypatch):
    """金标准：全数据源正常 → 与改动前完全一致（无缺失封顶、cap 来自纯硬风控）。"""
    _neutral_chan(monkeypatch)
    ctx = _ctx()
    res = rct.score_all(ctx)
    baseline = cf.defensive_state(
        ctx["closes"], None,
        {"risk_off": False, "basis_min_ap": None,
         "intraday_pct": 1.0, "overseas_drop": 0.0})
    assert res["caps"]["cap"] == baseline["cap"]
    assert res["caps"]["triggers"] == baseline["triggers"]
    assert not any(str(t).endswith("_missing") for t in res["caps"]["triggers"])


@pytest.mark.parametrize("over,tag", [
    ({"intraday": None}, "intraday_missing"),
    ({"overseas_drop": None}, "overseas_missing"),
    ({"erp_pctile": None}, "erp_missing"),
])
def test_score_all_missing_input_caps_conservatively(monkeypatch, over, tag):
    """任一风控输入缺失 → min(cap, 0.6) 且 triggers 写明缺失项（不再 fail-open）。"""
    _neutral_chan(monkeypatch)
    res = rct.score_all(_ctx(**over))
    assert tag in res["caps"]["triggers"]
    # 合成上涨序列基线 cap=1.0，fail-safe 后必须压到保守封顶
    assert res["caps"]["cap"] == rct.MISSING_INPUT_CAP


def test_score_all_erp_last_value_none_counts_as_missing(monkeypatch):
    """PE 序列存在但末期值缺失（数据未覆盖最新交易日）同样按缺失保守封顶。"""
    _neutral_chan(monkeypatch)
    res = rct.score_all(_ctx(erp_pctile=[0.5, 0.5, None]))
    assert "erp_missing" in res["caps"]["triggers"]
    assert res["caps"]["cap"] == rct.MISSING_INPUT_CAP


def test_score_all_all_missing_lists_every_tag(monkeypatch):
    """三源同时缺失 → 三个标签齐全且仍只封顶一次（不叠加）。"""
    _neutral_chan(monkeypatch)
    res = rct.score_all(_ctx(intraday=None, overseas_drop=None, erp_pctile=None))
    trig = res["caps"]["triggers"]
    for tag in ("intraday_missing", "overseas_missing", "erp_missing"):
        assert tag in trig
    assert res["caps"]["cap"] == rct.MISSING_INPUT_CAP


def test_score_all_real_zero_overseas_is_not_missing(monkeypatch):
    """外盘真实 0.0（数据存在但隔夜持平）不得被误判为缺失。"""
    _neutral_chan(monkeypatch)
    res = rct.score_all(_ctx(overseas_drop=0.0))
    assert "overseas_missing" not in res["caps"]["triggers"]


def test_missing_input_end_to_end_cap_then_warning(monkeypatch):
    """缺失链路端到端：score_all 保守封顶 → render_report 输出降级告警。"""
    _neutral_chan(monkeypatch)
    ctx = _ctx(intraday=None, overseas_drop=None, erp_pctile=None)
    res = rct.score_all(ctx)
    assert res["caps"]["cap"] <= rct.MISSING_INPUT_CAP

    ctx_out = dict(ctx)
    ctx_out.update({"history_bars": len(ctx["closes"]),
                    "history_last_date": "2026-09-09"})
    dec = {"position": res["caps"]["cap"], "changed": False,
           "direction": "hold", "note": []}
    txt = rct.render_report("2026-09-10", res, ctx_out, dec, prev_pos=0.0)
    assert "⚠ 降级告警" in txt
    assert "intraday_missing" in txt


# ---------------- 1. gather_context：缺失显式化 None ----------------

class _FakeDT(_dt.datetime):
    """固定时钟：2026-08-24（周一）14:45。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 24, 14, 45)


def _gather_df(last_date: str = "2026-08-21", n: int = 70):
    dates = pd.bdate_range(end=pd.Timestamp(last_date), periods=n)
    closes = [100.0 * (1 + 0.001 * i) for i in range(n)]
    return pd.DataFrame({
        "close": closes,
        "amount": [1e8 + i * 1e6 for i in range(n)],
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
    }, index=dates)


def _patch_gather(monkeypatch) -> None:
    monkeypatch.setattr(rct, "datetime", _FakeDT)
    monkeypatch.setattr(rct.nl, "load_factor_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_citic_pos_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_realtime_state", lambda: {})
    monkeypatch.setattr(rct, "load_stock_sina", lambda *a, **k: None)


def test_gather_context_failed_sources_are_none_not_zero(monkeypatch):
    """盘中/外盘/PE 全部失败 → 显式 None（不再以 0.0 冒充真实值）。"""
    _patch_gather(monkeypatch)

    def _boom(*a, **k):
        raise OSError("quote down")

    monkeypatch.setattr(rct, "get_quotes", _boom)
    monkeypatch.setattr(rct.ovs, "load_overseas", lambda *a, **k: {})
    monkeypatch.setattr(rct, "_load_erp_basis", lambda *a, **k: None)

    ctx = rct.gather_context(_gather_df())

    assert ctx["intraday"] is None
    assert ctx["overseas_drop"] is None
    assert ctx["erp_pctile"] is None


def test_gather_context_overseas_all_series_empty_is_none(monkeypatch):
    """外盘降级链全失败（三序列皆空）→ overseas_drop=None，不能伪装 0.0。"""
    _patch_gather(monkeypatch)
    monkeypatch.setattr(rct, "get_quotes", lambda *a, **k: {"399006": 0.5})
    monkeypatch.setattr(rct.ovs, "load_overseas",
                        lambda *a, **k: {"sox": {}, "ndx": {}, "inx": {}})
    monkeypatch.setattr(rct, "_load_erp_basis", lambda *a, **k: [0.5] * 70)

    ctx = rct.gather_context(_gather_df())

    assert ctx["overseas_drop"] is None
    assert ctx["intraday"] == 0.5


def test_gather_context_normal_path_keeps_real_values(monkeypatch):
    """全正常 → 保留真实数值（与改动前一致）。"""
    _patch_gather(monkeypatch)
    monkeypatch.setattr(rct, "get_quotes", lambda *a, **k: {"399006": -1.23})
    monkeypatch.setattr(
        rct.ovs, "load_overseas",
        lambda *a, **k: {"sox": {"2026-08-20": 100.0, "2026-08-21": 98.0}})
    monkeypatch.setattr(rct.ovs, "overnight_drop", lambda ov, now: -0.02)
    monkeypatch.setattr(rct, "_load_erp_basis", lambda *a, **k: [0.5] * 70)

    ctx = rct.gather_context(_gather_df())

    assert ctx["intraday"] == -1.23
    assert ctx["overseas_drop"] == -0.02
    assert ctx["erp_pctile"][-1] == 0.5


# ---------------- 3. render_report：降级告警 ----------------

def test_render_report_warns_on_missing_risk_inputs():
    """缺失风控输入 → 报告含降级告警 + 盘中行披露缺失，且不打印假 +0.00%。"""
    res = _safe_res(cap=rct.MISSING_INPUT_CAP,
                    triggers=["intraday_missing", "erp_missing"])
    ctx = {"intraday": None, "overseas_drop": None,
           "history_bars": 3000, "history_last_date": "2026-09-09"}
    dec = {"position": 0.6, "changed": False, "direction": "hold", "note": []}

    txt = rct.render_report("2026-09-10", res, ctx, dec, prev_pos=0.6)

    assert "⚠ 降级告警" in txt
    assert "盘中行情" in txt and "估值PE" in txt
    assert "intraday_missing" in txt
    assert "保守封顶" in txt and "60%" in txt
    assert "创业板指 行情缺失" in txt
    assert "创业板指 +0.00%" not in txt


def test_render_report_normal_path_has_no_missing_warning():
    """全正常 → 无降级告警、盘中行照常打印真实涨跌幅（行为零变化）。"""
    res = _safe_res(cap=1.0, triggers=[])
    ctx = {"intraday": 1.7, "overseas_drop": 0.0,
           "history_bars": 3000, "history_last_date": "2026-09-09"}
    dec = {"position": 0.0, "changed": False, "direction": "hold", "note": []}

    txt = rct.render_report("2026-09-10", res, ctx, dec, prev_pos=0.0)

    assert "⚠ 降级告警" not in txt
    assert "行情缺失" not in txt
    assert "创业板指 +1.70%" in txt
