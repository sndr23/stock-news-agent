# -*- coding: utf-8 -*-
"""factor_collector 核心纯函数单元测试（不触网）"""
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factor_collector import (  # noqa: E402
    calc_tech_factors,
    calc_basis,
    detect_anomalies,
    filter_by_cooldown,
    record_source_health,
    mark_source_health_alerted,
    run_once,
    _ma,
)

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用


def _make_klines(n=65, base=100, step=1.0, vol=100):
    """构造递增收盘价日K：close[i] = base + i*step"""
    out = []
    dates = pd.bdate_range("2026-01-01", periods=n)
    for i in range(n):
        c = base + i * step
        out.append({
            "date": dates[i].strftime("%Y-%m-%d"),
            "open": c - 0.5,
            "close": c,
            "high": c + 1.0,
            "low": c - 1.0,
            "volume": vol,
        })
    return out


def test_load_realtime_state_does_not_fallback_to_local_on_gist_failure(
        tmp_path, monkeypatch):
    """Gist 已配置但读取失败时，不应使用本地旧资讯状态。"""
    import factor_collector as fc

    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(fc, "_REALTIME_STATE_PATH", tmp_path / "real_time_state.json")
    fc._REALTIME_STATE_PATH.write_text(
        '{"seen": {"old": {"pushed": true}}}', encoding="utf-8")

    def _fail(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(fc.requests, "get", _fail)

    assert fc._load_realtime_state() == {}


def test_load_realtime_state_rejects_non_object_local_state(tmp_path, monkeypatch):
    """本地资讯状态根节点不是对象时，联动应安全降级为空。"""
    import factor_collector as fc

    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.setattr(fc, "_REALTIME_STATE_PATH", tmp_path / "real_time_state.json")
    fc._REALTIME_STATE_PATH.write_text("[]", encoding="utf-8")

    assert fc._load_realtime_state() == {}


def _install_collect_deps(monkeypatch):
    """mock 掉 run_once 全部数据/推送依赖，返回 spy 计数记录。"""
    import factor_collector as fc
    rec = {"save": 0, "push": 0, "snapshot": 0}

    def _save(state):
        rec["save"] += 1

    def _push(*a, **k):
        rec["push"] += 1
        return {"code": 0}

    def _build(*a, **k):
        rec["snapshot"] += 1
        return {"built": True}

    stubs = [
        ("fetch_index_quotes", lambda *a, **k: {}),
        ("fetch_fx", lambda *a, **k: {}),
        ("fetch_index_futures", lambda *a, **k: {}),
        ("fetch_index_kline", lambda *a, **k: []),
        ("calc_tech_factors", lambda *a, **k: {}),
        ("calc_vol_regime", lambda *a, **k: {"available": False}),
        ("calc_basis", lambda *a, **k: {}),
        ("monitor_stocks", lambda *a, **k: ((), {}, {})),
        ("fetch_market_flows", lambda *a, **k: {}),
        ("fetch_global_quotes", lambda *a, **k: {}),
        ("fetch_market_breadth", lambda *a, **k: {}),
        ("calc_style_rotation", lambda *a, **k: {}),
        ("fetch_zt_sentiment", lambda *a, **k: {}),
        ("fetch_sector_flows", lambda *a, **k: {}),
        ("fetch_liquidity", lambda *a, **k: {}),
        ("fetch_option_pcr", lambda *a, **k: {}),
        ("calc_daily_derived_factors", lambda *a, **k: {}),
        ("fetch_minute_kline", lambda *a, **k: []),
        ("calc_minute_factors", lambda *a, **k: {}),
        ("_load_state", lambda: {}),
        ("detect_anomalies", lambda *a, **k: ([], [])),
        ("detect_global_anomalies", lambda *a, **k: []),
        ("detect_breadth_anomalies", lambda *a, **k: []),
        ("detect_sentiment_anomalies", lambda *a, **k: []),
        ("detect_liquidity_anomalies", lambda *a, **k: []),
        ("detect_flow_anomalies", lambda *a, **k: []),
        ("detect_option_anomalies", lambda *a, **k: []),
        ("calc_risk_state", lambda *a, **k: "neutral"),
        ("format_snapshot", lambda *a, **k: "snap"),
        ("build_snapshot", _build),
        ("_save_state", _save),
        ("do_push", _push),
        # 2026-09-01 起方向打分在 persist（collect/push）块执行（此前只在 push
        # 块，云端停推后 direction_history 停更 4 天）——collect 测试需 mock
        # 方向合成，避免真实 _direction_analysis 依赖行情数据。
        ("_direction_analysis",
         lambda *a, **k: {"direction": "中性", "score": 0.0, "factors": []}),
        # 默认按交易日运行：避免周六/周日跑测试时 run_once 走进"非交易日跳过"
        # 分支，使断言依赖真实日历（需要非交易日的用例自行覆盖为 False）。
        ("_is_workday", lambda *a, **k: True),
    ]
    for name, f in stubs:
        monkeypatch.setattr(fc, name, f)
    return rec


def test_collect_writes_snapshot_state_not_push(monkeypatch):
    """--collect：写快照/状态（persist），绝不推送。修复停推回归的核心断言。"""
    rec = _install_collect_deps(monkeypatch)
    run_once(push=False, collect=True)
    assert rec["snapshot"] == 1
    assert rec["save"] == 1
    assert rec["push"] == 0


def test_collect_skips_state_write_on_non_trading_day(monkeypatch):
    """节假日采集不得用上一交易日数据刷新成当天新鲜快照。"""
    import factor_collector as fc

    rec = _install_collect_deps(monkeypatch)
    monkeypatch.setattr(fc, "_is_workday", lambda *args: False)

    result = run_once(push=False, collect=True)

    assert result["skipped"] is True
    assert result["reason"] == "non_trading_day"
    assert rec["snapshot"] == 0
    assert rec["save"] == 0
    assert rec["push"] == 0


def test_dryrun_is_pure_read_only(monkeypatch):
    """--dry-run（push=False, collect=False）：纯只读，不写快照/状态、不推送。"""
    rec = _install_collect_deps(monkeypatch)
    run_once(push=False, collect=False)
    assert rec["snapshot"] == 0
    assert rec["save"] == 0
    assert rec["push"] == 0


def test_run_once_keeps_health_alert_in_result_with_factor_push(monkeypatch):
    """健康告警与因子异动同轮发送时，返回审计结果不得丢失健康告警。"""
    import factor_collector as fc

    rec = _install_collect_deps(monkeypatch)
    monkeypatch.setattr(fc, "DATA_HEALTH_ALERT_ROUNDS", 1)
    monkeypatch.setattr(fc, "detect_anomalies", lambda *args, **kwargs: (
        [{"direction": "bullish", "title": "测试异动", "detail": "测试"}], {}))
    monkeypatch.setattr(fc, "filter_by_cooldown", lambda signals, state: signals)
    monkeypatch.setattr(fc, "do_push", lambda *args, **kwargs: (
        rec.__setitem__("push", rec["push"] + 1) or {"code": 200}))

    result = run_once(push=True)

    assert result["pushed"] == ["免费数据源连续失败告警", "测试异动"]
    assert rec["push"] >= 2


def test_console_configuration_replaces_unsupported_report_characters(monkeypatch):
    """Windows GBK 控制台输出快照中的告警符号时不应崩溃。"""
    import factor_collector as fc

    calls = {}

    class _Console:
        encoding = "gbk"

        def reconfigure(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(fc.sys, "stdout", _Console())

    fc._configure_stdout()

    assert calls == {"errors": "replace"}


def test_index_quotes_reject_nonfinite_price(monkeypatch):
    """腾讯指数响应含 NaN 价格时，不得作为有效行情返回。"""
    import factor_collector as fc

    parts = [""] * 38
    parts[1] = "上证指数"
    parts[3] = "nan"
    parts[4] = "3900"
    parts[32] = "0.1"
    parts[37] = "100"
    monkeypatch.setattr(
        fc, "_http_get", lambda *a, **k: 'v_sh000001="' + "~".join(parts) + '";')

    assert fc.fetch_index_quotes() == {}


def test_fx_rejects_nonfinite_price(monkeypatch):
    """新浪汇率响应含无穷价格时，不得进入增强修正层。"""
    import factor_collector as fc

    parts = [""] * 12
    parts[1] = "inf"
    parts[9] = "美元/日元"
    parts[11] = "0.1"
    monkeypatch.setattr(
        fc, "_http_get", lambda *a, **k: 'var hq_str_fx_susdjpy="' + ",".join(parts) + '";')

    assert fc.fetch_fx() == {}


def test_index_kline_rejects_nonfinite_last_bar(monkeypatch):
    """指数日K末根收盘无效时，应继续降级而不是把坏K线交给因子计算。"""
    import factor_collector as fc

    valid_dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=65)
    valid = [{"date": day.strftime("%Y-%m-%d"), "open": 100.0,
              "close": 101.0, "high": 102.0, "low": 99.0,
              "volume": 100.0} for day in valid_dates]
    invalid = [{"date": "2026-08-27", "open": 100.0, "close": float("nan"),
                "high": 102.0, "low": 99.0, "volume": 100.0}]
    monkeypatch.setattr(fc, "_fetch_kline_sina", lambda *a: invalid)
    monkeypatch.setattr(fc, "_fetch_kline_tencent", lambda *a: valid)

    assert fc.fetch_index_kline("sz399006", 65) == valid


def test_futures_sina_rejects_nonpositive_settlement(monkeypatch):
    """新浪期货昨结算为 0 时，应跳过该合约而不是产出伪基差。"""
    import factor_collector as fc

    parts = ["0", "0", "0", "4500", "0", "0"]
    monkeypatch.setattr(
        fc, "_http_get", lambda *a, **k: 'var hq_str_nf_IF0="' + ",".join(parts) + '";')

    assert fc._fetch_futures_sina() == {}


def test_global_quotes_em_rejects_nonfinite_values(monkeypatch):
    """东财全球指数返回 NaN 时，不得作为有效外盘读数。"""
    import factor_collector as fc
    import json

    payload = json.dumps({"data": {"diff": [
        {"f12": "KS11", "f2": float("nan"), "f3": 88}
    ]}})
    monkeypatch.setattr(fc, "_http_get", lambda *a, **k: payload)

    assert fc._fetch_global_quotes_em() == {}


def test_market_breadth_rejects_malformed_bucket(monkeypatch):
    """涨跌分布含损坏桶时，不能用剩余桶伪造完整市场宽度。"""
    import factor_collector as fc
    import json

    payload = json.dumps({"data": {"fenbu": [{"1": 100}, {"bad": 10}]}})
    monkeypatch.setattr(fc, "_http_get", lambda *a, **k: payload)

    assert fc.fetch_market_breadth() == {}


def test_sector_flows_reject_partial_rows(monkeypatch):
    """行业资金流缺少必要字段时，不得以部分行业标记源成功。"""
    import factor_collector as fc
    import json

    payload = json.dumps({"data": {"diff": [
        {"f14": "银行", "f62": 100000000},
        {"f14": "半导体"},
    ]}})
    monkeypatch.setattr(fc, "_http_get", lambda *a, **k: payload)

    assert fc.fetch_sector_flows() == {}


def test_liquidity_requires_both_expected_rates(monkeypatch):
    """资金面只返回 GC007 时，保留有效字段但不能算作完整成功。"""
    import factor_collector as fc

    fields = ["1", "GC007", "204007", "1.5", "1.4"] + [""] * 28
    fields[32] = "1"
    raw = 'v_sh204007="' + "~".join(fields) + '";'
    monkeypatch.setattr(fc, "_http_get", lambda *a, **k: raw)

    assert fc.fetch_liquidity() == {
        "gc007": {"price": 1.5, "change_pct": 1.0},
    }


def test_partial_fx_is_recorded_as_health_failure(tmp_path, monkeypatch):
    """汇率仅有一个品种时，仍可供展示但不能标记该维度健康。"""
    import factor_collector as fc

    _install_collect_deps(monkeypatch)
    captured = {}
    monkeypatch.setattr(fc, "build_snapshot",
                        lambda *a, **kw: captured.update(kw) or {"built": True})
    monkeypatch.setattr(fc, "detect_anomalies", lambda *a, **k: ([], {}))
    monkeypatch.setattr(fc, "fetch_fx", lambda: {
        "fx_susdjpy": {"name": "美元/日元", "price": 159.0, "change_pct": -0.3},
    })

    fc.run_once(push=True)

    assert captured["sources"]["ok"] == 0
    assert captured["sources"]["total"] == 14


def test_health_helpers_require_complete_structured_dimensions():
    """结构化增强维度部分返回时，健康度必须判定为失败。"""
    import factor_collector as fc

    assert not fc._complete_numeric_mapping(
        {"美元/日元": {"price": 159, "change_pct": -0.3}},
        fc.FX.values(), ("price", "change_pct"), positive_fields=("price",))
    assert not fc._complete_flows({"main_net_yi": 1, "margin_yi": 2})
    assert not fc._complete_option({"pcr": 1, "call_vol": 100,
                                    "put_vol": 100, "contracts": 2, "total": 4})
    assert fc._complete_breadth({
        "adv": 100, "dec": 80, "flat": 20, "down_pct": 40,
        "limit_up": 2, "limit_down": 1, "big_down": 5,
    })


def test_source_health_alerts_once_after_consecutive_failures():
    """免费源连续失败达到阈值才告警，未恢复前不重复生成告警。"""
    state = {}
    sources = {"指数K线": False, "汇率": True}

    assert record_source_health(state, sources, threshold=3) == []
    assert record_source_health(state, sources, threshold=3) == []
    assert record_source_health(state, sources, threshold=3) == ["指数K线"]
    mark_source_health_alerted(state, ["指数K线"])
    assert record_source_health(state, sources, threshold=3) == []
    assert state["source_health"]["指数K线"]["consecutive_failures"] == 4
    assert state["source_health"]["指数K线"]["alerted"] is True


def test_source_health_recovery_resets_counter_and_alert_latch():
    """源恢复后连续失败计数和告警锁存清零，下一轮故障可重新触发。"""
    state = {}
    failed = {"资金流": False}
    assert record_source_health(state, failed, threshold=2) == []
    assert record_source_health(state, failed, threshold=2) == ["资金流"]
    mark_source_health_alerted(state, ["资金流"])

    assert record_source_health(state, {"资金流": True}, threshold=2) == []
    rec = state["source_health"]["资金流"]
    assert rec["consecutive_failures"] == 0
    assert rec["alerted"] is False
    assert record_source_health(state, failed, threshold=2) == []
    assert record_source_health(state, failed, threshold=2) == ["资金流"]


def test_source_health_alert_can_be_sent_without_factor_push(monkeypatch):
    """健康告警开关独立于因子异动推送，采集模式也能触达告警出口。"""
    import factor_collector as fc

    state = {}
    monkeypatch.setattr(fc, "_load_state", lambda: state)
    monkeypatch.setattr(fc, "_save_state", lambda value: state.update(value))
    monkeypatch.setattr(fc, "DATA_HEALTH_ALERT_ROUNDS", 1)
    calls = []
    monkeypatch.setattr(fc, "do_push", lambda title, content: (
        calls.append((title, content)) or {"code": 200}))

    rec = {"指数K线": False}
    assert fc.maybe_push_source_health_alert(state, rec, allow_alert=True) is True
    assert calls and calls[0][0] == "免费数据源连续失败告警"
    assert state["source_health"]["指数K线"]["alerted"] is True


def test_source_health_alert_is_suppressed_when_not_allowed(monkeypatch):
    """dry-run 等只读模式不能改变状态，也不能发送健康告警。"""
    import factor_collector as fc

    state = {}
    monkeypatch.setattr(fc, "DATA_HEALTH_ALERT_ROUNDS", 1)
    monkeypatch.setattr(fc, "do_push", lambda *args: pytest.fail("dry-run 不应推送"))
    assert fc.maybe_push_source_health_alert(state, {"指数K线": False}, allow_alert=False) is False
    assert state == {}


def test_source_health_failed_delivery_does_not_latch(monkeypatch):
    """健康告警发送失败时不锁存，下一轮仍应继续尝试通知。"""
    import factor_collector as fc

    state = {}
    monkeypatch.setattr(fc, "DATA_HEALTH_ALERT_ROUNDS", 1)
    monkeypatch.setattr(fc, "do_push", lambda *args: {"code": 500})
    assert fc.maybe_push_source_health_alert(state, {"指数K线": False}, allow_alert=True) is False
    rec = state["source_health"]["指数K线"]
    assert rec["consecutive_failures"] == 1
    assert rec["alerted"] is False
    assert fc.maybe_push_source_health_alert(state, {"指数K线": False}, allow_alert=True) is False
    assert rec["consecutive_failures"] == 2


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


def test_calc_tech_factors_rejects_insufficient_history():
    """均线/突破/量比必须建立在至少 60 根完整日K之上。"""
    klines = _make_klines(59)
    quote = {"price": 158.0, "change_pct": 0.5,
             "prev_close": 157.0, "amount_wan": 1.0}

    assert calc_tech_factors("上证指数", klines, quote)["available"] is False


def test_fetch_index_kline_rejects_short_core_response(monkeypatch):
    """核心调用请求 65 根时，单根新鲜响应不能阻止免费源继续降级。"""
    import factor_collector as fc

    short = _make_klines(1)
    monkeypatch.setattr(fc, "_fetch_kline_sina", lambda *a: short)
    monkeypatch.setattr(fc, "_fetch_kline_tencent", lambda *a: short)
    monkeypatch.setattr(fc, "_fetch_kline_em", lambda *a: short)

    assert fc.fetch_index_kline("sz399006", 65) == []


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


def test_is_trading_time_rejects_china_statutory_holiday():
    """中国法定节假日即使落在周一至周五也不得进入盘中高频模式。"""
    from datetime import datetime
    from factor_collector import _is_trading_time

    assert _is_trading_time(datetime(2026, 10, 1, 10, 30)) is False


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


# ============================================================
# SNA-02 数据源加固：期货基差 + 资金流替代通道（全 mock，零网络）
# ============================================================

def _sina_fut_text(mapping):
    """构造新浪股指期货返回文本 {sina_code: (昨结算, 最新价)}（parts[0]/parts[3]）"""
    lines = []
    for code, (ps, p) in mapping.items():
        parts = [str(ps), "0", "0", str(p), "0", "0"]
        lines.append(f'var hq_str_{code}="' + ",".join(parts) + '"')
    return ";".join(lines) + ";"


def _patch_http(monkeypatch, routes):
    """按 URL 子串路由的 _http_get 替身：{子串: 文本}，未命中返回空串。
    返回 calls 列表记录实际请求过的 URL（断言降级链触达顺序用）。"""
    import factor_collector as fc
    calls = []

    def fake(url, **kw):
        calls.append(url)
        for key, text in routes.items():
            if key in url:
                return text
        return ""

    monkeypatch.setattr(fc, "_http_get", fake)
    return calls


def _patch_cffex(monkeypatch, ret):
    """中金所 akshare 替身：futures_hist_daily_cffex 可编程返回/抛错。"""
    import akshare
    calls = []

    def fake(date):
        calls.append(date)
        if isinstance(ret, Exception):
            raise ret
        return ret

    monkeypatch.setattr(akshare, "futures_hist_daily_cffex", fake)
    return calls


def _cffex_df():
    """中金所行情 DataFrame：IF 两个合约（IF2609 主力 vol 最大）+ IC/IM/IH 各一"""
    return pd.DataFrame({
        "variety": ["IF", "IF", "IC", "IM", "IH"],
        "volume": [52955.0, 100.0, 8000.0, 12000.0, 9000.0],
        "close": [4604.8, 4590.0, 6800.0, 7250.0, 2940.0],
        "pre_settle": [4590.0, 4580.0, 6790.0, 7240.0, 2935.0],
    })


# ---------------- 期货降级链 fetch_index_futures ----------------

def test_futures_sina_full_skips_cffex(monkeypatch):
    """新浪全量返回 → 不触达中金所（主链优先，省降级开销）"""
    import factor_collector as fc
    full = {"nf_IF0": (4480, 4500), "nf_IC0": (6790, 6810),
            "nf_IM0": (7280, 7300), "nf_IH0": (2950, 2960)}
    _patch_http(monkeypatch, {"hq.sinajs.cn": _sina_fut_text(full)})
    cffex_calls = _patch_cffex(monkeypatch, _cffex_df())
    out = fc.fetch_index_futures()
    assert len(out) == 4 and out["IF"]["price"] == 4500.0
    assert not cffex_calls, "新浪全量时不应触达中金所"


def test_futures_sina_partial_cffex_fills_gap(monkeypatch):
    """新浪部分缺失（IF/IC 在，IM/IH 缺）→ 中金所补齐缺失项，新浪已有值不被覆盖"""
    import factor_collector as fc
    partial = {"nf_IF0": (4480, 4500), "nf_IC0": (6790, 6810)}
    _patch_http(monkeypatch, {"hq.sinajs.cn": _sina_fut_text(partial)})
    _patch_cffex(monkeypatch, _cffex_df())
    out = fc.fetch_index_futures()
    assert len(out) == 4
    assert out["IF"]["price"] == 4500.0        # 新浪值保留（中金所 4604.8 不覆盖）
    assert out["IC"]["price"] == 6810.0        # 新浪值保留
    assert out["IM"]["price"] == 7250.0        # 中金所补齐
    assert out["IH"]["price"] == 2940.0        # 中金所补齐


def test_futures_sina_dead_cffex_rescues(monkeypatch):
    """新浪全失败 → 中金所兜底；主力合约 = 同品种 volume 最大"""
    import factor_collector as fc
    _patch_http(monkeypatch, {})
    _patch_cffex(monkeypatch, _cffex_df())
    out = fc.fetch_index_futures()
    assert out["IF"]["price"] == 4604.8   # vol=52955 的 IF2609 主力，非 4590
    assert out["IF"]["prev_settle"] == 4590.0
    assert out["IC"]["price"] == 6800.0


def test_futures_all_dead_returns_empty(monkeypatch):
    """双链全失败 → {} 不抛错（修正层基差因子缺省降 0，永不无信号）"""
    import factor_collector as fc
    _patch_http(monkeypatch, {})
    _patch_cffex(monkeypatch, RuntimeError("cffex down"))
    assert fc.fetch_index_futures() == {}


def test_futures_cffex_empty_lookback_then_give_up(monkeypatch):
    """中金所连续 4 日空表（非交易日）→ 放弃返回 {}，不抛错"""
    import factor_collector as fc
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    calls = _patch_cffex(monkeypatch, pd.DataFrame())
    _patch_http(monkeypatch, {})
    assert fc._fetch_futures_cffex() == {}
    assert len(calls) == 4, "应回看最近 3 个自然日后放弃"


def test_futures_cffex_error_lookback_continues(monkeypatch):
    """中金所某日接口异常时仍继续回看，后续交易日成功即可恢复。"""
    import akshare
    import factor_collector as fc

    calls = []
    responses = [RuntimeError("temporary failure"), _cffex_df()]

    def fake(date):
        calls.append(date)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(akshare, "futures_hist_daily_cffex", fake)
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)

    out = fc._fetch_futures_cffex()

    assert len(calls) == 2
    assert out["IF"]["price"] == 4604.8


