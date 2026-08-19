# -*- coding: utf-8 -*-
"""factor_collector 核心纯函数单元测试（不触网）"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factor_collector import (  # noqa: E402
    calc_tech_factors,
    calc_basis,
    detect_anomalies,
    filter_by_cooldown,
    _ma,
)

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用


def _make_klines(n=65, base=100, step=1.0, vol=100):
    """构造递增收盘价日K：close[i] = base + i*step"""
    out = []
    for i in range(n):
        c = base + i * step
        out.append({
            "date": f"2026-01-{i+1:02d}",
            "open": c - 0.5,
            "close": c,
            "high": c + 1.0,
            "low": c - 1.0,
            "volume": vol,
        })
    return out


def test_ma():
    assert _ma([1, 2, 3, 4, 5], 5) == 3.0
    assert _ma([1, 2, 3], 5) == 0.0  # 长度不足返回 0


def test_calc_tech_factors_trend_and_ma():
    klines = _make_klines()
    quote = {"price": 164.0, "change_pct": 0.5, "prev_close": 163.0, "amount_wan": 1.0}
    f = calc_tech_factors("上证指数", klines, quote)
    assert f["available"] is True
    assert abs(f["ma5"] - 162.0) < 1e-6
    assert abs(f["ma10"] - 159.5) < 1e-6
    assert abs(f["ma20"] - 154.5) < 1e-6
    assert abs(f["ma60"] - 134.5) < 1e-6
    assert f["trend"] == "多头排列"


def test_calc_tech_factors_breakout_and_volume():
    klines = _make_klines()
    # 最后一根：high 突破前20日高点，volume 放量 2x
    klines[-1]["high"] = 165.0
    klines[-1]["volume"] = 200
    quote = {"price": 164.0, "change_pct": 0.5, "prev_close": 163.0, "amount_wan": 1.0}
    f = calc_tech_factors("上证指数", klines, quote)
    assert f["breakout"] is True
    assert abs(f["vol_ratio5"] - 2.0) < 1e-6


def test_calc_tech_factors_unavailable():
    assert calc_tech_factors("上证指数", [], {})["available"] is False


def test_calc_basis():
    futures = {"IF": {"price": 4650.0, "prev_settle": 4640.0}}
    quotes = {"沪深300": {"price": 4625.0}}
    b = calc_basis(futures, quotes)
    assert abs(b["IF"]["basis"] - 25.0) < 1e-6
    assert abs(b["IF"]["basis_pct"] - 0.541) < 1e-2  # 25/4625*100 ≈ 0.5405


def test_calc_basis_discount():
    futures = {"IC": {"price": 7800.0, "prev_settle": 7900.0}}
    quotes = {"中证500": {"price": 7900.0}}
    b = calc_basis(futures, quotes)
    assert b["IC"]["basis"] < 0  # 贴水


def _base_tech(breakout=False, breakdown=False, vol_ratio5=1.0):
    return {
        "上证指数": {
            "name": "上证指数", "available": True,
            "price": 3900.0, "change_pct": 0.1,
            "breakout": breakout, "breakdown": breakdown,
            "vol_ratio5": vol_ratio5,
        },
        "创业板指": {
            "name": "创业板指", "available": True,
            "price": 3600.0, "change_pct": 0.2,
            "breakout": False, "breakdown": False,
            "vol_ratio5": 1.0,
        },
    }


def _hist(values):
    """构造按交易日采样的贴水历史（过去固定日期，保证运行时被 append 而非更新当日样本）"""
    return [{"d": f"2026-08-{i+1:02d}", "v": v} for i, v in enumerate(values)]


def test_detect_basis_spread_relative():
    # 当前贴水率创 20 日最深（-1.6 < 历史最深 -1.1）→ 触发
    basis = {"IC": {"index": "中证500", "fut": 7800, "spot": 7900, "basis": -100, "basis_pct": -1.6, "annual_pct": -16.6, "remaining_days": 35}}
    history = {"IC": _hist([-1.0, -0.9, -1.1, -1.0, -0.8])}
    signals, new_history = detect_anomalies(_base_tech(), basis, {}, history)
    assert any(s["key"] == "basis_IC" for s in signals)
    assert new_history["IC"][-1]["v"] == -1.6  # 当前值已追加（交易日采样）
    assert len(new_history["IC"]) == 6


def test_detect_basis_normal_no_alert():
    # 当前贴水 -1.2 未创 20 日最深（历史最深 -1.3）→ 不触发（常态贴水不误报）
    basis = {"IC": {"index": "中证500", "fut": 7800, "spot": 7900, "basis": -100, "basis_pct": -1.2, "annual_pct": -12.5, "remaining_days": 35}}
    history = {"IC": _hist([-1.0, -1.3, -1.1, -1.0, -0.8])}
    signals, _ = detect_anomalies(_base_tech(), basis, {}, history)
    assert not any(s["key"] == "basis_IC" for s in signals)


def test_detect_basis_same_day_update():
    # 同日多次采样应更新最后样本而非追加（P0 交易日采样：1 天仅 1 样本）
    basis = {"IC": {"index": "中证500", "fut": 7800, "spot": 7900, "basis": -100, "basis_pct": -1.4, "annual_pct": -14.0, "remaining_days": 35}}
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    history = {"IC": _hist([-1.0, -1.1]) + [{"d": today, "v": -1.2}]}  # 最后是今天
    signals, new_history = detect_anomalies(_base_tech(), basis, {}, history)
    assert new_history["IC"][-1]["v"] == -1.4  # 今天样本被更新
    assert len(new_history["IC"]) == 3  # 不追加


def test_detect_basis_history_insufficient():
    # 历史序列不足 5 个样本 → 不触发（避免冷启动误报）
    basis = {"IC": {"index": "中证500", "fut": 7800, "spot": 7900, "basis": -100, "basis_pct": -2.0, "annual_pct": -20.0, "remaining_days": 35}}
    history = {"IC": _hist([-1.0, -0.9])}
    signals, _ = detect_anomalies(_base_tech(), basis, {}, history)
    assert not any(s["key"] == "basis_IC" for s in signals)


def test_detect_fx_jpy_alert():
    fx = {"fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": -2.0}}
    signals, _ = detect_anomalies(_base_tech(), {}, fx)
    assert any(s["key"] == "fx_usdjpy" and s["direction"] == "bearish" for s in signals)


def test_detect_breakout_volume():
    tech = _base_tech(breakout=True, vol_ratio5=1.6)
    signals, _ = detect_anomalies(tech, {}, {})
    assert any(s["key"] == "breakout_上证指数" for s in signals)


def test_detect_breakdown_volume():
    tech = _base_tech(breakdown=True, vol_ratio5=1.8)
    signals, _ = detect_anomalies(tech, {}, {})
    assert any(s["key"] == "breakdown_上证指数" for s in signals)


def test_calc_risk_state():
    from factor_collector import calc_risk_state
    # 任一 warning 信号 → risk_off
    assert calc_risk_state([{"level": "warning", "key": "basis_IC"}]) == "risk_off"
    assert calc_risk_state([{"level": "info", "key": "breakout_上证指数"}]) == "neutral"
    assert calc_risk_state([]) == "neutral"


def test_calc_basis_direction():
    from factor_collector import _calc_basis_direction
    # 贴水逐期加深（更负）→ 走扩；逐期变浅 → 收敛；不单调/样本不足 → 走平
    assert _calc_basis_direction({"IC": [-1.0, -1.2, -1.4]})["IC"] == "走扩"
    assert _calc_basis_direction({"IC": [-1.4, -1.2, -1.0]})["IC"] == "收敛"
    assert _calc_basis_direction({"IC": [-1.2, -1.1, -1.3]})["IC"] == "走平"
    assert _calc_basis_direction({"IC": [-1.0, -1.1]})["IC"] == "走平"


def test_direction_analysis_bullish():
    from factor_collector import _direction_analysis
    # 贴水收敛(中性加仓) + 风险中性 + 放量突破 + 普涨宽度 → 偏多
    # P3 后 6 维多数表决：+1票需过半（3/6），单靠对冲+量价两票被中性维度稀释为 0.33
    tech = _base_tech(breakout=True, vol_ratio5=1.6)
    fx = {"fx_susdjpy": {"name": "美元/日元", "price": 159.0, "change_pct": -0.3}}
    history = {"IC": [-1.4, -1.2, -1.0], "IM": [-1.5, -1.3, -1.1]}
    a = _direction_analysis(tech, {}, fx, "neutral", history,
                            breadth={"down_pct": 15.0})
    assert a["direction"] == "偏多"
    assert a["score"] > 0


def test_direction_analysis_bearish():
    from factor_collector import _direction_analysis
    # 贴水走扩(中性减仓) + risk_off + 放量破位 + 日元急升 → 偏空
    tech = _base_tech(breakdown=True, vol_ratio5=1.8)
    fx = {"fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": -2.0}}
    history = {"IC": [-1.0, -1.2, -1.4], "IM": [-1.1, -1.3, -1.5]}
    a = _direction_analysis(tech, {}, fx, "risk_off", history)
    assert a["direction"] == "偏空"
    assert a["score"] < 0


def test_direction_changed():
    from factor_collector import _direction_changed
    assert _direction_changed({"direction": "偏多"}, "中性") is True
    assert _direction_changed({"direction": "偏多"}, "偏空") is True
    assert _direction_changed({"direction": "偏多"}, "偏多") is False
    assert _direction_changed({"direction": "偏多"}, "") is False  # 首次无基准


def test_next_expiry_days():
    from datetime import date
    from factor_collector import _next_expiry_days, _third_friday
    # 2026-08-14：下月第三个周五 = 9/18，剩余 35 天
    assert _third_friday(2026, 8).isoformat() == "2026-08-21"
    assert _next_expiry_days(date(2026, 8, 14)) == 35


def test_is_trading_time():
    from datetime import datetime
    from factor_collector import _is_trading_time
    # 交易时段 10:30 → True；午休 12:00 → False；周末 → False；非交易时段 20:00 → False
    assert _is_trading_time(datetime(2026, 8, 14, 10, 30)) is True   # 周五盘中
    assert _is_trading_time(datetime(2026, 8, 14, 13, 30)) is True   # 周五下午
    assert _is_trading_time(datetime(2026, 8, 14, 12, 0)) is False   # 午休
    assert _is_trading_time(datetime(2026, 8, 14, 20, 0)) is False   # 盘后
    assert _is_trading_time(datetime(2026, 8, 15, 10, 30)) is False  # 周六


def test_filter_by_cooldown():
    state = {"cooldown": {}}
    signals = [{"key": "k1", "title": "x"}]
    # 第一次：放行
    assert len(filter_by_cooldown(signals, state)) == 1
    assert "k1" in state["cooldown"]
    # 冷却期内：拦截
    assert len(filter_by_cooldown([{"key": "k1", "title": "x"}], state)) == 0
    # 新 key：放行
    assert len(filter_by_cooldown([{"key": "k2", "title": "y"}], state)) == 1
