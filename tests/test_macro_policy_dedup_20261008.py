"""2026-10-08 美联储沃勒点阵图多源报道合并回归。"""
import importlib.util
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
_PROJECT_ROOT = Path(__file__).parent.parent


def _load_rtp():
    cwd = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(
            "real_time_push_macro_dedup", _PROJECT_ROOT / "scripts" / "real_time_push.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.chdir(cwd)


rtp = _load_rtp()


def _sig(title, entities=("美联储",), events=(), numbers=(), sectors=(), scope="market"):
    return {
        "title_norm": rtp._normalize_title(title),
        "entities": list(entities),
        "events": list(events),
        "numbers": list(numbers),
        "sectors": list(sectors),
        "scope": scope,
        "stocks": [],
    }


_WALLER_TITLES = (
    "美联储理事沃勒点阵图可能反映2027年年初加息之后再降息",
    "美联储理事沃勒加息之后再降息2027年点阵图可能反映年初加息之后再降息",
    "美联储理事沃勒仍需进一步加息但加息不必连续进行",
)


def test_waller_dot_plot_reports_1_and_2_merge():
    assert rtp._is_same_event(_sig(_WALLER_TITLES[0]), _sig(_WALLER_TITLES[1])) is True


def test_waller_dot_plot_reports_1_and_3_merge():
    assert rtp._is_same_event(_sig(_WALLER_TITLES[0]), _sig(_WALLER_TITLES[2])) is True


def test_waller_dot_plot_reports_2_and_3_merge():
    assert rtp._is_same_event(_sig(_WALLER_TITLES[1]), _sig(_WALLER_TITLES[2])) is True


def test_pure_fed_hawkish_vs_dovish_reports_stay_separate():
    hawkish = _sig("美联储考虑加息")
    dovish = _sig("美联储考虑降息")
    assert rtp._is_same_event(hawkish, dovish) is False


def test_different_central_banks_stay_separate():
    boj = _sig("日本央行加息", entities=("日本央行",))
    fed = _sig("美联储加息", entities=("美联储",))
    assert rtp._is_same_event(boj, fed) is False


def test_unrelated_fed_and_nvidia_reports_stay_separate():
    fed = _sig("美联储加息")
    nvidia = _sig("英伟达发布新芯片", entities=("英伟达",))
    assert rtp._is_same_event(fed, nvidia) is False


def test_existing_boj_three_report_merge_stays_intact():
    first = _sig("消息人士称日本央行最快可能在9月加息", entities=("日本央行",),
                 events=("行情下跌",), sectors=("整体市场",))
    second = _sig("日本央行加息或提速美元对日元急跌市场担心潜在流动性冲击",
                  entities=("日本央行",), sectors=("整体市场",))
    third = _sig("消息人士称日本央行考虑9月加息并加快紧缩步伐",
                 entities=("日本央行",), sectors=("整体市场",))
    assert rtp._is_same_event(first, second) is True
    assert rtp._is_same_event(first, third) is True
    assert rtp._is_same_event(second, third) is True


def test_existing_nxp_ambarella_three_report_merge_stays_intact():
    news = (
        {"title": "恩智浦洽谈收购Ambarella", "content": ""},
        {"title": "据英国金融时报：恩智浦半导体(NXPI.O)正洽谈收购一家价值33亿美元的自动驾驶芯片公司", "content": ""},
        {"title": "安霸股价因传恩智浦洽谈收购而飙升", "content": ""},
    )
    judges = (
        {"entities": ["恩智浦", "Ambarella"]},
        {"entities": ["恩智浦", "Ambarella"]},
        {"entities": ["安霸", "恩智浦"]},
    )
    sigs = [rtp._push_event_sig(item, judge) for item, judge in zip(news, judges)]
    assert rtp._is_same_event(sigs[0], sigs[1]) is True
    assert rtp._is_same_event(sigs[0], sigs[2]) is True
    assert rtp._is_same_event(sigs[1], sigs[2]) is True
