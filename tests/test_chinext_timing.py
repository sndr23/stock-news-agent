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
    assert "风控封顶" in "".join(dec["note"])


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


def test_hysteresis_hold_is_not_reported_as_risk_cap():
    """滞回带保档不能误报为硬风控封顶。"""
    prev = {"position": 1.0, "pending": None}

    decision = ct.decide_position(0.38, 1.0, prev)

    assert decision["position"] == 1.0
    assert "风控封顶" not in "".join(decision["note"])
    assert "滞回带确认" not in "".join(decision["note"])


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


def test_dimension_modifier_applies_documented_component_caps():
    """实时修正层的单项上限必须与生产说明一致，而非只靠总上限。"""
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))

    snapshot = {
        "ts": "2026-08-27 14:30",
        "basis": {"IC": {"annual_pct": 1.0}, "IM": {"annual_pct": 1.0}},
        "flows": {"main_net_yi": 150.0},
        "sector_flows": {"inflow": ["AI"], "outflow": []},
        "sentiment": {"mood": "亢奋"},
        "breadth": {"down_pct": 20.0},
        "option": {"pcr": 0.5},
    }
    ctx = {"events": [{"dir": "bullish", "title_norm": "AI"},
                       {"dir": "bullish", "title_norm": "半导体"},
                       {"dir": "bullish", "title_norm": "光模块"}],
           "snapshot_stale": False}

    result = rct._dimension_modifier(snapshot, ctx)

    assert result["basis"] == pytest.approx(0.03)
    assert result["flow"] == pytest.approx(0.05)
    assert result["mood"] == pytest.approx(0.04)
    assert result["news"] == pytest.approx(0.06)
    assert result["score"] == pytest.approx(0.18)


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


def test_update_shadow_history_audit_fields():
    """2026-08-27 审计扩展：commit 溯源/仓位轨迹/盘中涨幅/风控状态/缠论结构/分维归因。

    背景：08-27 实证——综合分 -0.41 空仓但盘中 +1.7%，回撤规则显示新旧版本不一致
    （远端未部署），事后想验证"低分+盘中大涨后 N 日表现"却缺字段。补齐后
    shadow_history 可直接回答信号审计四问，不另起口径。
    """
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    from run_chinext_timing import update_shadow_history as ush

    res = {"core": {"score": -0.42, "signals": {
                "trend_ma20_60": -1.0, "volprice_quadrant": 0.2,
                "vol_regime": -0.58, "pullback_52w": -0.82, "dd60": -1.0}},
           "mods": {"basis": -0.04, "flow": 0.0, "mood": 0.0, "news": 0.03,
                    "chan": {"score": -0.04, "bustop": True, "last_signal": "B2"},
                    "stock": {"score": 0.0}},
           "score": -0.41,
           "caps": {"cap": 0.6, "triggers": ["距60日高点回撤-21.9%封顶6成"]}}
    ctx = {"closes": [100.0, 101.0], "dates": ["2026-08-26", "2026-08-27"],
           "overseas_drop": 0.0, "intraday": 1.7}
    state = {"history": []}
    ush(state, ctx, "2026-08-27", res["score"], res, 0.0, 0.0)
    h = state["history"][-1]
    assert h["commit"], "commit 溯源字段非空（git 或 unknown 兜底）"
    assert h["prev_pos"] == 0.0 and h["position"] == 0.0
    assert h["intraday_pct"] == 1.7
    assert h["cap"] == 0.6
    assert h["cap_triggers"] == ["距60日高点回撤-21.9%封顶6成"]
    assert h["chan_bustop"] is True
    assert h["chan_last_signal"] == "B2"
    assert h["sig"]["dd60"] == -1.0 and h["sig"]["trend_ma20_60"] == -1.0

    # 旧签名（6 参）兼容：prev_pos 缺省 None 不抛错
    state2 = {"history": []}
    ush(state2, ctx, "2026-08-27", res["score"], res, 0.0)
    assert state2["history"][-1]["prev_pos"] is None


