# filepath: tests/test_adaptive_quota.py
"""测试 _adaptive_quota 自适应配额分配"""
import pytest
from unittest.mock import patch
from src.agent.nodes import _adaptive_quota, _PREFILTER_TOTAL_LIMIT, _PREFILTER_MIN_QUOTA



class TestAdaptiveQuotaBasic:
    def _make_buckets(self, direct=0, sector=0, macro=0):
        return {
            "direct": [f"d{i}" for i in range(direct)],
            "sector": [f"s{i}" for i in range(sector)],
            "macro": [f"m{i}" for i in range(macro)],
        }

    def test_all_empty(self):
        buckets = self._make_buckets()
        result = _adaptive_quota(buckets)
        assert result["direct"] is None  # 无需截断
        assert result["sector"] == 0
        assert result["macro"] == 0

    def test_direct_only_under_limit(self):
        buckets = self._make_buckets(direct=5)
        result = _adaptive_quota(buckets)
        assert result["direct"] is None  # 5 < 20, 全留

    def test_direct_exceeds_limit(self):
        buckets = self._make_buckets(direct=25, sector=5, macro=5)
        result = _adaptive_quota(buckets)
        # direct 超限时截断到 total_limit - min_total，但回补后可能更多
        min_total = _PREFILTER_MIN_QUOTA["sector"] + _PREFILTER_MIN_QUOTA["macro"]
        assert result["direct"] is not None
        # direct 至少被截断（不应全留 25 条）
        assert result["direct"] < 25
        # sector/macro 保底
        assert result["sector"] >= min(_PREFILTER_MIN_QUOTA["sector"], 5)
        assert result["macro"] >= min(_PREFILTER_MIN_QUOTA["macro"], 5)

    def test_sector_macro_proportional(self):
        buckets = self._make_buckets(direct=5, sector=20, macro=10)
        result = _adaptive_quota(buckets)
        remaining = _PREFILTER_TOTAL_LIMIT - 5
        # sector:macro = 2:1
        expected_sector = round(remaining * 20 / 30)
        assert result["sector"] == max(expected_sector, min(_PREFILTER_MIN_QUOTA["sector"], 20))
        assert result["macro"] == remaining - result["sector"]

    def test_quota_does_not_exceed_total(self):
        """总配额不应超过 total_limit"""
        buckets = self._make_buckets(direct=15, sector=30, macro=30)
        result = _adaptive_quota(buckets)
        total = (result["direct"] or 15) + result["sector"] + result["macro"]
        assert total <= _PREFILTER_TOTAL_LIMIT

    def test_reclaim_unused_to_direct(self):
        """sector/macro 用不完的配额回补给 direct"""
        buckets = self._make_buckets(direct=25, sector=2, macro=1)
        result = _adaptive_quota(buckets)
        min_total = _PREFILTER_MIN_QUOTA["sector"] + _PREFILTER_MIN_QUOTA["macro"]
        # direct 被截断到 total - min_total，sector/macro 实际只用 2+1=3
        # 回补: total - (direct_quota + 2 + 1) = total - (total - min_total + 3) = min_total - 3
        assert result["direct"] is not None
        total = result["direct"] + result["sector"] + result["macro"]
        assert total == _PREFILTER_TOTAL_LIMIT

    def test_sector_avail_zero(self):
        """sector_avail=0 时，macro 不应超出 remaining"""
        buckets = self._make_buckets(direct=10, sector=0, macro=30)
        result = _adaptive_quota(buckets)
        remaining = _PREFILTER_TOTAL_LIMIT - 10
        assert result["macro"] <= remaining
        assert result["sector"] == 0

    def test_macro_min_quota_with_zero_sector(self):
        """sector_avail=0 时 macro 保底不应使总配额超过 top_n"""
        buckets = self._make_buckets(direct=16, sector=0, macro=30)
        result = _adaptive_quota(buckets)
        total = (result["direct"] or 16) + result["sector"] + result["macro"]
        assert total <= _PREFILTER_TOTAL_LIMIT, f"总配额{total}超过limit{_PREFILTER_TOTAL_LIMIT}"

pytestmark = pytest.mark.unit  # 纯单元测试：无网络/无真实 LLM 调用
