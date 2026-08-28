# -*- coding: utf-8 -*-
"""资讯<->策略三层协同桥（src/strategy/news_link.py）单元测试"""
import sys
from datetime import date, datetime, timedelta, timezone
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


def test_macro_exposure_mild_discount_no_trigger():
    # annual_pct 单位为百分点：微幅贴水 -2% 不构成深度贴水，不降仓
    st = {"snapshot": {"basis": {"IC": {"annual_pct": -2.0}}}}
    m = nl.macro_exposure(st)
    assert m["factor"] == 1.0
    assert not any("贴水" in r for r in m["reasons"])


def test_macro_exposure_basis_display_units():
    # 展示口径：-12.11 百分点 → "-12.1%"，禁止二次 ×100 变 "-1211.0%"
    st = {"snapshot": {"basis": {"IC": {"annual_pct": -12.11}}}}
    m = nl.macro_exposure(st)
    joined = " ".join(m["reasons"])
    assert "IC年化贴水 -12.1%" in joined
    assert "-1211" not in joined


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


def test_macro_exposure_citic_net_short():
    # 中信全合约净加空超阈值 → 降仓
    citic = {"pos_history": [{"day": "2026-08-21",
                               "net": {"IF": -1200, "IC": -800, "IH": -500, "IM": -700,
                                       "_total": -3200}}]}
    m = nl.macro_exposure({}, citic_state=citic, today=date(2026, 8, 24))
    assert m["factor"] <= 0.9
    assert any("净加空" in r for r in m["reasons"])


def test_macro_exposure_citic_net_long():
    # 中信净加多 → 不降仓
    citic = {"pos_history": [{"day": "2026-08-20",
                               "net": {"IF": 500, "IC": 300, "_total": 800}}]}
    m = nl.macro_exposure({}, citic_state=citic, today=date(2026, 8, 21))
    assert m["factor"] == 1.0


def test_macro_exposure_no_citic_history():
    assert nl.macro_exposure({}, citic_state={})["factor"] == 1.0


def test_load_factor_state_does_not_fallback_to_local_when_gist_file_missing(
        monkeypatch, tmp_path):
    """Gist 成功但文件缺失时，不能用本地旧快照冒充云端状态。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(nl, "_FACTOR_STATE_PATH", tmp_path / "factor_state.json")
    nl._FACTOR_STATE_PATH.write_text(
        '{"snapshot": {"flows": {"main_net_yi": -120}}}', encoding="utf-8")

    class _MissingFileResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"files": {}}'

    monkeypatch.setattr(nl, "urlopen", lambda *args, **kwargs: _MissingFileResp())

    assert nl.load_factor_state() == {}


def test_load_factor_state_rejects_gist_failure_instead_of_using_local_snapshot(
        monkeypatch, tmp_path):
    """Gist 读取失败时，不能静默回退本地旧快照。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(nl, "_FACTOR_STATE_PATH", tmp_path / "factor_state.json")
    nl._FACTOR_STATE_PATH.write_text(
        '{"snapshot": {"flows": {"main_net_yi": -120}}}', encoding="utf-8")
    monkeypatch.setattr(nl, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("network down")))

    with pytest.raises(RuntimeError, match="Gist 状态文件 factor_state.json"):
        nl.load_factor_state()


def test_load_realtime_state_does_not_fallback_to_local_when_gist_file_missing(
        monkeypatch, tmp_path):
    """Gist 成功但资讯文件缺失时，不能使用本地旧事件。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(nl, "_REALTIME_STATE_PATH", tmp_path / "real_time_state.json")
    nl._REALTIME_STATE_PATH.write_text(
        '{"pushed_events": [{"t": "2026-08-28 10:00:00"}]}', encoding="utf-8")

    class _MissingFileResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"files": {}}'

    monkeypatch.setattr(nl, "urlopen", lambda *args, **kwargs: _MissingFileResp())

    assert nl.load_realtime_state() == {}


def test_load_realtime_state_rejects_gist_failure_instead_of_using_local_state(
        monkeypatch, tmp_path):
    """Gist 读取失败时，不能静默使用本地旧资讯状态。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(nl, "_REALTIME_STATE_PATH", tmp_path / "real_time_state.json")
    nl._REALTIME_STATE_PATH.write_text(
        '{"pushed_events": [{"t": "2026-08-28 10:00:00"}]}', encoding="utf-8")
    monkeypatch.setattr(nl, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("network down")))

    with pytest.raises(RuntimeError, match="Gist 状态文件 real_time_state.json"):
        nl.load_realtime_state()


