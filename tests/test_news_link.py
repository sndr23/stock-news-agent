# -*- coding: utf-8 -*-
"""资讯<->策略三层协同桥（src/strategy/news_link.py）单元测试"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import news_link as nl  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用


def _event(title, dir_, stocks=(), sectors=(), entities=(), t="2026-08-21 10:00:00"):
    return {"title_norm": title, "dir": dir_, "stocks": list(stocks),
            "entities": list(entities), "sectors": list(sectors), "t": t}


# ---------- L1：持仓相关资讯匹配 ----------
def test_match_events_by_code():
    e = _event("招商银行三季报超预期", "bullish", stocks=["招商银行"])
    hits = nl.match_events_for_code([e], "600036", "招商银行")
    assert len(hits) == 1
    assert hits[0]["matched"] == "招商银行"


def test_match_events_by_code_number():
    e = _event("600036 获主力净流入居首", "bullish", entities=["600036"])
    hits = nl.match_events_for_code([e], "600036", "招商银行")
    assert len(hits) == 1


def test_match_events_by_name_substring():
    e = _event("贵州茅台创两月新高 北向加仓", "mildly_bullish", stocks=["贵州茅台"])
    hits = nl.match_events_for_code([e], "600519", "贵州茅台")
    assert len(hits) == 1


def test_match_no_hit_returns_empty():
    e = _event("宁德时代发布新品电池", "bullish", stocks=["宁德时代"])
    assert nl.match_events_for_code([e], "600036", "招商银行") == []


def test_related_news_for_holdings_agg():
    events = [
        _event("茅台提价超预期", "bullish", stocks=["贵州茅台"]),
        _event("茅台或上调出厂价", "neutral", entities=["贵州茅台"]),
        _event("招行获批金融债", "neutral", stocks=["招商银行"]),
        _event("白酒板块整体强势", "mildly_bullish", sectors=["白酒"]),
    ]
    holdings = {"600519": 0.02, "600036": 0.01}
    names = {"600519": "贵州茅台", "600036": "招商银行"}
    out = nl.related_news_for_holdings(events, holdings, names)
    assert "600519" in out and "600036" in out
    assert len(out["600519"]) == 2  # 两条茅台事件（同主体聚合）
    assert u"茅台提价超预期" == out["600519"][0].get("title_norm")


# ---------- L2：事件 -> alpha 修正 ----------
def test_alpha_correction_strong_only():
    events = [
        _event("茅台风波调查", "bearish", stocks=["贵州茅台"]),
        _event("招行零售回暖", "mildly_bullish", stocks=["招商银行"]),
    ]
    corr = nl.event_alpha_correction(events, ["600519", "600036"],
                                     {"600519": "贵州茅台", "600036": "招商银行"})
    assert "600519" in corr
    assert corr["600519"] == -1.0  # 强利空
    assert "600036" not in corr  # 偏多默认不计（strong_only=True）


def test_alpha_correction_include_mild():
    events = [_event("招行零售回暖", "mildly_bullish", stocks=["招商银行"])]
    corr = nl.event_alpha_correction(events, ["600036"],
                                     {"600036": "招商银行"}, strong_only=False)
    assert "600036" in corr
    assert corr["600036"] == 0.5


def test_apply_alpha_correction_scales():
    alpha = {"600519": 1.0, "600036": 2.0}
    corr = {"600519": -0.5}
    out = nl.apply_alpha_correction(alpha, corr, scale=1.0)
    assert out["600519"] == 0.5
    assert out["600036"] == 2.0


def test_apply_alpha_correction_sigma():
    # alpha_sigma 作为单日截面 std 传入，correction 已乘 sigma，叠加后即温调
    alpha = {"A": 0.0}
    corr = {"A": 0.3}
    assert nl.apply_alpha_correction(alpha, corr)["A"] == pytest.approx(0.3)


# ---------- L3：宏观 overlay ----------
def test_macro_exposure_normal():
    st = {"snapshot": {"basis": {"IC": {"annual_pct": 1.2}}}}
    m = nl.macro_exposure(st, base_exposure=1.0)
    assert m["factor"] == 1.0
    assert "正常" in "".join(m["reasons"])


def test_macro_exposure_deep_short_basis():
    st = {"snapshot": {"basis": {"IC": {"annual_pct": -6.0}}}}
    m = nl.macro_exposure(st, base_exposure=1.0)
    assert m["factor"] <= 0.95
    assert m["exposure"] < 1.0


def test_macro_exposure_fund_outflow():
    st = {"snapshot": {"flows": {"main_net_yi": -120.0}}}
    m = nl.macro_exposure(st)
    assert m["factor"] <= 0.92


def test_macro_exposure_risk_state():
    st = {"snapshot": {"risk_state": "risk_off"}}
    m = nl.macro_exposure(st)
    assert m["factor"] <= 0.92


def test_macro_exposure_absent_snapshot():
    assert nl.macro_exposure({})["factor"] == 1.0


# ---------- L2 反向：watchlist 合并 ----------
def test_merge_watchlist_no_dup():
    wl = {"stocks": [{"name": "招商银行", "code": "600036"}]}
    holdings = {"600036": 0.02, "600519": 0.01}
    names = {"600036": "招商银行", "600519": "贵州茅台"}
    m = nl.merge_watchlist_holdings(wl, holdings, names)
    # 600036 已在，不重复；新增 600519
    codes = [s.get("code") for s in m["stocks"] if isinstance(s, dict)]
    assert codes.count("600036") == 1
    assert "600519" in codes


def test_watchlist_holdings_state_shape():
    entries = nl.watchlist_holdings_state({"600036": 0.02}, {"600036": "招商银行"})
    assert entries[0] == {"name": "招商银行", "code": "600036", "source": "strategy"}


# ---------- 方向标签 ----------
def test_format_event_line():
    e = _event("茅台提价超预期", "bullish", stocks=["贵州茅台"])
    line = nl.format_event_line(e)
    assert "利多" in line
    assert "茅台提价超预期" in line