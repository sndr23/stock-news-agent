# -*- coding: utf-8 -*-
"""风险收缩期联动（factor_collector → real_time_push 降级）单元测试"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import real_time_push as rtp  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用


def _news(title, content=""):
    return {"title": title, "content": content}


def _judge(direction, sectors, scope="sector"):
    return {"direction": direction, "sectors": sectors, "scope": scope,
            "push": True, "score": 7, "is_leader_stock": False}


def test_downgrade_tech_bullish_no_event():
    # 科技 bullish 无硬事件佐证（景气/受益类情绪利好）→ 降级
    n = _news("AI算力板块景气度持续提升 产业链有望深度受益")
    j = _judge("bullish", ["算力"])
    assert rtp._risk_off_downgrade(n, j) is True


def test_downgrade_tech_bullish_with_amount():
    # 有具体金额 → 硬事件，放行
    n = _news("某公司获得算力服务订单 金额3.5亿元")
    j = _judge("bullish", ["算力"])
    assert rtp._risk_off_downgrade(n, j) is False


def test_downgrade_tech_bullish_with_event_word():
    # 命中硬事件动词（中标）→ 放行
    n = _news("数据中心中标国网重大项目")
    j = _judge("bullish", ["算力", "数据中心"])
    assert rtp._risk_off_downgrade(n, j) is False


def test_downgrade_non_tech_bullish():
    # 非科技 bullish → 不受影响
    n = _news("白酒龙头提价 渠道库存下降")
    j = _judge("bullish", ["白酒"])
    assert rtp._risk_off_downgrade(n, j) is False


def test_downgrade_tech_bearish():
    # 科技 bearish → 不受影响（风险期更应提示利空）
    n = _news("半导体产能过剩担忧升温")
    j = _judge("bearish", ["半导体"])
    assert rtp._risk_off_downgrade(n, j) is False


def test_downgrade_tech_neutral():
    # 科技 neutral → 不受影响
    n = _news("光模块板块震荡整理")
    j = _judge("neutral", ["光模块"])
    assert rtp._risk_off_downgrade(n, j) is False


def test_downgrade_liquid_cooling_no_event():
    # 液冷（OVERSEAS_TECH_KEYWORDS 缺词，靠宽词表兜底）无事件佐证 → 降级
    n = _news("多家上市公司加码液冷散热业务")
    j = _judge("bullish", ["液冷"])
    assert rtp._risk_off_downgrade(n, j) is True


def test_downgrade_outlook_word():
    # 展望修饰（"有望中标"）不算已发生 → 降级
    n = _news("企业有望中标数据中心项目")
    j = _judge("bullish", ["数据中心"])
    assert rtp._risk_off_downgrade(n, j) is True


def test_hard_data_percent():
    # 百分比硬数据（良率98%）→ 放行
    n = _news("台积电部分CoWoS产品生产良率升至98%至99%")
    j = _judge("bullish", ["半导体"])
    assert rtp._risk_off_downgrade(n, j) is False


def test_hard_event_expansion():
    # 完成态扩产动作（扩建/投运/产能）→ 放行
    n = _news("恩智浦扩建马来西亚封测基地 2028年Q1投运设计产能翻倍")
    j = _judge("bullish", ["半导体"])
    assert rtp._risk_off_downgrade(n, j) is False


def test_downgrade_emotion_research():
    # 研报/情绪类（机构称/市场空间）→ 降级
    n = _news("产业资本加速投入 机构称物理AI市场空间将迈向星辰大海")
    j = _judge("bullish", ["AI"])
    assert rtp._risk_off_downgrade(n, j) is True


def test_load_risk_state(tmp_path, monkeypatch):
    monkeypatch.setattr(rtp, "_FACTOR_STATE_PATH", tmp_path / "factor_state.json")
    # 文件不存在 → neutral（向后兼容：云端未跑 factor_collector 不影响现有推送）
    assert rtp._load_factor_risk_state() == "neutral"
    # risk_off
    (tmp_path / "factor_state.json").write_text('{"risk_state": "risk_off"}', encoding="utf-8")
    assert rtp._load_factor_risk_state() == "risk_off"
    # 非法值 → neutral
    (tmp_path / "factor_state.json").write_text('{"risk_state": "weird"}', encoding="utf-8")
    assert rtp._load_factor_risk_state() == "neutral"