def test_futures_cffex_skips_non_dataframe_response(monkeypatch):
    """中金所返回非表对象时，应继续回看而不是在.empty处崩溃。"""
    import factor_collector as fc

    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    calls = _patch_cffex(monkeypatch, {"error": "bad response"})

    assert fc._fetch_futures_cffex() == {}
    assert len(calls) == 4


def test_futures_cffex_skips_response_missing_contract_columns(monkeypatch):
    """中金所返回缺少主力排序字段的表时，应按源失败处理。"""
    import factor_collector as fc

    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    malformed = pd.DataFrame({"variety": ["IF"], "close": [4600.0]})
    calls = _patch_cffex(monkeypatch, malformed)

    assert fc._fetch_futures_cffex() == {}
    assert len(calls) == 4


def test_futures_cffex_direct_main_contract(monkeypatch):
    """_fetch_futures_cffex 主力识别：drop_duplicates(variety) 前按 volume 降序"""
    import factor_collector as fc
    _patch_cffex(monkeypatch, _cffex_df())
    out = fc._fetch_futures_cffex()
    assert set(out) == {"IF", "IC", "IM", "IH"}
    assert out["IF"]["price"] == 4604.8 and out["IC"]["prev_settle"] == 6790.0


def test_futures_sina_parse_fields(monkeypatch):
    """_fetch_futures_sina 字段解析：parts[0]=昨结算 / parts[3]=最新价"""
    import factor_collector as fc
    _patch_http(monkeypatch, {"hq.sinajs.cn": _sina_fut_text({"nf_IF0": (4480.0, 4500.5)})})
    out = fc._fetch_futures_sina()
    assert out == {"IF": {"price": 4500.5, "prev_settle": 4480.0}}


