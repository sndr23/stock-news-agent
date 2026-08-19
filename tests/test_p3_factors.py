# filepath: tests/test_p3_factors.py
"""P3 复审补齐因子单元测试（2026-08-19）

覆盖（对标机构级因子体系的四大缺口）：
1. P3-1 fetch_global_quotes（腾讯美股/港股解析）/ detect_global_anomalies（阈值与升级）
2. P3-2 fetch_market_breadth（东财涨跌分布桶语义）/ detect_breadth_anomalies（跌停潮/极端普跌）
3. P3-3 calc_vol_regime（合成K线：平稳段+尖峰段 → 高波分位）
4. P3-4 calc_style_rotation（合成K线：比价趋势 → 大盘/小盘占优）
5. _direction_analysis 六维合成（波动率/宽度投票）
6. build_snapshot / format_snapshot 新键
7. real_time_push 三展示端（市场环境行/LLM上下文/简报块）
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import factor_collector as fc  # noqa: E402
import real_time_push as rtp  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送

BJT = timezone(timedelta(hours=8))


def _sample_snapshot(ts=None) -> dict:
    return {
        "ts": ts or datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "risk_state": "risk_off",
        "indexes": {"上证指数": {"price": 3894.42, "change_pct": -2.40, "trend": "均线纠缠"}},
        "basis": {"IC": {"basis_pct": -0.92, "annual_pct": -11.17}},
        "fx": {"美元/日元": {"price": 159.19, "change_pct": -0.43}},
        "flows": {"main_net_yi": -1939.6},
        "global": {
            "纳斯达克100": {"price": 29490.96, "change_pct": -1.68},
            "英伟达": {"price": 219.74, "change_pct": -2.34},
        },
        "breadth": {"adv": 428, "dec": 4885, "down_pct": 91.6,
                    "limit_up": 36, "limit_down": 118, "big_down": 2189},
        "vol": {"上证指数": {"vol20": 28.5, "pctile": 85, "regime": "高波"}},
        "style": {"ratio": 0.2915, "chg5": 0.3, "chg20": -2.1, "trend": "小盘占优"},
    }


# ============================================================
# P3-1: fetch_global_quotes / detect_global_anomalies
# ============================================================

class TestFetchGlobalQuotes:
    def test_parse_tencent_payload(self, monkeypatch):
        def mk(var, name, price, chg):
            parts = ["1", name, "IDX", str(price), str(price * 1.01)] + [""] * 27 + [str(chg)]
            parts += [""] * 5  # 补齐字段数
            return f'{var}="{"~".join(parts)}";'

        text = (mk("v_usNDX", "纳斯达克100", 29490.96, -1.68)
                + mk("v_usNVDA", "英伟达", 219.74, -2.34)
                + mk("v_hkHSTECH", "恒生科技指数", 4682.05, -1.21))
        monkeypatch.setattr(fc, "_http_get",
                            lambda url, params=None, headers=None, encoding=None: text)
        out = fc.fetch_global_quotes()
        assert set(out) == {"纳斯达克100", "英伟达", "恒生科技指数"}
        assert out["英伟达"]["change_pct"] == -2.34
        assert out["纳斯达克100"]["price"] == 29490.96

    def test_empty_response(self, monkeypatch):
        monkeypatch.setattr(fc, "_http_get", lambda *a, **k: "")
        assert fc.fetch_global_quotes() == {}


class TestDetectGlobalAnomalies:
    def test_big_drop_warning(self):
        sigs = fc.detect_global_anomalies({"英伟达": {"price": 219.74, "change_pct": -3.5}})
        assert len(sigs) == 1
        s = sigs[0]
        assert s["key"] == "global_英伟达"
        assert s["level"] == "warning"   # |chg|≥3% 升级 warning → 联动 risk_off
        assert s["direction"] == "bearish"
        assert "隔夜" in s["title"]

    def test_moderate_move_info(self):
        sigs = fc.detect_global_anomalies({"纳斯达克100": {"price": 29490.0, "change_pct": -2.1}})
        assert sigs[0]["level"] == "info"

    def test_below_threshold_no_signal(self):
        assert fc.detect_global_anomalies({"标普500": {"price": 7691.0, "change_pct": -0.69}}) == []

    def test_empty_input(self):
        assert fc.detect_global_anomalies({}) == []


# ============================================================
# P3-2: fetch_market_breadth / detect_breadth_anomalies
# ============================================================

class TestFetchMarketBreadth:
    def test_bucket_semantics(self, monkeypatch):
        # 2026-08-19 真实返回结构（桶语义与涨跌停池交叉验证）
        fenbu = [{"-1": 279}, {"-10": 400}, {"-11": 118}, {"-6": 520},
                 {"0": 22}, {"1": 157}, {"11": 36}]
        text = json.dumps({"data": {"qdate": 20260819, "fenbu": fenbu}})
        monkeypatch.setattr(fc, "_http_get",
                            lambda url, params=None, headers=None, encoding=None: text)
        b = fc.fetch_market_breadth()
        assert b["adv"] == 157 + 36          # 正桶
        assert b["dec"] == 279 + 400 + 118 + 520
        assert b["flat"] == 22
        assert b["limit_down"] == 118        # -11 桶 ≈ 跌停（与跌停池 tc 一致）
        assert b["limit_up"] == 36           # 11 桶 ≈ 涨停（与涨停池 tc 一致）
        assert b["big_down"] == 400 + 118 + 520  # ≤-6 桶（-10/-11/-6）= 跌超5%
        assert b["down_pct"] == round(1317 / 1532 * 100, 1)  # dec/(adv+dec+flat)

    def test_empty_response(self, monkeypatch):
        monkeypatch.setattr(fc, "_http_get", lambda *a, **k: "")
        assert fc.fetch_market_breadth() == {}


class TestDetectBreadthAnomalies:
    def test_limit_down_wave(self):
        b = {"adv": 428, "dec": 4885, "down_pct": 91.6,
             "limit_up": 36, "limit_down": 118, "big_down": 2189}
        sigs = fc.detect_breadth_anomalies(b)
        keys = {s["key"] for s in sigs}
        assert keys == {"breadth_limit_down", "breadth_down_pct"}
        ld = next(s for s in sigs if s["key"] == "breadth_limit_down")
        assert ld["level"] == "warning"      # 跌停潮 → risk_off 联动
        assert "118" in ld["title"]

    def test_normal_breadth_no_signal(self):
        b = {"adv": 2600, "dec": 2400, "down_pct": 47.0,
             "limit_up": 60, "limit_down": 8, "big_down": 120}
        assert fc.detect_breadth_anomalies(b) == []

    def test_empty_input(self):
        assert fc.detect_breadth_anomalies({}) == []


# ============================================================
# P3-3: calc_vol_regime
# ============================================================

def _klines_from_returns(rets, start=100.0):
    """合成日K：按收益率序列生成收盘价，high/low 覆盖日内波幅"""
    out, price = [], start
    for i, r in enumerate(rets):
        price *= (1 + r)
        out.append({"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": price * 0.99, "close": price,
                    "high": price * 1.01, "low": price * 0.98, "volume": 1000})
    return out


class TestCalcVolRegime:
    def test_spike_after_calm_is_high_vol(self):
        # 前 200 日 ±0.3% 平稳，后 20 日 ±3% 尖峰 → 高波（分位≈100）
        rets = [0.003 if i % 2 == 0 else -0.003 for i in range(200)]
        rets += [0.03 if i % 2 == 0 else -0.03 for i in range(20)]
        v = fc.calc_vol_regime(_klines_from_returns(rets))
        assert v["available"] is True
        assert v["regime"] == "高波"
        assert v["pctile"] >= fc.TH_VOL_PCTILE_HIGH

    def test_calm_market_is_low_vol(self):
        # 全程 ±0.2% 极平稳 → 低波（分位≈50，因为最后一段与历史同分布）
        # 构造：前段 ±1%，末段 ±0.1% → 分位极低
        rets = [0.01 if i % 2 == 0 else -0.01 for i in range(220)]
        rets += [0.001 if i % 2 == 0 else -0.001 for i in range(20)]
        v = fc.calc_vol_regime(_klines_from_returns(rets))
        assert v["available"] is True
        assert v["pctile"] <= fc.TH_VOL_PCTILE_LOW
        assert v["regime"] == "低波"

    def test_short_history_unavailable(self):
        assert fc.calc_vol_regime(_klines_from_returns([0.01] * 30))["available"] is False
        assert fc.calc_vol_regime([])["available"] is False


# ============================================================
# P3-4: calc_style_rotation
# ============================================================

class TestCalcStyleRotation:
    def _mk(self, monkeypatch, big_rets, small_rets):
        def fake_kline(symbol, lmt=65):
            if "000016" in symbol:      # 上证50
                series = _klines_from_returns(big_rets)
            else:                        # 中证1000
                series = _klines_from_returns(small_rets)
            return series[-lmt:]
        monkeypatch.setattr(fc, "fetch_index_kline", fake_kline)

    def test_small_cap_outperform(self, monkeypatch):
        # 大盘横盘、小盘20日涨10% → 比价下降 → 小盘占优
        self._mk(monkeypatch, [0.0] * 65, [0.0] * 45 + [0.005] * 20)
        s = fc.calc_style_rotation()
        assert s["trend"] == "小盘占优"
        assert s["chg20"] < 0

    def test_big_cap_outperform(self, monkeypatch):
        self._mk(monkeypatch, [0.0] * 45 + [0.005] * 20, [0.0] * 65)
        s = fc.calc_style_rotation()
        assert s["trend"] == "大盘占优"

    def test_balanced(self, monkeypatch):
        self._mk(monkeypatch, [0.001] * 65, [0.001] * 65)
        s = fc.calc_style_rotation()
        assert s["trend"] == "风格均衡"


# ============================================================
# 六维方向合成
# ============================================================

class TestDirectionAnalysisSixDims:
    def _run(self, vol=None, breadth=None):
        # 基差持续走扩 → 对冲维度出现（空 history 时对冲维度缺省，仅 5 维）
        history = {"IC": [{"d": f"2026-08-{i:02d}", "v": -0.5 - i * 0.01} for i in range(1, 6)],
                   "IM": [{"d": f"2026-08-{i:02d}", "v": -0.6 - i * 0.01} for i in range(1, 6)]}
        return fc._direction_analysis({}, {}, {}, "neutral", history, vol=vol, breadth=breadth)

    def test_six_dims_present(self):
        a = self._run()
        names = [d[0] for d in a["factors"]]
        # P7 影子维度（流动性/期权情绪）恒定追加在 6 主维度之后
        assert names == ["对冲", "风险", "量价", "汇率", "波动率", "宽度",
                         "流动性", "期权情绪"]
        assert a["shadow"] == set()  # 无数据时影子维度全 0 分不标注

    def test_high_vol_votes_bearish(self):
        a = self._run(vol={"上证指数": {"regime": "高波", "vol20": 28.5, "pctile": 85}})
        vol_dim = next(d for d in a["factors"] if d[0] == "波动率")
        assert vol_dim[1] == -1.0
        assert "高波" in vol_dim[2]

    def test_extreme_breadth_votes_bearish(self):
        a = self._run(breadth={"down_pct": 91.6})
        bd = next(d for d in a["factors"] if d[0] == "宽度")
        assert bd[1] == -1.0

    def test_broad_rally_votes_bullish(self):
        a = self._run(breadth={"down_pct": 12.0})
        bd = next(d for d in a["factors"] if d[0] == "宽度")
        assert bd[1] == 1.0

    def test_neutral_breadth_zero(self):
        a = self._run(breadth={"down_pct": 50.0})
        bd = next(d for d in a["factors"] if d[0] == "宽度")
        assert bd[1] == 0.0


# ============================================================
# 快照构建与展示
# ============================================================

class TestSnapshotNewKeys:
    def test_build_snapshot_new_keys(self):
        snap = fc.build_snapshot(
            {}, {}, {}, "risk_off",
            global_quotes={"英伟达": {"price": 219.74, "change_pct": -2.34}},
            breadth={"adv": 428, "dec": 4885, "down_pct": 91.6,
                     "limit_up": 36, "limit_down": 118, "big_down": 2189},
            vol={"上证指数": {"available": True, "vol20": 28.5, "pctile": 85, "regime": "高波"}},
            style={"ratio": 0.2915, "chg5": 0.3, "chg20": -2.1, "trend": "小盘占优"},
        )
        assert snap["global"]["英伟达"]["change_pct"] == -2.34
        assert snap["breadth"]["limit_down"] == 118
        assert snap["vol"]["上证指数"]["regime"] == "高波"
        assert snap["style"]["trend"] == "小盘占优"

    def test_build_snapshot_omits_empty(self):
        snap = fc.build_snapshot({}, {}, {}, "neutral")
        for k in ("global", "breadth", "vol", "style"):
            assert k not in snap

    def test_format_snapshot_new_sections(self):
        text = fc.format_snapshot(
            {}, {}, {},
            global_quotes={"英伟达": {"price": 219.74, "change_pct": -2.34}},
            breadth={"adv": 428, "dec": 4885, "down_pct": 91.6,
                     "limit_up": 36, "limit_down": 118, "big_down": 2189},
            vol={"上证指数": {"available": True, "vol20": 28.5, "pctile": 85, "regime": "高波"}},
            style={"ratio": 0.2915, "chg5": 0.3, "chg20": -2.1, "trend": "小盘占优"},
        )
        assert "### 隔夜外盘" in text
        assert "英伟达" in text and "⚠️异动" in text
        assert "### 宽度与波动率" in text
        assert "跌停 118" in text
        assert "⚠️高波" in text
        assert "小盘占优" in text


# ============================================================
# real_time_push 三展示端
# ============================================================

class TestPushDisplayIntegration:
    def test_factor_env_line_new_dims(self):
        line = rtp._factor_env_line(_sample_snapshot())
        assert "隔夜纳指-1.68%" in line
        assert "⚠️隔夜英伟达-2.34%" in line
        assert "⚠️高波" in line
        assert "⚠️普跌92%" in line

    def test_factor_env_line_without_new_keys(self):
        snap = _sample_snapshot()
        for k in ("global", "breadth", "vol"):
            snap.pop(k)
        line = rtp._factor_env_line(snap)
        assert "纳指" not in line and "高波" not in line and "普跌" not in line

    def test_llm_env_context_new_dims(self):
        ctx = rtp._llm_env_context(_sample_snapshot())
        assert "隔夜纳斯达克100-1.68%、英伟达-2.34%" in ctx
        assert "AI硬件链" in ctx
        assert "高波" in ctx
        assert "极端普跌" in ctx and "跌停118家" in ctx

    def test_snapshot_block_new_lines(self):
        state = {"snapshot": _sample_snapshot(), "last_direction": "偏空"}
        lines = rtp._snapshot_block(state, "盘前因子环境")
        text = "\n".join(lines)
        assert "- 隔夜外盘:" in text and "英伟达" in text
        assert "- 市场宽度:" in text and "跌停118" in text
        assert "- 波动率:" in text and "高波" in text
        assert "- 风格轮动:" in text and "小盘占优" in text
