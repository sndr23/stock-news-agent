# -*- coding: utf-8 -*-
"""基金轮动信号层（src/strategy/fund_rotation.py）单元测试"""
import json
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import fund_rotation as frot  # noqa: E402
from src.strategy import fund_data as fdata  # noqa: E402

pytestmark = pytest.mark.unit


def _rets(pcts):
    """构造升序日增长率序列 [(date, pct), ...]"""
    return [(f"2026-01-{i+1:02d}", p) for i, p in enumerate(pcts)]


# ---------------- 动量与趋势 ----------------

def test_cum_pct_basic():
    assert frot.cum_pct(_rets([1.0, 1.0]), 2) == pytest.approx(2.01, abs=1e-6)
    assert frot.cum_pct(_rets([-1.0, -1.0]), 2) == pytest.approx(-1.99, abs=1e-6)


def test_cum_pct_empty_and_short():
    assert frot.cum_pct([], 20) == 0.0
    assert frot.cum_pct(_rets([2.0]), 20) == pytest.approx(2.0)


def test_momentum_score_weights():
    # 单调上涨：动量正、趋势门开
    rets = _rets([0.5] * 30)
    m = frot.momentum_score(rets, intraday_pct=1.0)
    assert m["score"] > 0
    assert m["trend_ok"] is True
    assert m["m5"] > 0


def test_momentum_score_downtrend_gate():
    # 持续下跌：趋势门关（净值在 MA20 下）
    rets = _rets([-1.0] * 30)
    m = frot.momentum_score(rets)
    assert m["trend_ok"] is False
    assert m["score"] < 0


def test_momentum_score_intraday_blends():
    rets = _rets([0.0] * 30)
    a = frot.momentum_score(rets, intraday_pct=0.0)["score"]
    b = frot.momentum_score(rets, intraday_pct=2.0)["score"]
    assert b > a  # 盘中上涨抬高分数


# ---------------- 滞回建议 ----------------

def _sig(code, score, trend_ok=True, m20=5.0):
    return {"code": code, "name": code, "score": score,
            "trend_ok": trend_ok, "m20": m20, "m60": score, "m5": score,
            "est_pct": 0.0, "covered_w": 60.0, "nav_days": 100}


def test_top_selection_and_equality_weights():
    sigs = [_sig("A", 10), _sig("B", 8), _sig("C", 6), _sig("D", 4), _sig("E", 2)]
    r = frot.build_rotation_advice(sigs, {}, exposure=1.0, max_positions=3)
    assert set(r.target) == {"A", "B", "C"}
    assert abs(sum(r.target.values()) - 1.0) < 1e-6  # 满仓等权


def test_hysteresis_holder_stays_in_buffer_zone():
    # D 持有、排名 4（跌出 Top3 但在缓冲带 Top4）→ 保留
    sigs = [_sig("A", 10), _sig("B", 8), _sig("C", 6), _sig("D", 4), _sig("E", 2)]
    r = frot.build_rotation_advice(sigs, {"D": 0.3}, exposure=1.0, max_positions=3,
                                   buffer_rank=1)
    assert "D" in r.target
    # D 排名跌到 5（缓冲带外）→ 卖出
    sigs2 = [_sig("A", 10), _sig("B", 9), _sig("C", 8), _sig("E", 7), _sig("D", 1)]
    r2 = frot.build_rotation_advice(sigs2, {"D": 0.3}, exposure=1.0, max_positions=3,
                                    buffer_rank=1)
    assert "D" not in r2.target
    assert any(a["code"] == "D" and a["action"] == "卖出" for a in r2.actions)


def test_trend_gate_blocks_new_buy_not_hold():
    # C 排名第2但趋势门关：非持有者不买；持有者可留
    sigs = [_sig("A", 10), _sig("C", 8, trend_ok=False), _sig("B", 6), _sig("D", 4)]
    r_new = frot.build_rotation_advice(sigs, {}, exposure=1.0, max_positions=3)
    assert "C" not in r_new.target  # 空仓者被趋势门挡住
    r_hold = frot.build_rotation_advice(sigs, {"C": 0.3}, exposure=1.0, max_positions=3)
    assert "C" in r_hold.target  # 持有者保留


def test_defensive_reduce_on_deep_drawdown():
    # A 排名第一但近20日 -12% 触发防守 → 移出目标
    sigs = [_sig("A", 10, m20=-12.0), _sig("B", 8), _sig("C", 6), _sig("D", 4)]
    r = frot.build_rotation_advice(sigs, {"A": 0.4}, exposure=1.0, max_positions=3)
    assert "A" not in r.target
    act = next(a for a in r.actions if a["code"] == "A")
    assert act["action"] == "卖出"
    assert "防守" in act["detail"]


def test_exposure_zero_sells_all():
    sigs = [_sig("A", 10), _sig("B", 8)]
    r = frot.build_rotation_advice(sigs, {"A": 0.5, "B": 0.5}, exposure=0.0)
    assert r.target == {}
    assert all(a["action"] == "卖出" for a in r.actions)


def test_per_fund_cap_and_partial_exposure():
    sigs = [_sig("A", 10), _sig("B", 8)]
    r = frot.build_rotation_advice(sigs, {}, exposure=0.5, max_positions=3,
                                   per_fund_cap=0.40)
    assert abs(sum(r.target.values()) - 0.5) < 1e-6
    assert all(w <= 0.40 + 1e-9 for w in r.target.values())


