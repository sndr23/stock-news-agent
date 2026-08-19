# -*- coding: utf-8 -*-
"""factor_collector run_once 端到端 mock 测试（不触网、不推微信）"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import factor_collector as fc  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络


def _mock_data(monkeypatch, tmp_path):
    """mock 数据源 + 状态路径 + 推送，返回记录推送的列表"""
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.setattr(fc, "STATE_PATH", tmp_path / "factor_state.json")

    monkeypatch.setattr(fc, "fetch_index_quotes", lambda: {
        "上证指数": {"price": 3930.0, "prev_close": 3900.0, "change_pct": 0.8, "amount_wan": 1.0},
        "创业板指": {"price": 3630.0, "prev_close": 3600.0, "change_pct": 0.8, "amount_wan": 1.0},
        "沪深300": {"price": 4665.0, "prev_close": 4660.0, "change_pct": 0.1, "amount_wan": 1.0},
        "中证500": {"price": 7990.0, "prev_close": 7990.0, "change_pct": 0.0, "amount_wan": 1.0},
        "中证1000": {"price": 7770.0, "prev_close": 7770.0, "change_pct": 0.0, "amount_wan": 1.0},
        "上证50": {"price": 2915.0, "prev_close": 2915.0, "change_pct": 0.0, "amount_wan": 1.0},
    })
    monkeypatch.setattr(fc, "fetch_fx", lambda: {
        "fx_susdjpy": {"name": "美元/日元", "price": 159.0, "change_pct": -0.3},
        "fx_susdcny": {"name": "美元/人民币", "price": 6.74, "change_pct": -0.01},
    })
    monkeypatch.setattr(fc, "fetch_index_futures", lambda: {
        "IF": {"price": 4650.0, "prev_settle": 4640.0},
        "IC": {"price": 7880.0, "prev_settle": 7900.0},
        "IM": {"price": 7660.0, "prev_settle": 7680.0},
        "IH": {"price": 2900.0, "prev_settle": 2910.0},
    })

    def fake_kline(symbol, lmt=65):
        return [{"date": f"2026-08-{i:02d}", "open": 100 + i, "close": 100 + i,
                 "high": 101 + i, "low": 99 + i, "volume": 100} for i in range(65)]
    monkeypatch.setattr(fc, "fetch_index_kline", fake_kline)

    # P1-2/P1-3：个股行情与资金流同样不触网（watchlist 已激活，空行情 → 无自选股异动）
    monkeypatch.setattr(fc, "fetch_stock_quotes", lambda symbols: {})
    monkeypatch.setattr(fc, "fetch_market_flows", lambda: {})
    # P3（2026-08-19）：外盘/宽度/风格同样不触网（空数据 → 无新增异动）
    monkeypatch.setattr(fc, "fetch_global_quotes", lambda: {})
    monkeypatch.setattr(fc, "fetch_market_breadth", lambda: {})
    monkeypatch.setattr(fc, "calc_style_rotation", lambda: {})
    # P4（2026-08-19）：涨停情绪/行业资金流不触网；胜率标注也隔离（不读 real_time_state）
    monkeypatch.setattr(fc, "fetch_zt_sentiment", lambda: {})
    monkeypatch.setattr(fc, "fetch_sector_flows", lambda: {})
    # P7（2026-08-19）：资金面利率/期权 PCR 同样不触网（空数据 → 无新增异动）
    monkeypatch.setattr(fc, "fetch_liquidity", lambda: {})
    monkeypatch.setattr(fc, "fetch_option_pcr", lambda: {})
    # P8（2026-08-19）：分钟K线不触网（空数据 → 分钟影子因子 0 分）
    monkeypatch.setattr(fc, "fetch_minute_kline", lambda *a, **kw: [])
    import signal_backtest as sb  # noqa: E402
    monkeypatch.setattr(sb, "compute_winrate", lambda days=30: {"n": 0})

    pushed = []
    monkeypatch.setattr(fc, "do_push", lambda title, content: (pushed.append({"title": title, "content": content}) or {"code": 200}))
    return pushed


def test_run_once_full_flow(tmp_path, monkeypatch):
    pushed = _mock_data(monkeypatch, tmp_path)
    result = fc.run_once(push=True)

    # 技术面/宏观因子计算成功
    assert result["tech"]["上证指数"]["available"] is True
    assert "IC" in result["basis"]
    assert result["fx"]["fx_susdjpy"]["price"] == 159.0
    # 状态落盘
    state = fc._load_state()
    assert "last_direction" in state
    assert "basis_history" in state
    assert "risk_state" in state
    # 首次运行无方向基准 → 不推方向信号；无异动 → 不推告警
    assert pushed == []


def test_run_once_direction_change_pushes(tmp_path, monkeypatch):
    pushed = _mock_data(monkeypatch, tmp_path)
    # 覆盖 fx：日元急升 -2% → 风险 risk_off + 汇率卖向；覆盖宽度：极端普跌 91.6%。
    # P3 后方向合成为 6 维多数表决（|均值|≥0.5 = 半数维度同向）：
    # 汇率-1 + 风险-1 + 宽度-1 → 3/5 票 → 偏空（与预置"中性"不同 → 触发方向信号）。
    # 单两票（如仅日元急升）不再翻方向——单因子异动仍即时走"量化因子异动"告警。
    monkeypatch.setattr(fc, "fetch_fx", lambda: {
        "fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": -2.0},
        "fx_susdcny": {"name": "美元/人民币", "price": 6.74, "change_pct": -0.01},
    })
    monkeypatch.setattr(fc, "fetch_market_breadth", lambda: {
        "adv": 428, "dec": 4885, "flat": 22, "down_pct": 91.6,
        "limit_up": 36, "limit_down": 118, "big_down": 2189,
    })
    fc._save_state({"last_direction": "中性", "basis_history": {}, "risk_state": "neutral"})
    fc.run_once(push=True)

    titles = [p["title"] for p in pushed]
    assert "量化方向信号" in titles  # 方向由中性→偏空，触发推送
    content = next(p["content"] for p in pushed if p["title"] == "量化方向信号")
    assert "利空" in content  # 偏空 → 展示为"利空"


def test_run_once_no_push_dry_state_unchanged(tmp_path, monkeypatch):
    """dry-run 不推送、不写状态（basis_history 不变）"""
    _mock_data(monkeypatch, tmp_path)
    fc.run_once(push=False)
    assert not (tmp_path / "factor_state.json").exists()  # 状态未被写


def test_run_once_records_direction_history(tmp_path, monkeypatch):
    """P4-6：push 模式每轮落盘 direction_history（当日一条，多轮覆盖）+ factor_ic 状态"""
    from datetime import datetime
    _mock_data(monkeypatch, tmp_path)
    fc._save_state({"last_direction": "中性", "basis_history": {}, "risk_state": "neutral"})
    fc.run_once(push=True)

    state = fc._load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    assert today in state["direction_history"]
    rec = state["direction_history"][today]
    assert rec["dir"] in ("偏多", "偏空", "中性")
    assert isinstance(rec["factors"], dict) and rec["factors"]   # 各维度分落盘
    assert "factor_ic" in state                                  # IC 状态（空历史 → {"n":0}）
    assert state["factor_ic"].get("n") == 0

    # 同日第二轮：覆盖当日条目，不产生重复日期
    fc.run_once(push=True)
    state2 = fc._load_state()
    days = [d for d in state2["direction_history"]]
    assert days.count(today) == 1


def test_direction_history_pruned_to_120(tmp_path, monkeypatch):
    """P4-6：direction_history 超 120 个交易日时裁掉最旧（防 Gist 状态膨胀）"""
    _mock_data(monkeypatch, tmp_path)
    old = {f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}": {"dir": "中性", "score": 0, "factors": {}}
           for i in range(130)}
    fc._save_state({"last_direction": "中性", "basis_history": {}, "risk_state": "neutral",
                    "direction_history": old})
    fc.run_once(push=True)
    dhist = fc._load_state()["direction_history"]
    assert len(dhist) == 120   # 131（130 旧 + 今日新）→ 裁 11 最旧 → 120