def test_shadow_probes_recorded():
    """P3 影子探针：rebound/low_repair 只记录不改仓位，条件边界正确。"""
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    from run_chinext_timing import _shadow_probes

    # 深回撤序列：60 日高点 100 → 现价 78（dd=-22%）
    closes = ([100.0] * 55) + [90.0, 85.0, 80.0, 79.0, 78.0]
    res_vp_pos = {"core": {"signals": {"volprice_quadrant": 0.5}},
                  "mods": {"chan": {"bustop": False}}}
    ctx = {"closes": closes, "intraday": 1.7}
    p = _shadow_probes(ctx, res_vp_pos)
    assert p["deep_dd"] is True
    # closes[-5:] = [90,85,80,79,78] 均值 82.4 > 现价 78 → 价在5日线下方，非低位修复
    assert p["low_repair"] is False
    # rebound：深回撤+盘中1.7%+量价正+无顶背驰 → True
    assert p["rebound"] is True

    # 顶背驰在场 → rebound 熄火（风控优先，与 P1 修复同语义）
    res_bustop = {"core": {"signals": {"volprice_quadrant": 0.5}},
                  "mods": {"chan": {"bustop": True}}}
    assert _shadow_probes(ctx, res_bustop)["rebound"] is False

    # 无深回撤 → 两探针全 False（条件前置不满足）
    ctx_up = {"closes": [100.0 + i for i in range(60)], "intraday": 1.7}
    p2 = _shadow_probes(ctx_up, res_vp_pos)
    assert p2["deep_dd"] is False and p2["rebound"] is False and p2["low_repair"] is False


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


def test_stale_factor_snapshot_does_not_change_modifier_score():
    """因子快照停更时，旧贴水/资金值不得继续改变当日综合分。"""
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))

    stale_snapshot = {
        "basis": {"IC": {"annual_pct": -20.0}},
        "flows": {"main_net_yi": -150.0},
    }
    result = rct._dimension_modifier(
        stale_snapshot, {"events": [], "snapshot_stale": True})

    assert result["score"] == 0.0
    assert result["basis"] == 0.0
    assert result["flow"] == 0.0


@pytest.mark.parametrize("snapshot_ts", [None, "2026-08-27", "bad timestamp",
                                          "2026-08-26 15:53"],
                         ids=["missing", "date-only", "invalid", "yesterday"])
def test_snapshot_without_valid_today_timestamp_is_stale(snapshot_ts):
    """有增强数据但无严格当日采集时间时，必须整体失效而不是继续改分。"""
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    from run_chinext_timing import _snapshot_is_stale

    snapshot = {"basis": {"IC": {"annual_pct": -12.0}}}
    if snapshot_ts is not None:
        snapshot["ts"] = snapshot_ts

    assert _snapshot_is_stale(snapshot, "2026-08-27") is True
    assert _snapshot_is_stale({}, "2026-08-27") is False


def test_snapshot_with_canonical_today_timestamp_is_fresh():
    """采集器写入的标准 YYYY-MM-DD HH:MM 当日时间戳可以通过校验。"""
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    from run_chinext_timing import _snapshot_is_stale

    assert _snapshot_is_stale({"ts": "2026-08-27 15:53",
                               "basis": {}}, "2026-08-27") is False


def test_timing_date_helpers_use_beijing_date_at_utc_midnight_boundary(monkeypatch):
    """GitHub Runner UTC 次日凌晨仍应按北京时间的交易日处理。"""
    import datetime as _dt

    instant = _dt.datetime(2026, 8, 28, 16, 30, tzinfo=_dt.timezone.utc)

    class _FakeDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(rct, "datetime", _FakeDateTime)

    assert rct._is_trading_day() is False  # BJT = 2026-08-29, Saturday


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


