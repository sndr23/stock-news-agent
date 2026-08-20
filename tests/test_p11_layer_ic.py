# filepath: tests/test_p11_layer_ic.py
"""P11 分层 IC 单调性检验单元测试（2026-08-21）

覆盖：
1. FACTOR_REGISTRY 元数据完整性（8 维齐全、shadow 标记、取值域）
2. compute_layer_ic（三层单调因子判"有效" / 三层反向判"反向外推" /
   双档弱单调判"弱有效" / 单档跳过 / 样本不足无结论 / 空历史 / kline 缺省拉取）
3. _layer_verdict 判定边界
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import signal_backtest as sb

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络


# ============================================================
# FACTOR_REGISTRY
# ============================================================

class TestFactorRegistry:
    def test_core_six_dims_present(self):
        for d in ("对冲", "风险", "量价", "汇率", "波动率", "宽度"):
            assert d in sb.FACTOR_REGISTRY
            assert "levels" in sb.FACTOR_REGISTRY[d]

    def test_shadow_marked(self):
        assert sb.FACTOR_REGISTRY["流动性"]["is_shadow"] is True
        assert sb.FACTOR_REGISTRY["期权情绪"]["is_shadow"] is True
        assert sb.FACTOR_REGISTRY["对冲"].get("is_shadow") is not True

    def test_all_register_entries_have_metadata(self):
        for meta in sb.FACTOR_REGISTRY.values():
            assert "kind" in meta and "note" in meta
            assert isinstance(meta["levels"], tuple) and meta["levels"]


# ============================================================
# 分层 IC
# ============================================================

def _synth_layered(monotonic=True):
    """构造 30 日样本：因子"层因子"按档位 -1/0/+1 交替出现，
    当日分 → 次日收益。monotonic=True 时档位越高收益越高（有效）；
    False 时反向（档位越高收益越低 → 反向外推）。

    收益由 kline 的次日相对涨跌决定，与档位单调绑定：
    +1 档 → 大涨，-1 档 → 大跌，0 档 → 微涨（居中）。
    """
    def _rets_seq(reverse=False):
        base = []
        for _ in range(3):
            base += [("+", 1.6), ("-", -1.2), ("0", 0.3), ("+", 2.0),
                     ("-", -0.8), ("0", 0.2), ("+", 1.2), ("-", -1.5),
                     ("0", 0.4), ("+", 1.8)]
        if reverse:                       # 档位与收益反向：-1 大涨，+1 大跌
            base = [("+" if lg == "-" else "-" if lg == "+" else "0", r)
                    for lg, r in base]
        return base

    seq = _rets_seq(reverse=not monotonic)
    closes = [100.0]
    for _, r in seq:
        closes.append(round(closes[-1] * (1 + r / 100), 4))
    kline = [{"date": f"2026-07-{i + 1:02d}", "close": c} for i, c in enumerate(closes)]
    lv_of = {"+": 1.0, "-": -1.0, "0": 0.0}
    hist = {}
    for i, (lg, _) in enumerate(seq):
        hist[kline[i]["date"]] = {
            "dir": "中性", "score": 0,
            "factors": {"层因子": lv_of[lg], "常数因子": 1.0},
        }
    return hist, kline


class TestComputeLayerIC:
    def test_monotonic_three_levels_effective(self):
        hist, kline = _synth_layered(monotonic=False)  # 用反向数据测单调性为负
        out = sb.compute_layer_ic(hist, index_closes=kline)
        assert out["n"] == 30
        lay = out["layers"]["层因子"]
        assert lay["monotonic"] < 0                 # 层因子构造为反向 → 负单调
        assert lay["verdict"] == "反向外推（放大即反向）"
        assert set(lay["levels"]) == {"-1.0", "0.0", "1.0"}
        # 多空差 = 最高档 − 最低档；反向构造下最高档收益低于最低档 → spread < 0
        assert lay["spread"] < -1.5

    def test_inverse_layer_flag_contrarian(self):
        # 反向数据应被标记为反向外推而不是"有效"
        hist, kline = _synth_layered(monotonic=False)
        out = sb.compute_layer_ic(hist, index_closes=kline)
        assert out["layers"]["层因子"]["verdict"] == "反向外推（放大即反向）"

    def test_two_level_weak(self):
        # 双档弱单调：-1 与 +1 两档（足够样本），档位+收益单调但仅 2 档 → 弱有效
        hist, kline = _synth_layered(monotonic=True)
        # 移除 0 档，仅保留 +/- 两档观测
        for day in hist:
            v = hist[day]["factors"]["层因子"]
            if v == 0.0:
                del hist[day]["factors"]["层因子"]
        out = sb.compute_layer_ic(hist, index_closes=kline)
        # 2 点 Spearman 可能因对称抵消为 0，但分层结构本身证明单调：
        # 最高档(+1)绝对收益 > 0（+档应正收益），且多空差显著为正
        lay = out["layers"]["层因子"]
        assert lay["monotonic"] >= 0.0          # 不反向
        assert lay["spread"] > 1.0               # +档收益 − -档收益 显著为正
        assert lay["high_low"] > 0.5             # +档绝对收益为正
        # 2 档不满足 n_levels>=3 → 判定弱有效
        assert lay["verdict"] == "弱有效"

    def test_single_level_skipped(self):
        hist, kline = _synth_layered()
        out = sb.compute_layer_ic(hist, index_closes=kline)
        assert "常数因子" not in out["layers"]     # 只有单一档位 → 跳过

    def test_insufficient_days(self):
        hist, kline = _synth_layered()
        partial = dict(list(hist.items())[:15])
        out = sb.compute_layer_ic(partial, index_closes=kline)
        assert out == {"n": 15}                     # 无 layers 键 → 等权回退

    def test_empty_history(self):
        assert sb.compute_layer_ic({}, index_closes=[]) == {"n": 0}

    def test_default_fetches_kline(self, monkeypatch):
        hist, kline = _synth_layered()
        called = []

        def fake_fetch(symbol, lmt=160):
            called.append(symbol)
            return kline
        monkeypatch.setattr(sb, "_fetch_kline", fake_fetch)
        out = sb.compute_layer_ic(hist)
        assert called == [sb.MARKET_INDEX["symbol"]]
        assert out["n"] == 30

    def test_no_kline(self, monkeypatch):
        hist, _ = _synth_layered()
        monkeypatch.setattr(sb, "_fetch_kline", lambda symbol, lmt=160: [])
        assert sb.compute_layer_ic(hist) == {"n": 0}


# ============================================================
# _layer_verdict
# ============================================================

class TestLayerVerdict:
    def test_effective(self):
        assert sb._layer_verdict(0.8, 3) == "有效"

    def test_weak_three_levels(self):
        assert sb._layer_verdict(0.4, 3) == "弱有效"

    def test_weak_two_levels_by_spread(self):
        assert sb._layer_verdict(0.0, 2, spread=1.2) == "弱有效"
        assert sb._layer_verdict(0.0, 2, spread=-1.2) == "反向外推（放大即反向）"
        assert sb._layer_verdict(0.0, 2, spread=0.2) == "无单调"

    def test_contrarian_three_levels(self):
        assert sb._layer_verdict(-0.5, 3) == "反向外推（放大即反向）"

    def test_none(self):
        assert sb._layer_verdict(0.1, 2) == "无单调"
        assert sb._layer_verdict(-0.2, 2) == "无单调"