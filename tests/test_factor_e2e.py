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
    # 覆盖 fx：日元急升 -2% → 风险 risk_off + 汇率卖向 → 方向偏空（与预置"中性"不同 → 触发方向信号）
    monkeypatch.setattr(fc, "fetch_fx", lambda: {
        "fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": -2.0},
        "fx_susdcny": {"name": "美元/人民币", "price": 6.74, "change_pct": -0.01},
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
