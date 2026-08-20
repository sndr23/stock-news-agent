# filepath: tests/test_p4_factors.py
"""P4 单元测试（2026-08-19）

覆盖：
1. P4-2 fetch_zt_sentiment（涨停池/炸板池解析、回退最近交易日、情绪档位）
   / detect_sentiment_anomalies（炸板率≥50% warning、连板高度≥6 info）
2. P4-3 fetch_sector_flows（东财行业资金流解析、TOP3 排序、单位亿）
3. P4-1 compute_winrate（signal_backtest 胜率提取）
   / format_direction_signal 胜率标注（n≥10 展示、小样本不展示）
4. build_snapshot / format_snapshot 新键（sentiment / sector_flows）
5. real_time_push 三展示端（市场环境行/LLM上下文/简报块）
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import factor_collector as fc  # noqa: E402
import real_time_push as rtp  # noqa: E402
import signal_backtest as sb  # noqa: E402

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送

BJT = timezone(timedelta(hours=8))


def _sample_snapshot(ts=None) -> dict:
    return {
        "ts": ts or datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "risk_state": "neutral",
        "indexes": {"上证指数": {"price": 3930.0, "change_pct": 0.8, "trend": "多头排列"}},
        "basis": {"IC": {"basis_pct": -0.52, "annual_pct": -6.3}},
        "fx": {"美元/日元": {"price": 159.19, "change_pct": -0.43}},
        "sentiment": {"zt": 36, "zb": 12, "zbr": 25.0, "max_lbc": 3,
                      "lbc_dist": "1板29/2板4/3板3", "mood": "低迷"},
        "sector_flows": {
            "inflow": [["煤炭", 11.8], ["焦炭", 6.8], ["银行", 5.2]],
            "outflow": [["软件开发", -15.3], ["互联网服务", -12.1], ["半导体", -9.8]],
        },
    }


# ============================================================
# P4-2: fetch_zt_sentiment
# ============================================================

def _zt_pool_data(tc, lbc_list):
    """构造涨停池 data：tc + pool（每项只含 lbc）"""
    return {"tc": tc, "qdate": 20260819,
            "pool": [{"lbc": n} for n in lbc_list]}


def _mock_pools(monkeypatch, zt_by_date, zb_by_date):
    """按日期 mock 涨停池/炸板池（_http_get 按 url 与 date 分发）"""
    def fake_http(url, params=None, headers=None, encoding=None):
        d = (params or {}).get("date", "")
        if "getTopicZTPool" in url:
            data = zt_by_date.get(d)
        elif "getTopicZBPool" in url:
            data = zb_by_date.get(d)
        else:
            return ""
        return json.dumps({"rc": 0, "data": data})
    monkeypatch.setattr(fc, "_http_get", fake_http)


class TestFetchZtSentiment:
    def test_parse_pools_today(self, monkeypatch):
        today = date.today().strftime("%Y%m%d")
        _mock_pools(monkeypatch,
                    {today: _zt_pool_data(36, [1] * 29 + [2] * 4 + [3] * 3)},
                    {today: {"tc": 12}})
        s = fc.fetch_zt_sentiment()
        assert s["zt"] == 36
        assert s["zb"] == 12
        assert s["zbr"] == round(12 / 48 * 100, 1)  # 25.0
        assert s["max_lbc"] == 3
        assert s["lbc_dist"] == "1板29/2板4/3板3"
        assert s["mood"] == "低迷"  # 36<50 且非冰点/亢奋

    def test_fallback_to_previous_trading_day(self, monkeypatch):
        today = date.today().strftime("%Y%m%d")
        yest = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        _mock_pools(monkeypatch,
                    {today: None, yest: _zt_pool_data(90, [1] * 80 + [6] * 10)},
                    {yest: {"tc": 5}})
        s = fc.fetch_zt_sentiment()
        # 当日无池（盘前/非交易日）→ 回退昨日；涨停90+炸板率5.3% → 亢奋
        assert s["zt"] == 90
        assert s["zb"] == 5
        assert s["zbr"] == 5.3
        assert s["mood"] == "亢奋"
        assert s["max_lbc"] == 6

    def test_all_days_empty(self, monkeypatch):
        _mock_pools(monkeypatch, {}, {})
        assert fc.fetch_zt_sentiment() == {}

    def test_mood_freeze_by_count(self, monkeypatch):
        today = date.today().strftime("%Y%m%d")
        _mock_pools(monkeypatch, {today: _zt_pool_data(25, [1] * 25)}, {today: {"tc": 3}})
        assert fc.fetch_zt_sentiment()["mood"] == "冰点"  # 涨停≤30

    def test_mood_freeze_by_zbr(self, monkeypatch):
        today = date.today().strftime("%Y%m%d")
        _mock_pools(monkeypatch, {today: _zt_pool_data(60, [1] * 60)}, {today: {"tc": 55}})
        # 炸板率 55/(60+55)=47.8% ≥45% → 冰点（且≥50%告警线在异动层另测）
        s = fc.fetch_zt_sentiment()
        assert s["mood"] == "冰点"
        assert s["zbr"] == 47.8

    def test_mood_normal(self, monkeypatch):
        today = date.today().strftime("%Y%m%d")
        _mock_pools(monkeypatch, {today: _zt_pool_data(60, [1] * 55 + [2] * 5)}, {today: {"tc": 4}})
        assert fc.fetch_zt_sentiment()["mood"] == "正常"


class TestDetectSentimentAnomalies:
    def test_zbr_extreme_warning(self):
        sigs = fc.detect_sentiment_anomalies(
            {"zt": 60, "zb": 65, "zbr": 52.0, "max_lbc": 2, "lbc_dist": "", "mood": "冰点"})
        assert len(sigs) == 1
        s = sigs[0]
        assert s["key"] == "sentiment_zbr"
        assert s["level"] == "warning"   # 并入 risk_off 口径
        assert s["direction"] == "bearish"
        assert "炸板率" in s["title"]

    def test_high_ladder_info(self):
        sigs = fc.detect_sentiment_anomalies(
            {"zt": 90, "zb": 10, "zbr": 10.0, "max_lbc": 7, "lbc_dist": "7板1", "mood": "亢奋"})
        assert len(sigs) == 1
        s = sigs[0]
        assert s["key"] == "sentiment_lbc"
        assert s["level"] == "info"      # 投机过热提示，不切 risk_off
        assert s["direction"] == "bullish"

    def test_both_triggered(self):
        sigs = fc.detect_sentiment_anomalies(
            {"zt": 50, "zb": 55, "zbr": 52.4, "max_lbc": 8, "lbc_dist": "", "mood": "冰点"})
        assert {s["key"] for s in sigs} == {"sentiment_zbr", "sentiment_lbc"}

    def test_empty_and_normal(self):
        assert fc.detect_sentiment_anomalies({}) == []
        assert fc.detect_sentiment_anomalies(
            {"zt": 36, "zb": 12, "zbr": 25.0, "max_lbc": 3}) == []


# ============================================================
# P4-3: fetch_sector_flows
# ============================================================

class TestFetchSectorFlows:
    def test_parse_and_rank(self, monkeypatch):
        rows = [
            {"f12": "BK0437", "f14": "煤炭", "f62": 1179380208.0},
            {"f12": "BK1492", "f14": "焦炭", "f62": 681017011.0},
            {"f12": "BK0475", "f14": "银行", "f62": 520000000.0},
            {"f12": "BK0733", "f14": "软件开发", "f62": -1530000000.0},
            {"f12": "BK0366", "f14": "互联网服务", "f62": -1210000000.0},
            {"f12": "BK1036", "f14": "半导体", "f62": -980000000.0},
            {"f12": "BK0001", "f14": "农业", "f62": 100000000.0},
        ]
        text = json.dumps({"data": {"total": 7, "diff": rows}})
        monkeypatch.setattr(fc, "_http_get",
                            lambda url, params=None, headers=None, encoding=None: text)
        out = fc.fetch_sector_flows(top_n=3)
        assert out["inflow"] == [("煤炭", 11.8), ("焦炭", 6.8), ("银行", 5.2)]
        assert out["outflow"] == [("软件开发", -15.3), ("互联网服务", -12.1), ("半导体", -9.8)]

    def test_empty_response(self, monkeypatch):
        monkeypatch.setattr(fc, "_http_get", lambda *a, **k: "")
        assert fc.fetch_sector_flows() == {}
        monkeypatch.setattr(fc, "_http_get", lambda *a, **k: json.dumps({"data": None}))
        assert fc.fetch_sector_flows() == {}


# ============================================================
# P4-1: compute_winrate / format_direction_signal 胜率标注
# ============================================================

class TestComputeWinrate:
    def test_extracts_overall_rates(self, monkeypatch):
        monkeypatch.setattr(sb, "_load_realtime_state",
                            lambda: {"pushed_events": [{"t": "2026-08-19 10:00"}]})
        monkeypatch.setattr(sb, "backtest", lambda events, days=30: {
            "overall": {"n": 20, "n_1": 18, "hit_1": 12,
                        "n_3": 18, "hit_3": 10, "n_5": 15, "hit_5": 9},
        })
        wr = sb.compute_winrate(days=30)
        assert wr["n"] == 20
        assert wr["hit_1"] == round(12 / 18 * 100, 1)
        assert wr["hit_3"] == round(10 / 18 * 100, 1)
        assert wr["hit_5"] == round(9 / 15 * 100, 1)

    def test_missing_horizon_none(self, monkeypatch):
        monkeypatch.setattr(sb, "_load_realtime_state", lambda: {"pushed_events": [{}]})
        monkeypatch.setattr(sb, "backtest", lambda events, days=30: {
            "overall": {"n": 3, "n_1": 2, "hit_1": 1},
        })
        wr = sb.compute_winrate()
        assert wr["n"] == 3
        assert wr["hit_1"] == 50.0
        assert wr["hit_3"] is None and wr["hit_5"] is None

    def test_no_events(self, monkeypatch):
        monkeypatch.setattr(sb, "_load_realtime_state", lambda: {})
        assert sb.compute_winrate() == {"n": 0}


def _sample_analysis(direction="偏空", score=-0.67):
    return {
        "direction": direction, "score": score,
        "factors": [("汇率", -1.0, "日元急升 -2.00%（套息平仓风险）"),
                    ("风险", -1.0, "风险收缩期"), ("宽度", -1.0, "极端普跌（92%个股下跌）")],
    }


class TestDirectionSignalWinrate:
    def test_winrate_appended_when_enough_samples(self):
        wr = {"n": 45, "hit_1": 68.9, "hit_3": 71.1, "hit_5": 73.3}
        out = fc.format_direction_signal(_sample_analysis(), "中性", winrate=wr)
        assert "信号可信度" in out
        assert "后1日 69%" in out
        assert "后3日 71%" in out
        assert "n=45" in out

    def test_small_sample_suppressed(self):
        wr = {"n": 5, "hit_1": 80.0, "hit_3": None, "hit_5": None}
        out = fc.format_direction_signal(_sample_analysis(), "中性", winrate=wr)
        assert "信号可信度" not in out

    def test_none_winrate_backward_compatible(self):
        out = fc.format_direction_signal(_sample_analysis(), "中性")
        assert "信号可信度" not in out
        assert "量化方向：利空" in out

    def test_partial_horizons(self):
        wr = {"n": 12, "hit_1": 66.7, "hit_3": None, "hit_5": 58.3}
        out = fc.format_direction_signal(_sample_analysis(), "中性", winrate=wr)
        assert "后1日 67%" in out and "后5日 58%" in out
        assert "后3日" not in out


# ============================================================
# 快照层：build_snapshot / format_snapshot
# ============================================================

class TestSnapshotP4Keys:
    def test_build_snapshot_contains_p4_keys(self):
        tech = {"上证指数": {"available": True, "price": 3930.0, "change_pct": 0.8, "trend": "多头"}}
        snap = fc.build_snapshot(tech, {}, {}, "neutral",
                                 sentiment={"zt": 36, "zb": 12, "zbr": 25.0, "max_lbc": 3,
                                            "lbc_dist": "1板29/2板4/3板3", "mood": "低迷"},
                                 sector_flows={"inflow": [("煤炭", 11.8)], "outflow": []})
        assert snap["sentiment"]["mood"] == "低迷"
        assert snap["sentiment"]["zt"] == 36
        assert snap["sector_flows"]["inflow"] == [["煤炭", 11.8]]  # tuple → list（JSON 可序列化）
        assert snap["sector_flows"]["outflow"] == []

    def test_build_snapshot_without_p4(self):
        snap = fc.build_snapshot({}, {}, {}, "neutral")
        assert "sentiment" not in snap
        assert "sector_flows" not in snap

    def test_format_snapshot_lines(self):
        out = fc.format_snapshot({}, {}, {}, flows={"main_net_yi": -194.0},
                                 sentiment={"zt": 36, "zb": 12, "zbr": 25.0, "max_lbc": 3,
                                            "lbc_dist": "1板29/2板4/3板3", "mood": "低迷"},
                                 sector_flows={"inflow": [("煤炭", 11.8), ("焦炭", 6.8)],
                                               "outflow": [("软件开发", -15.3)]})
        assert "涨停情绪：低迷" in out
        assert "涨停 36 家" in out
        assert "炸板率 25%" in out
        assert "行业主力净流入 TOP：煤炭 +11.8亿 / 焦炭 +6.8亿" in out
        assert "净流出 TOP：软件开发 -15.3亿" in out

    def test_format_snapshot_euphoria_flag(self):
        out = fc.format_snapshot({}, {}, {},
                                 sentiment={"zt": 90, "zb": 5, "zbr": 5.3, "max_lbc": 6,
                                            "lbc_dist": "6板1", "mood": "亢奋"})
        assert "🔥亢奋" in out


# ============================================================
# real_time_push 三展示端
# ============================================================

class TestRealtimePushP4Display:
    def test_env_line_extreme_mood(self):
        snap = _sample_snapshot()
        snap["sentiment"]["mood"] = "冰点"
        line = rtp._factor_env_line(snap)
        assert "❄️情绪冰点" in line

    def test_env_line_normal_mood_shown(self):
        snap = _sample_snapshot()  # mood=低迷
        line = rtp._factor_env_line(snap)
        assert "情绪低迷" in line  # P10：情绪档位始终显示
        assert "市场环境" in line

    def test_llm_context_sentiment_and_sector_flows(self):
        ctx = rtp._llm_env_context(_sample_snapshot())
        assert "涨停情绪低迷" in ctx
        assert "涨停36家" in ctx
        assert "连板高度3" in ctx
        assert "主力净流入行业：煤炭+11.8亿" in ctx
        assert "主力净流出行业：软件开发-15.3亿" in ctx

    def test_snapshot_block_p4_lines(self):
        factor_state = {"snapshot": _sample_snapshot(), "last_direction": "偏空"}
        lines = rtp._snapshot_block(factor_state, "因子环境")
        joined = "\n".join(lines)
        assert "涨停情绪: 低迷｜涨停36（连板高度3）｜炸板率25%" in joined
        assert "行业资金: 流入 煤炭 +11.8亿、焦炭 +6.8亿、银行 +5.2亿" in joined
        assert "量化综合方向: 偏空" in joined

    def test_snapshot_block_without_p4_backward_compatible(self):
        snap = _sample_snapshot()
        snap.pop("sentiment")
        snap.pop("sector_flows")
        lines = rtp._snapshot_block({"snapshot": snap}, "因子环境")
        joined = "\n".join(lines)
        assert "涨停情绪" not in joined
        assert "行业资金" not in joined
        assert "风险状态" in joined  # 既有行不受影响