def test_load_citic_pos_state_does_not_fallback_to_local_when_gist_file_missing(
        monkeypatch, tmp_path):
    """Gist 成功但中信文件缺失时，不能使用本地旧持仓。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(nl, "_CITIC_POS_STATE_PATH", tmp_path / "citic_pos_state.json")
    nl._CITIC_POS_STATE_PATH.write_text(
        '{"pos_history": [{"day": "2026-08-27", "net": {"_total": -3200}}]}',
        encoding="utf-8")

    class _MissingFileResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"files": {}}'

    monkeypatch.setattr(nl, "urlopen", lambda *args, **kwargs: _MissingFileResp())

    assert nl.load_citic_pos_state() == {}


def test_macro_exposure_ignores_stale_citic_position_history():
    """中信持仓超过一个交易日未更新时，不得继续触发宏观降仓。"""
    citic = {"pos_history": [{"day": "2026-08-25",
                               "net": {"_total": -3200}}]}

    m = nl.macro_exposure({}, citic_state=citic, today=date(2026, 8, 28))

    assert m["factor"] == 1.0
    assert any("过期" in reason for reason in m["reasons"])


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


# ---------- P7-1：今日资讯输入 = 已推送 ∪ 预筛候选 ----------
def _cand(title, dir_, entities=(), events=(), numbers=(), t="2026-08-22 10:00:00",
          pushed=None):
    e = {"title_norm": title, "dir": dir_, "entities": list(entities),
         "events": list(events), "numbers": list(numbers), "t": t}
    if pushed is not None:
        e["pushed"] = pushed
    return e


def test_today_news_events_union_pushed_and_candidate():
    state = {
        "pushed_events": [_cand("英伟达订单超预期", "bullish",
                                entities=["英伟达"], t="2026-08-22 09:00:00")],
        "candidate_events": [_cand("光模块集采招标", "mildly_bullish",
                                   entities=["中际旭创"], t="2026-08-22 10:30:00")],
    }
    evs = nl.today_news_events(state, "2026-08-22")
    assert len(evs) == 2  # 已推送 + 预筛弱档候选都覆盖


def test_today_news_events_dedup_same_event():
    # 同一事件同时出现在 pushed_events 与 candidate_events（已推送的候选并存），
    # 按 (日期,事件签名) 去重，只计一次，不重复计权。
    state = {
        "pushed_events": [_cand("英伟达订单超预期", "bullish",
                                entities=["英伟达"], t="2026-08-22 09:00:00")],
        "candidate_events": [_cand("英伟达订单超预期", "bullish",
                                   entities=["英伟达"], t="2026-08-22 09:05:00")],
    }
    evs = nl.today_news_events(state, "2026-08-22")
    assert len(evs) == 1


def test_today_news_events_excludes_other_days():
    # 只收敛当日真实候选，昨日事件（即便强档已推）不入今日资讯维度。
    state = {
        "pushed_events": [_cand("昨日利空", "bearish",
                                entities=["宁德时代"], t="2026-08-21 15:00:00")],
        "candidate_events": [_cand("今日利多", "bullish",
                                   entities=["中际旭创"], t="2026-08-22 09:00:00")],
    }
    evs = nl.today_news_events(state, "2026-08-22")
    assert len(evs) == 1
    assert evs[0]["title_norm"] == "今日利多"


def test_today_news_events_default_today():
    """UTC 次日凌晨时，缺省 today 仍应按北京时间取日期。"""
    instant = datetime(2026, 8, 28, 16, 30, tzinfo=timezone.utc)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    state = {"candidate_events": [_cand("北京时间今日事件", "bullish",
                                         t="2026-08-29 00:10:00")]}
    old_datetime = nl.datetime
    nl.datetime = _FakeDateTime
    try:
        evs = nl.today_news_events(state, None)
    finally:
        nl.datetime = old_datetime

    assert len(evs) == 1


def test_recent_pushed_events_uses_beijing_time_window(monkeypatch):
    """资讯状态的 48 小时窗口不能因 UTC/BJT 偏移多收一条。"""
    instant = datetime(2026, 8, 28, 0, 30, tzinfo=timezone(timedelta(hours=8)))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(nl, "datetime", _FakeDateTime)
    state = {"pushed_events": [_event(
        "窗口外事件", "bullish", t="2026-08-26 00:29:00")]}  # 48h01 前

    assert nl.recent_pushed_events(state, hours=48) == []


def test_today_news_events_empty_state():
    assert nl.today_news_events({}, "2026-08-22") == []
