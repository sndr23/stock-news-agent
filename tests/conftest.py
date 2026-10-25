# filepath: tests/conftest.py
"""pytest 配置：把项目根目录加入 sys.path，便于 import src.*"""
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_strategy_cache(tmp_path, monkeypatch):
    """全局隔离 src.strategy.data 的磁盘缓存目录。

    背景（2026-09-01 发现）：个别用例漏 patch _cache_set 时，mock 的假行情
    会经真实缓存写入 data/strategy_cache/，污染生产数据（曾写入
    close=1.0/amount=100 的单行假 K 线）。此处 autouse 强制所有测试的
    缓存读写都落在临时目录，任何用例无需各自再 patch。
    """
    from src.strategy import data as sdata

    cache_dir = tmp_path / "strategy_cache"
    monkeypatch.setattr(sdata, "CACHE_DIR", cache_dir)
    yield


@pytest.fixture(autouse=True)
def _disable_quiet_hours_by_default(monkeypatch):
    """P1-2（2026-10-26）：默认关闭时段静默，保证门禁与运行时刻无关。

    静默窗按北京时间挂钟判定（周末全天 / 每日 21:30–08:00），不隔离的话
    同一批 run_once 用例白天绿、夜间红（实测凌晨跑 20 个推送用例转红）。
    专项用例自行 monkeypatch.setenv("RT_QUIET_HOURS", "1") 并冻结 datetime
    复现静默窗，此处只提供确定性默认值。
    """
    monkeypatch.setenv("RT_QUIET_HOURS", "0")
    yield
