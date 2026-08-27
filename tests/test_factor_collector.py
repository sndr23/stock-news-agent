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
    run_once,
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


def test_dryrun_is_pure_read_only(monkeypatch):
    """--dry-run（push=False, collect=False）：纯只读，不写快照/状态、不推送。"""
    rec = _install_collect_deps(monkeypatch)
    run_once(push=False, collect=False)
    assert rec["snapshot"] == 0
    assert rec["save"] == 0
    assert rec["push"] == 0


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


class _FakeFlowPro:
    """Tushare 资金流替身：moneyflow_dc / moneyflow 可编程返回/抛错"""

    def __init__(self, dc_ret=None, mf_ret=None):
        self.dc_ret = dc_ret
        self.mf_ret = mf_ret
        self.calls = []

    def moneyflow_dc(self, **kw):
        self.calls.append(("dc", kw))
        if isinstance(self.dc_ret, Exception):
            raise self.dc_ret
        return self.dc_ret

    def moneyflow(self, **kw):
        self.calls.append(("mf", kw))
        if isinstance(self.mf_ret, Exception):
            raise self.mf_ret
        return self.mf_ret


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


def test_flows_em_ok_skips_tushare(monkeypatch):
    """东财主链成功 → 不触达 Tushare，无 main_net_source 标注（当日盘中口径）"""
    import factor_collector as fc
    from src.strategy import data as sdata
    _patch_http(monkeypatch, _em_full_text())
    pro = _FakeFlowPro()
    monkeypatch.setattr(sdata, "_tushare_client", lambda: pro)
    out = fc.fetch_market_flows()
    assert out["main_net_yi"] == 150.0
    assert out["margin_yi"] == 2000.0
    assert out["margin_chg_yi"] == 10.0
    assert "main_net_source" not in out
    assert not pro.calls, "东财成功时不应触达 Tushare"


def test_flows_em_dead_tushare_dc_rescues(monkeypatch):
    """东财失败 → Tushare moneyflow_dc 聚合兜底，标注 tushare_t1 口径"""
    import factor_collector as fc
    from src.strategy import data as sdata
    _patch_http(monkeypatch, {})
    pro = _FakeFlowPro(dc_ret=pd.DataFrame({"net_amount_main": [2e9, 3e9]}))
    monkeypatch.setattr(sdata, "_tushare_client", lambda: pro)
    out = fc.fetch_market_flows()
    assert out["main_net_yi"] == 50.0          # 5e9 元 → 50 亿
    assert out["main_net_source"] == "tushare_t1"
    assert pro.calls and pro.calls[0][0] == "dc"


def test_flows_dc_dead_moneyflow_channel(monkeypatch):
    """moneyflow_dc 不可用（无 net_amount_main 列）→ moneyflow 合成通道（万元→亿）"""
    import factor_collector as fc
    from src.strategy import data as sdata
    pro = _FakeFlowPro(
        dc_ret=pd.DataFrame({"foo": [1.0]}),
        mf_ret=pd.DataFrame({
            "buy_lg_amount": [1e6], "sell_lg_amount": [0.5e6],
            "buy_elg_amount": [2e6], "sell_elg_amount": [0.5e6]}))
    monkeypatch.setattr(sdata, "_tushare_client", lambda: pro)
    v = fc._fetch_main_net_tushare()
    # lg=+0.5e6, elg=+1.5e6 → 2e6 万元 = 200 亿
    assert v == 200.0
    assert [c[0] for c in pro.calls] == ["dc", "mf"]


def test_flows_tushare_exception_returns_none(monkeypatch):
    """Tushare 接口抛错 → None（上层放弃降级，返回 {} 不抛错）"""
    import factor_collector as fc
    from src.strategy import data as sdata
    pro = _FakeFlowPro(dc_ret=RuntimeError("api err"))
    monkeypatch.setattr(sdata, "_tushare_client", lambda: pro)
    assert fc._fetch_main_net_tushare() is None


def test_flows_no_token_no_rescue(monkeypatch):
    """东财失败 + Tushare 无 token → main_net 缺省；margin 维度独立保留"""
    import factor_collector as fc
    from src.strategy import data as sdata
    import json
    _patch_http(monkeypatch, {
        "push2.eastmoney.com": "",  # 主力净流入失败
        "datacenter-web.eastmoney.com": json.dumps({
            "result": {"data": [
                {"RZYE": 200000000000}, {"RZYE": 199000000000}]}}),
    })
    monkeypatch.setattr(sdata, "_tushare_client", lambda: None)
    out = fc.fetch_market_flows()
    assert "main_net_yi" not in out
    assert out["margin_yi"] == 2000.0, "margin 仅东财单源，独立失败互不影响"


def test_flows_all_dead_returns_empty(monkeypatch):
    """双链全失败 → {}（修正层资金流因子缺省降 0，永不无信号）"""
    import factor_collector as fc
    from src.strategy import data as sdata
    _patch_http(monkeypatch, {})
    monkeypatch.setattr(sdata, "_tushare_client", lambda: None)
    assert fc.fetch_market_flows() == {}


# ---------------- 快照透传 main_net_source ----------------

def test_snapshot_carries_main_net_source():
    """build_snapshot 透传 main_net_source=tushare_t1（口径审计：T-1 vs 当日盘中）"""
    from factor_collector import build_snapshot
    snap = build_snapshot({}, {}, {}, "neutral", flows={
        "main_net_yi": 50.0, "main_net_source": "tushare_t1"})
    assert snap["flows"]["main_net_yi"] == 50.0
    assert snap["flows"]["main_net_source"] == "tushare_t1"


def test_snapshot_no_source_key_when_em():
    """东财口径（无标注）→ 快照不含 main_net_source 键（向后兼容旧读取方）"""
    from factor_collector import build_snapshot
    snap = build_snapshot({}, {}, {}, "neutral", flows={"main_net_yi": 150.0})
    assert snap["flows"]["main_net_yi"] == 150.0
    assert "main_net_source" not in snap["flows"]