def test_gather_context_keeps_intraday_snapshot(monkeypatch):
    """v5.1 口径：末根为当日（14:45 partial）→ 保留作为当日快照，核心层用当日数据。

    背景（2026-08-28 用户拍板）：信号日 d 直接用 d 日 14:45 快照（当日盘中价/量能），
    而非 d-1 收盘——均线/动量/量价反映"当天到现在的走势"，对当日加减仓更有意义；
    14:45→15:00 的 15 分钟价差接受为近似。
    """
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
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

    # 情形 A：末根为当日（partial）→ 保留，末根仍为当日（v5.1 口径）
    df_a = _make_gather_df("2026-08-24", n=71)
    ctx_a = rct.gather_context(df_a)
    assert ctx_a["dates"][-1] == "2026-08-24", "当日快照应保留（v5.1 口径）"
    assert len(ctx_a["closes"]) == len(df_a)
    assert len(ctx_a["amounts"]) == len(ctx_a["closes"])
    assert len(ctx_a["highs"]) == len(ctx_a["closes"])
    assert len(ctx_a["lows"]) == len(ctx_a["closes"])
    assert ctx_a["day_amount_ratio"] > 0, "当日量能比应单独记录"

    # 情形 B：末根非当日（缓存命中，末根为昨日完整收盘）→ 不剔除，末根仍为昨日
    df_b = _make_gather_df("2026-08-21", n=70)
    ctx_b = rct.gather_context(df_b)
    assert ctx_b["dates"][-1] == "2026-08-21"
    assert len(ctx_b["closes"]) == len(df_b)
    # 情形 B 无当日量能比（末根为完整日，ratio 语义不存在）
    assert ctx_b["day_amount_ratio"] == 0.0


def test_gather_context_uses_latest_valid_citic_history(monkeypatch):
    """中信持仓历史无序时，择时上下文应使用最近有效日期而非列表末项。"""
    import datetime as _dt

    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 24, 14, 45)

    monkeypatch.setattr(rct, "datetime", _FakeDT)
    monkeypatch.setattr(rct, "get_quotes", lambda *a, **k: {})
    monkeypatch.setattr(rct.nl, "load_factor_state", lambda: {})
    monkeypatch.setattr(rct.nl, "load_citic_pos_state", lambda: {
        "pos_history": [
            {"day": "2026-08-20", "net": {"_total": 900}},
            {"day": "2026-08-23", "net": {"_total": -1800}},
        ]
    })
    monkeypatch.setattr(rct.nl, "load_realtime_state", lambda: {})
    monkeypatch.setattr(rct.ovs, "load_overseas", lambda *a, **k: {})
    monkeypatch.setattr(rct, "load_stock_sina", lambda *a, **k: None)
    monkeypatch.setattr(rct, "_load_erp_basis", lambda *a, **k: None)

    ctx = rct.gather_context(_make_gather_df("2026-08-21"))

    assert ctx["citic_day"] == "2026-08-23"
    assert ctx["citic_net"] == -1800.0


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


# ---------------- 顶背驰优先（2026-08-27）+ 报告溯源 ----------------

def _import_rct():
    import sys as _sys
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parent.parent
    if str(_root / "scripts") not in _sys.path:
        _sys.path.insert(0, str(_root / "scripts"))
    return rct


def test_chan_bustop_overrides_bullish_proxies():
    """2026-08-27 实证：顶背驰+B2+笔向上+upper 净 +0.01（买侧代理分抵消否决级）。

    修复后：bustop 时买点降级观察不加分，且保底 chan 分 ≤ -0.04——
    顶背驰是卖侧否决信号，不得被同源买侧代理拉回正值。
    """
    rct = _import_rct()
    ctx = {"highs": [1.0] * 40, "lows": [0.9] * 40, "closes": [1.0] * 40}
    fake = {"bustop": True, "last_signal": "B2", "bi_dir": "up",
            "zone": "upper", "trend_ok": True, "divergence": "top"}
    orig = rct.ch.chan_state

    def _fake(hh, ll, cc):
        return fake
    rct.ch.chan_state = _fake
    try:
        out = rct._chan_signal(ctx)
    finally:
        rct.ch.chan_state = orig
    assert out["score"] <= -0.04, "顶背驰下缠论分必须为负（否决保底）"
    assert "降级观察" in out["detail"], "B2 应标注降级观察而非直接给买侧加分"
    assert "顶背驰" in out["detail"]


def test_chan_b2_without_bustop_unchanged():
    """无顶背驰时 B2 买侧加分路径保持原状（回归保护）。"""
    rct = _import_rct()
    ctx = {"highs": [1.0] * 40, "lows": [0.9] * 40, "closes": [1.0] * 40}
    fake = {"bustop": False, "last_signal": "B2", "bi_dir": "up",
            "zone": "upper", "trend_ok": True, "divergence": "none"}
    orig = rct.ch.chan_state

    def _fake(hh, ll, cc):
        return fake
    rct.ch.chan_state = _fake
    try:
        out = rct._chan_signal(ctx)
    finally:
        rct.ch.chan_state = orig
    # 0.02(B2) + 0.03(笔向上) + 0.02(upper) = +0.07
    assert out["score"] == 0.07
    assert "降级观察" not in out["detail"]


