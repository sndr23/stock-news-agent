# filepath: tests/test_p4_factor_ic.py
"""P4-6 因子 IC 加权单元测试（2026-08-19）

覆盖：
1. _spearman（完美单调 / 反向 / 常数零方差 / 短样本）
2. compute_factor_ic（预测因子正 IC 高权重 / 反向因子只留地板 / 常数因子 IC=0 /
   样本不足无权重 / 空历史 / kline 缺省拉取）
3. _direction_analysis IC 加权（加权改判方向 / 未覆盖维度地板 / None 等权向后兼容）
4. format_direction_signal（IC 加权标注 + 维度权重展示 / 无权重兼容）
5. real_time_push 简报块 IC 加权标注
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import factor_collector as fc  # noqa: E402
import real_time_push as rtp  # noqa: E402
import signal_backtest as sb  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送


# ============================================================
# _spearman
# ============================================================

class TestSpearman:
    def test_perfect_monotonic(self):
        assert sb._spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)

    def test_inverse(self):
        assert sb._spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_constant_side_zero(self):
        assert sb._spearman([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) == 0.0
        assert sb._spearman([1, 2, 3, 4, 5], [7, 7, 7, 7, 7]) == 0.0

    def test_short_sample(self):
        assert sb._spearman([1, 2], [1, 2]) == 0.0

    def test_ties_average_rank(self):
        # 并列值取平均秩，不崩溃且结果在 [-1, 1]
        v = sb._spearman([1, 1, 2, 3, 4], [1, 2, 2, 3, 4])
        assert -1.0 <= v <= 1.0


# ============================================================
# compute_factor_ic
# ============================================================

def _synth(days=30):
    """构造合成数据：31 日 K 线（30 个次日收益）+ 前 30 日方向历史

    预测因子 = sign(次日收益) → 正 IC；反向因子 = -sign → 负 IC；常数因子 → 零方差。
    """
    rets = [1.2, -0.8, 2.0, -1.5, 0.9, -2.2, 0.5, -1.1, 1.8, -0.4] * 3
    closes = [100.0]
    for r in rets:
        closes.append(round(closes[-1] * (1 + r / 100), 4))
    kline = [{"date": f"2026-07-{i + 1:02d}", "close": c} for i, c in enumerate(closes)]
    hist = {}
    for i in range(days):
        r = rets[i]
        hist[kline[i]["date"]] = {
            "dir": "中性", "score": 0,
            "factors": {"预测因子": 1.0 if r > 0 else -1.0,
                        "反向因子": -1.0 if r > 0 else 1.0,
                        "常数因子": 1.0},
        }
    return hist, kline


class TestComputeFactorIC:
    def test_predictive_factor_gets_high_weight(self):
        hist, kline = _synth()
        out = sb.compute_factor_ic(hist, index_closes=kline)
        assert out["n"] == 30
        assert out["ic"]["预测因子"] > 0.5
        assert out["weights"]["预测因子"] == pytest.approx(max(out["ic"]["预测因子"], 0) + sb.IC_FLOOR, abs=1e-3)
        assert out["weights"]["预测因子"] > 0.6

    def test_contrarian_factor_floor_only(self):
        hist, kline = _synth()
        out = sb.compute_factor_ic(hist, index_closes=kline)
        assert out["ic"]["反向因子"] < -0.5          # 负 IC = 暂无预测力
        assert out["weights"]["反向因子"] == sb.IC_FLOOR  # 不反向加权，只留地板

    def test_constant_factor_zero_ic(self):
        hist, kline = _synth()
        out = sb.compute_factor_ic(hist, index_closes=kline)
        assert out["ic"]["常数因子"] == 0.0
        assert out["weights"]["常数因子"] == sb.IC_FLOOR

    def test_insufficient_days_no_weights(self):
        hist, kline = _synth()
        partial = dict(list(hist.items())[:15])
        out = sb.compute_factor_ic(partial, index_closes=kline)
        assert out == {"n": 15}   # 无 ic/weights 键 → 调用方等权回退

    def test_empty_history(self):
        assert sb.compute_factor_ic({}, index_closes=[]) == {"n": 0}

    def test_default_fetches_kline(self, monkeypatch):
        hist, kline = _synth()
        called = []

        def fake_fetch(symbol, lmt=160):
            called.append(symbol)
            return kline
        monkeypatch.setattr(sb, "_fetch_kline", fake_fetch)
        out = sb.compute_factor_ic(hist)  # index_closes=None → 现场拉上证日K
        assert called == [sb.MARKET_INDEX["symbol"]]
        assert out["n"] == 30

    def test_no_kline(self, monkeypatch):
        hist, _ = _synth()
        monkeypatch.setattr(sb, "_fetch_kline", lambda symbol, lmt=160: [])
        assert sb.compute_factor_ic(hist) == {"n": 0}


# ============================================================
# _direction_analysis IC 加权
# ============================================================

def _env_for_analysis():
    """对冲 +1（IC/IM 贴水收敛）、汇率 -1（日元急升）、其余维度 0 的输入环境"""
    tech = {"上证指数": {"available": True, "price": 3930.0, "change_pct": 0.5,
                     "trend": "多头排列", "vol_ratio5": 1.0,
                     "breakout": False, "breakdown": False}}
    fx = {"fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": -2.0}}
    history = {"IC": [-1.4, -1.2, -1.0], "IM": [-1.5, -1.3, -1.1]}
    return tech, fx, history


class TestDirectionAnalysisWeights:
    def test_equal_weight_neutral(self):
        tech, fx, history = _env_for_analysis()
        a = fc._direction_analysis(tech, {}, fx, "neutral", history, vol={}, breadth={})
        assert a["direction"] == "中性"        # (+1-1)/6 = 0
        assert a["weighted"] is False
        assert a["weights"] == {}

    def test_weighted_flips_direction(self):
        tech, fx, history = _env_for_analysis()
        # 对冲权重 2.0、其余地板 0.1：
        # (2.0×1 + 0.1×(-1) + 0.1×0×4) / (2.0 + 0.1×5) = 1.9/2.5 = 0.76 → 偏多
        a = fc._direction_analysis(tech, {}, fx, "neutral", history, vol={}, breadth={},
                                   weights={"对冲": 2.0})
        assert a["score"] == pytest.approx(0.76, abs=0.01)
        assert a["direction"] == "偏多"
        assert a["weighted"] is True
        assert a["weights"] == {"对冲": 2.0}

    def test_uncovered_dim_gets_floor(self):
        tech, fx, history = _env_for_analysis()
        # 只给"汇率"权重 2.0：对冲按地板 0.1 参与
        # (0.1×1 + 2.0×(-1)) / (0.1×5 + 2.0) = -1.9/2.5 = -0.76 → 偏空
        a = fc._direction_analysis(tech, {}, fx, "neutral", history, vol={}, breadth={},
                                   weights={"汇率": 2.0})
        assert a["score"] == pytest.approx(-0.76, abs=0.01)
        assert a["direction"] == "偏空"

    def test_none_weights_backward_compatible(self):
        tech, fx, history = _env_for_analysis()
        a = fc._direction_analysis(tech, {}, fx, "neutral", history, vol={}, breadth={},
                                   weights=None)
        assert a["direction"] == "中性"


# ============================================================
# format_direction_signal IC 加权展示
# ============================================================

class TestDirectionSignalWeightedDisplay:
    def _analysis(self, with_weights=True):
        a = {
            "direction": "偏多", "score": 0.76,
            "factors": [("对冲", 1.0, "IC/IM贴水收敛"),
                        ("汇率", -1.0, "日元急升 -2.00%（套息平仓风险）")],
        }
        if with_weights:
            a["weights"] = {"对冲": 2.0, "汇率": 0.1}
            a["ic_n"] = 30
        return a

    def test_weighted_tags_shown(self):
        out = fc.format_direction_signal(self._analysis(), "中性")
        assert "（IC加权 n=30）" in out
        assert "对冲（权重2.0）" in out
        assert "汇率（权重0.1）" in out
        assert "▲ 利好" in out and "▼ 利空" in out

    def test_without_weights_no_tags(self):
        out = fc.format_direction_signal(self._analysis(with_weights=False), "中性")
        assert "IC加权" not in out
        assert "权重" not in out
        assert "量化方向：利好" in out

    def test_weights_without_n_no_suffix(self):
        a = self._analysis()
        a.pop("ic_n")   # 有权重无样本数 → 只展示维度权重，不加 n 标注
        out = fc.format_direction_signal(a, "中性")
        assert "IC加权" not in out
        assert "对冲（权重2.0）" in out


# ============================================================
# real_time_push 简报块
# ============================================================

class TestSnapshotBlockICTag:
    def _factor_state(self, with_ic=True):
        import datetime as _dt
        today = _dt.datetime.now(rtp.BJT).strftime("%Y-%m-%d")
        fs = {"snapshot": {"ts": "2026-08-19 15:00", "risk_state": "neutral",
                           "indexes": {"上证指数": {"price": 3930.0, "change_pct": 0.8, "trend": ""}}},
              "last_direction": "偏多",
              # 2026-09-01 起 _snapshot_block 要求 direction_history 含当日键才
              # 视为当日结论（否则降级"打分日期未知"）——测试构造当日打分。
              "direction_history": {today: {"dir": "偏多", "score": 0.5, "factors": {}}}}
        if with_ic:
            fs["factor_ic"] = {"n": 25, "ic": {"对冲": 0.3}, "weights": {"对冲": 0.4}}
        return fs

    def test_ic_tag_shown(self):
        joined = "\n".join(rtp._snapshot_block(self._factor_state(), "因子环境"))
        assert "量化综合方向: 偏多（IC加权，n=25）" in joined

    def test_without_ic_no_tag(self):
        joined = "\n".join(rtp._snapshot_block(self._factor_state(with_ic=False), "因子环境"))
        assert "量化综合方向: 偏多" in joined
        assert "IC加权" not in joined

    def test_ic_without_weights_no_tag(self):
        fs = self._factor_state()
        fs["factor_ic"] = {"n": 5}   # 样本不足（等权回退中）
        joined = "\n".join(rtp._snapshot_block(fs, "因子环境"))
        assert "IC加权" not in joined
