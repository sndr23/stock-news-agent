# filepath: tests/test_watchlist.py
import pytest
"""关注列表配置校验：用户要求删除关注股票，排序改由 influence_scope 驱动

用户明确需求：删除关注列表的股票；最终排序以「影响范围(scope)」为主维度
（全球宏观 > 科技板块 > 龙头个股），关注列表加权机制已不再使用，故置空。
"""
import json
from pathlib import Path



def test_watchlist_stocks_deleted():
    p = Path(__file__).parent.parent / "watchlist.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    stocks = data.get("stocks", [])
    assert stocks == [], f"关注列表股票应已删除，当前: {stocks}"


def test_watchlist_sectors_deleted():
    p = Path(__file__).parent.parent / "watchlist.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    sectors = data.get("sectors", [])
    assert sectors == [], f"关注列表板块应已删除（scope 驱动排序取代），当前: {sectors}"

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