def test_no_action_when_stable():
    # 持仓正好等于目标 → 全部"持有"，无交易动作
    sigs = [_sig("A", 10), _sig("B", 8), _sig("C", 6), _sig("D", 4)]
    r = frot.build_rotation_advice(sigs, {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
                                   exposure=1.0, max_positions=3)
    trades = [a for a in r.actions if a["action"] != "持有"]
    assert not trades
    assert all(a["action"] == "持有" for a in r.actions if a["code"] in r.target)


def test_small_weight_diff_ignored():
    # 权重差 <5% 不产生调仓动作
    sigs = [_sig("A", 10), _sig("B", 8), _sig("C", 6), _sig("D", 4)]
    r = frot.build_rotation_advice(sigs, {"A": 0.34, "B": 0.33, "C": 0.33},
                                   exposure=1.0, max_positions=3)
    assert not [a for a in r.actions
                if a["action"] in ("加仓", "减仓")]


# ---------------- 盘中估值（fund_data 纯函数） ----------------

def test_intraday_estimate_weighted():
    holdings = [{"code": "600519", "weight": 10.0},
                {"code": "000858", "weight": 5.0}]
    quotes = {"600519": 2.0, "000858": 1.0}
    est = fdata.intraday_estimate(holdings, quotes)
    # (10*2 + 5*1)/15 = 1.667
    assert est["est_pct"] == pytest.approx(1.67, abs=0.01)
    assert est["covered_w"] == 15.0


def test_intraday_estimate_missing_quote():
    holdings = [{"code": "600519", "weight": 10.0},
                {"code": "000858", "weight": 5.0}]
    est = fdata.intraday_estimate(holdings, {"600519": 3.0})
    assert est["est_pct"] == 3.0  # 缺行情的剔除，不稀释
    assert est["covered_w"] == 10.0


def test_intraday_estimate_empty():
    assert fdata.intraday_estimate([], {}) == {"est_pct": 0.0, "covered_w": 0.0}


def test_holdings_cache_bad_root_falls_back_to_free_source(monkeypatch, tmp_path):
    """持仓缓存根节点损坏时，应继续请求天天基金免费接口而不是抛异常。"""
    cache_path = tmp_path / "fund_holdings_cache.json"
    cache_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(fdata, "HOLDINGS_CACHE_PATH", cache_path)
    rows = [{"secid": "1.600519", "code": "600519", "name": "贵州茅台",
             "weight": 10.0}]
    monkeypatch.setattr(fdata, "get_holdings", lambda *args, **kwargs: rows)

    assert fdata.load_holdings_cached(["000001"]) == {"000001": rows}


def test_holdings_cache_empty_free_response_uses_fresh_cache(monkeypatch, tmp_path):
    """免费接口返回空结果时，有效缓存仍应作为降级结果返回。"""
    cache_path = tmp_path / "fund_holdings_cache.json"
    rows = [{"secid": "1.600519", "code": "600519", "name": "贵州茅台",
             "weight": 10.0}]
    cache_path.write_text(
        json.dumps({"_ts": time.time(), "funds": {"000001": {"rows": rows}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(fdata, "HOLDINGS_CACHE_PATH", cache_path)
    monkeypatch.setattr(fdata, "get_holdings", lambda *args, **kwargs: [])

    assert fdata.load_holdings_cached(["000001"]) == {"000001": rows}


def test_holdings_cache_invalid_timestamp_is_not_used(monkeypatch, tmp_path):
    """持仓缓存时间戳非法时，不得在降级判断中抛异常或使用旧持仓。"""
    cache_path = tmp_path / "fund_holdings_cache.json"
    cache_path.write_text(
        '{"_ts":"not-a-timestamp","funds":{"000001":{"rows":[{"secid":"1.600519"}]}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(fdata, "HOLDINGS_CACHE_PATH", cache_path)
    monkeypatch.setattr(fdata, "get_holdings",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            OSError("free source unavailable")))

    assert fdata.load_holdings_cached(["000001"]) == {"000001": []}


def test_expired_holdings_cache_is_not_rejuvenated_after_repeated_failures(
        monkeypatch, tmp_path):
    """过期持仓在连续失败时不得因重写全局时间戳而无限续期。"""
    cache_path = tmp_path / "fund_holdings_cache.json"
    rows = [{"secid": "1.600519", "code": "600519", "name": "贵州茅台",
             "weight": 10.0}]
    cache_path.write_text(
        json.dumps({"_ts": time.time() - 8 * 86400,
                    "funds": {"000001": {"rows": rows}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(fdata, "HOLDINGS_CACHE_PATH", cache_path)
    monkeypatch.setattr(fdata, "get_holdings", lambda *args, **kwargs: [])

    first = fdata.load_holdings_cached(["000001"])
    second = fdata.load_holdings_cached(["000001"])

    assert first == {"000001": []}
    assert second == {"000001": []}


def test_sec_id_to_tencent_ignores_malformed_identifier():
    """损坏的免费持仓 secid 不应阻断其余证券行情转换。"""
    assert fdata.secid_to_tencent("bad-secid") == ""
