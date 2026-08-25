# -*- coding: utf-8 -*-
"""创业板多因子择时核心（src/strategy/chinext_timing.py）单元测试"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_timing as ct  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------- 合成数据 ----------------

def _up(n=320, r=0.002):
    """单调上涨收盘序列。"""
    return [100.0 * (1 + r) ** i for i in range(n)]


def _down(n=320, r=0.002):
    return [100.0 * (1 - r) ** i for i in range(n)]


def _flat_amounts(n=320, base=1e9):
    return [base] * n


# ---------------- 核心层 ----------------

def test_trend_score_uptrend_positive():
    t = ct.trend_score(_up())
    assert t["score"] > 0.9
    assert t["above20"] is True


def test_trend_score_downtrend_negative():
    t = ct.trend_score(_down())
    assert t["score"] < -0.9
    assert t["above20"] is False


def test_trend_score_short_history_returns_zero():
    assert ct.trend_score([100.0] * 30)["score"] == 0.0


def test_momentum_score_signs_and_intraday():
    up = ct.momentum_score(_up(r=0.006))
    assert up["score"] > 0.75  # 20日+12.7% 60日+43% → 接近满格
    dn = ct.momentum_score(_down(r=0.006))
    assert dn["score"] < -0.75
    a = ct.momentum_score(_up(), intraday_pct=0.0)["score"]
    b = ct.momentum_score(_up(), intraday_pct=3.0)["score"]
    assert b > a  # 盘中上涨抬升动量


def test_momentum_score_bounds():
    s = ct.momentum_score(_up(r=0.05), intraday_pct=10.0)  # 极端涨幅仍封顶
    assert s["score"] <= 1.0


def test_volume_price_four_quadrants():
    closes = _flat_then_move = [100.0] * 300
    amounts = _flat_amounts(301)
    # 放量上涨（末量2倍 + 末价+2%）
    c2 = closes + [102.0]
    a2 = amounts[:-1] + [amounts[-1] * 2.5]
    assert ct.volume_price_score(c2, a2)["score"] > 0.5
    # 放量下跌
    c3 = closes + [98.0]
    assert ct.volume_price_score(c3, a2)["score"] < -0.5
    # 缩量反弹（末量0.2倍）
    a4 = amounts[:-1] + [amounts[-1] * 0.2]
    s = ct.volume_price_score(c2, a4)["score"]
    assert 0 < s <= 0.3
    # 缩量阴跌
    assert -0.35 <= ct.volume_price_score(c3, a4)["score"] < 0


def test_volume_price_intraday_crash_penalty():
    closes = [100.0] * 300 + [102.0]
    amounts = _flat_amounts(301)
    calm = ct.volume_price_score(closes, amounts, intraday_pct=0.0)["score"]
    crash = ct.volume_price_score(closes, amounts, intraday_pct=-3.0)["score"]
    assert crash < calm


def test_volume_price_no_amount_degrades():
    closes = [100.0] * 300 + [102.0]
    assert ct.volume_price_score(closes, [0.0] * 301)["score"] == 0.0


def test_volatility_deep_drawdown_penalty():
    # 造 60 日高点回撤 >12%：先涨后深跌
    closes = [100.0 * (1.002 ** i) for i in range(280)] + \
             [100.0 * (1.002 ** 279) * (0.85 ** (i + 1)) for i in range(15)]
    v = ct.volatility_score(closes)
    assert v["dd60"] < -12
    assert v["score"] <= -0.99


def test_volatility_no_penalty_in_calm_uptrend():
    v = ct.volatility_score(_up())
    assert v["score"] == 0.0


def test_core_score_composition_and_bounds():
    c = ct.core_score(_up(), _flat_amounts(320), intraday_pct=1.0)
    assert c["score"] > 0.5
    c2 = ct.core_score(_down(), _flat_amounts(320), intraday_pct=-2.0)
    assert c2["score"] < -0.5
    assert -1.0 <= c["score"] <= 1.0


# ---------------- 修正层 ----------------

def test_derivatives_modifier_discount_ladder():
    assert ct.derivatives_modifier({"IC": {"annual_pct": -16.0}})["score"] == -0.15
    assert ct.derivatives_modifier({"IC": {"annual_pct": -10.0}})["score"] == -0.10
    assert ct.derivatives_modifier({"IC": {"annual_pct": -5.0}})["score"] == -0.05
    assert ct.derivatives_modifier({"IC": {"annual_pct": -1.0}})["score"] == 0.0
    assert ct.derivatives_modifier({"IC": {"annual_pct": 0.5}})["score"] == 0.05


def test_derivatives_modifier_citic_and_clamp():
    # 贴水-16 + 中信净-5000 → -0.25 被封到 -0.15
    r = ct.derivatives_modifier({"IC": {"annual_pct": -16.0}}, citic_net=-5000)
    assert r["score"] == -0.15
    assert ct.derivatives_modifier({}, citic_net=3000)["score"] == 0.05
    assert ct.derivatives_modifier({}, citic_net=-3000)["score"] == -0.10


def test_derivatives_modifier_empty():
    assert ct.derivatives_modifier({})["score"] == 0.0


def test_flows_modifier_thresholds():
    assert ct.flows_modifier({"main_net_yi": -150})["score"] == -0.10
    assert ct.flows_modifier({"main_net_yi": -60})["score"] == -0.05
    assert ct.flows_modifier({"main_net_yi": 20})["score"] == 0.0
    assert ct.flows_modifier({"main_net_yi": 150})["score"] == 0.05


def test_flows_modifier_sector_tech():
    sf = {"inflow": [["电子", 30]], "outflow": []}
    assert ct.flows_modifier({}, sf, ct.TECH_KW)["score"] == 0.03
    sf2 = {"inflow": [], "outflow": [["半导体", 20]]}
    assert ct.flows_modifier({}, sf2, ct.TECH_KW)["score"] == -0.03


def test_mood_modifier_signals():
    euphoria = ct.mood_modifier({"mood": "亢奋"}, {"down_pct": 20}, {"pcr": 0.5})
    assert euphoria["score"] == 0.08  # 亢奋+0.05 下跌少+0.03 PCR低+0.03 → 封顶0.08
    freeze = ct.mood_modifier({"mood": "冰点"}, {"down_pct": 80}, {"pcr": 2.0})
    assert freeze["score"] == -0.08
    assert ct.mood_modifier({}, {}, {})["score"] == 0.0


def test_news_modifier_weighting():
    evs = [
        {"dir": "bearish", "title_norm": "光模块龙头业绩暴雷", "sectors": [], "entities": []},
        {"dir": "bearish", "title_norm": "CPO板块遭大幅减持", "sectors": [], "entities": []},
        {"dir": "bullish", "title_norm": "某消费公司中标", "sectors": [], "entities": []},
    ]
    r = ct.news_modifier(evs)
    # (-1*1 -1*1 +1*0.2)/3 = -0.6 → -0.09
    assert r["score"] == pytest.approx(-0.09, abs=1e-6)
    assert r["n"] == 3


def test_news_modifier_strong_bearish_floors():
    evs = [{"dir": "bearish", "title_norm": f"科技利空{i}", "sectors": [], "entities": []}
           for i in range(6)]
    assert ct.news_modifier(evs)["score"] == -0.15  # 封底


def test_news_modifier_empty():
    r = ct.news_modifier([])
    assert r["score"] == 0.0 and r["n"] == 0


# ---------------- 硬风控 ----------------

def _snap(risk="normal", pctile=None, ic=None):
    s = {"risk_state": risk, "vol": {}, "basis": {}}
    if pctile is not None:
        s["vol"]["创业板指"] = {"pctile": pctile}
    if ic is not None:
        s["basis"]["IC"] = {"annual_pct": ic}
        s["basis"]["IM"] = {"annual_pct": ic}
    return s


def test_defensive_caps_none_triggered():
    r = ct.defensive_caps(_up(), 0.5, _snap(), None)
    assert r["cap"] == 1.0 and r["reasons"] == []


def test_defensive_caps_deep_drawdown():
    closes = [100.0 * (1.002 ** i) for i in range(280)] + \
             [100.0 * (1.002 ** 279) * (0.85 ** (i + 1)) for i in range(15)]
    r = ct.defensive_caps(closes, 0.0, _snap(), None)
    assert r["cap"] == 0.3


def test_defensive_caps_vol_pctile():
    assert ct.defensive_caps(_up(), 0.0, _snap(pctile=96), None)["cap"] == 0.6
    assert ct.defensive_caps(_up(), 0.0, _snap(pctile=85), None)["cap"] == 1.0


def test_defensive_caps_risk_off():
    assert ct.defensive_caps(_up(), 0.0, _snap(risk="risk_off"), None)["cap"] == 0.3


def test_defensive_caps_discount_plus_citic():
    r = ct.defensive_caps(_up(), 0.0, _snap(ic=-13.0), citic_net=-3000)
    assert r["cap"] == 0.6
    # 只有贴水没有中信加空 → 不触发
    assert ct.defensive_caps(_up(), 0.0, _snap(ic=-13.0), None)["cap"] == 1.0


def test_defensive_caps_intraday_crash():
    assert ct.defensive_caps(_up(), -3.0, _snap(), None)["cap"] == 0.3


# ---------------- 档位状态机 ----------------

def test_score_to_tier_boundaries():
    assert ct.score_to_tier(0.40) == 1.0
    assert ct.score_to_tier(0.39) == 0.9
    assert ct.score_to_tier(0.05) == 0.9
    assert ct.score_to_tier(0.00) == 0.9
    assert ct.score_to_tier(-0.15) == 0.9
    assert ct.score_to_tier(-0.16) == 0.6
    assert ct.score_to_tier(-0.25) == 0.6
    assert ct.score_to_tier(-0.30) == 0.6
    assert ct.score_to_tier(-0.31) == 0.0


def test_decide_downgrade_immediate():
    prev = {"position": 1.0, "pending": None}
    dec = ct.decide_position(-0.5, 1.0, prev)
    assert dec["changed"] and dec["direction"] == "down" and dec["position"] == 0.0


def test_decide_upgrade_needs_two_days():
    prev = {"position": 0.0, "pending": None}
    d1 = ct.decide_position(0.5, 1.0, prev)
    assert not d1["changed"] and d1["pending"] == {"target": 1.0, "days": 1}
    # 第2日同目标 → 确认执行（连续两日达标）
    d2 = ct.decide_position(0.5, 1.0, d1)
    assert d2["changed"] and d2["direction"] == "up" and d2["position"] == 1.0
    assert d2["pending"] is None


def test_decide_upgrade_target_dropout_resets():
    """第1日提出升档，第2日分数回落（目标变低/不变）→ 不升档。"""
    prev = {"position": 0.0, "pending": {"target": 1.0, "days": 1}}
    d = ct.decide_position(0.1, 1.0, prev)  # 目标回落到 0.9
    assert not d["changed"] and d["pending"] == {"target": 0.9, "days": 1}


def test_decide_upgrade_resets_when_target_changes():
    prev = {"position": 0.0, "pending": {"target": 1.0, "days": 1}}
    # 次日目标变为 0.9（分数回落）→ pending 重置为 0.9 的第1天
    d = ct.decide_position(0.1, 1.0, prev)
    assert not d["changed"] and d["pending"] == {"target": 0.9, "days": 1}


def test_decide_cap_forces_immediate_down():
    prev = {"position": 1.0, "pending": None}
    dec = ct.decide_position(0.5, 0.3, prev)  # 分数满仓但风控封顶3成
    assert dec["changed"] and dec["direction"] == "down" and dec["position"] == 0.3


def test_decide_hold_clears_pending():
    prev = {"position": 0.6, "pending": {"target": 1.0, "days": 1}}
    d = ct.decide_position(-0.20, 1.0, prev)  # 目标=当前档0.6
    assert not d["changed"] and d["pending"] is None


def test_hysteresis_band_blocks_threshold_jitter():
    """分数在满仓线 0.40 下方 0.05 处震荡：原逻辑立即降档，滞回带内维持。"""
    prev = {"position": 1.0, "pending": None}
    d = ct.decide_position(0.38, 1.0, prev)  # 0.38 ∈ [0.35, 0.40) → 维持满仓
    assert not d["changed"] and d["position"] == 1.0
    d2 = ct.decide_position(0.34, 1.0, prev)  # 明确跌破 0.35 → 降九成
    assert d2["changed"] and d2["position"] == 0.9


def test_hysteresis_band_downgrade_path():
    prev = {"position": 0.6, "pending": None}
    # 0.01 ∈ [0.0, 0.05) → 维持六成（原逻辑降三成）
    assert ct.decide_position(0.01, 1.0, prev)["position"] == 0.6
    # -0.28 ∈ [-0.40, -0.35) 滞回带 → 维持三成
    prev3 = {"position": 0.3, "pending": None}
    assert ct.decide_position(-0.28, 1.0, prev3)["position"] == 0.3
    # -0.41 明确跌破 -0.40（带滞回带维持线）→ 空仓
    assert ct.decide_position(-0.41, 1.0, prev3)["position"] == 0.0


def test_hysteresis_not_apply_to_upgrade():
    """滞回带只护降档，不放松升档：不达高档位线不提议升档；达满仓线才提议（两日确认）。"""
    prev = {"position": 0.6, "pending": None}
    d = ct.decide_position(-0.20, 1.0, prev)
    assert d["position"] == 0.6 and d["pending"] is None  # 未过0.9档升档线(-0.15)
    d2 = ct.decide_position(0.41, 1.0, prev)
    assert not d2["changed"] and d2["pending"]["target"] == 1.0  # 达满仓线，提议升档待确认


# ---------------- 合成 ----------------

def test_composite_bounded():
    core = {"score": 0.9}
    deriv = {"score": 0.15}
    flow = {"score": 0.10}
    mood = {"score": 0.08}
    news = {"score": 0.15}
    assert ct.composite(core, deriv, flow, mood, news) == 1.0  # 整体封顶
    # 修正合计 0.48 被封到 0.30
    assert ct.composite({"score": 0.0}, deriv, flow, mood, news) == 0.30
    core_n = {"score": -0.9}
    assert ct.composite(core_n, deriv, flow, mood, news) == -0.6  # -0.9+0.30


def test_modifier_cannot_flip_neutral_market():
    """修正层合计封顶 ±0.30 < 满仓线 0.40：中性核心永不被实时数据推到满仓档。"""
    mods = ({"score": 0.15}, {"score": 0.10}, {"score": 0.08}, {"score": 0.15})
    s = ct.composite({"score": 0.0}, *mods)
    assert s == 0.30
    assert ct.score_to_tier(s) == 0.9  # 最多到九成档，到不了满仓档


def test_spearman_ic_perfect_rank():
    # 完全正相关秩 → IC=1；完全负相关 → IC=-1
    assert ct.spearman_ic([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0, abs=1e-9)
    assert ct.spearman_ic([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0, abs=1e-9)
    # 并列秩处理不崩溃
    r = ct.spearman_ic([1, 1, 2, 2, 3], [5, 4, 3, 2, 1])
    assert -1.0 <= r <= 1.0


def test_shadow_ic_sample_threshold():
    # 样本 <10 → IC=None（不足门槛）
    hist = [{"core": i, "next_ret": 0.01 * i} for i in range(5)]
    out = ct.shadow_ic(hist)
    assert out["core"]["ic"] is None and out["core"]["n"] == 5
    # 样本 ≥10 且 core 与收益正相关 → 有 IC
    hist = [{"core": i, "next_ret": 0.01 * (i - 5)} for i in range(20)]
    out = ct.shadow_ic(hist)
    assert out["core"]["ic"] is not None and out["core"]["n"] == 20
    assert out["core"]["ic"] > 0.5
    # 无 next_ret 样本不计
    hist2 = [{"core": 1.0, "next_ret": None}] * 20
    out2 = ct.shadow_ic(hist2)
    assert out2["core"]["ic"] is None and out2["core"]["n"] == 0


# ---------------- 中际旭创双确认 ----------------

def test_stock_confirm_index_up_stock_weak_downgrade():
    """指数看多但个股趋势走弱 → 降档确认 -0.10。"""
    r = ct.stock_confirm(
        {"score": -0.8}, {"score": -0.6}, {"score": 0.7})
    assert r["score"] == -0.10
    assert r["agree"] is False


def test_stock_confirm_index_down_stock_strong_upgrade():
    """指数看空但个股企稳走强 → 温和升档 +0.08。"""
    r = ct.stock_confirm(
        {"score": 0.8}, {"score": 0.6}, {"score": -0.7})
    assert r["score"] == 0.08
    assert r["agree"] is False


def test_stock_confirm_agree_neutral():
    """指数与个股同向 → 中性（主信号已覆盖）。"""
    r = ct.stock_confirm(
        {"score": 0.8}, {"score": 0.6}, {"score": 0.7})
    assert r["score"] == 0.0
    assert r["agree"] is True


def test_stock_confirm_mild_divergence_no_action():
    """轻微背离（个股方向未破 ±0.1）→ 不动作。"""
    r = ct.stock_confirm(
        {"score": -0.05}, {"score": -0.05}, {"score": 0.7})
    assert r["score"] == 0.0
    assert r["agree"] is False


def test_stock_confirm_missing_data_skip():
    """个股数据不足 → 跳过，不动作。"""
    r = ct.stock_confirm(None, None, {"score": 0.7})
    assert r["score"] == 0.0
    assert r["agree"] is None


def test_stock_confirm_intraday_drop_triggers_downgrade():
    """当日盘中大跌拉低个股方向 → 对原本中性/看多的指数触发降档确认。"""
    # 无盘中时：基准 -0.05 不破 ±0.1 → 不动作
    r0 = ct.stock_confirm({"score": 0.0}, {"score": -0.1}, {"score": 0.7})
    assert r0["score"] == 0.0
    # 有 -5% 盘中（day=-0.4）→ stock_dir≈-0.45 → 指数多/个股弱，降档
    r = ct.stock_confirm({"score": 0.0}, {"score": -0.1}, {"score": 0.7},
                         intraday_pct=-5.0)
    assert r["score"] == -0.10
    assert r["agree"] is False


def test_stock_confirm_intraday_cap_bound():
    """盘中涨跌幅折算的动量增量有界（±0.4），极端值不失控。"""
    r_pos = ct.stock_confirm({"score": 0.8}, {"score": 0.6}, {"score": -0.7},
                             intraday_pct=99.0)
    r_neg = ct.stock_confirm({"score": 0.0}, {"score": -0.1}, {"score": 0.7},
                             intraday_pct=-99.0)
    # 有界后仍只落在定义档位（±0.10/±0.08/0）
    assert r_pos["score"] in (-0.10, 0.08, 0.0)
    assert r_neg["score"] in (-0.10, 0.08, 0.0)


def test_update_shadow_history_accepts_res_top_level():
    """回归：update_shadow_history 必须兼容 main() 传入的 res 顶层（keyerror 修复）。
    曾致推送成功后立即 KeyError:'basis'（res 顶层无 basis/flow/mood/news 直接键），
    chan/stock 若取顶层则恒 None→恒0（假数据）。验证正确解构 res["mods"]。"""
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    from run_chinext_timing import update_shadow_history as ush

    res = {"core": {"score": 0.6, "signals": {}},
           "mods": {"basis": -0.06, "flow": 0.0, "mood": 0.03, "news": 0.0,
                    "chan": {"score": -0.08}, "stock": {"score": -0.10}},
           "score": 0.54, "caps": {"cap": 0.3}}
    ctx = {"closes": [100.0, 101.0], "dates": ["2026-08-21", "2026-08-22"],
           "overseas_drop": -0.04}
    state = {"history": []}
    ush(state, ctx, "2026-08-22", res["score"], res, 0.3)  # 传 res 顶层，不应 KeyError
    h = state["history"][-1]
    assert h["core"] == 0.6
    assert h["basis"] == -0.06
    assert h["mood"] == 0.03
    assert h["chan"] == -0.08          # 修复前恒 0
    assert h["stock"] == -0.10         # 修复前恒 0
    assert h["sox"] == -0.04           # 外盘实值


def test_shadow_raw_captures_main_net_yi():
    """回归：_shadow_raw 必须读取快照键 main_net_yi（曾误读 main_net 致资金流原始值恒 None，
    资金维原始 IC 验门静默失效）；缺源一律置 None 且键恒存在（与"缺源降级0"解耦）。"""
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    from run_chinext_timing import _shadow_raw

    ctx = {"snapshot": {
        "basis": {"IC": {"annual_pct": -9.5}, "IM": {"annual_pct": -11.2}},
        "flows": {"main_net_yi": -86.4},
        "breadth": {"down_pct": 74.0},
        "option": {"pcr": 1.62}}}
    raw = _shadow_raw(ctx)
    assert raw["basis_min_ap"] == -11.2     # IC/IM 最差年化
    assert raw["main_net"] == -86.4         # 修复前恒 None
    assert raw["down_pct"] == 74.0
    assert raw["pcr"] == 1.62

    # 缺源：四键全部存在且为 None（不允许键缺失，避免下游 KeyError/假0）
    raw2 = _shadow_raw({"snapshot": {}})
    assert raw2 == {"basis_min_ap": None, "main_net": None,
                    "down_pct": None, "pcr": None}

    # 兼容旧快照缺失 flows 键
    raw3 = _shadow_raw({"snapshot": {"breadth": {"down_pct": 10.0}}})
    assert raw3["main_net"] is None
    assert raw3["down_pct"] == 10.0


def test_shadow_raw_stale_snapshot_all_none():
    """回归：_shadow_raw 遇快照停更（snapshot_stale=True）整体置 None。
    原始值非当日=假样本，会污染影子 IC 验门（20260824 实际事故：
    factor_state 停更 08-21，14:45 信号资金维仍用周五 +167.2亿 给 +0.05）。"""
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    from run_chinext_timing import _shadow_raw

    # 即便快照内是"新鲜值"，stale 标记存在即应整体置 None（防旧值冒充当日样本）
    ctx = {"snapshot_stale": True, "snapshot": {
        "basis": {"IC": {"annual_pct": -9.5}, "IM": {"annual_pct": -11.2}},
        "flows": {"main_net_yi": 167.2},
        "breadth": {"down_pct": 52.8},
        "option": {"pcr": 1.62}}}
    assert _shadow_raw(ctx) == {"basis_min_ap": None, "main_net": None,
                                "down_pct": None, "pcr": None}
    # stale 关闭时正常取值（对比对照）
    ctx2 = {"snapshot_stale": False, "snapshot": {
        "basis": {"IC": {"annual_pct": -9.5}, "IM": {"annual_pct": -11.2}},
        "flows": {"main_net_yi": 167.2},
        "breadth": {"down_pct": 52.8}}}
    assert _shadow_raw(ctx2)["main_net"] == 167.2
    assert _shadow_raw(ctx2)["basis_min_ap"] == -11.2


def _make_gather_df(last_date: str, n: int = 70):
    """构造 gather_context 输入 DataFrame：n 根日线，末根日期可指定。"""
    import pandas as pd
    dates = pd.bdate_range(end=pd.Timestamp(last_date), periods=n)
    closes = [100.0 * (1 + 0.001 * i) for i in range(n)]
    amounts = [1e8 + i * 1e6 for i in range(n)]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    return pd.DataFrame({"close": closes, "amount": amounts,
                         "high": highs, "low": lows}, index=dates)


def test_gather_context_drops_intraday_partial(monkeypatch):
    """口径收口：末根为当日（14:45 partial）时无条件剔除，信号只用 ≤d-1 完整日线。

    回归背景：此前仅替换 amounts[-1]（closes 仍含 partial 污染均线/波动），
    且依赖数据层缓存是否命中导致行为抖动（同一时点信号不可复现）。
    修复后无论缓存命中与否，当日 bar 一律剔除 → 行为恒一。
    """
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    import run_chinext_timing as rct
    import datetime as _dt

    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 24, 14, 45)   # 周一 14:45
    monkeypatch.setattr(rct, "datetime", _FakeDT)
    # 屏蔽其余数据源（本测试只验证日线口径）
    monkeypatch.setattr(rct, "get_quotes", lambda *a, **k: {})
    monkeypatch.setattr(rct.nl, "load_factor_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_citic_pos_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_realtime_state", lambda: {})
    monkeypatch.setattr(rct.ovs, "load_overseas", lambda *a, **k: {})
    monkeypatch.setattr(rct, "load_stock_sina", lambda *a, **k: None)
    monkeypatch.setattr(rct, "_load_erp_basis", lambda *a, **k: None)

    # 情形 A：末根为当日（partial）→ 剔除后最后一根应为 08-21（周五）
    # 71 根（08-18~08-24）剔除当日 → 70 根（08-18~08-21），与 B 完全同窗
    df_a = _make_gather_df("2026-08-24", n=71)
    ctx_a = rct.gather_context(df_a)
    assert ctx_a["dates"][-1] == "2026-08-21", "当日 partial 应被剔除"
    assert len(ctx_a["closes"]) == len(df_a) - 1
    assert len(ctx_a["amounts"]) == len(ctx_a["closes"])
    assert len(ctx_a["highs"]) == len(ctx_a["closes"])
    assert len(ctx_a["lows"]) == len(ctx_a["closes"])
    assert ctx_a["day_amount_ratio"] > 0, "当日量能比应单独记录"

    # 情形 B：末根非当日（缓存命中，末根为昨日完整收盘）→ 不剔除，结果与 A 等价
    df_b = _make_gather_df("2026-08-21", n=70)
    ctx_b = rct.gather_context(df_b)
    assert ctx_b["dates"][-1] == "2026-08-21"
    assert len(ctx_b["closes"]) == len(df_b)
    # A 剔除后与 B 的信号信息集完全一致（日期/收盘/量能对齐）
    assert ctx_a["dates"] == ctx_b["dates"]
    assert ctx_a["closes"] == ctx_b["closes"]
    assert ctx_a["amounts"] == ctx_b["amounts"]
    # 情形 B 无当日量能比（末根为完整日，ratio 语义不存在）
    assert ctx_b["day_amount_ratio"] == 0.0


def test_nested_get():
    """点分路径取值：支持 raw.basis_min_ap；任一环缺失返回 None 不抛异常。"""
    h = {"raw": {"basis_min_ap": -11.2, "main_net": -86.4}}
    assert ct._nested_get(h, "raw.basis_min_ap") == -11.2
    assert ct._nested_get(h, "raw.main_net") == -86.4
    assert ct._nested_get(h, "raw.nonexist") is None
    assert ct._nested_get(h, "nonexist.x") is None
    assert ct._nested_get({}, "raw.pcr") is None
    assert ct._nested_get({"raw": None}, "raw.pcr") is None


def test_shadow_ic_includes_raw_fields():
    """shadow_ic 应支持 raw 嵌套字段，且 None 样本自动剔除（缺源不算 0）。"""
    hist = [
        {"date": "2026-08-10", "core": 0.1, "next_ret": 0.01,
         "raw": {"basis_min_ap": -5.0, "main_net": 10.0}},
        {"date": "2026-08-11", "core": 0.2, "next_ret": 0.02,
         "raw": {"basis_min_ap": -8.0, "main_net": None}},
        {"date": "2026-08-12", "core": 0.3, "next_ret": 0.03,
         "raw": {"basis_min_ap": -12.0, "main_net": -30.0}},
        {"date": "2026-08-13", "core": 0.4, "next_ret": 0.04,
         "raw": {"basis_min_ap": -15.0, "main_net": None}},
        {"date": "2026-08-14", "core": 0.5, "next_ret": 0.05,
         "raw": {"basis_min_ap": -20.0, "main_net": -80.0}},
        {"date": "2026-08-15", "core": 0.6, "next_ret": 0.06,
         "raw": {"basis_min_ap": -25.0, "main_net": -100.0}},
        {"date": "2026-08-16", "core": 0.7, "next_ret": 0.07,
         "raw": {"basis_min_ap": -30.0, "main_net": -120.0}},
        {"date": "2026-08-17", "core": 0.8, "next_ret": 0.08,
         "raw": {"basis_min_ap": -35.0, "main_net": -140.0}},
        {"date": "2026-08-18", "core": 0.9, "next_ret": 0.09,
         "raw": {"basis_min_ap": -40.0, "main_net": -160.0}},
        {"date": "2026-08-19", "core": 1.0, "next_ret": 0.10,
         "raw": {"basis_min_ap": -45.0, "main_net": -180.0}},
    ]
    ic = ct.shadow_ic(hist)
    # raw.basis_min_ap 全部 10 条非 None
    assert ic["raw.basis_min_ap"]["n"] == 10
    # raw.main_net 只有 8 条（None 的 2 条被剔除）
    assert ic["raw.main_net"]["n"] == 8
    # basis_min_ap 单调递减（-5→-45）而收益单调递增 → 完美负相关
    assert ic["raw.basis_min_ap"]["h1"]["ic"] < -0.9


def test_layer_ic_monotonic_and_insufficient():
    """分层单调性：单调因子 spread 为正且 monotone=True；样本不足 ok=False。"""
    hist = [
        {"date": f"d{i}", "next_ret": 0.01 * i,
         "raw": {"basis_min_ap": -1.0 * i}}
        for i in range(1, 31)      # 30 条：因子越低 → 收益越高（负相关）
    ]
    lay = ct.layer_ic(hist, "raw.basis_min_ap")
    assert lay["ok"] is True
    assert lay["n"] == 30
    # 因子（负值递增）vs 收益（递增）：组1因子最负（-1）、收益最低 → 负相关
    assert lay["spread"] < 0
    assert lay["monotone"] is True

    # 样本不足
    small = hist[:5]
    lay2 = ct.layer_ic(small, "raw.basis_min_ap")
    assert lay2["ok"] is False
