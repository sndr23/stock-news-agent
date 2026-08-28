# filepath: tests/test_p8_factor_pool.py
# -*- coding: utf-8 -*-
"""P8 单元测试（2026-08-19）：因子池扩展 + 分钟级 K 线

覆盖：
1. fetch_minute_kline：腾讯 mkline JSON 解析；空/坏响应 → []
2. calc_daily_derived_factors：动量/反转/均线/量价/跳空打分；数据不足 → {}
3. calc_minute_factors：盘中动量/短线动能打分；<6 根 → {}
4. 影子维度接入：15 维全量进 factors（IC 回测口径），不改变综合分；
   零分影子不进 format_direction_signal 卡片（展示与记录解耦）
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import factor_collector as fc  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送


def _dkline(closes, vols=None, opens=None):
    """构造日K：closes 必填，vols/opens 缺省按 closes 推（无跳空、常量量）"""
    n = len(closes)
    vols = vols or [100] * n
    opens = opens or closes
    return [{"date": f"2026-{i % 12 + 1:02d}-{i % 28 + 1:02d}", "open": opens[i],
             "close": closes[i], "high": closes[i] + 1, "low": closes[i] - 1,
             "volume": vols[i]} for i in range(n)]


def _mk(closes, times=None):
    n = len(closes)
    times = times or [f"20260819{9 + i // 60:02d}{i % 60:02d}" for i in range(n)]
    return [{"time": times[i], "open": closes[i], "close": closes[i],
             "high": closes[i] + 1, "low": closes[i] - 1, "volume": 100} for i in range(n)]


class TestFetchMinuteKline:
    def _payload(self, rows):
        return json.dumps({"data": {"sh000001": {"m5": rows}}})

    def test_parses_mkline_rows(self, monkeypatch):
        rows = [["202608190935", "3900.0", "3905.0", "3906.0", "3899.0", "12345", {}],
                ["202608190940", "3905.0", "3910.0", "3911.0", "3904.0", "20000", {}]]
        monkeypatch.setattr(fc, "_http_get",
                            lambda url, **kw: self._payload(rows))
        out = fc.fetch_minute_kline("sh000001", "m5", 48)
        assert len(out) == 2
        assert out[0] == {"time": "202608190935", "open": 3900.0, "close": 3905.0,
                          "high": 3906.0, "low": 3899.0, "volume": 12345.0}

    def test_empty_and_malformed(self, monkeypatch):
        monkeypatch.setattr(fc, "_http_get", lambda url, **kw: "")
        assert fc.fetch_minute_kline() == []
        monkeypatch.setattr(fc, "_http_get", lambda url, **kw: "not json")
        assert fc.fetch_minute_kline() == []
        # 坏行跳过，好行保留
        rows = [["202608190935", "x", "y", "z"], ["202608190940", "1", "2", "3", "1", "9"]]
        monkeypatch.setattr(fc, "_http_get", lambda url, **kw: self._payload(rows))
        out = fc.fetch_minute_kline()
        assert len(out) == 1

    def test_nonfinite_or_nonpositive_rows_are_skipped(self, monkeypatch):
        """分钟K线价格必须为有限正数，成交量不得为负数。"""
        rows = [
            ["202608190935", "nan", "3905", "3906", "3899", "12345", {}],
            ["202608190940", "3905", "3910", "3911", "3904", "-1", {}],
            ["202608190945", "3905", "3910", "3911", "3904", "20000", {}],
        ]
        monkeypatch.setattr(fc, "_http_get",
                            lambda url, **kw: self._payload(rows))

        out = fc.fetch_minute_kline("sh000001", "m5", 48)

        assert len(out) == 1
        assert out[0]["time"] == "202608190945"


class TestDailyDerivedFactors:
    def test_insufficient_data_returns_empty(self):
        assert fc.calc_daily_derived_factors(_dkline([100] * 10)) == {}

    def test_uptrend_momentum_positive(self):
        closes = [100 + i for i in range(30)]
        out = fc.calc_daily_derived_factors(_dkline(closes))
        assert out["momentum_20d"] == 1.0
        assert out["ma_structure"] == 1.0       # 多头排列
        assert out["reversal_5d"] == 0.0        # 5日+5% < 超买8%
        assert out["volume_price"] == 0.0       # 常量量 → 量比1 不确认

    def test_downtrend_momentum_negative(self):
        closes = [130 - i for i in range(30)]
        out = fc.calc_daily_derived_factors(_dkline(closes))
        assert out["momentum_20d"] == -1.0
        assert out["ma_structure"] == -1.0

    def test_oversold_reversal_positive(self):
        # 最后 5 日从 100 跌到 94（-6% ≤ -5%）：closes[-5]=100 基准
        closes = [100.0] * 25 + [100.0, 99.0, 97.5, 95.5, 94.0]
        out = fc.calc_daily_derived_factors(_dkline(closes))
        assert out["reversal_5d"] == 1.0

    def test_overbought_reversal_negative(self):
        # 最后 5 日从 100 涨到 109（+9% ≥ +8%）
        closes = [100.0] * 25 + [100.0, 103.0, 105.5, 107.5, 109.0]
        out = fc.calc_daily_derived_factors(_dkline(closes))
        assert out["reversal_5d"] == -1.0

    def test_volume_confirms_direction(self):
        # 近5日放量1.5倍 + 昨涨 → +1；昨跌 → -1
        closes = [100.0] * 30
        closes[-1] = 101.0
        vols = [100] * 25 + [150] * 5
        out = fc.calc_daily_derived_factors(_dkline(closes, vols=vols))
        assert out["volume_price"] == 1.0
        closes2 = [100.0] * 30
        closes2[-1] = 99.0
        out2 = fc.calc_daily_derived_factors(_dkline(closes2, vols=vols))
        assert out2["volume_price"] == -1.0

    def test_gap_direction(self):
        closes = [100.0] * 30
        # 高开 1%：今日 open=101 vs 昨收 100
        opens = [c for c in closes]
        opens[-1] = 101.0
        out = fc.calc_daily_derived_factors(_dkline(closes, opens=opens))
        assert out["gap_today"] == 1.0
        opens[-1] = 99.0
        out2 = fc.calc_daily_derived_factors(_dkline(closes, opens=opens))
        assert out2["gap_today"] == -1.0


class TestMinuteFactors:
    def test_insufficient_returns_empty(self):
        assert fc.calc_minute_factors(_mk([1, 2, 3])) == {}

    def test_intraday_uptrend(self):
        closes = [3900 + i * 2 for i in range(20)]
        out = fc.calc_minute_factors(_mk(closes))
        assert out["intraday_momentum"] == 1.0
        assert out["short_term_energy"] == 1.0

    def test_intraday_downtrend(self):
        closes = [3940 - i * 2 for i in range(20)]
        out = fc.calc_minute_factors(_mk(closes))
        assert out["intraday_momentum"] == -1.0
        assert out["short_term_energy"] == -1.0

    def test_flat_market_zero(self):
        closes = [3900.0] * 20
        out = fc.calc_minute_factors(_mk(closes))
        assert out["intraday_momentum"] == 0.0
        assert out["short_term_energy"] == 0.0


class TestShadowIntegration:
    """P8 影子维度接入 _direction_analysis：15 维全量记录，综合分不变"""

    def _env(self):
        tech = {"上证指数": {"available": True, "price": 3930.0, "change_pct": 0.0,
                         "trend": "震荡", "vol_ratio5": 1.0,
                         "breakout": False, "breakdown": False}}
        fx = {"fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": 0.0}}
        return tech, fx

    def _daily(self, score):
        # 统一给 5 个日线影子维度打同一分（构造用）
        return {"momentum_20d": score, "_momentum_desc": "测试",
                "reversal_5d": score, "_reversal_desc": "测试",
                "ma_structure": score, "_ma_desc": "测试",
                "volume_price": score, "_vp_desc": "测试",
                "gap_today": score, "_gap_desc": "测试"}

    def test_p8_dims_recorded_without_score_change(self):
        tech, fx = self._env()
        base = fc._direction_analysis(tech, {}, fx, "neutral", {})
        ext = fc._direction_analysis(tech, {}, fx, "neutral", {},
                                     daily_factors=self._daily(1.0),
                                     minute_factors={"intraday_momentum": 1.0,
                                                     "short_term_energy": 1.0,
                                                     "_intraday_desc": "测试",
                                                     "_short_desc": "测试"})
        # 综合分/方向不受影子维度影响；维度数两侧一致（"对冲"依赖 basis，此处空）
        assert ext["score"] == base["score"]
        assert ext["direction"] == base["direction"]
        names = [n for n, _, _ in ext["factors"]]
        assert len(names) == len(base["factors"]) == 15  # 5主+2P7影子+7P8影子+1P12影子
        for expect in ("动量(20日)", "反转(5日)", "均线结构", "量价配合", "跳空缺口",
                       "盘中动量", "短线动能"):
            assert expect in names
        # 有数据时影子分生效、缺省时 0 分（分值记录供 IC 回测）
        ext_f = {n: s for n, s, _ in ext["factors"]}
        base_f = {n: s for n, s, _ in base["factors"]}
        assert ext_f["动量(20日)"] == 1.0 and base_f["动量(20日)"] == 0.0
        # 非零影子进 shadow 集合，全量影子在 shadow_all
        assert {"动量(20日)", "盘中动量", "短线动能"} <= ext["shadow"]
        assert {"动量(20日)", "盘中动量", "短线动能"} <= ext["shadow_all"]
        # 影子不进生效权重
        assert "动量(20日)" not in ext["eff_weights"]

    def test_missing_data_zero_score_not_shadow(self):
        tech, fx = self._env()
        a = fc._direction_analysis(tech, {}, fx, "neutral", {})
        fac = {n: s for n, s, _ in a["factors"]}
        assert fac["动量(20日)"] == 0.0
        assert fac["盘中动量"] == 0.0
        assert "动量(20日)" not in a["shadow"]      # 非零集合不含
        assert "动量(20日)" in a["shadow_all"]      # 全量集合含（展示隐藏判定用）

    def test_zero_shadow_hidden_from_card(self):
        """零分影子不进推送卡片（展示与记录解耦），非零影子展示"""
        tech, fx = self._env()
        a = fc._direction_analysis(tech, {}, fx, "neutral", {},
                                   daily_factors=self._daily(1.0))
        text = fc.format_direction_signal(a, "中性")
        assert "动量(20日)" in text        # 非零影子展示
        assert "影子·未参与合成" in text
        assert "盘中动量" not in text      # 零分影子隐藏
        assert "反转(5日)" in text         # 非零展示
