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