def test_render_report_has_commit_and_cap_note():
    """报告含代码版本（分清信号问题 vs 远端未部署）与非生效风控澄清行。"""
    rct = _import_rct()
    res = {
        "score": -0.41,
        "core": {"score": -0.42, "signals": {
            "trend_ma20_60": -1.0, "trend_momentum_60": -0.79,
            "volprice_quadrant": 0.2, "volprice_amihud": 0.51,
            "vol_regime": -0.58, "vol_term": 0.2, "value_erp": 0.0,
            "pullback_52w": -0.82, "dd60": -1.0}},
        "mods": {"basis": -0.04, "flow": 0.0, "mood": 0.0, "news": 0.03,
                 "chan": {"score": -0.04, "bustop": True, "bi_dir": "up",
                          "zone": "upper", "last_signal": "B2",
                          "detail": "缠论:顶背驰,B2降级观察,笔向上"},
                 "stock": {"score": 0.0, "detail": "跳过"}},
        "caps": {"cap": 0.3, "triggers": ["距60日高点回撤-21.9%封顶3成"]},
    }
    ctx = {"intraday": 1.7, "day_amount_ratio": 0.0, "overseas_drop": 0.0}
    dec = {"position": 0.0, "changed": False, "direction": "hold", "note": []}
    txt = rct.render_report("2026-08-27", res, ctx, dec, prev_pos=0.0)
    assert "（代码 " in txt, "报告应携带代码版本号"
    assert "未实际生效" in txt, "档位未越封顶线时应澄清风控非空仓主因"
    assert "硬风控仅限上限" in txt


def test_render_report_shows_index_data_window():
    """推送正文应显示核心信号实际使用的完整日线窗口。"""
    rct = _import_rct()
    res = {
        "score": 0.0,
        "core": {"score": 0.0, "signals": {
            "trend_ma20_60": 0.0, "trend_momentum_60": 0.0,
            "volprice_quadrant": 0.0, "volprice_amihud": 0.0,
            "vol_regime": 0.0, "vol_term": 0.0, "value_erp": 0.0,
            "pullback_52w": 0.0, "dd60": 0.0}},
        "mods": {"basis": 0.0, "flow": 0.0, "mood": 0.0, "news": 0.0,
                 "chan": {"score": 0.0, "detail": "缠论:中性"},
                 "stock": {"score": 0.0, "detail": "跳过"}},
        "caps": {"cap": 1.0, "triggers": []},
    }
    ctx = {"intraday": 0.0, "overseas_drop": 0.0,
           "history_bars": 1857, "history_last_date": "2026-08-26"}
    dec = {"position": 0.0, "changed": False, "direction": "hold", "note": []}

    txt = rct.render_report("2026-08-27", res, ctx, dec, prev_pos=0.0)

    assert "399006完整日线：1857根，截至2026-08-26" in txt


