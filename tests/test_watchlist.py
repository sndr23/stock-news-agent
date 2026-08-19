# filepath: tests/test_watchlist.py
"""关注列表配置校验

2026-08-13 曾按用户要求清空（scope 驱动排序取代加权）；
2026-08-19 P1-1 重新激活：用户填入 9 只自选股（带代码，供 factor_collector
个股异动监控 + 资讯龙头匹配双通道使用）。校验改为结构合法性：
每条目必须是 {name, code} 且 code 为 6 位数字。
"""
import json
import re
from pathlib import Path

import pytest


def test_watchlist_stock_entries_structure():
    p = Path(__file__).parent.parent / "watchlist.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    stocks = data.get("stocks", [])
    assert isinstance(stocks, list)
    for s in stocks:
        assert isinstance(s, dict), f"条目应为 dict（name+code）: {s}"
        assert str(s.get("name", "")).strip(), f"缺少 name: {s}"
        code = str(s.get("code", "")).strip()
        assert re.fullmatch(r"\d{6}", code), f"code 应为 6 位数字: {s}"


def test_watchlist_sectors_structure():
    p = Path(__file__).parent.parent / "watchlist.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    sectors = data.get("sectors", [])
    assert isinstance(sectors, list)


pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
