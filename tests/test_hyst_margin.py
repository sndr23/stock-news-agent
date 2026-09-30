# -*- coding: utf-8 -*-
"""降档滞回带生产值守卫（HYST_MARGIN=0.10，2026-09-30 用户拍板）。

定位：本文件**只**钉死降档滞回带相关的生产语义，防止无声漂移。
不重复 `test_chinext_timing.py` 已覆盖的通用状态机逻辑。
"""
import pytest

from src.strategy import chinext_timing as ct

pytestmark = pytest.mark.unit


def test_production_hyst_margin_value():
    """钉住生产滞回带值。改动必须走用户拍板 + 回测，不许无声漂移。"""
    assert ct.HYST_MARGIN == 0.10


def test_hyst_margin_applies_only_to_downgrade():
    """滞回带只放宽降档线，升档线不得被放宽（进场慢、出场快的非对称性）。"""
    assert ct._tier_with_hysteresis(0.40, 0.0, ct.TIERS) == 1.0
    # 从空仓升档：阈值不放宽
    assert ct._tier_with_hysteresis(0.28, 0.0, ct.TIERS) != 1.0


def test_hold_zone_between_threshold_and_hyst_edge():
    """带内（阈值 - HYST_MARGIN, 阈值）必须维持原档，不降档。"""
    prev = {"position": 1.0, "pending": None}
    edge = ct.TIERS[0][0] - ct.HYST_MARGIN
    for score in (edge + 0.001, edge + 0.05, edge + 0.099, ct.TIERS[0][0]):
        d = ct.decide_position(score, 1.0, prev)
        assert not d["changed"] and d["position"] == 1.0, score


def test_downgrade_triggers_exactly_at_hyst_edge():
    """明确跌破「阈值 - HYST_MARGIN」即降档，边界不含糊。"""
    prev = {"position": 1.0, "pending": None}
    edge = ct.TIERS[0][0] - ct.HYST_MARGIN
    d = ct.decide_position(edge - 1e-9, 1.0, prev)
    assert d["changed"] and d["position"] == 0.9
    assert d["direction"] == "down"


def test_risk_cap_downgrade_bypasses_hysteresis():
    """硬风控封顶触发的降档**不得**被滞回带挡住（风控优先）。"""
    prev = {"position": 1.0, "pending": None}
    # 分数远在带内（0.25 >= 0.20），但 cap=0.6 强制降档
    d = ct.decide_position(0.25, 0.6, prev)
    assert d["changed"] and d["position"] == 0.6
    assert "风控封顶" in "".join(d["note"])


def test_wider_band_monotonically_reduces_downgrade_count():
    """滞回带越宽，跌穿同一分数序列的降档次数不应增加（单调性守卫）。"""
    seq = [0.29, 0.26, 0.23, 0.21, 0.19, 0.24, 0.27, 0.22, 0.18, 0.25]
    counts = {}
    for width in (0.05, 0.10, 0.15):
        ct.HYST_MARGIN = width
        try:
            prev = {"position": 1.0, "pending": None}
            n = 0
            for s in seq:
                d = ct.decide_position(s, 1.0, prev)
                if d["changed"]:
                    n += 1
                prev = {"position": d["position"], "pending": d["pending"]}
            counts[width] = n
        finally:
            ct.HYST_MARGIN = 0.10
    assert counts[0.05] >= counts[0.10] >= counts[0.15], counts


def test_hyst_margin_does_not_affect_upgrade_confirmation_days():
    """滞回带改动不得影响升档确认天数（两者是独立机制）。

    升档线本身不被滞回带放宽：score=0.35 已越过满仓线 0.30，
    仍需连续 UPGRADE_CONFIRM_DAYS=2 日同目标确认才生效。
    """
    assert ct.UPGRADE_CONFIRM_DAYS == 2

    # 首日：只建立 pending，不生效
    prev = {"position": 0.6, "pending": None}
    d = ct.decide_position(0.35, 1.0, prev)
    assert not d["changed"] and d["position"] == 0.6
    assert d["pending"] == {"target": 1.0, "days": 1}

    # 次日：确认达成，升档生效
    prev2 = {"position": 0.6, "pending": {"target": 1.0, "days": 1}}
    d2 = ct.decide_position(0.35, 1.0, prev2)
    assert d2["changed"] and d2["position"] == 1.0
    assert d2["pending"] is None


@pytest.mark.parametrize("tier_threshold,expected_after_hold", [
    (0.30, 0.9),   # 满仓线
    (-0.25, 0.6),  # 九成线
    (-0.30, 0.0),  # 六成线
])
def test_each_tier_line_has_its_own_hysteresis_edge(tier_threshold, expected_after_hold):
    """每条档位线都独立享受 HYST_MARGIN 放宽，不能只对满仓线生效。"""
    prev = {"position": _pos_for(tier_threshold), "pending": None}
    edge = tier_threshold - ct.HYST_MARGIN
    d = ct.decide_position(edge - 0.01, 1.0, prev)
    assert d["changed"] and d["position"] == expected_after_hold


def _pos_for(threshold):
    return ct.score_to_tier(threshold + 1e-6)