def test_render_report_mods_health_marker():
    """2026-08-27 数据健康度：快照源缺失时修正行标注(缺)，区分"真实中性"与"降级0"。

    背景：08-27 推送资金/情绪恒 +0.00，无从判断是快照缺失还是真中性；
    且快照整体缺失（ts 空）时 stale 告警也不触发——双盲。标注后一眼可辨。
    """
    rct = _import_rct()
    res = {
        "score": -0.41,
        "core": {"score": -0.42, "signals": {
            "trend_ma20_60": -1.0, "trend_momentum_60": -0.79,
            "volprice_quadrant": 0.2, "volprice_amihud": 0.51,
            "vol_regime": -0.58, "vol_term": 0.2, "value_erp": 0.0,
            "pullback_52w": -0.82, "dd60": -1.0}},
        "mods": {"basis": 0.0, "flow": 0.0, "mood": 0.0, "news": 0.0,
                 "chan": {"score": 0.0, "detail": "缠论:中性"},
                 "stock": {"score": 0.0, "detail": "跳过"}},
        "caps": {"cap": 1.0, "triggers": []},
    }
    dec = {"position": 0.0, "changed": False, "direction": "hold", "note": []}

    # 快照整体缺失 → 三项全标(缺)
    ctx = {"intraday": 1.7, "day_amount_ratio": 0.0, "overseas_drop": 0.0}
    txt = rct.render_report("2026-08-27", res, ctx, dec, prev_pos=0.0)
    for k in ("贴水+0.00(缺)", "资金+0.00(缺)", "情绪+0.00(缺)"):
        assert k in txt, f"缺源应标注: {k}"

    # 快照齐全 → 无(缺)标注（真实中性）
    ctx_full = {"intraday": 1.7, "day_amount_ratio": 0.0, "overseas_drop": 0.0,
                "snapshot": {"basis": {"IC": {"annual_pct": -5.0}},
                             "flows": {"main_net_yi": -10.0},
                             "breadth": {"down_pct": 50.0}}}
    txt2 = rct.render_report("2026-08-27", res, ctx_full, dec, prev_pos=0.0)
    assert "(缺)" not in txt2

    # 快照停更（stale）→ 修正行不标(缺)（⚠ 整体告警行已覆盖，避免双重标注）
    ctx_stale = {"intraday": 1.7, "day_amount_ratio": 0.0,
                 "overseas_drop": 0.0, "snapshot_stale": True,
                 "snapshot": {}}
    txt3 = rct.render_report("2026-08-27", res, ctx_stale, dec, prev_pos=0.0)
    assert "(缺)" not in txt3


# ---------------- 策略失效熔断（P1-2，2026-08-29） ----------------

import sys as _sys
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent
if str(_ROOT / "scripts") not in _sys.path:
    _sys.path.insert(0, str(_ROOT / "scripts"))
import run_chinext_timing as rct  # noqa: E402

def _hist(rows):
    """构造影子 history：[{date, position, next_ret}, ...]"""
    return [{"date": f"2026-01-{i + 1:02d}", "position": p, "next_ret": r}
            for i, (p, r) in enumerate(rows)]


def test_cumulative_nav_tracks_strategy_and_benchmark():
    """策略净值 = Π(1+pos×ret)；基准 = Π(1+ret)；未回填记录跳过。"""
    h = _hist([(0.0, 0.10), (1.0, -0.10), (0.5, 0.10)])
    h.append({"date": "2026-01-04", "position": 1.0, "next_ret": None})
    nav, bh = rct._cumulative_nav(h)
    # 基准：(1.10)(0.90)(1.10)=1.089；策略：1.0×0.90×1.05=0.945
    assert bh == pytest.approx(1.089, abs=1e-4)
    assert nav == pytest.approx(0.945, abs=1e-4)


def test_strategy_health_stays_silent_on_insufficient_samples():
    """样本不足时必须不告警，避免小样本误报（安全默认）。"""
    h = _hist([(0.0, 0.05)] * 5)
    res = rct.strategy_health(h, window=20)
    assert res["ok"] is True
    assert res["level"] == "insufficient"


def test_strategy_health_alerts_when_lagging_benchmark():
    """滚动窗口内策略落后基准 ≥ 阈值 → 告警（不改仓位，只提示）。"""
    # 20 个交易日均上涨 1%，但策略始终空仓 → 落后基准约 22pp
    h = _hist([(0.0, 0.01)] * 20)
    res = rct.strategy_health(h, window=20, lag_gate=0.10)
    assert res["level"] == "alert"
    assert any("落后基准" in r for r in res["reasons"])
    assert res["stats"]["lag"] > 0.10


def test_strategy_health_alerts_on_drawdown():
    """滚动窗口内策略回撤 ≥ 阈值 → 告警。"""
    rows = [(1.0, 0.02)] * 5 + [(1.0, -0.05)] * 15   # 满仓连跌 → 回撤 >15%
    res = rct.strategy_health(_hist(rows), window=20, dd_gate=0.15)
    assert res["level"] == "alert"
    assert any("回撤" in r for r in res["reasons"])


def test_strategy_health_ok_when_tracking_benchmark():
    """策略与基准同步时不应告警。"""
    h = _hist([(1.0, 0.01)] * 20)
    res = rct.strategy_health(h, window=20)
    assert res["level"] == "ok" and res["ok"] is True
