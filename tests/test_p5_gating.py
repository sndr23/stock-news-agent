# filepath: tests/test_p5_gating.py
"""P5 单元测试（2026-08-19）：非线性门控 + 确信度分层推送 + 数据健康度

覆盖：
1. P5-1 _direction_analysis 门控：高波升权利空维度 / 套息平仓共振（普跌+日元急升）
   升权汇率与宽度 / 门控叠加（两个门控同时生效取乘积）/ 无门控时 eff_weights 等权
2. P5-2 run_once 确信度分层：弱翻转（|score|<0.67）不推送记 weak_direction /
   弱→强同向升级推送（escalated）/ 强翻转直接推送 / 简报弱信号行
3. P5-3 数据健康度：run_once 统计 sources / build_snapshot sources 键 /
   format_direction_signal 健康度警示 / 简报健康度行
4. format_direction_signal：门控归因行 / 强信号标注 / 确信度升级标注
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import factor_collector as fc  # noqa: E402
import real_time_push as rtp  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送


# ============================================================
# P5-1: 门控
# ============================================================

def _env(vol=None, breadth=None, fx=None):
    """构造分析环境：对冲走扩-1、风险 risk_off-1、汇率-1（日元急升）、其余 0"""
    tech = {"上证指数": {"available": True, "price": 3930.0, "change_pct": -0.5,
                     "trend": "空头排列", "vol_ratio5": 1.0,
                     "breakout": False, "breakdown": False}}
    fx = fx if fx is not None else {"fx_susdjpy": {"name": "美元/日元", "price": 156.0,
                                                    "change_pct": -2.0}}
    history = {"IC": [-1.4, -1.2, -1.0], "IM": [-1.5, -1.3, -1.1]}  # 贴水收敛→对冲+1？
    # 注意：值绝对值递减 = 贴水收敛 → 对冲+1；走扩用递增
    history = {"IC": [-1.0, -1.2, -1.4], "IM": [-1.1, -1.3, -1.5]}  # 递减→走扩→对冲-1
    return tech, fx, history


class TestGatingHighVol:
    def test_high_vol_upweights_bearish_dims(self):
        tech, fx, history = _env(vol={"上证指数": {"regime": "高波", "vol20": 28.5, "pctile": 85}},
                                 breadth={"down_pct": 50.0})
        a = fc._direction_analysis(tech, {}, fx, "risk_off", history,
                                   vol={"上证指数": {"regime": "高波", "vol20": 28.5,
                                                  "pctile": 85}},
                                   breadth={"down_pct": 50.0})
        # 等权无门控基线：(对冲-1+风险-1+量价0+汇率-1+波动率-1+宽度0)/6 = -0.67
        # 门控后：高波触发，利空维度(对冲/风险/汇率/波动率)×1.5，波动率本身-1
        # (-1.5-1.5+0-1.5-1.5+0) / (1.5+1.5+1+1.5+1.5+1) = -6/8 = -0.75
        assert a["score"] == pytest.approx(-0.75, abs=0.01)
        assert a["gates"] == ["高波状态·利空维度升权×1.5"]
        # 利空维度生效权重 1.5，中性维度 1.0
        assert a["eff_weights"]["对冲"] == 1.5
        assert a["eff_weights"]["量价"] == 1.0
        assert a["eff_weights"]["宽度"] == 1.0

    def test_high_vol_does_not_upweight_bullish(self):
        # 普涨环境（宽度+1、汇率+1 日元急贬），高波门控只升利空维度
        tech, fx, history = _env()
        fx = {"fx_susdjpy": {"name": "美元/日元", "price": 165.0, "change_pct": 2.0}}
        history = {"IC": [-1.4, -1.2, -1.0], "IM": [-1.5, -1.3, -1.1]}  # 收敛→对冲+1
        a = fc._direction_analysis(tech, {}, fx, "neutral", history,
                                   vol={"上证指数": {"regime": "高波", "vol20": 28.5, "pctile": 85}},
                                   breadth={"down_pct": 15.0})
        # 利空维度只有波动率(-1)；门控生效但其 eff_weights 中利好维度仍 1.0
        assert a["eff_weights"]["对冲"] == 1.0
        assert a["eff_weights"]["汇率"] == 1.0
        assert a["eff_weights"]["波动率"] == 1.5


class TestGatingCarryUnwind:
    def test_carry_unwind_resonance(self):
        # 极端普跌 91.6% + 日元急升 -2.0% → 汇率/宽度升权×1.5
        tech, fx, history = _env(breadth={"down_pct": 91.6})
        a = fc._direction_analysis(tech, {}, fx, "risk_off", history,
                                   vol={}, breadth={"down_pct": 91.6})
        # 6 维：对冲-1(1)、风险-1(1)、量价0(1)、汇率-1(1.5)、波动率0(1)、宽度-1(1.5)
        # = (-1-1-1.5-1.5)/7 ≈ -0.71（共振门控后比等权 -0.67 更极端）
        assert a["score"] == pytest.approx(-0.71, abs=0.01)
        assert len(a["gates"]) == 1
        assert "套息平仓共振" in a["gates"][0]
        assert a["eff_weights"]["汇率"] == 1.5
        assert a["eff_weights"]["宽度"] == 1.5
        assert a["eff_weights"]["对冲"] == 1.0

    def test_no_resonance_without_jpy_spike(self):
        # 普跌但日元平稳（-0.3%）→ 共振门控不触发，纯等权
        tech, fx, history = _env(breadth={"down_pct": 91.6})
        fx = {"fx_susdjpy": {"name": "美元/日元", "price": 159.0, "change_pct": -0.3}}
        a = fc._direction_analysis(tech, {}, fx, "neutral", history,
                                   vol={}, breadth={"down_pct": 91.6})
        # 对冲-1 + 宽度-1，其余 0 → -2/6 ≈ -0.33
        assert a["gates"] == []
        assert a["score"] == pytest.approx(-0.33, abs=0.01)

    def test_no_resonance_without_extreme_breadth(self):
        # 日元急升但普跌不极端（50%）→ 共振门控不触发
        tech, fx, history = _env(breadth={"down_pct": 50.0})
        a = fc._direction_analysis(tech, {}, fx, "neutral", history,
                                   vol={}, breadth={"down_pct": 50.0})
        assert a["gates"] == []

    def test_gates_stack(self):
        # 高波 + 普跌+日元急升 → 两个门控同时生效（利空维度含汇率/宽度各 ×2.25）
        vol = {"上证指数": {"regime": "高波", "vol20": 28.5, "pctile": 85}}
        tech, fx, history = _env(vol=vol, breadth={"down_pct": 91.6})
        a = fc._direction_analysis(tech, {}, fx, "risk_off", history,
                                   vol=vol, breadth={"down_pct": 91.6})
        assert len(a["gates"]) == 2
        # 汇率：受两个门控（利空×1.5、共振×1.5）→ 2.25；宽度：只受共振门控 1.5（宽度本身-1 也是利空！）
        # 宽度是利空维度且在共振集合 → 1.5×1.5=2.25
        assert a["eff_weights"]["汇率"] == pytest.approx(2.25)
        assert a["eff_weights"]["宽度"] == pytest.approx(2.25)
        assert a["eff_weights"]["对冲"] == 1.5  # 只受高波门控
        assert a["score"] <= -0.86              # 共振叠加后更强

    def test_no_gates_equal_weights(self):
        tech, fx, history = _env(breadth={"down_pct": 50.0})
        a = fc._direction_analysis(tech, {}, fx, "neutral", history,
                                   vol={}, breadth={"down_pct": 50.0})
        assert a["gates"] == []
        assert set(a["eff_weights"].values()) == {1.0}  # 全部等权 1.0


class TestGatingWithICWeights:
    def test_gate_multiplies_ic_weight(self):
        # IC 权重（对冲 2.0）× 高波门控（对冲-1 利空 → ×1.5）→ 生效 3.0
        vol = {"上证指数": {"regime": "高波", "vol20": 28.5, "pctile": 85}}
        tech, fx, history = _env(vol=vol, breadth={"down_pct": 50.0})
        a = fc._direction_analysis(tech, {}, fx, "risk_off", history,
                                   vol=vol, breadth={"down_pct": 50.0},
                                   weights={"对冲": 2.0})
        assert a["eff_weights"]["对冲"] == pytest.approx(3.0)
        assert a["eff_weights"]["量价"] == pytest.approx(0.1)  # 地板不受门控影响（非利空）


# ============================================================
# P5-1/P5-2/P5-3: format_direction_signal 展示
# ============================================================

class TestSignalDisplay:
    def _analysis(self, **kw):
        a = {
            "direction": "偏空", "score": -0.86,
            "factors": [("汇率", -1.0, "日元急升 -2.00%（套息平仓风险）"),
                        ("宽度", -1.0, "极端普跌（92%个股下跌）")],
            "eff_weights": {"汇率": 1.5, "宽度": 1.5},
            "gates": ["套息平仓共振（普跌+日元急升）·汇率/宽度升权×1.5"],
        }
        a.update(kw)
        return a

    def test_gate_attribution_line(self):
        out = fc.format_direction_signal(self._analysis(), "中性")
        assert "共振门控：套息平仓共振（普跌+日元急升）·汇率/宽度升权×1.5" in out
        assert "汇率（权重1.5）" in out
        assert "宽度（权重1.5）" in out

    def test_strong_signal_tag(self):
        out = fc.format_direction_signal(self._analysis(), "中性")
        assert "（强信号）" in out  # |−0.86| ≥ 0.67

    def test_weak_score_no_strong_tag(self):
        out = fc.format_direction_signal(self._analysis(score=-0.56), "中性")
        assert "强信号" not in out

    def test_escalated_tag(self):
        out = fc.format_direction_signal(self._analysis(escalated=True), "偏空")
        assert "（确信度升级）" in out

    def test_health_warning_when_low(self):
        out = fc.format_direction_signal(self._analysis(), "中性", sources={"ok": 5, "total": 11})
        assert "⚠️ 数据健康度 5/11" in out
        assert "本信号基于部分数据" in out

    def test_no_health_warning_when_ok(self):
        out = fc.format_direction_signal(self._analysis(), "中性", sources={"ok": 10, "total": 11})
        assert "数据健康度" not in out

    def test_none_sources_compat(self):
        out = fc.format_direction_signal(self._analysis(), "中性")
        assert "数据健康度" not in out


# ============================================================
# P5-2: run_once 确信度分层（e2e mock）
# ============================================================

def _mock_base(monkeypatch, tmp_path):
    """基础 mock：与 test_factor_e2e 同口径（全数据源空 → 无异动）"""
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.setattr(fc, "STATE_PATH", tmp_path / "factor_state.json")
    monkeypatch.setattr(fc, "fetch_index_quotes", lambda: {
        "上证指数": {"price": 3930.0, "prev_close": 3900.0, "change_pct": 0.8, "amount_wan": 1.0},
        "创业板指": {"price": 3630.0, "prev_close": 3600.0, "change_pct": 0.8, "amount_wan": 1.0},
        "沪深300": {"price": 4665.0, "prev_close": 4660.0, "change_pct": 0.1, "amount_wan": 1.0},
        "中证500": {"price": 7990.0, "prev_close": 7990.0, "change_pct": 0.0, "amount_wan": 1.0},
        "中证1000": {"price": 7770.0, "prev_close": 7770.0, "change_pct": 0.0, "amount_wan": 1.0},
        "上证50": {"price": 2915.0, "prev_close": 2915.0, "change_pct": 0.0, "amount_wan": 1.0},
    })
    monkeypatch.setattr(fc, "fetch_fx", lambda: {
        "fx_susdjpy": {"name": "美元/日元", "price": 159.0, "change_pct": -0.3},
        "fx_susdcny": {"name": "美元/人民币", "price": 6.74, "change_pct": -0.01},
    })
    monkeypatch.setattr(fc, "fetch_index_futures", lambda: {
        "IF": {"price": 4650.0, "prev_settle": 4640.0},
        "IC": {"price": 7985.0, "prev_settle": 7900.0},   # 浅贴水 -0.06%（配合预置收敛序列）
        "IM": {"price": 7765.0, "prev_settle": 7680.0},
        "IH": {"price": 2900.0, "prev_settle": 2910.0},
    })

    def fake_kline(symbol, lmt=260):
        return [{"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                 "open": 100 + i, "close": 100 + i,
                 "high": 101 + i, "low": 99 + i, "volume": 100} for i in range(65)]
    monkeypatch.setattr(fc, "fetch_index_kline", fake_kline)
    monkeypatch.setattr(fc, "fetch_stock_quotes", lambda symbols: {})
    monkeypatch.setattr(fc, "fetch_market_flows", lambda: {})
    monkeypatch.setattr(fc, "fetch_global_quotes", lambda: {})
    monkeypatch.setattr(fc, "fetch_market_breadth", lambda: {})
    monkeypatch.setattr(fc, "calc_style_rotation", lambda: {})
    monkeypatch.setattr(fc, "fetch_zt_sentiment", lambda: {})
    monkeypatch.setattr(fc, "fetch_sector_flows", lambda: {})
    # P7（2026-08-19）：资金面/期权同样不触网（空数据 → 影子维度 0 分，不进合成）
    monkeypatch.setattr(fc, "fetch_liquidity", lambda: {})
    monkeypatch.setattr(fc, "fetch_option_pcr", lambda *a, **k: {})
    # P8（2026-08-19）：分钟K线不触网（空数据 → 分钟影子因子 0 分）
    monkeypatch.setattr(fc, "fetch_minute_kline", lambda *a, **kw: [])
    import signal_backtest as sb  # noqa: E402
    monkeypatch.setattr(sb, "compute_winrate", lambda days=30: {"n": 0})
    monkeypatch.setattr(sb, "compute_factor_ic",
                        lambda history, index_closes=None: {"n": 0})

    pushed = []
    monkeypatch.setattr(fc, "do_push", lambda title, content: (
        pushed.append({"title": title, "content": content}) or {"code": 200}))
    return pushed


class TestConfidenceTiering:
    def test_weak_flip_not_pushed(self, tmp_path, monkeypatch):
        """弱翻转（|score|∈[0.5,0.67)）不单独推送，记 weak_direction 供简报"""
        pushed = _mock_base(monkeypatch, tmp_path)
        # 构造恰好弱偏多 +0.50（6 维：对冲+1、量价+1、宽度+1，风险/汇率/波动率 0）：
        # - 预置收敛基差序列 + 当日浅贴水延续 → 对冲+1
        # - 温和放量突破（递增序列+末日放量）→ 量价+1（info 信号不切 risk_off）
        # - 普涨 dp=15 → 宽度+1；日元温和 -0.3%（急贬会触发 warning 切 risk_off，避开）
        def up_kline(symbol, lmt=260):
            ks = [{"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                   "open": 100 + i * 0.5, "close": 100 + i * 0.5,
                   "high": 101 + i * 0.5, "low": 99 + i * 0.5, "volume": 100}
                  for i in range(64)]
            last = {"date": "2026-08-19", "open": 132, "close": 132, "high": 133,
                    "low": 131, "volume": 300}  # 温和新高 + 放量3倍
            return ks + [last]
        monkeypatch.setattr(fc, "fetch_index_kline", up_kline)
        monkeypatch.setattr(fc, "fetch_market_breadth", lambda: {
            "adv": 4200, "dec": 500, "flat": 100, "down_pct": 15.0,
            "limit_up": 60, "limit_down": 5, "big_down": 30,
        })
        basis_history = {  # 收敛序列，当日 -0.06 延续 → 收敛 → 对冲+1
            "IC": [{"d": f"2026-08-1{i}", "v": v} for i, v in zip("456", (-1.4, -1.2, -1.0))],
            "IM": [{"d": f"2026-08-1{i}", "v": v} for i, v in zip("456", (-1.5, -1.3, -1.1))],
        }
        fc._save_state({"last_direction": "中性", "basis_history": basis_history,
                        "risk_state": "neutral"})
        fc.run_once(push=True)

        titles = [p["title"] for p in pushed]
        assert "量化方向信号" not in titles          # 弱翻转不单独推送
        state = fc._load_state()
        assert state["last_direction"] == "偏多"       # 方向仍更新
        assert state["weak_direction"]["dir"] == "偏多"
        assert 0.5 <= abs(state["weak_direction"]["score"]) < fc.STRONG_DIR_THRESHOLD
        # 快照含数据健康度（mock 环境 6/14 源成功：行情/期货/汇率/K线/波动率/宽度；
        # P7 后源总数 11→13（资金面利率/期权PCR），P8 后 13→14（分钟K线））
        assert state["snapshot"]["sources"]["total"] == 13  # 期权PCR 影子因子下线（OPTION_PCR_ENABLED=False）后 13 源
        assert state["snapshot"]["sources"]["ok"] == 6

    def test_weak_then_strong_escalates(self, tmp_path, monkeypatch):
        """弱翻转未推 → 同向增强到强信号 → 升级推送（防漏报）"""
        pushed = _mock_base(monkeypatch, tmp_path)
        # 第一轮：默认 kline + 日元急升（5 维：风险-1、汇率-1，对冲单期缺省）→
        # -2/5 = -0.4 中性 → 不推方向信号（只推"量化因子异动"）
        monkeypatch.setattr(fc, "fetch_fx", lambda: {
            "fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": -2.0},
            "fx_susdcny": {"name": "美元/人民币", "price": 6.74, "change_pct": -0.01},
        })
        fc._save_state({"last_direction": "中性", "basis_history": {}, "risk_state": "neutral"})
        fc.run_once(push=True)
        assert "量化方向信号" not in [p["title"] for p in pushed]

        # 预置弱翻转状态：last_dir 已更新为偏空，weak_direction 留痕，清冷却
        state = fc._load_state()
        state["weak_direction"] = {"dir": "偏空", "score": -0.56, "ts": "2026-08-19 10:00"}
        state["last_direction"] = "偏空"
        state.pop("change_cooldown", None)
        fc._save_state(state)

        # 第二轮：同向增强到强信号（普跌91.6+日元急升 → 共振门控 → -0.67）
        monkeypatch.setattr(fc, "fetch_market_breadth", lambda: {
            "adv": 128, "dec": 4885, "flat": 22, "down_pct": 91.6,
            "limit_up": 36, "limit_down": 118, "big_down": 2189,
        })
        pushed.clear()
        fc.run_once(push=True)
        titles = [p["title"] for p in pushed]
        assert "量化方向信号" in titles  # 弱→强升级触发推送
        content = next(p["content"] for p in pushed if p["title"] == "量化方向信号")
        assert "确信度升级" in content
        assert "强信号" in content
        # weak_direction 推送后清除
        assert not fc._load_state().get("weak_direction")

    def test_strong_flip_pushes_directly(self, tmp_path, monkeypatch):
        """强翻转直接推送（无需弱信号铺垫）"""
        pushed = _mock_base(monkeypatch, tmp_path)
        monkeypatch.setattr(fc, "fetch_fx", lambda: {
            "fx_susdjpy": {"name": "美元/日元", "price": 156.0, "change_pct": -2.0},
            "fx_susdcny": {"name": "美元/人民币", "price": 6.74, "change_pct": -0.01},
        })
        monkeypatch.setattr(fc, "fetch_market_breadth", lambda: {
            "adv": 128, "dec": 4885, "flat": 22, "down_pct": 91.6,
            "limit_up": 36, "limit_down": 118, "big_down": 2189,
        })
        fc._save_state({"last_direction": "中性", "basis_history": {}, "risk_state": "neutral"})
        fc.run_once(push=True)
        titles = [p["title"] for p in pushed]
        assert "量化方向信号" in titles
        content = next(p["content"] for p in pushed if p["title"] == "量化方向信号")
        assert "强信号" in content
        assert "共振门控" in content  # 普跌+日元急升 → 门控归因
        assert "利空" in content


# ============================================================
# P5-3: 数据健康度
# ============================================================

class TestDataHealth:
    def test_run_once_records_sources(self, tmp_path, monkeypatch):
        """run_once 统计数据源健康度并写入 snapshot.sources"""
        pushed = _mock_base(monkeypatch, tmp_path)
        # mock 后成功源：指数行情/股指期货/汇率/指数K线/波动率(65条K线) = 5；失败源 9 个
        fc._save_state({"last_direction": "中性", "basis_history": {}, "risk_state": "neutral"})
        fc.run_once(push=True)
        snap = fc._load_state()["snapshot"]
        # P8 后源总数 13→14（分钟K线）
        assert snap["sources"]["total"] == 13  # 期权PCR 影子因子下线（OPTION_PCR_ENABLED=False）后 13 源
        assert snap["sources"]["ok"] == 5

    def test_run_once_alerts_after_three_failed_rounds(self, tmp_path, monkeypatch):
        """连续三轮免费源失败后推送一次健康度告警，后续轮次不重复刷屏。"""
        pushed = _mock_base(monkeypatch, tmp_path)
        for _ in range(4):
            fc.run_once(push=True)

        alerts = [p for p in pushed if p["title"] == "免费数据源连续失败告警"]
        assert len(alerts) == 1
        assert "连续失败 3 轮" in alerts[0]["content"]
        assert "资金流" in alerts[0]["content"]

        state = fc._load_state()
        assert state["source_health"]["资金流"]["consecutive_failures"] == 4
        assert state["source_health"]["资金流"]["alerted"] is True

    def test_build_snapshot_sources_key(self):
        snap = fc.build_snapshot({}, {}, {}, "neutral", sources={"ok": 9, "total": 11})
        assert snap["sources"] == {"ok": 9, "total": 11}
        # 缺省不写键
        snap2 = fc.build_snapshot({}, {}, {}, "neutral")
        assert "sources" not in snap2

    def test_snapshot_block_health_line(self):
        factor_state = {
            "snapshot": {"ts": "2026-08-19 15:00", "risk_state": "neutral",
                         "sources": {"ok": 5, "total": 11}},
            "last_direction": "偏空",
        }
        joined = "\n".join(rtp._snapshot_block(factor_state, "因子环境"))
        assert "数据健康度: ⚠️5/11 源正常" in joined

    def test_snapshot_block_health_ok_no_flag(self):
        factor_state = {
            "snapshot": {"ts": "2026-08-19 15:00", "risk_state": "neutral",
                         "sources": {"ok": 10, "total": 11}},
            "last_direction": "偏空",
        }
        joined = "\n".join(rtp._snapshot_block(factor_state, "因子环境"))
        assert "数据健康度: 10/11 源正常" in joined
        assert "⚠️" not in joined.split("数据健康度")[1].split("\n")[0]

    def test_snapshot_block_weak_signal_line(self):
        factor_state = {
            "snapshot": {"ts": "2026-08-19 15:00", "risk_state": "neutral"},
            "last_direction": "偏空",
            "weak_direction": {"dir": "偏多", "score": 0.56, "ts": "2026-08-19 10:30"},
        }
        joined = "\n".join(rtp._snapshot_block(factor_state, "因子环境"))
        assert "盘中弱信号: 偏多（+0.56，未达强信号门槛，仅供参考）" in joined

    def test_weak_same_as_last_not_shown(self):
        factor_state = {
            "snapshot": {"ts": "2026-08-19 15:00", "risk_state": "neutral"},
            "last_direction": "偏空",
            "weak_direction": {"dir": "偏空", "score": -0.56, "ts": "2026-08-19 10:30"},
        }
        joined = "\n".join(rtp._snapshot_block(factor_state, "因子环境"))
        assert "盘中弱信号" not in joined