# ---------------- 资金流降级链 fetch_market_flows ----------------

def _em_full_text():
    """东财全量文本：主力净流入 150 亿 + 融资余额 20000 亿/日增 100 亿"""
    import json
    return {
        "push2.eastmoney.com": json.dumps({
            "data": {"klines": ["2026-08-27,15000000000,1,2,3,4,5"]}}),
        "datacenter-web.eastmoney.com": json.dumps({
            "result": {"data": [
                {"RZYE": 200000000000}, {"RZYE": 199000000000}]}}),
    }


def test_flows_em_dead_returns_margin_only(monkeypatch):
    """东财主力流失败时保留独立成功的融资字段，不抛错。"""
    import factor_collector as fc
    import json
    _patch_http(monkeypatch, {
        "push2.eastmoney.com": "",  # 主力净流入失败
        "datacenter-web.eastmoney.com": json.dumps({
            "result": {"data": [
                {"RZYE": 200000000000}, {"RZYE": 199000000000}]}}),
    })
    out = fc.fetch_market_flows()
    assert "main_net_yi" not in out
    assert out["margin_yi"] == 2000.0, "margin 仅东财单源，独立失败互不影响"


def test_flows_all_dead_returns_empty(monkeypatch):
    """东财两个接口全失败 → {}（修正层资金流因子缺省降 0，永不无信号）"""
    import factor_collector as fc
    _patch_http(monkeypatch, {})
    assert fc.fetch_market_flows() == {}
